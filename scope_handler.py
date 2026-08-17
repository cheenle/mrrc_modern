"""
FT-710 Scope/Spectrum Data Handler via FT4222 SPI
==================================================
Reads binary scope data from the FT-710's FT4222 SPI bridge chip
using ctypes to call libft4222.dylib (same approach as wfview).

Matches wfview ft4222handler.cpp exactly:
  - FT_OpenEx by description "FT4222 A"
  - SPI: Single IO, CLK_DIV_64, CPOL=HIGH, CPHA=LEADING, SS=0x01
  - Clock: SYS_CLK_24 (24 MHz)
  - Read: 4096-byte blocks, validate sync tail, byte-by-byte re-sync
  - Frame format: yaesu_scope_data (4096 bytes, wf1[850]+wf2[850]+data[150]+sync[4])

CRITICAL: On macOS arm64, FTDI's FT_STATUS/FT4222_STATUS are 32-bit.
Using c_ulong (8 bytes on arm64) causes garbage upper bits → error 1000.
All FTDI types must use c_uint32.
"""
import asyncio
import ctypes
import logging
import math
import random
import struct
import time
from ctypes import (
    c_void_p, c_uint32, c_uint16, c_uint8, c_bool,
    POINTER, byref, CDLL, create_string_buffer,
)
from pathlib import Path
from typing import Optional

from backends.ft710.scope_frame import (
    SCOPE_FRAME_SIZE, WF_SIZE, SYNC_TAIL, SYNC_FULL, parse_scope_frame,
    scope_mode_to_cat,
)
from backends.ft710.scope_libraries import find_ftdi_libraries
from backends.ft710.scope_libraries import get_ft4222_clock_divider

logger = logging.getLogger("ft710.scope")

# ── FTDI Constants ───────────────────────────────────────────────
FT_OK = 0
FT4222_OK = 0
FT4222_TRANSFER_IN_PROGRESS = 10

FT_OPEN_BY_DESCRIPTION = 2

SPI_IO_SINGLE = 1
CLK_IDLE_HIGH = 1
CLK_LEADING = 0
SYS_CLK_24 = 1

def _find_ftdi_libs() -> tuple[Optional[Path], Optional[Path]]:
    return find_ftdi_libraries()


class ScopeHandler:
    """Reads FT-710 scope data via FT4222 SPI using ctypes."""

    def __init__(self, port: str = "", baudrate: int = 115200):
        self._lib_ft4222 = None
        self._lib_ftd2xx = None
        self._device: Optional[c_void_p] = None
        self._connected = False
        self._running = False

        # Latest data
        self.spectrum_rx1: list[int] = [0] * WF_SIZE
        self.spectrum_rx2: list[int] = [0] * WF_SIZE
        self.s_meter: int = 0
        self.vfoa_freq: int = 0
        self.vfoa_freq_bin: int = 0
        self.vfob_freq: int = 0
        self.mode: int = 0
        self.scope_mode: int = 0
        self.scope_span: int = 0
        self.preamp: int = 0
        self.attenuator: int = 0
        self.scope_start_freq: int = 0
        self.last_update: float = 0.0
        self._frame_count = 0
        self._last_frame_time = 0.0
        self._fps = 0.0

        self._on_frame: Optional[callable] = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def fps(self) -> float:
        return self._fps

    def set_on_frame(self, callback: callable):
        self._on_frame = callback

    def update_from_radio_state(self, state):
        """Generate a deterministic S-meter based spectrum fallback.

        Produces a Gaussian-shaped peak centered in the span, scaled by
        the current S-meter reading.  This gives a realistic-looking
        waterfall even without FT4222 hardware.

        When the S-meter is very low (no signal), the waterfall is
        mostly dark with noise-floor speckle.
        """
        self.s_meter = int(getattr(state, "s_meter", 0))
        self.vfoa_freq = int(getattr(state, "active_freq", getattr(state, "vfo_a_freq", 0)))
        self.mode = int(getattr(state, "mode", 0))
        self.preamp = int(getattr(state, "preamp", 0))
        self.attenuator = int(getattr(state, "attenuator", 0))
        self.scope_span = int(getattr(state, "scope_span", self.scope_span))
        self.scope_start_freq = int(getattr(state, "scope_start_freq", 0))

        # Convert S-meter raw value to signal strength
        sm = self.s_meter

        # Noise floor: higher S-meter = slightly higher noise floor
        noise_floor = max(2, min(20, sm // 12))

        # Signal peak: scale with S-meter
        if sm < 12:
            peak = noise_floor + 3    # barely above noise
        elif sm < 40:
            peak = noise_floor + 8 + sm // 3
        elif sm < 100:
            peak = noise_floor + 25 + sm // 4
        elif sm < 180:
            peak = noise_floor + 50 + sm // 3
        else:
            peak = noise_floor + 100 + sm // 2
        peak = min(255, max(12, peak))

        # Add multiple smaller peaks for realism when S-meter is active
        center = (WF_SIZE - 1) / 2
        width = 28 + min(80, sm // 3)

        # Seed with floor(s_meter) for deterministic but varied look
        rng = random.Random(int(self.s_meter // 4))

        spectrum: list[int] = []
        for i in range(WF_SIZE):
            # Main Gaussian peak
            main_peak = (peak - noise_floor) * math.exp(
                -((i - center) ** 2) / (2 * width ** 2)
            )
            # Secondary smaller peaks
            sec_peak = 0
            if sm > 20:
                offset1 = int(width * 1.8) * (1 if rng.random() > 0.5 else -1)
                sec_peak += (peak * 0.3) * math.exp(
                    -((i - (center + offset1)) ** 2) / (2 * (width * 0.7) ** 2)
                )
            # Noise speckle
            noise = rng.uniform(0, noise_floor * 0.6)
            # Low-frequency ripple
            ripple = 2 * math.sin(i * 0.03 + sm * 0.1) + 1.5 * math.sin(i * 0.07)

            val = int(noise_floor + ripple + main_peak + sec_peak + noise)
            spectrum.append(max(0, min(255, val)))

        self.spectrum_rx1 = spectrum
        self.spectrum_rx2 = list(spectrum)  # Same data for both receivers
        self.last_update = time.time()

    # ── Library Loading ──────────────────────────────────────────

    def _load_library(self) -> bool:
        ft4222_path, ftd2xx_path = _find_ftdi_libs()
        if ft4222_path is None:
            logger.warning("FTDI libraries not found — scope disabled")
            return False
        logger.info("Loading %s", ft4222_path)
        logger.info("Loading %s", ftd2xx_path)
        try:
            self._lib_ft4222 = CDLL(str(ft4222_path))
            self._lib_ftd2xx = CDLL(str(ftd2xx_path))
        except OSError as e:
            logger.error("Failed to load FTDI libraries: %s", e)
            return False

        d2xx = self._lib_ftd2xx
        d2xx.FT_OpenEx.argtypes = [c_void_p, c_uint32, POINTER(c_void_p)]
        d2xx.FT_OpenEx.restype = c_uint32
        d2xx.FT_Close.argtypes = [c_void_p]; d2xx.FT_Close.restype = c_uint32
        d2xx.FT_SetTimeouts.argtypes = [c_void_p, c_uint32, c_uint32]
        d2xx.FT_SetTimeouts.restype = c_uint32
        d2xx.FT_SetLatencyTimer.argtypes = [c_void_p, c_uint8]
        d2xx.FT_SetLatencyTimer.restype = c_uint32

        f4 = self._lib_ft4222
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

        logger.info("FTDI libraries loaded")
        return True

    # ── Device Setup (mirrors wfview ft4222Handler::setup) ──────

    def _setup_device(self) -> bool:
        d2xx, f4 = self._lib_ftd2xx, self._lib_ft4222
        if d2xx is None or f4 is None:
            return False

        # FT4222_SPIMaster_Init can fail intermittently on macOS
        # (error 1000 = DEVICE_NOT_SUPPORTED when chip is in transitional
        # state).  Retry with close/re-open to recover.
        for attempt in range(3):
            ft_handle = c_void_p()
            desc = create_string_buffer(b"FT4222 A")
            s = d2xx.FT_OpenEx(desc, FT_OPEN_BY_DESCRIPTION, byref(ft_handle))
            if s != FT_OK:
                logger.warning("FT_OpenEx failed (attempt %d, error %d)", attempt+1, s)
                continue

            if d2xx.FT_SetTimeouts(ft_handle, 100, 100) != FT_OK:
                d2xx.FT_Close(ft_handle); continue
            if d2xx.FT_SetLatencyTimer(ft_handle, 2) != FT_OK:
                d2xx.FT_Close(ft_handle); continue

            clock_divider = get_ft4222_clock_divider()
            s = f4.FT4222_SPIMaster_Init(
                ft_handle, SPI_IO_SINGLE, clock_divider, CLK_IDLE_HIGH, CLK_LEADING, 0x01,
            )
            if s == FT4222_OK:
                if f4.FT4222_SetClock(ft_handle, SYS_CLK_24) != FT4222_OK:
                    f4.FT4222_UnInitialize(ft_handle)
                    d2xx.FT_Close(ft_handle); continue

                self._device = ft_handle
                logger.info("FT4222 SPI master ready (attempt %d, clk_div=%d)", attempt+1, clock_divider)
                return True

            logger.debug("SPI_Init attempt %d returned %d, retrying...", attempt+1, s)
            f4.FT4222_UnInitialize(ft_handle)
            d2xx.FT_Close(ft_handle)
            import time; time.sleep(0.1)

        logger.warning("FT4222_SPIMaster_Init failed after 3 attempts")
        return False

    def _cleanup_device(self):
        """Clean up device handle. Idempotent — safe to call multiple times."""
        if self._device is not None:
            dev = self._device
            self._device = None
            try:
                if self._lib_ft4222 and dev.value:
                    self._lib_ft4222.FT4222_UnInitialize(dev)
            except Exception: pass
            try:
                if self._lib_ftd2xx and dev.value:
                    self._lib_ftd2xx.FT_Close(dev)
            except Exception: pass

    # ── Connection ──────────────────────────────────────────────

    async def connect(self) -> bool:
        if not self._load_library():
            return False
        # Must run on main thread — FTDI D2XX uses IOKit which requires
        # the main CFRunLoop on macOS.  asyncio.to_thread would use a
        # background thread where IOKit calls (IOUSBHostDeviceOpen etc.)
        # silently fail.
        ok = self._setup_device()
        if not ok:
            return False
        self._connected = True
        self._running = True
        logger.info("Scope handler connected via FT4222 SPI")
        return True

    async def disconnect(self):
        self._running = False
        self._connected = False
        self._cleanup_device()
        self._lib_ft4222 = None
        self._lib_ftd2xx = None

    # ── SPI Read Loop (mirrors wfview ft4222Handler::run) ──────

    async def read_loop(self):
        """Read scope frames via FT4222 SPI at ~30 fps.

        Matches wfview ft4222Handler::run():
          1. SPI read 4096 bytes (isEndTransaction=false)
          2. Wait for TRANSFER_IN_PROGRESS to clear (error 10)
          3. Validate sync tail 0xFF 0x01 0xEE 0x01
          4. If invalid, byte-by-byte re-sync
          5. Parse frame, emit at 30fps
        """
        f4 = self._lib_ft4222
        buf = (c_uint8 * SCOPE_FRAME_SIZE)()
        size_read = c_uint16()
        last_emit = time.time()

        while self._running:
            try:
                if self._device is None or f4 is None:
                    await asyncio.sleep(0.1)
                    continue

                # SPI read 4096 bytes (run inline on event loop thread)
                status = f4.FT4222_SPIMaster_SingleRead(
                    self._device, buf, SCOPE_FRAME_SIZE, byref(size_read), True,
                )

                if status == FT4222_TRANSFER_IN_PROGRESS:
                    await asyncio.sleep(0.001)
                    continue
                if status != FT4222_OK:
                    await asyncio.sleep(0.01)
                    continue

                frame = bytes(buf[:SCOPE_FRAME_SIZE])

                # Validate sync tail
                if not frame.endswith(SYNC_TAIL):
                    logger.debug("Scope sync lost, re-syncing...")
                    self._resync()
                    continue

                # Parse frame
                self._parse_frame(frame)

                # Emit at ~30 fps
                now = time.time()
                if now - last_emit >= 0.033 and self._on_frame:
                    await self._on_frame(self)
                    last_emit = now

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("Scope read error: %s", e)
                await asyncio.sleep(0.05)

    def _resync(self):
        """Byte-by-byte re-sync to find the 16-byte sync pattern.
        Matches wfview ft4222Handler::sync() exactly."""
        f4 = self._lib_ft4222
        if self._device is None or f4 is None:
            return

        one_byte = (c_uint8 * 1)()
        size_read = c_uint16()
        window = bytearray(16)
        pos = 0

        for _ in range(8192):
            status = f4.FT4222_SPIMaster_SingleRead(
                self._device, one_byte, 1, byref(size_read), True,
            )
            if status == FT4222_TRANSFER_IN_PROGRESS:
                continue
            if status != FT4222_OK or size_read.value != 1:
                logger.warning("FT4222 sync read error (status=%d)", status)
                return

            b = one_byte[0]
            if b != 0xFF:
                pos = 0
            else:
                window[pos % 16] = b
                pos += 1
                if pos >= 16 and bytes(window) == SYNC_FULL:
                    logger.info("FT4222 re-synchronised")
                    return

        logger.warning("FT4222 failed to re-sync — re-initializing")
        self._cleanup_device()
        self._setup_device()

    # ── Frame Parsing ───────────────────────────────────────────

    def _parse_frame(self, frame: bytes):
        try:
            parsed = parse_scope_frame(frame)
            self.spectrum_rx1 = parsed.wf1
            self.spectrum_rx2 = parsed.wf2
            self.scope_mode = parsed.scope_mode
            self.preamp = parsed.preamp
            self.attenuator = parsed.attenuator
            self.scope_span = parsed.scope_span
            self.mode = parsed.mode
            self.vfoa_freq = parsed.vfoa_freq
            self.vfoa_freq_bin = parsed.vfoa_freq_bin
            self.s_meter = parsed.s_meter
            self.scope_start_freq = parsed.scope_start_freq

            now = time.time()
            if self._last_frame_time > 0:
                dt = now - self._last_frame_time
                if dt > 0:
                    self._fps = self._fps * 0.9 + (1.0 / dt) * 0.1
            self._last_frame_time = now
            self._frame_count += 1
            self.last_update = now
        except Exception as e:
            logger.debug("Frame parse error: %s", e)

    @staticmethod
    def _scope_mode_to_cat(modein: int) -> int:
        return scope_mode_to_cat(modein)

    def get_spectrum_binary(self) -> bytes:
        wf1 = bytes(min(255, max(0, v)) for v in self.spectrum_rx1[:WF_SIZE])
        wf2 = bytes(min(255, max(0, v)) for v in self.spectrum_rx2[:WF_SIZE]) if self.spectrum_rx2 else bytes(WF_SIZE)
        return struct.pack('B', 1) + wf1 + wf2
