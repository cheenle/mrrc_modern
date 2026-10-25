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

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

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

    def test_restart_bluetooth_takes_no_arguments(self):
        """蓝牙 HID 唯一的补救通道：固定 argv、零参数，别的什么都不许要。

        2026-10-19 现场：配对成功、链路也通，但 bluez 手里的 GATT 数据库残缺，
        键盘设备建了又拆；清 bond 重配三次都没用，重启 bluetoothd 一次就好。
        服务用户没有 root，这条动词是它唯一能走的路。
        """
        self.assertEqual(box_helper.argv_for("restart-bluetooth", []),
                         ["systemctl", "restart", "bluetooth"])
        for args in (["now"], ["--force"], [""], [";"], ["&& reboot"]):
            with self.subTest(args=args):
                self.assertIsNone(box_helper.argv_for("restart-bluetooth", args))

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

    def test_set_hostname_refuses_a_trailing_newline(self):
        """Python's `$` matches *before* a final `\\n`, so a `$`-anchored regex
        accepts `abc\\n` and would hand the verb a hostname with a newline stuck
        to it. `\\Z` is the anchor that means end-of-string, and this is why the
        two validators use it."""
        self.assertIsNone(box_helper.argv_for("set-hostname", ["abc\n"]))

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
        cases = [("reboot", []), ("avahi-reload", []), ("restart-bluetooth", []),
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
    """Every request in this class runs against a fake `subprocess.run`, so no
    test here can ever execute a real command — the one that would (`reboot`)
    included, today or when a case is added later. The fake records the argv so a
    test can say what *would* have run instead of only that the reply looked
    well-formed."""

    def setUp(self):
        self.run_calls = []

        def fake_run(argv, **kwargs):
            self.run_calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        patcher = mock.patch.object(box_helper.subprocess, "run", fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_malformed_request_is_refused_without_running_anything(self):
        for request in (None, [], "reboot", 7,
                        {"verb": "reboot", "args": "now"},
                        {"verb": "reboot", "args": [1, 2]},
                        {"args": []}, {}):
            with self.subTest(request=request):
                reply = box_helper.handle(request)
                self.assertFalse(reply["ok"])
                self.assertTrue(reply["error"])
        self.assertEqual(self.run_calls, [])

    def test_the_reply_is_always_a_dict_with_ok(self):
        for request in ({}, {"verb": "nope"}, {"verb": "reboot"}):
            with self.subTest(request=request):
                reply = box_helper.handle(request)
                self.assertIsInstance(reply, dict)
                self.assertIn("ok", reply)

    def test_fix_resolv_conf_takes_no_arguments_and_runs_the_shared_script(self):
        """The resolver repair is one script, shared with `mrrc-update`.

        2026-10-25: boxes built before that day carry the *build machine's*
        /etc/resolv.conf (Docker Desktop's 192.168.65.7 on MRRC-BG1SB), so glibc
        waits 5 s per lookup and fails. Fixing it needs root; the service user is
        not root. The script path is a constant and takes no argument, so this verb
        cannot be turned into "run something else as root".
        """
        self.assertEqual(box_helper.argv_for("fix-resolv-conf", []),
                         ["bash", box_helper.FIX_RESOLV_CONF])
        for args in (["/tmp/evil.sh"], ["--force"], [""]):
            with self.subTest(args=args):
                self.assertIsNone(box_helper.argv_for("fix-resolv-conf", args))

    def test_the_repair_ships_its_output_back(self):
        """The page and the Windows tool show what was found and changed, so the
        reply carries the script's stdout — a bare {"ok": true} would leave the
        operator unable to tell "repaired" from "nothing to do"."""
        self.assertIn("fix-resolv-conf", box_helper.OUTPUT_VERBS)

    def test_a_reboot_runs_exactly_the_tables_argv(self):
        """A reboot must reach `subprocess.run` as the literal argv the verb
        table chose. Asserting the argv — not just `"ok" in reply` — is what
        makes this a test of the table rather than of the reply's shape."""
        reply = box_helper.handle({"verb": "reboot", "args": []})
        self.assertTrue(reply["ok"])
        self.assertEqual(self.run_calls, [["systemctl", "reboot"]])


class _StreamingConn:
    """A connection over a fixed byte payload that never reports EOF.

    `recv` returns the next slice and *never* the empty bytes that mean the peer
    closed, so a helper that kept reading would keep getting data."""

    def __init__(self, payload: bytes):
        self._payload = payload
        self._served = 0
        self.recv_calls = 0
        self.sent = b""

    def recv(self, size):
        self.recv_calls += 1
        chunk = self._payload[self._served:self._served + size]
        self._served += len(chunk)
        return chunk

    def sendall(self, data):
        self.sent += data


class AnswerTests(unittest.TestCase):
    def test_an_unbounded_stream_is_refused_and_reading_stops(self):
        """A peer that streams without ever sending a newline must not make a
        root process buffer without limit: MAX_LINE bytes with no newline is a
        malformed request, and the helper stops reading rather than draining an
        endless stream."""
        conn = _StreamingConn(b"x" * (box_helper.MAX_LINE * 4))
        box_helper._answer(conn)
        reply = json.loads(conn.sent.decode("utf-8"))
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "malformed request")
        self.assertEqual(conn.recv_calls, 1)
        self.assertLess(conn._served, len(conn._payload))

    def test_a_normal_line_is_still_answered(self):
        """The cap sits at MAX_LINE, far above any real request (the largest is
        a 63-character hostname), so an ordinary one-line request still parses
        and dispatches."""
        payload = json.dumps({"verb": "nope", "args": []}).encode("utf-8") + b"\n"
        conn = _StreamingConn(payload)
        box_helper._answer(conn)
        reply = json.loads(conn.sent.decode("utf-8"))
        self.assertEqual(reply["error"], "refused: nope")


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


class SetPasswordTests(unittest.TestCase):
    """root / mrrc 的口令可以从面板改，但秘密只能走 stdin。

    `chpasswd` 读的是 `user:password` 行，所以换行会被读成第二行（口令注入），
    而 argv 是全世界可读的（/proc/<pid>/cmdline）——两件事都在这里钉住。
    """

    def test_the_two_accounts_the_image_ships_are_allowed(self):
        for user in ("root", "mrrc"):
            self.assertEqual(box_helper.password_problem(user, "abcd1234"), "")

    def test_an_arbitrary_user_name_is_refused(self):
        self.assertNotEqual(box_helper.password_problem("nobody", "abcd1234"), "")
        self.assertNotEqual(box_helper.password_problem("", "abcd1234"), "")

    def test_a_short_password_is_refused(self):
        self.assertNotEqual(box_helper.password_problem("root", "1234567"), "")

    def test_a_newline_is_refused_because_chpasswd_reads_lines(self):
        self.assertNotEqual(box_helper.password_problem("root", "abcd1234\nroot:x"), "")

    def test_the_secret_reaches_chpasswd_on_stdin_and_never_in_argv(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["input"] = kwargs.get("input")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(box_helper.subprocess, "run", fake_run):
            result = box_helper.do_set_password(["root", "abcd1234"])
        self.assertTrue(result["ok"])
        self.assertEqual(seen["argv"], ["chpasswd"])
        self.assertEqual(seen["input"], "root:abcd1234\n")
        self.assertNotIn("abcd1234", " ".join(seen["argv"]))

