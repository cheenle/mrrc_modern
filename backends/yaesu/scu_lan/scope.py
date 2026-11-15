"""
SCU-LAN10 scope producer — 4096-byte sweep bodies into the server's handler
=========================================================================
``ScopeProducer`` for a radio behind a LAN unit.  The unit pushes one 4096-byte
body per sweep (≈25/s) on port+3; `scope_format` turns a body into the server's
850-bin line, and this producer writes it into the shared ``ScopeHandler`` in
place — the same fields ``backends/ic7300/civ_scope.py`` fills, so the frontend,
`spectrum_profile`'s bandwidth tiers and the 30 Hz scheduler are untouched.

Three decisions worth knowing:

* **Geometry comes from CAT, never from the body.**  The sweep is 850-ish bytes
  of amplitude with no metadata we can trust (the published decoder ignores
  everything past the boundary marker).  So the span and mode are read with
  ``SS`` and the centre with ``FA``, refreshed every couple of seconds — a
  frequency axis guessed from the body is how a waterfall ends up plausible and
  wrong (design record §4).
* **Only the MAIN sweep is decoded.**  The body's tail contains a region the
  upstream project never managed to decode; ``spectrum_rx2`` therefore stays
  zeros — an honest "no second receiver data" rather than a copy of the first.
* **It must not stop when the last waterfall closes.**  On this backend the
  scope bodies ride the same session as CAT and audio, so
  ``keeps_running()`` returns True; ``server.ws_spectrum`` reads it and leaves a
  network producer alone (otherwise closing a browser tab would silence every
  listener, FT8 and the recorder included).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from backends.ft710.scope_frame import WF_SIZE
from backends.yaesu.scu_lan import scope_format

logger = logging.getLogger("yaesu.scu_lan.scope")

OnFrameCallback = Callable[[object], Awaitable[None]]

#: ``SS`` P2=5 span code → span width in Hz.
#:
#: provenance: UNVERIFIED.  Read off the Japanese FTDX101 CAT manual's table
#: (``0:1k 5k 10k 20k 50k 100k 200k 500k 1M``); the OCR of that table was
#: ambiguous about how many entries exist, so `_diag_scu_lan.py --spans` prints
#: the raw `SS05;` answer for each code and this table is corrected from that
#: report.  A code outside the table leaves the frequency axis untouched rather
#: than inventing one.
SS_SPAN_HZ = {
    0: 1_000, 1: 5_000, 2: 10_000, 3: 20_000, 4: 50_000,
    5: 100_000, 6: 200_000, 7: 500_000, 8: 1_000_000,
}

#: `SS` P2=6 mode answers that mean "the VFO sits at the centre of the sweep".
#: 0-2 are the 3DSS modes, 3-5 the waterfall ones; 6-8 and 9-11 are cursor/fix.
SS_CENTER_MODES = frozenset({0, 3, 4, 5})

#: How often the geometry (span/mode/centre) is refreshed.  Cheaper than it
#: looks: one `SS` and one `FA` per cycle, against a poll scheduler that already
#: reads `FA` up to 10×/s.
GEOMETRY_REFRESH_S = 2.0

#: A body whose sweep cannot be decoded (no boundary marker, empty) is counted,
#: not logged 25×/s.
_BAD_BODY_LOG_EVERY = 250


class ScuLanScopeProducer:
    """ScopeProducer for the SCU-LAN10's port+3 sweep stream."""

    def __init__(
        self,
        client,
        *,
        cat=None,
        scope=None,
        on_frame: Optional[OnFrameCallback] = None,
        refresh_s: float = GEOMETRY_REFRESH_S,
    ) -> None:
        self._client = client
        self._cat = cat
        self._scope = scope
        self._on_frame = on_frame
        self._refresh_s = refresh_s
        self._active = False
        self._task: Optional[asyncio.Task] = None
        self._geometry_task: Optional[asyncio.Task] = None
        self._frames = 0
        self._bad_bodies = 0
        self._dropped = 0
        self._stall_warned = False
        self._last_body_at = 0.0
        #: Latched geometry, None until CAT answers.
        self._span_index: Optional[int] = None
        self._span_hz: Optional[int] = None
        self._center_hz: Optional[int] = None
        self._centered = True
        self._previous_on_scope = None

    # ── ScopeProducer protocol ─────────────────────────────────────

    def set_on_frame(self, cb: OnFrameCallback) -> None:
        self._on_frame = cb

    def notify_tx(self, tx: bool) -> None:
        """The unit keeps sending sweeps during TX; nothing to pause here.

        (The FT-710's SPI stream garbles on TX and its producer pauses it — this
        one has no SPI and no such behaviour to mirror.)
        """
        return None

    def keeps_running(self) -> bool:
        """The sweep stream shares its session with CAT and audio (§docstring)."""
        return True

    async def start(self) -> None:
        """Attach to the session's scope callback and start geometry refresh."""
        self._active = True
        self._last_body_at = time.monotonic()
        self._previous_on_scope = getattr(self._client, "on_scope", None)
        self._client.on_scope = self._on_body
        if self._cat is not None and self._geometry_task is None:
            self._geometry_task = asyncio.create_task(
                self._geometry_loop(), name="scu-lan-scope-geometry")
        logger.info("SCU-LAN10 scope: consuming sweeps on port+3")
        # One-shot read so the first sweeps already have an axis.
        await self._refresh_geometry()

    async def stop(self) -> None:
        """Detach.  Idempotent; leaves the session itself running."""
        self._active = False
        current = getattr(self._client, "on_scope", None)
        if getattr(current, "__self__", None) is self:
            self._client.on_scope = self._previous_on_scope
        for task in (self._geometry_task, self._task):
            if task is not None and not task.done():
                task.cancel()
        self._geometry_task = None
        self._task = None

    # ── inbound ────────────────────────────────────────────────────

    def _on_body(self, body: bytes) -> None:
        """Client callback (event loop): decode one sweep, publish it once."""
        if not self._active:
            return
        if self._task is not None and not self._task.done():
            # A slow frame must not build a queue of stale waterfalls.
            self._dropped += 1
            return
        self._task = asyncio.create_task(self._handle(body),
                                         name="scu-lan-scope-frame")

    async def _handle(self, body: bytes) -> None:
        self._last_body_at = time.monotonic()
        self._stall_warned = False
        bins = scope_format.to_scope_bins(body, WF_SIZE)
        if not bins:
            self._bad_bodies += 1
            if self._bad_bodies % _BAD_BODY_LOG_EVERY == 1:
                logger.warning(
                    "SCU-LAN10 scope: %d undecodable sweeps (no boundary marker "
                    "in the expected range) — the %s may encode the sweep "
                    "differently from the FTDX10 this rule was measured on",
                    self._bad_bodies, "radio")
            return
        self._frames += 1
        scope = self._scope
        if scope is None:
            return

        scope.spectrum_rx1 = bins
        scope.spectrum_rx2 = [0] * WF_SIZE

        if self._center_hz is not None and self._span_hz:
            if self._span_index is not None:
                scope.scope_span = self._span_index
            if self._centered:
                scope.scope_start_freq = self._center_hz - self._span_hz // 2
            else:
                # Cursor/fix modes put the VFO elsewhere in the sweep and the
                # body carries no edge information; keep the last good axis
                # instead of drawing a scale we cannot justify.
                pass

        now = time.time()
        if scope._last_frame_time > 0:
            dt = now - scope._last_frame_time
            if dt > 0:
                scope._fps = scope._fps * 0.9 + (1.0 / dt) * 0.1
        scope._last_frame_time = now
        scope._frame_count += 1
        scope.last_update = now
        if not scope._connected:
            scope._connected = True
            logger.info(
                "SCU-LAN10 scope: first sweep — spectrum active (span=%s, "
                "centre=%s, bins=%d)", self._span_hz, self._center_hz, len(bins))

        if self._on_frame is not None:
            await self._on_frame(scope)

    # ── geometry ───────────────────────────────────────────────────

    async def _geometry_loop(self) -> None:
        stall = 0
        while self._active:
            # 三条 CAT 查询各有 0.4 s 上限，合计 1.2 s —— 电台不回话时每 2 秒
            # 抢走 1.2 秒的 CAT 锁，会把 100 ms 的状态轮询挤成「卡顿」。
            # 一次全部落空就退到 3 倍间隔，恢复后再回来。
            await asyncio.sleep(self._refresh_s * (3 if stall else 1))
            try:
                answered = await self._refresh_geometry()
            except Exception as exc:                        # noqa: BLE001
                logger.debug("SCU-LAN10 scope: geometry refresh failed: %s", exc)
                stall = stall + 1
                continue
            if answered:
                if stall:
                    logger.info("SCU-LAN10 scope: geometry answering again "
                                "after %d quiet cycles", stall)
                stall = 0
            else:
                stall += 1
                if stall == 1:
                    logger.debug("SCU-LAN10 scope: geometry queries went "
                                 "unanswered — backing off")

    async def _refresh_geometry(self) -> int:
        """Read span/mode (`SS`) and centre frequency (`FA`) over CAT.

        Returns how many of the three answered — the loop backs off when none
        did (see `_geometry_loop`).
        """
        cat = self._cat
        if cat is None:
            return 0
        answered = 0
        span_answer = await cat.query("SS05", timeout=0.4)
        if span_answer:
            answered += 1
            self._take_span(span_answer)
        mode_answer = await cat.query("SS06", timeout=0.4)
        if mode_answer:
            answered += 1
            self._take_mode(mode_answer)
        freq_answer = await cat.query("FA", timeout=0.4)
        if freq_answer:
            answered += 1
            self._take_center(freq_answer)
        return answered

    # Parsers are separated so a field answer can be replayed in a test without
    # a radio — the answer shapes below are guesses until one arrives, and the
    # only defensible thing to do with a guess is to make it auditable.

    def _take_span(self, answer: Optional[str]) -> None:
        value = _ss_value(answer, expected_query=5)
        if value is None:
            return
        hz = SS_SPAN_HZ.get(value)
        if hz is None:
            logger.debug("SCU-LAN10 scope: unknown SS span code %r", value)
            return
        self._span_hz = hz
        # The UI indexes spans by position, and our table is the radio's own
        # order, so the code IS the index.
        self._span_index = value

    def _take_mode(self, answer: Optional[str]) -> None:
        value = _ss_value(answer, expected_query=6)
        if value is None:
            return
        self._centered = value in SS_CENTER_MODES

    def _take_center(self, answer: Optional[str]) -> None:
        if not answer:
            return
        digits = "".join(ch for ch in answer[2:] if ch.isdigit())
        if len(digits) < 5:
            return
        try:
            self._center_hz = int(digits)
        except ValueError:
            return

    # ── diagnostics ────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """JSON-safe producer state for the log / support bundle."""
        return {
            "frames": self._frames,
            "bad_bodies": self._bad_bodies,
            "dropped": self._dropped,
            "span_hz": self._span_hz,
            "span_index": self._span_index,
            "center_hz": self._center_hz,
            "centered": self._centered,
            "body_age_s": (0.0 if not self._last_body_at else
                           round(time.monotonic() - self._last_body_at, 2)),
        }


def _ss_value(answer: Optional[str], *, expected_query: int) -> Optional[int]:
    """The value in an `SS` answer for ``P2 = expected_query``.

    The Set form is ``SS P1 P2 P3 ...`` and the Read form ``SS P1 P2;``; the
    answer is expected to echo ``SS P1 P2`` followed by the value in ``P3`` and
    fixed zeros in ``P4``-``P7`` (``SS0550000;`` = MAIN, SPAN, code 5).  That
    shape has never been observed on hardware here, so the parse is deliberately
    shallow: a single leading digit is the value, and anything that does not
    look like that shape returns None — a misread number latched into the
    frequency axis is worse than no axis at all.  A two-digit MODE answer
    (10/11) degrades to 1, which is also a non-centre mode, so the only thing
    that consumes it ("is the VFO at the middle of the sweep?") stays right.
    """
    if not answer:
        return None
    text = answer.strip().rstrip(";")
    if not text.startswith("SS") or len(text) < 5:
        return None
    # ASCII digits, converted without int(): the linter cannot see that the
    # filter makes int() total, and ord-48 is both total and faster.
    digits = [ord(ch) - 48 for ch in text[2:] if "0" <= ch <= "9"]
    if len(digits) < 3:
        return None
    _band, item = digits[0], digits[1]
    if item != expected_query:
        return None
    return digits[2]
