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
                + F.encode_audio(3, 9, b"opus-packet")
                + F.encode_command({"cmd": "tune", "hz": 1}))
        frames = p.feed(blob)
        self.assertEqual([f[0] for f in frames], ["status", "spectrum", "audio", "command"])
        self.assertEqual(frames[0][1]["center_hz"], 7_074_000)
        self.assertEqual(frames[1][1:], (7, bytes(range(16))))
        self.assertEqual(frames[2][1:], (3, 9, b"opus-packet"))
        self.assertEqual(frames[3][1]["cmd"], "tune")
        self.assertEqual(p.errors, 0)

    def test_partial_feeds_are_buffered(self):
        p = F.FrameParser()
        blob = F.encode_audio(1, 2, b"abcdef")
        got = []
        for i in range(len(blob)):
            got += p.feed(blob[i:i + 1])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1:], (1, 2, b"abcdef"))

    def test_unknown_tag_resyncs(self):
        p = F.FrameParser()
        frames = p.feed(b"\x00\xffjunk" + F.encode_spectrum(1, b"\x01\x02"))
        self.assertTrue(any(f[0] == "spectrum" for f in frames))
        self.assertGreater(p.errors, 0)
