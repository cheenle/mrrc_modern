"""条目模型与检索 —— 纯函数，没有 I/O，所以阈值可以被单测钉死。

**打分用 BM25（k1=1.2, b=0.75），标题计 3 份、正文计 1 份。**

这个选择是量出来的，不是偏好。在 86 条真实条目上试过三种：

| 方案 | 「CQ 播放有失真怎么办」 | 「怎么给树莓派烧卡」（离题） | 结论 |
| --- | --- | --- | --- |
| 字段加权覆盖率（正文参与） | 0.747（不成命中） | **1.000（全库最高）** | 长正文让任何查询都"沾一点" |
| IDF 加权覆盖率 | 0.747 | 1.000 | 短查询 + 长条目仍然骗得过 |
| 余弦（TF-IDF） | 0.083 | 0.088 | 长条目把分母撑爆，全部沉底 |
| **BM25** | **9.86** | 7.86 | 长度归一 + 词频饱和，离题的压在命中线下 |

阈值 `HIT_MIN_SCORE = 8.0` / `HIT_MIN_MARGIN = 1.0` 是**绝对 BM25 分**，在 86 条真实
条目上标定：4 条该命中的全部命中（9.9 / 14.6 / 14.0 / 16.6），2 条离题的都被挡下
（7.9 / 6.6）。另有 2 条（「频谱不出图」「FT8 一个都解不出来」）**没达成命中**，它们会落到
「兜底生成 + 待确认」—— 这是可接受的方向：宁可不答，也不要拿一条不相干的条目当权威答案。

因为它是绝对分，**语料规模变了就要重标**（IDF 随 N 走）。`tests/test_assistant_gateway.py`
里那份 golden set 就是为此存在的：它拿真实的 `assistant_kb.json` 跑，命中/未命中的名单
一旦变化就会红。

校准方法与样本写在 `docs/superpowers/specs/2026-10-30-ai-assistant-design.md` §11，
上线两周后按真实提问重标。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

#: 命中门槛（BM25 分数，不是 0…1 的归一值）。第二条防「什么都沾一点」。
HIT_MIN_SCORE = 8.0
HIT_MIN_MARGIN = 1.0

TITLE_BOOST = 3
K1 = 1.2
B = 0.75

_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def keywords(text: str) -> list[str]:
    """与 `dev_tools/make_assistant_kb.keywords` 一字不差（同一切分）。"""
    words = {w.lower() for w in _LATIN.findall(text) if len(w) >= 2}
    for run in _CJK_RUN.findall(text):
        words.update(run[i:i + 2] for i in range(len(run) - 1))
    return sorted(words)


def _applies(entry: dict, model: str) -> bool:
    """`applies_to.models` 为空 = 全机型；非空 = 只对这些机型成立。

    版本不设上界：`version_min` 是「从这版起」，比它新的机器依然该命中 —— 一个
    1.28.22 才修好的问题，在 1.29 上依然是「已修好」。
    """
    models = entry.get("applies_to", {}).get("models") or []
    return not models or model in models


@dataclass(frozen=True)
class Scored:
    entry: dict
    score: float


def _term_freq(entry: dict) -> Counter:
    tf: Counter = Counter()
    for term in keywords(entry.get("title", "")):
        tf[term] += TITLE_BOOST
    for term in keywords(entry.get("body", "")):
        tf[term] += 1
    return tf


class Index:
    """一次构建，多次查询 —— IDF 与平均长度只跟语料有关。"""

    def __init__(self, entries):
        self.entries = list(entries)
        self._tf = [_term_freq(e) for e in self.entries]
        self._len = [sum(tf.values()) for tf in self._tf]
        total = len(self.entries) or 1
        self._avgdl = (sum(self._len) / total) if self._len else 1.0
        df: Counter = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        self._idf = {t: math.log(1 + (total - c + 0.5) / (c + 0.5))
                     for t, c in df.items()}

    def score(self, query_terms, position: int) -> float:
        tf, dl = self._tf[position], self._len[position]
        total = 0.0
        for term in set(query_terms):
            freq = tf.get(term)
            if not freq:
                continue
            total += self._idf.get(term, 0.0) * (freq * (K1 + 1)) / (
                freq + K1 * (1 - B + B * dl / self._avgdl))
        return total


def retrieve(entries, query: str, version: str, model: str,
             limit: int = 3) -> list:
    applicable = [e for e in entries if _applies(e, model)]
    if not applicable:
        return []
    index = Index(applicable)
    terms = keywords(query)
    scored = [Scored(entry, round(index.score(terms, i), 2))
              for i, entry in enumerate(applicable)]
    scored = [s for s in scored if s.score > 0]
    scored.sort(key=lambda s: (-s.score, s.entry.get("id", "")))
    return scored[:limit]


def is_hit(hits: list) -> bool:
    """命中判据：分数够高，且与第二名拉开差距。"""
    if not hits or hits[0].score < HIT_MIN_SCORE:
        return False
    return len(hits) == 1 or hits[0].score - hits[1].score >= HIT_MIN_MARGIN
