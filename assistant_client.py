"""盒子侧的助手客户端：**只做三件事** —— 摘状态、转发、把失败翻译成人话。

知识库不在本地（规格 §3），所以这里没有任何检索逻辑：断网就是断网。这看起来是退步，
但它换来的是「一件事实只有一处定义」—— 盒子不参与判断，就不会和服务端说出两个版本。

出站走 `net_tls.urlopen`（仓库的 AST 守卫要求所有出站请求经过它，见
`tests/test_tls_trust_store.py`）。`opener` 可注入 ⇒ 单测不联网。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import urllib.request
from typing import Callable, Optional

import net_tls

ASK_TIMEOUT_S = 10
OFFLINE_ANSWER = ("助手需要联网才能回答；本机不留知识库。"
                  "如果这是故障，请先点「遇到问题」提一个诊断包。")

#: 发送白名单。**加字段必须同时改这里和 tests/test_assistant_client.py 的键集断言** ——
#: 那条断言存在的意义就是让"顺手多带一个字段"变成一次需要解释的改动。
ALLOWED_KEYS = ("model", "version", "fixpack", "serial_connected", "audio_rx_open",
                "audio_tx_open", "last_errors", "platform", "ui_lang")

MAX_ERRORS = 3


def state_summary(state: dict) -> dict:
    """从调用方的状态字典里**挑选**（不是构造）白名单字段。"""
    summary = {key: state[key] for key in ALLOWED_KEYS if key in state}
    errors = summary.get("last_errors") or []
    summary["last_errors"] = [str(item)[:120] for item in list(errors)[:MAX_ERRORS]]
    return summary


def box_id(data_dir) -> str:
    """稳定的匿名盒子 id：首次运行时随机生成并落盘，之后一直用它。

    为什么需要它：**访问口令是共享的**（一个 token 发给所有盒子），如果身份也用 token，
    全舰队就会挤进同一个会话桶和同一份每日配额。身份与口令分开，才能既用一个秘密，
    又让每台盒子有自己的上下文与额度。
    """
    path = data_dir / "assistant-box-id"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if len(text) >= 16:
        return text[:32]
    text = secrets.token_hex(16)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        pass                      # 只读目录也能用：本次进程内有效
    return text


def offline(reason: str = "offline") -> dict:
    return {"answer": OFFLINE_ANSWER, "citations": [], "mode": "offline",
            "needs_confirm": True, "degraded": reason}


class AssistantClient:
    def __init__(self, base_url: str, instance_token: str, *,
                 box: str = "", opener: Optional[Callable] = None):
        self._base = base_url.rstrip("/")
        self._token = instance_token
        self._box = box
        self._opener = opener or (lambda req, timeout: net_tls.urlopen(req, timeout=timeout))

    def ask(self, conversation_id: Optional[str], message: str, state: dict) -> dict:
        body = json.dumps({
            "conversation_id": conversation_id,
            "message": message,
            "state": state_summary(state),
            "lang": state.get("ui_lang", "zh"),
        }).encode()
        req = urllib.request.Request(f"{self._base}/assistant/ask", data=body, method="POST",
                                     headers=self._headers())
        return self._call(req)

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "X-Mrrc-Instance": self._token}
        if self._box:
            headers["X-Mrrc-Box"] = self._box
        return headers

    def clear(self, conversation_id: Optional[str]) -> dict:
        body = json.dumps({"conversation_id": conversation_id or ""}).encode()
        req = urllib.request.Request(f"{self._base}/assistant/conversation", data=body,
                                     method="DELETE", headers=self._headers())
        return self._call(req)

    def _call(self, req) -> dict:
        try:
            raw = self._opener(req, ASK_TIMEOUT_S).read()
            payload = json.loads(raw.decode("utf-8"))
        except Exception:                     # noqa: BLE001 - 任何失败都降级，绝不抛给面板
            return offline()
        return payload if isinstance(payload, dict) else offline("bad_payload")
