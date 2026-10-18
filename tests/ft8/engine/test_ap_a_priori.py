"""P5a: feeding the decoder the QSO a-priori it was always entitled to.

``DecodeConfig.ap`` has defaulted to ``True`` since the field existed, so AP was
never "off" -- it was starved.  ``runtime.py`` built ``DecodeConfig(profile=…,
threads=…)`` and nothing else, which left ``my_call``/``dx_call``/``dx_grid``
empty and made the native arming condition

    ltry_a8 = ap_enabled .and. len_trim(hiscall)>=3 .and. len_trim(hisgrid4)>=4

permanently false (``ft8/dsp/wsjt_improved.f90:178``).
``docs/ft8-dl-speculative-notes.md`` §4.2 measures what feeding it buys: 13/100
extra decodes at the recall cliff, 0 lost out of 100 paired trials, and 0
invented messages across 316 trials (§4.3).

These tests guard the three properties that make it safe to turn on:

* **Off by default, and off means byte-identical.**  Every shipped box and every
  offline install keeps today's frame exactly.
* **The provider is consulted per slot, never cached.**  ``dx_call`` changes
  mid-QSO; a value resolved at startup would go stale, and a stale a-priori is
  the case §4.3 measures.
* **Nothing is invented.**  Fields are filled only when known, and
  ``rx_frequency`` is deliberately left at its default -- a8 activates within
  ±10 Hz of it, so a wrong frequency is worse than not arming a8 at all.
"""

from __future__ import annotations

import asyncio

import numpy as np

from ft8.core.models import DecodeConfig, DecodePath
from ft8.core.protocol import encode_frame
from ft8.engine.dsp_decode import SupervisorDecoder, decode_config_frame
from ft8.engine.sequencer import QSOState, Sequencer
from ft8.platform.cloud_decode import CloudDecoder
from ft8.runtime import (
    AP_A_PRIORI_ENV,
    Ft8Runtime,
    AP_DX_FREQUENCY_FRESH_SLOTS,
    DxFrequency,
    _ap_a_priori_enabled,
    _ap_context_for,
    _qso_progress,
)

SAMPLES = np.arange(180_000, dtype=np.int16).tobytes()


class _FakeSupervisor:
    """Captures the request frame and answers with a minimal decode_ok."""

    def __init__(self) -> None:
        self.frames: list[dict[str, object]] = []

    def request(self, frame: dict[str, object], timeout: float) -> dict[str, object]:
        encode_frame({**frame, "v": 1, "generation": 1, "request_id": 1})
        self.frames.append(frame)
        return {
            "v": 1,
            "type": "decode_ok",
            "generation": 1,
            "request_id": 2,
            "slot_id": frame["slot_id"],
            "path": "improved",
            "results": [],
            "overflow": False,
            "elapsed_seconds": 0.1,
            "deadline_missed": False,
        }


def _decode(decoder: SupervisorDecoder, slot_id: int) -> None:
    asyncio.run(decoder.decode(slot_id, SAMPLES))


def _config_of(supervisor: _FakeSupervisor, index: int = -1) -> dict[str, object]:
    frame = supervisor.frames[index]
    config = frame["config"]
    assert isinstance(config, dict)
    return config


def _sequencer(**overrides: object) -> Sequencer:
    return Sequencer(my_call="BA4EG", my_grid="PM00", **overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_the_a_priori_is_off_unless_asked_for() -> None:
    """Every shipped box and every offline install stays exactly as it is today."""

    assert _ap_a_priori_enabled({}) is False
    assert _ap_a_priori_enabled({AP_A_PRIORI_ENV: ""}) is False
    assert _ap_a_priori_enabled({AP_A_PRIORI_ENV: "0"}) is False
    assert _ap_a_priori_enabled({AP_A_PRIORI_ENV: "false"}) is False
    assert _ap_a_priori_enabled({AP_A_PRIORI_ENV: "  "}) is False


def test_the_a_priori_turns_on_when_asked() -> None:
    assert _ap_a_priori_enabled({AP_A_PRIORI_ENV: "1"}) is True
    assert _ap_a_priori_enabled({AP_A_PRIORI_ENV: "yes"}) is True


# --------------------------------------------------------------------------
# What the provider produces
# --------------------------------------------------------------------------


def test_an_idle_station_supplies_only_its_own_call() -> None:
    """No partner ⇒ no dx_call/dx_grid, and qso_progress 0 is already the default.

    ``my_call`` is still sent: it is the station's own call, known at every
    moment, and the native arming conditions for the partner-dependent paths
    (``ltry_a8``, ``lenabledxcsearch``) both require ``hiscall`` -- so an idle
    station cannot arm them no matter what we send.
    """

    fields = _ap_context_for(_sequencer())(480)
    assert fields == {"qso_progress": 0, "my_call": "BA4EG"}
    assert "dx_call" not in fields and "dx_grid" not in fields


def test_a_qso_in_progress_supplies_the_calls_and_the_grid() -> None:
    sequencer = _sequencer(
        state=QSOState.REPORT, dx_call="JA1VAT", dx_grid="QN06"
    )
    assert _ap_context_for(sequencer)(480) == {
        "qso_progress": 2,
        "my_call": "BA4EG",
        "dx_call": "JA1VAT",
        "dx_grid": "QN06",
    }


def test_a_partner_without_a_grid_is_still_worth_feeding() -> None:
    """The symbol-mask AP needs only the calls; requiring the grid would disable
    the mechanism that produced 10 of the 13 measured gains."""

    fields = _ap_context_for(_sequencer(state=QSOState.REPLYING, dx_call="JA1VAT"))(480)
    assert fields["dx_call"] == "JA1VAT"
    assert "dx_grid" not in fields


def test_rx_frequency_is_absent_without_a_tracker() -> None:
    """a8 activates within ±10 Hz of nfqso = rx_frequency; a guess is worse than
    not arming it.  With no sightings recorded, the field stays at its default."""

    fields = _ap_context_for(
        _sequencer(state=QSOState.REPORT, dx_call="JA1VAT", dx_grid="QN06")
    )(480)
    assert "rx_frequency" not in fields


def test_rx_frequency_is_absent_without_a_grid_tracker_too() -> None:
    """An empty tracker behaves like no tracker: nothing to aim at."""

    fields = _ap_context_for(
        _sequencer(state=QSOState.REPORT, dx_call="JA1VAT", dx_grid="QN06"),
        DxFrequency(),
    )(480)
    assert "rx_frequency" not in fields


# --------------------------------------------------------------------------
# P5c: pointing a8 at where the partner actually was
# --------------------------------------------------------------------------


def _tracker(call: str = "JA1VAT", frequency: float = 1234.4, slot_id: int = 480) -> DxFrequency:
    tracker = DxFrequency()
    tracker.note(slot_id, call, frequency)
    return tracker


def test_a_fresh_sighting_supplies_rx_frequency() -> None:
    fields = _ap_context_for(
        _sequencer(state=QSOState.REPORT, dx_call="JA1VAT", dx_grid="QN06"),
        _tracker(slot_id=478),
    )(480)
    assert fields["rx_frequency"] == 1234


def test_rx_frequency_is_rounded_to_the_integer_the_protocol_wants() -> None:
    fields = _ap_context_for(
        _sequencer(state=QSOState.REPORT, dx_call="JA1VAT"),
        _tracker(frequency=1234.6),
    )(480)
    assert fields["rx_frequency"] == 1235


def test_a_stale_sighting_is_not_sent() -> None:
    """A stale nfqso sends a8 to an empty part of the band to try 128 hypotheses."""

    stale = 480 + AP_DX_FREQUENCY_FRESH_SLOTS + 1
    fields = _ap_context_for(
        _sequencer(state=QSOState.REPORT, dx_call="JA1VAT"), _tracker(slot_id=480)
    )(stale)
    assert "rx_frequency" not in fields


def test_the_freshness_window_includes_its_own_edge() -> None:
    edge = 480 + AP_DX_FREQUENCY_FRESH_SLOTS
    fields = _ap_context_for(
        _sequencer(state=QSOState.REPORT, dx_call="JA1VAT"), _tracker(slot_id=480)
    )(edge)
    assert fields["rx_frequency"] == 1234


def test_the_window_covers_several_missed_decode_cycles() -> None:
    """Partners transmit on alternate slots, so one missed decode must not disarm a8."""

    assert AP_DX_FREQUENCY_FRESH_SLOTS >= 4


def test_a_previous_partners_frequency_is_not_reused() -> None:
    """After the operator switches partners, aiming at the old frequency is worse
    than not arming a8 at all."""

    fields = _ap_context_for(
        _sequencer(state=QSOState.REPORT, dx_call="VK3ABC", dx_grid="QF22"),
        _tracker(call="JA1VAT"),
    )(480)
    assert "rx_frequency" not in fields


def test_midnight_wrap_does_not_look_stale() -> None:
    tracker = _tracker(slot_id=5759)
    fields = _ap_context_for(
        _sequencer(state=QSOState.REPORT, dx_call="JA1VAT"), tracker
    )(2)
    assert fields["rx_frequency"] == 1234, "slot ids wrap at 5760 per day"


def test_the_newest_sighting_wins() -> None:
    tracker = DxFrequency()
    tracker.note(470, "JA1VAT", 900.0)
    tracker.note(479, "JA1VAT", 1500.0)
    fields = _ap_context_for(
        _sequencer(state=QSOState.REPORT, dx_call="JA1VAT"), tracker
    )(480)
    assert fields["rx_frequency"] == 1500


def test_an_empty_call_is_never_recorded() -> None:
    tracker = DxFrequency()
    tracker.note(480, "", 1500.0)
    assert tracker.fresh_for("JA1VAT", 481) is None
    assert tracker.fresh_for("", 481) is None


def test_the_provider_rereads_the_sequencer_every_call() -> None:
    """dx_call changes mid-QSO; a cached snapshot would feed a stale partner."""

    sequencer = _sequencer()
    provider = _ap_context_for(sequencer)
    assert "dx_call" not in provider(480)

    sequencer.dx_call = "JA1VAT"
    sequencer.dx_grid = "QN06"
    sequencer.state = QSOState.REPORT
    assert provider(480)["dx_call"] == "JA1VAT"

    sequencer.dx_call = ""
    sequencer.state = QSOState.DONE
    again = provider(480)
    assert "dx_call" not in again
    assert again["qso_progress"] == 0
    assert again["my_call"] == "BA4EG", "our own call never goes stale"


# --------------------------------------------------------------------------
# What reaches the wire
# --------------------------------------------------------------------------


def test_without_a_provider_the_frame_is_byte_identical_to_today() -> None:
    """The reversibility guarantee: ap_context=None must not touch the frame."""

    plain = _FakeSupervisor()
    with SupervisorDecoder(plain, DecodeConfig(profile=1, threads=4)) as decoder:
        _decode(decoder, 480)

    noop = _FakeSupervisor()
    with SupervisorDecoder(
        noop, DecodeConfig(profile=1, threads=4), ap_context=lambda _slot_id: {}
    ) as decoder:
        _decode(decoder, 480)

    assert _config_of(plain) == _config_of(noop)
    # And both are what the config has always said: AP enabled, nothing fed.
    config = decode_config_frame(DecodeConfig(profile=1, threads=4))
    for key, value in _config_of(plain).items():
        if key not in ("utc_hhmmss", "path"):
            assert value == config[key], key


def test_with_a_provider_the_a_priori_reaches_the_native_frame() -> None:
    supervisor = _FakeSupervisor()
    provider = _ap_context_for(
        _sequencer(state=QSOState.REPORT, dx_call="JA1VAT", dx_grid="QN06")
    )
    with SupervisorDecoder(
        supervisor, DecodeConfig(profile=1, threads=4), ap_context=provider
    ) as decoder:
        _decode(decoder, 480)

    config = _config_of(supervisor)
    assert config["my_call"] == "BA4EG"
    assert config["dx_call"] == "JA1VAT"
    assert config["dx_grid"] == "QN06"
    assert config["qso_progress"] == 2
    assert config["ap"] is True, "AP was already on by default; the fix feeds it"
    # The documented omission, asserted on the wire rather than in prose.
    assert config["rx_frequency"] == DecodeConfig().rx_frequency


def test_the_provider_is_consulted_per_slot_not_once_at_startup() -> None:
    supervisor = _FakeSupervisor()
    sequencer = _sequencer()
    with SupervisorDecoder(
        supervisor, DecodeConfig(profile=1, threads=4), ap_context=_ap_context_for(sequencer)
    ) as decoder:
        _decode(decoder, 480)
        assert _config_of(supervisor, 0)["qso_progress"] == 0

        sequencer.dx_call = "JA1VAT"
        sequencer.dx_grid = "QN06"
        sequencer.state = QSOState.REPORT
        _decode(decoder, 481)

    assert _config_of(supervisor, 1)["dx_call"] == "JA1VAT"
    assert _config_of(supervisor, 1)["qso_progress"] == 2


def test_the_path_override_still_wins_over_the_provider() -> None:
    """The cascade chooses fast/deep per slot; the a-priori must not disturb that."""

    supervisor = _FakeSupervisor()
    with SupervisorDecoder(
        supervisor,
        DecodeConfig(profile=1, threads=4),
        ap_context=_ap_context_for(_sequencer(state=QSOState.REPORT, dx_call="JA1VAT")),
    ) as decoder:
        asyncio.run(decoder.decode_path(482, SAMPLES, DecodePath.FAST))

    config = _config_of(supervisor)
    assert config["path"] == DecodePath.FAST.value
    assert config["dx_call"] == "JA1VAT"


# --------------------------------------------------------------------------
# The cloud path
# --------------------------------------------------------------------------


def _cloud(ap_context=None) -> CloudDecoder:
    return CloudDecoder(
        "https://host.example:8990/mrrc-decode",
        "token",
        ap_context=ap_context,
    )


def test_the_cloud_frame_is_unchanged_without_a_provider() -> None:
    assert _cloud()._request_frame(480, 360_000)["config"] == _cloud(
        lambda _slot_id: {}
    )._request_frame(480, 360_000)["config"]


def test_the_host_gets_the_same_a_priori() -> None:
    """The host runs the same native decoder, and the config already travels in
    full with every request -- so the a-priori costs nothing extra on the wire."""

    provider = _ap_context_for(
        _sequencer(state=QSOState.REPORT, dx_call="JA1VAT", dx_grid="QN06")
    )
    frame = _cloud(provider)._request_frame(480, 360_000)
    config = frame["config"]
    assert isinstance(config, dict)
    assert config["dx_call"] == "JA1VAT"
    assert config["dx_grid"] == "QN06"
    assert config["my_call"] == "BA4EG"
    assert config["qso_progress"] == 2
    # The host's own knobs still win.
    assert config["path"] == DecodePath.IMPROVED.value


# --------------------------------------------------------------------------
# qso_progress → WSJT-X nQSOProgress rows
# --------------------------------------------------------------------------


def test_every_phase_maps_to_its_own_tx_row() -> None:
    """ft8bvar selects its AP masks by row, and the row comments name the Tx stage.

    Collapsing every late phase onto row 2 (what this function used to do) meant
    rows 3 and 4 -- the only ones carrying iaptype 6/5/4, i.e. RR73 / 73 / RRR --
    were never selected, so the decoder never tried the hypotheses most likely at
    the end of a QSO.
    """

    assert _qso_progress(QSOState.REPLYING) == 1       # Tx1: DX MY GRID
    assert _qso_progress(QSOState.REPORT) == 2         # Tx2: DX MY -nn
    assert _qso_progress(QSOState.ROGER_REPORT) == 3   # Tx3: DX MY R-nn
    assert _qso_progress(QSOState.ROGERS) == 4         # Tx4: DX MY RR73
    assert _qso_progress(QSOState.SIGNOFF) == 5        # Tx5: DX MY 73


def test_no_qso_in_progress_maps_to_the_cq_row() -> None:
    assert _qso_progress(QSOState.IDLE) == 0
    assert _qso_progress(QSOState.CALLING) == 0
    assert _qso_progress(QSOState.DONE) == 0


def test_every_state_has_a_row_and_no_row_is_out_of_range() -> None:
    """The Fortran tables are dimensioned (0:5, 1:27); a 6 would read out of bounds."""

    for state in QSOState:
        row = _qso_progress(state)
        assert 0 <= row <= 5, (state, row)


def test_the_cascade_escalation_test_is_unchanged() -> None:
    """Blast radius: cascade.py:58 escalates on ``my_call and qso_progress > 0``.

    A faithful row mapping must keep that predicate exactly as it was -- in a QSO
    it escalates, with no QSO it does not.  This is the reason IDLE/CALLING/DONE
    all map to 0 rather than to some "unknown" row.
    """

    in_qso = [s for s in QSOState if s not in (QSOState.IDLE, QSOState.CALLING, QSOState.DONE)]
    assert in_qso, "the enum changed; re-read this test"
    for state in in_qso:
        assert _qso_progress(state) > 0, state
    for state in (QSOState.IDLE, QSOState.CALLING, QSOState.DONE):
        assert _qso_progress(state) == 0, state


def test_the_provider_carries_the_row_of_the_current_phase() -> None:
    """End to end: the sequencer's phase, not a constant, reaches the frame."""

    sequencer = _sequencer(state=QSOState.ROGERS, dx_call="JA1VAT", dx_grid="QN06")
    assert _ap_context_for(sequencer)(480)["qso_progress"] == 4
    sequencer.state = QSOState.REPLYING
    assert _ap_context_for(sequencer)(480)["qso_progress"] == 1


# --------------------------------------------------------------------------
# P5c wiring: decoded messages → the partner's frequency → rx_frequency
# --------------------------------------------------------------------------


class _Parsed:
    def __init__(self, from_call: str) -> None:
        self.from_call = from_call


class _Result:
    def __init__(self, frequency: float) -> None:
        self.frequency = frequency


class _Message:
    def __init__(self, from_call: str, frequency: float) -> None:
        self.parsed = _Parsed(from_call)
        self.result = _Result(frequency)


class _SlotDecode:
    def __init__(self, slot_id: int, *messages: _Message, late: bool = False) -> None:
        self.slot_id = slot_id
        self.messages = list(messages)
        self.late = late


class _RuntimeStub:
    """Borrows the real method; nothing here needs a supervisor or a rig."""

    _note_partner_frequency = Ft8Runtime._note_partner_frequency

    def __init__(self, dx_call: str = "JA1VAT", enabled: bool = True) -> None:
        self.sequencer = _sequencer(state=QSOState.REPORT, dx_call=dx_call, dx_grid="QN06")
        self._dx_frequency = DxFrequency() if enabled else None


def test_the_partners_decode_is_recorded() -> None:
    stub = _RuntimeStub()
    stub._note_partner_frequency(
        _SlotDecode(480, _Message("VK3ABC", 900.0), _Message("JA1VAT", 1234.4))
    )
    assert stub._dx_frequency.fresh_for("JA1VAT", 481) == 1234.4


def test_another_stations_decode_is_not_recorded() -> None:
    stub = _RuntimeStub()
    stub._note_partner_frequency(_SlotDecode(480, _Message("VK3ABC", 900.0)))
    assert stub._dx_frequency.fresh_for("JA1VAT", 481) is None


def test_a_late_batch_is_never_used() -> None:
    """NFR-001/NFR-009: a late decode is display-only and never fed to the
    sequencer -- the same rule keeps it out of the a-priori."""

    stub = _RuntimeStub()
    stub._note_partner_frequency(
        _SlotDecode(480, _Message("JA1VAT", 1234.4), late=True)
    )
    assert stub._dx_frequency.fresh_for("JA1VAT", 481) is None


def test_the_provisional_batch_does_feed_it() -> None:
    """The partner's message often lands in the fast answer first, and a fresher
    sighting is the whole point -- so provisional is not filtered out."""

    stub = _RuntimeStub()
    batch = _SlotDecode(480, _Message("JA1VAT", 1234.4))
    batch.provisional = True  # type: ignore[attr-defined]
    stub._note_partner_frequency(batch)
    assert stub._dx_frequency.fresh_for("JA1VAT", 481) == 1234.4


def test_with_no_partner_there_is_nothing_to_track() -> None:
    stub = _RuntimeStub(dx_call="")
    stub._note_partner_frequency(_SlotDecode(480, _Message("JA1VAT", 1234.4)))
    assert stub._dx_frequency.fresh_for("JA1VAT", 481) is None


def test_the_gate_off_means_no_tracker_and_no_crash() -> None:
    stub = _RuntimeStub(enabled=False)
    assert stub._dx_frequency is None
    stub._note_partner_frequency(_SlotDecode(480, _Message("JA1VAT", 1234.4)))


def test_end_to_end_a_sighting_becomes_rx_frequency() -> None:
    """The whole chain: decoded message → tracker → provider → the frame."""

    stub = _RuntimeStub()
    stub._note_partner_frequency(_SlotDecode(480, _Message("JA1VAT", 1234.4)))
    fields = _ap_context_for(stub.sequencer, stub._dx_frequency)(481)
    assert fields["rx_frequency"] == 1234
    assert fields["dx_call"] == "JA1VAT"


def test_tx_frequency_is_still_left_alone() -> None:
    """nftx means *our* transmit offset; the probe set it to the signal's
    frequency, so filling it from sequencer.tx_frequency would be a different,
    unmeasured configuration."""

    stub = _RuntimeStub()
    stub._note_partner_frequency(_SlotDecode(480, _Message("JA1VAT", 1234.4)))
    fields = _ap_context_for(stub.sequencer, stub._dx_frequency)(481)
    assert "tx_frequency" not in fields


def test_it_survives_a_runtime_whose_init_never_ran() -> None:
    """The same contract as the cloud-decode trace: optional, never fatal.

    ``tests/ft8/test_server_wiring.py`` builds the runtime with ``object.__new__``
    (its docstring: constructing the whole runtime to pin one field is
    disproportionate), so ``_dx_frequency`` may simply not exist.  This runs on
    every batch of a live decode path, so a missing attribute must not be able to
    take decoding down.
    """

    stub = object.__new__(Ft8Runtime)
    stub.sequencer = _sequencer(state=QSOState.REPORT, dx_call="JA1VAT")
    stub._note_partner_frequency(_SlotDecode(480, _Message("JA1VAT", 1234.4)))  # must not raise


def test_it_survives_a_sequencer_without_partner_fields() -> None:
    stub = object.__new__(Ft8Runtime)
    stub._dx_frequency = DxFrequency()
    stub.sequencer = type("S", (), {"my_call": "BA4EG"})()
    stub._note_partner_frequency(_SlotDecode(480, _Message("JA1VAT", 1234.4)))  # must not raise


def test_an_unusable_frequency_is_skipped_not_fatal() -> None:
    stub = _RuntimeStub()
    stub._note_partner_frequency(_SlotDecode(480, _Message("JA1VAT", float("nan"))))
    # NaN is a float, so it records; the guard is against non-numeric junk.
    stub._note_partner_frequency(_SlotDecode(481, _Message("JA1VAT", None)))
    stub._note_partner_frequency(_SlotDecode(482, _Message("JA1VAT", "1234")))
    assert stub._dx_frequency.slot_id == 482


def test_a_message_without_a_result_does_not_raise() -> None:
    stub = _RuntimeStub()
    stub._note_partner_frequency(
        _SlotDecode(480, type("M", (), {"parsed": _Parsed("JA1VAT")})())
    )
    assert stub._dx_frequency.fresh_for("JA1VAT", 481) is None


def test_the_shipped_config_matches_the_measured_one() -> None:
    """What P5a+P5c put on the wire must be the configuration §4.2/§4.4 measured.

    The gain numbers (20/120, a8 = 11 of them, zero losses, zero inventions) were
    produced by ``ap_probe``'s ``ap_fed`` arm.  If the runtime drifts away from
    that arm, the evidence stops applying to the shipped code -- so the two are
    compared field by field here, and the one deliberate difference
    (``tx_frequency``) is named rather than left implicit.
    """

    from tools.ft8 import ap_probe

    sequencer = _sequencer(state=QSOState.REPORT, dx_call="JA1VAT", dx_grid="QN06")
    tracker = DxFrequency()
    tracker.note(479, "JA1VAT", 1234.4)
    # Same base the probe uses: the production deep config (profile 1, cycles 3,
    # sensitivity 3, low_threshold), not a bare DecodeConfig -- otherwise the
    # comparison would "pass" against a configuration nobody ships.
    base = {k: v for k, v in ap_probe.BASE.items() if k != "path"}
    shipped = DecodeConfig(**base).replace(  # type: ignore[arg-type]
        utc_hhmmss=20_000, **_ap_context_for(sequencer, tracker)(480)
    )

    measured = ap_probe._config(
        ap=True,
        my_call=ap_probe.MY_CALL,
        dx_call=ap_probe.DX_CALL,
        dx_grid=ap_probe.DX_GRID,
        qso_progress=2,
        rx_frequency=1234,
        tx_frequency=1234,
    )

    differences = {
        field: (getattr(shipped, field), getattr(measured, field))
        for field in shipped.__dataclass_fields__
        if getattr(shipped, field) != getattr(measured, field)
    }
    assert set(differences) <= {"tx_frequency", "utc_hhmmss"}, differences
    assert "tx_frequency" in differences, (
        "the probe set tx_frequency to the signal frequency; production must not "
        "(nftx means *our* transmit offset) -- if this ever matches, the "
        "deliberate omission in _ap_context_for has been undone"
    )
    # The four fields that arm the native AP paths must agree exactly.
    for field in ("ap", "my_call", "dx_call", "dx_grid", "qso_progress", "rx_frequency"):
        assert getattr(shipped, field) == getattr(measured, field), field
