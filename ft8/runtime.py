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
import os
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from ft8.core.models import DecodeConfig, auto_thread_count
from ft8.engine.orchestrator import (
    DEFAULT_DECISION_CUTOFF_SECONDS,
    FT8_PERIOD_SECONDS,
    boundary_epoch_for_slot,
)
from ft8.engine.audio_rx import DECODER_SAMPLE_RATE

#: How long before the nominal slot end to dispatch the decode.  An FT8
#: waveform occupies the first 12.64 s of the 15 s slot; 1.2 s leaves a
#: 1.16 s guard for signals with a large dt before the padded region could
#: clip them.  0 restores the upstream (wait-for-the-whole-slot) behaviour.
EARLY_DECODE_S = 1.2
FT8_SLOT_SECONDS = FT8_PERIOD_SECONDS
from ft8.core.supervisor import WorkerSupervisor
from ft8.engine.audio_rx import UtcRing
from ft8.engine.cascade import CascadePolicy, CascadingDecoder
from ft8.engine.dsp_decode import SupervisorDecoder
from ft8.engine.dsp_encode import SupervisorEncoder
from ft8.engine.latency import LatencyHistogram
from ft8.engine.orchestrator import Orchestrator
from ft8.engine.repository import Repository
from ft8.engine.safety import SafetyController
from ft8.engine.sequencer import QSOState, Sequencer
from ft8.engine.tx_driver import TxDriver
from ft8.engine.waterfall import SpectrumComputer, SpectrumFanout
from ft8.platform.cloud_decode import cloud_decoder_from_environ
from ft8.platform.rx_tap import RxTap
from ft8.platform.slot_dump import SlotDumper
from ft8.platform.settings import Ft8Settings

log = logging.getLogger("mrrc.ft8.runtime")

DEFAULT_PROFILE = 3

# 每多少个时隙打一行累计计数器（20 × 15 s = 5 分钟）。存在的理由见 `_log_counters`。
_COUNTERS_EVERY_SLOTS = 20


def _qso_progress(state: QSOState) -> int:
    """AP 上下文只在通联进行中成立：IDLE / DONE 记 0，其余阶段都算进行中。"""

    return 0 if state in (QSOState.IDLE, QSOState.DONE) else 2


def _dump_slot(dumper: SlotDumper | None, slot_id: int, samples: bytes | None) -> None:
    """Hand one slot to the recall dump, if the operator asked for one.

    Deliberately a free function taking ``None``: the common case (no dump
    configured) is one branch on a local, not a method call per slot.
    """

    if dumper is not None and samples is not None:
        dumper.write(slot_id, samples)


def _policy_from_settings(settings: Ft8Settings) -> CascadePolicy:
    """设置里的五个升级策略字段 → CascadePolicy。

    逐字段显式搬运，**不做任何 getattr 兜底**：字段名写错要当场 AttributeError，
    而不是静默用回默认值（R23 的教训 —— 一个看起来生效、其实没接上的旋钮）。
    """

    return CascadePolicy(
        escalate_on_ap_context=settings.escalate_on_ap_context,
        escalate_on_weak=settings.escalate_on_weak,
        min_decodes=settings.min_decodes,
        min_peak_snr=settings.min_peak_snr,
        escalate_on_request=settings.escalate_on_request,
    )


class Ft8Runtime:
    """The FT8 engine, dormant until FT8 mode starts it."""

    def __init__(self, *, rig: Any, tx_player: Any, sequencer_factory: Callable[[], Sequencer],
                 data_dir: Path | str, dev_rate: int = 44_100,
                 on_decode: Callable[[Any], None] | None = None,
                 on_event: Callable[[Any], None] | None = None,
                 on_slot_start: Callable[[int], None] | None = None,
                 on_waterfall: Callable[[bytes], None] | None = None,
                 cascade_policy_provider: Callable[[], CascadePolicy] | None = None) -> None:
        self._rig = rig
        self._tx_player = tx_player
        self._sequencer_factory = sequencer_factory
        self._data_dir = Path(data_dir)
        # Create it before Repository opens its database.  sqlite3 cannot create
        # missing parent directories, and on a fresh install nothing else has:
        # Ft8SettingsStore only mkdirs when it *saves*, so <runtime>/ft8/ does
        # not exist on a machine that has never run FT8 — and Repository's
        # OperationalError took the whole FT8 composition down with it (the
        # server then ran SSB-only, silently, which is how this was found: the
        # macOS clean-room run of the packaged app).
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._dev_rate = dev_rate
        self._on_decode = on_decode
        self._on_event = on_event
        self._on_slot_start = on_slot_start
        self._on_waterfall = on_waterfall
        # 设置派生的升级策略来源（Task 14）。注入而不是在这里造第二个
        # Ft8SettingsStore：store 的唯一所有者在 server.py，第二个实例是一条
        # 路径今天恰好等价、明天不等的静默双源头。缺省 None → start() 用 CascadePolicy()。
        self._cascade_policy_provider = cascade_policy_provider

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
        # The cascade wrapper handed to the orchestrator; ``self.decoder``
        # stays the closable SupervisorDecoder (the cascade has no close()).
        self._cascade: CascadingDecoder | None = None
        # Task 14's "deep decode this slot" switch flips this; False keeps
        # should_escalate's ``escalate_on_request and requested`` off.
        self._deep_decode_requested = False
        self.ring: UtcRing | None = None
        self.rx_tap: RxTap | None = None
        self.latency = LatencyHistogram()
        self.recent_decodes: deque[Any] = deque(maxlen=40)   # wire batches for replay
        self._dial_freq_hz: int | None = None
        self._worked: Any = None
        self._worked_dirty = True
        self._dumped_slots = 0
        # 本进程已交付的时隙数，只用来决定累计计数器那一行的节奏。
        self._slots_logged = 0
        self._wf_logged = False
        self.waterfall = SpectrumFanout()
        self._supervisor: WorkerSupervisor | None = None
        self._task: asyncio.Task[Any] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def cascade_stats(self) -> dict[str, int] | None:
        """级联解码器的运行期计数；引擎未启动时为 None。

        `deadline_risk` 来自级联解码器本身，两个计数器来自编排器 —— 升级率
        = `escalations / decodes`，是判断阈值松紧、以及判断深路径**有没有在跑**
        的主指标（2026-10-13 之前现场实测恒为 0：截止 2.5 s 把每一次升级都挡在
        预算守卫外）。

        `orchestrator` 用 `getattr(..., None)` 取：构造到 `start()` 之间的实例
        还没有它，此时少两个键而不是抛 AttributeError —— 否则 `/api/ft8/health`
        在引擎没起来时 500。与 `_cascade` 同一约定。
        """

        cascade = getattr(self, "_cascade", None)
        if cascade is None:
            return None
        stats = {
            "deadline_risk": cascade.deadline_risk,
            "cloud_ok": cascade.cloud_ok,
            "cloud_fallback": cascade.cloud_fallback,
            "cloud_seconds": cascade.cloud_seconds,
        }
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is not None:
            counters = orchestrator.counters
            stats["escalations"] = counters.escalations
            stats["fast_decodes"] = counters.fast_decodes
        return stats

    def set_cascade_policy(self, settings: Ft8Settings) -> None:
        """把持久设置里的升级策略推给**正在跑**的级联解码器。

        ``/station`` 保存成功后调用；引擎未启动时 ``self._cascade`` 是 None，
        此时无事可做（下一次 ``start()`` 会经 provider 拿到同一份设置）。
        判空不能省 —— 与 ``self.decoder`` 同样的空值约定。
        """

        if self._cascade is not None:
            self._cascade.set_policy(_policy_from_settings(settings))

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

        # Early decode: an FT8 waveform ends at 12.64 s, so the tail of the
        # 15 s slot carries no signal and waiting for it is pure latency.
        # The orchestrator stops waiting EARLY_DECODE_S sooner and this
        # source serves a slot whose tail is still in flight, zero-padded by
        # the ring.  Returns are unchanged for a slot that is already whole.
        early_min_samples = int((FT8_SLOT_SECONDS - EARLY_DECODE_S) * DECODER_SAMPLE_RATE)

        def read_slot(slot_id: int) -> bytes | None:
            assert self.ring is not None
            full = self.ring.read_slot(slot_id)
            if full is not None:
                _dump_slot(self._slot_dump, slot_id, full)
                return full
            # 提前解码走的是这一支（尾巴零补齐），所以**两条路都要落盘** ——
            # 提前派发的那一份就是解码器实际吃进去的输入。
            partial = self.ring.read_slot_partial(slot_id, early_min_samples)
            _dump_slot(self._slot_dump, slot_id, partial)
            return partial

        # The upstream grace (0.4 s) assumes an ~85 ms capture chain.  On the
        # W103D the chain is guest-buffer(100 ms) + 80 ms read block + soxr
        # (~80 ms) + scheduling — measured as a STABLE +0.8 s offset between
        # the tap's timeline and the wall clock.  It does NOT stay there: after
        # any TX the platform reopens the capture stream and the offset settles
        # at ~+1.8 s (measured), which a 1.5 s grace cannot cover — every slot
        # was then skipped and decoding stopped dead after a QSO.  The grace is
        # only an upper bound (polling dispatches the instant the slot lands),
        # so 3.0 s costs nothing when the chain is quick and covers the
        # post-TX chain when it is not.
        #
        # Note the coupling with EARLY_DECODE_S: the wait now starts 1.2 s
        # before the slot ends, so the effective budget for the capture chain
        # is grace - EARLY_DECODE_S.  A grace of 1.5 s therefore left only
        # 0.3 s and the 1.8 s post-TX chain blew straight through it — the
        # exact "no decodes after a QSO" field report.  Keep grace comfortably
        # above EARLY_DECODE_S + the measured chain offset.
        # 级联解码：默认走快路径，按策略在同一时隙内升级到 WSJT-X 深路径。
        # clock 一律不传 —— CascadingDecoder 的默认 time.time 与 orchestrator 的
        # clock（`orchestrator.py:125`）同源，这正是 R20 要钉住的那个不变量。
        # cutoff **必须显式传**，且必须传 orchestrator 的那一个常量：级联的预算守卫
        # 与 orchestrator 判定 deadline miss 用的是同一条物理量，各自写死只会在
        # 有人改了 `decision_cutoff` 之后悄悄分叉（见 cascade.py 里那段注记）。
        #
        # 不要写成 ``self.decoder = CascadingDecoder(self.decoder, …)``：stop() 会调
        # ``self.decoder.close()``，而 CascadingDecoder 没有 close()，那一行又裹在
        # suppress(Exception) 里 —— worker 与共享内存段会被静默泄漏。级联对象另存
        # ``self._cascade``，只喂给 orchestrator。
        #
        # 策略从注入的 provider 取（Task 14）：写死 ``CascadePolicy()`` 会让每次重启
        # 都退回默认阈值，用户存过的策略只在本次进程内有效。provider 缺席（如
        # tests/ft8/test_runtime_cascade_wiring.py 那种直接构造）才退回默认值。
        policy = (
            self._cascade_policy_provider()
            if self._cascade_policy_provider is not None
            else CascadePolicy()
        )
        # 云端深路径（spec 2026-10-14，A1）：只在 MRRC_FT8_CLOUD_DECODE_URL 非空时
        # 存在。默认为 None —— 那是一台没配云端（也是每一台出厂盒子）的默认行为，
        # 级联因此与引入它之前逐字一致。
        cloud = cloud_decoder_from_environ(os.environ)
        self._cloud = cloud
        # 召回测量的素材（设计 §12）：默认关闭，只在 MRRC_FT8_SLOT_DUMP_DIR
        # 设了才存在。写的是解码器实际收到的那一份字节。
        self._slot_dump = SlotDumper.from_environ(os.environ)
        self._cascade = CascadingDecoder(
            self.decoder,
            policy,
            cutoff=DEFAULT_DECISION_CUTOFF_SECONDS,
            boundary_epoch_for=boundary_epoch_for_slot,
            ap_context=lambda: (sequencer.my_call, _qso_progress(sequencer.state)),
            requested=lambda: self._deep_decode_requested,
            remote=cloud,
            cloud_deadline_s=(cloud.deadline_s if cloud is not None else 2.5),
        )
        self.orchestrator = Orchestrator(
            self._cascade,
            read_slot,
            sequencer,
            delivery_grace=3.0,
            early_decode_s=EARLY_DECODE_S,
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

    def _escalation_was_cloud(self, batch: Any) -> bool:
        """Whether this escalated batch was answered by the hub rather than the box.

        A separate marker token rather than a suffix on ``escalated``: the field
        greps that word and the counters line, and changing their shape would
        break every one of those greps (the 2026-10-14 ones) for a fact that
        only became interesting when the cloud channel existed.
        """

        if not getattr(batch, "escalated", False):
            return False
        cascade = getattr(self, "_cascade", None)
        return getattr(cascade, "last_deep_path", "") == "cloud"

    def _on_batch(self, slot_decode: Any) -> None:
        """One SlotDecode → the ``{slot_id, messages:[…]}`` batch the UI renders.

        Mirrors MRRC-FT8's ``decode_message_view`` + ``decode_highlight``:
        call/grid/is_cq/to_me/mine for the row, DXCC entity and the JTDX-style
        highlight category when a worked index is available.
        """

        # Task 14:「深度解码本槽」是一次性动作。批次回来就说明这一槽的 decode
        # 已经结束，语义正好是「只对这一槽有效」，所以在这里清回 False。
        # 不清的后果不是"多跑一次"，而是从按下那一刻起**每个时隙**都升级，
        # 每槽稳定多付 ~2.2 s 深解码 —— 一个「按一次、此后永久变慢」的按钮。
        # 放在函数最前面：即便下面任何一行抛异常，标志也已经清掉。
        self._deep_decode_requested = False

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
        # 路径与两个标记必须进这一行：深路径**成功**的升级在 cascade.py 里一个字都不
        # 记（那里只记拒绝），`late` 更是只活在 WebSocket 帧里。少了它们，现场就只能
        # 从发布偏移去推「这一槽升级了吗」，而这条推断已经被证伪过两次（原生库 banner
        # 不是深路径标记；空槽也慢，偏移本身还在漂）。
        batch = getattr(slot_decode, "batch", None)
        log.info("slot %s: %d message(s) from %d result(s) [%s%s%s%s]",
                 getattr(slot_decode, "slot_id", "?"), len(views),
                 len(getattr(batch, "results", ()) or ()),
                 getattr(batch, "path", "?"),
                 " escalated" if getattr(batch, "escalated", False) else "",
                 " cloud" if self._escalation_was_cloud(batch) else "",
                 " late" if getattr(slot_decode, "late", False) else "")
        # Counts batches (= decodes), NOT slots: a skipped or errored slot never
        # reaches this line.  It is the log cadence only — the slot total the
        # operator reads comes from ``counters.slots_started`` (see _log_counters).
        self._slots_logged += 1
        if self._slots_logged % _COUNTERS_EVERY_SLOTS == 0:
            self._log_counters()
        slot_id = int(getattr(slot_decode, "slot_id", 0))
        dispatched = getattr(slot_decode, "dispatched_epoch", None)
        finished = getattr(slot_decode, "finished_epoch", None)
        # Real-world latency accounting for the operator (and for field
        # diagnosis): when the decode actually finished, and how long after
        # the slot it was dispatched/finished.
        timing = {
            "slot_start_epoch": slot_id * 15.0,
            "slot_end_epoch": (slot_id + 1) * 15.0,
            "dispatched_epoch": dispatched,
            "finished_epoch": finished,
        }
        if dispatched is not None:
            timing["dispatched_after_slot_end_s"] = round(dispatched - (slot_id + 1) * 15.0, 2)
        if finished is not None:
            timing["finished_after_slot_end_s"] = round(finished - (slot_id + 1) * 15.0, 2)
        for view in views:
            view["finished_epoch"] = finished
            view["finished_after_slot_end_s"] = timing.get("finished_after_slot_end_s")
        frame = {
            "type": "decodes",
            "slot_id": slot_id,
            "late": bool(getattr(slot_decode, "late", False)),
            "timing": timing,
            "messages": views,
        }
        self.recent_decodes.append(frame)
        if self._on_decode is not None:
            self._on_decode(frame)

    def _log_counters(self) -> None:
        """累计计数器进日志 —— 这是 ``/api/ft8/engine/stats`` 的日志版。

        2026-10-14 现场：Phase A 让深路径跑起来之后，操作员问「多少槽升级了、多少槽
        掉出截止线」，而日志答不了 —— 升一次级没有任何日志，`late` 只在帧里。能读的
        数字只有一个要 30 天会话 cookie 的 HTTP 接口。于是诊断只能靠推断，推错两次。

        键名写成 ``k=v`` 是为了可 grep 也可断言（tests/ft8/test_runtime_slot_counters.py）。
        引擎没起来时 ``orchestrator`` 是 None，此时静默返回 —— 与 ``cascade_stats``
        同一约定，绝不为了打日志抛异常。
        """

        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            return
        counters = orchestrator.counters
        rate = (100.0 * counters.escalations / counters.decodes) if counters.decodes else 0.0
        # ``slots_started``, not ``self._slots_logged``: the latter counts
        # ``_on_batch`` calls, i.e. slots that produced a decode, so it always
        # equals ``decodes`` — printing it as "slot(s)" reports a number that
        # cannot answer the question this line exists for ("is the receiver
        # hearing anything?"), because a skipped slot (no data within the grace
        # window) never reaches ``_on_batch`` and leaves no trace.  ``skipped``
        # is what makes that visible; 2026-10-14, a field reading of
        # ``decodes == N`` was briefly read as "no slots were skipped", which the
        # line could not support.  The cadence trigger stays on the batch count
        # on purpose — it is a log cadence, not a field the operator reads.
        log.info(
            "decode counters after %d slot(s): decodes=%d fast=%d "
            "escalations=%d (%.1f%%) late=%d errors=%d skipped=%d",
            counters.slots_started, counters.decodes, counters.fast_decodes,
            counters.escalations, rate, counters.deadline_misses,
            counters.decode_errors, counters.slots_skipped,
        )

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
