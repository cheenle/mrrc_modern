"""快路径报出的 ``snr`` 必须是真实的 dB。

现场缺陷 B（2026-10-13）：快路径原本照抄上游 demo 的 ``score / 2`` —— 那是归一化
的 Costas 同步分，**饱和**（合成扫频实测：真值 ≥ −6 dB 的整个区间都读 17）。它不是
显示问题：``orchestrator.py`` 把 ``snr`` 喂给 sequencer，sequencer 把它编进**实际
发射的报文**（``sequencer.py:180/234/287``，REPORT / ROGER_REPORT），所以一个饱和的
同步分会被当作"收你多少 dB"发到空中、写进 QSO 日志。

修法是快路径直接从 ft8_lib 的水声图（``WF_ELEM_MAG`` 本身就是功率 dB）在解出的 79
个音位置上量信噪比。标定工具是 ``tools/ft8/calibrate_fast_snr.py``（人工跑，输出残差
表）；本文件是它的自动化回归守卫 —— 工具会被人忘记跑，测试不会。

**这里刻意不比对具体数字。** 断言"0 dB 输入必须读 0"会跟 ``FAST_SNR_CALIBRATION_DB``
一起漂，改常数就得改测试，于是守卫慢慢变成橡皮图章。要钉的是**性质**：估计值随真实
SNR 单调跟随、且落在真值的窄带内。饱和的旧实现四个电平全读同一个值，任何丢掉量纲
（除以 2、取绝对值、截断到非负）的改动也过不去。
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parents[3]

#: 真值 → 允许的偏差。标定后实测残差是 0 dB（+24…−12 dB 全程，−18 dB 处 +1），
#: 留 3 dB 是给 int16 量化、随机噪声的种子敏感性、以及日后微调标定常数的余地。
#: 弱信号端两条路径会一起偏乐观（standard 在 −18 dB 自己也 +1），所以别按
#: 最弱档去收紧这个数 —— 那不是本估计的偏差。
TOLERANCE_DB = 3.0

LEVELS_DB = (6.0, 0.0, -6.0, -12.0, -18.0)

#: 归一化后的峰值上限（满量程的比例），与标定工具同值。
HEADROOM = 0.9


def _noisy_slot(clean: np.ndarray, level_db: float, rng: np.random.Generator) -> np.ndarray:
    """把已知的 12 kHz 时隙加白噪声到给定的 SNR（2500 Hz 参考带宽）。

    与 ``tools/ft8/calibrate_fast_snr.py`` 的构造逐字相同 —— 两种算法对不上，
    这里量的就不是 WSJT-X 定义的那个 SNR。

    峰值必须先归一化到不削顶：conftest 的 ``cq_slot`` 是 0.73 满量程，−18 dB 那档
    噪声 σ 是它的 6.8 倍满量程，整个时隙会被削成方波。削顶降低噪声功率（信号被
    erf 线性化保留、噪声功率卡在满量程），于是有效 SNR 被抬高、两条路径都恰好读回
    真值 —— 那样量到的是限幅器，不是估算器。信号与噪声同乘一个因子，SNR 不变。
    """

    base = clean.astype(np.float32) / 32_768.0
    signal_power = float(np.mean(base.astype(np.float64) ** 2))
    # 12 kHz 采样携带的白噪声铺满 6 kHz，而 SNR 定义在 2500 Hz 上。
    noise_power = signal_power / (10.0 ** (level_db / 10.0)) * (6000.0 / 2500.0)
    noise = rng.normal(0.0, math.sqrt(noise_power), base.size)
    mix = base + noise
    peak = float(np.max(np.abs(mix)))
    if peak > HEADROOM:
        mix = mix * (HEADROOM / peak)
    return np.clip(np.rint(mix * 32_768.0), -32_768, 32_767).astype(np.int16)


def _fast_snr(call_fast, raw_library, samples: np.ndarray) -> int:
    status, results, count, overflow = call_fast(raw_library, samples)
    assert status == 0
    assert overflow == 0
    hits = [
        results[i].snr
        for i in range(count)
        if "K1ABC" in results[i].text.decode().rstrip("\x00")
    ]
    assert hits, "the fast path did not decode the signal it was handed"
    return hits[0]


def test_fast_snr_tracks_the_true_snr(call_fast, raw_library, cq_slot) -> None:
    """五个电平各自落在真值 ±3 dB 内 —— 饱和的估计做不到这件事。"""

    rng = np.random.default_rng(20_261_013)
    reported = {}
    for level in LEVELS_DB:
        samples = _noisy_slot(cq_slot, level, rng)
        reported[level] = _fast_snr(call_fast, raw_library, samples)
        assert abs(reported[level] - level) <= TOLERANCE_DB, (
            f"at {level:+.0f} dB the fast path reported {reported[level]:+d} dB; "
            f"reported so far: {reported}"
        )


def test_fast_snr_actually_varies_with_the_input_level(call_fast, raw_library, cq_slot) -> None:
    """跨 24 dB 的输入，估计值必须跟着走 —— 这就是旧实现失效的那一条。

    这条比上面那条弱（差一个常数偏移也能通过），但它**指名**了缺陷：旧实现下
    每个电平都读 17，``max - min == 0``，这个断言当场红，而上面那条要等到
    真值恰好落在 17 附近才红。
    """

    rng = np.random.default_rng(20_261_013)
    values = [
        _fast_snr(call_fast, raw_library, _noisy_slot(cq_slot, level, rng))
        for level in (6.0, -18.0)
    ]
    spread = max(values) - min(values)
    assert spread >= 18.0, (
        f"6 dB 与 −18 dB 输入只拉开了 {spread} dB（{values}）—— 估计值被压缩了"
    )


def test_fast_results_do_not_claim_a_provisional_snr() -> None:
    """快路径必须**不清** ``WSJT_FLAG_SNR_PROVISIONAL``（bit 4）。

    清位是级联的开关：``cascade.py:_snr_is_trustworthy`` 只要看到这个位就跳过
    峰值判据 ``min_peak_snr``。既然这里已经量到真 dB，再置位等于把峰值判据
    永久关掉 —— 弱信号时隙不会被升级，而这一点不会有任何别的症状。
    """

    source = (ROOT / "ft8" / "dsp" / "wsjt_fast.c").read_text(encoding="utf-8")
    assert "WSJT_FLAG_SNR_PROVISIONAL" not in source, (
        "wsjt_fast.c 又开始置 provisional 位了 —— 那会让级联的峰值判据对"
        "快路径批次永久失效"
    )
    assert "out->flags = 0;" in source
