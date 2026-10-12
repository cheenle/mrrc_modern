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
                 on_slot_start: Callable[[int], None] | None = None) -> None:
        self._rig = rig
        self._tx_player = tx_player
        self._sequencer_factory = sequencer_factory
        self._data_dir = Path(data_dir)
        self._dev_rate = dev_rate
        self._on_decode = on_decode
        self._on_event = on_event
        self._on_slot_start = on_slot_start

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
        self.recent_decodes: deque[Any] = deque(maxlen=500)
        self._decode_seq = 0
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
        self.decoder = SupervisorDecoder(
            supervisor,
            DecodeConfig(profile=DEFAULT_PROFILE, threads=auto_thread_count()),
            histogram=self.latency,
        )
        self.encoder = SupervisorEncoder(supervisor)
        self.tx_driver = TxDriver(sequencer, self.encoder, self.safety)

        self.ring = UtcRing()
        computer = SpectrumComputer()

        def spectrum_tap(block: Any, epoch: float) -> None:
            for frame in computer.push(block, epoch):
                self.waterfall.publish(frame)

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
        log.info("FT8 engine started (profile %d, threads %d)", DEFAULT_PROFILE, auto_thread_count())

    def _on_batch(self, batch: Any) -> None:
        """Buffer the slot's decodes (web layer) and fan out to the callback."""

        self._decode_seq += 1
        for result in getattr(batch, "results", ()):
            self.recent_decodes.append({
                "seq": self._decode_seq,
                "slot_id": getattr(batch, "slot_id", 0),
                "text": getattr(result, "text", ""),
                "snr": getattr(result, "snr", 0),
                "dt": round(float(getattr(result, "dt", 0.0)), 1),
                "freq_hz": round(float(getattr(result, "frequency", 0.0)), 1),
            })
        if self._on_decode is not None:
            self._on_decode(batch)

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
