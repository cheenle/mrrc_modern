"""Remote-session concurrency + uplink metering (hub open issue I-H1).

The interesting tests are the ones about *honesty*: the numbers these counters
produce are what decides whether the hub builds RX fan-out, so a peak that
decays silently, a byte counter that double-counts a retry, or an identifier
that leaks into the snapshot would each mislead a real capacity decision.
"""
import unittest
from pathlib import Path

import session_metrics as sm


class FakeClock:
    """Deterministic monotonic clock: windowed peaks tested without sleeping."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.m = sm.SessionMetrics(window_seconds=60.0, clock=self.clock)

    def test_sockets_are_counted_per_role_and_kind(self):
        self.m.open("listener", "control", "tok-a")
        self.m.open("listener", "spectrum", "tok-a")
        self.m.open("operator", "control", "tok-b")
        snap = self.m.snapshot()
        self.assertEqual(snap["listeners"]["sockets"], 2)
        self.assertEqual(snap["operators"]["sockets"], 1)
        self.assertEqual(snap["sockets_by_kind"]["control"], 2)
        self.assertEqual(snap["sockets_by_kind"]["spectrum"], 1)
        self.assertEqual(snap["sockets_by_kind"]["audio_rx"], 0)

    def test_one_session_may_own_several_sockets(self):
        """A listener is one person with three sockets — the person count is a
        *session* count, or capacity planning would over-count listeners 3×."""
        for kind in ("control", "spectrum", "audio_rx"):
            self.m.open("listener", kind, "tok-a")
        snap = self.m.snapshot()
        self.assertEqual(snap["listeners"]["sessions"], 1)
        self.assertEqual(snap["listeners"]["sockets"], 3)

    def test_two_sessions_are_two_listeners(self):
        self.m.open("listener", "audio_rx", "tok-a")
        self.m.open("listener", "audio_rx", "tok-b")
        self.assertEqual(self.m.snapshot()["listeners"]["sessions"], 2)

    def test_last_socket_of_a_session_removes_the_session(self):
        self.m.open("listener", "control", "tok-a")
        self.m.open("listener", "spectrum", "tok-a")
        self.m.close("listener", "spectrum", "tok-a")
        self.assertEqual(self.m.snapshot()["listeners"]["sessions"], 1)
        self.m.close("listener", "control", "tok-a")
        self.assertEqual(self.m.snapshot()["listeners"]["sessions"], 0)

    def test_double_close_does_not_underflow(self):
        """Cleanup runs from the normal return path *and* the error path, and a
        broadcast may already have dropped the socket — underflow here would
        silently corrupt every later capacity number."""
        self.m.open("listener", "audio_rx", "tok-a")
        self.m.close("listener", "audio_rx", "tok-a")
        self.m.close("listener", "audio_rx", "tok-a")
        self.m.close("listener", "audio_rx", None)
        snap = self.m.snapshot()
        self.assertEqual(snap["listeners"]["sockets"], 0)
        self.assertEqual(snap["listeners"]["sessions"], 0)

    def test_close_of_never_opened_session_is_harmless(self):
        self.m.close("listener", "audio_rx", "never-opened")
        self.assertEqual(self.m.snapshot()["listeners"]["sessions"], 0)


class PeakTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.m = sm.SessionMetrics(window_seconds=60.0, clock=self.clock)

    def test_lifetime_peak_survives_disconnects(self):
        self.m.open("listener", "audio_rx", "tok-a")
        self.m.open("listener", "audio_rx", "tok-b")
        self.m.close("listener", "audio_rx", "tok-a")
        self.m.close("listener", "audio_rx", "tok-b")
        snap = self.m.snapshot()
        self.assertEqual(snap["listeners"]["sessions"], 0)
        self.assertEqual(snap["listeners"]["peak_sessions_lifetime"], 2)

    def test_window_peak_forgets_an_old_spike(self):
        """A spike from three windows ago must not keep inflating the peak the
        fan-out decision reads."""
        for token in ("tok-a", "tok-b", "tok-c"):
            self.m.open("listener", "audio_rx", token)
        for token in ("tok-a", "tok-b", "tok-c"):
            self.m.close("listener", "audio_rx", token)
        self.clock.advance(120.0)
        self.m.open("listener", "audio_rx", "tok-d")
        snap = self.m.snapshot()
        self.assertEqual(snap["listeners"]["sessions"], 1)
        self.assertEqual(snap["listeners"]["peak_sessions_window"], 1)
        self.assertEqual(snap["listeners"]["peak_sessions_lifetime"], 3)

    def test_window_peak_keeps_a_recent_spike(self):
        self.m.open("listener", "audio_rx", "tok-a")
        self.m.open("listener", "audio_rx", "tok-b")
        self.clock.advance(10.0)
        self.m.close("listener", "audio_rx", "tok-b")
        self.assertEqual(self.m.snapshot()["listeners"]["peak_sessions_window"], 2)

    def test_window_peak_is_never_below_current(self):
        """With no samples left the current value *is* the observation."""
        self.m.open("listener", "audio_rx", "tok-a")
        self.clock.advance(3600.0)
        snap = self.m.snapshot()
        self.assertEqual(snap["listeners"]["peak_sessions_window"], 1)

    def test_operator_peaks_tracked_separately(self):
        self.m.open("operator", "control", "op-a")
        self.m.open("operator", "audio_tx", "op-a")
        snap = self.m.snapshot()
        self.assertEqual(snap["operators"]["sessions"], 1)
        self.assertEqual(snap["operators"]["peak_sockets_lifetime"], 2)
        self.assertEqual(snap["listeners"]["peak_sockets_lifetime"], 0)


class ByteTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.m = sm.SessionMetrics(window_seconds=60.0, clock=self.clock)

    def test_bytes_accumulate_per_kind(self):
        self.m.add_bytes("spectrum", 1701)
        self.m.add_bytes("spectrum", 1701)
        self.m.add_bytes("audio_rx", 160)
        total = self.m.snapshot()["uplink_bytes_total"]
        self.assertEqual(total["spectrum"], 3402)
        self.assertEqual(total["audio_rx"], 160)

    def test_non_positive_byte_counts_are_ignored(self):
        self.m.add_bytes("spectrum", 0)
        self.m.add_bytes("spectrum", -5)
        self.assertEqual(self.m.snapshot()["uplink_bytes_total"]["spectrum"], 0)

    def test_report_yields_a_rate_and_resets_the_delta(self):
        """408 kbps is the measured spectrum cost (1701 B × 30 fps); the report
        has to reproduce that from real frames, or the capacity model is fiction."""
        for _ in range(30):
            self.m.add_bytes("spectrum", 1701)
        self.clock.advance(1.0)
        report = self.m.take_report()
        self.assertEqual(report["bytes_since_report"]["spectrum"], 51_030)
        self.assertAlmostEqual(report["kbps_since_report"]["spectrum"], 408.2, places=1)
        self.assertEqual(report["elapsed_seconds"], 1.0)
        # Second report must not re-count the same frames.
        self.clock.advance(1.0)
        again = self.m.take_report()
        self.assertEqual(again["bytes_since_report"]["spectrum"], 0)
        self.assertEqual(again["kbps_since_report"]["spectrum"], 0.0)
        # But the lifetime total is still there.
        self.assertEqual(again["uplink_bytes_total"]["spectrum"], 51_030)

    def test_report_rate_uses_real_elapsed_time(self):
        self.m.add_bytes("audio_rx", 8_000)   # 64 kbps for exactly one second
        self.clock.advance(2.0)
        report = self.m.take_report()
        self.assertAlmostEqual(report["kbps_since_report"]["audio_rx"], 32.0, places=1)

    def test_report_carries_concurrency_and_peaks(self):
        self.m.open("listener", "audio_rx", "tok-a")
        self.m.open("listener", "audio_rx", "tok-b")
        report = self.m.take_report()
        self.assertEqual(report["listeners"]["sessions"], 2)
        self.assertEqual(report["listeners"]["peak_sessions_lifetime"], 2)


class PrivacyTests(unittest.TestCase):
    def test_snapshot_and_report_carry_no_session_identifiers(self):
        """A token is an opaque key inside this module — if it ever reaches the
        snapshot it reaches /api/session_metrics and every log line, which is a
        credential leak (NFR-068) and an operator-activity leak too."""
        m = sm.SessionMetrics(clock=FakeClock())
        m.open("listener", "audio_rx", "secret-token-value")
        m.open("operator", "control", "another-secret")
        rendered = f"{m.snapshot()!r}{m.take_report()!r}"
        self.assertNotIn("secret-token-value", rendered)
        self.assertNotIn("another-secret", rendered)

    def test_snapshot_keys_are_stable(self):
        snap = sm.SessionMetrics(clock=FakeClock()).snapshot()
        self.assertEqual(
            set(snap),
            {"uptime_seconds", "window_seconds", "listeners", "operators",
             "sockets_by_kind", "uplink_bytes_total", "spectrum_profiles"},
        )
        self.assertEqual(set(snap["sockets_by_kind"]), set(sm.KINDS))
        self.assertEqual(set(snap["listeners"]),
                         {"sessions", "sockets", "peak_sessions_lifetime",
                          "peak_sessions_window", "peak_sockets_lifetime"})
        self.assertEqual(set(snap["uplink_bytes_total"]), set(sm.METERED_KINDS))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.m = sm.SessionMetrics(clock=FakeClock())

    def test_unknown_role_is_rejected(self):
        with self.assertRaises(ValueError):
            self.m.open("admin", "control", "tok")

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            self.m.open("listener", "video", "tok")
        with self.assertRaises(ValueError):
            self.m.add_bytes("video", 10)
        with self.assertRaises(ValueError):
            self.m.close("listener", "video", "tok")


class SourceGuardTests(unittest.TestCase):
    """The counters are only worth anything if they stay wired to the sockets.

    A dropped `metrics.open` would not fail anything — it would quietly report
    zero listeners, which reads as "fan-out is unnecessary". Source-level
    guards are the established pattern for exactly this (test_atr1000_server).
    """

    @classmethod
    def setUpClass(cls):
        cls.server = Path("server.py").read_text(encoding="utf-8")
        cls.config = Path("config.py").read_text(encoding="utf-8")

    def endpoint(self, path: str) -> str:
        self.assertIn(f'@app.websocket("{path}")', self.server)
        return self.server.split(f'@app.websocket("{path}")', 1)[1]

    def test_every_websocket_endpoint_meters_open_and_close(self):
        for path, kind in (("/WSradio", "control"),
                           ("/WSspectrum", "spectrum"),
                           ("/WSaudioRX", "audio_rx"),
                           ("/WSaudioTX", "audio_tx"),
                           ("/WSatr1000", "atr")):
            body = self.endpoint(path)
            self.assertIn(f'metrics.open(', body, path)
            self.assertIn(f'"{kind}", token)', body, path)
            self.assertIn(f'metrics.close(', body, path)

    def test_control_endpoint_meters_both_cleanup_paths(self):
        """ /WSradio can fail while sending the initial full state, before the
        main loop's finally is reached — both paths must decrement."""
        body = self.endpoint("/WSradio")
        self.assertEqual(body.count('metrics.close(role, "control", token)'), 2)

    def test_uplink_bytes_are_metered_on_both_fan_out_paths(self):
        self.assertIn('metrics.add_bytes("spectrum", len(binary))', self.server)
        self.assertIn('metrics.add_bytes("audio_rx", len(frame))', self.server)

    def test_rest_endpoint_exists(self):
        self.assertIn('@app.get("/api/session_metrics")', self.server)
        self.assertIn("return JSONResponse(metrics.snapshot())", self.server)

    def test_report_loop_is_created_and_cancelled(self):
        self.assertIn("if SESSION_METRICS_INTERVAL_S > 0:", self.server)
        self.assertIn('asyncio.create_task(\n            _session_metrics_loop(), '
                      'name="session_metrics")', self.server)
        self.assertIn("_session_metrics_task.cancel()", self.server)

    def test_env_knobs_are_documented_in_config(self):
        self.assertIn('_env_float("MRRC_SESSION_METRICS_INTERVAL_S"', self.config)
        self.assertIn('_env_float("MRRC_SESSION_METRICS_WINDOW_S"', self.config)

    def test_metrics_singleton_is_built_from_config_window(self):
        self.assertIn("metrics = SessionMetrics(window_seconds=SESSION_METRICS_WINDOW_S)",
                      self.server)


class SpectrumProfileCountersTests(unittest.TestCase):
    """Per-tier counters are how the AD-025 acceptance numbers get proven.

    ``add_bytes`` can say how much spectrum went out but not whether it was 12
    full frames or 24 short ones — and that distinction is the whole feature.
    """

    def setUp(self):
        self.m = sm.SessionMetrics(window_seconds=60.0, clock=FakeClock())

    def test_frames_and_bytes_accumulate_per_profile(self):
        self.m.add_spectrum_profile_frame("high", 1701)
        self.m.add_spectrum_profile_frame("high", 1701)
        self.m.add_spectrum_profile_frame("low", 851)
        snap = self.m.snapshot()["spectrum_profiles"]
        self.assertEqual(snap["high"], {"frames": 2, "bytes": 3402})
        self.assertEqual(snap["low"], {"frames": 1, "bytes": 851})
        self.assertNotIn("mid", snap)

    def test_idle_server_reports_no_tiers(self):
        """Don't invent buckets nobody used — the log line prints them all."""
        self.assertEqual(
            sm.SessionMetrics(clock=FakeClock()).snapshot()["spectrum_profiles"], {})

    def test_snapshot_returns_copies(self):
        self.m.add_spectrum_profile_frame("mid", 851)
        self.m.snapshot()["spectrum_profiles"]["mid"]["frames"] = 999
        self.assertEqual(self.m.snapshot()["spectrum_profiles"]["mid"]["frames"], 1)

    def test_non_positive_bytes_still_count_the_frame(self):
        self.m.add_spectrum_profile_frame("high", 0)
        self.assertEqual(self.m.snapshot()["spectrum_profiles"]["high"],
                         {"frames": 1, "bytes": 0})

    def test_unknown_profile_name_gets_its_own_bucket(self):
        """This module keeps zero app dependencies: names come from the caller."""
        self.m.add_spectrum_profile_frame("whatever", 10)
        self.assertEqual(self.m.snapshot()["spectrum_profiles"]["whatever"]["frames"], 1)

    def test_empty_profile_name_is_ignored(self):
        self.m.add_spectrum_profile_frame("", 10)
        self.assertEqual(self.m.snapshot()["spectrum_profiles"], {})


if __name__ == "__main__":
    unittest.main()
