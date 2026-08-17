"""
Shared FT-710 scope frame parsing and pipe payload encoding.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import struct


SCOPE_FRAME_SIZE = 4096
WF_SIZE = 850
SYNC_TAIL = b"\xff\x01\xee\x01"
SYNC_FULL = SYNC_TAIL * 4
DATA_OFFSET = 2900
PIPE_PAYLOAD_VERSION = 2


@dataclass
class ScopeFrame:
    wf1: list[int]
    wf2: list[int]
    scope_mode: int = 0
    scope_span: int = 0
    preamp: int = 0
    attenuator: int = 0
    mode: int = 0
    s_meter: int = 0
    vfoa_freq: int = 0           # offset 64, 5-byte BCD (matches wfview yaesucommander.cpp:192)
    vfoa_freq_bin: int = 0       # offset 132, 4-byte BE binary (wfview comment: "VFOA Freq in Hex (BE)")
    scope_start_freq: int = 0


def scope_mode_to_cat(modein: int) -> int:
    mapping = {
        0x0: 0x01, 0x1: 0x02, 0x2: 0x07, 0x3: 0x03,
        0x4: 0x05, 0x5: 0x0D, 0x6: 0x04, 0x7: 0x0B,
        0x8: 0x08, 0x9: 0x0C, 0xA: 0x0A, 0xB: 0x0F,
        0xC: 0x06, 0xD: 0x09, 0xE: 0x0E,
    }
    return mapping.get(modein, modein)


def parse_scope_frame(frame: bytes, require_sync: bool = True) -> ScopeFrame:
    """Parse a raw 4096-byte FT-710 scope frame.

    Frame layout (from wfview ft4222handler.cpp):
      bytes 0-849:     wf1 spectrum (850 bytes, inverted)
      bytes 850-1699:  wf2 spectrum (850 bytes, inverted)
      bytes 1700-2899: reserved / additional data
      bytes 2900-3049: metadata block (150 bytes)
      bytes 3050-4091: padding
      bytes 4092-4095: sync tail (0xFF 0x01 0xEE 0x01)

    Sync validation can be skipped with require_sync=False for
    testing or diagnostic purposes.
    """
    if len(frame) < SCOPE_FRAME_SIZE:
        raise ValueError(f"scope frame too short: {len(frame)}")
    frame = frame[:SCOPE_FRAME_SIZE]

    # Validate sync tail unless explicitly skipped
    if require_sync:
        if not frame.endswith(SYNC_TAIL):
            raise ValueError("scope frame missing sync tail")
    elif frame.endswith(SYNC_TAIL):
        pass  # sync is valid even if not required

    # Decode spectrum data — FT-710 outputs inverted bytes
    wf1 = [~b & 0xFF for b in frame[0:WF_SIZE]]
    wf2 = [~b & 0xFF for b in frame[WF_SIZE:WF_SIZE * 2]]
    parsed = ScopeFrame(wf1=wf1, wf2=wf2)

    # Parse metadata block at offset DATA_OFFSET
    data = frame[DATA_OFFSET:DATA_OFFSET + 150]
    if len(data) >= 145:
        parsed.scope_mode = data[17]
        pa_att = data[27]
        parsed.preamp = pa_att & 0x03
        parsed.attenuator = (pa_att >> 2) & 0x03
        parsed.scope_span = data[32]
        parsed.mode = scope_mode_to_cat(data[60])
        try:
            freq_digits = data[64:69].hex()
            parsed.vfoa_freq = int(freq_digits) if freq_digits.isdigit() else 0
        except ValueError:
            parsed.vfoa_freq = 0
        parsed.s_meter = data[110]
        try:
            parsed.scope_start_freq = struct.unpack(">I", data[144:148])[0]
        except struct.error:
            parsed.scope_start_freq = 0
        # Binary VFOA frequency at offset 132 (4-byte BE).  wfview's comment
        # maps 00 D8 24 08 → 14.165 MHz, so this is the VFO-A frequency in
        # straight binary, not BCD.  Decoded here for comparison with the
        # BCD value at offset 64 and with the CAT FA; query.
        try:
            parsed.vfoa_freq_bin = struct.unpack(">I", data[132:136])[0]
        except struct.error:
            parsed.vfoa_freq_bin = 0

    return parsed


def frame_quality(wf1: list[int]) -> dict:
    """Return quality metrics for a spectrum frame.

    Useful for diagnosing whether the FT4222 is producing valid data:
      - all_zeros: True if every sample is 0 (no signal at all)
      - all_ones:  True if every sample is 255 (inversion might be wrong)
      - nonzero_pct: percentage of non-zero samples
      - max_val, min_val: extreme values
      - dynamic_range: max - min
    """
    if not wf1:
        return {"all_zeros": True, "all_ones": False, "nonzero_pct": 0.0,
                "max_val": 0, "min_val": 0, "dynamic_range": 0}
    nz = sum(1 for v in wf1 if v > 0)
    mx = max(wf1)
    mn = min(wf1)
    return {
        "all_zeros": nz == 0,
        "all_ones": all(v == 255 for v in wf1),
        "nonzero_pct": (nz / len(wf1)) * 100.0,
        "max_val": mx,
        "min_val": mn,
        "dynamic_range": mx - mn,
    }


def encode_pipe_payload(frame: ScopeFrame) -> bytes:
    """Encode parsed data for scope_pipe -> server transport."""
    wf1 = bytes(min(255, max(0, v)) for v in frame.wf1[:WF_SIZE])
    wf2 = bytes(min(255, max(0, v)) for v in frame.wf2[:WF_SIZE])
    metadata = {
        "scope_mode": frame.scope_mode,
        "scope_span": frame.scope_span,
        "preamp": frame.preamp,
        "attenuator": frame.attenuator,
        "mode": frame.mode,
        "s_meter": frame.s_meter,
        "vfoa_freq": frame.vfoa_freq,
        "vfoa_freq_bin": frame.vfoa_freq_bin,
        "scope_start_freq": frame.scope_start_freq,
    }
    meta_bytes = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
    return bytes([PIPE_PAYLOAD_VERSION]) + wf1 + wf2 + struct.pack(">H", len(meta_bytes)) + meta_bytes


def parse_pipe_payload(payload: bytes) -> ScopeFrame:
    """Decode scope_pipe payloads. Version 1 contains spectrum only."""
    if len(payload) < 1 + WF_SIZE:
        raise ValueError(f"scope pipe payload too short: {len(payload)}")
    version = payload[0]
    if version == 1:
        wf1 = list(payload[1:1 + WF_SIZE])
        wf2_start = 1 + WF_SIZE
        wf2 = list(payload[wf2_start:wf2_start + WF_SIZE])
        if len(wf2) < WF_SIZE:
            wf2 = wf2 + [0] * (WF_SIZE - len(wf2))
        return ScopeFrame(wf1=wf1, wf2=wf2)
    if version != PIPE_PAYLOAD_VERSION:
        raise ValueError(f"unsupported scope pipe payload version: {version}")

    min_len = 1 + WF_SIZE * 2 + 2
    if len(payload) < min_len:
        raise ValueError(f"scope pipe payload v2 too short: {len(payload)}")
    wf1 = list(payload[1:1 + WF_SIZE])
    wf2_start = 1 + WF_SIZE
    wf2 = list(payload[wf2_start:wf2_start + WF_SIZE])
    meta_len_start = 1 + WF_SIZE * 2
    meta_len = struct.unpack(">H", payload[meta_len_start:meta_len_start + 2])[0]
    meta_start = meta_len_start + 2
    metadata = json.loads(payload[meta_start:meta_start + meta_len].decode("utf-8")) if meta_len else {}
    return ScopeFrame(
        wf1=wf1,
        wf2=wf2,
        scope_mode=int(metadata.get("scope_mode", 0)),
        scope_span=int(metadata.get("scope_span", 0)),
        preamp=int(metadata.get("preamp", 0)),
        attenuator=int(metadata.get("attenuator", 0)),
        mode=int(metadata.get("mode", 0)),
        s_meter=int(metadata.get("s_meter", 0)),
        vfoa_freq=int(metadata.get("vfoa_freq", 0)),
        vfoa_freq_bin=int(metadata.get("vfoa_freq_bin", 0)),
        scope_start_freq=int(metadata.get("scope_start_freq", 0)),
    )
