"""The root helper's verb table, tested as a table.

This process runs as root and answers a socket. Everything that makes that
acceptable is in `argv_for`: four verbs, each with its own argument check, all of
them returning an argv *list* that a test can compare to an exact expected value.
There is no string concatenation anywhere, so there is no shell to inject into —
and the assertions below are written to keep it that way.

The peer check is tested as a pure function for the same reason: `SO_PEERCRED`
comes from the kernel, and the decision made from it is the part that can be
wrong.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "linux"))

import box_helper  # noqa: E402


class VerbTableTests(unittest.TestCase):
    def test_reboot_takes_no_arguments(self):
        self.assertEqual(box_helper.argv_for("reboot", []),
                         ["systemctl", "reboot"])
        for args in (["now"], ["--force"], [""]):
            with self.subTest(args=args):
                self.assertIsNone(box_helper.argv_for("reboot", args))

    def test_avahi_reload_takes_no_arguments(self):
        self.assertEqual(box_helper.argv_for("avahi-reload", []),
                         ["systemctl", "reload", "avahi-daemon"])
        self.assertIsNone(box_helper.argv_for("avahi-reload", ["x"]))

    def test_set_hostname_accepts_the_shipped_default(self):
        """`MRRC-Modern` is the name in the image. A lowercase-only validator
        would leave the box unable to keep its own hostname."""
        self.assertEqual(
            box_helper.argv_for("set-hostname", ["MRRC-Modern"]),
            ["hostnamectl", "set-hostname", "--static", "MRRC-Modern"])

    def test_set_hostname_refuses_anything_that_is_not_a_hostname(self):
        for args in ([], ["-a"], ["--help"], ["a b"], ["a/b"], ["a:b"],
                     [".."], ["a" * 64], ["名字"], ["$(reboot)"],
                     ["a;reboot"], ["`reboot`"], ["a\nreboot"]):
            with self.subTest(args=args):
                self.assertIsNone(box_helper.argv_for("set-hostname", args))

    def test_set_timezone_needs_a_real_zone(self):
        if os.path.isfile("/usr/share/zoneinfo/UTC"):
            self.assertEqual(box_helper.argv_for("set-timezone", ["UTC"]),
                             ["timedatectl", "set-timezone", "UTC"])

    def test_set_timezone_refuses_a_path_or_a_made_up_zone(self):
        for args in ([], [".."], ["../../etc/passwd"], ["/etc/passwd"],
                     ["-x"], ["Not/AZone"], ["Asia/Shanghai/x"],
                     ["Asia/Shanghai\nreboot"]):
            with self.subTest(args=args):
                self.assertIsNone(box_helper.argv_for("set-timezone", args))

    def test_an_unknown_verb_is_refused(self):
        for verb in ("", "sh", "exec", "install", "rm", "systemctl", None):
            with self.subTest(verb=verb):
                self.assertIsNone(box_helper.argv_for(verb, []))

    def test_no_verb_can_ever_reach_a_shell(self):
        """The argv is a list, and its first element is a fixed absolute-looking
        program name. There is no `sh -c` in the table, so there is nothing for a
        metacharacter to be interpreted by."""
        cases = [("reboot", []), ("avahi-reload", []),
                 ("set-hostname", ["box"]),
                 ("set-timezone", ["UTC"])]
        for verb, args in cases:
            argv = box_helper.argv_for(verb, args)
            if argv is None:
                continue
            with self.subTest(verb=verb):
                self.assertIsInstance(argv, list)
                self.assertNotIn("sh", argv)
                self.assertNotIn("-c", argv)
                self.assertTrue(all(isinstance(part, str) for part in argv))


class HandleTests(unittest.TestCase):
    def test_a_malformed_request_is_refused_without_running_anything(self):
        for request in (None, [], "reboot", 7,
                        {"verb": "reboot", "args": "now"},
                        {"verb": "reboot", "args": [1, 2]},
                        {"args": []}, {}):
            with self.subTest(request=request):
                reply = box_helper.handle(request)
                self.assertFalse(reply["ok"])
                self.assertTrue(reply["error"])

    def test_the_reply_is_always_a_dict_with_ok(self):
        for request in ({}, {"verb": "nope"}, {"verb": "reboot"}):
            with self.subTest(request=request):
                reply = box_helper.handle(request)
                self.assertIsInstance(reply, dict)
                self.assertIn("ok", reply)


class PeerTests(unittest.TestCase):
    def test_only_the_expected_uid_is_allowed(self):
        self.assertTrue(box_helper.peer_is_allowed(1001, 1001))
        for uid in (0, 1000, 1002, 65534):
            with self.subTest(uid=uid):
                self.assertFalse(box_helper.peer_is_allowed(uid, 1001))


class SyncHostsTests(unittest.TestCase):
    def test_it_replaces_the_127_0_1_1_line(self):
        """`hostnamectl set-hostname` stopped touching /etc/hosts in systemd 240.
        Without this, the box answers to its new name on mDNS and to nothing at
        all locally — and Debian's `hostname -f` starts failing, which `sudo`
        and some mailers use."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hosts"
            path.write_text("127.0.0.1\tlocalhost\n"
                            "127.0.1.1\tarmbian\n"
                            "::1\tlocalhost ip6-localhost\n",
                            encoding="utf-8")
            box_helper.sync_hosts("MRRC-Modern", str(path))
            text = path.read_text(encoding="utf-8")
            self.assertIn("127.0.1.1\tMRRC-Modern", text)
            self.assertNotIn("armbian", text)
            self.assertIn("127.0.0.1\tlocalhost", text)
            self.assertIn("::1\tlocalhost ip6-localhost", text)

    def test_it_adds_the_line_when_there_is_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hosts"
            path.write_text("127.0.0.1\tlocalhost\n", encoding="utf-8")
            box_helper.sync_hosts("MRRC-Modern", str(path))
            self.assertIn("127.0.1.1\tMRRC-Modern",
                          path.read_text(encoding="utf-8"))

    def test_a_missing_file_is_reported_not_fatal(self):
        self.assertFalse(box_helper.sync_hosts("box", "/no/such/hosts"))


if __name__ == "__main__":
    unittest.main()
