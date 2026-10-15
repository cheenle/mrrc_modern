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
from ft8.runtime import AP_A_PRIORI_ENV, _ap_a_priori_enabled, _ap_context_for

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

    fields = _ap_context_for(_sequencer())()
    assert fields == {"qso_progress": 0, "my_call": "BA4EG"}
    assert "dx_call" not in fields and "dx_grid" not in fields


def test_a_qso_in_progress_supplies_the_calls_and_the_grid() -> None:
    sequencer = _sequencer(
        state=QSOState.REPORT, dx_call="JA1VAT", dx_grid="QN06"
    )
    assert _ap_context_for(sequencer)() == {
        "qso_progress": 2,
        "my_call": "BA4EG",
        "dx_call": "JA1VAT",
        "dx_grid": "QN06",
    }


def test_a_partner_without_a_grid_is_still_worth_feeding() -> None:
    """The symbol-mask AP needs only the calls; requiring the grid would disable
    the mechanism that produced 10 of the 13 measured gains."""

    fields = _ap_context_for(_sequencer(state=QSOState.REPLYING, dx_call="JA1VAT"))()
    assert fields["dx_call"] == "JA1VAT"
    assert "dx_grid" not in fields


def test_rx_frequency_is_deliberately_left_alone() -> None:
    """a8 activates within ±10 Hz of nfqso = rx_frequency; a guess is worse than
    not arming it.  Unlocking a8 is a separate step (notes §6.1, §7 P5c)."""

    fields = _ap_context_for(
        _sequencer(state=QSOState.REPORT, dx_call="JA1VAT", dx_grid="QN06")
    )()
    assert "rx_frequency" not in fields


def test_the_provider_rereads_the_sequencer_every_call() -> None:
    """dx_call changes mid-QSO; a cached snapshot would feed a stale partner."""

    sequencer = _sequencer()
    provider = _ap_context_for(sequencer)
    assert "dx_call" not in provider()

    sequencer.dx_call = "JA1VAT"
    sequencer.dx_grid = "QN06"
    sequencer.state = QSOState.REPORT
    assert provider()["dx_call"] == "JA1VAT"

    sequencer.dx_call = ""
    sequencer.state = QSOState.DONE
    again = provider()
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
        noop, DecodeConfig(profile=1, threads=4), ap_context=lambda: {}
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
        lambda: {}
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
