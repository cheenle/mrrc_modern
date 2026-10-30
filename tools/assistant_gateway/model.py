"""OpenAI 兼容的模型客户端。

`opener` 可注入 ⇒ 单测里没有网络。真实现走 `net_tls.urlopen`：
仓库有一条 AST 守卫（`tests/test_tls_trust_store.py`）要求所有出站 `urlopen` 要么带
`context=`、要么经过 net_tls —— 网关也是出货代码，不能例外。

失败一律翻译成 `ModelUnavailable`：调用方据此降级成「待确认」，**绝不**把异常抛给客户
（助手不是一条能让面板崩掉的链路）。
"""
from __future__ import annotations

import json
import urllib.request
from typing import Callable

import net_tls


class ModelUnavailable(Exception):
    """模型不可用（网络 / 超时 / 协议错）。"""


class ModelClient:
    def __init__(self, endpoint: str, model: str, api_key: str, *,
                 timeout: int = 20, opener: Callable | None = None,
                 thinking: bool = False):
        self._endpoint = endpoint
        self._model = model
        self._key = api_key
        self._timeout = timeout
        #: DeepSeek V4 的思考模式**默认打开**，思维链会先吃掉 max_tokens —— 实测把
        #: 700 token 的预算耗光之后 `content` 是空串，面板上就是一条空白答案。
        #: 我们的答案本来就要求"只复述条目里的事实"，思考只会加延迟和成本。
        self._thinking = thinking
        #: 最近一次调用的 usage（审计与成本核算用；失败时保持上一次的值不动）。
        self.last_usage: dict = {}
        self._opener = opener or (lambda req, timeout: net_tls.urlopen(req, timeout=timeout))

    def complete(self, messages: list, *, max_tokens: int = 700) -> str:
        payload = {"model": self._model, "messages": messages,
                   "temperature": 0.2, "max_tokens": max_tokens}
        if not self._thinking:
            payload["thinking"] = {"type": "disabled"}
        body = json.dumps(payload).encode()
        req = urllib.request.Request(self._endpoint, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._key}",
        })
        try:
            raw = self._opener(req, self._timeout).read()
            answer = json.loads(raw.decode("utf-8"))
            text = answer["choices"][0]["message"]["content"].strip()
            self.last_usage = answer.get("usage") or {}
        except Exception as exc:                     # noqa: BLE001 - 一律降级
            raise ModelUnavailable(str(exc)) from exc
        if not text:
            # 空答案比错误更危险：它会渲染成一个空气泡，看起来像产品坏了。
            raise ModelUnavailable("empty completion")
        return text
