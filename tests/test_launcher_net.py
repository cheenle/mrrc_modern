"""The launcher asks the server which URL to open instead of guessing (v1.24.6).

Design record for why this module exists: the installed 1.24.5 build logged
``falling back to plain HTTP`` while the launcher opened ``https://127.0.0.1:8888``.
A browser pointed at a scheme nothing listens for shows a protocol error on a blank tab,
and the operator reported it as a black screen. These tests pin the probe behaviour the
fix rests on.
"""
from __future__ import annotations

import errno
import unittest
from unittest.mock import MagicMock, patch

import launcher_net


def _fake_urlopen(answering_schemes):
    """Stand in for urllib.request.urlopen: only the listed schemes accept a connection.

    A refused connection is raised as ConnectionRefusedError, which is an OSError — the
    same shape the real stack produces when nothing is listening on the port.
    """
    def fake(url, timeout=None, context=None):
        scheme = url.split("://", 1)[0]
        if scheme not in answering_schemes:
            raise ConnectionRefusedError(errno.ECONNREFUSED, "connection refused")
        return MagicMock()      # a 200/401 answer: the response object is not inspected
    return fake


class AnswersTests(unittest.TestCase):
    def test_http_answer_is_detected(self):
        with patch("urllib.request.urlopen", _fake_urlopen({"http"})):
            self.assertTrue(launcher_net.answers("http://127.0.0.1:8888", timeout_s=0))

    def test_tls_probe_is_used_for_https_url_only(self):
        """The TLS decision comes off the URL, so the caller cannot contradict it.

        Probing plain HTTP with a TLS context is what made the server's log say
        "Invalid HTTP request received" in the field, and probing HTTPS without one
        can never succeed.
        """
        seen = []

        def fake(url, timeout=None, context=None):
            seen.append(context)
            raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")

        with patch("urllib.request.urlopen", fake):
            launcher_net.answers("https://127.0.0.1:8888", timeout_s=0)
            launcher_net.answers("http://127.0.0.1:8888", timeout_s=0)

        self.assertIsNotNone(seen[0], "https probe must offer TLS")
        self.assertIsNone(seen[1], "http probe must not offer TLS")

    def test_secure_override_wins_over_scheme(self):
        seen = []

        def fake(url, timeout=None, context=None):
            seen.append(context)
            raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")

        with patch("urllib.request.urlopen", fake):
            launcher_net.answers("http://127.0.0.1:8888", timeout_s=0, secure=True)
        self.assertIsNotNone(seen[0])

    def test_exited_process_stops_the_wait(self):
        """Never spend the whole timeout on a child that already died."""
        proc = MagicMock()
        proc.poll.return_value = 3
        with patch("urllib.request.urlopen", _fake_urlopen(set())):
            self.assertFalse(launcher_net.answers("http://127.0.0.1:8888", proc=proc,
                                                  timeout_s=30))

    def test_health_path_is_probed(self):
        requested = []

        def fake(url, timeout=None, context=None):
            requested.append(url)
            raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")

        with patch("urllib.request.urlopen", fake):
            launcher_net.answers("http://localhost:8888/", timeout_s=0)
        self.assertEqual(requested, ["http://localhost:8888/api/health"],
                         "a trailing slash must not double up the probe path")


class FirstAnsweringTests(unittest.TestCase):
    def test_returns_the_scheme_that_answers(self):
        with patch("urllib.request.urlopen", _fake_urlopen({"http"})):
            self.assertEqual(
                launcher_net.first_answering(
                    ["https://127.0.0.1:8888", "http://127.0.0.1:8888"], timeout_s=0),
                "http://127.0.0.1:8888")

    def test_order_is_the_callers_policy(self):
        """Both schemes answer -> the first one asked for wins."""
        with patch("urllib.request.urlopen", _fake_urlopen({"http", "https"})):
            self.assertEqual(
                launcher_net.first_answering(
                    ["https://127.0.0.1:8888", "http://127.0.0.1:8888"], timeout_s=0),
                "https://127.0.0.1:8888")

    def test_none_when_nothing_answers(self):
        with patch("urllib.request.urlopen", _fake_urlopen(set())):
            self.assertIsNone(
                launcher_net.first_answering(["http://127.0.0.1:8888"], timeout_s=0))


class ServedUrlTests(unittest.TestCase):
    def test_preferred_is_kept_when_it_answers(self):
        preferred = "https://127.0.0.1:8888"
        with patch("urllib.request.urlopen", _fake_urlopen({"https"})):
            self.assertEqual(launcher_net.served_url(
                preferred, launcher_net.other_scheme(preferred), timeout_s=0), preferred)

    def test_falls_back_to_the_answering_scheme(self):
        """The v1.24.5 field case: launcher wants HTTPS, server serves HTTP."""
        preferred = "https://127.0.0.1:8888"
        with patch("urllib.request.urlopen", _fake_urlopen({"http"})):
            self.assertEqual(launcher_net.served_url(
                preferred, launcher_net.other_scheme(preferred), timeout_s=0),
                "http://127.0.0.1:8888")

    def test_preferred_when_neither_answers(self):
        """No better option exists; the caller's timeout path explains it better."""
        preferred = "https://127.0.0.1:8888"
        with patch("urllib.request.urlopen", _fake_urlopen(set())):
            self.assertEqual(launcher_net.served_url(
                preferred, launcher_net.other_scheme(preferred), timeout_s=0), preferred)


class TlsContextFailureTests(unittest.TestCase):
    """A transient `_ssl` import failure must not be fatal (field log 2026-10-10).

    A Windows frozen bundle raised
    ``ImportError: DLL load failed while importing _ssl: 另一个程序正在使用此文件``
    from inside ``tls_context()`` — the previous instance still held the extracted file —
    and it escaped the probe, so the launcher died with “MRRC Modern 启动失败” while the
    server it was about to start would have run fine. A probe answers a question; it may
    never end the program.
    """

    def test_the_https_probe_reports_no_answer_instead_of_raising(self):
        with patch.object(launcher_net, "tls_context",
                          side_effect=ImportError("DLL load failed while importing _ssl")):
            self.assertFalse(launcher_net.answers("https://127.0.0.1:8888", timeout_s=0))

    def test_the_launcher_still_reaches_the_scheme_that_answers(self):
        """The whole point: the operator gets a working window, not a failure dialog."""
        with patch.object(launcher_net, "tls_context",
                          side_effect=ImportError("DLL load failed while importing _ssl")), \
                patch("urllib.request.urlopen", _fake_urlopen({"http"})):
            self.assertEqual(
                launcher_net.served_url("https://127.0.0.1:8888",
                                        "http://127.0.0.1:8888", timeout_s=0),
                "http://127.0.0.1:8888")

    def test_a_broken_tls_stack_does_not_break_the_first_answering_chain(self):
        with patch.object(launcher_net, "tls_context", side_effect=ImportError("nope")), \
                patch("urllib.request.urlopen", _fake_urlopen({"http"})):
            self.assertEqual(
                launcher_net.first_answering(
                    ["https://127.0.0.1:8888", "http://127.0.0.1:8888"], timeout_s=0),
                "http://127.0.0.1:8888")


class OtherSchemeTests(unittest.TestCase):
    def test_round_trip_keeps_host_and_port(self):
        for url in ("https://127.0.0.1:8888", "http://localhost:8888"):
            flipped = launcher_net.other_scheme(url)
            self.assertNotEqual(flipped, url)
            self.assertEqual(launcher_net.other_scheme(flipped), url)
            self.assertEqual(flipped.split("://", 1)[1], url.split("://", 1)[1])

    def test_unknown_scheme_is_returned_unchanged(self):
        self.assertEqual(launcher_net.other_scheme("ftp://host:21"), "ftp://host:21")


if __name__ == "__main__":
    unittest.main()
