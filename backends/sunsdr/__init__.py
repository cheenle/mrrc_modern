"""SunSDR2 DX native backend (Expert Electronics SunSDR2 DX, UDP control).

Task 1 delivers the pure protocol layer in :mod:`backends.sunsdr.protocol`;
the RadioBackend wiring (Task 2+) arrives in a later task.
"""
from .protocol import (
    MAGIC,
    TRX_INDEX,
    IF_OFFSET_HZ,
    IQ_SCALE,
    IQ_SAMPLES_PER_PACKET,
    IQ_PAYLOAD_BYTES,
    IQ_PACKET_BYTES,
    IQ_SUBTYPE,
    TELEM_POWER_SUBTYPE,
    TELEM_STATUS_SUBTYPE,
    PTT_CONFIRM_BIT,
    STREAM_COUNTER_START,
    STREAM_COUNTER_STEP,
    SAMPLE_RATES,
    HW_INIT_RATE_INDEX,
    DEFAULT_RATE,
    STREAM_CTRL_WORDS,
    CmdID,
    build_packet,
    build_ptt,
    drive_byte,
    build_drive,
    build_rx_frequency,
    build_tx_frequency,
    build_hw_init,
    build_stream_ctrl,
    build_boot_sequence,
    BOOT_PHASE1_HEX,
    BOOT_STATUS_HEX,
    BOOT_STREAM_PRE_HEX,
    BOOT_STREAM_POST_HEX,
    pack_iq_24bit,
    unpack_iq_24bit,
    parse_rx_packet,
    parse_telemetry,
)

__all__ = [name for name in dir() if not name.startswith("_")]
