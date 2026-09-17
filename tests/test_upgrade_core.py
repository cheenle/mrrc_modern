"""Update channel core (spec 2026-09-17-upgrade-channel, slice 1).

Pure logic — nothing here downloads or installs.  The interesting tests are the
ones about *proof*: what may be recorded as a successful upgrade, and what may
not.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import upgrade_core as up


def manifest(**over):
    base = {
        "latest": "1.18.0",
        "installer": {"version": "1.18.0",
                      "url": "https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-Setup.exe",
                      "sha256": "a" * 64, "size": 45_970_364},
        "previous": {"version": "1.17.0",
                     "url": "https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-v1.17.0-Setup.exe",
                     "sha256": "b" * 64, "size": 45_000_000},
        "minSupported": "1.15.0", "mandatory": False,
        "releasedAt": "2026-10-01T10:00:00", "notes": "新版本说明",
    }
    base.update(over)
    return base


class VersionTests(unittest.TestCase):
    def test_parses_and_ignores_prefix_and_suffix(self):
        self.assertEqual(up.parse_version("v1.17.0"), (1, 17, 0))
        self.assertEqual(up.parse_version("1.17.0-rc1"), (1, 17, 0))
        self.assertIsNone(up.parse_version("nightly"))
        self.assertIsNone(up.parse_version(""))

    def test_compare(self):
        self.assertEqual(up.compare_versions("1.17.0", "1.17.0"), 0)
        self.assertEqual(up.compare_versions("1.17.0", "1.17.1"), -1)
        self.assertEqual(up.compare_versions("1.18.0", "1.17.9"), 1)
        self.assertEqual(up.compare_versions("2.0.0", "1.99.99"), 1)
        self.assertEqual(up.compare_versions("1.16.9", "1.17.0"), -1)

    def test_malformed_sorts_oldest(self):
        self.assertEqual(up.compare_versions("nightly", "1.17.0"), -1)
        self.assertEqual(up.compare_versions("1.17.0", "nightly"), 1)


class ManifestTests(unittest.TestCase):
    def test_accepts_a_good_manifest(self):
        parsed = up.parse_manifest(manifest())
        self.assertEqual(parsed["latest"], "1.18.0")
        self.assertEqual(parsed["installer"]["size"], 45_970_364)
        self.assertEqual(parsed["previous"]["version"], "1.17.0")

    def test_rejects_missing_or_inconsistent_fields(self):
        cases = {
            "缺 latest": {k: v for k, v in manifest().items() if k != "latest"},
            "缺 installer": {**manifest(), "installer": None},
            "版本不一致": {**manifest(), "installer": {"version": "1.17.0", "url": "https://x/y",
                                                    "sha256": "a" * 64, "size": 1}},
            "非 https": {**manifest(), "installer": {"version": "1.18.0", "url": "http://x/y",
                                                     "sha256": "a" * 64, "size": 1}},
            "sha 长度错": {**manifest(), "installer": {"version": "1.18.0", "url": "https://x/y",
                                                      "sha256": "abc", "size": 1}},
            "size 非法": {**manifest(), "installer": {"version": "1.18.0", "url": "https://x/y",
                                                     "sha256": "a" * 64, "size": 0}},
            "previous 等于 latest": {**manifest(), "previous": {"version": "1.18.0"}},
        }
        for label, data in cases.items():
            with self.assertRaises(up.ManifestError, msg=label):
                up.parse_manifest(data)

    def test_rejects_non_json(self):
        with self.assertRaises(up.ManifestError):
            up.parse_manifest(b"<html>404</html>")


class CheckTests(unittest.TestCase):
    def test_newer_available(self):
        result = up.check("1.17.0", up.parse_manifest(manifest()))
        self.assertTrue(result["available"])
        self.assertEqual(result["latest"], "1.18.0")
        self.assertEqual(result["size"], 45_970_364)
        self.assertIn("MRRC-Modern-Setup.exe", result["url"])

    def test_same_or_installed_newer_is_not_available(self):
        self.assertFalse(up.check("1.18.0", up.parse_manifest(manifest()))["available"])
        self.assertFalse(up.check("1.19.0", up.parse_manifest(manifest()))["available"])

    def test_below_min_supported_is_flagged(self):
        result = up.check("1.14.0", up.parse_manifest(manifest()))
        self.assertTrue(result["belowMinSupported"])

    def test_offline_check_reports_a_reason_instead_of_raising(self):
        with mock.patch.object(up.urllib.request, "urlopen", side_effect=OSError("no route to host")):
            result = up.check_quietly("1.17.0")
        self.assertFalse(result["available"])
        self.assertIn("no route to host", result["reason"])

    def test_check_quietly_uses_the_injected_fetch(self):
        with mock.patch.object(up, "fetch_manifest", return_value=up.parse_manifest(manifest())):
            self.assertTrue(up.check_quietly("1.17.0")["available"])


class StateTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "updates" / "state.json"
            self.assertEqual(up.read_state(path), {})
            state = up.record_check({}, up.check("1.17.0", up.parse_manifest(manifest())))
            up.save_state(path, state)
            loaded = up.read_state(path)
        self.assertTrue(loaded["lastCheck"]["available"])
        self.assertEqual(loaded["lastCheck"]["latest"], "1.18.0")

    def test_corrupt_state_reads_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{oops", encoding="utf-8")
            self.assertEqual(up.read_state(path), {})

    def test_unknown_status_is_refused(self):
        with self.assertRaises(ValueError):
            up.record_result({}, "probably_fine", "1.18.0")

    def test_launching_the_installer_is_not_success(self):
        """Spec §2 D3: `installing` means 'we started it', nothing more."""
        state = up.mark_install_started({}, "1.18.0")
        self.assertEqual(state["lastResult"]["status"], "installing")
        self.assertFalse(up.is_success(state))
        self.assertEqual(state["pendingInstall"]["version"], "1.18.0")

    def test_only_the_new_version_boot_can_prove_success(self):
        state = up.mark_install_started({}, "1.18.0")
        # the OLD version keeps running and tries to declare victory
        still_old = up.confirm_boot(state, "1.17.0")
        self.assertFalse(up.is_success(still_old))
        self.assertEqual(still_old["pendingInstall"]["version"], "1.18.0")
        # the new version boots: that is the proof
        new = up.confirm_boot(state, "1.18.0")
        self.assertTrue(up.is_success(new, "1.18.0"))
        self.assertNotIn("pendingInstall", new)

    def test_success_requires_the_matching_version(self):
        state = up.record_result({}, "ok", "1.18.0")
        self.assertTrue(up.is_success(state))
        self.assertFalse(up.is_success(state, "1.17.0"))

    def test_a_completed_upgrade_leaves_no_pending_marker(self):
        state = up.confirm_boot(up.mark_install_started({}, "1.18.0"), "1.18.0")
        self.assertNotIn("pendingInstall", up.confirm_boot(state, "1.18.0"))


class FetchTests(unittest.TestCase):
    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def test_fetches_and_validates(self):
        body = json.dumps(manifest()).encode("utf-8")
        with mock.patch.object(up.urllib.request, "urlopen",
                               return_value=self._Response(body)) as urlopen:
            parsed = up.fetch_manifest("https://example.invalid/latest.json")
        self.assertEqual(parsed["latest"], "1.18.0")
        self.assertIn("latest.json", urlopen.call_args[0][0].full_url)

    def test_bad_payload_raises_manifest_error(self):
        with mock.patch.object(up.urllib.request, "urlopen",
                               return_value=self._Response(b"not json")):
            with self.assertRaises(up.ManifestError):
                up.fetch_manifest("https://example.invalid/latest.json")


if __name__ == "__main__":
    unittest.main()
