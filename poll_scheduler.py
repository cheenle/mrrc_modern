"""
Poll Scheduler
==============
Background asyncio tasks that poll the radio at different rates.
Tiered polling: high-frequency for freq/mode/S-meter, medium for
TX status/meters, low for settings, very low for telemetry.

Also handles serial connection monitoring and auto-reconnect.

Radio-specifics live in the active backend: the settings/slow/meter
poll tables come from ``backend.settings_poll_items()`` /
``slow_poll_items()`` / ``tx_meter_items()`` / ``always_meter_items()``
(see backends/base.py); this module only owns the polling mechanics
(intervals, skip/stale-read guards, PTT preemption, watchdog).
"""
import asyncio
import logging
import time  # Added missing import
from typing import Optional, Callable, Awaitable

from backends.ft710.cat_controller import CatController
from radio_state import RadioState
from config import (
    POLL_IF_INTERVAL, POLL_VFO_INTERVAL, POLL_TX_STATUS_INTERVAL, POLL_TX_METERS_INTERVAL,
    POLL_SETTINGS_INTERVAL, POLL_SLOW_INTERVAL, POLL_TIMEOUT,
)

logger = logging.getLogger("ft710.poll")


class PollScheduler:
    """Manages background polling tasks for the radio."""

    def __init__(
        self,
        cat: CatController,
        state: RadioState,
        on_state_changed: Optional[Callable[[], Awaitable[None]]] = None,
        on_reconnected: Optional[Callable[[], Awaitable[None]]] = None,
        backend=None,
    ):
        self.cat = cat
        self.state = state
        # The active RadioBackend — source of the settings/slow/meter
        # poll tables and the parsed initial_state_sync.  Optional so
        # legacy tests can drive the scheduler with a bare fake CAT.
        self._backend = backend
        self._on_state_changed = on_state_changed  # async callback for broadcasts
        # Called once after a successful watchdog reconnect + state re-sync.
        # server.py wires this to backend.init_scope(): a USB re-enumeration
        # resets the radio's scope output (FT-710: EX040101), so without
        # re-init the spectrum freezes after any serial hiccup.
        self._on_reconnected = on_reconnected
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._user_command_lock = asyncio.Lock()
        # Skip certain polls after a user set command to avoid echo
        self._skip_until: dict[str, float] = {}
        # Timestamp of the last user-initiated command; pollers pause briefly
        # so the user's next command doesn't queue behind a poll cycle.
        self._last_user_command: float = 0.0
        # How long (seconds) to pause background polling after a user command.
        self._user_command_pause: float = 0.3
        # ── Idle rate scaling ──────────────────────────────────────
        # When 0 control clients are connected, poll intervals are
        # multiplied by IDLE_MULTIPLIER to reduce CPU/Serial load.
        # Set via set_active() from server.py on client connect/disconnect.
        self._idle_multiplier: int = 1
        self.IDLE_MULTIPLIER: int = 4  # IF poll: 100ms→400ms, settings: 2s→8s, etc.
        # ── TX meter logging state ─────────────────────────────────
        # Track whether we've logged the first TX meter read (instance-level)
        self._tx_meter_first_logged: bool = False

    async def start(self):
        """Launch all background polling tasks."""
        self._running = True
        self._tasks = [
            asyncio.create_task(self._poll_if(), name="poll_if"),
            asyncio.create_task(self._poll_vfo(), name="poll_vfo"),
            asyncio.create_task(self._poll_tx_status(), name="poll_tx"),
            asyncio.create_task(self._poll_tx_meters(), name="poll_tx_meters"),
            asyncio.create_task(self._poll_settings(), name="poll_settings"),
            asyncio.create_task(self._poll_slow(), name="poll_slow"),
            asyncio.create_task(self._connection_watchdog(), name="conn_watch"),
        ]
        logger.info("Poll scheduler started (%d tasks)", len(self._tasks))

    async def stop(self):
        """Cancel all polling tasks."""
        self._running = False
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Poll scheduler stopped")

    def note_user_command(self):
        """Record that a user-initiated command was just sent.

        Called by server.py after every UI-triggered set command.
        Causes poll loops to briefly pause so the user's next command
        isn't stuck behind a queued poll cycle on the serial lock.
        """
        self._last_user_command = time.time()

    def set_active(self, has_clients: bool):
        """Scale poll intervals based on whether control clients exist.

        When no clients are connected, multiply all poll intervals by
        IDLE_MULTIPLIER (4x) to drastically reduce CPU / serial bus load.
        Called from server.py on each client connect/disconnect.
        """
        new = 1 if has_clients else self.IDLE_MULTIPLIER
        if new != self._idle_multiplier:
            old = self._idle_multiplier
            self._idle_multiplier = new
            logger.info(
                "Poll rate scaling: %s (multiplier %d→%d)",
                "active" if has_clients else "idle", old, new,
            )

    async def _polling_paused(self) -> bool:
        """Return True if background polling should yield for a user command.

        When the user is actively tuning or adjusting settings, each
        poll cycle sends 3+ serial commands (FA/MD0/SM0).  If a user
        command arrives during that cycle, it waits behind all of them.
        By pausing briefly after each user command, the next user command
        can grab the serial lock immediately.
        """
        return time.time() < self._last_user_command + self._user_command_pause

    def skip_next_poll(self, field: str, duration: float = 2.0):
        """Skip polling for a given field for `duration` seconds.

        Called after a user-initiated set command to avoid echoing
        the value back before the radio actually processes it.
        """
        self._skip_until[field] = time.time() + duration

    async def _should_skip(self, field: str) -> bool:
        until = self._skip_until.get(field, 0)
        return time.time() < until

    # ── Tier 1: High-frequency (freq + mode + S-meter via IF) ────

    async def _poll_if(self):
        """Poll freq+mode+S-meter at high frequency.

        Uses dedicated FA/MD0/SM0 commands instead of IF; because
        the FT-710's IF response format is binary/BCD and harder to
        parse reliably across firmware versions.
        """
        failures = 0
        _loop_count = 0
        _last_logged_freq = None
        while self._running:
            try:
                if await self._polling_paused():
                    await asyncio.sleep(0.05)
                    continue
                if self.cat._cancel_polls.is_set():
                    await asyncio.sleep(0.01)
                    continue
                if self.cat.connected:
                    changes = {}
                    if not await self._should_skip("if"):
                        # 3 queries only (FA/MD0/SM0) to keep this cycle
                        # short (~120 ms) so PTT/sets aren't blocked on the
                        # serial lock.  VS (active VFO) and FB (VFO-B freq)
                        # change rarely and are polled at 0.5 s in _poll_vfo.
                        # Inter-query pause checks let a user command preempt
                        # after the in-flight query (~40 ms).
                        if not await self._polling_paused() and not self.cat._cancel_polls.is_set():
                            freq = await self.cat.get_frequency("A", timeout=POLL_TIMEOUT)
                            if freq is not None and 30000 <= freq <= 75000000:
                                # Guard: if a user frequency-setting command was
                                # issued while we were awaiting the serial response,
                                # discard the stale reading.  Two checks:
                                #   _should_skip("if")  – set by skip_next_poll (1 s)
                                #   _polling_paused()   – set by note_user_command (0.3 s)
                                # Together they cover the gap between the command
                                # starting and skip_next_poll being called.
                                if not await self._should_skip("if") and not await self._polling_paused():
                                    changes["vfo_a_freq"] = freq
                                    # Only log significant frequency changes (>1kHz) or periodically
                                    if (_last_logged_freq is None
                                            or (freq != _last_logged_freq
                                                and abs(freq - _last_logged_freq) > 1000)
                                            or _loop_count % 500 == 0):
                                        _delta = ""
                                        if _last_logged_freq is not None:
                                            _delta = f" ({freq - _last_logged_freq:+d} Hz)"
                                        logger.info(
                                            "IF poll: vfo_a=%d Hz%s (loop=%d)",
                                            freq, _delta, _loop_count)
                                        _last_logged_freq = freq
                        if not await self._polling_paused() and not self.cat._cancel_polls.is_set():
                            mode = await self.cat.get_mode(timeout=POLL_TIMEOUT)
                            if mode is not None:
                                changes["mode"] = mode
                        if not await self._polling_paused() and not self.cat._cancel_polls.is_set():
                            sm = await self.cat.get_s_meter(timeout=POLL_TIMEOUT)
                            if sm is not None:
                                changes["s_meter"] = sm
                    if changes:
                        if not self.state.serial_connected:
                            # Poll recovered after a failure streak — flip the
                            # flag back.  Previously only the watchdog's full
                            # reconnect did this, so a transient timeout streak
                            # stuck the UI at "radio disconnected" forever.
                            changes["serial_connected"] = True
                            logger.info(
                                "IF poll recovered — serial_connected=True "
                                "(failures=%d)", failures)
                        changed = self.state.update(**changes)
                        if changed and self._on_state_changed:
                            await self._on_state_changed()
                        failures = 0
                    else:
                        failures += 1
                else:
                    failures += 1
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("IF poll error: %s", e)
                failures += 1

            # NOTE (2026-08-15): a previous patch flipped serial_connected
            # to False on failures>=5 here.  That was wrong: an IF poll
            # failure is a transient serial-contention timeout (tuning /
            # channel switches queue commands), NOT a radio disconnect — it
            # made the UI flash "电台未连接" in a 2-6 s loop.  Disconnect is
            # decided solely by the watchdog (cat.connected) below; IF poll
            # success still flips the flag back True (recovery path above).
            _loop_count += 1
            await asyncio.sleep(POLL_IF_INTERVAL * self._idle_multiplier)

    # ── Tier 1b: Active VFO + VFO-B freq (medium cadence) ──────────

    async def _poll_vfo(self):
        """Poll VS (active VFO) and FB (VFO-B freq) at medium cadence.

        These change rarely (only on user VFO switch / VFO-B tuning) so
        they don't need the 0.1 s fast-poll cadence — keeping them out
        of _poll_if shortens the fast cycle and keeps PTT/sets snappy.
        """
        while self._running:
            try:
                if await self._polling_paused():
                    await asyncio.sleep(0.05)
                    continue
                if self.cat._cancel_polls.is_set():
                    await asyncio.sleep(0.01)
                    continue
                if self.cat.connected and not await self._should_skip("vfo"):
                    changes = {}
                    active = await self.cat.get_active_vfo(timeout=POLL_TIMEOUT)
                    if active is not None:
                        # Guard: if skip_next_poll("vfo", …) was called
                        # while we were awaiting, discard the stale reading.
                        # Also check _polling_paused() to cover the gap
                        # between the command starting and skip_next_poll.
                        if not await self._should_skip("vfo") and not await self._polling_paused():
                            changes["active_vfo"] = active
                    if not await self._polling_paused() and not self.cat._cancel_polls.is_set():
                        freq_b = await self.cat.get_frequency("B", timeout=POLL_TIMEOUT)
                        if freq_b is not None and 30000 <= freq_b <= 75000000:
                            if not await self._should_skip("vfo") and not await self._polling_paused():
                                changes["vfo_b_freq"] = freq_b
                    if changes:
                        changed = self.state.update(**changes)
                        if changed and self._on_state_changed:
                            await self._on_state_changed()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("VFO poll error: %s", e)
            await asyncio.sleep(POLL_VFO_INTERVAL * self._idle_multiplier)

    # ── Tier 2B: TX status ────────────────────────────────────────

    async def _poll_tx_status(self):
        """Poll TX; at medium frequency to detect radio-originated PTT changes."""
        failures = 0
        while self._running:
            try:
                if await self._polling_paused():
                    await asyncio.sleep(0.05)
                    continue
                if self.cat._cancel_polls.is_set():
                    await asyncio.sleep(0.01)
                    continue
                if self.cat.connected and not await self._should_skip("tx_status"):
                    ptt = await self.cat.get_ptt(timeout=POLL_TIMEOUT)
                    if ptt is not None:
                        was_tx = self.state.tx_status > 0
                        changed = self.state.update(tx_status=ptt)
                        # Reset TX-only meters to zero when transitioning to RX,
                        # otherwise they keep the last TX reading forever
                        # (the TX-meters poller only runs during transmit).
                        if ptt == 0 and was_tx:
                            changed |= self.state.update(
                                power_meter=0, alc_meter=0,
                                swr_meter=0, comp_meter=0,
                                id_meter=0)
                        if changed and self._on_state_changed:
                            await self._on_state_changed()
                        failures = 0
                    else:
                        failures += 1
                else:
                    failures += 1
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("TX poll error: %s", e)
                failures += 1
            await asyncio.sleep(POLL_TX_STATUS_INTERVAL * self._idle_multiplier)

    # ── Tier 2A: TX meters (COMP, ALC, Power, SWR) — TX only ─────

    def _meter_lists(self) -> tuple[list, list]:
        """(tx_only_items, always_items): [(label, field, async getter)].

        Items come from the active backend; the capabilities gate drops
        Vd/Id entries when the radio has no drain meters.  Without a
        backend (legacy tests) the FT-710 RM3-RM8 table is used.
        """
        if self._backend is not None:
            tx_items = list(self._backend.tx_meter_items())
            always_items = list(self._backend.always_meter_items())
            caps = getattr(self._backend, "capabilities", None)
            if caps is not None and not caps.has_vd_id_meters:
                tx_items = [i for i in tx_items
                            if i[1] not in ("vd_meter", "id_meter")]
                always_items = [i for i in always_items
                                if i[1] not in ("vd_meter", "id_meter")]
            return tx_items, always_items
        cat = self.cat
        return (
            [("COMP", "comp_meter", lambda t: cat.get_meter("RM3", timeout=t)),
             ("ALC", "alc_meter", lambda t: cat.get_meter("RM4", timeout=t)),
             ("PWR", "power_meter", lambda t: cat.get_meter("RM5", timeout=t)),
             ("SWR", "swr_meter", lambda t: cat.get_meter("RM6", timeout=t)),
             ("ID", "id_meter", lambda t: cat.get_meter("RM7", timeout=t))],
            [("VD", "vd_meter", lambda t: cat.get_meter("RM8", timeout=t))],
        )

    async def _poll_tx_meters(self):
        """Poll the backend's TX meter items during transmit, and the
        always-on items on every cycle so those meters update at 0.5 s
        instead of the 5 s slow tier."""
        failures = 0
        tx_items, always_items = self._meter_lists()
        while self._running:
            try:
                if await self._polling_paused():
                    await asyncio.sleep(0.05)
                    continue
                # Yield immediately if a priority command (PTT/tune) is
                # pending — don't start new meter queries that would block it.
                if self.cat._cancel_polls.is_set():
                    await asyncio.sleep(0.01)
                    continue
                if self.cat.connected:
                    results = {}
                    missed = []
                    # TX-only meters (e.g. FT-710 RM3-RM7: COMP, ALC, Power,
                    # SWR, drain current — the latter only responds in TX).
                    if self.state.is_transmitting:
                        for label, field, getter in tx_items:
                            if await self._polling_paused() or self.cat._cancel_polls.is_set():
                                break
                            if not await self._should_skip(field):
                                v = await getter(POLL_TIMEOUT)
                                if v is not None:
                                    results[field] = v
                                else:
                                    missed.append(label)
                    # Always-on meters (e.g. FT-710 RM8 drain voltage,
                    # which responds during RX).
                    for label, field, getter in always_items:
                        if await self._polling_paused() or self.cat._cancel_polls.is_set():
                            break
                        if not await self._should_skip(field):
                            v = await getter(POLL_TIMEOUT)
                            if v is not None:
                                results[field] = v
                            else:
                                missed.append(label)

                    if results:
                        changed = self.state.update(**results)
                        if changed and self._on_state_changed:
                            await self._on_state_changed()
                        failures = 0
                        if (not self._tx_meter_first_logged
                                and self.state.is_transmitting):
                            logger.info(
                                "TX meters active: RF_PWR=%dW | %s",
                                self.state.rf_power,
                                " ".join(f"{label}={results[field]}"
                                         for label, field, _ in tx_items
                                         if field in results),
                            )
                            self._tx_meter_first_logged = True
                    else:
                        # Only warn if we expected results (TX mode) or if
                        # the always-on meter is consistently failing.
                        # During RX, TX-only meters may legitimately return None.
                        if self.state.is_transmitting:
                            failures += 1
                            logger.warning(
                                "TX meter poll: all queries returned None "
                                "(missed=%s, is_transmitting=%s, connected=%s)",
                                missed, self.state.is_transmitting, self.cat.connected,
                            )
                        else:
                            # RX: nothing to report — not an error
                            failures = 0
                else:
                    # Not transmitting — reset everything.
                    self._tx_meter_first_logged = False
                    failures = 0
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("TX meter poll error: %s", e, exc_info=True)
                failures += 1

            if failures >= 3:
                logger.warning(
                    "TX meter poll: %d consecutive empty cycles "
                    "(is_transmitting=%s, connected=%s)",
                    failures, self.state.is_transmitting, self.cat.connected,
                )
            # Fast 0.1s refresh during TX, normal 0.5s during RX
            interval = 0.1 if self.state.is_transmitting else POLL_TX_METERS_INTERVAL
            await asyncio.sleep(interval * self._idle_multiplier)

    # ── Tier 3: Settings (filter, gains, preamp, att, NR, NB, AN, tuner) ──

    async def _poll_settings(self):
        """Poll slowly-changing radio settings (backend-provided items).

        Each item is (field, async getter(timeout) -> value|None); the
        radio-specific query + parsing lives in the backend.
        """
        items = self._backend.settings_poll_items() if self._backend else []

        while self._running:
            try:
                if await self._polling_paused():
                    await asyncio.sleep(0.05)
                    continue
                if self.cat._cancel_polls.is_set():
                    await asyncio.sleep(0.01)
                    continue
                if self.cat.connected:
                    changes = {}
                    for field, getter in items:
                        # Yield between queries if a user command (PTT, tune,
                        # etc.) is pending — otherwise this 14-query cycle
                        # holds the serial lock for ~500 ms and stalls PTT.
                        if await self._polling_paused():
                            break
                        if await self._should_skip(field):
                            continue
                        value = await getter(POLL_TIMEOUT)
                        if value is None:
                            continue
                        # Re-check AFTER the await: a user set command
                        # may have arrived while this query was in
                        # flight.  The response then carries the
                        # pre-command (stale) value — applying it would
                        # snap the UI back to the old setting.  Same
                        # guard pattern as _poll_if / _poll_vfo.
                        if await self._should_skip(field) \
                                or await self._polling_paused():
                            continue
                        changes[field] = value
                    if changes:
                        changed = self.state.update(**changes)
                        if changed and self._on_state_changed:
                            await self._on_state_changed()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("Settings poll error: %s", e)
            await asyncio.sleep(POLL_SETTINGS_INTERVAL * self._idle_multiplier)

    # ── Tier 4: Slow telemetry (compressor, contour, AMC, RI) ────────
    # NOTE: id_meter/vd_meter live in Tier 2A (_poll_tx_meters) for
    # 10× faster refresh.

    async def _poll_slow(self):
        """Slow poll for misc telemetry (backend-provided items).

        Each item is (skip_key, async getter(timeout) -> dict); the
        getter returns RadioState field changes ({} on failure) — one
        query may yield several fields (e.g. the FT-710 RI response).
        """
        items = self._backend.slow_poll_items() if self._backend else []

        while self._running:
            try:
                if await self._polling_paused():
                    await asyncio.sleep(0.05)
                    continue
                if self.cat._cancel_polls.is_set():
                    await asyncio.sleep(0.01)
                    continue
                if self.cat.connected:
                    changes = {}
                    for skip_key, getter in items:
                        if not await self._should_skip(skip_key):
                            result = await getter(POLL_TIMEOUT)
                            if result:
                                changes.update(result)
                    if changes:
                        changed = self.state.update(**changes)
                        if changed and self._on_state_changed:
                            await self._on_state_changed()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("Slow poll error: %s", e)
            await asyncio.sleep(POLL_SLOW_INTERVAL * self._idle_multiplier)

    # ── Connection Watchdog ────────────────────────────────────────

    async def _connection_watchdog(self):
        """Monitor serial connection and attempt reconnection on failure."""
        while self._running:
            try:
                if not self.cat.connected:
                    self.state.update(serial_connected=False)
                    if self._on_state_changed:
                        await self._on_state_changed()
                    logger.warning("Serial disconnected, attempting reconnect...")
                    reconnected = await self.cat.reconnect_loop()
                    if reconnected:
                        # Perform full state sync after reconnect
                        logger.info("Reconnected! Performing state sync...")
                        if self._backend is not None:
                            sync_data = await self._backend.initial_state_sync()
                        else:
                            sync_data = await self.cat.initial_state_sync()
                        new_state = RadioState.from_sync_result(sync_data)
                        new_state.serial_connected = True
                        # Copy all fields
                        for field_name in vars(new_state):
                            if not field_name.startswith('_'):
                                setattr(self.state, field_name, getattr(new_state, field_name))
                        self.state.mark_dirty(*list(vars(new_state).keys()))
                        if self._on_state_changed:
                            await self._on_state_changed()
                        # Re-run scope init (EX040101/EX040200) — the radio's
                        # scope output does not survive a USB re-enumeration,
                        # and scope_pipe cannot recover without it.
                        if self._on_reconnected:
                            try:
                                await self._on_reconnected()
                            except Exception as e:
                                logger.warning("Post-reconnect hook failed: %s", e)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Watchdog error: %s", e)
            await asyncio.sleep(1.0)
