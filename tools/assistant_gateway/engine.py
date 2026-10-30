"""判定引擎：规则层 → KB 检索 → 兜底生成，外加出处校验。

三条硬规矩（规格 §5）：

1. **规则层不调模型、不扣配额** —— 最高频的三个现场问题（串口没连、声卡没开、版本落后）
   各有确定答案，让模型去"组织"它们只是给它编的机会；
2. **KB 命中的事实必须来自条目** —— 模型只按条目复述，渲染时带 citations；
3. **生成式答案一旦提到无法核对的版本号就降级** —— 客户的机器版本就在 `state` 里，
   对不上就是模型在编，宁可标「待确认」。

例外：模型不可用（ModelUnavailable）时回落到「最接近的条目 + 待确认」，不返回 500。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from tools.assistant_gateway import kb as gkb
from tools.assistant_gateway.model import ModelUnavailable

_VERSION = re.compile(r"\b1\.\d{1,2}\.\d{1,2}\b")

RULE_SERIAL = ("串口没连上电台，所以拿不到它的数据。打开「连接设置」选中端口并保存，页面会重连。")
RULE_AUDIO = ("RX 声卡没打开。确认电台已用 USB 连上、且没有被别的程序占用，"
              "然后在设置页重连音频。")
RULE_OUTDATED = ("你现在的版本是 {version}，这个问题在 {target} 才修好 —— 在设置页点「软件更新」即可。")


@dataclass
class Answer:
    answer: str
    mode: str                      # rule | kb | generated | offline
    citations: list = field(default_factory=list)
    needs_confirm: bool = False
    degraded: str = ""
    tokens: int = 0


def _rule_answer(question: str, state: dict) -> Answer | None:
    if any(w in question for w in ("频谱", "瀑布", "不出图", "没有波形")) \
            and not state.get("serial_connected"):
        return Answer(answer=RULE_SERIAL, mode="rule")
    if any(w in question for w in ("没声音", "听不到", "没有声音")) \
            and not state.get("audio_rx_open"):
        return Answer(answer=RULE_AUDIO, mode="rule")
    return None


def _cite(entry: dict) -> dict:
    return {"id": entry.get("id", ""), "label": entry.get("title", ""),
            "url": entry.get("url", ""), "fixpack": entry.get("fixpack", ""),
            "version_min": entry.get("applies_to", {}).get("version_min", "")}


def _unverifiable_version(text: str, known: set) -> str:
    """答案里提到的版本号，凡是与「本机版本 + 命中条目版本」都不符的，交出来。"""
    mentioned = set(_VERSION.findall(text))
    return ",".join(sorted(mentioned - known))


class Engine:
    def __init__(self, *, entries, model, quota_per_day: int = 20):
        self._entries = entries
        self._model = model
        self._quota = quota_per_day
        self._last_tokens = 0

    def answer(self, question: str, state: dict, *, history: list,
               misses: int = 0) -> Answer:
        """`misses` 是这台实例**今天已经用掉的兜底次数**（网关按实例查库后传进来）。"""
        ruled = _rule_answer(question, state)
        if ruled is not None:
            return ruled

        version = state.get("version", "")
        hits = gkb.retrieve(self._entries, question, version, state.get("model", ""))
        hit = gkb.is_hit(hits)

        if hit:
            entry = hits[0].entry
            known = {version, entry.get("applies_to", {}).get("version_min", "")} - {""}
            text = self._compose(question, state, history, entry, known)
            if text is None:
                return self._kb_only(entry, degraded="model_unavailable")
            bad = _unverifiable_version(text, known)
            return Answer(answer=text, mode="kb",
                          citations=[_cite(entry)], tokens=self._last_tokens,
                          needs_confirm=bool(bad), degraded="unverifiable" if bad else "")

        if misses >= self._quota:
            return self._quota_answer(hits)

        known = {version} - {""}
        text = self._compose(question, state, history, None, known)
        if text is None:
            return self._quota_answer(hits, degraded="model_unavailable")
        bad = _unverifiable_version(text, known)
        # 兜底生成的答案**永远**需要确认 —— 这是规矩，不是启发式。
        return Answer(answer=text, mode="generated", needs_confirm=True,
                      tokens=self._last_tokens,
                      degraded="unverifiable" if bad else "")

    # ── helpers ───────────────────────────────────────────────
    def _quota_answer(self, hits, degraded: str = "quota") -> Answer:
        if not hits:
            return Answer(answer="没有查到相关的知识条目。建议点「遇到问题」提一个诊断包。",
                          mode="kb", degraded=degraded, needs_confirm=True)
        best = hits[0].entry
        return Answer(answer=f"自动答疑暂时不可用，知识库里最接近的是：{best.get('title','')}",
                      mode="kb", citations=[_cite(best)], needs_confirm=True,
                      degraded=degraded)

    def _kb_only(self, entry: dict, *, degraded: str) -> Answer:
        """模型挂了但条目在：把条目本身当答案，标待确认。"""
        body = entry.get("body", "").strip()[:400]
        return Answer(answer=f"{entry.get('title','')}\n\n{body}", mode="kb",
                      citations=[_cite(entry)], needs_confirm=True, degraded=degraded)

    def _compose(self, question, state, history, entry, known) -> str | None:
        self._last_tokens = 0
        system = ["你是 MRRC Modern（业余无线电远程控制产品）的助手。",
                  "只回答产品相关问题；不确定就直说不确定，**不要**编造版本号、命令或配置项。",
                  f"客户机器：{state.get('model')} / {state.get('version')} · {state.get('fixpack')}",
                  f"串口已连：{state.get('serial_connected')}；RX 声卡：{state.get('audio_rx_open')}；"
                  f"TX 声卡：{state.get('audio_tx_open')}",
                  "回答里提到的版本号只能来自上面这些，或来自下面给出的知识条目。"]
        if entry is not None:
            system.append("下面的知识条目是事实来源，回答必须与之一致，不得添加条目里没有的事实：")
            system.append(f"【{entry.get('title','')}】\n{entry.get('body','')}")
        messages = [{"role": "system", "content": "\n".join(system)}]
        for turn in history[-3:]:
            messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append({"role": "user", "content": question})
        try:
            text = self._model.complete(messages)
        except ModelUnavailable:
            return None
        self._last_tokens = sum(
            int((getattr(self._model, "last_usage", {}) or {}).get(key) or 0)
            for key in ("prompt_tokens", "completion_tokens"))
        return text
