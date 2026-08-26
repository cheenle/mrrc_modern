"""
IC-7300 CI-V scope diagnostics (one-shot, no hardware in CI)
=============================================================
Answers three questions on a live radio WITHOUT the server running:

  1. What is the radio's real CI-V address?  (frames from the radio)
  2. Does 27 11 01 (scope data output ON) actually start a sustained
     0x27 0x00 segment stream?  (counts segments over 4 seconds)
  3. Do addressed queries get answered?  (0x03 frequency read)

Usage:
    venv/bin/python _diag_ic7300_scope.py [PORT] [BAUD] [CIV_ADDR]

Examples:
    venv/Scripts/python.exe _diag_ic7300_scope.py COM5 115200 0x94
    venv/Scripts/python.exe _diag_ic7300_scope.py COM3 38400 0x94

Protocol facts (Icom CI-V):
    frame   = FE FE to from cmd data... FD (no checksum byte)
    from    = 0xE0 = controller; to = 0x94 = IC-7300 default
    0x03    = read frequency (responds only if `to` matches the radio)
    0x27 0x11 01 = scope data output ON   (0x27 0x10 = display only)
    0x27 0x14 00 = scope mode center
    0x27 0x00    = scope waveform segment stream (radio pushes these)
    0xFB / 0xFA  = OK / NG acknowledgements

Safety: this tool only reads PTT state. It never transmits a key-up/PTT-ON
command, and it disables scope data output before closing the serial port.
"""
from __future__ import annotations

import os
import sys
import time

try:
    import serial
except ImportError:  # pragma: no cover - exercised only on incomplete installs
    serial = None

from backends.ic7300.civ_codec import (
    CivFrameParser,
    build_frame,
    decode_freq_bcd,
    parse_scope_segment,
)

CONTROLLER_ADDR = 0xE0
WINDOW_S = 4.0


def main() -> None:
    if serial is None:
        sys.exit("pyserial not installed — run: pip install pyserial")

    port = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "MRRC_SERIAL_PORT", "COM3"
    )
    baud = int(
        sys.argv[2]
        if len(sys.argv) > 2
        else os.environ.get("MRRC_BAUD_RATE", "115200")
    )
    civ_to = int(
        sys.argv[3]
        if len(sys.argv) > 3
        else os.environ.get("IC7300_CIV_ADDR", "0x94"),
        16,
    )

    print(
        f"Opening {port} @ {baud} baud, controller addr "
        f"0x{CONTROLLER_ADDR:02X}, querying radio addr 0x{civ_to:02X} ..."
    )
    try:
        ser = serial.Serial(port, baud, timeout=0.2, write_timeout=1.0)
    except serial.SerialException as exc:
        sys.exit(f"FAIL: cannot open {port}: {exc}")

    parser = CivFrameParser()
    counts = {
        "freq_resp": 0,
        "ptt_read": 0,
        "ptt_tx": 0,
        "scope_seg": 0,
        "scope_info": 0,
        "ok": 0,
        "ng": 0,
        "other": 0,
    }
    from_addrs: dict[int, int] = {}
    info_modes: set[int] = set()

    try:
        ser.reset_input_buffer()

        # Probe set: frequency read, PTT read, scope output ON, center mode.
        # PTT is read-only: this script never sends a key-up command.
        ser.write(build_frame(0x03, to=civ_to))
        time.sleep(0.3)
        ser.write(build_frame(0x1C, bytes((0x00,)), to=civ_to))
        time.sleep(0.3)
        ser.write(build_frame(0x27, bytes((0x11, 0x01)), to=civ_to))
        ser.write(build_frame(0x27, bytes((0x14, 0x00)), to=civ_to))
        ser.flush()

        started = time.monotonic()
        print(f"Listening for {WINDOW_S:.0f}s ...")
        while time.monotonic() - started < WINDOW_S:
            chunk = ser.read(512)
            if not chunk:
                continue
            for frame in parser.feed(chunk):
                from_addrs[frame.from_addr] = (
                    from_addrs.get(frame.from_addr, 0) + 1
                )
                if frame.command == 0x03 and frame.data:
                    counts["freq_resp"] += 1
                    freq_hz = decode_freq_bcd(frame.data[:5])
                    print(
                        f"  freq response: {freq_hz / 1e6:.4f} MHz  "
                        f"(from 0x{frame.from_addr:02X})"
                    )
                elif frame.command == 0x1C and frame.data[:1] == b"\x00":
                    counts["ptt_read"] += 1
                    if len(frame.data) > 1 and frame.data[1] == 0x01:
                        counts["ptt_tx"] += 1
                        print(
                            "  [TX!] PTT read says TRANSMITTING "
                            f"(from 0x{frame.from_addr:02X})"
                        )
                elif frame.command == 0x27:
                    segment = parse_scope_segment(frame)
                    if segment is None:
                        counts["other"] += 1
                        continue
                    counts["scope_seg"] += 1
                    if segment.sequence == 1:
                        counts["scope_info"] += 1
                        if segment.scope_mode is not None:
                            info_modes.add(segment.scope_mode)
                elif frame.command == 0xFB:
                    counts["ok"] += 1
                elif frame.command == 0xFA:
                    counts["ng"] += 1
                else:
                    counts["other"] += 1
    finally:
        try:
            ser.write(build_frame(0x27, bytes((0x11, 0x00)), to=civ_to))
            ser.flush()
        except Exception as exc:  # best-effort shutdown on a failing link
            print(f"WARNING: could not disable scope data output: {exc}")
        ser.close()

    print("\n=== RESULTS ===")
    print(
        "Frames from radio addresses: "
        f"{ {hex(k): v for k, v in sorted(from_addrs.items())} }"
    )
    real_addr = (
        f"0x{max(from_addrs, key=lambda addr: from_addrs[addr]):02X}"
        if from_addrs
        else "UNKNOWN"
    )
    print(
        f"  -> radio real address is {real_addr}  "
        f"(controller used 0x{civ_to:02X})"
    )
    print(f"Frequency query (0x03) responses: {counts['freq_resp']}")
    print(
        f"PTT read (0x1C 00) responses: {counts['ptt_read']},  "
        f"of which TRANSMITTING: {counts['ptt_tx']}"
    )
    if counts["ptt_tx"]:
        print(
            "  >>> RADIO IS ACTUALLY KEYED/TX. If the panel TX lamp is on while "
            "only this script is open, the cause is NOT MRRC: check VOX, "
            "BK-IN, other software, or a physical PTT input."
        )
    print(
        f"Scope segments (0x27 0x00) in {WINDOW_S:.0f}s: "
        f"{counts['scope_seg']}"
    )
    print(f"  of which seq-1 info chunks: {counts['scope_info']}")
    if info_modes:
        mode_names = {0: "center", 1: "fixed", 2: "scroll-c", 3: "scroll-f"}
        modes = [mode_names.get(mode, f"0x{mode:02X}?") for mode in sorted(info_modes)]
        print(f"  info-chunk scope modes seen: {modes}")
    print(
        f"OK acks (0xFB): {counts['ok']}   NG acks (0xFA): "
        f"{counts['ng']}   other frames: {counts['other']}"
    )

    print("\n=== INTERPRETATION ===")
    if not from_addrs:
        print("NO frames from the radio at all -> wrong port, wrong baud, or radio off.")
    else:
        if counts["freq_resp"] == 0:
            print(
                "No response to 0x03 -> WRONG CI-V ADDRESS "
                "(set IC7300_CIV_ADDR) or the radio NAKs addressed commands."
            )
        else:
            print("Frequency query answered -> address + baud are correct.")
        if counts["scope_seg"] == 0:
            print(
                "NO scope stream after 27 11 01 -> radio menu 'CI-V Output' "
                "must be 'Scope' (MENU > Set > Connectors > CI-V > CI-V Output)."
            )
        elif counts["scope_seg"] < 10:
            print(
                f"Scope stream started but only {counts['scope_seg']} segments in "
                f"{WINDOW_S:.0f}s (expected 10+/s) -> stream stops early: check "
                "radio menu, USB cable, or CI-V load."
            )
        else:
            print(
                "Scope stream sustained -> the server-side consumer has a bug; "
                "paste the server STARTUP log (before the WS lines)."
            )
    if counts["ng"]:
        print(
            "Radio sent NG (0xFA) for one or more set commands -> command "
            "format/parameter rejected; note which commands above."
        )
    if counts["ptt_tx"] and counts["ptt_read"] == counts["ptt_tx"]:
        print(
            "NOTE: every PTT read said TX -> the radio stays keyed regardless of "
            "any command. Most likely VOX/BK-IN or an external controller."
        )
    elif not counts["ptt_tx"] and counts["ptt_read"]:
        print(
            "PTT reads all RX -> radio is NOT transmitting by itself; any TX shown "
            "in the MRRC UI comes from the UI keying it."
        )


if __name__ == "__main__":
    main()
