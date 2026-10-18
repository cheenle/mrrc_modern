"""NanoVNA 测量结果的纯数学层。

S11 是复数反射系数 Γ，页面上每一个数都由它派生：SWR、阻抗、相位、谐振点。
所以存盘只存**原始 S11** —— 能算出来的东西不存第二份，也不会因为以后改了
公式而让旧数据说不清自己是什么。
"""
from __future__ import annotations

import math
from typing import Sequence

Z0 = 50.0
SWR_CAP = 99.9                 # 开路不是无穷大，是"超出量程"
SUSPECT_NEIGHBOUR_SWR = 1.35   # 孤立 1.00 的掉读判据（沿用 antenna-sweep 的规则）


def swr(re: float, im: float) -> float:
    """驻波比 = (1+|Γ|)/(1−|Γ|)，|Γ| ≥ 1 时封顶。"""
    g = math.hypot(re, im)
    if g >= 1.0:
        return SWR_CAP
    return (1.0 + g) / (1.0 - g)


def to_z(re: float, im: float) -> tuple[float, float]:
    """Γ → Z = Z0(1+Γ)/(1−Γ)，返回 (R, X)。"""
    den = (1.0 - re) ** 2 + im ** 2
    if den < 1e-18:
        return (float("inf"), 0.0)
    num_re = 1.0 - re * re - im * im
    num_im = 2.0 * im
    return (Z0 * num_re / den, Z0 * num_im / den)


def analyse(freqs: Sequence[float], s11: Sequence[tuple[float, float]]) -> dict:
    """一次扫描的前端载荷 —— 前端不重算任何物理量。"""
    swrs = [swr(re, im) for re, im in s11]
    zs = [to_z(re, im) for re, im in s11]
    clamped = [min(v, 1e6) for v in (z[0] for z in zs)]
    return {
        "swr": [round(v, 4) for v in swrs],
        "z": [[round(r, 3), round(x, 3)] for r, x in zip(clamped, (z[1] for z in zs))],
        "resonances": resonances(freqs, swrs),
        "suspect": suspect_points(swrs),
    }


def resonances(freqs: Sequence[float], swrs: Sequence[float],
               floor: float = 1.2, ceiling: float = 2.0) -> list[dict]:
    """连续低于 ``floor`` 的游程 = 一个谐振平台，取游程中心频率与最低点。

    孤立一个点不算谐振 —— 它通常是读数事故（``suspect_points`` 会标出来），
    而真正的谐振在 10 kHz 步进下必然是一片平滑的平台。
    """
    out: list[dict] = []
    i, n = 0, len(swrs)
    while i < n:
        if swrs[i] > floor:
            i += 1
            continue
        j = i
        while j + 1 < n and swrs[j + 1] <= floor:
            j += 1
        if j - i >= 1:
            lo = min(range(i, j + 1), key=lambda k: swrs[k])
            if swrs[lo] < ceiling:
                out.append({
                    "f_hz": round((freqs[i] + freqs[j]) / 2.0, 1),
                    "f_min_hz": freqs[lo],
                    "swr_min": round(swrs[lo], 3),
                    "width_hz": round(freqs[j] - freqs[i], 1),
                })
        i = j + 1
    return out


def suspect_points(swrs: Sequence[float]) -> list[int]:
    """单点 ≈1.00 而两侧都 > 1.35 —— 掉读，不是谐振。"""
    out: list[int] = []
    for k in range(1, len(swrs) - 1):
        if swrs[k] <= 1.05 and swrs[k - 1] > SUSPECT_NEIGHBOUR_SWR \
                and swrs[k + 1] > SUSPECT_NEIGHBOUR_SWR:
            out.append(k)
    return out


def to_csv(freqs: Sequence[float], s11: Sequence[tuple[float, float]]) -> str:
    lines = ["freq_hz,s11_re,s11_im,swr,r_ohm,x_ohm"]
    for f, (re, im) in zip(freqs, s11):
        r, x = to_z(re, im)
        lines.append("%.6f,%.6f,%.6f,%.4f,%.3f,%.3f"
                     % (f, re, im, swr(re, im), r, x))
    return "\n".join(lines) + "\n"


def to_s1p(freqs: Sequence[float], s11: Sequence[tuple[float, float]]) -> str:
    """Touchstone v1，只 S11 —— 让数据能流出去，不被本仓绑死。"""
    lines = ["! NanoVNA sweep exported by MRRC Modern",
             "# Hz S RI R 50"]
    lines += ["%.6f %.6f %.6f" % (f, re, im) for f, (re, im) in zip(freqs, s11)]
    return "\n".join(lines) + "\n"


def compare(a: dict, b: dict) -> dict:
    """两次扫描按频率对齐（较粗的一侧线性插值），返回 ΔSWR 与最大差值点。"""
    fa, sa = list(a["freqs"]), list(a["swr"])
    fb, sb = list(b["freqs"]), list(b["swr"])
    if len(fa) != len(fb) or fa[0] != fb[0] or fa[-1] != fb[-1]:
        if len(fa) >= len(fb):
            sb = _resample(fb, sb, fa)
        else:
            sa = _resample(fa, sa, fb)
            fa = fb
    deltas = [y - x for x, y in zip(sa, sb)]
    k = max(range(len(deltas)), key=lambda i: abs(deltas[i]))
    return {"freqs": fa, "delta_swr": [round(d, 4) for d in deltas],
            "worst_hz": fa[k], "worst_delta": round(deltas[k], 4)}


def _resample(freqs: Sequence[float], values: Sequence[float],
              target: Sequence[float]) -> list[float]:
    out: list[float] = []
    j = 0
    last = len(freqs) - 1
    for f in target:
        while j + 1 < last and freqs[j + 1] < f:
            j += 1
        f0, f1 = freqs[j], freqs[min(j + 1, last)]
        v0, v1 = values[j], values[min(j + 1, last)]
        out.append(v0 if f1 == f0 else v0 + (v1 - v0) * (f - f0) / (f1 - f0))
    return out
