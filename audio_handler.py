"""
FT-710 Audio Handler
====================
Captures RX audio from the FT-710's USB audio device and streams it
via Opus to the browser. Receives TX audio from the browser and plays
it to the FT-710's USB audio device.

Uses PyAudio for sound card I/O and libopus for compression.
"""

import asyncio
import logging
import sys
import threading
import time
from collections import deque
from typing import Optional

import numpy as np

from opus_rx import RxOpusEncoder, AUDIO_TAG_PCM, AUDIO_TAG_OPUS, DEFAULT_BITRATE, RX_RATE
from audio_resample import resample_441_to_48, resample_48_to_441, resample_pcm

logger = logging.getLogger("ft710.audio")

# ── PyAudio ────────────────────────────────────────────────────────────
try:
    import pyaudio
    HAS_PYAUDIO = True
except ImportError:
    HAS_PYAUDIO = False
    pyaudio = None
    logger.warning("PyAudio not available — audio disabled. Install: pip install pyaudio")

# ── Audio Config ───────────────────────────────────────────────────────
RX_SAMPLE_RATE = 44100       # FT-710 native USB audio rate
RX_CHUNK_SIZE = 882          # 20 ms @ 44.1 kHz (= 960 samples after resampling to 48k)
RX_CHANNELS = 1
TX_SAMPLE_RATE = 44100       # FT-710 native USB audio rate
TX_CHANNELS = 1

# Generic enumeration names of the FT-710's built-in USB sound card on
# Windows — the exact string varies by driver/OS build ("USB Audio CODEC"
# or "USB Audio Device") and may be localized ("麦克风 (USB Audio Device)")
# or prefixed ("2- USB Audio CODEC"), so match case-insensitive substrings.
# "USB Audio" is the common prefix of both forms.
USB_AUDIO_NAME_HINTS = ('usb audio codec', 'usb audio device')

# ── TX jitter-buffer config ────────────────────────────────────────────
# The browser delivers one 20 ms Opus frame per ~20 ms over WebSocket, but
# network jitter (especially mobile/Wi-Fi power-save) makes arrivals bursty.
# Pre-buffering cushions the DAC before the first write (prevents underrun
# clicks at PTT key-up); the hard cap bounds accumulated latency by dropping
# the oldest queued frame.  Linear-interp resampling at the exact 160:147
# ratio keeps 960→882 frame boundaries phase-continuous, so no per-frame
# state is needed in the resampler itself.
TX_FRAME_MS = 20
TX_PREBUFFER_MS = 60          # ~3 frames cushion before playback starts
TX_MAX_BUFFER_MS = 400        # drop oldest beyond this (~20 frames)
TX_DRAIN_MS = 50              # bounded drain on PTT release (word-tail flush)
TX_PREBUFFER_BYTES = TX_SAMPLE_RATE * TX_CHANNELS * 2 * TX_PREBUFFER_MS // 1000
TX_MAX_BUFFER_BYTES = TX_SAMPLE_RATE * TX_CHANNELS * 2 * TX_MAX_BUFFER_MS // 1000


class AudioHandler:
    """Captures RX audio from sound card and encodes via Opus.
    Receives TX audio (decoded PCM) and plays to sound card."""

    # Class-level defaults: test-built instances skip __init__ but the TX
    # diagnostics counters must still exist for feed/write/tx_stats.
    _tx_written = 0
    _tx_write_err = 0
    _tx_peak = 0

    def __init__(self,
                 rx_device_index: Optional[int] = None,
                 tx_device_index: Optional[int] = None):
        self.rx_device = rx_device_index
        self.tx_device = tx_device_index
        self._pa: Optional["pyaudio.PyAudio"] = None
        self._rx_stream: Optional["pyaudio.Stream"] = None
        self._tx_stream: Optional["pyaudio.Stream"] = None
        self._rx_running = False
        self._tx_queue: deque = deque()  # Queue of int16 PCM bytes to play
        # TX playback state — guarded by _tx_lock so the drain loop (worker
        # thread), feed_tx_audio (event-loop thread) and stop_tx (worker
        # thread) never race on the stream handle or queue. _tx_write_lock
        # serializes actual stream.write() calls so the drain loop and a
        # graceful stop_tx never write the same PortAudio stream concurrently
        # (PortAudio blocking I/O is not thread-safe per stream).
        self._tx_lock = threading.Lock()
        self._tx_write_lock = threading.Lock()
        self._tx_queued_bytes = 0
        self._tx_primed = False
        # TX session diagnostics — reset in start_tx, read via tx_stats()
        # after stop_tx. Lets the server log exactly which link of the TX
        # chain carried audio (fed → queued → written → peak).
        self._tx_written = 0
        self._tx_write_err = 0
        self._tx_peak = 0

        # RX silence watchdog state: bit-exact zero chunks mean the radio's
        # USB audio is muted/wedged — a quiet band is never this silent.
        self._rx_zero_since: Optional[float] = None
        self._rx_last_peak = 0

        # Opus encoder for RX audio
        self.opus_enabled = True
        self.opus_encoder: Optional[RxOpusEncoder] = None
        try:
            self.opus_encoder = RxOpusEncoder(bitrate=DEFAULT_BITRATE)
        except Exception as e:
            logger.warning("Opus encoder unavailable, RX stays on Int16 PCM: %s", e)
            self.opus_enabled = False

        if HAS_PYAUDIO:
            self._init_pyaudio()

    def _init_pyaudio(self):
        """Initialize PyAudio and list available devices."""
        try:
            self._pa = pyaudio.PyAudio()
            logger.info("PyAudio initialized. Available devices:")
            for i in range(self._pa.get_device_count()):
                info = self._pa.get_device_info_by_index(i)
                logger.info(
                    "  [%d] %s (in=%d, out=%d, rate=%d)",
                    i, info['name'],
                    info.get('maxInputChannels', 0),
                    info.get('maxOutputChannels', 0),
                    int(info.get('defaultSampleRate', 0)),
                )
        except Exception as e:
            logger.error("PyAudio init failed: %s", e)
            self._pa = None

    def _reinit_pyaudio(self):
        """Tear down and re-create the PortAudio context.

        A USB re-enumeration — e.g. every radio power-off/on, where the
        FT-710's whole USB hub (sound card included) drops and re-enumerates
        — invalidates the CoreAudio device IDs cached inside PortAudio at
        Pa_Initialize time.  Every subsequent stream open then fails with
        -9999 (paUnanticipatedHostError) until PortAudio is re-initialized.
        Device indices are NOT stable across re-enumeration, so callers
        must re-run _find_*_device afterwards.
        """
        try:
            if self._pa is not None:
                self._pa.terminate()
        except Exception:
            pass
        self._pa = None
        self._init_pyaudio()

    # ── RX: Capture from sound card → Opus frames ──────────────────

    def _find_rx_device(self) -> Optional[int]:
        """Find a suitable input device.

        Priority:
        1. Explicit device index/name from config (env FT710_AUDIO_RX_DEVICE)
        2. Name match: "FT-710", "FT710", "YAESU"
        3. Name match: USB_AUDIO_NAME_HINTS — the FT-710's built-in USB
           sound card enumerates under a generic name on Windows ("USB
           Audio CODEC" or "USB Audio Device", no "FT-710" in it);
           localized Windows wraps it ("麦克风 (USB Audio Device)") and
           PortAudio may prefix it ("2- USB Audio CODEC")
        4. FT-710 heuristics: single input channel (in=1) — FT-710 USB audio
           has one mono RX input, unlike stereo USB mics (in=2)
        5. Fallback: first device with any input channels
        """
        if self.rx_device is not None:
            return self.rx_device
        if self._pa is None:
            return None

        from config import AUDIO_RX_DEVICE
        if AUDIO_RX_DEVICE:
            # Try as integer index first
            try:
                idx = int(AUDIO_RX_DEVICE)
                if 0 <= idx < self._pa.get_device_count():
                    info = self._pa.get_device_info_by_index(idx)
                    if info.get('maxInputChannels', 0) > 0:
                        logger.info("Using configured audio input: [%d] %s", idx, info.get('name', ''))
                        return idx
                    logger.warning("Configured device [%d] has no input channels", idx)
            except ValueError:
                pass
            # Try as name substring
            for i in range(self._pa.get_device_count()):
                info = self._pa.get_device_info_by_index(i)
                if info.get('maxInputChannels', 0) > 0:
                    if AUDIO_RX_DEVICE.lower() in info.get('name', '').lower():
                        logger.info("Found configured audio input: [%d] %s", i, info.get('name', ''))
                        return i
            logger.warning("Configured audio device '%s' not found", AUDIO_RX_DEVICE)

        # Try to find a device with "FT-710" or "YAESU" in the name
        for i in range(self._pa.get_device_count()):
            info = self._pa.get_device_info_by_index(i)
            if info.get('maxInputChannels', 0) > 0:
                name = info.get('name', '')
                if 'FT-710' in name or 'FT710' in name or 'YAESU' in name.upper():
                    logger.info("Found FT-710 audio input: [%d] %s", i, name)
                    return i

        # The FT-710's built-in USB sound card enumerates on Windows under a
        # generic name (USB_AUDIO_NAME_HINTS). The same physical device
        # usually appears once per host API (MME/DirectSound/WASAPI); every
        # entry opens the same hardware, so the first match is safe. When
        # several *distinct* codec devices exist (e.g. an external digimode
        # interface), warn and let the operator lock the choice via
        # FT710_AUDIO_RX_DEVICE.
        codec_matches = []  # (index, name, has_output)
        for i in range(self._pa.get_device_count()):
            info = self._pa.get_device_info_by_index(i)
            if info.get('maxInputChannels', 0) > 0:
                name = info.get('name', '')
                if any(h in name.lower() for h in USB_AUDIO_NAME_HINTS):
                    codec_matches.append(
                        (i, name, info.get('maxOutputChannels', 0) > 0))
        if codec_matches:
            # When several distinct codec devices exist, prefer the
            # full-duplex one: the FT-710 USB sound card exposes both input
            # and output, while a codec-named interloper (headset/interface)
            # is often input-only. First-index ordering would otherwise route
            # RX to the wrong device. Same-hardware duplicates across host
            # APIs share duplex-ness, so first match still wins among them.
            full_duplex = [m for m in codec_matches if m[2]]
            idx, name, _ = (full_duplex or codec_matches)[0]
            logger.info("Using USB audio input (FT-710 built-in sound card): [%d] %s",
                        idx, name)
            if len(codec_matches) > 1:
                logger.warning(
                    "Multiple USB audio inputs: %s — using [%d]. If RX "
                    "picks the wrong one, set FT710_AUDIO_RX_DEVICE to the index "
                    "from the startup device list.",
                    ', '.join(f"[{i}] {n}" for i, n, _ in codec_matches), idx)
            return idx

        # Heuristic: FT-710 USB audio has exactly 1 input channel (mono RX)
        # Most other "USB Audio CODEC" devices have 2 (stereo). Prefer mono.
        mono_candidates = []
        for i in range(self._pa.get_device_count()):
            info = self._pa.get_device_info_by_index(i)
            channels = info.get('maxInputChannels', 0)
            if channels == 1:
                mono_candidates.append((i, info.get('name', '')))
        if mono_candidates:
            idx, name = mono_candidates[0]
            logger.info("Using mono audio input (likely FT-710): [%d] %s", idx, name)
            if len(mono_candidates) > 1:
                logger.info("  Other mono candidates: %s",
                            ', '.join(f"[{i}] {n}" for i, n in mono_candidates[1:]))
            return idx

        # Fallback: first device with input channels
        for i in range(self._pa.get_device_count()):
            info = self._pa.get_device_info_by_index(i)
            if info.get('maxInputChannels', 0) > 0:
                logger.info("Using default audio input: [%d] %s", i, info.get('name', ''))
                return i
        return None

    def start_rx(self) -> bool:
        """Start capturing RX audio from the sound card."""
        if self._rx_running:
            return True
        if self._pa is None:
            logger.warning("PyAudio not available — cannot start RX")
            return False

        dev = self._find_rx_device()
        if dev is None:
            logger.warning("No audio input device found")
            return False

        for reinit_round in range(2):
            try:
                self._rx_stream = self._pa.open(
                    format=pyaudio.paInt16,
                    channels=RX_CHANNELS,
                    rate=RX_SAMPLE_RATE,
                    input=True,
                    input_device_index=dev,
                    frames_per_buffer=RX_CHUNK_SIZE,
                    stream_callback=None,  # We'll read in the asyncio loop
                )
                self._rx_running = True
                dev_info = self._pa.get_device_info_by_index(dev)
                logger.info("RX audio started: [%d] %s @ %d Hz (resampled to 48k for Opus)",
                            dev, dev_info.get('name', ''), RX_SAMPLE_RATE)
                return True
            except Exception as e:
                if reinit_round == 0:
                    # A USB re-enumeration (radio power cycle) invalidates
                    # the device IDs PortAudio cached at Pa_Initialize time;
                    # every open then fails (macOS: -9999) until re-init.
                    logger.warning("RX open failed (%s) — re-initializing PortAudio", e)
                    self._reinit_pyaudio()
                    if self._pa is None:
                        break
                    dev = self._find_rx_device()
                    if dev is None:
                        logger.error("No audio input device after PortAudio re-init")
                        break
                else:
                    logger.error("Failed to start RX audio: %s", e)
        return False

    def stop_rx(self):
        """Stop RX audio capture."""
        self._rx_running = False
        if self._rx_stream:
            try:
                self._rx_stream.stop_stream()
                self._rx_stream.close()
            except Exception:
                pass
            self._rx_stream = None

    def restart_rx(self):
        """Close and reopen the RX capture stream (Windows full-duplex fix).

        Windows MME/DirectSound quirk with the FT-710's C-Media USB codec:
        opening a playback stream on the same device for TX (start_tx)
        silently wedges the capture side — the RX stream stays open and
        error-free but delivers silence.  macOS CoreAudio is unaffected
        (and must not pay the reopen cost per PTT), so this is a no-op
        off Windows.  Called after every TX→RX transition.  Blocks on
        PyAudio calls — run via asyncio.to_thread.
        """
        import sys as _sys
        if _sys.platform != "win32":
            return
        if self._pa is None or not self._rx_running:
            return
        logger.info("RX audio: reopening capture stream after TX (Windows full-duplex workaround)")
        try:
            self.stop_rx()
        except Exception:
            pass
        time.sleep(0.05)
        if not self.start_rx():
            logger.warning("RX audio restart failed — capture stays down")

    def read_rx_chunk(self) -> Optional[bytes]:
        """Read one chunk of int16 PCM audio from the RX stream.
        Non-blocking — uses get_read_available() to check before read.
        Returns None if not enough data is available."""
        if not self._rx_running or self._rx_stream is None:
            return None
        try:
            # Use get_read_available to check, then read what's there
            count = self._rx_stream.get_read_available()
            if count >= RX_CHUNK_SIZE:
                # Read one chunk's worth, or at most 4 chunks to prevent lag
                to_read = min(count, RX_CHUNK_SIZE * 4)
                data = self._rx_stream.read(to_read, exception_on_overflow=False)
                if data and len(data) >= 2:
                    return resample_441_to_48(data)
        except OSError as e:
            if "Stream not open" in str(e) or "No such device" in str(e):
                logger.warning("RX stream lost — attempting restart")
                self._rx_running = False
                try: self.stop_rx()
                except Exception: pass
                time.sleep(0.5)
                self.start_rx()
            else:
                logger.debug("RX read error: %s", e)
        except Exception as e:
            logger.debug("RX read error: %s", e)
        return None

    def note_rx_chunk(self, pcm: Optional[bytes]):
        """Feed the RX-silence watchdog: record the chunk peak and the start
        of any continuous all-zero run. Called by _audio_rx_loop in server.py.
        Bit-exact zeros mean the radio's USB audio is muted/wedged — even a
        quiet band produces converter noise, never a perfect zero stream."""
        peak = 0
        if pcm:
            arr = np.frombuffer(pcm, dtype=np.int16)
            if arr.size:
                peak = int(np.abs(arr.astype(np.int32)).max())
        self._rx_last_peak = peak
        if peak > 0:
            self._rx_zero_since = None
        elif self._rx_zero_since is None:
            self._rx_zero_since = time.monotonic()

    def rx_silent_seconds(self) -> float:
        """Seconds of continuous bit-exact-zero RX audio (0.0 = not silent)."""
        if self._rx_zero_since is None:
            return 0.0
        return time.monotonic() - self._rx_zero_since

    def encode_rx_audio(self, pcm: bytes) -> list[bytes]:
        """Encode RX PCM audio into tagged WebSocket frames.
        Returns list of bytes, each prefixed with codec tag byte."""
        if len(pcm) < 64:
            return []

        frames: list[bytes] = []
        if self.opus_enabled and self.opus_encoder is not None:
            try:
                for pkt in self.opus_encoder.push(pcm):
                    frames.append(bytes([AUDIO_TAG_OPUS]) + pkt)
            except Exception as e:
                logger.warning("Opus encode failed, sending PCM: %s", e)
                frames = [bytes([AUDIO_TAG_PCM]) + pcm]
        else:
            frames = [bytes([AUDIO_TAG_PCM]) + pcm]
        return frames

    # ── TX: Receive from browser → play to sound card ───────────────

    def _find_tx_device(self) -> Optional[int]:
        """Find a suitable output device.

        Priority:
        1. Explicit device from config (env FT710_AUDIO_TX_DEVICE)
        2. Name match: "FT-710", "FT710", "YAESU"
        3. Name match: USB_AUDIO_NAME_HINTS — the FT-710's built-in USB
           sound card enumerates under a generic name on Windows ("USB
           Audio CODEC" or "USB Audio Device")
        4. Heuristic: device with both input AND output (full-duplex USB audio)
        5. Fallback: system default output
        """
        if self.tx_device is not None:
            return self.tx_device
        if self._pa is None:
            return None

        from config import AUDIO_TX_DEVICE
        if AUDIO_TX_DEVICE:
            try:
                idx = int(AUDIO_TX_DEVICE)
                if 0 <= idx < self._pa.get_device_count():
                    info = self._pa.get_device_info_by_index(idx)
                    if info.get('maxOutputChannels', 0) > 0:
                        logger.info("Using configured audio output: [%d] %s", idx, info.get('name', ''))
                        return idx
            except ValueError:
                for i in range(self._pa.get_device_count()):
                    info = self._pa.get_device_info_by_index(i)
                    if info.get('maxOutputChannels', 0) > 0:
                        if AUDIO_TX_DEVICE.lower() in info.get('name', '').lower():
                            logger.info("Found configured audio output: [%d] %s", i, info.get('name', ''))
                            return i

        # Try FT-710 USB audio output by name
        for i in range(self._pa.get_device_count()):
            info = self._pa.get_device_info_by_index(i)
            if info.get('maxOutputChannels', 0) > 0:
                name = info.get('name', '')
                if 'FT-710' in name or 'FT710' in name or 'YAESU' in name.upper():
                    logger.info("Found FT-710 audio output: [%d] %s", i, name)
                    return i

        # The FT-710's built-in USB sound card enumerates on Windows under a
        # generic name (USB_AUDIO_NAME_HINTS; see _find_rx_device). Prefer
        # it over the full-duplex heuristic, which can grab a random sound
        # card and pipe TX modulation to the PC speakers instead of the radio.
        codec_matches = []  # (index, name, has_input)
        for i in range(self._pa.get_device_count()):
            info = self._pa.get_device_info_by_index(i)
            if info.get('maxOutputChannels', 0) > 0:
                name = info.get('name', '')
                if any(h in name.lower() for h in USB_AUDIO_NAME_HINTS):
                    codec_matches.append(
                        (i, name, info.get('maxInputChannels', 0) > 0))
        if codec_matches:
            # When several distinct codec devices exist, prefer the
            # full-duplex one: the FT-710 USB sound card exposes both input
            # and output, while a codec-named interloper (headset/interface)
            # is often output-only. First-index ordering would otherwise send
            # TX modulation to the wrong device. Same-hardware duplicates
            # across host APIs share duplex-ness, so first match still wins
            # among them.
            full_duplex = [m for m in codec_matches if m[2]]
            idx, name, _ = (full_duplex or codec_matches)[0]
            logger.info("Using USB audio output (FT-710 built-in sound card): [%d] %s",
                        idx, name)
            if len(codec_matches) > 1:
                logger.warning(
                    "Multiple USB audio outputs: %s — using [%d]. If TX "
                    "plays through the wrong device, set FT710_AUDIO_TX_DEVICE "
                    "to the index from the startup device list.",
                    ', '.join(f"[{i}] {n}" for i, n, _ in codec_matches), idx)
            return idx

        # Heuristic: prefer a device that has BOTH input and output
        # (full-duplex USB audio like FT-710)
        for i in range(self._pa.get_device_count()):
            info = self._pa.get_device_info_by_index(i)
            if info.get('maxOutputChannels', 0) > 0 and info.get('maxInputChannels', 0) > 0:
                logger.info("Using full-duplex audio output: [%d] %s", i, info.get('name', ''))
                return i

        # Fallback: system default output
        try:
            default = self._pa.get_default_output_device_info()
            if default:
                logger.info("Using default audio output: [%d] %s",
                           default.get('index'), default.get('name', ''))
                return default.get('index')
        except Exception:
            pass
        return None

    def _wasapi_tx_variant(self, dev: int):
        """Windows: find the WASAPI entry of the same output device and its
        native mix rate.  Returns (index, rate) or None.

        Same-physical-codec entries share the device name across host
        APIs (MME/DirectSound/WASAPI); the WASAPI entry's
        defaultSampleRate is the audio engine's native mix rate for the
        codec (48 kHz on the FT-710's C-Media).
        """
        try:
            chosen_name = self._pa.get_device_info_by_index(dev).get('name', '')
            for i in range(self._pa.get_device_count()):
                info = self._pa.get_device_info_by_index(i)
                if info.get('maxOutputChannels', 0) < 1:
                    continue
                if info.get('name', '') != chosen_name:
                    continue
                api = self._pa.get_host_api_info_by_index(info.get('hostApi', 0))
                if 'WASAPI' not in api.get('name', '').upper():
                    continue
                rate = int(info.get('defaultSampleRate', 0))
                if rate > 0:
                    return (i, rate)
        except Exception:
            pass
        return None

    def start_tx(self) -> bool:
        """Start TX audio playback stream.

        On macOS CoreAudio, opening a second half-duplex stream on the same
        USB Audio Device that already has an RX input stream can fail with
        kAudioUnitErr_InvalidPropertyValue (-10851).  We retry up to
        START_TX_RETRIES times with a staggered back-off between attempts.

        Idempotent: if a TX stream is already active, returns True
        immediately without clearing the queue or re-opening the device,
        so duplicate PTT commands never discard in-flight voice audio.
        """
        if self._pa is None:
            return False

        # Already transmitting — don't tear down a working stream.
        with self._tx_lock:
            if self._tx_stream is not None:
                try:
                    if self._tx_stream.is_active():
                        return True
                except Exception:
                    pass  # stream handle is stale; proceed to open a new one

        dev = self._find_tx_device()
        if dev is None:
            logger.warning("No audio output device found for TX")
            return False

        # Windows: prefer the WASAPI entry of the same codec at its native
        # mix rate (48 kHz).  The C-Media MME 44.1 kHz path paces ~1.4x
        # slow, starving the drain loop and crackling TX audio (V2.9
        # field report); WASAPI@48k paces correctly and needs no resample.
        rate = TX_SAMPLE_RATE
        if sys.platform == "win32":
            variant = self._wasapi_tx_variant(dev)
            if variant is not None:
                dev, rate = variant
        self._tx_rate = rate
        self._tx_prebuffer_bytes = rate * TX_CHANNELS * 2 * TX_PREBUFFER_MS // 1000
        self._tx_max_buffer_bytes = rate * TX_CHANNELS * 2 * TX_MAX_BUFFER_MS // 1000

        # If a TX stream is already active, close it now so CoreAudio can
        # release the device before we try to open a new one.  This also
        # serializes rapid PTT toggles that otherwise race at the AUHAL level.
        with self._tx_lock:
            stale = self._tx_stream
            self._tx_stream = None
            self._tx_queue.clear()
            self._tx_queued_bytes = 0
            self._tx_primed = False
        if stale is not None:
            with self._tx_write_lock:
                try:
                    stale.stop_stream()
                    stale.close()
                except Exception:
                    pass
            # macOS CoreAudio may need a tick to fully release the device.
            time.sleep(0.05)

        # Retry loop: PortAudio on macOS sometimes fails to open a second
        # half-duplex stream on a full-duplex device when the RX stream is
        # already running.  Staggered back-off (100/200/350 ms) gives the
        # AUHAL time to settle between attempts.  If all attempts fail, the
        # likely cause is a USB re-enumeration (radio power cycle) that
        # invalidated the device IDs cached at Pa_Initialize time — every
        # open then fails with -9999.  Re-initialize PortAudio once and
        # retry with a freshly resolved device index.
        START_TX_RETRIES = 3
        _RETRY_DELAYS = (0.100, 0.200, 0.350)
        last_error = None
        for reinit_round in range(2):
            for attempt in range(START_TX_RETRIES):
                try:
                    stream = self._pa.open(
                        format=pyaudio.paInt16,
                        channels=TX_CHANNELS,
                        rate=rate,
                        output=True,
                        output_device_index=dev,
                        frames_per_buffer=RX_CHUNK_SIZE,
                    )
                except Exception as e:
                    last_error = e
                    if attempt < START_TX_RETRIES - 1:
                        time.sleep(_RETRY_DELAYS[attempt])
                    continue

                # Success — atomically install the new stream.
                with self._tx_lock:
                    old = self._tx_stream
                    self._tx_stream = stream
                    self._tx_queue.clear()
                    self._tx_queued_bytes = 0
                    self._tx_primed = False
                    self._tx_written = 0
                    self._tx_write_err = 0
                    self._tx_peak = 0
                # Reset diagnostic one-shot flags for this TX session.
                for _k in ('_dbg_no_pcm', '_dbg_no_resample', '_dbg_no_stream',
                            '_dbg_no_stream_w', '_dbg_not_primed',
                            '_dbg_first_feed', '_dbg_first_write', '_dbg_write_err'):
                    setattr(self, _k, False)

                if old is not None:
                    with self._tx_write_lock:
                        try:
                            old.stop_stream()
                            old.close()
                        except Exception:
                            pass

                dev_info = self._pa.get_device_info_by_index(dev)
                logger.info("TX audio started: [%d] %s @ %d Hz (pre-buffer %dms, cap %dms)%s",
                            dev, dev_info.get('name', ''), rate,
                            TX_PREBUFFER_MS, TX_MAX_BUFFER_MS,
                            f" (attempt {attempt + 1})" if attempt > 0 else "")
                return True

            if reinit_round == 0:
                logger.warning("TX open failed x%d (%s) — re-initializing PortAudio "
                               "(USB re-enumeration?)", START_TX_RETRIES, last_error)
                self._reinit_pyaudio()
                if self._pa is None:
                    break
                dev = self._find_tx_device()
                if dev is None:
                    logger.error("No audio output device after PortAudio re-init")
                    break
                if sys.platform == "win32":
                    variant = self._wasapi_tx_variant(dev)
                    if variant is not None:
                        dev, rate = variant

        logger.error("Failed to start TX audio after %d attempts: %s",
                     START_TX_RETRIES * 2, last_error)
        return False

    def stop_tx(self, graceful: bool = False, drain_ms: int = TX_DRAIN_MS):
        """Stop TX audio playback.

        graceful=False (default — disconnect / teardown / safety):
            Drop all queued audio and close immediately. PortAudio's
            stop_stream() still drains whatever is already in the device
            buffer (≤ ~60 ms) before close, avoiding a click.

        graceful=True (normal PTT release):
            Write remaining queued frames to the device (bounded by
            drain_ms), then call stop_stream() — which BLOCKS until the
            device buffer finishes playing to the DAC (Pa_StopStream
            semantics) — so in-flight word-endings actually go out over RF
            before the caller drops PTT. The caller MUST invoke this before
            set_ptt(False), and MUST run it off the event loop (to_thread),
            since the blocking drain can take up to drain_ms.

        Both paths take _tx_write_lock so the drain loop can't write the
        stream mid-close.
        """
        # Take ownership of the stream + queue atomically. After this, the
        # drain loop's write_tx_chunk sees _tx_stream=None and stops.
        with self._tx_lock:
            stream = self._tx_stream
            queue = self._tx_queue
            self._tx_stream = None
            self._tx_queue = deque()
            self._tx_queued_bytes = 0
            self._tx_primed = False

        if stream is None:
            return

        with self._tx_write_lock:  # wait for any in-flight drain-loop write
            if graceful and stream.is_active():
                rate = getattr(self, '_tx_rate', TX_SAMPLE_RATE)
                budget = rate * TX_CHANNELS * 2 * drain_ms // 1000
                flushed = 0
                while queue and flushed < budget:
                    data = queue.popleft()
                    try:
                        stream.write(data)  # blocks when device buffer full
                        flushed += len(data)
                    except Exception as e:
                        logger.debug("TX graceful drain write error: %s", e)
                        break
            # Pa_StopStream blocks until pending device-buffer audio has
            # played; this is what guarantees the tail reaches the DAC.
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
            # Give macOS CoreAudio a tick to fully release the device before
            # the next start_tx call can open a new stream on it (avoids
            # kAudioUnitErr_InvalidPropertyValue on rapid PTT toggles).
            time.sleep(0.02)

    def feed_tx_audio(self, pcm: bytes):
        """Queue TX audio for playback. Input is 48 kHz Int16 PCM (from Opus
        decoder or browser); resampled to 44.1 kHz for FT-710 native USB
        audio. Enforces a hard queue-depth cap (drops oldest) to bound
        latency under network jitter bursts.
        """
        if not pcm or len(pcm) < 2:
            return
        rate = getattr(self, '_tx_rate', TX_SAMPLE_RATE)
        if rate == RX_RATE:
            data = pcm  # 48 kHz stream (e.g. Windows WASAPI) — no resample
        else:
            data = resample_pcm(pcm, RX_RATE, rate)
        if not data:
            return
        with self._tx_lock:
            if self._tx_stream is None:
                return
            # Session peak (for the TX-session diagnostic log) — one cheap
            # numpy pass over the resampled frame.
            peak = int(np.abs(np.frombuffer(data, dtype=np.int16)).max())
            if peak > self._tx_peak:
                self._tx_peak = peak
            self._tx_queue.append(data)
            self._tx_queued_bytes += len(data)
            # Cap: drop oldest frames until under the limit (keep at least one
            # so a single oversized frame still plays).
            max_bytes = getattr(self, '_tx_max_buffer_bytes', TX_MAX_BUFFER_BYTES)
            while (self._tx_queued_bytes > max_bytes
                   and len(self._tx_queue) > 1):
                self._tx_queued_bytes -= len(self._tx_queue.popleft())

    def write_tx_chunk(self):
        """Write queued TX audio to the output stream.

        Pre-buffers TX_PREBUFFER_MS before the first write to cushion
        WebSocket jitter, then drains the queue one frame at a time.
        PyAudio's blocking write() self-rate-limits to the DAC clock, so
        this never outruns the device. Called from the asyncio event loop
        via a worker thread (see _audio_tx_drain_loop in server.py).

        The queue pop is under _tx_lock (brief); the blocking write is under
        _tx_write_lock so feed_tx_audio (event loop) is never blocked by a
        slow write, while a concurrent graceful stop_tx stays serialized.
        """
        while True:
            with self._tx_lock:
                stream = self._tx_stream
                if stream is None or not stream.is_active():
                    return
                if not self._tx_primed:
                    prebuffer = getattr(self, '_tx_prebuffer_bytes', TX_PREBUFFER_BYTES)
                    if self._tx_queued_bytes < prebuffer:
                        return  # build cushion before first write
                    self._tx_primed = True
                if not self._tx_queue:
                    return
                data = self._tx_queue.popleft()
                self._tx_queued_bytes -= len(data)
            # stop_tx / start_tx may have swapped out this stream while we
            # waited for the write lock; if so, don't write to a stale/
            # closed stream. (Visibility is fine under the GIL; the swap
            # happens under _tx_lock with a release before contending here.)
            with self._tx_write_lock:
                if self._tx_stream is not stream:
                    return
                try:
                    stream.write(data)
                    with self._tx_lock:
                        self._tx_written += 1
                except Exception as e:
                    with self._tx_lock:
                        self._tx_write_err += 1
                    if not getattr(self, '_dbg_write_err', False):
                        self._dbg_write_err = True
                        logger.warning(
                            "[TX-AUDIO] write error (counted, logged once per "
                            "TX session): %s", e)
                    return

    def tx_stats(self) -> dict:
        """TX-session diagnostics for the server to log on PTT release."""
        with self._tx_lock:
            return {
                "written": self._tx_written,
                "write_err": self._tx_write_err,
                "peak": self._tx_peak,
                "queued": len(self._tx_queue),
            }

    def has_pending_tx_audio(self) -> bool:
        """Return True when the TX drain loop has useful work to do."""
        with self._tx_lock:
            stream = self._tx_stream
            if stream is None:
                return False
            try:
                return bool(stream.is_active())
            except Exception:
                return False

    # ── Cleanup ─────────────────────────────────────────────────────

    def close(self):
        """Stop all streams and release PyAudio."""
        self.stop_rx()
        self.stop_tx()
        if self.opus_encoder:
            try:
                self.opus_encoder.close()
            except Exception:
                pass
            self.opus_encoder = None
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None
        logger.info("Audio handler closed")
