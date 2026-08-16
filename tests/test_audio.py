"""
Tests for audio_handler.py and opus_rx.py — SDD AD-004 (tagged dual-codec audio).
Verifies: codec tag constants, encoder/decoder lifecycle, PCM framing,
audio device name matching logic.
"""
import re
import struct
import unittest
from pathlib import Path

from opus_rx import (
    AUDIO_TAG_PCM,
    AUDIO_TAG_OPUS,
    RX_RATE,
    FRAME_SAMPLES,
    DEFAULT_BITRATE,
    MIN_BITRATE,
    MAX_BITRATE,
    _libopus_candidates,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class CodecTagTests(unittest.TestCase):
    """SDD AD-004: 1-byte codec tag per frame."""

    def test_tags_are_distinct(self):
        self.assertNotEqual(AUDIO_TAG_PCM, AUDIO_TAG_OPUS)

    def test_pcm_tag_is_zero(self):
        self.assertEqual(AUDIO_TAG_PCM, 0x00)

    def test_opus_tag_is_one(self):
        self.assertEqual(AUDIO_TAG_OPUS, 0x01)

    def test_tag_fits_in_one_byte(self):
        self.assertLess(AUDIO_TAG_PCM, 256)
        self.assertLess(AUDIO_TAG_OPUS, 256)


class OpusConstantsTests(unittest.TestCase):
    """SDD NFR-060, NFR-061: Opus codec configuration."""

    def test_rx_rate_is_48khz(self):
        self.assertEqual(RX_RATE, 48000)

    def test_frame_samples_20ms_at_48khz(self):
        self.assertEqual(FRAME_SAMPLES, 960)

    def test_default_bitrate_is_64kbps(self):
        self.assertEqual(DEFAULT_BITRATE, 64000)

    def test_min_bitrate_is_8kbps(self):
        self.assertEqual(MIN_BITRATE, 8000)

    def test_max_bitrate_is_128kbps(self):
        self.assertEqual(MAX_BITRATE, 128000)

    def test_bitrate_range(self):
        self.assertLess(MIN_BITRATE, DEFAULT_BITRATE)
        self.assertLess(DEFAULT_BITRATE, MAX_BITRATE)

    def test_windows_packaged_opus_dll_candidates_are_checked(self):
        candidates = [
            str(path)
            for path in _libopus_candidates(
                platform="win32",
                resource_roots=[Path(r"C:\Program Files\MRRC FT-710")],
                find_library_result=None,
            )
        ]
        self.assertIn(r"C:\Program Files\MRRC FT-710\opus.dll", candidates)
        self.assertIn(
            r"C:\Program Files\MRRC FT-710\_internal\opus.dll",
            candidates,
        )
        self.assertIn(
            r"C:\Program Files\MRRC FT-710\vendor\opus\windows\bin\x64\opus.dll",
            candidates,
        )


class TxFrontendContractTests(unittest.TestCase):
    """TX browser path must preserve the 48 kHz / 20 ms Opus contract."""

    def test_tx_capture_worklet_does_not_downsample_microphone_audio(self):
        source = (REPO_ROOT / "static" / "tx_capture_worklet.js").read_text(encoding="utf-8")
        self.assertIn("this._outRate = 48000", source)
        self.assertNotIn("this._outRate = 16000", source)

    def test_tx_capture_worklet_resamples_actual_context_rate_to_48khz(self):
        source = (REPO_ROOT / "static" / "tx_capture_worklet.js").read_text(encoding="utf-8")
        self.assertIn("this._resampleStep = this._inRate / this._outRate", source)
        self.assertIn("_resampleTo48k", source)

    def test_tx_opus_worker_encodes_20ms_48khz_frames(self):
        source = (REPO_ROOT / "static" / "tx_opus_worker.js").read_text(encoding="utf-8")
        self.assertIn("var FRAME_SIZE = 960", source)
        self.assertIn("new OpusEncoder(48000, 1, 2048, 20)", source)

    def test_tx_opus_worker_updates_sab_read_pointer_with_atomic_index(self):
        source = (REPO_ROOT / "static" / "tx_opus_worker.js").read_text(encoding="utf-8")
        self.assertIn("Atomics.store(_readPtr, 0, rp + n)", source)
        self.assertNotIn("Atomics.store(_readPtr, rp + n)", source)

    def test_tx_main_prefers_audio_worklet_frame_capture(self):
        source = (REPO_ROOT / "static" / "ft710_main.js").read_text(encoding="utf-8")
        self.assertRegex(
            source,
            r"audioWorklet\.addModule\(\s*['\"]"
            r"/tx_capture_worklet\.js\?v=tx-audio-4['\"]\s*,?\s*\)",
        )
        self.assertIn("new AudioWorkletNode", source)
        self.assertRegex(source, r"type:\s*['\"]float_frame['\"]")

    def test_tx_start_creates_worker_before_start_command(self):
        """First PTT must not lose mic frames because the worker missed start."""
        source = (REPO_ROOT / "static" / "ft710_main.js").read_text(encoding="utf-8")
        start_fn = source[source.index("function startTXAudio()"):source.index("function startTXAudioFallback()")]
        ensure_idx = start_fn.index("ensureTXOpusWorker()")
        start_match = re.search(
            r"postMessage\(\s*\{\s*type:\s*['\"]start['\"]\s*\}",
            start_fn,
        )
        self.assertIsNotNone(start_match)
        start_idx = start_match.start()
        self.assertLess(ensure_idx, start_idx)

    def test_intentional_close_flag_is_mutable(self):
        """Power-off must not throw before stale TX sockets are closed."""
        source = (REPO_ROOT / "static" / "ft710_main.js").read_text(encoding="utf-8")
        self.assertRegex(source, r"\blet\s+_intentionalClose\s*=\s*false\b")
        self.assertNotRegex(source, r"\bconst\s+_intentionalClose\b")

    def test_tx_audio_send_uses_websocket_backpressure_guard(self):
        """TX audio should drop frames under network stall instead of queuing latency."""
        source = (REPO_ROOT / "static" / "ft710_main.js").read_text(encoding="utf-8")
        self.assertIn("TX_AUDIO_MAX_BUFFERED_BYTES", source)
        self.assertIn("wsAudioTX.bufferedAmount", source)
        self.assertIn("window.__txAudioDroppedFrames", source)

    def test_tx_static_assets_are_cache_busted(self):
        main_source = (REPO_ROOT / "static" / "ft710_main.js").read_text(encoding="utf-8")
        worker_source = (REPO_ROOT / "static" / "tx_opus_worker.js").read_text(encoding="utf-8")
        sw_source = (REPO_ROOT / "static" / "sw.js").read_text(encoding="utf-8")
        self.assertIn("tx_opus_worker.js?v=tx-audio-4", main_source)
        self.assertIn("tx_capture_worklet.js?v=tx-audio-4", main_source)
        self.assertIn("opus_codec.js?v=tx-audio-4", worker_source)
        self.assertIn("ft710-v24", sw_source)

    def test_tx_debug_tone_bypasses_microphone_capture(self):
        main_source = (REPO_ROOT / "static" / "ft710_main.js").read_text(encoding="utf-8")
        worker_source = (REPO_ROOT / "static" / "tx_opus_worker.js").read_text(encoding="utf-8")
        self.assertIn("window.TXDebug", main_source)
        self.assertRegex(main_source, r"type:\s*['\"]tone_start['\"]")
        self.assertIn("function startTone", worker_source)
        self.assertIn("Math.sin(_tonePhase)", worker_source)

    def test_tx_audio_drain_does_not_block_event_loop(self):
        source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("await asyncio.to_thread(audio.write_tx_chunk)", source)

    def test_tx_opus_encoder_uses_valid_high_quality_ctl_settings(self):
        source = (REPO_ROOT / "static" / "modules" / "opus_codec.js").read_text(encoding="utf-8")
        self.assertIn("setValue(bitrate_ptr, 64000", source)
        self.assertIn("_opus_encoder_ctl(this.handle, 4006, vbr_ptr)", source)
        self.assertNotIn("_opus_encoder_ctl(this.handle, 4004, vbr_ptr)", source)
        self.assertNotIn("_opus_encoder_ctl(this.handle, 4030", source)


class RxRecordingFrontendTests(unittest.TestCase):
    """RX recording must produce real MP3 via lamejs encoder."""

    def test_record_button_sits_next_to_tune(self):
        source = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        ptt_footer = source[source.index('<footer class="ptt-footer">'):source.index("</footer>", source.index('<footer class="ptt-footer">'))]
        self.assertIn('id="btn-tune"', ptt_footer)
        self.assertIn('id="btn-record"', ptt_footer)
        self.assertLess(ptt_footer.index('id="btn-tune"'), ptt_footer.index('id="btn-record"'))

    def test_recorder_uses_lamejs_for_real_mp3_encoding(self):
        source = (REPO_ROOT / "static" / "ft710_main.js").read_text(encoding="utf-8")
        self.assertIn("window.RXRecorder", source)
        self.assertIn("lamejs.Mp3Encoder", source)
        self.assertIn("new lamejs.Mp3Encoder", source)
        self.assertIn("encoder.encodeBuffer", source)
        self.assertIn("encoder.flush", source)
        self.assertIn("audio/mpeg", source)
        self.assertIn(".mp3", source)
        self.assertIn("_f32ToInt16", source)

    def test_lamejs_is_lazy_loaded_not_in_html(self):
        # lame.js (~500 KB) is intentionally NOT in index.html — the REC
        # feature lazy-loads it on first click via _loadLame().
        html_source = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("lame.js", html_source)
        main_source = (REPO_ROOT / "static" / "ft710_main.js").read_text(encoding="utf-8")
        self.assertIn("function _loadLame()", main_source)
        self.assertIn("/modules/lame.js", main_source)

    def test_decoded_rx_frames_feed_recorder(self):
        source = (REPO_ROOT / "static" / "ft710_main.js").read_text(encoding="utf-8")
        self.assertIn("function feedRXRecorderFrame(f32)", source)
        self.assertGreaterEqual(source.count("feedRXRecorderFrame("), 3)

    def test_record_button_click_is_bound_in_ui(self):
        source = (REPO_ROOT / "static" / "ft710_ui.js").read_text(encoding="utf-8")
        self.assertIn("const recordBtn = document.getElementById('btn-record')", source)
        self.assertIn("window.RXRecorder.toggle()", source)
        self.assertIn("record-active", source)


class TXBufferTests(unittest.TestCase):
    """Server-side TX jitter buffer: pre-buffer, hard cap, thread-safe drain."""

    def _make_handler(self):
        """Build an AudioHandler without running __init__ (avoids PyAudio/Opus).
        Only the TX-playback fields are wired, which is all feed/drain/write use.
        """
        import threading
        from collections import deque
        from audio_handler import AudioHandler
        h = AudioHandler.__new__(AudioHandler)
        h._tx_stream = None
        h._tx_queue = deque()
        h._tx_queued_bytes = 0
        h._tx_primed = False
        h._tx_lock = threading.Lock()
        h._tx_write_lock = threading.Lock()
        return h

    # One 20 ms Opus frame decodes to 960 Int16 samples @ 48 k = 1920 bytes,
    # which resamples to 882 samples @ 44.1 k = 1764 bytes (exact 160:147).
    _FRAME48 = b"\x00" * 1920
    _FRAME44_BYTES = 1764

    def test_feed_drops_oldest_beyond_cap(self):
        from audio_handler import TX_MAX_BUFFER_BYTES
        h = self._make_handler()
        h._tx_stream = object()  # non-None sentinel; feed only checks "is None"
        for _ in range(60):      # feed well past the cap
            h.feed_tx_audio(self._FRAME48)
        # Queue must stay bounded by the cap (allow one frame of slack).
        self.assertLessEqual(h._tx_queued_bytes, TX_MAX_BUFFER_BYTES + self._FRAME44_BYTES)
        self.assertGreater(len(h._tx_queue), 0)

    def test_feed_counts_oldest_frame_drops(self):
        """A slow Windows output stream must be visible in session stats."""
        from audio_handler import TX_MAX_BUFFER_BYTES
        h = self._make_handler()
        h._tx_stream = object()
        h._tx_max_buffer_bytes = self._FRAME44_BYTES * 2
        for _ in range(5):
            h.feed_tx_audio(self._FRAME48)
        self.assertEqual(h.tx_stats()["queue_drops"], 3)
        self.assertLessEqual(h._tx_queued_bytes, TX_MAX_BUFFER_BYTES)

    def test_feed_drops_when_stream_closed(self):
        h = self._make_handler()
        h._tx_stream = None
        h.feed_tx_audio(self._FRAME48)
        self.assertEqual(len(h._tx_queue), 0)
        self.assertEqual(h._tx_queued_bytes, 0)

    def test_write_prebuffers_before_first_write(self):
        from audio_handler import TX_PREBUFFER_BYTES
        h = self._make_handler()

        class FakeStream:
            def __init__(self):
                self.writes = []

            def is_active(self):
                return True

            def write(self, data):
                self.writes.append(data)

        stream = FakeStream()
        h._tx_stream = stream
        # One frame is below the pre-buffer threshold → no write yet.
        h.feed_tx_audio(self._FRAME48)
        h.write_tx_chunk()
        self.assertEqual(len(stream.writes), 0)
        self.assertFalse(h._tx_primed)
        # Feed past the threshold → primed and drained.
        for _ in range(10):
            h.feed_tx_audio(self._FRAME48)
        h.write_tx_chunk()
        self.assertTrue(h._tx_primed)
        self.assertGreater(len(stream.writes), 0)

    def _fake_stream(self):
        class FakeStream:
            def __init__(self):
                self.writes = []
                self.stopped = False
                self.closed = False

            def is_active(self):
                return not self.closed

            def write(self, data):
                self.writes.append(data)

            def stop_stream(self):
                self.stopped = True

            def close(self):
                self.closed = True
        return FakeStream()

    def test_graceful_stop_drains_queue_then_closes(self):
        h = self._make_handler()
        stream = self._fake_stream()
        h._tx_stream = stream
        for _ in range(3):
            h.feed_tx_audio(self._FRAME48)
        h._tx_primed = True
        h.stop_tx(graceful=True)
        self.assertEqual(len(stream.writes), 3)   # all queued frames → device
        self.assertTrue(stream.stopped)            # Pa_StopStream drained device
        self.assertTrue(stream.closed)
        self.assertIsNone(h._tx_stream)
        self.assertEqual(len(h._tx_queue), 0)

    def test_graceful_stop_bounds_drain(self):
        h = self._make_handler()
        stream = self._fake_stream()
        h._tx_stream = stream
        for _ in range(20):
            h.feed_tx_audio(self._FRAME48)
        h._tx_primed = True
        h.stop_tx(graceful=True, drain_ms=40)      # 40 ms = 2 frames
        self.assertEqual(len(stream.writes), 2)    # only 2 drained, rest dropped
        self.assertTrue(stream.closed)

    def test_force_stop_drops_queue(self):
        h = self._make_handler()
        stream = self._fake_stream()
        h._tx_stream = stream
        for _ in range(5):
            h.feed_tx_audio(self._FRAME48)
        h.stop_tx(graceful=False)
        self.assertEqual(len(stream.writes), 0)    # nothing drained (force)
        self.assertTrue(stream.closed)
        self.assertIsNone(h._tx_stream)

    def test_has_pending_tx_audio_is_false_when_no_stream(self):
        h = self._make_handler()
        self.assertFalse(h.has_pending_tx_audio())

    def test_has_pending_tx_audio_is_true_for_active_stream(self):
        h = self._make_handler()
        stream = self._fake_stream()
        h._tx_stream = stream
        self.assertTrue(h.has_pending_tx_audio())


class TXReleaseOrderTests(unittest.TestCase):
    """PTT release must drain queued audio before dropping RF."""

    def test_ptt_off_drains_before_rf_drop(self):
        source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        # Graceful stop_tx (drain) must precede set_ptt(False) *in the PTT-off
        # branch*.  There is also a set_ptt(False) in the start_tx error path
        # (PTT-on branch) — skip past it.
        drain_idx = source.index("audio.stop_tx, True")
        # The drain call is inside the PTT-off branch; the preceding
        # set_ptt(False) belongs to the error path in the PTT-on branch.
        # Search for the first set_ptt(False) *after* the drain call.
        ptoff_idx = source.index("await cat.set_ptt(False)", drain_idx)
        self.assertLess(drain_idx, ptoff_idx)

    def test_tx_has_single_owner_guard(self):
        source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("_tx_owner_ws", source)

    def test_stop_does_not_clear_queue_on_s_text(self):
        """'s:' must not clear the queue (would chop tail before drain)."""
        source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        # The 's:' branch should be a no-op pass, not a queue.clear()
        s_branch = source[source.index('"s:"'):]
        # No queue clear within the 's:'/'stop' text branch (first 400 chars)
        self.assertNotIn("_tx_queue.clear()", s_branch[:400])


class RXBackpressureTests(unittest.TestCase):
    """RX path should not let a slow audio client stall the whole broadcast loop."""

    def test_rx_loop_uses_timeout_guard_for_ws_send(self):
        source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("asyncio.wait_for(ws.send_bytes(frame)", source)

    def test_rx_loop_uses_shared_send_helper(self):
        source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("async def _send_audio_frames_to_clients(", source)
        self.assertIn("await _send_audio_frames_to_clients(", source)

    def test_rx_loop_skips_encode_when_no_clients(self):
        source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("if not audio_rx_clients:", source)
        self.assertIn("await asyncio.sleep(idle_interval)", source)
        self.assertIn("continue", source)



class AudioFrameFormatTests(unittest.TestCase):
    """SDD §9.3, §9.4: Audio frame format (1-byte tag + payload)."""

    def test_tagged_pcm_frame_starts_with_zero(self):
        """PCM frames: AUDIO_TAG_PCM (0x00) + Int16 PCM bytes."""
        pcm_data = struct.pack("<480h", *([0] * 480))  # 960 bytes
        tagged = bytes([AUDIO_TAG_PCM]) + pcm_data
        self.assertEqual(tagged[0], 0x00)
        self.assertEqual(len(tagged), 1 + 960)

    def test_tagged_opus_frame_starts_with_one(self):
        """Opus frames: AUDIO_TAG_OPUS (0x01) + Opus packet bytes."""
        opus_packet = b"\x00" * 80  # typical Opus frame ~40-80 bytes
        tagged = bytes([AUDIO_TAG_OPUS]) + opus_packet
        self.assertEqual(tagged[0], 0x01)
        self.assertEqual(len(tagged), 1 + 80)

    def test_pcm_frame_contains_even_byte_count(self):
        """Int16 PCM frames must have even byte count (2 bytes per sample)."""
        samples = [1000, -500, 32767, -32768]
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        self.assertEqual(len(pcm), len(samples) * 2)
        self.assertEqual(len(pcm) % 2, 0)

    def test_int16_range(self):
        """PCM samples must fit in int16 range."""
        for val in (-32768, -1, 0, 1, 32767):
            packed = struct.pack("<h", val)
            unpacked = struct.unpack("<h", packed)[0]
            self.assertEqual(unpacked, val)

    def test_multiple_frames_independent_tags(self):
        """Each frame has its own independent tag."""
        frame1 = bytes([AUDIO_TAG_PCM]) + b"\x00" * 960
        frame2 = bytes([AUDIO_TAG_OPUS]) + b"\x01" * 60
        self.assertEqual(frame1[0], 0x00)
        self.assertEqual(frame2[0], 0x01)

    def test_48khz_mono_pcm_bandwidth(self):
        """48kHz mono Int16 = 48000 * 2 = 96000 bytes/sec = 768 kbps."""
        bytes_per_sec = RX_RATE * 2  # 16-bit = 2 bytes/sample
        kbps = bytes_per_sec * 8 / 1000
        self.assertAlmostEqual(kbps, 768.0, delta=1)


class AudioDeviceDetectionTests(unittest.TestCase):
    """SDD AD-008: FT-710 audio device name matching."""

    def test_ft710_name_pattern_matches(self):
        """'FT-710' substring should match."""
        names = [
            "USB Audio CODEC (FT-710)",
            "FT-710 USB Audio",
            "YAESU FT-710 Audio",
        ]
        for name in names:
            self.assertTrue(
                "FT-710" in name or "FT710" in name or "YAESU" in name.upper()
            )

    def test_non_ft710_name_does_not_match(self):
        """Other audio devices should not match."""
        names = [
            "Built-in Microphone",
            "External USB Headset",
            "HDMI Audio Output",
        ]
        for name in names:
            self.assertFalse(
                "FT-710" in name or "FT710" in name or "YAESU" in name.upper()
            )


class _FakePyAudio:
    """Minimal PyAudio stand-in for device-selection tests."""

    def __init__(self, devices, host_apis=None):
        self._devices = devices
        self._host_apis = host_apis

    def get_device_count(self):
        return len(self._devices)

    def get_device_info_by_index(self, i):
        return self._devices[i]

    def get_host_api_info_by_index(self, i):
        if self._host_apis is not None:
            return {"name": self._host_apis[i]}
        return {"name": "MME"}

    def get_default_output_device_info(self):
        for i, d in enumerate(self._devices):
            if d.get("maxOutputChannels", 0) > 0:
                return {**d, "index": i}
        raise OSError("no default output")


def _dev(name, inputs=0, outputs=0, rate=44100, host_api=0):
    return {
        "name": name,
        "maxInputChannels": inputs,
        "maxOutputChannels": outputs,
        "defaultSampleRate": rate,
        "hostApi": host_api,
    }


class USBCodecDeviceSelectionTests(unittest.TestCase):
    """SDD AD-008: generic USB-audio name tier (FT-710 built-in sound card
    on Windows — enumerates as "USB Audio CODEC" or "USB Audio Device"
    depending on driver/OS) must win over the mono / full-duplex heuristics,
    which otherwise grab a laptop mic or PC speakers and silently break RX/TX."""

    def _make_handler(self, devices):
        from audio_handler import AudioHandler
        h = AudioHandler.__new__(AudioHandler)
        h.rx_device = None
        h.tx_device = None
        h._pa = _FakePyAudio(devices)
        return h

    def test_rx_prefers_usb_codec_over_mono_heuristic(self):
        # Mono webcam mic enumerates first; the codec must still win by name.
        h = self._make_handler([
            _dev("Mono Webcam Mic", inputs=1),
            _dev("麦克风 (USB Audio CODEC)", inputs=1),
        ])
        self.assertEqual(h._find_rx_device(), 1)

    def test_rx_prefers_full_duplex_codec_when_multiple_match(self):
        # Field report (macOS): two codec-named devices enumerate at once —
        # "USB Audio CODEC" (input-only interloper, e.g. a headset mic) ahead
        # of "USB Audio Device" (the full-duplex FT-710). First-match would
        # grab the interloper; the FT-710 sound card has both input + output,
        # so prefer the codec match that is full-duplex.
        h = self._make_handler([
            _dev("USB Audio CODEC", inputs=1),
            _dev("USB Audio Device", inputs=1, outputs=2),
        ])
        self.assertEqual(h._find_rx_device(), 1)

    def test_tx_prefers_full_duplex_codec_when_multiple_match(self):
        # Same collision on the TX side: output-only "USB Audio CODEC" lands
        # first, full-duplex "USB Audio Device" (FT-710) second. TX modulation
        # must route to the full-duplex codec, not the first hit.
        h = self._make_handler([
            _dev("USB Audio CODEC", outputs=2),
            _dev("USB Audio Device", inputs=1, outputs=2),
        ])
        self.assertEqual(h._find_tx_device(), 1)

    def test_rx_uses_first_codec_match_when_duplicated_per_host_api(self):
        # Same physical device under MME + WASAPI — first match is fine.
        h = self._make_handler([
            _dev("麦克风 (USB Audio CODEC)", inputs=1),
            _dev("Microphone (USB Audio CODEC)", inputs=1),
        ])
        self.assertEqual(h._find_rx_device(), 0)

    def test_tx_prefers_usb_codec_over_full_duplex_heuristic(self):
        # A full-duplex laptop sound card must not steal TX modulation.
        h = self._make_handler([
            _dev("Realtek Full-Duplex Card", inputs=2, outputs=2),
            _dev("扬声器 (USB Audio CODEC)", outputs=2),
        ])
        self.assertEqual(h._find_tx_device(), 1)

    def test_tx_uses_first_codec_match_when_duplicated_per_host_api(self):
        h = self._make_handler([
            _dev("Speakers (2- USB Audio CODEC)", outputs=2),
            _dev("扬声器 (USB Audio CODEC)", outputs=2),
        ])
        self.assertEqual(h._find_tx_device(), 0)

    def test_rx_matches_usb_audio_device_variant(self):
        # Field report: some Windows driver/OS builds enumerate the FT-710
        # sound card as "USB Audio Device" instead of "USB Audio CODEC".
        h = self._make_handler([
            _dev("Built-in Microphone", inputs=2),
            _dev("麦克风 (USB Audio Device)", inputs=1),
        ])
        self.assertEqual(h._find_rx_device(), 1)

    def test_tx_matches_usb_audio_device_variant(self):
        h = self._make_handler([
            _dev("Built-in Speakers", outputs=2),
            _dev("扬声器 (USB Audio Device)", outputs=2),
        ])
        self.assertEqual(h._find_tx_device(), 1)

    def test_common_prefix_lock_matches_both_variants(self):
        import config
        old_rx, old_tx = config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE
        config.AUDIO_RX_DEVICE = "USB Audio"
        config.AUDIO_TX_DEVICE = "USB Audio"
        try:
            h = self._make_handler([
                _dev("Built-in Microphone", inputs=2),
                _dev("USB Audio Device", inputs=1, outputs=2),
                _dev("Built-in Speakers", outputs=2),
            ])
            self.assertEqual(h._find_rx_device(), 1)
            self.assertEqual(h._find_tx_device(), 1)
        finally:
            config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE = old_rx, old_tx

    def test_explicit_name_lock_still_wins(self):
        import config
        old_rx, old_tx = config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE
        config.AUDIO_RX_DEVICE = "USB Audio CODEC"
        config.AUDIO_TX_DEVICE = "USB Audio CODEC"
        try:
            h = self._make_handler([
                _dev("Built-in Microphone", inputs=2),
                _dev("USB Audio CODEC", inputs=1, outputs=2),
                _dev("Built-in Speakers", outputs=2),
            ])
            self.assertEqual(h._find_rx_device(), 1)
            self.assertEqual(h._find_tx_device(), 1)
        finally:
            config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE = old_rx, old_tx


class RestartRxTests(unittest.TestCase):
    """Windows full-duplex workaround (SDD V2.8): the TX playback stream
    silently wedges RX capture on the same C-Media USB codec, so the RX
    stream is reopened on every TX→RX transition (Windows only)."""

    def _make_handler(self):
        from audio_handler import AudioHandler
        h = AudioHandler.__new__(AudioHandler)
        h._rx_running = True
        h._rx_stream = object()
        h._pa = object()  # non-None sentinel; stop/start are stubbed below
        return h

    def test_windows_reopens_stop_then_start(self):
        from unittest.mock import patch
        h = self._make_handler()
        calls = []
        h.stop_rx = lambda: calls.append("stop")
        h.start_rx = lambda: (calls.append("start"), True)[1]
        with patch("sys.platform", "win32"):
            h.restart_rx()
        self.assertEqual(calls, ["stop", "start"])

    def test_non_windows_is_noop(self):
        from unittest.mock import patch
        h = self._make_handler()

        def _boom():
            raise AssertionError("streams must not be touched off Windows")

        h.stop_rx = _boom
        h.start_rx = _boom
        with patch("sys.platform", "darwin"):
            h.restart_rx()

    def test_skipped_when_rx_not_running(self):
        from unittest.mock import patch
        h = self._make_handler()
        h._rx_running = False
        called = []
        h.stop_rx = lambda: called.append("stop")
        with patch("sys.platform", "win32"):
            h.restart_rx()
        self.assertEqual(called, [])

    def test_failed_reopen_logs_warning(self):
        from unittest.mock import patch
        h = self._make_handler()
        calls = []
        h.stop_rx = lambda: calls.append("stop")
        h.start_rx = lambda: (calls.append("start"), False)[1]
        with patch("sys.platform", "win32"):
            h.restart_rx()
        self.assertEqual(calls, ["stop", "start"])


class TxDeviceDomainTests(unittest.TestCase):
    """SDD AD-011: Opus PCM is always converted from 48 kHz to the
    FT-710's fixed 44.1 kHz device domain (960→882 samples per 20 ms)."""

    def _feed_handler(self, rate=None):
        import threading
        from collections import deque
        from audio_handler import AudioHandler
        h = AudioHandler.__new__(AudioHandler)
        h._tx_stream = object()
        h._tx_queue = deque()
        h._tx_queued_bytes = 0
        h._tx_peak = 0
        h._tx_lock = threading.Lock()
        if rate is not None:
            h._tx_rate = rate
        return h

    def test_48k_frame_resamples_to_exact_44k1_frame(self):
        h = self._feed_handler()
        h.feed_tx_audio(b"\x01\x02" * 960)
        self.assertEqual(len(h._tx_queue[0]), 882 * 2)
        self.assertEqual(h._tx_queued_bytes, 1764)

    def test_stale_48k_rate_cannot_bypass_resampling(self):
        h = self._feed_handler(48000)
        frame = b"\x01\x02" * 960
        h.feed_tx_audio(frame)
        self.assertEqual(len(h._tx_queue[0]), 882 * 2)
        self.assertNotEqual(h._tx_queue[0], frame)

    def test_prebuffer_budget_is_derived_from_44100(self):
        from audio_handler import TX_PREBUFFER_BYTES, TX_PREBUFFER_MS
        self.assertEqual(TX_PREBUFFER_BYTES, 44100 * 2 * TX_PREBUFFER_MS // 1000)

    def test_max_buffer_budget_is_derived_from_44100(self):
        from audio_handler import TX_MAX_BUFFER_BYTES, TX_MAX_BUFFER_MS
        self.assertEqual(TX_MAX_BUFFER_BYTES, 44100 * 2 * TX_MAX_BUFFER_MS // 1000)


class _FakeTxStream:
    def __init__(self):
        self._active = True

    def is_active(self):
        return self._active

    def write(self, _data):
        return None

    def stop_stream(self):
        self._active = False

    def close(self):
        self._active = False


class _FakePyAudioOpen(_FakePyAudio):
    """_FakePyAudio extended with open() so start_tx can run end-to-end."""

    def __init__(self, devices, host_apis=None):
        super().__init__(devices, host_apis)
        self.open_calls = []

    def open(self, **kwargs):
        self.open_calls.append(kwargs)
        return _FakeTxStream()


class StartTxWindowsTests(unittest.TestCase):
    """start_tx end-to-end on win32: must not raise (v1.7.4 sys-import
    regression), and must keep the selected device at 44.1 kHz."""

    def _make_handler(self, devices, host_apis):
        import threading
        from collections import deque
        from audio_handler import AudioHandler
        h = AudioHandler.__new__(AudioHandler)
        h._pa = _FakePyAudioOpen(devices, host_apis)
        h.rx_device = None
        h.tx_device = None
        h._tx_stream = None
        h._tx_queue = deque()
        h._tx_queued_bytes = 0
        h._tx_primed = False
        h._tx_lock = threading.Lock()
        h._tx_write_lock = threading.Lock()
        return h

    def test_start_tx_keeps_selected_device_at_44100_on_win32(self):
        import config
        from unittest.mock import patch
        old_rx, old_tx = config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE
        config.AUDIO_RX_DEVICE = ""
        config.AUDIO_TX_DEVICE = ""
        try:
            h = self._make_handler(
                [_dev("扬声器 (USB Audio CODEC)", outputs=2, rate=44100, host_api=0),
                 _dev("扬声器 (USB Audio CODEC)", outputs=2, rate=48000, host_api=1)],
                ["MME", "Windows WASAPI"],
            )
            with patch("sys.platform", "win32"):
                ok = h.start_tx()
            self.assertTrue(ok)
            self.assertEqual(h._tx_rate, 44100)
            self.assertEqual(h._pa.open_calls[0]["output_device_index"], 0)
            self.assertEqual(h._pa.open_calls[0]["rate"], 44100)
            self.assertEqual(h._pa.open_calls[0]["frames_per_buffer"], 882)
            self.assertEqual(h._tx_prebuffer_bytes, 44100 * 2 * 60 // 1000)
            self.assertEqual(h._tx_max_buffer_bytes, 44100 * 2 * 400 // 1000)
        finally:
            config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE = old_rx, old_tx

    def test_start_tx_stays_44100_on_macos(self):
        import config
        from unittest.mock import patch
        old_rx, old_tx = config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE
        config.AUDIO_RX_DEVICE = ""
        config.AUDIO_TX_DEVICE = ""
        try:
            h = self._make_handler(
                [_dev("扬声器 (USB Audio CODEC)", outputs=2, rate=44100, host_api=0)],
                ["Core Audio"],
            )
            with patch("sys.platform", "darwin"):
                ok = h.start_tx()
            self.assertTrue(ok)
            self.assertEqual(h._tx_rate, 44100)
            self.assertEqual(h._pa.open_calls[0]["rate"], 44100)
        finally:
            config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE = old_rx, old_tx


class _FailingOpenPyAudio(_FakePyAudioOpen):
    """Fake PyAudio whose open() always fails with the macOS USB
    re-enumeration signature error (-9999)."""

    def open(self, **kwargs):
        self.open_calls.append(kwargs)
        raise OSError("[Errno -9999] Unanticipated host error")


class PortAudioReinitTests(unittest.TestCase):
    """USB re-enumeration (every radio power cycle drops/re-adds the
    FT-710's sound card) invalidates the device IDs PortAudio cached at
    Pa_Initialize time — subsequent opens fail with -9999 (field report
    2026-07-27: every PTT failed after CAT power cycles until the server
    was restarted).  start_rx/start_tx must re-initialize PortAudio once
    and retry with a freshly resolved device index."""

    DEVICES = None  # set per test

    def _patch_devices_empty(self):
        import config
        self._old_rx, self._old_tx = config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE
        config.AUDIO_RX_DEVICE = ""
        config.AUDIO_TX_DEVICE = ""

    def _restore_devices(self):
        import config
        config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE = self._old_rx, self._old_tx

    def _make_handler(self, pa):
        import threading
        from collections import deque
        from audio_handler import AudioHandler
        h = AudioHandler.__new__(AudioHandler)
        h._pa = pa
        h.rx_device = None
        h.tx_device = None
        h._tx_stream = None
        h._tx_queue = deque()
        h._tx_queued_bytes = 0
        h._tx_primed = False
        h._tx_lock = threading.Lock()
        h._tx_write_lock = threading.Lock()
        h._rx_stream = None
        h._rx_running = False
        return h

    def test_start_tx_reinits_pyaudio_then_succeeds(self):
        self._patch_devices_empty()
        try:
            bad = _FailingOpenPyAudio([_dev("USB Audio Device", inputs=1, outputs=2)])
            good = _FakePyAudioOpen([_dev("USB Audio Device", inputs=1, outputs=2)])
            h = self._make_handler(bad)
            reinit_calls = []
            def fake_reinit():
                reinit_calls.append(1)
                h._pa = good
            h._reinit_pyaudio = fake_reinit
            self.assertTrue(h.start_tx())
            self.assertEqual(len(reinit_calls), 1)
            self.assertEqual(len(bad.open_calls), 3)   # first round exhausted
            self.assertEqual(len(good.open_calls), 1)  # retry after re-init
        finally:
            self._restore_devices()

    def test_start_tx_reinit_preserves_44100_when_enumeration_changes(self):
        from unittest.mock import patch
        self._patch_devices_empty()
        try:
            bad = _FailingOpenPyAudio(
                [_dev("USB Audio Device", inputs=1, outputs=2,
                      rate=44100, host_api=0),
                 _dev("USB Audio Device", inputs=1, outputs=2,
                      rate=48000, host_api=1)],
                ["MME", "Windows WASAPI"],
            )
            good = _FakePyAudioOpen(
                [_dev("Built-in Speakers", outputs=2,
                      rate=48000, host_api=0),
                 _dev("USB Audio Device", inputs=1, outputs=2,
                      rate=48000, host_api=1)],
                ["MME", "Windows WASAPI"],
            )
            h = self._make_handler(bad)

            def fake_reinit():
                h._pa = good

            h._reinit_pyaudio = fake_reinit
            with patch("sys.platform", "win32"):
                self.assertTrue(h.start_tx())
            self.assertEqual(len(bad.open_calls), 3)
            self.assertEqual(good.open_calls[0]["output_device_index"], 1)
            self.assertEqual(good.open_calls[0]["rate"], 44100)
            self.assertEqual(good.open_calls[0]["frames_per_buffer"], 882)
        finally:
            self._restore_devices()

    def test_start_tx_gives_up_when_reinit_does_not_help(self):
        self._patch_devices_empty()
        try:
            bad = _FailingOpenPyAudio([_dev("USB Audio Device", inputs=1, outputs=2)])
            still_bad = _FailingOpenPyAudio([_dev("USB Audio Device", inputs=1, outputs=2)])
            h = self._make_handler(bad)
            def fake_reinit():
                h._pa = still_bad
            h._reinit_pyaudio = fake_reinit
            self.assertFalse(h.start_tx())
            self.assertEqual(len(bad.open_calls), 3)
            self.assertEqual(len(still_bad.open_calls), 3)
        finally:
            self._restore_devices()

    def test_start_rx_reinits_pyaudio_then_succeeds(self):
        self._patch_devices_empty()
        try:
            bad = _FailingOpenPyAudio([_dev("USB Audio Device", inputs=1, outputs=2)])
            good = _FakePyAudioOpen([_dev("USB Audio Device", inputs=1, outputs=2)])
            h = self._make_handler(bad)
            reinit_calls = []
            def fake_reinit():
                reinit_calls.append(1)
                h._pa = good
            h._reinit_pyaudio = fake_reinit
            self.assertTrue(h.start_rx())
            self.assertTrue(h._rx_running)
            self.assertEqual(len(reinit_calls), 1)
            self.assertEqual(len(bad.open_calls), 1)   # single try, then re-init
            self.assertEqual(len(good.open_calls), 1)
        finally:
            self._restore_devices()


if __name__ == "__main__":
    unittest.main()
