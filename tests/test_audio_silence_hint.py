"""The near-silent RX warning must name the macOS microphone permission.

Reported 2026-09-18: RX played silence in the installed .app.  The log showed a
successfully opened capture stream, peak=0.0% and the operator was told to check
the radio's AF gain — while the actual cause was macOS refusing audio input
because the bundle carried no NSMicrophoneUsageDescription key.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class AudioPermissionHintTests(unittest.TestCase):
    def test_mentions_microphone_settings_in_a_frozen_mac_app(self):
        with patch.object(sys, "platform", "darwin"), patch.object(sys, "frozen", True, create=True):
            hint = server._audio_permission_hint()
        self.assertIn("Microphone", hint)
        self.assertIn("Privacy", hint)

    def test_stays_silent_on_other_platforms(self):
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(server._audio_permission_hint(), "")

    def test_stays_silent_for_a_source_checkout_on_macos(self):
        # A terminal-launched checkout inherits Terminal's own permission, so the
        # hint would be noise there.
        with patch.object(sys, "platform", "darwin"), patch.object(sys, "frozen", False, create=True):
            self.assertEqual(server._audio_permission_hint(), "")

    def test_hint_is_wired_into_the_silence_warning(self):
        source = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn("check radio AF gain / USB audio connection%s", source.replace("\n", " "))


if __name__ == "__main__":
    unittest.main()
