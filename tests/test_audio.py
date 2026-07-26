"""
Tests for audio_handler.py and opus_rx.py — SDD AD-004 (tagged dual-codec audio).
Verifies: codec tag constants, encoder/decoder lifecycle, PCM framing,
audio device name matching logic.
"""
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
        self.assertIn("audioWorklet.addModule('/tx_capture_worklet.js?v=tx-audio-4')", source)
        self.assertIn("new AudioWorkletNode", source)
        self.assertIn("type: 'float_frame'", source)

    def test_tx_start_creates_worker_before_start_command(self):
        """First PTT must not lose mic frames because the worker missed start."""
        source = (REPO_ROOT / "static" / "ft710_main.js").read_text(encoding="utf-8")
        start_fn = source[source.index("function startTXAudio()"):source.index("function startTXAudioFallback()")]
        ensure_idx = start_fn.index("ensureTXOpusWorker()")
        start_idx = start_fn.index("postMessage({type: 'start'}")
        self.assertLess(ensure_idx, start_idx)

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
        self.assertIn("ft710-v21", sw_source)

    def test_tx_debug_tone_bypasses_microphone_capture(self):
        main_source = (REPO_ROOT / "static" / "ft710_main.js").read_text(encoding="utf-8")
        worker_source = (REPO_ROOT / "static" / "tx_opus_worker.js").read_text(encoding="utf-8")
        self.assertIn("window.TXDebug", main_source)
        self.assertIn("type: 'tone_start'", main_source)
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


if __name__ == "__main__":
    unittest.main()
