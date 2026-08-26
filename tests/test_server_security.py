"""Regression coverage for SDD open issues I8 and I9.

I8 — serve_static path traversal: a raw ``STATIC_DIR / path`` join let
``GET /../server.py`` (or an absolute request path) read arbitrary files.
The fix resolves the joined path and requires containment inside
STATIC_DIR; these tests pin that behaviour at the helper level so no
running server is needed.

I9 — constant-time password comparison: ``password != WEB_PASSWORD``
leaked prefix/length timing. The helper now uses hmac.compare_digest;
tests verify correctness semantics (right/wrong/empty/non-ASCII) plus
the startup default-password warning gate.
"""
import logging
import unittest
from unittest.mock import patch

import server
from config import DEFAULT_WEB_PASSWORD, WEB_PASSWORD


class ResolveStaticPathTests(unittest.TestCase):
    """I8: _resolve_static_path must never escape STATIC_DIR."""

    def test_empty_path_resolves_to_index(self):
        resolved = server._resolve_static_path("")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.name, "index.html")

    def test_legit_asset_stays_contained(self):
        for rel in ("index.html", "ft710_main.js", "modules/ptt_manager.js"):
            resolved = server._resolve_static_path(rel)
            if resolved is None:
                # Only skip when the asset genuinely does not exist on disk
                # (resolve() does not require existence, so this should not
                # happen in-repo).
                self.fail(f"expected contained resolution for {rel}")
            self.assertTrue(resolved.is_relative_to(server.STATIC_DIR.resolve()))

    def test_parent_traversal_rejected(self):
        for evil in (
            "../server.py",
            "../../server.py",
            "static/../../../etc/passwd",
            "modules/../../server.py",
        ):
            self.assertIsNone(
                server._resolve_static_path(evil), msg=f"traversal escaped: {evil}")

    def test_dot_dot_lookalikes_are_harmless_literals(self):
        # '....' is a literal filename, not a parent reference — resolving it
        # stays inside STATIC_DIR and simply misses (SPA fallback territory).
        resolved = server._resolve_static_path("....//....//server.py")
        self.assertIsNotNone(resolved)
        self.assertTrue(resolved.is_relative_to(server.STATIC_DIR.resolve()))

    def test_absolute_request_path_rejected(self):
        # Path join with an absolute segment replaces the base entirely —
        # exactly why the naive join was exploitable.
        self.assertIsNone(server._resolve_static_path("/etc/passwd"))

    def test_dot_segments_to_hidden_file_rejected(self):
        self.assertIsNone(server._resolve_static_path("./../config.py"))

    def test_nonexistent_but_contained_path_is_returned(self):
        # Containment is the security boundary; existence is handled by the
        # caller (SPA fallback). A missing file inside STATIC_DIR resolves.
        resolved = server._resolve_static_path("no_such_asset.bin")
        self.assertIsNotNone(resolved)
        self.assertTrue(resolved.is_relative_to(server.STATIC_DIR.resolve()))


class PasswordCompareTests(unittest.TestCase):
    """I9: constant-time comparison semantics."""

    def test_correct_password_matches(self):
        self.assertTrue(server._password_matches(WEB_PASSWORD))

    def test_wrong_password_rejected(self):
        self.assertFalse(server._password_matches(WEB_PASSWORD + "x"))
        self.assertFalse(server._password_matches("a"))
        self.assertFalse(server._password_matches(""))

    def test_none_and_non_ascii_do_not_raise(self):
        # Old == path could raise on odd types; encode() path must not.
        self.assertFalse(server._password_matches(None))
        self.assertFalse(server._password_matches("пароль"))
        self.assertFalse(server._password_matches("密码\x00"))

    def test_compare_digest_is_used_not_eq(self):
        import inspect
        source = inspect.getsource(server._password_matches)
        self.assertIn("hmac.compare_digest", source)
        self.assertNotIn("!=", source)


class DefaultPasswordWarningTests(unittest.TestCase):
    """I9 ratchet: loud startup warning while the default password is active."""

    def test_warns_when_default_password_configured(self):
        with patch.object(server, "WEB_PASSWORD", DEFAULT_WEB_PASSWORD):
            with self.assertLogs(server.logger, level=logging.WARNING) as captured:
                fired = server._warn_if_default_password()
        self.assertTrue(fired)
        self.assertTrue(any("default password" in line for line in captured.output))

    def test_quiet_when_strong_password_configured(self):
        with patch.object(server, "WEB_PASSWORD", "a-strong-unique-passphrase-42!"):
            fired = server._warn_if_default_password()
        self.assertFalse(fired)

    def test_warning_wired_into_lifespan_startup(self):
        source = open("server.py", encoding="utf-8").read()
        self.assertIn("_warn_if_default_password()", source)


if __name__ == "__main__":
    unittest.main()
