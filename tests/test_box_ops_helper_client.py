"""The client half of the root helper, tested without a helper.

`sender` is the injection point: it takes the request dict and a timeout and
returns one line of JSON. That is the whole transport, so everything below —
including the failure modes that matter — runs on the desktop builds too.

Two contracts this file exists for:

* **Nothing raises.** The helper may be absent (desktop, unit restarting, socket
  permissions wrong) and the page may still be open in front of a user. Every
  one of those is a `{"ok": False}` value, never an exception, because the
  alternative is a 500 in a request handler and a panel that hangs.
* **Nothing is concatenated.** The request is one JSON object with a verb and an
  argv-shaped list. There is no code path here that builds a command string.
"""
from __future__ import annotations

import json
import unittest

import box_ops


def answering(payload: dict = None, text: str = None):
    """A sender that records what it was asked and returns a canned reply."""
    calls = []

    def sender(request, timeout):
        calls.append(request)
        if text is not None:
            return text
        return json.dumps(payload if payload is not None else {"ok": True})

    sender.calls = calls
    return sender


def exploding(kind=OSError):
    def sender(request, timeout):
        raise kind("no such file or directory")
    return sender


class CallHelperTests(unittest.TestCase):
    def test_a_request_is_one_json_object_with_a_verb_and_a_list(self):
        sender = answering()
        box_ops.call_helper("set-hostname", "MRRC-Modern", sender=sender)
        self.assertEqual(
            json.loads(json.dumps(sender.calls[0])),
            {"verb": "set-hostname", "args": ["MRRC-Modern"]},
        )

    def test_arguments_are_stringified_so_the_line_never_fails_to_encode(self):
        sender = answering()
        box_ops.call_helper("set-timezone", "Asia/Shanghai", sender=sender)
        self.assertEqual(sender.calls[0]["args"], ["Asia/Shanghai"])

    def test_a_reply_is_parsed(self):
        sender = answering(payload={"ok": True, "note": "done"})
        self.assertEqual(box_ops.call_helper("reboot", sender=sender),
                         {"ok": True, "note": "done"})

    def test_an_unreachable_helper_is_a_value(self):
        result = box_ops.call_helper("reboot", sender=exploding())
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_a_permission_error_is_also_a_value(self):
        result = box_ops.call_helper("reboot", sender=exploding(PermissionError))
        self.assertFalse(result["ok"])

    def test_garbage_from_the_helper_is_a_value(self):
        for text in ("", "not json", "[1, 2, 3]", "null", '{"ok":'):
            with self.subTest(text=text):
                result = box_ops.call_helper(
                    "reboot", sender=answering(text=text))
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])


class HostnameValidationTests(unittest.TestCase):
    def test_the_shipped_default_is_valid(self):
        """`MRRC-Modern` has an uppercase M. A lowercase-only regex — the shape
        every example on the internet uses — would reject the box's own name."""
        self.assertEqual(box_ops.hostname_problem("MRRC-Modern"), "")

    def test_ordinary_names_are_valid(self):
        for name in ("mrrc", "mrrc-modern", "box2", "a", "B" * 63):
            with self.subTest(name=name):
                self.assertEqual(box_ops.hostname_problem(name), "")

    def test_what_is_not_a_hostname(self):
        for name in ("", "   ", "-leading", "trailing-", "has space",
                     "has_underscore", "dot.name", "a" * 64, "名字",
                     "with/slash", "with:colon"):
            with self.subTest(name=name):
                self.assertTrue(box_ops.hostname_problem(name), name)


class TimezoneValidationTests(unittest.TestCase):
    def test_a_real_zone_passes(self):
        for zone in ("Asia/Shanghai", "UTC", "Europe/London"):
            with self.subTest(zone=zone):
                if zone == "UTC" or __import__("os").path.exists(
                        "/usr/share/zoneinfo/" + zone):
                    self.assertEqual(box_ops.timezone_problem(zone), "")

    def test_a_path_traversal_or_a_made_up_zone_does_not(self):
        for zone in ("", "  ", "..", "../../etc/passwd", "/etc/passwd",
                     "Asia/../../etc/passwd", "Not/AZone"):
            with self.subTest(zone=zone):
                self.assertTrue(box_ops.timezone_problem(zone), zone)


class VerbTests(unittest.TestCase):
    def test_set_hostname_validates_before_it_asks(self):
        """A rejected name must never reach the socket: the helper validates too,
        but a page that round-trips to find out is a page that reports the
        helper's error wording instead of its own."""
        sender = answering()
        result = box_ops.set_hostname("-bad", sender=sender)
        self.assertFalse(result["ok"])
        self.assertEqual(sender.calls, [])

    def test_set_hostname_forwards_a_good_name(self):
        sender = answering()
        box_ops.set_hostname("MRRC-Modern", sender=sender)
        self.assertEqual(sender.calls[0],
                         {"verb": "set-hostname", "args": ["MRRC-Modern"]})

    def test_set_timezone_validates_before_it_asks(self):
        sender = answering()
        result = box_ops.set_timezone("../etc", sender=sender)
        self.assertFalse(result["ok"])
        self.assertEqual(sender.calls, [])

    def test_reboot_and_avahi_reload_take_no_arguments(self):
        sender = answering()
        box_ops.reboot(sender=sender)
        box_ops.reload_avahi(sender=sender)
        self.assertEqual([c["verb"] for c in sender.calls],
                         ["reboot", "avahi-reload"])
        self.assertEqual([c["args"] for c in sender.calls], [[], []])


class PasswordValidationTests(unittest.TestCase):
    def test_a_short_password_is_refused(self):
        self.assertTrue(box_ops.password_problem("short", "short"))

    def test_a_mismatch_is_refused(self):
        self.assertTrue(
            box_ops.password_problem("longenough1", "longenough2"))

    def test_an_empty_password_is_refused(self):
        self.assertTrue(box_ops.password_problem("", ""))

    def test_eight_characters_is_the_floor(self):
        """The same floor the existing POST /api/setup enforces — the onboarding
        page must not be the softer door into the same key."""
        self.assertEqual(
            box_ops.password_problem("12345678", "12345678"), "")

    def test_surrounding_spaces_are_part_of_the_password(self):
        """A password is used byte-for-byte. Trimming it here would set a
        password the user did not type, and they would not be able to log in."""
        self.assertTrue(
            box_ops.password_problem(" 12345678", "12345678"))


if __name__ == "__main__":
    unittest.main()
