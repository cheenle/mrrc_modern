"""
Tests for audio_handler.py and opus_rx.py — SDD AD-004 (tagged dual-codec audio).
Verifies: codec tag constants, encoder/decoder lifecycle, PCM framing,
audio device name matching logic.
"""
import re
import struct
import unittest
from pathlib import Path

import server
from audio_profile import PROFILE_PCM16
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
                resource_roots=[Path(r"C:\Program Files\MRRC Modern")],
                find_library_result=None,
            )
        ]
        self.assertIn(r"C:\Program Files\MRRC Modern\opus.dll", candidates)
        self.assertIn(
            r"C:\Program Files\MRRC Modern\_internal\opus.dll",
            candidates,
        )
        self.assertIn(
            r"C:\Program Files\MRRC Modern\vendor\opus\windows\bin\x64\opus.dll",
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
        assert start_match is not None
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
        self.assertIn("mrrc-v69", sw_source)

    def test_tx_debug_tone_bypasses_microphone_capture(self):
        main_source = (REPO_ROOT / "static" / "ft710_main.js").read_text(encoding="utf-8")
        worker_source = (REPO_ROOT / "static" / "tx_opus_worker.js").read_text(encoding="utf-8")
        self.assertIn("window.TXDebug", main_source)
        self.assertRegex(main_source, r"type:\s*['\"]tone_start['\"]")
        self.assertIn("function startTone", worker_source)
        self.assertIn("Math.sin(_tonePhase)", worker_source)

    def test_tx_audio_drain_does_not_block_event_loop(self):
        """The blocking PortAudio write must never run on the event loop.

        It was `asyncio.to_thread` (the *shared* default executor); measured on
        an FT-991A box 2026-11-01, competing CAT queries starved it to 45.2
        frames/s against the 50 needed, dropping 63 of 828 frames.  It now runs
        on a dedicated single-worker executor — same offload, own thread — so
        assert the property (offloaded) rather than one spelling of it.
        """
        source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        offloaded = ("await asyncio.to_thread(audio.write_tx_chunk)" in source
                     or "run_in_executor(" in source and
                     "_TX_AUDIO_EXECUTOR, audio.write_tx_chunk" in source)
        self.assertTrue(offloaded, "the TX write must be handed to a thread")
        self.assertNotIn("\n                audio.write_tx_chunk()", source)

    def test_tx_audio_writes_have_their_own_executor(self):
        """Sharing the default executor with CAT reads is what starved it."""
        source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("_TX_AUDIO_EXECUTOR = concurrent.futures.ThreadPoolExecutor(", source)
        self.assertIn("max_workers=1, thread_name_prefix=\"txaudio\"", source)

    def test_the_event_loop_lag_probe_runs(self):
        """A loop stall costs 5 audio frames per 100 ms — it must be visible."""
        source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("async def _event_loop_lag_probe", source)
        self.assertIn('name="loop_lag"', source)
        self.assertIn("EVENT_LOOP_LAG_ALERT_S", source)

    def test_tx_opus_encoder_uses_valid_high_quality_ctl_settings(self):
        source = (REPO_ROOT / "static" / "modules" / "opus_codec.js").read_text(encoding="utf-8")
        self.assertIn("setValue(bitrate_ptr, 64000", source)
        self.assertIn("_opus_encoder_ctl(this.handle, 4006, vbr_ptr)", source)
        self.assertNotIn("_opus_encoder_ctl(this.handle, 4004, vbr_ptr)", source)
        self.assertNotIn("_opus_encoder_ctl(this.handle, 4030", source)


class ServerSideRecordingContractTests(unittest.TestCase):
    """V2.42: recording moved to the server; the browser must not encode.

    The old browser recorder concatenated untimestamped frames, so delivery
    jitter ended up in the file.  These assertions pin the replacement
    contract: REC talks to the server, and no MP3 encoder ships to clients.
    """

    def test_record_button_sits_next_to_tune(self):
        source = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        ptt_footer = source[source.index('<footer class="ptt-footer">'):source.index("</footer>", source.index('<footer class="ptt-footer">'))]
        self.assertIn('id="btn-tune"', ptt_footer)
        self.assertIn('id="btn-record"', ptt_footer)
        self.assertLess(ptt_footer.index('id="btn-tune"'), ptt_footer.index('id="btn-record"'))

    def test_browser_recorder_is_gone(self):
        main = (REPO_ROOT / "static" / "ft710_main.js").read_text(encoding="utf-8")
        self.assertNotIn("window.RXRecorder", main)
        self.assertNotIn("lamejs", main)
        self.assertNotIn("feedRXRecorderFrame", main)
        self.assertNotIn("feedTXRecorderFrame", main)
        self.assertNotIn("_loadLame", main)
        self.assertNotIn("/modules/lame.js", main)

    def test_lamejs_asset_is_deleted(self):
        self.assertFalse((REPO_ROOT / "static" / "modules" / "lame.js").exists())

    def test_rec_button_talks_to_the_server_and_shows_server_state(self):
        ui = (REPO_ROOT / "static" / "ft710_ui.js").read_text(encoding="utf-8")
        self.assertIn("sendCommand('recording'", ui)
        self.assertIn("radioState.recording", ui)
        self.assertIn("record-active", ui)
        self.assertNotIn("window.RXRecorder", ui)

    def test_recording_state_message_is_handled(self):
        main = (REPO_ROOT / "static" / "ft710_main.js").read_text(encoding="utf-8")
        self.assertIn('"recordingState"', main)
        self.assertIn("renderRecordingState", main)


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
        # 纯媒体 socket：仍带 12 ms 超时（慢客户端只死自己）。
        self.assertIn("asyncio.wait_for(ws.send_bytes(frame)", source)
        # 复用 socket（/WSterminal）：**不能**在扇出里 await 它 —— 它同时背着控制
        # 与 PTT 安全链，而且 send_bytes 会先把整帧写进 transport 再 await drain，
        # 「超时」并不丢帧。走有界队列，满队列才是真丢（FP77）。
        self.assertIn("_terminal_offer(queue, TERMINAL_CHAN_RX_AUDIO, frame, mailbox)",
                      source)
        self.assertNotIn("_terminal_prefix", source)

    def test_rx_loop_uses_shared_send_helper(self):
        source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("async def _send_audio_frames_to_clients(", source)
        self.assertIn("await _send_audio_frames_to_clients(", source)

    def test_rx_loop_skips_encode_when_no_clients(self):
        source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        # Idle with no listeners: skip the PCM read/encode entirely.  A
        # running recording keeps the loop awake (V2.42) — otherwise a REC
        # with nobody listening would capture silence.  So does a live FT8
        # RX tap (feat/ft8): the FT8 decoder has no browser client, and
        # without this term it never receives audio at all.
        self.assertIn("_ft8_tap_live = _ft8_runtime is not None and _ft8_runtime.rx_tap is not None",
                      source)
        # The local speaker (AD-028) is a listener with no browser client too,
        # so it joins the FT8 tap in the idle gate.
        self.assertIn("_speaker_live = _local_speaker is not None and _local_speaker.active", source)
        self.assertIn("and not _ft8_tap_live and not _speaker_live)", source)

        self.assertIn("await asyncio.sleep(idle_interval)", source)
        self.assertIn("continue", source)

    def test_pcm16_only_tick_does_not_encode_opus(self):
        """A box whose only listeners declared PCM16 must not run libopus."""
        import asyncio
        import contextlib
        from unittest import mock

        import server

        class LoudCapture:
            _rx_running = True
            _rx_last_peak = 0
            opus_enabled = True

            def __init__(self):
                self.encoded = []

            def read_rx_chunk(self):
                return b"\x01\x00" * 960

            def note_rx_chunk(self, block):
                pass

            def rx_silent_seconds(self):
                return 0.0

            def restart_rx(self):
                pass

            def encode_rx_audio(self, block):
                self.encoded.append(block)
                raise AssertionError("legacy Opus must not run for a PCM16-only group")

        async def drive():
            audio = LoudCapture()
            encoder = mock.Mock()
            encoder.push.return_value = [b"\x02" + bytes(640)]
            ws_pcm16 = object()
            saved = (server.audio, server.radio, server.audio_rx_clients,
                     server._audio_rx_profiles, server._pcm16_rx_encoder,
                     server._rec_session, server._ft8_runtime,
                     server._local_speaker)
            server.audio = audio
            server.radio = None
            server.audio_rx_clients = {ws_pcm16}
            server._audio_rx_profiles = {ws_pcm16: "pcm16"}
            server._pcm16_rx_encoder = encoder
            server._rec_session = mock.Mock(active=False)
            server._ft8_runtime = None
            server._local_speaker = None
            task = asyncio.create_task(server._audio_rx_loop())
            try:
                await asyncio.sleep(0.15)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                (server.audio, server.radio, server.audio_rx_clients,
                 server._audio_rx_profiles, server._pcm16_rx_encoder,
                 server._rec_session, server._ft8_runtime,
                 server._local_speaker) = saved
            return audio, encoder

        audio, encoder = asyncio.run(drive())
        self.assertEqual(audio.encoded, [],
                         "a PCM16-only tick burned a core on Opus frames")
        self.assertGreaterEqual(encoder.push.call_count, 1)

    def test_legacy_only_tick_still_calls_the_opus_encoder(self):
        """A socket that never declared keeps byte-for-byte the legacy path."""
        import asyncio
        import contextlib
        from unittest import mock

        import server

        class LoudCapture:
            _rx_running = True
            _rx_last_peak = 0
            opus_enabled = True

            def __init__(self):
                self.encoded = []

            def read_rx_chunk(self):
                return b"\x01\x00" * 960

            def note_rx_chunk(self, block):
                pass

            def rx_silent_seconds(self):
                return 0.0

            def restart_rx(self):
                pass

            def encode_rx_audio(self, block):
                self.encoded.append(block)
                return [b"\x01opus"]

        async def drive():
            audio = LoudCapture()
            encoder = mock.Mock()
            ws_legacy = object()
            saved = (server.audio, server.radio, server.audio_rx_clients,
                     server._audio_rx_profiles, server._pcm16_rx_encoder,
                     server._rec_session, server._ft8_runtime,
                     server._local_speaker)
            server.audio = audio
            server.radio = None
            server.audio_rx_clients = {ws_legacy}
            server._audio_rx_profiles = {}
            server._pcm16_rx_encoder = encoder
            server._rec_session = mock.Mock(active=False)
            server._ft8_runtime = None
            server._local_speaker = None
            sent = []

            async def spy(frames, clients, **kw):
                sent.append((len(frames), ws_legacy in clients))
                return set()

            with mock.patch.object(server, "_send_audio_frames_to_clients", spy):
                task = asyncio.create_task(server._audio_rx_loop())
                try:
                    await asyncio.sleep(0.15)
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            (server.audio, server.radio, server.audio_rx_clients,
             server._audio_rx_profiles, server._pcm16_rx_encoder,
             server._rec_session, server._ft8_runtime,
             server._local_speaker) = saved
            return audio, encoder, sent

        audio, encoder, sent = asyncio.run(drive())
        self.assertTrue(audio.encoded,
                        "the legacy group stopped being encoded")
        self.assertTrue(sent and all(is_legacy for _, is_legacy in sent),
                        "a legacy-only tick sent to somebody else: %r" % (sent,))
        encoder.push.assert_not_called()

    def test_transmitting_tick_encodes_for_neither_group(self):
        """While keyed, neither wire format may be produced or sent.

        The radio's half-duplex RX output is muted silence; encoding it would
        pile silent packets into every client's jitter buffer, which then has
        to DRAIN after unkey before real audio is heard.  The guard sits on the
        group encode, exactly where ``_needs_frames`` used to gate Opus.
        """
        import asyncio
        import contextlib
        from unittest import mock

        import server

        class LoudCapture:
            _rx_running = True
            _rx_last_peak = 0
            opus_enabled = True

            def __init__(self):
                self.encoded = []

            def read_rx_chunk(self):
                return b"\x01\x00" * 960

            def note_rx_chunk(self, block):
                pass

            def rx_silent_seconds(self):
                return 0.0

            def encode_rx_audio(self, block):
                self.encoded.append(block)
                return [b"\x01opus"]

        class TxingRadio:
            is_transmitting = True
            tx_status = 1
            squelch_open = False

        async def drive():
            audio = LoudCapture()
            encoder = mock.Mock()
            encoder.push.return_value = [b"\x02" + bytes(640)]
            ws_pcm16, ws_legacy = object(), object()
            saved = (server.audio, server.radio, server.audio_rx_clients,
                     server._audio_rx_profiles, server._pcm16_rx_encoder,
                     server._rec_session, server._ft8_runtime,
                     server._local_speaker)
            server.audio = audio
            server.radio = TxingRadio()
            server.audio_rx_clients = {ws_pcm16, ws_legacy}
            server._audio_rx_profiles = {ws_pcm16: "pcm16"}
            server._pcm16_rx_encoder = encoder
            server._rec_session = mock.Mock(active=False)
            server._ft8_runtime = None
            server._local_speaker = None
            task = asyncio.create_task(server._audio_rx_loop())
            try:
                await asyncio.sleep(0.15)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                (server.audio, server.radio, server.audio_rx_clients,
                 server._audio_rx_profiles, server._pcm16_rx_encoder,
                 server._rec_session, server._ft8_runtime,
                 server._local_speaker) = saved
            return audio, encoder

        audio, encoder = asyncio.run(drive())
        self.assertEqual(audio.encoded, [],
                         "a transmitting tick must not encode Opus")
        encoder.push.assert_not_called()

    def test_a_pcm16_conversion_failure_closes_only_the_pcm16_sockets(self):
        """4004 reaches the sockets that asked for PCM16, never the legacy ones.

        The one thing this must not do is relabel 48 kHz audio as ``0x02``: a
        device playing 48 kHz at its 16 kHz rate would be 3× fast, which is far
        harder to diagnose from the field than a closed socket with a reason.
        """
        import asyncio
        import contextlib
        from unittest import mock

        import server

        class LoudCapture:
            _rx_running = True
            _rx_last_peak = 0
            opus_enabled = True

            def __init__(self):
                self.encoded = []

            def read_rx_chunk(self):
                return b"\x01\x00" * 960

            def note_rx_chunk(self, block):
                pass

            def rx_silent_seconds(self):
                return 0.0

            def encode_rx_audio(self, block):
                self.encoded.append(block)
                return [b"\x01opus"]

        class FakeWS:
            def __init__(self):
                self.frames = []
                self.closed = None

            async def send_bytes(self, data):
                self.frames.append(data)

            async def close(self, *, code=1000, reason=""):
                self.closed = (code, reason)

        async def drive():
            audio = LoudCapture()
            encoder = mock.Mock()
            encoder.push.side_effect = RuntimeError("soxr stream died")
            ws_pcm16, ws_legacy = FakeWS(), FakeWS()
            saved = (server.audio, server.radio, server.audio_rx_clients,
                     server._audio_rx_profiles, server._pcm16_rx_encoder,
                     server._rec_session, server._ft8_runtime,
                     server._local_speaker)
            server.audio = audio
            server.radio = None
            server.audio_rx_clients = {ws_pcm16, ws_legacy}
            server._audio_rx_profiles = {ws_pcm16: "pcm16"}
            server._pcm16_rx_encoder = encoder
            server._rec_session = mock.Mock(active=False)
            server._ft8_runtime = None
            server._local_speaker = None
            task = asyncio.create_task(server._audio_rx_loop())
            try:
                await asyncio.sleep(0.15)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                (server.audio, server.radio, server.audio_rx_clients,
                 server._audio_rx_profiles, server._pcm16_rx_encoder,
                 server._rec_session, server._ft8_runtime,
                 server._local_speaker) = saved
            return audio, encoder, ws_pcm16, ws_legacy

        audio, encoder, ws_pcm16, ws_legacy = asyncio.run(drive())
        self.assertEqual(ws_pcm16.closed, (4004, "PCM16 resampler unavailable"))
        self.assertIsNone(ws_legacy.closed,
                          "a legacy listener must survive a PCM16 failure")
        self.assertTrue(ws_legacy.frames,
                        "the legacy group keeps receiving Opus after the failure")
        self.assertFalse(any(f[0] == 0x02 for f in ws_legacy.frames),
                         "48 kHz audio was relabelled as PCM16")
        # The converter is not required to fail on the very first tick — so the
        # assertion is about the *shape* of the recovery, never a call count:
        # once it did fail, that group is closed and never pushed to again.
        self.assertGreaterEqual(encoder.push.call_count, 1)
        self.assertEqual(len(ws_pcm16.frames), 0,
                         "a socket kept being fed after it was closed with 4004")


    def test_a_legacy_opus_failure_does_not_reach_the_pcm16_group(self):
        """libopus dying must not close or mute anybody who asked for PCM16.

        One shared try around both encodes made the opposite true: the Opus
        error surfaced as a "PCM16 RX conversion failed" line, healthy 16 kHz
        listeners were closed with 4004, and the same failing encode was then
        retried immediately in the handler.
        """
        import asyncio
        import contextlib
        from unittest import mock

        import server

        class DyingOpusCapture:
            _rx_running = True
            _rx_last_peak = 0
            opus_enabled = True

            def __init__(self):
                self.attempts = 0

            def read_rx_chunk(self):
                return b"\x01\x00" * 960

            def note_rx_chunk(self, block):
                pass

            def rx_silent_seconds(self):
                return 0.0

            def restart_rx(self):
                pass

            def encode_rx_audio(self, block):
                self.attempts += 1
                raise RuntimeError("libopus died")

        class FakeWS:
            def __init__(self):
                self.frames = []
                self.closed = None

            async def send_bytes(self, data):
                self.frames.append(data)

            async def close(self, *, code=1000, reason=""):
                self.closed = (code, reason)

        async def drive():
            audio = DyingOpusCapture()
            encoder = mock.Mock()

            # Variable-length output on purpose: priming returns nothing, then
            # one frame, then several while the filter catches up.  Nothing
            # here reads a push's list length as a tick count — the assertions
            # below count sends, not frames per send.
            def variable_push(block):
                variable_push.n += 1
                return [b"\x02" + bytes(640)] * ((variable_push.n - 1) % 3)

            variable_push.n = 0
            encoder.push.side_effect = variable_push
            ws_pcm16, ws_legacy = FakeWS(), FakeWS()
            saved = (server.audio, server.radio, server.audio_rx_clients,
                     server._audio_rx_profiles, server._pcm16_rx_encoder,
                     server._rec_session, server._ft8_runtime,
                     server._local_speaker)
            server.audio = audio
            server.radio = None
            server.audio_rx_clients = {ws_pcm16, ws_legacy}
            server._audio_rx_profiles = {ws_pcm16: "pcm16"}
            server._pcm16_rx_encoder = encoder
            server._rec_session = mock.Mock(active=False)
            server._ft8_runtime = None
            server._local_speaker = None
            closer = mock.AsyncMock()
            sent = []

            async def spy(frames, clients, **kw):
                sent.append((len(frames), ws_pcm16 in clients))
                return set()

            with mock.patch.object(server, "_close_pcm16_listeners", closer), \
                    mock.patch.object(server, "_send_audio_frames_to_clients", spy):
                task = asyncio.create_task(server._audio_rx_loop())
                try:
                    await asyncio.sleep(0.15)
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            (server.audio, server.radio, server.audio_rx_clients,
             server._audio_rx_profiles, server._pcm16_rx_encoder,
             server._rec_session, server._ft8_runtime,
             server._local_speaker) = saved
            pcm16_ticks = [n for n, is_pcm16 in sent if is_pcm16]
            return audio, encoder, closer, pcm16_ticks, sent

        audio, encoder, closer, pcm16_ticks, sent = asyncio.run(drive())
        closer.assert_not_awaited()
        self.assertTrue(pcm16_ticks,
                        "the PCM16 group lost its audio to an Opus failure "
                        "(sends seen: %r)" % (sent,))
        # PCM16 was fed on every tick that produced complete frames, and the
        # only sends in this window are PCM16 sends: a legacy encode that dies
        # cannot open a side channel to the browser listener beside it.
        self.assertTrue(all(is_pcm16 for _, is_pcm16 in sent),
                        "non-PCM16 send during a legacy failure: %r" % (sent,))
        self.assertGreaterEqual(len(pcm16_ticks), 2,
                                "the converter never reached a full frame")
        # Base behaviour: the exception still escapes this loop's handler, so
        # the *next* tick is what tries again — never a second attempt inside
        # one tick, and never a close of somebody who asked for PCM16.
        self.assertGreaterEqual(audio.attempts, len(pcm16_ticks))
        # Every tick ran the 16 kHz pass before the legacy encode was even
        # attempted, which is what makes the two failures non-interfering; the
        # priming pass simply had nothing complete to send that tick.
        self.assertEqual(audio.attempts, encoder.push.call_count)

    def test_a_late_legacy_failure_keeps_the_pcm16_group_streaming(self):
        """libopus dying mid-session must not stall the 16 kHz group either.

        The order alone is not enough: fan-out has to run per group, so the
        PCM16 send in the same tick still happens after the Opus call began to
        raise.  One shared send block made every later tick die at the legacy
        encode and silently stop delivering PCM16 — with no error line about
        it anywhere.
        """
        import asyncio
        import contextlib
        from unittest import mock

        import server

        class FlakyOpusCapture:
            _rx_running = True
            _rx_last_peak = 0
            opus_enabled = True

            def __init__(self, fail_after):
                self.fail_after = fail_after
                self.attempts = 0

            def read_rx_chunk(self):
                return b"\x01\x00" * 960

            def note_rx_chunk(self, block):
                pass

            def rx_silent_seconds(self):
                return 0.0

            def restart_rx(self):
                pass

            def encode_rx_audio(self, block):
                self.attempts += 1
                if self.attempts > self.fail_after:
                    raise RuntimeError("libopus died")
                # One frame per call, so a legacy send is recognisable by its
                # length and the PCM16 pass can prime without faking a frame.
                return [b"\x01opus"]

        async def drive():
            audio = FlakyOpusCapture(fail_after=2)
            encoder = mock.Mock()

            # Variable-length output, the real contract: nothing while the
            # filter primes, then one frame, then several while it catches up.
            def variable_push(block):
                variable_push.n += 1
                return [b"\x02" + bytes(640)] * ((variable_push.n - 1) % 3)

            variable_push.n = 0
            encoder.push.side_effect = variable_push
            ws_pcm16, ws_legacy = object(), object()
            saved = (server.audio, server.radio, server.audio_rx_clients,
                     server._audio_rx_profiles, server._pcm16_rx_encoder,
                     server._rec_session, server._ft8_runtime,
                     server._local_speaker)
            server.audio = audio
            server.radio = None
            server.audio_rx_clients = {ws_pcm16, ws_legacy}
            server._audio_rx_profiles = {ws_pcm16: "pcm16"}
            server._pcm16_rx_encoder = encoder
            server._rec_session = mock.Mock(active=False)
            server._ft8_runtime = None
            server._local_speaker = None
            pcm16_sent, legacy_sent = [], []

            async def spy(frames, clients, **kw):
                # The first byte is the codec tag: 0x02 is a PCM16 frame,
                # anything else came from the legacy encoder.  A priming tick
                # produces no send at all, so these lists hold only ticks that
                # actually delivered something to a group.
                (pcm16_sent if frames[0][0] == 0x02 else legacy_sent).append(
                    len(frames))
                return set()

            with mock.patch.object(server, "_send_audio_frames_to_clients", spy):
                task = asyncio.create_task(server._audio_rx_loop())
                try:
                    await asyncio.sleep(0.15)
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            (server.audio, server.radio, server.audio_rx_clients,
             server._audio_rx_profiles, server._pcm16_rx_encoder,
             server._rec_session, server._ft8_runtime,
             server._local_speaker) = saved
            return audio, encoder, pcm16_sent, legacy_sent

        audio, encoder, pcm16_sent, legacy_sent = asyncio.run(drive())
        # Every tick ran the conversion first, so a legacy encode that starts
        # dying mid-session cannot stop the 16 kHz pass…
        self.assertEqual(encoder.push.call_count, audio.attempts,
                         "the conversion stopped running once Opus began to die")
        # …and its frames keep reaching the wire afterwards.
        self.assertGreaterEqual(len(pcm16_sent), 2,
                                "the PCM16 group was barely fed at all: %r"
                                % (pcm16_sent,))
        self.assertGreaterEqual(len(pcm16_sent[2:]), 1,
                                "the PCM16 group stopped being served after the "
                                "Opus encode began to fail")
        # The base path is unchanged in the other direction: legacy frames went
        # out while libopus lived, and nothing at all after it died — no retry,
        # no half-built frame, nobody closed.
        self.assertEqual(len(legacy_sent), audio.fail_after,
                         "legacy sends do not match the ticks Opus survived: %r"
                         % (legacy_sent,))

    def test_a_close_that_raises_still_leaves_no_ghost_profile(self):
        """The socket is gone even when ``close()`` cannot say so.

        This is the half-open path AD-038 has to survive: the endpoint removes
        the client first and only then sends the close, so if that send raises
        — a dead TCP, a hub tunnel torn down mid-frame — a later text frame on
        the same object must not re-arm a profile nobody encodes for or ever
        closes.
        """
        import asyncio
        from unittest import mock

        import server

        class ExplodingWS:
            def __init__(self):
                self.received = [
                    {"text": '{"type":"audioCaps","profile":"pcm16"}'},
                    {"type": "websocket.disconnect"},
                ]
                self.sent_text = []

            async def accept(self):
                pass

            async def receive(self):
                return self.received.pop(0)

            async def send_text(self, text):
                self.sent_text.append(text)

            async def close(self, **kwargs):
                raise RuntimeError("peer already gone")

        saved = (server.audio_rx_clients, server.audio_tx_clients,
                 server._audio_rx_profiles, server._pcm16_rx_encoder,
                 server._auth_tokens)
        # This class is a plain TestCase and asyncio.run() does not restore the
        # loop between tests, so the process-wide client sets have to be handed
        # back by hand if an assertion fails mid-flight.  Both halves of the
        # case — the connection and the stray frame that follows it — live
        # inside this one region: replacing a module global *after* the
        # restore would leave every later test pointing at a set this case made.
        try:
            encoder = mock.Mock()
            server.audio_rx_clients = set()
            server.audio_tx_clients = set()
            server._pcm16_rx_encoder = encoder
            server._auth_tokens = {"tok": object()}
            ws = ExplodingWS()
            with mock.patch.object(server, "_token_from_ws_handshake",
                                   return_value="tok"):
                asyncio.run(server.ws_audio_rx(ws))
            self.assertIn('"accepted":true', ws.sent_text[0])
            # The close error was swallowed, but the state still went away.
            self.assertEqual(server._audio_rx_profiles, {})
            self.assertEqual(server.audio_rx_clients, set())
            self.assertEqual(encoder.reset.call_count, 1)
            self.assertIsNone(server._pcm16_rx_encoder)

            # And after that connection is finished with, a stray text frame of
            # the same shape cannot bring the profile back.
            asyncio.run(server._handle_audio_caps(
                ws, '{"type":"audioCaps","profile":"pcm16"}', tx=False))
            self.assertEqual(server._audio_rx_profiles, {},
                             "a dead socket re-registered itself as a PCM16 listener")
        finally:
            (server.audio_rx_clients, server.audio_tx_clients,
             server._audio_rx_profiles, server._pcm16_rx_encoder,
             server._auth_tokens) = saved

    def test_rx_loop_skips_broadcast_while_transmitting(self):
        """While the radio is keyed, its USB RX output is muted silence.
        Broadcasting those frames piles silent Opus into every client's
        jitter buffer, which must then DRAIN after unkey before real RX
        audio is heard.  The guard must gate encode+send only — the read,
        the recording tap and the silence watchdog stay live."""
        source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("_radio_txing = (radio is not None", source)
        # AD-039 added the terminal's pcm48 group to the same gate.
        self.assertIn("if not _radio_txing and (legacy_clients or pcm16_clients",
                      source)
        self.assertIn("or pcm48_clients):", source)
        # Each group is encoded by its own statement, each with its own
        # consequence: a shared call would let one group's exception be handled
        # as the other group's failure.
        # Opus 编码挪出事件循环（py-spy：PerMessageDeflate.encode 1.9 %、
        # RxOpusEncoder.push 9.1 %，两个都在循环里）。编码器有状态，但 RX 循环
        # 是它唯一的调用者，并且下一个 tick 之前就 await 完了。
        self.assertIn("await asyncio.to_thread(", source)
        self.assertIn("audio.encode_rx_audio, pcm_for_clients)", source)
        self.assertIn("pcm16_frames = _encode_pcm16_rx(pcm_for_clients)", source)
        self.assertIn("pcm48_frames = [bytes([AUDIO_TAG_PCM]) + pcm_for_clients]",
                      source)
        self.assertNotIn("frames, pcm16_frames = _encode_rx_for_profiles(", source)
        # The recording tap must stay BEFORE the guard (always fed).
        tap_idx = source.index("_rec_tap_rx(pcm")
        guard_idx = source.index("_radio_txing = (radio is not None")
        self.assertLess(tap_idx, guard_idx)
        # ... and the guard must sit before every group's send helper call.
        for call in ("frames, legacy_clients)", "pcm16_frames, pcm16_clients)",
                     "pcm48_frames, pcm48_clients)"):
            send_idx = source.index(call, guard_idx)
            self.assertLess(guard_idx, send_idx)

    def test_pcm16_failure_handling_cannot_touch_the_legacy_encode(self):
        """The PCM16 guard wraps the conversion alone, nothing wider.

        A ``try`` that also covered ``audio.encode_rx_audio`` would report an
        Opus death as a PCM16 failure, close healthy 16 kHz listeners with
        4004, and re-run the same failing encode inside the handler.
        """
        import ast
        from pathlib import Path

        tree = ast.parse(Path("server.py").read_text(encoding="utf-8"))
        loop = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "_audio_rx_loop")
        # The tick-wide handler is the outermost try; only inner guards count.
        outer = min(node.lineno for node in ast.walk(loop)
                    if isinstance(node, ast.Try))
        guarded = []
        for node in ast.walk(loop):
            if isinstance(node, ast.Try) and node.lineno != outer:
                # body + handlers, deliberately **not** ``finally``: a cleanup
                # arm runs even when the guarded call raised, so anything put
                # there is executed by every other group's failure too.
                parts = list(node.body) + list(node.handlers)
                names = {n.id for stmt in parts for n in ast.walk(stmt)
                         if isinstance(n, ast.Name)}
                attrs = {n.attr for stmt in parts for n in ast.walk(stmt)
                         if isinstance(n, ast.Attribute)}
                guarded.append(names | attrs)
        # Exactly one guarded region carries the PCM16 conversion — its `body`
        # only, never its finally arm — and none of them carries the legacy
        # encoder.
        pcm16_guards = [s for s in guarded if "_encode_pcm16_rx" in s]
        self.assertEqual(len(pcm16_guards), 1)
        self.assertTrue(all("encode_rx_audio" not in s for s in pcm16_guards))
        self.assertTrue(all("_close_pcm16_listeners" in s for s in pcm16_guards))


class RxProfileLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """The wire format follows the socket: connect, declare, disconnect."""

    def setUp(self):
        self.saved = (server.audio_rx_clients, server._audio_rx_profiles,
                      server._pcm16_rx_encoder, server._auth_tokens)

    def tearDown(self):
        (server.audio_rx_clients, server._audio_rx_profiles,
         server._pcm16_rx_encoder, server._auth_tokens) = self.saved

    async def test_disconnecting_the_last_pcm16_socket_resets_the_converter(self):
        """A new subscriber must not inherit the previous session's filter tails."""
        from unittest import mock

        class WS:
            def __init__(self):
                self.accepted = False
                self.received = [{"text": '{"type":"audioCaps","profile":"pcm16"}'},
                                 {"type": "websocket.disconnect"}]
                self.sent_text = []

            async def accept(self):
                self.accepted = True

            async def receive(self):
                return self.received.pop(0)

            async def send_text(self, text):
                self.sent_text.append(text)

            async def close(self, **kwargs):
                pass

        encoder = mock.Mock()
        server._pcm16_rx_encoder = encoder
        server._auth_tokens = {"tok": object()}
        # Own the client set for the duration: the endpoint adds and removes
        # the socket there, and a stale entry from another module would look
        # like a leak.
        server.audio_rx_clients = set()
        ws = WS()
        ws.query_params = {}
        with mock.patch.object(server, "_token_from_ws_handshake",
                              return_value="tok"):
            await server.ws_audio_rx(ws)
        self.assertIn('"accepted":true', ws.sent_text[0])
        self.assertEqual(encoder.reset.call_count, 1)
        self.assertIsNone(server._pcm16_rx_encoder)
        self.assertEqual(server._audio_rx_profiles, {})
        self.assertEqual(server.audio_rx_clients, set())

    async def test_an_undeclared_socket_stays_on_the_legacy_group(self):
        ws = object()
        server.audio_rx_clients = {ws}
        pcm16 = {w for w in server.audio_rx_clients
                 if server._audio_rx_profiles.get(w) == PROFILE_PCM16}
        self.assertEqual(pcm16, set())


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


class LoopbackDeviceGuardTests(unittest.TestCase):
    """A loopback capture endpoint is a silent RX that looks like a broken
    server.

    Field report 2026-11-11 (bundle 20261111-212640-e397, Xiegu G7250,
    Windows 11): the connection dialog had saved
    ``MRRC_AUDIO_RX_DEVICE="主声音捕获驱动程序"`` / ``"立体声混音 (Realtek HD
    Audio Stereo input)"`` — both computer loopbacks that open fine and
    carry no radio audio — and the operator reported "电脑没有接收到电台的
    声音" while the server log showed only a missing-device warning.
    """

    def _make_handler(self, devices, rx_lock=None, tx_lock=None):
        import config
        from audio_handler import AudioHandler
        config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE = rx_lock or "", tx_lock or ""
        self.addCleanup(setattr, config, "AUDIO_RX_DEVICE", "")
        self.addCleanup(setattr, config, "AUDIO_TX_DEVICE", "")
        h = AudioHandler.__new__(AudioHandler)
        h.rx_device = None
        h.tx_device = None
        h._pa = _FakePyAudio(devices)
        return h

    def test_name_predicate_covers_the_windows_endpoints(self):
        from audio_handler import is_loopback_device_name as loop
        for name in ("主声音捕获驱动程序", "主声音驱动程序",
                     "立体声混音 (Realtek HD Audio Stereo input)",
                     "Stereo Mix (Realtek(R) Audio)", "What U Hear"):
            self.assertTrue(loop(name), name)
        for name in ("麦克风 (USB Audio Device)", "USB Audio CODEC",
                     "麦克风 (Realtek(R) Audio)", ""):
            self.assertFalse(loop(name), name)

    def test_rx_auto_detect_skips_a_loopback_input(self):
        h = self._make_handler([
            _dev("立体声混音 (Realtek HD Audio Stereo input)", inputs=2),
            _dev("麦克风 (Realtek(R) Audio)", inputs=2),
        ])
        self.assertEqual(h._find_rx_device(), 1)

    def test_rx_returns_none_when_only_loopbacks_exist(self):
        # None makes start_rx log "No audio input device found" instead of
        # opening a stream that is guaranteed silent.
        h = self._make_handler([
            _dev("主声音捕获驱动程序", inputs=2),
            _dev("立体声混音 (Realtek HD Audio Stereo input)", inputs=2),
        ])
        self.assertIsNone(h._find_rx_device())

    def test_an_explicit_loopback_choice_is_honoured_but_warned_about(self):
        h = self._make_handler([
            _dev("立体声混音 (Realtek HD Audio Stereo input)", inputs=2),
            _dev("麦克风 (USB Audio Device)", inputs=1),
        ], rx_lock="立体声混音")
        with self.assertLogs("mrrc.audio", level="WARNING") as logs:
            self.assertEqual(h._find_rx_device(), 0)
        self.assertTrue(any("loopback" in line for line in logs.output), logs.output)

    def test_tx_auto_detect_skips_a_loopback_output(self):
        h = self._make_handler([
            _dev("主声音驱动程序", outputs=2),
            _dev("扬声器 (Realtek(R) Audio)", inputs=2, outputs=2),
        ])
        self.assertEqual(h._find_tx_device(), 1)

    def test_a_device_that_just_failed_to_open_is_not_a_missing_device(self):
        """The -9999 retry path excludes the index it just tried; the log used
        to say "not found", which sent the operator hunting for a device that
        was in the picker all along (2026-11-11 bundle)."""
        h = self._make_handler([
            _dev("麦克风 (USB Audio Device)", inputs=1),
            _dev("麦克风 (Realtek(R) Audio)", inputs=2),
        ], rx_lock="USB Audio Device")
        with self.assertLogs("mrrc.audio", level="WARNING") as logs:
            self.assertEqual(h._find_rx_device(exclude={0}), 1)
        joined = "\n".join(logs.output)
        self.assertIn("just failed to open", joined)
        self.assertNotIn("not found", joined)


class RestartRxTests(unittest.TestCase):
    """TX playback can degrade the long-lived RX capture stream, so the
    stream is reopened on every TX→RX transition."""

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

    def test_macos_reopens_stop_then_start(self):
        h = self._make_handler()
        calls = []
        h.stop_rx = lambda: calls.append("stop")
        h.start_rx = lambda: (calls.append("start"), True)[1]
        h.restart_rx()
        self.assertEqual(calls, ["stop", "start"])

    def test_restart_does_not_require_windows_platform(self):
        from unittest.mock import patch
        h = self._make_handler()
        calls = []
        h.stop_rx = lambda: calls.append("stop")
        h.start_rx = lambda: (calls.append("start"), True)[1]
        with patch("sys.platform", "darwin"):
            h.restart_rx()
        self.assertEqual(calls, ["stop", "start"])

    def test_skipped_when_rx_not_running(self):
        h = self._make_handler()
        h._rx_running = False
        called = []
        h.stop_rx = lambda: called.append("stop")
        h.restart_rx()
        self.assertEqual(called, [])

    def test_failed_reopen_logs_warning(self):
        h = self._make_handler()
        calls = []
        h.stop_rx = lambda: calls.append("stop")
        h.start_rx = lambda: (calls.append("start"), False)[1]
        h.restart_rx()
        self.assertEqual(calls, ["stop", "start"])

    def test_restart_settle_is_short(self):
        """The post-stop settle must not pad the TX→RX gap: stop_tx already
        sleeps 20 ms before releasing PTT, so a 50 ms settle here doubled
        the dead air on every release."""
        from unittest.mock import patch
        import audio_handler as ah
        h = self._make_handler()
        h.stop_rx = lambda: None
        h.start_rx = lambda: True
        with patch("audio_handler.time.sleep") as sl:
            h.restart_rx()
        slept = [c.args[0] for c in sl.call_args_list]
        self.assertTrue(slept and all(s <= ah.RX_REOPEN_SETTLE_S for s in slept),
                        slept)


class RxCachedDeviceHintTests(unittest.TestCase):
    """restart_rx() runs after every TX→RX; _find_rx_device() re-scans the
    whole PortAudio device list (up to 4 tiers of enumeration), a large
    slice of the measured ~270 ms reopen.  start_rx() must reuse the
    (index, name) hint recorded by the last successful open, falling back
    to the full scan when the hint is stale (USB re-enumeration)."""

    def _make_pa(self, devices):
        class FakePA:
            def __init__(self):
                self._devices = devices
                self.open_kwargs: dict = {}

            def get_device_count(self):
                return max(self._devices) + 1

            def get_device_info_by_index(self, i):
                if i not in self._devices:
                    raise IOError("invalid device index")
                return self._devices[i]

            def open(self, **kwargs):
                self.open_kwargs = dict(kwargs)
                return object()

        return FakePA()

    def _make_handler(self, pa):
        from audio_handler import AudioHandler
        h = AudioHandler.__new__(AudioHandler)
        h._pa = pa
        h._rx_running = False
        h._rx_stream = None
        h.rx_device = None
        h._radio_hints = ("ft-710", "ft710", "yaesu")
        h._rx_dev_rate = 44100
        h._rx_chunk = 882
        h._reinit_pyaudio = lambda: None
        h._device_summary = lambda *a, **kw: "fake"
        return h

    def test_start_rx_reuses_cached_device_without_rescan(self):
        pa = self._make_pa({3: {"name": "USB Audio CODEC",
                                "maxInputChannels": 1}})
        h = self._make_handler(pa)
        h._rx_dev_hint = (3, "USB Audio CODEC")
        scanned = []
        h._find_rx_device = lambda exclude=None: scanned.append(1) or 3
        self.assertTrue(h.start_rx())
        self.assertEqual(scanned, [])  # fast path: no device-list rescan
        self.assertEqual(pa.open_kwargs["input_device_index"], 3)

    def test_start_rx_refreshes_hint_after_open(self):
        pa = self._make_pa({3: {"name": "USB Audio CODEC",
                                "maxInputChannels": 1}})
        h = self._make_handler(pa)
        h._rx_dev_hint = None
        h._find_rx_device = lambda exclude=None: 3
        self.assertTrue(h.start_rx())
        self.assertEqual(h._rx_dev_hint, (3, "USB Audio CODEC"))

    def test_start_rx_stale_hint_falls_back_to_rescan(self):
        """USB re-enumeration renamed the device at the hinted index —
        the hint must be rejected and the full scan must run."""
        pa = self._make_pa({3: {"name": "Some Other Mic",
                                "maxInputChannels": 2}})
        h = self._make_handler(pa)
        h._rx_dev_hint = (3, "USB Audio CODEC")
        scanned = []
        h._find_rx_device = lambda exclude=None: scanned.append(1) or 3
        self.assertTrue(h.start_rx())
        self.assertEqual(scanned, [1])
        self.assertEqual(pa.open_kwargs["input_device_index"], 3)


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
            pa = h._pa
            assert pa is not None
            self.assertEqual(pa.open_calls[0]["output_device_index"], 0)
            self.assertEqual(pa.open_calls[0]["rate"], 44100)
            self.assertEqual(pa.open_calls[0]["frames_per_buffer"], 882)
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
            pa = h._pa
            assert pa is not None
            self.assertEqual(pa.open_calls[0]["rate"], 44100)
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

    def test_reinit_closes_live_rx_stream_before_terminate(self):
        """Pa_Terminate must never run while the RX stream is still open.

        Field report 2026-10-16 (bundle 20261016-230940-40c1): terminating
        PortAudio underneath the RX reader killed the process natively — no
        Python traceback — and the radio, keyed first, never got its TX0.
        """
        events = []

        class _Stream:
            def stop_stream(self):
                events.append("rx.stop")

            def close(self):
                events.append("rx.close")

        class _PA:
            def terminate(self):
                events.append("pa.terminate")

        h = self._make_handler(_PA())
        h._rx_stream = _Stream()
        h._rx_running = True
        h._init_pyaudio = lambda: events.append("pa.init")

        h._reinit_pyaudio()

        self.assertIn("rx.close", events)
        self.assertLess(events.index("rx.close"), events.index("pa.terminate"))
        self.assertIsNone(h._rx_stream)
        # RX stays marked running so _audio_rx_loop's stall watchdog rebuilds it.
        self.assertTrue(h._rx_running)

    def test_reinit_closes_live_tx_stream_before_terminate(self):
        events = []

        class _Stream:
            def stop_stream(self):
                events.append("tx.stop")

            def close(self):
                events.append("tx.close")

        class _PA:
            def terminate(self):
                events.append("pa.terminate")

        h = self._make_handler(_PA())
        h._tx_stream = _Stream()
        h._init_pyaudio = lambda: events.append("pa.init")

        h._reinit_pyaudio()

        self.assertLess(events.index("tx.close"), events.index("pa.terminate"))
        self.assertIsNone(h._tx_stream)

    def test_read_rx_chunk_skips_while_rebuild_holds_the_lock(self):
        """A rebuild in flight yields no audio, not a read on a dying stream."""

        class _Boom:
            def get_read_available(self):
                raise AssertionError("read raced a stream rebuild")

        h = self._make_handler(object())
        h._rx_stream = _Boom()
        h._rx_running = True
        h._rx_chunk = 960

        lock = h._stream_lock()
        lock.acquire()
        try:
            self.assertIsNone(h.read_rx_chunk())
        finally:
            lock.release()

    def test_stop_rx_closes_stream_and_nulls_it(self):
        closed = []

        class _Stream:
            def stop_stream(self):
                pass

            def close(self):
                closed.append(1)

        h = self._make_handler(object())
        h._rx_stream = _Stream()
        h._rx_running = True

        h.stop_rx()

        self.assertEqual(closed, [1])
        self.assertIsNone(h._rx_stream)
        self.assertFalse(h._rx_running)

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


class ParameterizedRateTests(unittest.TestCase):
    """Phase 4a: AudioHandler device rates come from backend capabilities.
    48000/48000 (IC-7300) must skip resampling entirely; 44100 (FT-710
    default) keeps the 960↔882 resample path byte-identical."""

    def _rx_handler(self, dev_rate, chunk):
        from audio_handler import AudioHandler
        h = AudioHandler.__new__(AudioHandler)
        h._rx_dev_rate = dev_rate
        h._rx_chunk = chunk
        h._rx_running = True
        return h

    @staticmethod
    def _rx_stream(pcm):
        class FakeRxStream:
            def get_read_available(self):
                return len(pcm) // 2

            def read(self, n, exception_on_overflow=False):
                return pcm[: n * 2]
        return FakeRxStream()

    def test_rx_48k_passthrough_skips_resample(self):
        from unittest.mock import patch
        pcm = b"\x01\x02" * 960          # 960 samples @ 48 kHz = 20 ms
        h = self._rx_handler(48000, 960)
        h._rx_stream = self._rx_stream(pcm)
        with patch("audio_handler.resample_pcm") as rs:
            out = h.read_rx_chunk()
        rs.assert_not_called()
        self.assertEqual(out, pcm)

    def test_rx_44k1_resamples_to_48k(self):
        from unittest.mock import patch
        pcm = b"\x01\x02" * 882          # 882 samples @ 44.1 kHz = 20 ms
        h = self._rx_handler(44100, 882)
        h._rx_stream = self._rx_stream(pcm)
        with patch("audio_handler.resample_pcm",
                   wraps=__import__("audio_resample").resample_pcm) as rs:
            out = h.read_rx_chunk()
        rs.assert_called_once_with(pcm, 44100, 48000)
        assert out is not None
        self.assertEqual(len(out), 960 * 2)

    def _tx_handler(self, dev_rate):
        import threading
        from collections import deque
        from audio_handler import AudioHandler
        h = AudioHandler.__new__(AudioHandler)
        h._tx_dev_rate = dev_rate
        h._tx_stream = object()
        h._tx_queue = deque()
        h._tx_queued_bytes = 0
        h._tx_peak = 0
        h._tx_lock = threading.Lock()
        return h

    def test_tx_48k_passthrough_skips_resample(self):
        from unittest.mock import patch
        frame = b"\x01\x02" * 960
        h = self._tx_handler(48000)
        with patch("audio_handler.resample_pcm") as rs:
            h.feed_tx_audio(frame)
        rs.assert_not_called()
        self.assertEqual(h._tx_queue[0], frame)
        self.assertEqual(h._tx_queued_bytes, len(frame))

    def test_tx_44k1_resamples_to_device_rate(self):
        from unittest.mock import patch
        frame = b"\x01\x02" * 960
        h = self._tx_handler(44100)
        with patch("audio_handler.resample_pcm",
                   wraps=__import__("audio_resample").resample_pcm) as rs:
            h.feed_tx_audio(frame)
        rs.assert_called_once_with(frame, 48000, 44100)
        self.assertEqual(len(h._tx_queue[0]), 882 * 2)

    def test_tx_pcm16_uses_streaming_direct_device_bridge(self):
        from unittest.mock import patch
        frame = b"\x01\x02" * 320
        h = self._tx_handler(44100)
        with patch("audio_handler.StreamingPcmResampler") as cls:
            cls.return_value.process.return_value = b"\x03\x04" * 882
            h.feed_tx_audio(frame, input_rate=16000)
        cls.assert_called_once_with(16000, 44100)
        self.assertEqual(len(h._tx_queue[0]), 882 * 2)

    def test_legacy_tx_does_not_construct_streaming_bridge(self):
        from unittest.mock import patch
        h = self._tx_handler(44100)
        with patch("audio_handler.StreamingPcmResampler") as cls:
            h.feed_tx_audio(b"\x01\x02" * 960)
        cls.assert_not_called()

    def test_start_tx_opens_stream_at_configured_48k(self):
        import config
        old_rx, old_tx = config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE
        config.AUDIO_RX_DEVICE = ""
        config.AUDIO_TX_DEVICE = ""
        try:
            import threading
            from collections import deque
            from audio_handler import AudioHandler
            h = AudioHandler.__new__(AudioHandler)
            h._pa = _FakePyAudioOpen(
                [_dev("USB Audio Device", inputs=1, outputs=2, rate=48000)])
            h.rx_device = None
            h.tx_device = None
            h._tx_stream = None
            h._tx_queue = deque()
            h._tx_queued_bytes = 0
            h._tx_primed = False
            h._tx_lock = threading.Lock()
            h._tx_write_lock = threading.Lock()
            h._tx_dev_rate = 48000
            h._tx_chunk = 960
            self.assertTrue(h.start_tx())
            self.assertEqual(h._tx_rate, 48000)
            pa = h._pa
            assert pa is not None
            self.assertEqual(pa.open_calls[0]["rate"], 48000)
            self.assertEqual(pa.open_calls[0]["frames_per_buffer"], 960)
            self.assertEqual(h._tx_prebuffer_bytes, 48000 * 2 * 60 // 1000)
            self.assertEqual(h._tx_max_buffer_bytes, 48000 * 2 * 400 // 1000)
        finally:
            config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE = old_rx, old_tx


class CustomNameHintsTests(unittest.TestCase):
    """Phase 4a: device auto-detect name tier honors backend-provided hints
    (IC-7300) without requiring FT-710 substrings; the generic USB-codec
    tier (with full-duplex preference) must still find "USB Audio CODEC"."""

    IC7300_HINTS = ("ic-7300", "ic7300", "usb audio codec", "usb audio device")

    def _make_handler(self, devices, hints):
        """Build via the real __init__ (PyAudio/Opus patched out) so the
        hint-filtering wiring is exercised, then attach a fake PyAudio."""
        from unittest.mock import patch
        import audio_handler
        with patch.object(audio_handler, "HAS_PYAUDIO", False), \
             patch.object(audio_handler, "RxOpusEncoder",
                          side_effect=RuntimeError("no opus in test")):
            h = audio_handler.AudioHandler(name_hints=hints)
        h._pa = _FakePyAudio(devices)
        return h

    def test_generic_usb_hints_are_stripped_from_radio_tier(self):
        from unittest.mock import patch
        import audio_handler
        with patch.object(audio_handler, "HAS_PYAUDIO", False), \
             patch.object(audio_handler, "RxOpusEncoder",
                          side_effect=RuntimeError("no opus in test")):
            h = audio_handler.AudioHandler(name_hints=self.IC7300_HINTS)
        self.assertEqual(h._radio_hints, ("ic-7300", "ic7300"))

    def test_defaults_preserve_ft710_behavior(self):
        from unittest.mock import patch
        import audio_handler
        with patch.object(audio_handler, "HAS_PYAUDIO", False), \
             patch.object(audio_handler, "RxOpusEncoder",
                          side_effect=RuntimeError("no opus in test")):
            h = audio_handler.AudioHandler()
        self.assertEqual(h._rx_dev_rate, 44100)
        self.assertEqual(h._tx_dev_rate, 44100)
        self.assertEqual(h._rx_chunk, 882)
        self.assertEqual(h._tx_chunk, 882)
        self.assertEqual(h._radio_hints, ("ft-710", "ft710", "yaesu"))

    def test_ic7300_name_matches_radio_tier(self):
        h = self._make_handler([
            _dev("Built-in Microphone", inputs=2),
            _dev("IC-7300", inputs=1, outputs=2),
        ], self.IC7300_HINTS)
        self.assertEqual(h._find_rx_device(), 1)

    def test_usb_codec_found_without_radio_name_in_hints(self):
        # Requirement: with IC-7300 hints a plain "USB Audio CODEC" device
        # must still be found (generic USB tier, no "ft-710" required).
        h = self._make_handler([
            _dev("Built-in Microphone", inputs=2),
            _dev("USB Audio CODEC", inputs=1, outputs=2),
        ], self.IC7300_HINTS)
        self.assertEqual(h._find_rx_device(), 1)
        self.assertEqual(h._find_tx_device(), 1)

    def test_ft710_name_does_not_match_ic7300_hints(self):
        # A stray FT-710-named device must not win tier 2 under IC-7300
        # hints; the codec tier takes over instead.
        h = self._make_handler([
            _dev("YAESU FT-710 USB Audio", inputs=1, outputs=2),
            _dev("USB Audio CODEC", inputs=1, outputs=2),
        ], self.IC7300_HINTS)
        self.assertEqual(h._find_rx_device(), 1)

    def test_full_duplex_preference_kept_with_custom_hints(self):
        # Input-only codec interloper first, full-duplex codec (the radio)
        # second — the full-duplex preference must survive custom hints.
        h = self._make_handler([
            _dev("USB Audio CODEC", inputs=1),
            _dev("USB Audio Device", inputs=1, outputs=2),
        ], self.IC7300_HINTS)
        self.assertEqual(h._find_rx_device(), 1)

    def test_default_hints_match_yaesu_name(self):
        from audio_handler import AudioHandler
        h = AudioHandler.__new__(AudioHandler)   # class-level FT-710 defaults
        h.rx_device = None
        h.tx_device = None
        h._pa = _FakePyAudio([
            _dev("Built-in Microphone", inputs=2),
            _dev("YAESU FT-710 USB Audio", inputs=1, outputs=2),
        ])
        self.assertEqual(h._find_rx_device(), 1)
        self.assertEqual(h._find_tx_device(), 1)


class CapabilitiesAudioWiringTests(unittest.TestCase):
    """Phase 4a: server.py builds AudioHandler from backend capabilities;
    FT-710 capabilities equal the historical AudioHandler defaults."""

    def test_ft710_capabilities_match_audio_defaults(self):
        from backends import create_backend
        import audio_handler
        caps = create_backend("ft710", port="/dev/null").capabilities
        self.assertEqual(caps.audio_rx_rate, audio_handler.RX_SAMPLE_RATE)
        self.assertEqual(caps.audio_tx_rate, audio_handler.TX_SAMPLE_RATE)
        for hint in audio_handler.RADIO_NAME_HINTS:
            self.assertIn(hint, caps.audio_name_hints)
        for hint in audio_handler.USB_AUDIO_NAME_HINTS:
            self.assertIn(hint, caps.audio_name_hints)

    def test_ic7300_capabilities_are_48k(self):
        from backends import create_backend
        caps = create_backend("ic7300", port="/dev/null").capabilities
        self.assertEqual(caps.audio_rx_rate, 48000)
        self.assertEqual(caps.audio_tx_rate, 48000)

    def test_server_constructs_audio_from_capabilities(self):
        source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("AudioHandler(rx_rate=_caps.audio_rx_rate", source)
        self.assertIn("tx_rate=_caps.audio_tx_rate", source)
        self.assertIn("name_hints=tuple(_caps.audio_name_hints)", source)


class ConfiguredNameHostApiPreferenceTests(unittest.TestCase):
    """V2.33: the connection dialog lists every host-API duplicate of the
    radio codec ("麦克风 (USB Audio CODEC )" on MME vs "麦克风 (USB Audio
    CODEC)" on WDM-KS — one invisible trailing space apart). Saving the
    WDM-KS name locks device selection to an entry whose open() fails with
    -9999 on the affected Windows rigs (field log 2026-09-10). When a
    configured name matches several host-API duplicates, prefer a
    non-WDM-KS entry."""

    DEVICES = [
        _dev("Microsoft Sound Mapper - Input", inputs=2, host_api=0),
        _dev("麦克风 (USB Audio CODEC )", inputs=2, host_api=0),    # MME
        _dev("麦克风 (USB Audio CODEC )", inputs=2, host_api=1),    # WASAPI
        _dev("麦克风 (USB Audio CODEC)", inputs=2, host_api=2),     # WDM-KS
        _dev("扬声器 (USB Audio CODEC )", outputs=2, host_api=0),   # MME
        _dev("扬声器 (USB Audio CODEC )", outputs=2, host_api=1),   # WASAPI
        _dev("扬声器 (USB Audio CODEC)", outputs=2, host_api=2),    # WDM-KS
    ]
    HOST_APIS = ["MME", "Windows WASAPI", "Windows WDM-KS"]

    def setUp(self):
        import config
        self._old = (config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE)

    def tearDown(self):
        import config
        config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE = self._old

    def _handler(self):
        from audio_handler import AudioHandler
        h = AudioHandler.__new__(AudioHandler)
        h._pa = _FakePyAudio(self.DEVICES, self.HOST_APIS)
        h.rx_device = None
        h.tx_device = None
        h._radio_hints = ("ic-7300", "icom")
        return h

    def test_rx_prefers_non_wdmks_duplicate(self):
        # A name WITHOUT the closing paren matches all host-API duplicates
        # (the MME entries carry an invisible trailing space before ")"),
        # and the non-WDM-KS one must win.
        import config
        config.AUDIO_RX_DEVICE = "麦克风 (USB Audio CODEC"
        self.assertEqual(self._handler()._find_rx_device(), 1)  # MME, not [3] WDM-KS

    def test_tx_prefers_non_wdmks_duplicate(self):
        import config
        config.AUDIO_TX_DEVICE = "扬声器 (USB Audio CODEC"
        self.assertEqual(self._handler()._find_tx_device(), 4)  # MME speaker

    def test_exclude_skips_failed_candidate(self):
        # The dialog-saved exact name ("USB Audio CODEC)") only matches the
        # WDM-KS duplicate (the MME/WASAPI names carry a trailing space
        # before ")"). Once it fails and is excluded, the codec-hints tier
        # must take over and resolve a non-WDM-KS duplicate.
        import config
        config.AUDIO_RX_DEVICE = "麦克风 (USB Audio CODEC)"
        h = self._handler()
        self.assertEqual(h._find_rx_device(), 3)                 # exact name → WDM-KS
        self.assertEqual(h._find_rx_device(exclude={3}), 1)      # MME via codec tier
        self.assertEqual(h._find_rx_device(exclude={1, 3}), 2)   # WASAPI next
        self.assertEqual(h._find_rx_device(exclude={1, 2, 3}), 0)  # fallback tier


class WdmksRecoveryEndToEndTests(unittest.TestCase):
    """V2.33 field log 2026-09-10: the connection-dialog-saved name matched
    only the WDM-KS duplicate; every open failed with -9999 (6× per PTT)
    and audio never came up. With the exclude retry, the second resolution
    must land on a non-WDM-KS duplicate and the stream must open."""

    DEVICES = [
        _dev("Microsoft Sound Mapper - Input", inputs=2, host_api=0),
        _dev("麦克风 (USB Audio CODEC )", inputs=2, host_api=0),    # MME — works
        _dev("麦克风 (USB Audio CODEC )", inputs=2, host_api=1),    # WASAPI
        _dev("麦克风 (USB Audio CODEC)", inputs=2, host_api=2),     # WDM-KS — -9999
        _dev("扬声器 (USB Audio CODEC )", outputs=2, host_api=0),   # MME — works
        _dev("扬声器 (USB Audio CODEC )", outputs=2, host_api=1),   # WASAPI
        _dev("扬声器 (USB Audio CODEC)", outputs=2, host_api=2),    # WDM-KS — -9999
    ]
    HOST_APIS = ["MME", "Windows WASAPI", "Windows WDM-KS"]

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

    class _SelectiveFailPyAudio(_FakePyAudioOpen):
        """Opens fail with the -9999 signature on WDM-KS indices only."""

        wdmks_indices = frozenset({3, 6})

        def open(self, **kwargs):
            self.open_calls.append(kwargs)
            if (kwargs.get("input_device_index") in self.wdmks_indices
                    or kwargs.get("output_device_index") in self.wdmks_indices):
                raise OSError("[Errno -9999] Unanticipated host error")
            return _FakeTxStream()

    def _patch_devices(self, rx, tx):
        import config
        self._old = (config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE)
        config.AUDIO_RX_DEVICE = rx
        config.AUDIO_TX_DEVICE = tx

    def _restore_devices(self):
        import config
        config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE = self._old

    def test_rx_recovers_on_mme_duplicate_after_wdmks_failure(self):
        self._patch_devices("麦克风 (USB Audio CODEC)", "")
        try:
            pa = self._SelectiveFailPyAudio(self.DEVICES, self.HOST_APIS)
            h = self._make_handler(pa)
            h._reinit_pyaudio = lambda: None  # re-init covered separately
            self.assertTrue(h.start_rx())
            self.assertTrue(h._rx_running)
            # First open hit the WDM-KS duplicate; the retry (after exclude)
            # landed on the working MME one.
            self.assertEqual(pa.open_calls[0]["input_device_index"], 3)
            self.assertEqual(pa.open_calls[-1]["input_device_index"], 1)
        finally:
            self._restore_devices()

    def test_tx_recovers_on_mme_duplicate_after_wdmks_failure(self):
        self._patch_devices("", "扬声器 (USB Audio CODEC)")
        try:
            pa = self._SelectiveFailPyAudio(self.DEVICES, self.HOST_APIS)
            h = self._make_handler(pa)
            h._reinit_pyaudio = lambda: None
            from unittest.mock import patch
            with patch("sys.platform", "win32"):
                self.assertTrue(h.start_tx())
            self.assertEqual(pa.open_calls[0]["output_device_index"], 6)
            self.assertEqual(pa.open_calls[-1]["output_device_index"], 4)
        finally:
            self._restore_devices()


class DeviceExcludeRetryTests(unittest.TestCase):
    """V2.33: when the selected device fails to open (-9999), the reinit
    retry must resolve a *different* candidate instead of re-selecting the
    same locked entry (field log 2026-09-10: 6 identical failures on the
    WDM-KS duplicate, then give-up, on every PTT)."""

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

    def _patch_devices(self):
        import config
        self._old = (config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE)
        config.AUDIO_RX_DEVICE = ""
        config.AUDIO_TX_DEVICE = ""

    def _restore_devices(self):
        import config
        config.AUDIO_RX_DEVICE, config.AUDIO_TX_DEVICE = self._old

    def test_start_rx_excludes_failed_device_on_retry(self):
        self._patch_devices()
        try:
            h = self._make_handler(_FailingOpenPyAudio([_dev("Mic", inputs=1)]))
            find_calls = []

            def fake_find(exclude=None):
                find_calls.append(set(exclude or ()))
                return 5

            h._find_rx_device = fake_find
            h._reinit_pyaudio = lambda: None
            ok = h.start_rx()
            self.assertFalse(ok)                        # open keeps failing
            self.assertEqual(find_calls, [set(), {5}])  # retry excludes failed dev
        finally:
            self._restore_devices()

    def test_start_tx_excludes_failed_device_on_retry(self):
        self._patch_devices()
        try:
            h = self._make_handler(_FailingOpenPyAudio([_dev("Spk", outputs=2)]))
            find_calls = []

            def fake_find(exclude=None):
                find_calls.append(set(exclude or ()))
                return 6

            h._find_tx_device = fake_find
            h._reinit_pyaudio = lambda: None
            ok = h.start_tx()
            self.assertFalse(ok)                        # open keeps failing
            self.assertEqual(find_calls, [set(), {6}])  # retry excludes failed dev
        finally:
            self._restore_devices()


if __name__ == "__main__":
    unittest.main()


class TxQueueDepthTests(unittest.TestCase):
    """Server-side audio producers (the CQ player) need the queue depth.

    They feed one 20 ms frame per tick while the device queue stays shallow, so
    a slow sound card makes the producer wait instead of hitting the drop cap.
    """

    def test_tx_queue_frames_reports_queued_chunks(self):
        import audio_handler
        h = audio_handler.AudioHandler()
        h._tx_stream = object()                      # pretend the output is open
        self.assertEqual(h.tx_queue_frames(), 0)
        h.feed_tx_audio(b"\x00\x00" * 480)           # one 10 ms chunk
        h.feed_tx_audio(b"\x00\x00" * 480)
        self.assertEqual(h.tx_queue_frames(), 2)

    def test_tx_queue_frames_is_zero_without_a_stream(self):
        import audio_handler
        h = audio_handler.AudioHandler()
        h.feed_tx_audio(b"\x00\x00" * 480)           # dropped: stream not open
        self.assertEqual(h.tx_queue_frames(), 0)

    def test_tx_queue_frames_is_zero_after_stop_tx(self):
        import audio_handler
        h = audio_handler.AudioHandler()
        h._tx_stream = object()
        h.feed_tx_audio(b"\x00\x00" * 480)
        h._tx_stream = None                          # device closed
        self.assertEqual(h.tx_queue_frames(), 0)


class RxHostBufferTests(unittest.TestCase):
    """The capture stream asks for config.RX_HOST_BUFFER_MS of host buffer.

    ``frames_per_buffer`` is the *host buffer*, not the read size: the read gate
    stays at one 20 ms chunk (RX_CHUNK_MS), but the device needs slack behind it.
    One period of slack is what the box's ALSA/xhci path cannot survive — the
    capture interface stops while PyAudio still reports an open, error-free
    stream (W103D, 2026-10-11: 4% coverage, then permanent silence).
    """

    @staticmethod
    def _handler(rx_rate=44100, name="USB Audio Device: - (hw:1,0)"):
        import audio_handler
        h = audio_handler.AudioHandler(rx_rate=rx_rate, tx_rate=rx_rate)
        h._pa = _FakePyAudioOpen([_dev(name, inputs=1, outputs=0, rate=rx_rate)])
        return h

    @staticmethod
    def _open_calls(handler):
        """The fake PortAudio's recorded open() calls.

        ``_pa`` is Optional on the real object; this fixture always sets it,
        and saying so once keeps the three buffer assertions readable.
        """
        assert handler._pa is not None
        return handler._pa.open_calls

    def test_100ms_of_buffer_is_4410_frames_at_44k1(self):
        from unittest.mock import patch
        h = self._handler()
        with patch("config.RX_HOST_BUFFER_MS", 100):
            self.assertTrue(h.start_rx())
        call = self._open_calls(h)[-1]
        self.assertEqual(call["frames_per_buffer"], 4410)
        self.assertEqual(call["rate"], 44100)      # the codec domain is not touched
        self.assertEqual(call["channels"], 1)
        h.stop_rx()

    def test_a_48k_backend_scales_with_its_own_device_rate(self):
        """The IC-7300's codec is 48 kHz end-to-end; the buffer follows it."""
        from unittest.mock import patch
        h = self._handler(rx_rate=48000, name="USB Audio Device")
        with patch("config.RX_HOST_BUFFER_MS", 100):
            self.assertTrue(h.start_rx())
        self.assertEqual(self._open_calls(h)[-1]["frames_per_buffer"], 4800)
        h.stop_rx()

    def test_a_shallow_setting_never_goes_below_one_read_chunk(self):
        """A buffer shallower than the read gate would starve the loop."""
        from unittest.mock import patch
        h = self._handler()
        with patch("config.RX_HOST_BUFFER_MS", 5):
            self.assertTrue(h.start_rx())
        self.assertEqual(self._open_calls(h)[-1]["frames_per_buffer"], h._rx_chunk)
        h.stop_rx()


class RxStallWatchdogTests(unittest.TestCase):
    """A capture that stops delivering must be visible and self-healing.

    ``read_rx_chunk()`` returns None for "nothing yet" and for "this stream is
    dead" alike, so before this watchdog a stalled capture was silent at both
    ends: the loop kept its 50 Hz pace, the listener heard nothing, and no log
    line existed. Real case: the W103D's ALSA capture stops on an overflow while
    PyAudio still reports an open, error-free stream (2026-10-11).
    """

    def test_a_stalled_capture_is_reported_and_reopened(self):
        import asyncio
        import contextlib
        import server

        reopened = []

        class StalledCapture:
            _rx_running = True
            _rx_dev_rate = 44100
            _rx_last_peak = 0
            opus_enabled = True

            def read_rx_chunk(self):
                return None                      # the interface stopped: no PCM, forever

            def note_rx_chunk(self, pcm):
                pass

            def rx_silent_seconds(self):
                return 0.0

            def restart_rx(self):
                reopened.append(True)

        async def drive():
            saved = (server.audio, server.audio_rx_clients, server.RX_STALL_ALERT_S,
                     server.RX_FIRST_PCM_GRACE_S, server._rx_restart_task)
            server.audio = StalledCapture()
            server.audio_rx_clients = {object()}
            server.RX_STALL_ALERT_S = 0.2
            server.RX_FIRST_PCM_GRACE_S = 0.0
            server._rx_restart_task = None
            task = asyncio.create_task(server._audio_rx_loop())
            try:
                with self.assertLogs("mrrc", level="WARNING") as logs:
                    await asyncio.sleep(0.7)
                    await asyncio.sleep(0.1)   # let asyncio.to_thread finish the reopen
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                (server.audio, server.audio_rx_clients, server.RX_STALL_ALERT_S,
                 server.RX_FIRST_PCM_GRACE_S, server._rx_restart_task) = saved
            return logs

        logs = asyncio.run(drive())
        self.assertTrue(any("RX capture stalled" in line for line in logs.output), logs.output)
        self.assertTrue(reopened, "the watchdog must reopen the stalled capture")

    def test_a_freshly_opened_stream_gets_its_first_pcm_grace(self):
        """The FT-991A needs ~5 s to wake its ALSA capture — do not call it dead.

        Measured (BG7MWR W103D + FT-991A, 2026-11-01, v1.28.28): the stream
        reports open at 12:08:12.777 and the first PCM lands at 12:08:18.2, so a
        3 s watchdog fired first, logged "capture stalled" and reopened a stream
        that was merely still waking up — on every start-up, and the reopen cost
        the FT8 tap a wall-clock re-anchor. The grace is only for a stream that
        has not delivered its first byte yet; after that a stall is a stall.
        """
        import asyncio
        import contextlib
        import logging
        import server

        reopened = []

        class _Base:
            _rx_running = True
            _rx_dev_rate = 44100
            _rx_last_peak = 0
            opus_enabled = True

            def note_rx_chunk(self, pcm):
                pass

            def rx_silent_seconds(self):
                return 0.0

            def restart_rx(self):
                reopened.append(True)

            def encode_rx_audio(self, pcm):
                return None

        class NeverDelivered(_Base):
            """Wakes slowly: not one byte inside the observation window."""

            def read_rx_chunk(self):
                return None

        class OneChunkThenSilence(_Base):
            """Delivered data, then went quiet — the pre-existing stall case."""

            def __init__(self):
                self.sent = False

            def read_rx_chunk(self):
                if self.sent:
                    return None
                self.sent = True
                return b"\x01\x00" * 480

        async def drive(capture, grace: float, seconds: float):
            saved = (server.audio, server.audio_rx_clients, server.RX_STALL_ALERT_S,
                     server.RX_FIRST_PCM_GRACE_S, server._rx_restart_task)
            server.audio = capture
            server.audio_rx_clients = {object()}
            server.RX_STALL_ALERT_S = 0.2
            server.RX_FIRST_PCM_GRACE_S = grace
            server._rx_restart_task = None
            records = []

            class _Collect(logging.Handler):
                def emit(self, record):
                    records.append(record.getMessage())

            handler = _Collect()
            handler.setLevel(logging.WARNING)
            logging.getLogger("mrrc").addHandler(handler)
            task = asyncio.create_task(server._audio_rx_loop())
            try:
                await asyncio.sleep(seconds)
            finally:
                logging.getLogger("mrrc").removeHandler(handler)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                (server.audio, server.audio_rx_clients, server.RX_STALL_ALERT_S,
                 server.RX_FIRST_PCM_GRACE_S, server._rx_restart_task) = saved
            return [line for line in records if "RX capture stalled" in line]

        # 1. Never delivered a byte, inside the grace window: silence.
        self.assertFalse(asyncio.run(drive(NeverDelivered(), grace=5.0, seconds=1.0)),
                         "a stream that is still waking up is not a stall")
        self.assertFalse(reopened)

        # 2. Never delivered a byte, grace exhausted: a real stall.
        self.assertTrue(asyncio.run(drive(NeverDelivered(), grace=0.2, seconds=1.0)),
                        "past the grace a dead stream must still be caught")
        self.assertTrue(reopened)
        reopened.clear()

        # 3. Data flowed, then stopped: the grace does not delay the alert.
        self.assertTrue(asyncio.run(drive(OneChunkThenSilence(), grace=8.0, seconds=0.7)),
                        "once the first byte arrived, 0.2 s of silence is a stall")
        self.assertTrue(reopened)


    def test_no_watchdog_without_a_listener(self):
        """No consumer at all means the loop idles by design — not a stall.

        The rule is "somebody needs the audio" (a browser, the FT8 tap, a
        recording), not "a WebSocket is open"; see
        ``test_the_ft8_tap_counts_as_a_listener``.
        """
        import asyncio
        import contextlib
        import server

        reopened = []

        class StalledCapture:
            _rx_running = True
            _rx_dev_rate = 44100
            _rx_last_peak = 0
            opus_enabled = True

            def read_rx_chunk(self):
                return None

            def note_rx_chunk(self, pcm):
                pass

            def restart_rx(self):
                reopened.append(True)

        async def drive():
            saved = (server.audio, server.audio_rx_clients, server.RX_STALL_ALERT_S)
            server.audio = StalledCapture()
            server.audio_rx_clients = set()
            server.RX_STALL_ALERT_S = 0.05
            task = asyncio.create_task(server._audio_rx_loop())
            try:
                await asyncio.sleep(0.4)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                server.audio, server.audio_rx_clients, server.RX_STALL_ALERT_S = saved

        asyncio.run(drive())
        self.assertEqual(reopened, [], "the idle path must not reopen anything")

    def test_the_ft8_tap_counts_as_a_listener(self):
        """A stalled capture is reopened for the FT8 tap, not only for browsers.

        Field 2026-10-17 (W103D 192.168.1.220): the USB tree re-enumerated at
        13:31:39 and the ALSA capture went silent.  The operator was on the FT8
        page, which never opens ``/WSaudioRX``, so ``audio_rx_clients`` was
        empty and the watchdog refused to reopen — the decoder starved for the
        rest of the process (``slots_skipped=66/67, decodes=0``).  A consumer of
        the audio, not a WebSocket, is what makes a stall recoverable.
        """
        import asyncio
        import contextlib
        import types
        import server

        reopened = []

        class StalledCapture:
            _rx_running = True
            _rx_dev_rate = 44100
            _rx_last_peak = 0
            opus_enabled = True

            def read_rx_chunk(self):
                return None

            def note_rx_chunk(self, pcm):
                pass

            def restart_rx(self):
                reopened.append(True)

        async def drive():
            saved = (server.audio, server.audio_rx_clients, server.RX_STALL_ALERT_S,
                     server.RX_FIRST_PCM_GRACE_S, server._rx_restart_task,
                     server._ft8_runtime)
            server.audio = StalledCapture()
            server.audio_rx_clients = set()
            server.RX_STALL_ALERT_S = 0.05
            server.RX_FIRST_PCM_GRACE_S = 0.0
            server._rx_restart_task = None
            server._ft8_runtime = types.SimpleNamespace(rx_tap=object())
            task = asyncio.create_task(server._audio_rx_loop())
            try:
                with self.assertLogs("mrrc", level="WARNING") as logs:
                    await asyncio.sleep(0.3)
                    await asyncio.sleep(0.1)   # let asyncio.to_thread finish
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                (server.audio, server.audio_rx_clients, server.RX_STALL_ALERT_S,
                 server.RX_FIRST_PCM_GRACE_S, server._rx_restart_task,
                 server._ft8_runtime) = saved
            return logs

        logs = asyncio.run(drive())
        self.assertTrue(any("RX capture stalled" in line for line in logs.output), logs.output)
        self.assertTrue(reopened, "the FT8 tap keeps the capture alive; it must be reopened")

    def test_an_idle_gap_is_not_a_stalled_capture(self):
        """A quiet stretch with nobody listening is idleness, not a dead capture.

        Field 2026-11-13 (BG7YQV, Xiegu G7250 over its LAN port): the deep-sleep
        gate skipped every read for 63 s while no browser was open, so the stall
        clock was still anchored at the last block read.  The moment a listener
        appeared the loop computed "no PCM for 60 s", logged "RX capture
        stalled" 0.4 s after the connect and called ``restart_rx()`` — which on
        the network handler **clears the audio queue**, throwing away the frames
        it was waiting for (216 frames/s were arriving at the time).

        The window starts when a consumer appears, so nothing is reported inside
        one alert window of the connect.  That the watchdog still fires for a
        capture that stays dead is
        ``test_a_stalled_capture_is_reported_and_reopened``'s job.
        """
        import asyncio
        import contextlib
        import server

        class QuietCapture:
            _rx_running = True
            _rx_dev_rate = 44100
            _rx_last_peak = 0
            opus_enabled = False

            def __init__(self):
                self.reopened = 0

            def read_rx_chunk(self):
                return None                  # nothing to deliver yet

            def note_rx_chunk(self, pcm):
                pass

            def rx_silent_seconds(self):
                return 0.0

            def restart_rx(self):
                self.reopened += 1

        # A handler of our own: `assertLogs` insists on at least one warning,
        # and the whole point here is that there is none inside the window.
        import logging
        seen = []

        class _Catch(logging.Handler):
            def emit(self, record):
                seen.append(record.getMessage())

        catcher = _Catch()
        logging.getLogger("mrrc").addHandler(catcher)

        async def drive():
            capture = QuietCapture()
            saved = (server.audio, server.audio_rx_clients, server.RX_STALL_ALERT_S,
                     server.RX_FIRST_PCM_GRACE_S, server._rx_restart_task)
            server.audio = capture
            server.audio_rx_clients = set()
            server.RX_STALL_ALERT_S = 1.5
            server.RX_FIRST_PCM_GRACE_S = 0.0
            server._rx_restart_task = None
            task = asyncio.create_task(server._audio_rx_loop())
            try:
                # Idle ticks at 0, 0.5 … 2.5; the connect lands at 2.7 and the
                # next tick (3.0) is the first one that reads.
                await asyncio.sleep(2.7)
                server.audio_rx_clients = {object()}
                # 0.9 s later the wake tick has certainly run, and the unfixed
                # code has already cried "no PCM for 2 s".  The fixed code
                # cannot report anything before 3.0 + 1.5.
                await asyncio.sleep(0.9)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                (server.audio, server.audio_rx_clients, server.RX_STALL_ALERT_S,
                 server.RX_FIRST_PCM_GRACE_S, server._rx_restart_task) = saved
            return capture

        try:
            capture = asyncio.run(drive())
        finally:
            logging.getLogger("mrrc").removeHandler(catcher)
        early = [line for line in seen if "RX capture stalled" in line]
        self.assertEqual(early, [], "an idle gap was reported as a stalled capture")
        self.assertEqual(capture.reopened, 0, "the idle path must not clear the queue")

    def test_the_rx_level_is_measured_over_the_whole_block(self):
        """The first block's *head* is not the block.

        Field 2026-11-13 (BG7YQV, G7250 over LAN): the connect diagnostic sampled
        ``pcm[:200]`` — the first 100 samples — and reported ``peak=0.0%`` plus
        "check radio AF gain / USB audio connection", a connection that model
        does not have.  A 5 s capture of the same stream measured 287/287 frames
        loud at 11.1 % FS.
        """
        import asyncio
        import contextlib
        import struct
        import server

        # 100 silent samples then a loud tail: exactly the shape that fooled the
        # head-only measurement.
        BLOCK = b"\x00\x00" * 100 + struct.pack("<860h", *([3000] * 860))

        class LoudCapture:
            _rx_running = True
            _rx_dev_rate = 48000
            _rx_last_peak = 3000
            opus_enabled = False

            def __init__(self):
                self.sent = []

            def read_rx_chunk(self):
                return BLOCK

            def note_rx_chunk(self, pcm):
                pass

            def rx_silent_seconds(self):
                return 0.0

            def restart_rx(self):
                pass

            def encode_rx_audio(self, pcm):
                return [b"\x00" + pcm]

        class FakeWS:
            def __init__(self):
                self.frames = []

            async def send_bytes(self, data):
                self.frames.append(data)

        async def drive():
            capture = LoudCapture()
            ws = FakeWS()
            saved = (server.audio, server.audio_rx_clients)
            server.audio = capture
            server.audio_rx_clients = {ws}
            task = asyncio.create_task(server._audio_rx_loop())
            try:
                with self.assertLogs("mrrc", level="INFO") as logs:
                    await asyncio.sleep(0.2)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                server.audio, server.audio_rx_clients = saved
            return logs

        logs = asyncio.run(drive())
        active = [line for line in logs.output if "RX audio broadcast active" in line]
        self.assertTrue(active, logs.output)
        self.assertIn("peak=9.2%", active[0], active[0])      # 3000 / 32767
        self.assertFalse([line for line in logs.output if "near-silent" in line],
                         "a loud block was called silent")


class EventLoopHygieneTests(unittest.TestCase):
    """Two costs py-spy found *inside* the event loop, and the GC that stalls it.

    2026-11-24, on the deployed box (880 samples / 25 s): `PerMessageDeflate.encode`
    1.9 %, `RxOpusEncoder.push` 9.1 %. Neither belongs on the loop — the payloads
    are 20 ms audio frames and spectrum blocks (nothing compresses), and the
    encoder only runs while a browser is connected. The 185–226 ms event-loop
    stalls that made the far end stutter correlated with **gen2 GC**, one stall
    logged 37 ms after a 148 ms collection.
    """

    SOURCE = (REPO_ROOT / "server.py").read_text(encoding="utf-8")

    def test_permessage_deflate_is_off(self):
        self.assertIn("ws_per_message_deflate=False", self.SOURCE)

    def test_gc_is_frozen_and_the_generations_are_stretched(self):
        self.assertIn("_gc.freeze()", self.SOURCE)
        self.assertIn("_gc.set_threshold(2000, 20, 20)", self.SOURCE)

    def test_the_gc_probe_logs_only_slow_collections(self):
        self.assertIn("_gc.callbacks.append(_gc_probe)", self.SOURCE)
        self.assertIn("GC pause %.0f ms (gen %d", self.SOURCE)
        self.assertIn("if dt >= 0.050:", self.SOURCE)
