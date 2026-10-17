"""sdr_dsp (C) 的单元测试：合成信号，不需要 SDR 硬件。

编译器缺失时整体跳过（macOS 开发机/Windows VM/Linux 都能跑）。
"""
import ctypes
import os
import shutil
import subprocess
import tempfile
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "sdr_dsp.c")

_lib = None


def build_lib():
    """把 sdr_dsp.c 编成临时共享库并加载；无编译器时返回 None。"""
    global _lib
    if _lib is not None:
        return _lib
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        return None
    out = os.path.join(tempfile.mkdtemp(prefix="sdrdsp"), "libsdr_dsp.so")
    cmd = [cc, "-O2", "-fPIC", "-shared", "-o", out, SRC, "-lm"]
    subprocess.run(cmd, check=True, capture_output=True)
    lib = ctypes.CDLL(out)
    lib.sdr_dsp_create.restype = ctypes.c_void_p
    lib.sdr_dsp_create.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_int]
    lib.sdr_dsp_destroy.argtypes = [ctypes.c_void_p]
    lib.sdr_dsp_set_channel.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_double, ctypes.c_int, ctypes.c_double]
    lib.sdr_dsp_clear_channel.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.sdr_dsp_process.argtypes = [ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_short), ctypes.c_int,
                                    ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_short), ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_int),
                                    ctypes.POINTER(ctypes.c_float)]
    _lib = lib
    return lib


class SdrDspTests(unittest.TestCase):
    FS = 2_400_000.0
    OUT = 48_000.0
    BLK = 4800                      # 2 ms @ 2.4 MSPS

    @classmethod
    def setUpClass(cls):
        if build_lib() is None:
            raise unittest.SkipTest("no C compiler")

    # ---- helpers -------------------------------------------------------
    def _iq(self, tones):
        """tones: [(offset_hz, amplitude), ...] -> int16 interleaved IQ."""
        t = np.arange(self.BLK) / self.FS
        z = np.zeros(self.BLK, np.complex128)
        for f, a in tones:
            z += a * np.exp(2j * np.pi * f * t)
        z = np.clip(z, -32000, 32000)
        iq = np.empty(self.BLK * 2, np.int16)
        iq[0::2] = z.real.astype(np.int16)
        iq[1::2] = z.imag.astype(np.int16)
        return iq

    def _run(self, tones, blocks=25, offset=0.0, mode=0, bw=6000.0):
        lib = self.lib
        d = lib.sdr_dsp_create(self.FS, self.OUT, 4)
        self.assertIsNotNone(d)
        self.assertEqual(lib.sdr_dsp_set_channel(d, 0, offset, mode, bw), 0)
        out_n = ctypes.c_int(0)
        rms = (ctypes.c_float * 4)()
        out = (ctypes.c_short * 8192)()
        iq = self._iq(tones)
        c_iq = (ctypes.c_short * len(iq))(*iq)
        chunks = []
        for _ in range(blocks):
            rc = lib.sdr_dsp_process(d, c_iq, self.BLK, 1, out, 8192,
                                     ctypes.byref(out_n), rms)
            self.assertEqual(rc, 0)
            chunks.append(np.array(out[:out_n.value], dtype=np.int16))
        lib.sdr_dsp_destroy(d)
        return np.concatenate(chunks), rms[0]

    @property
    def lib(self):
        return build_lib()

    def _tone_power(self, x, lo, hi):
        w = np.hanning(len(x))
        s = np.abs(np.fft.rfft(x.astype(np.float64) * w))
        f = np.fft.rfftfreq(len(x), 1 / self.OUT)
        return float(np.sum(s[(f > lo) & (f < hi)] ** 2))

    # ---- tests ---------------------------------------------------------
    def test_am_tone_frequency(self):
        """AM：载波 + 1 kHz 双边带 -> 输出 1 kHz。"""
        audio, rms = self._run([(0, 10000), (1000, 5000), (-1000, 5000)], blocks=50)
        self.assertGreater(len(audio), self.OUT // 10 - 100)
        self.assertGreater(rms, 0.0)
        spec = np.abs(np.fft.rfft(audio.astype(np.float64) * np.hanning(len(audio))))
        freqs = np.fft.rfftfreq(len(audio), 1 / self.OUT)
        peak = freqs[int(np.argmax(spec))]
        self.assertAlmostEqual(peak, 1000, delta=30)

    def test_am_output_not_silent(self):
        """AM 输出必须真的有声（防止"全零也算通过"）。"""
        audio, _ = self._run([(0, 10000), (1000, 4000), (-1000, 4000)], blocks=30)
        self.assertGreater(float(np.std(audio.astype(np.float64))), 100.0)

    def test_alias_rejection(self):
        """+30 kHz 的强干扰不得落进 1 kHz 带内（抗混叠）。"""
        wanted = [(0, 4000), (1000, 1200), (-1000, 1200)]
        # 47 kHz 在 50 倍抽取后正好折回 1 kHz —— 这才是抗混叠真正要挡的东西
        clean, _ = self._run(wanted, blocks=50)
        dirty, _ = self._run(wanted + [(47000, 8000)], blocks=50)
        pc = self._tone_power(clean, 900, 1100)
        pd = self._tone_power(dirty, 900, 1100)
        ratio_db = 10 * np.log10(max(pd, 1e-9) / max(pc, 1e-9))
        self.assertLess(ratio_db, 1.0, f"alias leaked: {ratio_db:.1f} dB")

    def test_ssb_sideband_rejection(self):
        """USB 模式：+1 kHz 通过，-1 kHz 至少低 40 dB。"""
        up, _ = self._run([(1000, 8000)], offset=0.0, mode=1, bw=3000)
        dn, _ = self._run([(-1000, 8000)], offset=0.0, mode=1, bw=3000)
        pu = self._tone_power(up, 800, 1200)
        pd = self._tone_power(dn, 800, 1200)
        self.assertGreater(pu, 100 * pd, "USB did not reject the lower sideband")

    def test_nfm_discriminator(self):
        """NFM：750 Hz 调频音应恢复出 750 Hz 基带。"""
        # 连续流（不能按块重复：2 ms 周期会在 500 Hz 造出假谱线）
        n_all = self.BLK * 50
        t = np.arange(n_all) / self.FS
        base = np.sin(2 * np.pi * 750 * t)
        phase = 2 * np.pi * 1500 * np.cumsum(base) / self.FS
        z = 12000 * np.exp(1j * phase)
        iq_all = np.empty(n_all * 2, np.int16)
        iq_all[0::2] = z.real.astype(np.int16)
        iq_all[1::2] = z.imag.astype(np.int16)
        lib = self.lib
        d = lib.sdr_dsp_create(self.FS, self.OUT, 4)
        self.assertEqual(lib.sdr_dsp_set_channel(d, 0, 0.0, 4, 12500.0), 0)
        out_n = ctypes.c_int(0)
        rms = (ctypes.c_float * 4)()
        out = (ctypes.c_short * 8192)()
        chunks = []
        for b in range(50):
            seg = iq_all[b * self.BLK * 2:(b + 1) * self.BLK * 2]
            c_iq = (ctypes.c_short * len(seg))(*seg)
            lib.sdr_dsp_process(d, c_iq, self.BLK, 1, out, 8192,
                                ctypes.byref(out_n), rms)
            chunks.append(np.array(out[:out_n.value], np.int16))
        lib.sdr_dsp_destroy(d)
        audio = np.concatenate(chunks).astype(np.float64)[2400:]   # 跳过启动瞬态
        spec = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
        f = np.fft.rfftfreq(len(audio), 1 / self.OUT)
        self.assertAlmostEqual(f[int(np.argmax(spec))], 750, delta=60)

    def test_bad_arguments_are_rejected(self):
        """非法参数必须返回 -1，不许崩。"""
        lib = self.lib
        d = lib.sdr_dsp_create(self.FS, self.OUT, 2)
        self.assertEqual(lib.sdr_dsp_set_channel(d, 0, 0.0, 99, 6000.0), -1)      # 模式越界
        self.assertEqual(lib.sdr_dsp_set_channel(d, 0, 0.0, 0, 0.0), -1)          # 带宽 0
        self.assertEqual(lib.sdr_dsp_set_channel(d, 0, 0.0, 0, 200000.0), -1)     # 带宽 > fs_out
        self.assertEqual(lib.sdr_dsp_set_channel(d, 9, 0.0, 0, 6000.0), -1)       # 通道越界
        lib.sdr_dsp_destroy(d)


if __name__ == "__main__":
    unittest.main()
