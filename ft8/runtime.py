"""FT8 runtime: assemble the engine pieces and own their lifecycle.

One instance per server.  ``start()`` is called when the operator switches
into FT8 mode and ``stop()`` on the way back to SSB — the DSP worker is not
kept resident (design §4.6).  Everything hardware-facing is injected: the
platform's CAT (as a ``PlatformRig``), the platform's TX gate player and the
platform RX tap's device rate.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from ft8.core.models import DecodeConfig, auto_thread_count
from ft8.core.supervisor import WorkerSupervisor
from ft8.engine.audio_rx import UtcRing
from ft8.engine.dsp_decode import SupervisorDecoder
from ft8.engine.dsp_encode import SupervisorEncoder
from ft8.engine.latency import LatencyHistogram
from ft8.engine.orchestrator import Orchestrator
from ft8.engine.repository import Repository
from ft8.engine.safety import SafetyController
from ft8.engine.sequencer import Sequencer
from ft8.engine.tx_driver import TxDriver
from ft8.engine.waterfall import SpectrumComputer, SpectrumFanout
from ft8.platform.rx_tap import RxTap

log = logging.getLogger("mrrc.ft8.runtime")

DEFAULT_PROFILE = 3


class Ft8Runtime:
    """The FT8 engine, dormant until FT8 mode starts it."""

    def __init__(self, *, rig: Any, tx_player: Any, sequencer_factory: Callable[[], Sequencer],
                 data_dir: Path | str, dev_rate: int = 44_100,
                 on_decode: Callable[[Any], None] | None = None,
                 on_event: Callable[[Any], None] | None = None,
                 on_slot_start: Callable[[int], None] | None = None,
                 on_waterfall: Callable[[bytes], None] | None = None) -> None:
        self._rig = rig
        self._tx_player = tx_player
        self._sequencer_factory = sequencer_factory
        self._data_dir = Path(data_dir)
        self._dev_rate = dev_rate
        self._on_decode = on_decode
        self._on_event = on_event
        self._on_slot_start = on_slot_start
        self._on_waterfall = on_waterfall

        self.repository = Repository(str(self._data_dir / "qso.db"))
        # Built here (cheap, no hardware) so the REST layer can read state in
        # SSB mode too; start() only brings up the worker and the orchestrator.
        self.sequencer: Sequencer = sequencer_factory()
        self.safety: SafetyController = SafetyController(
            rig, tx_player, sequencer=self.sequencer, on_event=on_event
        )
        self.decoder: SupervisorDecoder | None = None
        self.encoder: SupervisorEncoder | None = None
        self.tx_driver: TxDriver | None = None
        self.orchestrator: Orchestrator | None = None
        self.ring: UtcRing | None = None
        self.rx_tap: RxTap | None = None
        self.latency = LatencyHistogram()
        self.recent_decodes: deque[Any] = deque(maxlen=40)   # wire batches for replay
        self._dial_freq_hz: int | None = None
        self._worked: Any = None
        self._worked_dirty = True
        self._dumped_slots = 0
        self._wf_logged = False
        self.waterfall = SpectrumFanout()
        self._supervisor: WorkerSupervisor | None = None
        self._task: asyncio.Task[Any] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the DSP worker, the orchestrator and the RX tap."""
        if self.running:
            return
        sequencer = self.sequencer
        supervisor = WorkerSupervisor()
        await asyncio.to_thread(supervisor.start)
        self._supervisor = supervisor
        profile = DEFAULT_PROFILE
        try:
            stored = self.repository.get_setting("decoder_profile")
            if isinstance(stored, int) and 0 <= stored <= 4:
                profile = stored
        except Exception:
            log.debug("decoder_profile unavailable; using %d", DEFAULT_PROFILE)
        self.decoder = SupervisorDecoder(
            supervisor,
            DecodeConfig(profile=profile, threads=auto_thread_count()),
            histogram=self.latency,
        )
        self.encoder = SupervisorEncoder(supervisor)
        self.tx_driver = TxDriver(sequencer, self.encoder, self.safety)

        self.ring = UtcRing()
        computer = SpectrumComputer()

        def spectrum_tap(block: Any, epoch: float) -> None:
            # Exceptions here would vanish into the RX loop's broad except;
            # log them so a broken waterfall is diagnosable in the field.
            try:
                frames = computer.push(block, epoch)
            except Exception:
                log.exception("waterfall compute failed")
                return
            for frame in frames:
                self.waterfall.publish(frame)
                if self._on_waterfall is not None:
                    # Compact WF01 binary (SpectrumFrame.to_bytes) — the PWA's
                    # parseFrame/push render it unchanged.
                    payload = frame.to_bytes()
                    if not self._wf_logged:
                        self._wf_logged = True
                        log.info("waterfall forwarding (%d bytes/line)", len(payload))
                    try:
                        self._on_waterfall(payload)
                    except Exception:
                        log.exception("waterfall forward failed")

        self.rx_tap = RxTap(self.ring, dev_rate=self._dev_rate, on_block=spectrum_tap)
        await self.safety.start()

        def read_slot(slot_id: int) -> bytes | None:
            assert self.ring is not None
            return self.ring.read_slot(slot_id)

        self.orchestrator = Orchestrator(
            self.decoder,
            read_slot,
            sequencer,
            on_decode=self._on_batch,
            on_decode_error=lambda slot_id, error: log.error("decode slot %s: %s", slot_id, error),
            on_slot_start=self._slot_started,
        )
        self._task = asyncio.create_task(self.orchestrator.run(), name="ft8_orchestrator")
        log.info("FT8 engine started (profile %d, threads %d)", profile, auto_thread_count())

    # ---- decode → wire batch (the FT8 UI's Band Activity feed) -----------

    def set_dial_frequency(self, freq_hz: int | None) -> None:
        """The platform tells the runtime where the dial is (band for DXCC)."""

        self._dial_freq_hz = int(freq_hz) if freq_hz else None

    def _worked_index(self):
        """Lazily built WorkedIndex (rebuilt when the repository says dirty)."""

        from ft8.engine.dxcc import get_cty_database, worked_index

        if self._worked is None or self._worked_dirty:
            try:
                self._worked = worked_index(self.repository, get_cty_database())
                self._worked_dirty = False
            except Exception:
                log.debug("worked index unavailable", exc_info=True)
                return None
        return self._worked

    def _on_batch(self, slot_decode: Any) -> None:
        """One SlotDecode → the ``{slot_id, messages:[…]}`` batch the UI renders.

        Mirrors MRRC-FT8's ``decode_message_view`` + ``decode_highlight``:
        call/grid/is_cq/to_me/mine for the row, DXCC entity and the JTDX-style
        highlight category when a worked index is available.
        """

        from ft8.engine.bands import band_from_freq_hz
        from ft8.engine.dxcc import decode_highlight, get_cty_database
        from ft8.engine.msgparse import addressed_to, base_call

        my_call = self.sequencer.my_call or ""
        band = band_from_freq_hz(self._dial_freq_hz or 0)
        index = self._worked_index()
        cty = get_cty_database()
        views: list[dict[str, Any]] = []
        for message in getattr(slot_decode, "messages", ()):
            parsed, result = message.parsed, message.result
            call = getattr(parsed, "from_call", "") or ""
            mine = bool(call) and bool(my_call) and base_call(call) == base_call(my_call)
            entity, is_new = "", False
            if call and not mine:
                hit = cty.lookup(call)
                entity = hit[0] if hit else ""
            view = {
                "text": result.text,
                "snr": result.snr,
                "dt": round(float(result.dt), 1),
                "freq": round(float(result.frequency), 1),
                "call": call,
                "grid": getattr(parsed, "grid", "") or "",
                "is_cq": bool(getattr(parsed, "is_cq", False)),
                "to_me": addressed_to(parsed, my_call),
                "mine": mine,
                "is_new_dxcc": is_new,
                "entity": entity,
            }
            view["highlight"] = decode_highlight(
                call=view["call"], grid=view["grid"], entity=entity, band=band,
                to_me=view["to_me"], mine=mine, index=index,
            )
            views.append(view)

        if not views and self._dumped_slots < 2:
            # Field diagnosis: keep the exact 12 kHz slot the decoder rejected.
            try:
                raw = self.ring.read_slot(getattr(slot_decode, "slot_id", -1)) if self.ring else None
                if raw:
                    path = Path(f"/tmp/ft8-slot-{getattr(slot_decode, 'slot_id', 0)}.raw")
                    path.write_bytes(raw)
                    self._dumped_slots += 1
                    log.info("dumped rejected slot audio to %s (%d bytes)", path, len(raw))
            except Exception:
                log.debug("slot dump failed", exc_info=True)
        log.info("slot %s: %d message(s) from %d result(s)",
                 getattr(slot_decode, "slot_id", "?"), len(views),
                 len(getattr(getattr(slot_decode, "batch", None), "results", ()) or ()))
        frame = {
            "type": "decodes",
            "slot_id": getattr(slot_decode, "slot_id", 0),
            "late": bool(getattr(slot_decode, "late", False)),
            "messages": views,
        }
        self.recent_decodes.append(frame)
        if self._on_decode is not None:
            self._on_decode(frame)

    def _slot_started(self, slot_id: int) -> None:
        if self.tx_driver is not None:
            asyncio.create_task(self.tx_driver.on_slot_start(slot_id))
        if self._on_slot_start is not None:
            self._on_slot_start(slot_id)

    async def stop(self) -> None:
        """Stop TX first, then the orchestrator and the DSP worker."""
        self.safety.disarm()
        with suppress(Exception):
            await self.safety.stop_tx("mode-switch")
        if self.orchestrator is not None:
            with suppress(Exception):
                self.orchestrator.stop()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        # Detach the tap BEFORE closing the worker so the platform RX loop
        # can never hand audio to a runtime that is going away.
        self.rx_tap = None
        if self.decoder is not None:
            with suppress(Exception):
                self.decoder.close()
            self.decoder = None
        if self.encoder is not None:
            with suppress(Exception):
                self.encoder.close()
            self.encoder = None
        if self._supervisor is not None:
            with suppress(Exception):
                await asyncio.to_thread(self._supervisor.stop)
            self._supervisor = None
        log.info("FT8 engine stopped")
