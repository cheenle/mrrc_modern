"""Call help: the credential rules first, because everything else is replaceable.

The password is the one value in this feature that must never come to rest. These
tests are written before the code so the rules are the spec rather than a
description of what got built.
"""
from __future__ import annotations

import re
import unittest

import help_session


class CredentialTests(unittest.TestCase):
    def test_a_fresh_pair_every_time(self):
        a = help_session.make_credentials()
        b = help_session.make_credentials()
        self.assertNotEqual(a, b)
        self.assertNotEqual(a[0], b[0], "the username is fresh too")

    def test_the_password_is_long_and_urlsafe(self):
        user, pw = help_session.make_credentials()
        self.assertGreaterEqual(len(pw), 20)
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9_\-]+", pw), pw)
        self.assertTrue(re.fullmatch(r"help-[0-9a-f]{4}", user), user)

    def test_the_username_is_typeable(self):
        """It gets typed at a hub shell by a human, so: short, lowercase, no oddities."""
        user, _ = help_session.make_credentials()
        self.assertLessEqual(len(user), 12)

    def test_compare_is_constant_time(self):
        """Source-level, because timing itself is not testable here."""
        import inspect
        src = inspect.getsource(help_session)
        self.assertIn("hmac.compare_digest", src)
        self.assertNotIn("== password", src)

    def test_a_customer_supplied_username_is_checked_for_shape(self):
        """The customer may bring their own; it is typed by a human at a hub shell."""
        self.assertTrue(help_session._usable_username("zhang-san"))
        self.assertTrue(help_session._usable_username("BG7IFP"))
        for bad in ("", "a", "has space", "semi;colon", "quote'", "x" * 40, "路径"):
            with self.subTest(bad=bad):
                self.assertFalse(help_session._usable_username(bad))

    def test_matching_never_short_circuits_on_the_username(self):
        """"user right, password wrong" and "both wrong" must cost the same work."""
        self.assertTrue(help_session.credentials_match("u", "p", "u", "p"))
        self.assertFalse(help_session.credentials_match("u", "p", "u", "x"))
        self.assertFalse(help_session.credentials_match("u", "p", "x", "p"))
        self.assertFalse(help_session.credentials_match("u", "p", "x", "x"))


class LeakTests(unittest.TestCase):
    """Nothing about the password may reach a carrier that outlives the session."""

    def test_the_module_never_writes_it(self):
        import inspect
        src = inspect.getsource(help_session)
        for forbidden in ("write_text", "json.dump"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
