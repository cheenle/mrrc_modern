"""`/v1/assistant/ask`、`/v1/assistant/status`、`/v1/assistant/conversation`（DELETE）。

`ThreadingHTTPServer` + 手写 JSON —— 与 `tools/support_receiver/` 同一风格：这个服务要能
在任意一台 Linux 上 `python3 -m tools.assistant_gateway.gateway` 起来，不引入 Web 框架。

限额在**解析之前**：请求体 ≤ 16 KB、message ≤ 4000 字符。实例 token 由配置表给出
（安装时的匿名 id，不是呼号），拒绝一切未知 token —— 这是唯一的一道门。
"""
from __future__ import annotations

import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 16 * 1024
MAX_MESSAGE = 4000
#: 每实例每小时最多问多少次。配额（每天 20 次**兜底**）管的是钱，这条管的是滥用：
#: 共享 token 一旦泄露，没有它对方可以无限刷。
MAX_ASKS_PER_HOUR = 40
#: 盒子自报的匿名 id。只接受十六进制 —— 它同时是会话键与配额键，不许出现自由文本。
_BOX_ID = re.compile(r"^[0-9a-f]{16,32}$")


def _parse(raw: bytes) -> dict:
    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def make_handler(*, engine, store, instances: dict, clock=time.monotonic):
    hits: dict = {}          # instance_id -> [时间戳…]，进程内滑动窗口

    def _allowed(instance_id: str) -> bool:
        now = clock()
        window = [t for t in hits.get(instance_id, []) if now - t < 3600]
        if len(window) >= MAX_ASKS_PER_HOUR:
            hits[instance_id] = window
            return False
        window.append(now)
        hits[instance_id] = window
        return True

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):       # noqa: A002 - 基类签名如此
            """审计走 store，不刷 stdout（基类参数名就叫 format，照抄才不会报警）。"""
            return

        def _json(self, status: int, payload: dict) -> None:
            blob = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def _instance(self):
            """(身份, 访问口令是否有效)。身份优先用 X-Mrrc-Box —— 见 assistant_client.box_id。"""
            token = self.headers.get("X-Mrrc-Instance") or ""
            owner = instances.get(token)
            if not owner:
                return None
            box = (self.headers.get("X-Mrrc-Box") or "").strip().lower()
            return box if _BOX_ID.match(box) else owner

        def do_POST(self) -> None:                  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._json(400, {"error": "bad content-length"})
            if length > MAX_BODY:
                return self._json(413, {"error": "body too large"})
            instance_id = self._instance()
            if not instance_id:
                return self._json(401, {"error": "unknown instance"})
            if self.path == "/v1/assistant/status":
                return self._json(200, {"enabled": True, "instance": instance_id})
            if self.path != "/v1/assistant/ask":
                return self._json(404, {"error": "not found"})
            payload = _parse(self.rfile.read(length))
            message = str(payload.get("message") or "")[:MAX_MESSAGE].strip()
            if not message:
                return self._json(400, {"error": "empty message"})
            lang = str(payload.get("lang") or "zh")
            conv = payload.get("conversation_id") or store.create_conversation(instance_id, lang)
            if not store.conversation(conv):
                conv = store.create_conversation(instance_id, lang)
            if not _allowed(instance_id):
                return self._json(429, {"error": "too many requests this hour"})
            history = store.history(conv, limit=3)
            answer = engine.answer(message, payload.get("state") or {}, history=history,
                                   misses=store.count_misses_today(instance_id))
            store.append_turn(conv, "user", message)
            turn_id = store.append_turn(conv, "assistant", answer.answer, mode=answer.mode,
                                        citations=answer.citations,
                                        needs_confirm=answer.needs_confirm,
                                        tokens=answer.tokens)
            return self._json(200, {
                "conversation_id": conv, "turn_id": turn_id, "answer": answer.answer,
                "citations": answer.citations, "mode": answer.mode,
                "needs_confirm": answer.needs_confirm, "degraded": answer.degraded,
            })

        def do_DELETE(self) -> None:                # noqa: N802
            instance_id = self._instance()
            if not instance_id:
                return self._json(401, {"error": "unknown instance"})
            if self.path != "/v1/assistant/conversation":
                return self._json(404, {"error": "not found"})
            length = self.headers.get("Content-Length") or "0"
            try:
                payload = _parse(self.rfile.read(int(length)))
            except ValueError:
                payload = {}
            conv = payload.get("conversation_id") or ""
            if not conv:
                # 还没有会话就点「清空会话」是正常的第一次操作，不是错误：
                # 回 404 会让面板显示"助手需要联网才能回答"，把一次空操作说成故障。
                return self._json(200, {"deleted": False})
            owner = store.conversation(conv)
            if not owner:
                return self._json(200, {"deleted": False})
            if owner["instance_id"] != instance_id:
                # 别人的会话一律当"不存在" —— 不泄露它是否存在。
                return self._json(404, {"error": "not found"})
            store.delete_conversation(conv)
            return self._json(200, {"deleted": True})

    return Handler


def make_server(*, host: str, port: int, engine, store, instances: dict) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(
        engine=engine, store=store, instances=instances))
