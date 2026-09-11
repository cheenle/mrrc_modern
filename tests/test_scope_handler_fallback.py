import unittest
from pathlib import Path

from radio_state import RadioState
from scope_handler import ScopeHandler, WF_SIZE


class ScopeHandlerFallbackTests(unittest.TestCase):
    def test_smeter_fallback_generates_full_width_spectrum(self):
        state = RadioState(vfo_a_freq=14_200_000, s_meter=120, mode=2, preamp=1, attenuator=0)
        scope = ScopeHandler()

        scope.update_from_radio_state(state)

        self.assertEqual(len(scope.spectrum_rx1), WF_SIZE)
        self.assertEqual(len(scope.spectrum_rx2), WF_SIZE)
        self.assertEqual(scope.vfoa_freq, 14_200_000)
        self.assertEqual(scope.s_meter, 120)
        self.assertGreater(max(scope.spectrum_rx1), min(scope.spectrum_rx1))
        self.assertGreater(scope.last_update, 0)


class ScopeMeterSentinelTests(unittest.TestCase):
    """SDD V2.39 field fix: a fresh ScopeHandler carried s_meter=0, and
    server._on_scope_frame broadcasts it whenever >= 0 — so on the IC-7300
    (whose CI-V scope segments carry NO S-meter byte, unlike the FT-710's
    FT4222 frame at data[110]) every ~30 fps waveform forced radio.s_meter
    back to 0 while the 10 Hz CAT poll (15 02) wrote the real value: the UI
    S-meter flickered between 0 and the true reading. The "no data"
    sentinel is now -1, which the >= 0 broadcast gate skips; the FT-710
    (real per-frame value) and the synthetic fallback (update_from_radio_state
    seeds the true radio value) are unchanged."""

    def test_initial_scope_meter_is_no_data_sentinel(self):
        scope = ScopeHandler()
        self.assertEqual(scope.s_meter, -1)  # not 0: 0 is a legal S-zero

    def test_synthetic_fallback_still_seeds_real_value(self):
        state = RadioState(vfo_a_freq=14_200_000, s_meter=88, mode=2)
        scope = ScopeHandler()
        scope.update_from_radio_state(state)
        self.assertEqual(scope.s_meter, 88)

    def test_server_gate_skips_no_data_and_keeps_real_frames(self):
        """The _on_scope_frame gate in server.py must treat -1 as "no data"
        (skip) and any >= 0 parsed value as broadcastable."""
        source = (Path(__file__).resolve().parents[1] / "server.py").read_text(
            encoding="utf-8")
        self.assertIn("if _scope.s_meter >= 0:", source)
        # And the sentinel must live in ScopeHandler, not be a literal here.
        handler_src = (Path(__file__).resolve().parents[1]
                       / "scope_handler.py").read_text(encoding="utf-8")
        self.assertIn("self.s_meter: int = -1", handler_src)


if __name__ == "__main__":
    unittest.main()
