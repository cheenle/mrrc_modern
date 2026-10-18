"""libsdr_dsp 的 ctypes 包装。

`sdr_pipe.py` 是唯一的使用者：每 20 ms 块喂一次 IQ，拿回每通道的 PCM 与 RMS。
库缺失时 `locate()` 返回 None（桌面版 `MRRC_SDR=off` 就走这条路，不抛异常）。

设计见 docs/superpowers/specs/2026-10-18-sdr-receiver-design.md
"""

from __future__ import annotations

import ctypes
import os

import numpy as np

# 与 sdr_dsp.h 的 sdr_mode_t 一一对应
MODE_AM, MODE_USB, MODE_LSB, MODE_CW, MODE_NFM, MODE_WFM = range(6)

STRIDE = 4096          # 每通道 PCM 缓冲（48 kHz 下约 85 ms，远大于 20 ms 块）


def _repo_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


class Channelizer:
    """把 2.4/4.8 MSPS 的 IQ 变成每通道 48 kHz PCM。"""

    _lib_cache: dict[str, ctypes.CDLL] = {}

    def __init__(self, fs_in: float, fs_out: float = 48000.0, max_ch: int = 32):
        path = self.locate()
        if path is None:
            raise RuntimeError("libsdr_dsp not found (set MRRC_SDR_DSP_LIB)")
        lib = self._lib_cache.get(path)
        if lib is None:
            lib = ctypes.CDLL(path)
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
            lib.sdr_dsp_process.restype = ctypes.c_int
            self._lib_cache[path] = lib
        self._lib = lib
        self.max_ch = max_ch
        self._d = lib.sdr_dsp_create(float(fs_in), float(fs_out), int(max_ch))
        if not self._d:
            raise ValueError(f"unsupported rate pair: fs_in={fs_in} fs_out={fs_out}")
        self._out = np.zeros(max_ch * STRIDE, dtype=np.int16)
        self._rms = (ctypes.c_float * max_ch)()
        self._out_n = ctypes.c_int(0)

    # -- 生命周期 ---------------------------------------------------------
    def close(self) -> None:
        if getattr(self, "_d", None):
            self._lib.sdr_dsp_destroy(self._d)
            self._d = None

    def __del__(self):                                  # pragma: no cover - GC 路径
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- 配置 -------------------------------------------------------------
    def set_channel(self, chid: int, offset_hz: float, mode: int, bw_hz: float) -> None:
        rc = self._lib.sdr_dsp_set_channel(self._d, int(chid), float(offset_hz),
                                           int(mode), float(bw_hz))
        if rc != 0:
            raise ValueError(f"set_channel rejected: offset={offset_hz} mode={mode} bw={bw_hz}")

    def clear_channel(self, chid: int) -> None:
        self._lib.sdr_dsp_clear_channel(self._d, int(chid))

    # -- 数据 -------------------------------------------------------------
    def process(self, iq: np.ndarray, nchan: int) -> tuple[list[bytes], list[float]]:
        """iq: 交错 int16 IQ。返回 (每通道 PCM bytes, 每通道 rms)。"""
        buf = np.ascontiguousarray(iq, dtype=np.int16)
        n = int(buf.size // 2)
        ptr = buf.ctypes.data_as(ctypes.POINTER(ctypes.c_short))
        out_ptr = self._out.ctypes.data_as(ctypes.POINTER(ctypes.c_short))
        rc = self._lib.sdr_dsp_process(self._d, ptr, n, int(nchan), out_ptr, STRIDE,
                                       ctypes.byref(self._out_n), self._rms)
        if rc != 0:
            raise RuntimeError(f"sdr_dsp_process failed ({rc})")
        nout = int(self._out_n.value)
        pcm = [self._out[c * STRIDE:c * STRIDE + nout].tobytes() for c in range(nchan)]
        return pcm, [float(self._rms[c]) for c in range(nchan)]

    # -- 定位 -------------------------------------------------------------
    @staticmethod
    def locate() -> str | None:
        """MRRC_SDR_DSP_LIB → 仓库根 → /usr/local/lib。找不到返回 None。"""
        env = os.environ.get("MRRC_SDR_DSP_LIB")
        if env:
            return env if os.path.exists(env) else None
        for name in ("libsdr_dsp.so", "libsdr_dsp.dylib", "sdr_dsp.dll"):
            cand = os.path.join(_repo_root(), name)
            if os.path.exists(cand):
                return cand
        for cand in ("/usr/local/lib/libsdr_dsp.so", "/usr/lib/libsdr_dsp.so"):
            if os.path.exists(cand):
                return cand
        return None
