"""sdr_dsp_binding 的测试：库存在就跑，缺失则跳过（桌面版路径）。"""
import os
import unittest

import numpy as np

from sdr_dsp_binding import Channelizer, MODE_AM
from tests.test_sdr_dsp import build_lib_path


class BindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = build_lib_path()
        if path is None:
            raise unittest.SkipTest("no C compiler / lib")
        os.environ["MRRC_SDR_DSP_LIB"] = path

    def test_locates_the_library(self):
        self.assertIsNotNone(Channelizer.locate())

    def test_missing_library_is_graceful(self):
        old = os.environ.get("MRRC_SDR_DSP_LIB")
        os.environ["MRRC_SDR_DSP_LIB"] = "/nonexistent/libsdr_dsp.so"
        try:
            self.assertIsNone(Channelizer.locate())
        finally:
            if old is not None:
                os.environ["MRRC_SDR_DSP_LIB"] = old

    def test_process_returns_pcm_and_rms(self):
        with Channelizer(2_400_000.0, 48000.0, 4) as cz:
            cz.set_channel(0, 0.0, MODE_AM, 6000.0)
            t = np.arange(4800) / 2_400_000.0
            z = (12000 * np.exp(2j * np.pi * 1000 * t)).astype(np.complex64)
            iq = np.empty(9600, np.int16)
            iq[0::2] = z.real
            iq[1::2] = z.imag
            # 首块没有历史，滤波器要吃掉一个窗长；跑几块后应稳定在 96 样本（2 ms @ 48 kHz）
            for _ in range(4):
                pcm, rms = cz.process(iq, 1)
            self.assertEqual(len(pcm), 1)
            self.assertEqual(len(pcm[0]) % 2, 0)
            self.assertEqual(len(pcm[0]) // 2, 96)
            self.assertGreater(rms[0], 0.0)

    def test_bad_channel_raises(self):
        with Channelizer(2_400_000.0, 48000.0, 2) as cz:
            with self.assertRaises(ValueError):
                cz.set_channel(0, 0.0, MODE_AM, 48000.0)   # 带宽 >= 输出率

    def test_unsupported_rate_pair_raises(self):
        with self.assertRaises(ValueError):
            Channelizer(2_400_100.0, 48000.0, 2)          # 非整数抽取


if __name__ == "__main__":
    unittest.main()
