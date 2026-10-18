"""sdr_pipe 的离线模式测试：喂 IQ 文件，断言帧序列。不需要 SDR 硬件。"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy as np

import sdr_frames as F
from tests.test_sdr_dsp import build_lib_path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class OfflinePipeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lib = build_lib_path()
        if lib is None:
            raise unittest.SkipTest("no C compiler / libsdr_dsp")
        cls.env = dict(os.environ, MRRC_SDR_DSP_LIB=lib)

    def _iq_file(self, seconds=0.2):
        n = int(2_400_000 * seconds)
        t = np.arange(n) / 2_400_000.0
        z = 12000 * (np.exp(2j * np.pi * 1000 * t) + 0.5 * np.exp(2j * np.pi * 2500 * t))
        iq = np.empty(n * 2, np.int16)
        iq[0::2] = np.clip(z.real, -32000, 32000).astype(np.int16)
        iq[1::2] = np.clip(z.imag, -32000, 32000).astype(np.int16)
        path = os.path.join(tempfile.mkdtemp(), "iq.iq")
        iq.tofile(path)
        return path

    def _run(self, extra=()):
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "sdr_pipe.py"),
             "--offline", self._iq_file(), "--seconds", "0.2", "--no-rt", *extra],
            cwd=ROOT, env=self.env, capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-800:])
        parser = F.FrameParser()
        return parser.feed(proc.stdout), parser

    def test_emits_status_spectrum_and_audio(self):
        frames, parser = self._run()
        self.assertEqual(parser.errors, 0)
        kinds = {f[0] for f in frames}
        self.assertIn("status", kinds)
        self.assertIn("spectrum", kinds)
        if any(f[0] == "status" and f[1].get("opus") for f in frames):
            self.assertIn("audio", kinds)
        status = [f[1] for f in frames if f[0] == "status"][-1]
        self.assertIn("center_hz", status)
        self.assertTrue(status.get("offline"))

    def test_spectrum_bins_have_the_documented_size(self):
        frames, _ = self._run()
        specs = [f for f in frames if f[0] == "spectrum"]
        self.assertGreater(len(specs), 5)
        self.assertEqual(len(specs[0][2]), 1024)
        self.assertGreater(max(specs[0][2]), 0)

    def test_no_channels_means_no_audio(self):
        frames, _ = self._run(extra=("--channels", ""))
        self.assertNotIn("audio", {f[0] for f in frames})
        self.assertIn("spectrum", {f[0] for f in frames})


if __name__ == "__main__":
    unittest.main()


class FrameCodecTests(unittest.TestCase):
    """进程边界的契约：编码/解析/部分喂入/坏字节重对齐。"""

    def test_round_trip_all_frame_types(self):
        p = F.FrameParser()
        blob = (F.encode_status({"center_hz": 7_074_000})
                + F.encode_spectrum(7, bytes(range(16)))
                + F.encode_audio(3, 9, b"opus-packet", 0.42)
                + F.encode_command({"cmd": "tune", "hz": 1}))
        frames = p.feed(blob)
        self.assertEqual([f[0] for f in frames], ["status", "spectrum", "audio", "command"])
        self.assertEqual(frames[0][1]["center_hz"], 7_074_000)
        self.assertEqual(frames[1][1:], (7, bytes(range(16))))
        self.assertEqual(frames[2][1:3], (3, 9))
        self.assertAlmostEqual(frames[2][3], 0.42, delta=0.01)
        self.assertEqual(frames[2][4], b"opus-packet")
        self.assertEqual(frames[3][1]["cmd"], "tune")
        self.assertEqual(p.errors, 0)

    def test_partial_feeds_are_buffered(self):
        p = F.FrameParser()
        blob = F.encode_audio(1, 2, b"abcdef", 0.5)
        got = []
        for i in range(len(blob)):
            got += p.feed(blob[i:i + 1])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1:3], (1, 2))
        self.assertEqual(got[0][4], b"abcdef")

    def test_unknown_tag_resyncs(self):
        p = F.FrameParser()
        frames = p.feed(b"\x00\xffjunk" + F.encode_spectrum(1, b"\x01\x02"))
        self.assertTrue(any(f[0] == "spectrum" for f in frames))
        self.assertGreater(p.errors, 0)


class AudioFrameRegressionTests(unittest.TestCase):
    """真机抓到的缺陷：A1 帧的 seq 曾经按 u8 打包（<BBHH），序号过 255 就抛
    struct.error，整条 pipe 退出、接收机变成不可用。布局是 chid u8 / seq u16 /
    level u8 / len u16 —— 三个地方（编码、解析、服务端 WS 帧）必须一致。"""

    def test_sequence_numbers_above_255_round_trip(self):
        blob = F.encode_audio(2, 65_000, b"x" * 10, 0.5)
        frames = F.FrameParser().feed(blob)
        self.assertEqual(len(frames), 1)
        _, chid, seq, level, payload = frames[0]
        self.assertEqual((chid, seq, len(payload)), (2, 65_000, 10))
        self.assertAlmostEqual(level, 0.5, delta=0.01)

    def test_level_is_clamped(self):
        for rms, want in ((2.0, 255), (-1.0, 0), (0.0, 0)):
            blob = F.encode_audio(0, 1, b"z", rms)
            _, _, _, level, _ = F.FrameParser().feed(blob)[0]
            self.assertAlmostEqual(level, want / 255.0, delta=0.01)

    def test_server_ws_frame_matches_the_same_layout(self):
        import server
        kind, payload = server._sdr_ws_encode(("audio", 1, 40_000, 0.25, b"ab"))
        self.assertEqual(kind, "bytes")
        self.assertEqual(payload[0], 0x02)
        chid, seq, level, ln = __import__("struct").unpack_from("<BHBH", payload, 1)
        self.assertEqual((chid, seq, ln), (1, 40_000, 2))
        self.assertEqual(payload[7:], b"ab")   # 0x02 + chid/seq/level/len = 7 字节头
