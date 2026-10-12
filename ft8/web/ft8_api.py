"""Versioned REST intent endpoints under ``/api/v1`` (§10.1, §11.3).

Control rules (§10.3): reads need a session; TX-starting, radio and
sequencer mutations also need the control lease; STOP needs only a session
and bypasses the lease (NFR-038); diagnostic export needs a recent password
re-authentication (NFR-039).  Every mutation validates Host/Origin
(NFR-035), honors an ``Idempotency-Key`` header and, when
``X-Expected-Revision`` is sent, rejects stale clients with
``stale_revision`` (§10.1).  Rejections carry the stable reasons from §10.1
plus ``tx_active``, ``reauth_required`` and ``rate_limited``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from ..engine.adif import generate_adif
from ..engine.msgparse import ParsedMessage
from ..engine.orchestrator import FT8_PERIOD_SECONDS
from ..engine.repository import Repository, VoidWindowExpired
from ..engine.safety import Interlock, SafetyController, TxRefused
from ..engine.tx_driver import TX_ENCODE_SECONDS, TX_FIT_MARGIN_SECONDS, TX_WAVEFORM_SECONDS
from ..engine.tx_frequency import FrequencyOccupancy
from ..engine.sequencer import DEFAULT_TX_AUDIO_FREQUENCY, DisarmReason, Sequencer
log = logging.getLogger("mrrc-ft8.api")
from .auth_shim import PlatformAuthAdapter, Session, host_allowed, origin_allowed

COOKIE_NAME = "mrrc_auth"        # the platform AUTH_COOKIE
COOKIE_MAX_AGE_S = 43_200  # matches the 12 h absolute session cap

REASON_LEASE_REQUIRED = "lease_required"
REASON_STALE_REVISION = "stale_revision"
REASON_INTERLOCK_OPEN = "interlock_open"
REASON_TX_ACTIVE = "tx_active"
REASON_REAUTH_REQUIRED = "reauth_required"
REASON_RATE_LIMITED = "rate_limited"
REASON_UNAUTHENTICATED = "unauthenticated"
REASON_FORBIDDEN = "forbidden"

# Rig level tokens (rigctld ``l``/``L`` commands) are uppercase Hamlib names.
_LEVEL_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,15}$")
# Mode tokens in rigctld ``M <mode> <passband>`` are uppercase Hamlib names.
_MODE_NAME_RE = re.compile(r"^[A-Z0-9]{1,16}$")

# §10.5 schema-validated settings; safety-impacting ones are TX-locked.


def _valid_lines_rate(value: object) -> bool:
    """waterfall_lines_per_second: number within [1, 10], not a bool."""

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return 1.0 <= float(value) <= 10.0
    except (TypeError, ValueError):
        return False


SETTING_SCHEMA: dict[str, Callable[[Any], bool]] = {
    "decoder_profile": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 4,
    "decoder_threads": lambda v: isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 12,
    "waterfall_lines_per_second": lambda v: _valid_lines_rate(v),
    "cq_loop_idle_timeout_s": lambda v: isinstance(v, int) and not isinstance(v, bool) and 60 <= v <= 3600,
    "auto_call_new_dxcc": lambda v: isinstance(v, bool),
    "auto_band_hunt": lambda v: isinstance(v, bool),
}
SAFETY_IMPACTING_SETTINGS = frozenset({"decoder_profile", "decoder_threads"})


class IdempotencyCache:
    """Short-lived key → response replay for retried mutations (§10.1)."""

    def __init__(self, *, ttl_s: float = 600.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._entries: dict[str, tuple[float, int, dict[str, Any]]] = {}

    def lookup(self, key: str) -> tuple[int, dict[str, Any]] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, status, body = entry
        if self._clock() - stored_at > self._ttl:
            self._entries.pop(key, None)
            return None
        return status, body

    def store(self, key: str, status: int, body: dict[str, Any]) -> None:
        self._entries[key] = (self._clock(), status, body)
        cutoff = self._clock() - self._ttl
        for stale in [k for k, (t, _, _) in self._entries.items() if t < cutoff]:
            self._entries.pop(stale, None)


class _BandHuntCache:
    """TTL cache for raw upstream /api/band_hunt bodies (proxy-only).

    TTL = min(window_min, 3600) s: small windows stay fresh, deep windows avoid
    repeated cold 5-8 s fetches. Raw (unfiltered) bodies are cached; the
    worked-entity filter always runs fresh per request so a just-completed QSO
    shows immediately. Errors and ``ok:false`` bodies are never cached.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 64,
    ) -> None:
        self._clock = clock
        self._max_entries = max_entries
        self._entries: dict[tuple, tuple[float, dict[str, Any]]] = {}

    def get(self, key: tuple) -> dict[str, Any] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires, body = entry
        if self._clock() >= expires:
            del self._entries[key]
            return None
        return body

    def put(self, key: tuple, body: dict[str, Any], ttl_s: float) -> None:
        if len(self._entries) >= self._max_entries:
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            del self._entries[oldest]
        self._entries[key] = (self._clock() + ttl_s, body)


@dataclass
class AppState:
    """Composition root shared by the REST and WS layers."""

    auth: Any
    lease: Any    # unused: the platform's control role replaces the FT8 lease
    safety: Any
    sequencer: Any
    repository: Any
    allowed_hosts: frozenset[str] = frozenset()
    my_call: str = ""
    my_grid: str = ""
    rig: Any = None          # RigClient when wired
    mode_controller: Any = None  # Ft8ModeController when the platform wires it
    runtime: Any = None      # Ft8Runtime (orchestrator lives there once started)
    settings_store: Any = None  # Ft8SettingsStore (rig mode for the band tune)
    platform_dial: Any = None  # callable() -> int | None, the radio's real dial
    orchestrator: Any = None  # running orchestrator when wired
    latency: Any = None      # LatencyHistogram when the DSP path is wired
    tx_driver: Any = None    # TxDriver; always wired (Null encoder without DSP)
    cq_loop: Any = None      # wired by a later task
    state_broadcast: Any = None     # StateBroadcaster when the WS layer runs
    decode_broadcast: Any = None    # DecodeBroadcaster when the WS layer runs
    waterfall_fanout: Any = None    # SpectrumFanout when the WS layer runs
    revision: int = 0
    idempotency: IdempotencyCache = field(default_factory=IdempotencyCache)
    selected: ParsedMessage | None = None
    selected_snr_db: int | None = None
    selected_slot_id: int | None = None  # slot the selected message was heard in
    selected_freq: float | None = None  # audio offset the selected message was heard at
    occupancy: FrequencyOccupancy = field(default_factory=FrequencyOccupancy)
    radio_freq_hz: int | None = None  # last polled dial frequency, if rig is up
    rig_mode: str = "USB"  # operating mode re-asserted on every band tune
    last_manual_tune_mono: float | None = None  # manual band change (monotonic)
    _rig_level_cache: dict[str, Any] | None = None  # transient rig level probe cache
    last_tx: dict[str, Any] | None = None  # last transmitted message (desktop client)
    dxcc_cache: Any = None  # cached DxccSummary; rebuilt when repository.dxcc_dirty
    worked_index: Any = None  # cached WorkedIndex; rebuilt with dxcc_cache
    band_hunt_url: str | None = None  # pskreporter /api/band_hunt (NFR-088); None = off
    band_hunt_cache: _BandHuntCache = field(default_factory=_BandHuntCache)
    device_config: Any = None  # DeviceConfigStore when wired

    def bump(self) -> int:
        self.revision += 1
        return self.revision

    def actor(self, session: Session) -> str:
        """Audit handle: a session prefix, never the cookie secret (NFR-075)."""

        return f"session-{session.id[:8]}"


def _ok(body: dict[str, Any] | None = None, status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, **(body or {})}, status_code=status)


def _parse_freq(value: object) -> float | None:
    """Coerce a client-supplied audio offset (Hz); None when unusable.

    Bools are ints in Python and must not pass as 1 Hz/0 Hz offsets.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        freq = float(value)
    except (TypeError, ValueError):  # pragma: no cover - guarded by isinstance
        return None
    return freq if freq > 0 else None


def _reject(status: int, reason: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": False, "reason": reason, **extra}, status_code=status)


def _cty_database() -> Any:
    """Shared lazy cty.dat singleton (engine layer; API + auto-call use it)."""

    from ..engine.dxcc import get_cty_database

    return get_cty_database()


async def _fresh_dxcc_cache(state: Any) -> None:
    """Rebuild state.dxcc_cache only when missing or a QSO write happened
    since the last build (repository.dxcc_dirty).  Runs the scan off-thread;
    sub-second thanks to the indexed lookup (Task 1)."""
    if state.dxcc_cache is None or state.repository.dxcc_dirty:
        from ..engine.dxcc import dxcc_summary, worked_index

        state.dxcc_cache = await asyncio.to_thread(
            dxcc_summary, state.repository, _cty_database()
        )
        state.worked_index = await asyncio.to_thread(
            worked_index, state.repository, _cty_database()
        )
        state.repository.dxcc_dirty = False


def get_state(request: Request) -> AppState:
    return getattr(request.app.state, "ft8_state", None) or request.app.state.app_state


async def require_session(request: Request) -> Session:
    state = get_state(request)
    session = state.auth.authenticate(request.cookies.get(COOKIE_NAME))
    if session is None:
        raise _HttpReject(_reject(401, REASON_UNAUTHENTICATED))
    return session


async def validate_mutation(request: Request) -> None:
    """Host/Origin validation plus idempotency/revision for mutations."""

    state = get_state(request)
    # Host/Origin ACL: the platform's own request policy already gates the
    # app; when the caller injects host_check we additionally apply it.
    host_check = getattr(state, "host_check", None)
    if host_check is not None and not host_check(request):
        raise _HttpReject(_reject(403, REASON_FORBIDDEN))
    expected = request.headers.get("x-expected-revision")
    if expected is not None:
        try:
            if int(expected) != state.revision:
                raise ValueError
        except ValueError:
            raise _HttpReject(
                _reject(409, REASON_STALE_REVISION, revision=state.revision)
            ) from None


class _HttpReject(Exception):
    def __init__(self, response: JSONResponse) -> None:
        self.response = response


async def require_lease(
    request: Request, session: Session = Depends(require_session)
) -> Session:
    """TX-affecting endpoints need the platform's control role.

    The FT8 lease object is gone: ``server.py`` already has one global
    control role (and one TX session), so this check maps onto it instead
    of inventing a second ownership system.
    """
    state = get_state(request)
    role_of = getattr(state.auth, "role_of", None)
    # Platform roles: "operator" (full control) / "listener" (read-only).
    if role_of is not None and role_of(session.id) != "operator":
        raise _HttpReject(_reject(403, REASON_FORBIDDEN))
    return session


def attach_ft8_router(app: FastAPI, state: AppState) -> None:
    """Mount the FT8 REST surface on the platform's FastAPI app.

    ``app.state.ft8_state`` is how ``get_state`` finds the composition root;
    the reject handler is installed once per app.
    """

    app.state.ft8_state = state

    if not getattr(app.state, "_ft8_reject_handler", False):
        @app.exception_handler(_HttpReject)
        async def _on_reject(_request: Request, exc: _HttpReject) -> JSONResponse:  # type: ignore[misc]
            return exc.response

        app.state._ft8_reject_handler = True

    app.include_router(create_router(state))


def create_app(state: AppState) -> FastAPI:
    """Standalone app for tests only: REST without the platform."""

    app = FastAPI(title="MRRC-FT8", docs_url=None, redoc_url=None, openapi_url=None)
    attach_ft8_router(app, state)
    return app


def create_router(state: AppState) -> APIRouter:
    """Build the ``/api/v1`` router bound to one composition root."""

    router = APIRouter(prefix="/api/ft8")

    async def mutate(
        request: Request,
        key: str | None,
        status: int,
        body: dict[str, Any],
    ) -> JSONResponse:
        """Store and return a mutation response for idempotent replay."""

        state.bump()
        if state.state_broadcast is not None:
            state.state_broadcast.publish(_snapshot(state, None))
        response = _ok({**body, "revision": state.revision}, status=status)
        if key:
            state.idempotency.store(key, status, {"ok": True, **body, "revision": state.revision})
        return response

    def replay(request: Request) -> JSONResponse | None:
        key = request.headers.get("idempotency-key")
        if not key:
            return None
        hit = state.idempotency.lookup(key)
        if hit is None:
            return None
        status, body = hit
        return JSONResponse(body, status_code=status, headers={"x-idempotent-replay": "true"})

    # ---- session ---------------------------------------------------------

    @router.post("/mode")
    async def set_mode(request: Request, session: Session = Depends(require_lease)) -> JSONResponse:
        """Switch the whole station between SSB and FT8 (global mode).

        Control role only (it moves the radio and hands TX to the FT8
        engine); the controller serializes concurrent switches and rolls
        back to SSB if the engine fails to start.
        """
        await validate_mutation(request)
        body = await request.json()
        target = str((body or {}).get("mode", "")).lower()
        if target not in ("ssb", "ft8"):
            return _reject(422, "invalid_request", detail="mode must be 'ssb' or 'ft8'")
        controller = state.mode_controller
        if controller is None:
            return _reject(409, "ft8_unavailable")
        try:
            if target == "ft8":
                await controller.switch_to_ft8()
            else:
                await controller.switch_to_ssb()
        except Exception as exc:                 # engine start failure rolls back
            log.exception("mode switch failed")
            return _reject(500, "mode_switch_failed", detail=str(exc))
        state.bump()
        return _ok({"mode": str(controller.mode)})

    @router.post("/radio/band")
    async def radio_band(request: Request, session: Session = Depends(require_lease)) -> JSONResponse:
        """Tune the dial to a band's FT8 frequency (the page's band selector).

        Frequency only: the operating mode was set when FT8 mode started,
        and re-asserting it on every tune belongs to the platform's own
        band handling.
        """
        await validate_mutation(request)
        body = await request.json()
        try:
            freq = int((body or {}).get("freq_hz", 0))
        except (TypeError, ValueError):
            freq = 0
        if not 100_000 <= freq <= 60_000_000:
            return _reject(422, "invalid_request", detail="freq_hz out of range")
        if state.rig is None:
            return _reject(409, "rig_unavailable")
        try:
            await state.rig.set_frequency(freq)
            # Re-assert the FT8 operating mode as well: a band change on the
            # radio can restore a band-stack mode (LSB on 14 MHz, measured), and
            # the page only ever wants the data mode.  Frequency alone leaves
            # the station undecodable with no visible cause.
            settings = None
            if getattr(state, "settings_store", None) is not None:
                settings = state.settings_store.load()
            await state.rig.set_mode(
                getattr(settings, "rig_mode", "DATA-U") or "DATA-U", 3000
            )
            await state.rig.set_filter_width(3000)
        except Exception as exc:
            log.warning("band tune failed: %s", exc)
            return _reject(500, "rig_error", detail=str(exc))
        state.radio_freq_hz = freq
        runtime = getattr(state, "runtime", None)
        if runtime is not None and hasattr(runtime, "set_dial_frequency"):
            runtime.set_dial_frequency(freq)
        state.bump()
        return _ok({"freq_hz": freq})

    @router.get("/state")
    async def state_snapshot(session: Session = Depends(require_session)) -> JSONResponse:
        # _ok envelope: boot() gates on ``snapshot.ok`` before applying the
        # initial snapshot (worked_calls drives hide-already-worked); a bare
        # dict made the gate always falsy and the client relied on the WS
        # hello alone.
        return _ok(_snapshot(state, session))

    @router.get("/health")
    async def health(session: Session = Depends(require_session)) -> dict[str, Any]:
        return _health(state)

    # ---- operation ----------------------------------------------------------

    @router.post("/operation/select")
    async def operation_select(
        request: Request, session: Session = Depends(require_lease)
    ) -> JSONResponse:
        await validate_mutation(request)
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("dx_call"), str) or not body["dx_call"]:
            return _reject(422, "invalid_request")
        # Selecting never arms or transmits (§15.6).
        state.selected = ParsedMessage(
            text=str(body.get("text") or body["dx_call"]),
            is_cq=bool(body.get("is_cq", False)),
            from_call=body["dx_call"].upper(),
            grid=str(body.get("dx_grid") or "").upper(),
        )
        state.selected_snr_db = body.get("snr_db") if isinstance(body.get("snr_db"), int) else None
        state.selected_slot_id = body.get("slot_id") if isinstance(body.get("slot_id"), int) else None
        state.selected_freq = _parse_freq(body.get("freq"))
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"selected": state.selected.from_call})

    @router.post("/operation/reply")
    async def operation_reply(request: Request, session: Session = Depends(require_lease)) -> JSONResponse:
        await validate_mutation(request)
        if (hit := replay(request)) is not None:
            return hit
        content_length = request.headers.get("content-length")
        body = await request.json() if content_length and content_length != "0" else {}
        if isinstance(body, dict) and isinstance(body.get("dx_call"), str) and body["dx_call"]:
            # Single-round-trip double-click: select + arm in one request so a
            # manual reply races the current slot's fit window with one RTT
            # (the fit deadline is ~2.2 s into the slot).  Mirrors
            # ``operation/select`` exactly; selecting never transmits by itself.
            state.selected = ParsedMessage(
                text=str(body.get("text") or body["dx_call"]),
                is_cq=bool(body.get("is_cq", False)),
                from_call=body["dx_call"].upper(),
                grid=str(body.get("dx_grid") or "").upper(),
            )
            state.selected_snr_db = body.get("snr_db") if isinstance(body.get("snr_db"), int) else None
            state.selected_slot_id = body.get("slot_id") if isinstance(body.get("slot_id"), int) else None
            state.selected_freq = _parse_freq(body.get("freq"))
        if state.selected is None or state.selected_snr_db is None:
            return _reject(409, "no_selection")
        try:
            await state.safety.arm()
        except TxRefused as exc:
            return _reject(409, REASON_INTERLOCK_OPEN, detail=str(exc))
        # UC-003: transmit on the slot opposite the one the partner's message
        # was heard in; a message without a known slot defaults to even.
        slot_id = state.selected_slot_id
        tx_phase = 0 if slot_id is None else 1 - (slot_id % 2)
        # UC-003: transmit on the audio offset the partner was decoded at so
        # their receiver pairs the reply; clients without ``freq`` keep the
        # station default.
        tx_frequency = (
            state.selected_freq
            if state.selected_freq and state.selected_freq > 0
            else DEFAULT_TX_AUDIO_FREQUENCY
        )
        state.sequencer.reply_to(
            state.selected,
            state.selected_snr_db,
            tx_phase=tx_phase,
            tx_frequency=tx_frequency,
        )
        await _audit(state, session, "reply", state.selected.from_call, "")
        return await mutate(
            request,
            request.headers.get("idempotency-key"),
            200,
            {
                "sequencer": state.sequencer.state.value,
                "scheduled_tx": _scheduled_tx(tx_phase),
            },
        )

    @router.post("/operation/cq")
    async def operation_cq(request: Request, session: Session = Depends(require_lease)) -> JSONResponse:
        await validate_mutation(request)
        if (hit := replay(request)) is not None:
            return hit
        content_length = request.headers.get("content-length")
        body = await request.json() if content_length and content_length != "0" else {}
        if not isinstance(body, dict):
            return _reject(422, "invalid_request")
        loop = bool(body.get("loop", False))
        if loop:
            if state.cq_loop is None:
                return _reject(503, "cq_loop_unavailable")
            await state.cq_loop.start()  # arms via safety; refusal is audited
            if not state.cq_loop.active:
                return _reject(409, REASON_INTERLOCK_OPEN)
        else:
            try:
                await state.safety.arm()
            except TxRefused as exc:
                return _reject(409, REASON_INTERLOCK_OPEN, detail=str(exc))
            # UC-004: CQ on an unoccupied offset near the station default so
            # the call does not overlap another signal; falls back to the
            # default when the band around it is full.
            state.sequencer.start_cq(
                tx_frequency=state.occupancy.pick_frequency()
            )
        await _audit(state, session, "cq", "", "loop" if loop else "")
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"sequencer": state.sequencer.state.value})

    @router.post("/operation/enable_tx_off")
    async def operation_enable_tx_off(request: Request, session: Session = Depends(require_lease)) -> JSONResponse:
        await validate_mutation(request)
        if (hit := replay(request)) is not None:
            return hit
        state.safety.disarm(DisarmReason.MANUAL)
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"armed": False})

    @router.post("/operation/clear-fault")
    async def operation_clear_fault(request: Request, session: Session = Depends(require_lease)) -> JSONResponse:
        """Release latched interlock faults after the operator verified the
        repair (§15.5): without this path a transient fault locks TX until
        process restart.  Clearing never arms by itself."""

        await validate_mutation(request)
        if (hit := replay(request)) is not None:
            return hit
        content_length = request.headers.get("content-length")
        body = await request.json() if content_length and content_length != "0" else {}
        if not isinstance(body, dict):
            return _reject(422, "invalid_request")
        raw = body.get("interlock")
        if raw is not None and (not isinstance(raw, str) or raw not in Interlock):
            return _reject(422, "invalid_request")
        targets = (
            [Interlock(raw)]
            if raw is not None
            else sorted(state.safety.faults, key=lambda f: f.value)
        )
        for interlock in targets:
            state.safety.clear_fault(interlock)
        scope = raw if raw is not None else f"all ({len(targets)} latched)"
        await _audit(state, session, "clear_fault", "", scope)
        return await mutate(
            request, request.headers.get("idempotency-key"), 200, {"safety": state.safety.health}
        )

    @router.post("/operation/stop")
    async def operation_stop(request: Request, session: Session = Depends(require_session)) -> JSONResponse:
        """Priority STOP: any authenticated session, no lease (NFR-038)."""

        await validate_mutation(request)
        if (hit := replay(request)) is not None:
            return hit
        await state.safety.stop_tx(reason=f"api:{state.actor(session)}")
        await _audit(state, session, "stop", "", "priority STOP TX")
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"stopped": True})

    @router.get("/logs/qsos")
    async def logs_qsos(request: Request, session: Session = Depends(require_session)) -> JSONResponse:
        # Optional ``since_days`` window (desktop client logbook); default 7
        # keeps the mobile PWA and legacy callers unchanged.
        raw = request.query_params.get("since_days")
        since_days = 7
        if raw is not None:
            try:
                since_days = float(raw)
            except (TypeError, ValueError):
                return _reject(422, "invalid_request")
            if not 1 <= since_days <= 3650:
                return _reject(422, "invalid_request")
        qsos = await asyncio.to_thread(state.repository.list_qsos, since_days=since_days)
        # _ok envelope: the drawer's api.qsos() gate is ``res.ok`` (same as
        # every mutation); a bare dict made the log overlay always take the
        # error branch even on 200 ("Could not load log: 200").
        return _ok({"qsos": [_qso_view(q) for q in qsos], "revision": state.revision})

    @router.post("/logs/qsos/{qso_id}/void")
    async def logs_void(request: Request, qso_id: int, session: Session = Depends(require_session)) -> JSONResponse:
        await validate_mutation(request)
        if (hit := replay(request)) is not None:
            return hit
        body = await request.json() if request.headers.get("content-length") else {}
        reason = body.get("reason", "") if isinstance(body, dict) else ""
        try:
            await asyncio.to_thread(
                state.repository.void_qso,
                qso_id,
                actor=state.actor(session),
                reason=str(reason),
            )
        except KeyError:
            return _reject(404, "not_found")
        except VoidWindowExpired:
            return _reject(409, "void_window_expired")
        except ValueError as exc:
            return _reject(409, "invalid_state", detail=str(exc))
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"voided": qso_id})

    @router.get("/logs/adif")
    async def logs_adif(session: Session = Depends(require_session)) -> PlainTextResponse:
        qsos = await asyncio.to_thread(
            state.repository.list_qsos, include_void=False, since_days=7
        )
        document = await asyncio.to_thread(generate_adif, qsos)
        return PlainTextResponse(
            document,
            media_type="text/plain",
            headers={"content-disposition": 'attachment; filename="mrrc-ft8.adi"'},
        )

    # ---- DXCC -------------------------------------------------------------

    @router.get("/dxcc")
    async def dxcc(session: Session = Depends(require_session)) -> JSONResponse:
        # 低频数据：缓存到首次打开/任何 QSO 写入（dirty）后才重建（决策 A）。
        await _fresh_dxcc_cache(state)
        return _ok(state.dxcc_cache.to_dict())

    @router.get("/settings")
    async def settings_view(session: Session = Depends(require_session)) -> dict[str, Any]:
        values = await asyncio.to_thread(_read_settings, state)
        return {"settings": values, "schema": sorted(SETTING_SCHEMA)}

    @router.put("/settings")
    async def settings_update(request: Request, session: Session = Depends(require_session)) -> JSONResponse:
        await validate_mutation(request)
        body = await request.json()
        if not isinstance(body, dict) or not body:
            return _reject(422, "invalid_request")
        for key, value in body.items():
            validator = SETTING_SCHEMA.get(key)
            if validator is None or not validator(value):
                return _reject(422, "invalid_setting", setting=key)
        if (
            state.safety.armed or state.safety.ptt_on
        ) and SAFETY_IMPACTING_SETTINGS.intersection(body):
            return _reject(409, REASON_TX_ACTIVE)  # §10.5: safety-impacting locked during TX
        for key, value in body.items():
            await asyncio.to_thread(state.repository.set_setting, key, value)
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"updated": sorted(body)})

    # ---- station device configuration (spec 2026-08-10) ----------------------
    return router


# ---- helpers -----------------------------------------------------------------


def _session_view(session: Session) -> dict[str, Any]:
    return {
        "created_epoch": session.created_epoch,
        "reauth_recent": session.reauth_epoch is not None,
    }


def _dial_hz(state: AppState) -> int | None:
    """The radio's real dial frequency (falls back to the cached value)."""

    if getattr(state, "platform_dial", None) is not None:
        try:
            value = state.platform_dial()
            if value:
                return int(value)
        except Exception:                     # never fail a snapshot on this
            log.debug("platform dial unavailable", exc_info=True)
    return state.radio_freq_hz


def _snapshot(state: AppState, session: Session | None) -> dict[str, Any]:
    lease = state.lease.current() if state.lease is not None else None
    sequencer = state.sequencer
    snapshot: dict[str, Any] = {
        "revision": state.revision,
        "lease": {
            "held": lease is not None,
            "mine": session is not None
            and lease is not None
            and lease.session_id == session.id,
        },
        "safety": state.safety.health,
        "sequencer": {
            "state": sequencer.state.value,
            "tx_enabled": sequencer.tx_enabled,
            "dx_call": sequencer.dx_call,
            "cq_loop": (
                state.cq_loop.status()
                if state.cq_loop is not None
                else {"active": False, "idle_remaining_s": 0}
            ),
        },
        "selected": (
            None
            if state.selected is None
            else {"call": state.selected.from_call, "grid": state.selected.grid}
        ),
        "radio": {"freq_hz": _dial_hz(state)},
        "last_tx": state.last_tx,
        "station": {
            "my_call": state.my_call,
            "my_grid": state.my_grid,
            "worked_calls": sorted(state.repository.worked_calls()),
        },
    }
    orchestrator = state.orchestrator
    if orchestrator is None and getattr(state, "runtime", None) is not None:
        orchestrator = state.runtime.orchestrator
    if orchestrator is not None:
        counters = orchestrator.counters
        snapshot["orchestrator"] = {
            "slots_started": counters.slots_started,
            "slots_skipped": counters.slots_skipped,
            "decodes": counters.decodes,
            "deadline_misses": counters.deadline_misses,
        }
    return snapshot


def _health(state: AppState) -> dict[str, Any]:
    health: dict[str, Any] = {
        "safety": state.safety.health,
        "lease_held": state.lease is not None and state.lease.current() is not None,
        "sessions": state.auth.session_count(),   # was a bound method upstream: not serializable
    }
    if state.latency is not None:
        health["decode_latency"] = state.latency.snapshot()
    orchestrator = state.orchestrator
    if orchestrator is None and getattr(state, "runtime", None) is not None:
        orchestrator = state.runtime.orchestrator
    if orchestrator is not None:
        counters = orchestrator.counters
        health["deadline_misses"] = counters.deadline_misses
    return health


def _scheduled_tx(tx_phase: int) -> dict[str, Any]:
    """Best-effort next TX slot for an armed reply, fit-deadline aware.

    UC-003/I9: a reply can only transmit on slots of ``tx_phase`` parity, and
    a 12.64 s waveform must start by ~2.2 s into the slot (the fit guard).
    Returns the slot the ``TxDriver`` will use so the UI can tell the operator
    when the reply actually goes out instead of leaving them guessing.
    """

    now = time.time()
    period = FT8_PERIOD_SECONDS
    cur = int(now // period)  # pi-lens-ignore: unchecked-throwing-call-python
    cur_start = cur * period
    # The encode round-trip is reserved: the driver encodes at arm time and
    # only transmits if the waveform fits AFTER the encode (honest fit).
    fit_deadline = (
        cur_start + period - TX_WAVEFORM_SECONDS - TX_FIT_MARGIN_SECONDS - TX_ENCODE_SECONDS
    )
    if tx_phase == cur % 2 and now <= fit_deadline:
        return {
            "slot_id": cur,
            "utc": time.strftime("%H%M%S", time.gmtime(cur_start)),
            "deferred": False,
        }
    nxt = cur + 1
    while nxt % 2 != tx_phase:
        nxt += 1
    return {
        "slot_id": nxt,
        "utc": time.strftime("%H%M%S", time.gmtime(nxt * period)),
        "deferred": True,
    }


async def _audit(
    state: AppState, session: Session, operation: str, target: str, detail: str
) -> None:
    await asyncio.to_thread(
        state.repository.record_audit,
        actor=state.actor(session),
        operation=operation,
        target=target,
        detail=detail,
    )


def _qso_view(qso: Any) -> dict[str, Any]:
    # DXCC entity name from the cty database (server-authoritative) so the
    # client's logbook can show it without a local cty.dat.  Additive key;
    # None when the call has no entity.
    entity = _cty_database().lookup(qso.dx_call)
    return {
        "id": qso.id,
        "my_call": qso.my_call,
        "dx_call": qso.dx_call,
        "dx_grid": qso.dx_grid,
        "report_sent": qso.report_sent,
        "report_rcvd": qso.report_rcvd,
        "started_utc": qso.started_utc,
        "mode": qso.mode,
        "freq_hz": qso.freq_hz,
        "band": qso.band,
        "status": qso.status.value,
        "completed_epoch": qso.completed_epoch,
        "dxcc_entity": entity[0] if entity else None,
    }


def _read_settings(state: AppState) -> dict[str, Any]:
    return {key: state.repository.get_setting(key) for key in sorted(SETTING_SCHEMA)}


def _build_diagnostic_bundle(state: AppState) -> bytes:
    """Raw local bundle; auth secrets and cookie values are never included."""

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in {
            "health.json": _health(state),
            "settings.json": _read_settings(state),
            "qsos.json": [_qso_view(q) for q in state.repository.list_qsos()],
            "audit.json": state.repository.audit_events(),
        }.items():
            data = json.dumps(payload, indent=2, default=str).encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()
