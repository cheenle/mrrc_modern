"""FT8 event stream: one WebSocket at /WSft8.

Handshake authentication is the platform's (``_token_from_ws_handshake``:
header, then cookie; the URL carries no credential).  Frames are JSON:

  {"type": "hello",   "mode": "ssb"|"ft8", "decodes": [...]}
  {"type": "decodes", "items": [...]}
  {"type": "mode",    "mode": "ssb"|"ft8"}
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger("mrrc.ft8.ws")

BUS_CAPACITY = 256


class Ft8EventBus:
    """Bounded fan-out: slow subscribers lose the oldest frames, never block."""

    def __init__(self, capacity: int = BUS_CAPACITY) -> None:
        self._capacity = capacity
        self._queues: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._capacity)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    def publish(self, frame: dict) -> None:
        for queue in list(self._queues):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - racy drain
                    pass
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:  # pragma: no cover - raced with a drain
                pass

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)


class Ft8WsState:
    """Callbacks the platform provides to the FT8 stream."""

    def __init__(self, *, bus: Ft8EventBus,
                 token_from_handshake: Callable[[WebSocket], str],
                 auth_ok: Callable[[str], bool],
                 mode: Callable[[], str],
                 recent_decodes: Callable[[], list[dict]],
                 control_request: Callable[[str, dict], Any] | None = None) -> None:
        self.bus = bus
        self.token_from_handshake = token_from_handshake
        self.auth_ok = auth_ok
        self.mode = mode
        self.recent_decodes = recent_decodes
        self.control_request = control_request


def create_ft8_ws_router(state: Ft8WsState) -> APIRouter:
    router = APIRouter()

    @router.websocket("/WSft8")
    async def ft8_stream(ws: WebSocket) -> None:
        token = state.token_from_handshake(ws)
        if not state.auth_ok(token):
            await ws.accept()
            await ws.close(code=4401, reason="unauthenticated")
            return
        await ws.accept()
        queue = state.bus.subscribe()
        try:
            await ws.send_json({
                "type": "hello",
                "mode": state.mode(),
                "decodes": state.recent_decodes()[-50:],
            })
            while True:
                frame = await queue.get()
                await ws.send_json(frame)
        except WebSocketDisconnect:
            pass
        except Exception:                     # pragma: no cover - transport noise
            log.debug("FT8 stream closed", exc_info=True)
        finally:
            state.bus.unsubscribe(queue)

    return router
