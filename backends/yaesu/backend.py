"""Profile-driven RadioBackend for the Yaesu ASCII-CAT family.

Structure follows `backends/ic7300/backend.py`: every table, capability and
poll item is derived from ``self._profile``, and each model class overrides
only two attributes.  Two mechanisms implement the unverified-model boundary
of spec 2026-09-12 §6: `_tx_allowed()` (PTT/TUNE refused until the operator
sets MRRC_ALLOW_UNVERIFIED_TX=1) and `_check_model_identity()` (read-only
`ID;` logging, warn-only on mismatch).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import config

from backends.base import RadioBackend, RadioCapabilities
from backends.yaesu.cat_core import YaesuCatController
from backends.yaesu.yaesu_profiles import (
    YaesuModelProfile, band_entry, get_profile,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:                                # pragma: no cover
    from backends.yaesu.scu_lan.client import ScuLanClient

# One warning per process: the gate is a configuration state, not a
# per-keystroke event (a log flood would bury real failures).
_TX_GATE_WARNED = False


def _log_tx_gate_once(profile: YaesuModelProfile) -> None:
    global _TX_GATE_WARNED
    if _TX_GATE_WARNED:
        return
    _TX_GATE_WARNED = True
    logger.warning(
        "%s is not hardware-verified — transmit refused. Set "
        "MRRC_ALLOW_UNVERIFIED_TX=1 and restart to enable TX after checking "
        "the radio with _diag_yaesu.py.", profile.display_name)


class YaesuBackend(RadioBackend):
    """RadioBackend for Yaesu ASCII-CAT radios (profile-driven)."""

    _profile: YaesuModelProfile = get_profile("ftdx10")
    _display_name = "Yaesu"

    def __init__(self, port: str, baud_rate: Optional[int] = None):
        self._port = port
        #: The SCU-LAN10 session and its serial lookalike, when
        #: ``MRRC_SCU_LAN=1`` and the three credentials are present.  Both stay
        #: None on every other wiring, which keeps the serial path untouched.
        self._scu_client = None
        self._scu_transport = None
        transport = self._make_scu_transport()
        self._cat = YaesuCatController(port, baud_rate, profile=self._profile,
                                       transport=transport)

    # ── SCU-LAN10 network transport (design 2026-11-15 §10) ──────────

    def _scu_lan_params(self):
        """``(host, user, password, base_port)`` or None, naming what is missing.

        A missing key is *named* rather than silently degrading: the operator
        has no way to see that the box is still looking for a serial port that
        does not exist on this wiring.
        """
        if not bool(getattr(config, "SCU_LAN", False)):
            return None
        host = (str(getattr(config, "SCU_LAN_HOST", "") or "").strip())
        user = (str(getattr(config, "SCU_LAN_USER", "") or "").strip())
        password = (str(getattr(config, "SCU_LAN_PASS", "") or "").strip())
        base_port = getattr(config, "SCU_LAN_PORT", 50000)
        missing = [name for name, value in (
            ("MRRC_SCU_LAN_HOST", host),
            ("MRRC_SCU_LAN_USER", user),
            ("MRRC_SCU_LAN_PASS", password),
        ) if not value]
        if missing:
            logger.warning(
                "MRRC_SCU_LAN=1 but %s is not set — using the serial port "
                "instead (the SCU-LAN10 holds the radio's own USB port, so "
                "this wiring cannot work without them)", ", ".join(missing))
            return None
        return host, user, password, base_port

    def _make_scu_transport(self):
        params = self._scu_lan_params()
        if params is None:
            return None
        from backends.yaesu.scu_lan.client import ScuLanClient
        from backends.yaesu.scu_lan.transport import ScuLanCatTransport
        host, user, password, base_port = params
        self._scu_client = ScuLanClient(host, user, password,
                                        base_port=base_port)
        self._scu_transport = ScuLanCatTransport(self._scu_client)
        logger.info("%s: CAT/scope/audio over the SCU-LAN10 at %s:%s "
                    "(no serial port, no sound card)",
                    self._display_name, host, base_port)
        return self._scu_transport

    def network_client(self) -> Optional["ScuLanClient"]:
        """The SCU-LAN10 session (None on the serial wiring)."""
        return self._scu_client

    def network_state(self) -> Optional[dict]:
        """JSON-safe LAN link state for ``/api/state`` and the support bundle."""
        if self._scu_client is None:
            return None
        return self._scu_client.snapshot()

    # ── Capabilities ────────────────────────────────────────────────

    @property
    def capabilities(self) -> RadioCapabilities:
        p = self._profile
        return RadioCapabilities(
            model_name=p.model_key,
            display_name=self._display_name,
            default_baud=p.default_baud,
            audio_rx_rate=p.audio_rx_rate,
            audio_tx_rate=p.audio_tx_rate,
            audio_name_hints=p.audio_name_hints,
            has_atu=p.has_atu,
            has_auto_notch=True,
            has_vd_id_meters=p.has_vd_id_meters,
            vfo_b_direct=p.vfo_b_direct,
            filter_model="width_table",
            att_steps=p.att_steps,
            preamp_steps=tuple(p.preamp_labels.values()),
            scope_type=("scu_lan" if self._scu_client is not None else "none"),
            scope_spans={},
            scope_speeds=(),
            tune_via=p.tune_via,
            tuner_tune_cmd=p.tuner_tune_cmd,
            verified=p.verified,
            tx_gated=not self._tx_allowed(),
            dual_rx=p.dual_rx,
            unverified_meters=p.unverified_meters,
            audio_gain_boost=p.audio_gain_boost,
        )

    def _tx_allowed(self) -> bool:
        if config.ALLOW_UNVERIFIED_TX:
            return True
        if not self._profile.verified:
            _log_tx_gate_once(self._profile)
            return False
        return True

    # ── UI tables (from the profile) ────────────────────────────────

    @property
    def bands(self) -> list:
        """`{name, start, end, default_freq}` — the shape the server and every
        client read (see `yaesu_profiles.band_entry`)."""
        return [band_entry(*band) for band in self._profile.bands]

    @property
    def ui_modes(self) -> list:
        return list(self._profile.ui_modes)

    @property
    def mode_name_to_num(self) -> dict:
        return dict(self._profile.mode_numbers)

    def filter_tables(self) -> dict:
        p = self._profile
        voice, narrow = [], []
        narrow_modes = ("CW-U", "CW-L", "RTTY-U", "RTTY-L", "DATA-U", "DATA-L")
        for mode, widths in p.filter_widths.items():
            (narrow if mode in narrow_modes else voice).extend(
                w for w in widths if w not in (narrow if mode in narrow_modes else voice))
        return {"voice": voice, "narrow": narrow,
                "narrowModes": sorted(narrow_modes)}

    def state_tables(self) -> dict:
        """Profile tables for RadioState.configure()."""
        p = self._profile
        cal = p.s_meter_cal
        return {
            "mode_num_to_name": {v: k for k, v in p.mode_numbers.items()},
            "preamp_labels": {str(k): v for k, v in p.preamp_labels.items()},
            "attenuator_labels": ({i: "ON" if i else "OFF"
                                   for i in range(len(p.att_steps))}
                                  if p.attenuator_is_on_off else
                                  {i: (f"{db} dB" if db else "OFF")
                                   for i, db in enumerate(p.att_steps)}),
            "get_band_for_frequency": self._band_for_frequency,
            "get_filter_hz": self._filter_hz,
            "raw_to_dbm": cal.value,
            "raw_to_s_unit": cal.s_unit,
            "raw_to_power": config.RAW_TO_METER_TABLES["power"](p.power_max_w),
            "raw_to_swr": config.RAW_TO_METER_TABLES["swr"](),
            "raw_to_voltage": config.RAW_TO_METER_TABLES["voltage"](),
            "raw_to_current": config.RAW_TO_METER_TABLES["current"](),
        }

    def _band_for_frequency(self, freq_hz: int) -> Optional[dict]:
        for label, low, high in self._profile.bands:
            if low <= freq_hz <= high:
                return {"name": label, "low": low, "high": high}
        return None

    def _filter_hz(self, mode_name: str, index: int) -> Optional[int]:
        for idx, hz in self._profile.filter_widths.get(mode_name, ()):
            if idx == index:
                return hz
        return None

    # ── Poll items ──────────────────────────────────────────────────

    def settings_poll_items(self) -> list:
        """2s-tier items; getters return PARSED RadioState values (never raw)."""
        cat = self._cat
        return [
            ("af_gain", lambda t=None: cat.get_af_gain(timeout=t)),
            ("rf_gain", lambda t=None: cat.get_rf_gain(timeout=t)),
            ("rf_power", lambda t=None: cat.get_rf_power(timeout=t)),
            ("filter_width", lambda t=None: cat.get_filter_width(timeout=t)),
            ("preamp", lambda t=None: cat.get_preamp(timeout=t)),
            ("attenuator", lambda t=None: cat.get_attenuator(timeout=t)),
            ("agc", lambda t=None: cat.get_agc(timeout=t)),
            # Squelch level: write-only until 2026-11-01, so the UI slider was a
            # local echo and the radio's own SQL knob never showed up (field,
            # BG7MWR: "sql 没能同步").  Same defect class as the FT-710's
            # NB/NR levels, fixed there on 2026-10-28.
            ("squelch", lambda t=None: cat.get_squelch(timeout=t)),
            # NB / NR level: the radio's own knob has to reach the UI, and the
            # set path in server.py already reads these back — but the readers
            # did not exist on this controller, so every nb_level/nr_level set
            # raised AttributeError (field, BG7MWR W103D + FT-991A, 2026-11-01).
            # Same pair the FT-710 has had since 2026-10-28.
            ("nb_level", lambda t=None: cat.get_nb_level(timeout=t)),
            ("nr_level", lambda t=None: cat.get_nr_level(timeout=t)),
        ]

    def slow_poll_items(self) -> list:
        cat = self._cat

        async def _freq_mode(timeout=None):
            values: dict = {}
            if (f := await cat.get_frequency("A", timeout=timeout)) is not None:
                values["vfo_a_freq"] = f
            if (m := await cat.get_mode(timeout=timeout)) is not None:
                values["mode"] = m
            return values

        return [("freq_mode", _freq_mode)]

    def tx_meter_items(self) -> list:
        cat = self._cat
        return [
            ("Power", "power_meter", lambda t=None: cat.get_meter("RM5", timeout=t)),
            ("SWR", "swr_meter", lambda t=None: cat.get_meter("RM6", timeout=t)),
            ("ALC", "alc_meter", lambda t=None: cat.get_meter("RM4", timeout=t)),
            ("COMP", "comp_meter", lambda t=None: cat.get_meter("RM3", timeout=t)),
        ]

    def always_meter_items(self) -> list:
        cat = self._cat
        return [("S", "s_meter", lambda t=None: cat.get_s_meter(timeout=t))]

    # ── Connection / command surface (delegating) ───────────────────

    @property
    def cat(self) -> YaesuCatController:
        """Direct access to the wrapped controller.

        `server.py` reads this unconditionally during lifespan startup
        (and the field diagnostic drives it), so it must exist on every
        backend — a missing attribute there aborts application startup
        (found by the task-9 boot smoke test).
        """
        return self._cat

    @property
    def connected(self) -> bool:
        return self._cat.connected

    @property
    def model(self) -> str:
        return self._profile.model_key

    async def connect(self) -> bool:
        if self._scu_client is not None:
            await self._scu_client.start()
            if not await self._scu_client.wait_until_connected(timeout=8.0):
                # Not fatal: the session client keeps rebuilding it, and the
                # CAT layer reports a dead session as transient.  Say which
                # failure it is, though — a wrong credential is one mute unit.
                reason = ("the unit rejected the credentials (it answers "
                          "nothing on a bad login)"
                          if self._scu_client.auth_failed else
                          "no answer yet — check the address and that the "
                          "unit is on the same IPv4 network")
                logger.warning("%s: SCU-LAN10 session not up on %s:%d — %s",
                               self._display_name, self._scu_client.host,
                               self._scu_client.base_port, reason)
        ok = await self._cat.connect()
        if ok:
            await self._check_model_identity()
        return ok

    async def disconnect(self) -> None:
        await self._cat.disconnect()
        if self._scu_client is not None:
            await self._scu_client.stop()

    async def reconnect_loop(self) -> bool:
        if self._scu_client is not None:
            # There is no port to reopen: the session client rebuilds the link
            # itself (full seven phases), so waiting is the whole job here.
            logger.info("%s: waiting for the SCU-LAN10 session to return",
                        self._display_name)
            return await self._scu_client.wait_until_connected(timeout=600.0)
        return await self._cat.reconnect_loop()

    async def boot_verify(self, cat=None, timeout: float = 0.4) -> bool:
        """The Yaesu family answers a frequency read-back (`FA;`)."""
        return bool(await self._cat.query("FA", timeout=timeout))

    # ── Identity (spec §6.2) ────────────────────────────────────────

    async def _check_model_identity(self) -> Optional[str]:
        """Log the `ID;` answer; warn only when the profile expects another.

        Never a release: a mismatch means the profile (or the operator's
        model selection) is wrong, not that the radio must be refused.
        """
        observed = await self._cat.get_model_id()
        p = self._profile
        if observed is None:
            logger.debug("%s: no answer to ID; (optional command)", p.display_name)
            return None
        if not p.id_answer:
            logger.info("%s: model ID observed: %s (no expectation recorded "
                        "for this model yet)", p.display_name, observed)
            return observed
        if observed.upper() == p.id_answer.upper():
            logger.info("%s: model ID %s matches the profile",
                        p.display_name, observed)
        else:
            logger.warning("%s: model ID mismatch — radio answered %s, profile "
                           "expects %s. Check the selected model in the "
                           "connection dialog; control continues.",
                           p.display_name, observed, p.id_answer)
        return observed

    async def initial_state_sync(self) -> dict:
        return await self._cat.initial_state_sync()


def make_backend(model: str) -> type:
    """Return the backend class for a model key (used by tests and the factory)."""
    return _CLASSES[model]


# ── Mechanical delegates for the rest of the abstract surface ───────
#
# Design review finding (2026-09-12): `RadioBackend` is an ABC and
# `__getattr__` does NOT satisfy ABCMeta — the class would have stayed
# abstract and `FTDX10Backend("/dev/null")` would raise TypeError.  The
# delegates are therefore bound onto the class at import time, and the
# abstract set is cleared afterwards because ABCMeta froze it at class
# creation.  `test_every_abstract_method_is_implemented` keeps this honest
# if the ABC grows a method later.
_GATED_METHODS = {"set_ptt", "set_tune"}


def _make_delegate(name: str):
    async def _delegate(self, *args, **kwargs):
        if name in _GATED_METHODS and args and args[0] and not self._tx_allowed():
            return False
        if name in _GATED_METHODS and kwargs.get("tx") and not self._tx_allowed():
            return False
        return await getattr(self._cat, name)(*args, **kwargs)

    _delegate.__name__ = name
    _delegate.__qualname__ = f"YaesuBackend.{name}"
    return _delegate


# Everything the class implements itself (including the gated PTT/TUNE pair).
_EXPLICIT = {
    "capabilities", "create_scope_producer", "bands", "ui_modes",
    "mode_name_to_num", "filter_tables", "state_tables",
    "settings_poll_items", "slow_poll_items", "tx_meter_items",
    "always_meter_items", "connected", "model", "connect", "disconnect",
    "reconnect_loop", "boot_verify", "initial_state_sync",
    "_tx_allowed", "_check_model_identity",
}

for _name in sorted(RadioBackend.__abstractmethods__ - _EXPLICIT):
    setattr(YaesuBackend, _name, _make_delegate(_name))

YaesuBackend.__abstractmethods__ = frozenset()


# ── Model classes (profile + label only, like the Icom family) ──────

class FTDX10Backend(YaesuBackend):
    _profile = get_profile("ftdx10")
    _display_name = "Yaesu FTDX10"


class FTDX101DBackend(YaesuBackend):
    _profile = get_profile("ftdx101d")
    _display_name = "Yaesu FTDX101D"


class FTDX101MPBackend(YaesuBackend):
    _profile = get_profile("ftdx101mp")
    _display_name = "Yaesu FTDX101MP"


class FTX1Backend(YaesuBackend):
    _profile = get_profile("ftx1")
    _display_name = "Yaesu FTX-1F"


class FT891Backend(YaesuBackend):
    _profile = get_profile("ft891")
    _display_name = "Yaesu FT-891"


class FT991ABackend(YaesuBackend):
    _profile = get_profile("ft991a")
    _display_name = "Yaesu FT-991A"


_CLASSES = {
    "ftdx10": FTDX10Backend,
    "ftdx101d": FTDX101DBackend,
    "ftdx101mp": FTDX101MPBackend,
    "ftx1": FTX1Backend,
    "ft891": FT891Backend,
    "ft991a": FT991ABackend,
}
