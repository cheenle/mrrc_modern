"""Subprocess probe for fail-closed OpenMP team sizing."""

from __future__ import annotations

import ctypes as c
import sys


class Config(c.Structure):
    _fields_ = [
        ("struct_size", c.c_int32),
        ("sample_rate", c.c_int32),
        ("sample_count", c.c_int32),
        ("profile", c.c_int32),
        ("threads", c.c_int32),
        ("cycles", c.c_int32),
        ("sensitivity", c.c_int32),
        ("flags", c.c_int32),
        ("qso_progress", c.c_int32),
        ("rx_frequency", c.c_int32),
        ("tx_frequency", c.c_int32),
        ("low_frequency", c.c_int32),
        ("high_frequency", c.c_int32),
        ("ap_width", c.c_int32),
        ("utc_hhmmss", c.c_int32),
        ("reserved", c.c_int32),
        ("my_call", c.c_char * 13),
        ("dx_call", c.c_char * 13),
        ("dx_grid", c.c_char * 7),
        ("padding", c.c_char * 3),
    ]


class Result(c.Structure):
    _fields_ = [
        ("slot_id", c.c_int64),
        ("sync", c.c_float),
        ("dt", c.c_float),
        ("frequency", c.c_float),
        ("quality", c.c_float),
        ("snr", c.c_int32),
        ("ap_type", c.c_int32),
        ("flags", c.c_int32),
        ("reserved", c.c_int32),
        ("text", c.c_char * 38),
        ("padding", c.c_char * 2),
    ]


def main() -> None:
    library = c.CDLL(sys.argv[1])
    decode = library.wsjt_ft8_decode_improved
    decode.argtypes = [
        c.POINTER(c.c_int16),
        c.POINTER(Config),
        c.c_int64,
        c.POINTER(Result),
        c.c_int32,
        c.POINTER(c.c_int32),
        c.POINTER(c.c_int32),
    ]
    decode.restype = c.c_int32

    samples = (c.c_int16 * 180_000)()
    results = (Result * 256)()
    c.memset(c.byref(results), 0xA5, c.sizeof(results))
    before = bytes(results)
    count = c.c_int32(91)
    overflow = c.c_int32(92)
    config = Config()
    config.struct_size = c.sizeof(Config)
    config.sample_rate = 12_000
    config.sample_count = 180_000
    config.profile = 3
    config.threads = 4
    config.cycles = 1
    config.sensitivity = 2
    config.flags = 0
    config.rx_frequency = 1500
    config.tx_frequency = 1500
    config.low_frequency = 1400
    config.high_frequency = 1600
    config.ap_width = 50
    config.utc_hhmmss = 120000
    config.my_call = b"N0CALL"

    status = decode(
        samples,
        c.byref(config),
        1,
        results,
        256,
        c.byref(count),
        c.byref(overflow),
    )
    assert status == 8, status
    assert count.value == 0, count.value
    assert overflow.value == 0, overflow.value
    assert bytes(results) == before


if __name__ == "__main__":
    main()
