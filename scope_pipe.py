#!/usr/bin/env python3
"""
FT-710 Scope Data Pipe
======================
Standalone process that reads scope data from FT4222 SPI and writes
binary frames to stdout.  Run as a subprocess from server.py.

Binary frame format (written to stdout):
  4-byte BE uint32 frame_len
  frame_len bytes of spectrum data (version + wf1 + wf2 + metadata)

This avoids ctypes+asyncio threading issues in the main server.

Protocol:
  - Every frame: <4-byte BE length><payload>
  - Heartbeat every 1s when idle (len=0): <0x00 0x00 0x00 0x00>
  - Status lines on stderr prefixed with "STATUS:" for machine parsing
  - Control channel on stdin: "TX:1\n" / "TX:0\n" from the server marks
    radio transmit (PTT/TUNE) state.  The FT-710 garbles its scope
    stream during TX, so while TX is active SPI reads pause and all
    sync/stall recovery counters freeze; one clean re-sync runs when
    RX resumes.  stdin EOF (parent died) also stops the pipe — a guard
    against orphaned workers on Windows (see the heartbeat comment).
"""
import ctypes
import signal
import struct
import sys
import threading
import time
from ctypes import (
    c_void_p, c_uint32, c_uint16, c_uint8, c_bool,
    POINTER, byref, CDLL, create_string_buffer,
)

from scope_frame import (
    SCOPE_FRAME_SIZE,
    encode_pipe_payload,
    parse_scope_frame,
)
from scope_libraries import configure_windows_dll_search_path
from scope_libraries import require_ftdi_libraries
from scope_libraries import get_ft4222_clock_divider

FT_OK = 0
FT4222_OK = 0
FT4222_TRANSFER_IN_PROGRESS = 10
FT_OPEN_BY_DESCRIPTION = 2
SPI_IO_SINGLE = 1
CLK_IDLE_HIGH = 1
CLK_LEADING = 0
SYS_CLK_24 = 1

MAX_CONSECUTIVE_ERRORS = 50
MAX_REINIT_CYCLES = 5       # consecutive full-device reinitialisations before giving up
STALL_TIMEOUT = 2.0         # seconds without a successful frame before reinit
TRANSFER_PROGRESS_SLEEP = 0.002  # sleep between TRANSFER_IN_PROGRESS polls
STDOUT_HEARTBEAT_S = 1.0    # len=0 keepalive; also detects a dead parent (EPIPE)
RESYNC_SETTLE_S = 1.0       # idle-bus settle between close and reopen in resync_device

running = True


def stop_running(_signum, _frame):
    global running
    running = False


# ── TX-aware pause (stdin control channel) ──────────────────────────

def apply_control_line(line: str, state: dict) -> bool:
    """Apply one stdin control line.  Returns True for known commands.

    state keys: tx_active (bool), tx_resync (bool).  TX:1 marks the
    radio transmitting (pause SPI reads); TX:0 marks RX again and arms
    a one-shot clean re-sync.
    """
    cmd = line.strip().upper()
    if cmd == "TX:1":
        state["tx_active"] = True
        return True
    if cmd == "TX:0":
        if state["tx_active"]:
            state["tx_resync"] = True
        state["tx_active"] = False
        return True
    return False


def _stdin_reader(state: dict):
    """Background thread: apply server control lines from stdin.

    EOF means the parent closed the pipe (server exited) — stop the main
    loop.  Extra guard against orphaned workers holding the FT4222 on
    Windows, where TerminateProcess on the onefile bootloader never
    reaches the real child process.
    """
    global running
    try:
        for line in sys.stdin:
            apply_control_line(line, state)
    except Exception:
        pass
    running = False


def emit_status(msg: str):
    """Write a machine-parseable status line to stderr."""
    sys.stderr.write(f"STATUS:{msg}\n")
    sys.stderr.flush()


def resync_device(d2xx, f4, ft_handle, clock_divider: int):
    """Close the FT4222, let the bus idle, then reopen it fresh.

    Replaces the old byte-by-byte re-sync (wfview ft4222Handler::sync):
    every FT4222 SingleRead is its own SPI transaction (CS toggles per
    call), so a contiguous multi-byte sync pattern can never be observed
    one byte at a time — the byte resync only consumed the stream and
    worsened alignment.  A close + ~1s settle + reopen is the pattern
    that reliably realigns (same as a full pipe restart).
    """
    close_device(d2xx, f4, ft_handle)
    time.sleep(RESYNC_SETTLE_S)
    return open_device(d2xx, f4, clock_divider)


def open_device(d2xx, f4, clock_divider: int):
    """Open and initialize the FT4222 SPI device.

    Returns (ft_handle, d2xx, f4) on success, or (None, None, None) on failure.
    The caller owns the ctypes library handles — they are NOT unloaded here.
    """
    for attempt in range(5):
        ft_handle = c_void_p()
        desc = create_string_buffer(b"FT4222 A")
        s = d2xx.FT_OpenEx(desc, FT_OPEN_BY_DESCRIPTION, byref(ft_handle))
        if s != FT_OK:
            emit_status(f"open_failed:attempt_{attempt+1}:error_{s}")
            time.sleep(0.3)
            continue

        d2xx.FT_SetTimeouts(ft_handle, 100, 100)
        d2xx.FT_SetLatencyTimer(ft_handle, 2)

        s = f4.FT4222_SPIMaster_Init(
            ft_handle, SPI_IO_SINGLE, clock_divider, CLK_IDLE_HIGH, CLK_LEADING, 0x01,
        )
        if s == FT4222_OK:
            if f4.FT4222_SetClock(ft_handle, SYS_CLK_24) == FT4222_OK:
                emit_status(f"spi_ready:attempt_{attempt+1}:clk_div_{clock_divider}")
                return ft_handle
            else:
                emit_status(f"set_clock_failed:attempt_{attempt+1}")

        # Cleanup failed attempt
        try:
            f4.FT4222_UnInitialize(ft_handle)
        except Exception:
            pass
        try:
            d2xx.FT_Close(ft_handle)
        except Exception:
            pass
        emit_status(f"spi_init_retry:attempt_{attempt+1}:error_{s}")
        time.sleep(0.3)

    emit_status("spi_init_failed:all_attempts")
    return None


def close_device(d2xx, f4, ft_handle):
    """Safely close and uninitialize the FT4222 device."""
    if ft_handle is None:
        return
    try:
        f4.FT4222_UnInitialize(ft_handle)
    except Exception:
        pass
    try:
        d2xx.FT_Close(ft_handle)
    except Exception:
        pass


def main():
    global running
    signal.signal(signal.SIGINT, stop_running)
    signal.signal(signal.SIGTERM, stop_running)

    # stdin control channel (TX pause/resume, parent-death detection)
    control = {"tx_active": False, "tx_resync": False}
    threading.Thread(target=_stdin_reader, args=(control,), daemon=True).start()

    # ── Load libraries ──────────────────────────────────────────────
    configure_windows_dll_search_path()
    ft4222_path, ftd2xx_path = require_ftdi_libraries()
    emit_status(f"libs_loaded:ft4222={ft4222_path.name}:ftd2xx={ftd2xx_path.name}")

    d2xx = CDLL(str(ftd2xx_path))
    f4 = CDLL(str(ft4222_path))

    # Setup D2XX function signatures
    d2xx.FT_OpenEx.argtypes = [c_void_p, c_uint32, POINTER(c_void_p)]
    d2xx.FT_OpenEx.restype = c_uint32
    d2xx.FT_Close.argtypes = [c_void_p]
    d2xx.FT_Close.restype = c_uint32
    d2xx.FT_SetTimeouts.argtypes = [c_void_p, c_uint32, c_uint32]
    d2xx.FT_SetTimeouts.restype = c_uint32
    d2xx.FT_SetLatencyTimer.argtypes = [c_void_p, c_uint8]
    d2xx.FT_SetLatencyTimer.restype = c_uint32

    # Setup FT4222 function signatures
    f4.FT4222_UnInitialize.argtypes = [c_void_p]
    f4.FT4222_UnInitialize.restype = c_uint32
    f4.FT4222_SPIMaster_Init.argtypes = [
        c_void_p, c_uint32, c_uint32, c_uint32, c_uint32, c_uint8,
    ]
    f4.FT4222_SPIMaster_Init.restype = c_uint32
    f4.FT4222_SPIMaster_SingleRead.argtypes = [
        c_void_p, POINTER(c_uint8), c_uint16, POINTER(c_uint16), c_bool,
    ]
    f4.FT4222_SPIMaster_SingleRead.restype = c_uint32
    f4.FT4222_SetClock.argtypes = [c_void_p, c_uint32]
    f4.FT4222_SetClock.restype = c_uint32

    # ── Open device ─────────────────────────────────────────────────
    clock_divider = get_ft4222_clock_divider()
    ft_handle = open_device(d2xx, f4, clock_divider)
    if ft_handle is None:
        emit_status("fatal:cannot_open_device")
        sys.exit(1)

    # ── Read loop ───────────────────────────────────────────────────
    buf = (c_uint8 * SCOPE_FRAME_SIZE)()
    sz = c_uint16()
    frame_count = 0
    last_emit = time.time()
    last_heartbeat = time.time()
    last_stdout_write = time.time()
    last_successful_frame = time.time()  # time-based stall detection
    consecutive_errors = 0
    consecutive_bad_frames = 0
    reinit_count = 0
    tx_pause_logged = False

    emit_status("pipe_running")

    try:
        while running:
            now = time.time()

            # ── Stdout heartbeat ──────────────────────────────────
            # Keeps the pipe protocol alive while idle AND detects a
            # dead parent: writing to a readerless pipe raises
            # BrokenPipeError, which lands in the outer except and lets
            # this process exit instead of orphaning and holding the
            # FT4222 device (Windows: TerminateProcess on the onefile
            # bootloader never reaches the real child process).
            if now - last_stdout_write >= STDOUT_HEARTBEAT_S:
                sys.stdout.buffer.write(struct.pack('>I', 0))
                sys.stdout.buffer.flush()
                last_stdout_write = now

            # ── TX pause: radio is transmitting ─────────────────────
            # The FT-710 garbles its scope stream during TX.  Reading
            # it would just churn the sync/stall recovery machinery
            # (and, historically, run it into fatal:too_many_reinits),
            # so freeze everything until RX resumes.
            if control["tx_active"]:
                last_successful_frame = now
                consecutive_errors = 0
                consecutive_bad_frames = 0
                reinit_count = 0
                if not tx_pause_logged:
                    emit_status("tx_pause")
                    tx_pause_logged = True
                time.sleep(0.05)
                continue
            if tx_pause_logged:
                tx_pause_logged = False
            if control["tx_resync"]:
                # TX → RX transition: full device re-sync (close, settle,
                # reopen) — the pattern that reliably realigns the stream.
                control["tx_resync"] = False
                emit_status("tx_resume:resync")
                ft_handle = resync_device(d2xx, f4, ft_handle, clock_divider)
                if ft_handle is None:
                    emit_status("fatal:resync_failed_after_tx")
                    break
                emit_status("sync_recovered")
                last_successful_frame = time.time()

            # ── Time-based stall detection ──────────────────────────
            # If no successful frame for STALL_TIMEOUT seconds (and we've
            # had at least one frame to know the device was working),
            # the SPI bus may be hung.  Re-initialize the device.
            # Using elapsed time instead of a TRANSFER_IN_PROGRESS counter
            # is robust regardless of SPI clock speed — a slow clock
            # may need >100ms per frame, which the old 50-iteration
            # counter (50ms) could never satisfy.
            if (frame_count > 0
                    and now - last_successful_frame > STALL_TIMEOUT
                    and reinit_count <= MAX_REINIT_CYCLES):
                reinit_count += 1
                emit_status(f"spi_stalled:{now - last_successful_frame:.1f}s:"
                            f"reinit_{reinit_count}/{MAX_REINIT_CYCLES}")
                close_device(d2xx, f4, ft_handle)
                ft_handle = open_device(d2xx, f4, clock_divider)
                if ft_handle is None:
                    emit_status("fatal:reinit_failed_after_stall")
                    break
                last_successful_frame = time.time()
                consecutive_errors = 0
                consecutive_bad_frames = 0
                continue

            # ── Heartbeat ───────────────────────────────────────────
            if now - last_heartbeat >= 2.0:
                emit_status(f"heartbeat:frames={frame_count}:errors={consecutive_errors}")
                last_heartbeat = now

            # ── SPI read ────────────────────────────────────────────
            status = f4.FT4222_SPIMaster_SingleRead(
                ft_handle, buf, SCOPE_FRAME_SIZE, byref(sz), False,
            )

            if status == FT4222_TRANSFER_IN_PROGRESS:
                # Normal condition: data not ready yet.  Sleep briefly
                # and retry without counting this as an error.
                time.sleep(TRANSFER_PROGRESS_SLEEP)
                continue

            if status != FT4222_OK:
                consecutive_errors += 1
                # ── Diagnostic: sample the raw FT4222 error codes ──
                if consecutive_errors in (1, 5, 10, 20, 30, 40, 45, 48):
                    emit_status(f"spi_read_error:status_{status}:"
                                f"count_{consecutive_errors}")
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    reinit_count += 1
                    if reinit_count > MAX_REINIT_CYCLES:
                        emit_status(f"fatal:too_many_errors:{consecutive_errors}:"
                                    f"reinit_count_{reinit_count}")
                        break
                    emit_status(f"too_many_errors:{consecutive_errors}:"
                                f"reinit_{reinit_count}/{MAX_REINIT_CYCLES}")
                    close_device(d2xx, f4, ft_handle)
                    ft_handle = open_device(d2xx, f4, clock_divider)
                    if ft_handle is None:
                        emit_status("fatal:reinit_failed")
                        break
                    last_successful_frame = time.time()
                    consecutive_errors = 0
                    consecutive_bad_frames = 0
                else:
                    time.sleep(0.005)
                continue

            consecutive_errors = 0  # reset on successful read

            if sz.value != SCOPE_FRAME_SIZE:
                # Partial read — skip and retry
                emit_status(f"short_read:{sz.value}")
                time.sleep(0.001)
                continue

            frame = bytes(buf[:SCOPE_FRAME_SIZE])

            # Parse frame
            try:
                parsed = parse_scope_frame(frame)
                last_successful_frame = time.time()
                consecutive_bad_frames = 0
                reinit_count = 0  # successful frame resets the reinit counter
            except ValueError:
                # Frame doesn't parse — stream is misaligned/garbled.
                # Re-sync at the device level (close/settle/reopen); the
                # old byte-by-byte resync could never work on the FT4222
                # (each 1-byte SingleRead is its own SPI transaction).
                consecutive_bad_frames += 1
                reinit_count += 1
                emit_status(f"sync_lost:bad_frame_{consecutive_bad_frames}:"
                            f"resync_{reinit_count}/{MAX_REINIT_CYCLES}")
                if reinit_count > MAX_REINIT_CYCLES:
                    emit_status(f"fatal:too_many_resyncs:{reinit_count}")
                    break
                ft_handle = resync_device(d2xx, f4, ft_handle, clock_divider)
                if ft_handle is None:
                    emit_status("fatal:resync_failed")
                    break
                emit_status("sync_recovered")
                last_successful_frame = time.time()
                consecutive_bad_frames = 0
                continue

            # Encode and write to stdout
            payload = encode_pipe_payload(parsed)

            # Write length-prefixed frame to stdout
            sys.stdout.buffer.write(struct.pack('>I', len(payload)))
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()
            last_stdout_write = time.time()

            frame_count += 1

            # Periodic diagnostic (every 30 frames)
            if frame_count % 30 == 0:
                now = time.time()
                fps = 30.0 / (now - last_emit) if (now - last_emit) > 0 else 0
                n_nonzero = sum(1 for b in parsed.wf1 if b > 0)
                emit_status(
                    f"diag:frames={frame_count}:fps={fps:.1f}:"
                    f"wf1_max={max(parsed.wf1)}:wf1_nz={n_nonzero}:"
                    f"span={parsed.scope_span}:s_meter={parsed.s_meter}"
                )
                last_emit = now

    except Exception as e:
        emit_status(f"pipe_error:{e}")
    finally:
        emit_status("pipe_stopping")
        close_device(d2xx, f4, ft_handle)
        emit_status("pipe_stopped")


if __name__ == "__main__":
    main()
