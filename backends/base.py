"""
Radio backend abstraction
=========================
Defines the contract every radio backend (FT-710, IC-7300, ...) must
implement so that ``server.py``/``poll_scheduler.py`` can drive any
supported rig through one interface.

- ``RadioCapabilities`` — static, JSON-serializable description of a
  radio model (audio rates, meter/filter/scope model, feature flags).
- ``RadioBackend`` — abstract CAT control surface.  The method set
  mirrors the de-facto public interface of the FT-710 ``CatController``
  (the only backend at this stage); Yaesu-internal helpers (``_write``,
  ``_read_until``) are deliberately excluded.
- ``ScopeProducer`` — minimal protocol for pushing parsed scope frames
  into the server's ``ScopeHandler``.

Scope note (Phase 1): the FT-710's scope machinery (FT4222 subprocess
spawn/read) lives in ``backends/ft710/scope_producer.py`` behind the
``ScopeProducer`` protocol; ``server.py`` only calls
``start()``/``stop()``/``notify_tx()`` and supplies the ``on_frame``
callback that merges scope metadata into ``RadioState``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Awaitable, Callable, Optional, Protocol


@dataclass
class RadioCapabilities:
    """Static description of a radio model, sent to the UI as JSON."""

    model_name: str                       # machine key, e.g. "ft710"
    display_name: str                     # human label, e.g. "Yaesu FT-710"
    default_baud: int
    audio_rx_rate: int                    # device sample rate, RX path
    audio_tx_rate: int                    # device sample rate, TX path
    audio_name_hints: tuple = ()          # substrings for USB audio auto-detect
    has_atu: bool = False                 # internal antenna tuner
    has_auto_notch: bool = False
    has_vd_id_meters: bool = False        # drain voltage/current meters
    vfo_b_direct: bool = False            # can address VFO-B directly (FB)
    filter_model: str = "width_table"     # "width_table" | "fil123"
    att_steps: tuple = ()                 # attenuator dB steps, e.g. (0, 6, 12, 18)
    preamp_steps: tuple = ()              # e.g. ("OFF", "AMP1", "AMP2")
    scope_type: str = "none"              # "ft4222" | "civ27" | "none"
    scope_spans: dict = field(default_factory=dict)
    tune_via: str = "tx2"                 # "tx2" (CAT tune carrier) | "atu"

    def to_dict(self) -> dict:
        """JSON-serializable representation for WebSocket/REST clients."""
        data = asdict(self)
        data["audio_name_hints"] = list(self.audio_name_hints)
        data["att_steps"] = list(self.att_steps)
        data["preamp_steps"] = list(self.preamp_steps)
        return data


class ScopeProducer(Protocol):
    """Minimal scope-data producer contract.

    The frame callback receives a parsed scope frame object (for FT-710,
    a ``backends.ft710.scope_frame.ScopeFrame``) destined for the
    server's ``ScopeHandler``.
    """

    async def start(self) -> None:
        """Start producing frames (spawn subprocess / open CI-V stream)."""
        ...

    async def stop(self) -> None:
        """Stop producing frames and release hardware."""
        ...

    def notify_tx(self, tx: bool) -> None:
        """Tell the producer the radio entered/left TX (streams may garble)."""
        ...

    def set_on_frame(self, cb: Callable[[Any], Awaitable[None]]) -> None:
        """Register the async callback invoked for each parsed frame."""
        ...


class RadioBackend(ABC):
    """Abstract CAT control surface shared by all radio backends."""

    @property
    @abstractmethod
    def capabilities(self) -> RadioCapabilities:
        """Static capabilities of this radio model."""
        ...

    def create_scope_producer(
        self,
        scope_handler: Any = None,
        on_frame: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> Optional[ScopeProducer]:
        """Return a ScopeProducer for this radio, or None when the radio
        has no scope stream (the server's S-meter fallback covers that).

        ``scope_handler`` is the server's shared ``ScopeHandler`` the
        producer writes parsed frames into; ``on_frame`` is the async
        callback invoked after each parsed frame.
        """
        return None

    async def boot_verify(self, cat, timeout: float = 0.4) -> bool:
        """After power-on, return True once the radio answers a boot check.

        ``cat`` is this backend's CAT controller.  The default is the
        Yaesu frequency read-back ("FA") used by the FT-710; Icom
        backends override with a CI-V read (the Yaesu FA command is
        invalid on CI-V, where 0xFA is the NG reply code).
        """
        return bool(await cat.query("FA", timeout=timeout))

    # ── UI Tables (per-radio, sent in the fullState push) ──────────

    @property
    def bands(self) -> list:
        """Band definitions for the UI band buttons."""
        return []

    @property
    def ui_modes(self) -> list:
        """Primary modes exposed in the UI cycle button (in order)."""
        return []

    @property
    def mode_name_to_num(self) -> dict:
        """UI mode name -> backend-native mode register number.

        Numbers are written to the radio as-is by ``set_mode()``, so each
        backend must return its own protocol's numbering (Yaesu register
        values for FT-710, CI-V mode bytes for Icom).  Default {} lets
        callers fall back to their legacy table.
        """
        return {}

    def filter_tables(self) -> dict:
        """Filter-width tables: {"voice": [...], "narrow": [...], "narrowModes": [...]}."""
        return {"voice": [], "narrow": [], "narrowModes": []}

    # ── Neutral-Layer Hooks (radio_state / poll_scheduler) ─────────

    def state_tables(self) -> dict:
        """Tables consumed by ``RadioState.configure(**tables)``.

        Keys: mode_num_to_name, preamp_labels, attenuator_labels,
        get_band_for_frequency, get_filter_hz, raw_to_dbm, raw_to_s_unit,
        raw_to_power, raw_to_swr, raw_to_voltage, raw_to_current.
        Default {} leaves RadioState on its built-in FT-710 tables.
        """
        return {}

    def settings_poll_items(self) -> list:
        """2s-tier poll items: [(field, async getter(timeout) -> value|None)].

        The getter returns the PARSED RadioState value (None on query
        failure); the scheduler applies its skip/stale-read guards.
        """
        return []

    def slow_poll_items(self) -> list:
        """5s-tier poll items: [(skip_key, async getter(timeout) -> dict)].

        The getter returns a dict of RadioState field changes ({} on
        failure) — a single query may yield several fields (FT-710 RI).
        """
        return []

    def tx_meter_items(self) -> list:
        """TX-only meter items: [(label, field, async getter(timeout))]."""
        return []

    def always_meter_items(self) -> list:
        """Always-polled meter items, same shape as tx_meter_items()."""
        return []

    # ── Scope Init Hook ────────────────────────────────────────────

    async def init_scope(self) -> None:
        """Radio-specific scope-enable sequence over CAT.

        Called once at startup (even when the initial CAT probe failed —
        the serial port may still work) and again after every serial
        reconnect (a USB re-enumeration resets the radio's scope output).
        Default: no-op (radio has no scope or needs no init).
        """
        return None

    # ── Connection Management ──────────────────────────────────────

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @property
    @abstractmethod
    def model(self) -> str: ...

    @abstractmethod
    async def connect(self) -> bool: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def reconnect_loop(self) -> bool: ...

    # ── Command Interface ──────────────────────────────────────────

    @abstractmethod
    async def send_command(self, cmd: str, timeout: Optional[float] = None) -> Optional[str]: ...

    @abstractmethod
    async def send_set_command(self, cmd: str) -> bool: ...

    @abstractmethod
    async def send_priority_set_command(self, cmd: str) -> bool: ...

    @abstractmethod
    async def query(self, cmd: str, timeout: Optional[float] = None) -> Optional[str]: ...

    @abstractmethod
    async def set(self, cmd: str) -> bool: ...

    # ── High-Level Command Helpers ─────────────────────────────────

    @abstractmethod
    async def set_frequency(self, freq_hz: int, vfo: str = "A") -> bool: ...

    @abstractmethod
    async def get_active_vfo(self, timeout: Optional[float] = None) -> Optional[str]: ...

    @abstractmethod
    async def get_frequency(self, vfo: str = "A", timeout: Optional[float] = None) -> Optional[int]: ...

    @abstractmethod
    async def set_mode(self, mode_num: int) -> bool: ...

    @abstractmethod
    async def get_mode(self, timeout: Optional[float] = None) -> Optional[int]: ...

    @abstractmethod
    async def set_ptt(self, tx: bool) -> bool: ...

    @abstractmethod
    async def set_tune(self, tune: bool) -> bool: ...

    @abstractmethod
    async def get_ptt(self, timeout: Optional[float] = None) -> Optional[int]: ...

    @abstractmethod
    async def get_s_meter(self, timeout: Optional[float] = None) -> Optional[int]: ...

    @abstractmethod
    async def get_info(self) -> Optional[dict]: ...

    @abstractmethod
    async def get_meter(self, meter: str, timeout: Optional[float] = None) -> Optional[int]: ...

    @abstractmethod
    async def set_filter_width(self, index: int) -> bool: ...

    @abstractmethod
    async def get_filter_width(self) -> Optional[int]: ...

    @abstractmethod
    async def set_af_gain(self, value: int) -> bool: ...

    @abstractmethod
    async def set_rf_gain(self, value: int) -> bool: ...

    @abstractmethod
    async def set_rf_power(self, value: int) -> bool: ...

    @abstractmethod
    async def set_preamp(self, value: int) -> bool: ...

    @abstractmethod
    async def set_attenuator(self, value: int) -> bool: ...

    @abstractmethod
    async def set_noise_blanker(self, on: bool) -> bool: ...

    @abstractmethod
    async def set_noise_reduction(self, on: bool) -> bool: ...

    @abstractmethod
    async def set_auto_notch(self, on: bool) -> bool: ...

    @abstractmethod
    async def set_compressor(self, on: bool) -> bool: ...

    @abstractmethod
    async def set_tuner(self, value: int) -> bool: ...

    @abstractmethod
    async def set_vfo(self, vfo: str) -> bool: ...

    @abstractmethod
    async def set_split(self, on: bool) -> bool: ...

    @abstractmethod
    async def set_power(self, on: bool) -> bool: ...

    @abstractmethod
    async def set_squelch(self, value: int) -> bool: ...

    @abstractmethod
    async def set_mic_gain(self, value: int) -> bool: ...

    @abstractmethod
    async def set_band_stack(self, bsr: int) -> bool: ...

    @abstractmethod
    async def set_antenna(self, ant: int) -> bool: ...

    @abstractmethod
    async def get_antenna(self) -> Optional[int]: ...

    @abstractmethod
    async def set_agc(self, value: int) -> bool: ...

    @abstractmethod
    async def get_agc(self) -> Optional[int]: ...

    @abstractmethod
    async def set_dnr(self, value: int) -> bool: ...

    @abstractmethod
    async def get_dnr(self) -> Optional[int]: ...

    @abstractmethod
    async def set_contour(self, value: int) -> bool: ...

    @abstractmethod
    async def get_contour(self) -> Optional[int]: ...

    @abstractmethod
    async def set_drive(self, value: int) -> bool: ...

    # ── Meter & Radio Info Commands ─────────────────────────────────

    @abstractmethod
    async def set_meter_display(self, meter: int) -> bool: ...

    @abstractmethod
    async def get_meter_display(self, timeout: Optional[float] = None) -> Optional[int]: ...

    @abstractmethod
    async def set_amc_level(self, level: int) -> bool: ...

    @abstractmethod
    async def get_amc_level(self, timeout: Optional[float] = None) -> Optional[int]: ...

    @abstractmethod
    async def get_radio_info(self, timeout: Optional[float] = None) -> Optional[dict]: ...

    # ── Scope/Spectrum Commands ────────────────────────────────────

    @abstractmethod
    async def set_scope_on(self, on: bool) -> bool: ...

    @abstractmethod
    async def get_scope_on(self) -> Optional[int]: ...

    @abstractmethod
    async def set_scope_span(self, span: int) -> bool: ...

    @abstractmethod
    async def set_scope_speed(self, speed: int) -> bool: ...

    @abstractmethod
    async def set_scope_mode(self, mode: int) -> bool: ...

    # ── Misc Settings ──────────────────────────────────────────────

    @abstractmethod
    async def set_nb_level(self, level: int) -> bool: ...

    @abstractmethod
    async def set_nr_level(self, level: int) -> bool: ...

    @abstractmethod
    async def set_compressor_level(self, level: int) -> bool: ...

    @abstractmethod
    async def set_monitor(self, on: bool) -> bool: ...

    @abstractmethod
    async def set_monitor_gain(self, value: int) -> bool: ...

    @abstractmethod
    async def set_vox(self, on: bool) -> bool: ...

    @abstractmethod
    async def set_break_in(self, on: bool) -> bool: ...

    @abstractmethod
    async def set_key_speed(self, speed: int) -> bool: ...

    @abstractmethod
    async def set_cw_pitch(self, pitch: int) -> bool: ...

    @abstractmethod
    async def set_rit(self, on: bool) -> bool: ...

    @abstractmethod
    async def set_rit_freq(self, freq: int) -> bool: ...

    @abstractmethod
    async def set_xit(self, on: bool) -> bool: ...

    # ── Bulk State Query ──────────────────────────────────────────

    @abstractmethod
    async def initial_state_sync(self) -> dict: ...
