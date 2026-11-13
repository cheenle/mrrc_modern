"""
Xiegu CI-V model profiles (Icom-style command set)
==================================================
Xiegu's whole current range speaks **Icom CI-V** over USB serial — the
X6100/X6200 extended manuals document the same framing, BCD frequency
encoding and command numbers that Icom uses, which is also why Hamlib keeps
them in ``rigs/icom/xiegu.c`` and why wfview drives an X6100/IC-705 rig file.

The ``CivModelProfile`` dataclass lives in ``backends/ic7300/civ_profiles.py``
and is reused here unchanged: the directory name there is historical (it hosts
the shared CI-V core, see its docstring), the fields are model facts, not Icom
facts.  Everything Xiegu-specific is *data* in this module, so a new Xiegu
model is one profile entry plus a backend subclass — the same shape as the
IC-705/IC-7610/IC-7760 round.

Provenance (spec ``docs/superpowers/specs/2026-11-07-xiegu-g7250-design.md``)
---------------------------------------------------------------------------
Xiegu has **not published a G7250 CI-V document** and no upstream driver
exists (checked 2026-11-07: wfview ``master`` ``rigs/`` has no Xiegu file,
Hamlib ``master`` ``rigs/icom/xiegu.c`` stops at the X6200, GitHub has exactly
one G7250 project).  Every value below is therefore one of:

``[RE]``  third-party reverse engineering with hardware evidence, published in
          ``github.com/nathan86daniels-commits/xiegu-g7250-open-control``
          (LAN protocol tagged ``[HW]`` = seen on a real radio there; CI-V side
          follows the X6100/X6200 Icom subset, commands the radio refuses are
          recorded as ``FA`` NAK).
``[X6]``  documented for the X6100/X6200 (Radioddity extended manuals) and
          assumed to carry over.
``[TODO]`` inherited from the IC-7300 tables — wrong until a real G7250 says
          otherwise; such fields are also listed in ``unverified_meters`` /
          reported through ``_diag_xiegu.py`` on the first hardware session.

This model is ``verified=False``: the server refuses PTT/TUNE until the
operator sets ``MRRC_ALLOW_UNVERIFIED_TX=1`` (AD-019 / NFR-067), exactly like
the three unverified Icom models.
"""
from __future__ import annotations

from backends.ic7300.civ_profiles import CivModelProfile, MeterCal
from backends.ic7300.config_ic7300 import (
    BANDS as ICOM_BANDS,
    FIL_DEFAULT_WIDTHS_HZ,
    FIL_WIDTH_RANGE_HZ,
    PREAMP_LABELS,
)

# ── CI-V addressing ────────────────────────────────────────────────
# [RE] The G7250's address is not documented.  The community CAT link probes
# these three in order: 0xA4 is the X6200/X6100 address (and IC-705's, which is
# why wfview's 705 rig file works on those radios), 0x88 the stock G90/G106,
# 0x70 a legacy G90 variant.  The backend resolves the live address at connect
# time and logs the one that answered; ``civ_addr`` below is only the first
# candidate so a fixed-address override stays possible.
# 0xA4 is the address the USB port answers on hardware (field, BG7YQV
# 2026-11-12: ``Connected to IC-7300 (CI-V 0xA4) on COM3``).  0xA0 is what the
# radio reports over its **network** side — the vendor rig file in
# ``docs/wfview-g7250.rig`` says ``CIVAddress=160`` and wfview logs
# ``Received radio capabilities … CIV: a0`` — so it is tried too, after the
# addresses with field evidence.
CANDIDATE_CIV_ADDRESSES: tuple[int, ...] = (0xA4, 0x88, 0x70, 0xA0)

DEFAULT_CIV_ADDRESS = CANDIDATE_CIV_ADDRESSES[0]

# X6100/X6200 ship 19200 for CAT, but the G7250's USB port runs at 115200
# [RE] (boot/console traffic on the same bridge at 115200, and the community
# project's default CAT rate is 115200).  config.DEFAULT_BAUD_RATE carries the
# same value for MRRC_BAUD_RATE.
DEFAULT_BAUD_RATE = 115200

# ── The network link: Icom's protocol, not raw IQ ─────────────────
# A third-party reverse-engineering note described a raw IQ stream (14-byte
# tune frame to 50002, 768-byte IQ payloads from 50001).  **This firmware does
# not do that** — measured 2026-11-12 on a live unit: sixteen 14-byte frame
# variants to 50001/50002, ten-second listens on 50000/50003/50004 and
# CH9121-style probes all drew zero responses, while wfview on the same machine
# worked.  Its log shows the truth: the radio speaks **Icom's network-radio
# protocol** (RS-BA1) — 50001 control (``I am here`` → login → token →
# capabilities, announcing itself as an *IC-7300*), 50002 CI-V tunnel, 50003
# audio at 16 kHz.  See ``docs/g7250-wfview-analysis.md``.
#
# The constants below are kept because the port numbers are real; the tune
# frame layout is only of historical interest.
LAN_DEFAULT_IP = "192.168.3.199"
LAN_IQ_PORT = 50001
LAN_CONTROL_PORT = 50002
LAN_IQ_RATE_HZ = 48_000
LAN_IQ_HALF_SPAN_HZ = 24_000
LAN_TUNE_HEADER = b"\xAA\x55\x55\xAA\x00\x01"
LAN_TUNE_MIN_HZ = 100_000
LAN_TUNE_MAX_HZ = 54_000_000

# Modes the G7250 documents (SSB, CW, NFM, AM, DIG).  Register numbers are the
# Icom CI-V ones [X6]; RTTY (0x04/0x08) is deliberately absent — the radio does
# not list RTTY, and an unsupported mode register comes back as FA anyway.
_G7250_MODES = {
    0x00: "LSB",
    0x01: "USB",
    0x02: "AM",
    0x03: "CW-U",
    0x05: "FM",
    0x07: "CW-L",
}

# HF + 50 MHz only (0.5-30 MHz / 50-54 MHz); the G7250 has no 4 m/70 cm, so the
# IC-7300 band table minus its non-HF entries is exactly right.
_G7250_BANDS = tuple(dict(b) for b in ICOM_BANDS if b["name"] in {
    "160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m",
})

_SOURCE = (
    "https://github.com/nathan86daniels-commits/xiegu-g7250-open-control "
    "(README 'Protocol', g7250/civ.py, g7250/cat.py) — no vendor CI-V document "
    "for the G7250 exists yet; CI-V surface assumed from the X6100/X6200 "
    "Icom subset; meter curves inherited from the verified IC-7300 tables"
)

PROFILES: dict[str, CivModelProfile] = {
    "g7250": CivModelProfile(
        model_key="g7250",
        display_name="Xiegu G7250",
        source=_SOURCE,
        civ_addr=DEFAULT_CIV_ADDRESS,
        # 1A 05 <item>: not documented for the G7250 and the X6100/X6200 did not
        # publish one either.  Empty means "send nothing" — CivController skips
        # the enable step, and the backend polls instead of relying on
        # transceive broadcasts.  TODO(hw-verify): record the working item.
        transceive_item=b"",
        bands=_G7250_BANDS,
        mode_num_to_name=dict(_G7250_MODES),
        mode_name_to_num={v: k for k, v in _G7250_MODES.items()},
        # Icom FIL1/FIL2/FIL3 semantics [X6]; widths are TODO(hw-verify).
        fil_default_widths_hz=dict(FIL_DEFAULT_WIDTHS_HZ),
        fil_width_range_hz=dict(FIL_WIDTH_RANGE_HZ),
        preamp_labels=dict(PREAMP_LABELS),
        # 0x11 00/20 is the Icom on/off attenuator form [X6].  TODO(hw-verify):
        # the G7250 may expose no attenuator at all — a FA answer leaves the
        # control inert (CivNakError) rather than mis-setting it.
        att_steps=(0, 20),
        # The G7250 is a 100 W HF radio like the IC-7300, so the verified
        # 100 W curves are the closest offline starting point.  TODO(hw-verify).
        meter_cal=MeterCal(rated_power_w=100.0),
        # Scope geometry from the vendor rig file
        # (docs/wfview-g7250.rig): SpectrumLenMax=475, SpectrumAmpMax=160.
        # The division count is **1, not the rig file's 11**: measured through
        # the LAN tunnel (2026-11-13, 1.28.58 field probe) the radio ships the
        # whole 475-bin sweep in a single 497-byte `27 00` frame with
        # seq_max=0x01, about 8.5 frames/s.  Keeping the rig file's 11 only
        # produced one "segment count 1 != profile expectation 11" warning per
        # session; ScopeAssembler already treats seq_max==1 as a whole sweep.
        scope_bins=475,
        scope_seq_max=1,
        scope_amp_max=160,
        audio_rx_rate=48_000,
        audio_tx_rate=48_000,
        # Built-in USB sound card [X6].  The exact ALSA/CoreAudio/Windows name
        # is not published; these are the three strings the Xiegu and generic
        # USB-audio stacks have used.  TODO(hw-verify) — a wrong hint only means
        # the operator picks the device by hand.
        audio_name_hints=("xiegu", "usb audio device", "usb audio codec"),
        has_atu=True,                 # 20-150 ohm internal ATU
        filter_model="fil123",
        tune_via="atu",
        dual_rx=False,
        mainsub_vfo=False,
        verified=False,
        # Everything a meter shows comes from the inherited IC-7300 curve.  The
        # *presence* of the meters is no longer a guess: the vendor rig file
        # lists 15 02 / 15 11 / 15 12 / 15 13 / 15 15 / 15 16, i.e. S-meter,
        # power, SWR, ALC, Vd and Id.
        unverified_meters=("s_meter", "power", "swr", "alc",
                           "voltage", "current"),
        # No expectation recorded: a fabricated model-ID would raise false
        # mismatch warnings (spec §6.2).  `_diag_xiegu.py` records the real
        # 19 00 bytes on the first hardware session.
        model_id_bytes=None,
    ),
}


def get_profile(model_key: str) -> CivModelProfile:
    """Return the profile for ``model_key`` (KeyError on unknown models)."""
    return PROFILES[model_key]
