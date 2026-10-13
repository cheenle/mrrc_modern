#!/usr/bin/env python3
"""标定快路径的 SNR 估计（``ft8/dsp/wsjt_fast.c`` 的 ``fast_snr_db``）。

为什么要这个脚本：快路径原本照抄上游的 ``score / 2`` —— 那是归一化的 Costas
同步分，**会饱和**（≥ −6 dB 整个区间压在 17 不动），而 sequencer 会把它当作
"收你多少 dB"发到空中。修法是直接从 ft8_lib 的水声图（本身就是功率 dB）在解出
的音位置上量信噪比。它有一个经验常数 ``FAST_SNR_CALIBRATION_DB`` 需要定标，
基准就是同一仓里的 ``standard`` 路径 —— WSJT-X 的真 dB 估计。

用法::

    .venv/bin/python tools/ft8/calibrate_fast_snr.py

输出一张 真实 SNR / fast / standard 的对照表与残差统计。改常数后就跑这个：
残差中位数应该落在 0 附近。

**电平必须先归一化到不削顶。** 直接 `np.clip(buffer + noise, …)` 会在弱信号档把噪声
推到 ±1.0：−18 dB 那档噪声 σ 是信号的 6.8 倍满量程，整个时隙被削成方波。削顶**降低**
噪声功率（信号被 erf 线性化保留、噪声功率却卡在 32768²），于是有效 SNR 被抬高，两条
路径都恰好读回真值 —— 在一个失真的输入上拟合出来的常数，看起来是最准的。所以下面
按 0.9 满量程归一化：信号与噪声同乘一个因子，SNR 不变，削顶不参与测量。
"""

from __future__ import annotations

import math
import os
import sys

# FT8 铁律：在任何 NumPy / libwsjt_core 接触之前设好栈大小
os.environ.setdefault("OMP_STACKSIZE", "10M")

import numpy as np  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from ft8.core.binding import CoreBinding  # noqa: E402
from ft8.core.models import DecodeConfig, DecodePath  # noqa: E402

LIB = os.path.join(REPO, "ft8", "dsp", "build", "libwsjt_core.so")
if not os.path.exists(LIB):
    LIB = os.path.join(REPO, "ft8", "dsp", "build", "libwsjt_core.dylib")

TX_SAMPLES = 606_720
RX_SAMPLES = 180_000
SLOT_OFFSET = 6_000  # 0.5 s into the slot — the FT8 convention
LEVELS_DB = (24, 18, 12, 6, 0, -6, -12, -18, -21)
#: 归一化后的峰值上限（满量程的比例）。留 10% 余量，量化噪声碰不到 ±1.0。
HEADROOM = 0.9
MESSAGE = "CQ K1ABC FN42"


def _config(path: DecodePath) -> DecodeConfig:
    return DecodeConfig(
        sample_rate=12_000,
        sample_count=RX_SAMPLES,
        profile=3,
        threads=3,
        cycles=1,
        sensitivity=1,
        path=path,
        low_frequency=200,
        high_frequency=3000,
    )


def main() -> int:
    binding = CoreBinding(LIB)
    wave = np.zeros(TX_SAMPLES, dtype=np.float32)
    binding.encode(MESSAGE, 1500.0, 48_000, wave)
    signal = wave.reshape(-1, 4).mean(axis=1)  # 48 kHz -> 12 kHz
    print(f"lib: {LIB}")
    print(f"encoded {MESSAGE!r}: {len(signal)/12_000:.2f} s @ 12 kHz")

    peak = float(np.abs(signal).max())
    unit = signal * (12_000.0 / peak)
    rng = np.random.default_rng(20261013)

    print(f"\n{'true':>6} {'fast':>6} {'std':>6} {'resid':>6}")
    residuals = []
    for level in LEVELS_DB:
        buffer = np.zeros(RX_SAMPLES, dtype=np.float32)
        buffer[SLOT_OFFSET:SLOT_OFFSET + len(unit)] = unit
        signal_power = float(np.mean(buffer**2))
        # SNR is referenced to the 2500 Hz bandwidth; the noise here is
        # white over the whole 6 kHz the 12 kHz sample rate carries.
        noise_power = signal_power / (10 ** (level / 10.0)) * (6000.0 / 2500.0)
        noise = rng.normal(0.0, math.sqrt(noise_power), RX_SAMPLES).astype(np.float32)
        # 同乘一个因子 ⇒ SNR 不变；不削顶 ⇒ 量到的是估算器本身，不是限幅器。
        mix = buffer + noise
        headroom = float(np.max(np.abs(mix)))
        if headroom > HEADROOM * 32768.0:
            mix = mix * (HEADROOM * 32768.0 / headroom)
        samples = np.clip(np.rint(mix), -32768, 32767).astype(np.int16)

        row = []
        for path in (DecodePath.FAST, DecodePath.STANDARD):
            batch = binding.decode(samples, _config(path), 1)
            hits = [r for r in batch.results if "K1ABC" in r.text]
            row.append(hits[0].snr if hits else None)
        fast, standard = row
        resid = "n/a" if fast is None or standard is None else f"{fast - standard:+d}"
        if isinstance(resid, str) and resid != "n/a":
            residuals.append(fast - standard)
        print(f"{level:>6} {str(fast):>6} {str(standard):>6} {resid:>6}")

    if residuals:
        arr = np.array(residuals, dtype=float)
        print(f"\nresidual (fast - standard): median {np.median(arr):+.1f} "
              f"mean {arr.mean():+.2f} min {arr.min():+.0f} max {arr.max():+.0f}")
        print(f"=> FAST_SNR_CALIBRATION_DB should be {-np.median(arr):+.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
