"""Session tokens survive a service restart (field report: repeated logins).

Why this module exists: the login cookie lives 30 days, but the server only
ever kept the matching tokens in two in-memory sets.  Every restart — including
the routine restarts an upgrade performs — wiped them, so browsers kept
presenting a perfectly valid cookie and were bounced to the login page anyway.
The operator saw "it keeps asking me to log in" with no restart of the *box*.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import server


class AuthTokenPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "auth-tokens.json"
        patches = [
            mock.patch.object(server, "_AUTH_TOKENS_FILE", self.path),
            mock.patch.object(server, "_auth_tokens", set()),
            mock.patch.object(server, "_listen_tokens", set()),
            mock.patch.object(server, "_token_issued", {}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _simulate_restart(self) -> None:
        """Wipe the in-memory sets and reload from disk, as a restart would."""
        server._auth_tokens.clear()
        server._listen_tokens.clear()
        server._token_issued.clear()
        server._load_auth_tokens()

    def test_login_survives_a_restart(self) -> None:
        server._add_auth_token("operator-token", listen=False)
        server._add_auth_token("listen-token", listen=True)

        self._simulate_restart()

        self.assertIn("operator-token", server._auth_tokens)
        self.assertIn("listen-token", server._auth_tokens)
        # The listen-only role must survive too — otherwise a listener is
        # silently promoted to operator after an upgrade.
        self.assertIn("listen-token", server._listen_tokens)
        self.assertNotIn("operator-token", server._listen_tokens)

    def test_logout_removes_the_token_from_disk(self) -> None:
        server._add_auth_token("bye", listen=False)
        server._drop_auth_token("bye")

        self._simulate_restart()

        self.assertNotIn("bye", server._auth_tokens)
        stored = json.loads(self.path.read_text())["tokens"]
        self.assertEqual([entry["token"] for entry in stored], [])

    def test_tokens_older_than_the_cookie_are_dropped(self) -> None:
        """A 30-day cookie whose token aged out must not be honoured."""
        self.path.write_text(
            json.dumps(
                {
                    "tokens": [
                        {
                            "token": "stale",
                            "role": "operator",
                            "issued_epoch": time.time() - server._AUTH_TOKENS_MAX_AGE_S - 60,
                        },
                        {
                            "token": "fresh",
                            "role": "operator",
                            "issued_epoch": time.time() - 60,
                        },
                    ]
                }
            )
        )

        server._load_auth_tokens()

        self.assertIn("fresh", server._auth_tokens)
        self.assertNotIn("stale", server._auth_tokens)

    def test_corrupt_store_starts_empty_instead_of_crashing(self) -> None:
        self.path.write_text("{not json at all")

        server._load_auth_tokens()          # must not raise

        self.assertEqual(server._auth_tokens, set())

    def test_missing_store_is_not_an_error(self) -> None:
        server._load_auth_tokens()
        self.assertEqual(server._auth_tokens, set())

    def test_store_is_owner_readable_only(self) -> None:
        server._add_auth_token("secret-token", listen=False)
        mode = self.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
