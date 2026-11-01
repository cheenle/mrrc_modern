"""A 2 m/70 cm radio must not be treated as an HF-only one (2026-11-01).

Field report (bundle `20261101-103310-caf7`, BG7MWR, W103D + FT-991A):

* ``频率显示在100M以上不现实百M位`` — the header had no hundreds-of-MHz digit,
  so 145.000.000 rendered as ``45.000.0``;
* the UI clamped every tune step / waterfall click / typed entry to 75 MHz —
  the FT-710's ceiling — so on 2 m a ▲ press yanked the radio back to 75 MHz;
* the server's poll guard (``30000 <= freq <= 75000000``) threw away every
  reading above 75 MHz, so the state never followed the radio up there even
  though the radio took ``FA144500000``/``FA435000000``;
* the span selector raised ``AttributeError: ... no attribute
  'set_scope_span'`` (``频谱显示只能在100宽度``) and squelch was write-only
  (``sql 没能同步``).

Each assertion below pins one of those, plus the FT-991A CAT rate the same
bundle proved (the radio answers at 4800, not the family's 38400).
"""
import asyncio
import re
import unittest
from pathlib import Path

from config import FREQ_HF_MAX_HZ, FREQ_MIN_HZ, default_baud_for, plausible_freq_range
from poll_scheduler import PollScheduler
from radio_state import RadioState

REPO_ROOT = Path(__file__).resolve().parents[1]


def _bands_of(model: str) -> list:
    """The band table the backend for *model* publishes (no hardware)."""
    from backends import create_backend  # noqa: PLC0415 - keeps import cost off collection

    backend = create_backend(model, port="/dev/null")
    return backend.bands


class PlausibleFreqRangeTests(unittest.TestCase):
    """The window is the model's, and widening it never narrows an HF radio."""

    def test_hf_models_keep_the_historical_75_mhz_ceiling(self):
        for model in ("ft710", "ft891", "ic7300"):
            self.assertEqual(plausible_freq_range(_bands_of(model)),
                             (FREQ_MIN_HZ, FREQ_HF_MAX_HZ), model)

    def test_vhf_uhf_models_widen_the_ceiling_to_their_own_band_table(self):
        for model in ("ft991a", "ftx1", "ic705"):
            low, high = plausible_freq_range(_bands_of(model))
            self.assertEqual(low, FREQ_MIN_HZ, model)
            self.assertGreaterEqual(high, 435_000_000, model)

    def test_no_band_table_means_the_default_window(self):
        """A fake backend in a test has no bands — it must not crash or shrink."""
        self.assertEqual(plausible_freq_range(()), (FREQ_MIN_HZ, FREQ_HF_MAX_HZ))
        self.assertEqual(plausible_freq_range(None), (FREQ_MIN_HZ, FREQ_HF_MAX_HZ))

    def test_the_old_triple_shape_is_tolerated(self):
        """The Yaesu profiles used to publish [label, low, high] triples."""
        self.assertEqual(plausible_freq_range([("2m", 144_000_000, 148_000_000)]),
                         (FREQ_MIN_HZ, 148_000_000))

    def test_malformed_entries_are_ignored_not_fatal(self):
        bands = [None, {}, {"end": "nope"}, {"name": "x"}, {"end": 146_000_000}]
        self.assertEqual(plausible_freq_range(bands), (FREQ_MIN_HZ, 146_000_000))


class FakeCat:
    """The poll scheduler never touches the port in these tests."""

    connected = True
    _cancel_polls = None  # not read by _freq_usable


class FakeBandsBackend:
    def __init__(self, bands):
        self.bands = bands


class PollWindowTests(unittest.TestCase):
    """`_freq_usable`: the guard that used to discard every VHF/UHF reading."""

    def _scheduler(self, model):
        return PollScheduler(FakeCat(), RadioState(),
                             backend=FakeBandsBackend(_bands_of(model)))

    def test_a_two_metre_reading_survives_on_an_ft991a(self):
        s = self._scheduler("ft991a")
        self.assertTrue(s._freq_usable(144_500_000))
        self.assertTrue(s._freq_usable(435_000_000))

    def test_the_same_reading_is_still_rejected_on_an_hf_radio(self):
        s = self._scheduler("ft710")
        self.assertFalse(s._freq_usable(144_500_000))
        self.assertTrue(s._freq_usable(14_270_000))

    def test_line_noise_is_rejected_on_every_model(self):
        for model in ("ft710", "ft991a"):
            s = self._scheduler(model)
            self.assertFalse(s._freq_usable(0), model)
            self.assertFalse(s._freq_usable(2_400_000_000), model)

    def test_discards_are_counted_and_logged_once(self):
        """A wrong window must be diagnosable instead of silently empty."""
        s = self._scheduler("ft710")
        with self.assertLogs("mrrc.poll", level="WARNING") as captured:
            s._freq_usable(144_500_000)
            s._freq_usable(144_500_001)
        self.assertEqual(s._freq_distrusted, 2)
        self.assertEqual(len(captured.records), 1)
        self.assertIn("implausible", captured.records[0].getMessage())


class BroadcastGuardTests(unittest.TestCase):
    """The CI-V broadcast path shares the window (it had its own copy)."""

    def setUp(self):
        import server  # noqa: PLC0415

        self.server = server
        self._saved_window = server._FREQ_WINDOW
        self._saved_radio = server.radio
        server.radio = RadioState()

    def tearDown(self):
        self.server._FREQ_WINDOW = self._saved_window
        self.server.radio = self._saved_radio

    def test_a_vhf_broadcast_is_accepted_once_the_window_is_widened(self):
        self.server._FREQ_WINDOW = plausible_freq_range(_bands_of("ft991a"))
        # Pre-set the value so `radio.update` reports no change: that keeps
        # `_on_cat_broadcast` off the event loop (no running loop here).
        self.server.radio.update(vfo_a_freq=435_000_000)
        self.server._on_cat_broadcast("vfo_a_freq", 435_000_000)
        self.assertEqual(self.server.radio.vfo_a_freq, 435_000_000)

    def test_the_hf_only_window_still_discards_it(self):
        self.server._FREQ_WINDOW = plausible_freq_range(_bands_of("ft710"))
        self.server._on_cat_broadcast("vfo_a_freq", 435_000_000)
        self.assertNotEqual(self.server.radio.vfo_a_freq, 435_000_000)


class Ft991aProfileTests(unittest.TestCase):
    def test_the_cat_rate_is_the_measured_factory_value(self):
        """The bundle showed `no answer at 38400 ... answers at 4800` every boot."""
        from backends.yaesu.yaesu_profiles import get_profile  # noqa: PLC0415

        self.assertEqual(get_profile("ft991a").default_baud, 4800)
        self.assertEqual(default_baud_for("ft991a"), 4800,
                         "the connection dialog writes default_baud_for()")

    def test_the_provenance_names_the_measurement(self):
        from backends.yaesu.yaesu_profiles import get_profile  # noqa: PLC0415

        note = get_profile("ft991a").provenance["default_baud"]
        self.assertIn("4800", note)
        self.assertIn("20261101", note)


class SpectrumControlTests(unittest.TestCase):
    """The span/speed/mode selectors must stop raising on a scope-free radio."""

    def test_the_controller_implements_every_call_the_server_makes(self):
        import server  # noqa: PLC0415
        from backends.yaesu.cat_core import YaesuCatController  # noqa: PLC0415

        source = Path(server.__file__).read_text(encoding="utf-8")
        # `elif field == "scope_span": ... await cat.set_scope_span(v)` and the
        # two siblings — the exact call sites that raised AttributeError.
        for field, method in (("scope_span", "set_scope_span"),
                              ("scope_speed", "set_scope_speed"),
                              ("scope_mode", "set_scope_mode")):
            self.assertIn(f'elif field == "{field}":', source)
            self.assertIn(f"await cat.{method}(", source)
            self.assertTrue(hasattr(YaesuCatController, method), method)


class SquelchReadBackTests(unittest.TestCase):
    """`sql 没能同步`: the level was write-only for the whole Yaesu family."""

    def test_every_yaesu_model_polls_it(self):
        from backends.yaesu import backend as yb  # noqa: PLC0415

        for model in ("ftdx10", "ftdx101d", "ftdx101mp", "ftx1", "ft891", "ft991a"):
            fields = [f for f, _ in yb.make_backend(model)("/dev/null").settings_poll_items()]
            self.assertIn("squelch", fields, model)

    def test_the_ft710_polls_it_too(self):
        from backends.ft710.backend import FT710Backend  # noqa: PLC0415

        fields = [f for f, _ in FT710Backend(port="dummy").settings_poll_items()]
        self.assertIn("squelch", fields)

    def test_the_first_state_push_carries_the_radio_value(self):
        from backends.yaesu import cat_core  # noqa: PLC0415

        source = Path(cat_core.__file__).read_text(encoding="utf-8")
        sync = source[source.index("async def initial_state_sync"):]
        self.assertIn('("squelch", lambda: self.get_squelch())', sync)


class FrontendContractTests(unittest.TestCase):
    """The browser has to be able to *show* and *reach* 100 MHz and up."""

    def setUp(self):
        self.html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.ui = (REPO_ROOT / "static" / "ft710_ui.js").read_text(encoding="utf-8")
        self.listen = (REPO_ROOT / "static" / "listen.js").read_text(encoding="utf-8")
        self.css = (REPO_ROOT / "static" / "ft710.css").read_text(encoding="utf-8")

    def test_the_header_has_a_hundreds_of_mhz_digit(self):
        self.assertIn('id="f100m"', self.html)
        self.assertLess(self.html.index('id="f100m"'), self.html.index('id="f10m"'),
                        "the lead digit is the leftmost one")
        self.assertIn("display: none", self.css)          # hidden by default
        self.assertIn("#freq-display.wide .freq-lead", self.css)

    def test_the_renderer_computes_and_prints_it(self):
        self.assertIn("const m100 = Math.floor(hz / 100_000_000) % 10", self.ui)
        self.assertIn("m100: String(m100)", self.ui)
        self.assertIn("setText('f100m', f.m100)", self.ui)

    def test_the_lead_digit_only_appears_for_a_vhf_capable_radio(self):
        self.assertIn("classList.toggle('wide'", self.ui)
        self.assertRegex(self.ui,
                         r"classList\.toggle\('wide', _freqBounds\(\)\.max > 99_999_999\)")

    def test_no_tune_path_clamps_to_the_ft710_ceiling(self):
        """The 75 MHz literal may only survive as the *fallback* bound."""
        for name, source in (("ft710_ui.js", self.ui), ("listen.js", self.listen)):
            self.assertNotIn("Math.min(75000000", source, name)
        self.assertIn("FREQ_HF_MAX_HZ = 75_000_000", self.ui)
        self.assertIn("_clampFreq", self.ui)
        self.assertIn("Math.max(b.min, Math.min(b.max, hz))", self.ui)

    def test_the_window_is_derived_from_the_band_table(self):
        self.assertIn("function _freqBounds()", self.ui)
        self.assertIn("typeof bands !== 'undefined'", self.ui)
        # listen.js has its own copy of the derivation (it never loads ft710_ui.js).
        self.assertIn("const end = Number(b && b.end);", self.listen)
        self.assertIn("next > max", self.listen)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
