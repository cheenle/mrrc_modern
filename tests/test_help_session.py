"""Call help: the credential rules first, because everything else is replaceable.

The password is the one value in this feature that must never come to rest. These
tests are written before the code so the rules are the spec rather than a
description of what got built.
"""
from __future__ import annotations

import re
import os
import unittest

import paramiko

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


class _FakeServer:
    """Records the credential injection, which is the one wiring fact this file can
    check without a socket: the session owns the pair, the server only checks it."""

    def __init__(self):
        self.installed = []
        self.stopped = 0

    def set_credentials(self, user, pw):
        self.installed.append((user, pw))

    def stop(self):
        self.stopped += 1


class StateMachineTests(unittest.TestCase):
    """off → arming → open → closed, with one way out from every state.

    The failure this guards is the half-cleaned one: an arming that bound a socket
    and then failed to get a tunnel port, returning to `off` and leaving the
    listener behind.
    """

    def setUp(self):
        self.now = 1_000_000.0
        self.events = []
        self.server = _FakeServer()
        self.opened = []
        self.tunnel_calls = []
        self.closed_portal = []

    def _session(self, **kw):
        params = dict(
            opener=lambda minutes: 19000,
            tunnel=lambda lp, rp, add: self.tunnel_calls.append((lp, rp, add)),
            listener=lambda: 34567,
            closer=lambda: self.closed_portal.append(self.now),
            clock=lambda: self.now,
            on_event=self.events.append,
            server=self.server,
        )
        params.update(kw)
        return help_session.HelpSession(**params)

    def test_it_starts_off(self):
        self.assertEqual(self._session().state, help_session.OFF)

    def test_enable_opens_and_reports_what_the_ui_needs(self):
        s = self._session()
        st = s.enable(minutes=30)
        self.assertEqual(s.state, help_session.OPEN)
        self.assertEqual(st["port"], 19000)
        self.assertEqual(st["expires_at"], self.now + 30 * 60)
        self.assertTrue(st["username"].startswith("help-"))
        self.assertNotIn("password", st, "the status dict has no password field")
        self.assertEqual([t[2] for t in self.tunnel_calls], [True], "the proxy was added")

    def test_the_credentials_are_injected_into_the_server(self):
        s = self._session()
        s.enable(minutes=30)
        self.assertEqual(len(self.server.installed), 1)
        user, pw = self.server.installed[0]
        self.assertEqual(user, s.status()["username"])
        self.assertEqual(pw, s.password_for_display())

    def test_a_customer_supplied_pair_is_used_as_given(self):
        s = self._session()
        st = s.enable(minutes=30, username="zhang-san", password="correct horse battery")
        self.assertEqual(st["username"], "zhang-san")
        self.assertEqual(s.password_for_display(), "correct horse battery")

    def test_a_bad_customer_username_is_refused_before_anything_is_bound(self):
        s = self._session()
        with self.assertRaises(ValueError):
            s.enable(minutes=30, username="has space")
        self.assertEqual(s.state, help_session.CLOSED, "refused, and nothing left open")

    def test_a_too_short_password_is_refused(self):
        s = self._session()
        with self.assertRaises(ValueError):
            s.enable(minutes=30, password="short")

    def test_arming_failure_still_goes_through_close(self):
        """绑定成功、申请隧道失败 ⇒ 必须把监听拆掉（否则留个孤儿）。"""
        def boom(port, remote, add):
            raise RuntimeError("reload failed")
        s = self._session(tunnel=boom)
        with self.assertRaises(RuntimeError):
            s.enable(minutes=30)
        self.assertEqual(s.state, help_session.CLOSED)
        self.assertEqual(s.close_reason, "error")
        self.assertGreaterEqual(self.server.stopped, 1, "the listener was torn down")
        self.assertIn("closed", [e["event"] for e in self.events])

    def test_portal_failure_before_the_tunnel_also_cleans_up(self):
        def boom(minutes):
            raise RuntimeError("portal unreachable")
        s = self._session(opener=boom)
        with self.assertRaises(RuntimeError):
            s.enable(minutes=30)
        self.assertEqual(s.state, help_session.CLOSED)
        self.assertEqual(self.tunnel_calls, [], "no proxy to remove")
        self.assertGreaterEqual(self.server.stopped, 1)

    def test_expiry_closes_it(self):
        s = self._session()
        s.enable(minutes=30)
        self.now += 30 * 60 + 1
        self.assertEqual(s.tick(), "expired")
        self.assertEqual(s.state, help_session.CLOSED)
        self.assertEqual(self.tunnel_calls[-1][2], False, "the proxy was removed")

    def test_it_does_not_auto_renew(self):
        s = self._session()
        s.enable(minutes=30)
        self.now += 30 * 60 + 1
        s.tick()
        self.assertIsNone(s.tick())

    def test_the_window_has_a_ceiling(self):
        s = self._session()
        st = s.enable(minutes=10_000)
        self.assertEqual(st["expires_at"] - self.now,
                         help_session.MAX_WINDOW_MINUTES * 60)

    def test_close_is_idempotent_and_the_first_reason_wins(self):
        s = self._session()
        s.enable(minutes=30)
        s.close("user")
        s.close("portal")
        self.assertEqual(s.close_reason, "user")

    def test_a_second_enable_while_open_is_refused(self):
        s = self._session()
        s.enable(minutes=30)
        with self.assertRaises(RuntimeError):
            s.enable(minutes=30)

    def test_closing_notifies_the_portal(self):
        s = self._session()
        s.enable(minutes=30)
        s.close("user")
        self.assertEqual(len(self.closed_portal), 1)

    def test_a_failing_portal_notification_does_not_block_the_rest(self):
        """关闭必须尽量走完：门户报错不能把监听留在那里。"""
        s = self._session(closer=lambda: (_ for _ in ()).throw(RuntimeError("502")))
        s.enable(minutes=30)
        s.close("user")
        self.assertEqual(s.state, help_session.CLOSED)
        self.assertGreaterEqual(self.server.stopped, 1)

    def test_close_zeroes_the_password(self):
        s = self._session()
        s.enable(minutes=30)
        self.assertTrue(s.password_for_display())
        s.close("user")
        self.assertEqual(s.password_for_display(), "")


class AuditTests(unittest.TestCase):
    """What the customer can see, and what the support bundle carries."""

    def setUp(self):
        self.now = 1_000_000.0
        self.events = []

    def _session(self):
        return help_session.HelpSession(
            opener=lambda minutes: 19000, tunnel=lambda lp, rp, add: None,
            listener=lambda: 34567, clock=lambda: self.now,
            on_event=self.events.append, server=_FakeServer())

    def test_open_and_close_are_recorded(self):
        s = self._session()
        s.enable(minutes=30)
        s.close("user")
        kinds = [e["event"] for e in self.events]
        self.assertEqual(kinds, ["opened", "closed"])
        self.assertEqual(self.events[1]["reason"], "user")

    def test_no_event_carries_the_password(self):
        s = self._session()
        s.enable(minutes=30)
        pw = s.password_for_display()
        s.close("user")
        self.assertTrue(pw)
        self.assertNotIn(pw, repr(self.events))

    def test_auth_failures_are_counted_and_evented(self):
        s = self._session()
        s.enable(minutes=30)
        for _ in range(help_session.MAX_AUTH_FAILURES):
            s.note_auth_failure()
        self.assertEqual(s.state, help_session.CLOSED)
        self.assertEqual(s.close_reason, "auth_failures")
        self.assertIn("auth_fail", [e["event"] for e in self.events])

    def test_a_connection_is_counted(self):
        s = self._session()
        s.enable(minutes=30)
        s.note_connection(duration_s=12.5)
        self.assertEqual(s.status()["connections"], 1)
        self.assertIn("disconnect", [e["event"] for e in self.events])


class SshServerTests(unittest.TestCase):
    """A real paramiko client against the real server, on loopback.

    No tunnel, no portal, no hardware: this is the part of the feature that must
    work on every platform, so it runs everywhere.
    """

    def setUp(self):
        self.events = []
        self.server = help_session.SshHelpServer(on_event=self.events.append)
        self.port = self.server.start()          # 绑 127.0.0.1:0，返回端口
        self.user, self.pw = help_session.make_credentials()
        self.server.set_credentials(self.user, self.pw)

    def tearDown(self):
        self.server.stop()

    def _client(self, user=None, pw=None):
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect("127.0.0.1", port=self.port, username=user or self.user,
                  password=pw or self.pw, allow_agent=False, look_for_keys=False,
                  timeout=5)
        return c

    def test_the_listener_is_loopback_only(self):
        """围栏 1：不是 0.0.0.0。"""
        self.assertEqual(self.server.bind_host, "127.0.0.1")
        self.assertEqual(help_session.BIND_HOST, "127.0.0.1")

    def test_the_password_is_what_gets_in(self):
        self._client().close()

    def test_a_wrong_password_is_refused(self):
        import paramiko
        with self.assertRaises(paramiko.AuthenticationException):
            self._client(pw="wrong-password-here")

    def test_a_wrong_username_is_refused(self):
        import paramiko
        with self.assertRaises(paramiko.AuthenticationException):
            self._client(user="help-0000")

    def test_public_key_auth_is_refused(self):
        """围栏 6：只允许口令认证（否则一把授权的公钥就是一个后门）。"""
        import paramiko
        key = paramiko.RSAKey.generate(2048)   # paramiko 5 has no Ed25519Key.generate
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        with self.assertRaises(paramiko.AuthenticationException):
            c.connect("127.0.0.1", port=self.port, username=self.user,
                      pkey=key, allow_agent=False, look_for_keys=False, timeout=5)

    def test_the_probe_command_returns_its_mark(self):
        c = self._client()
        try:
            chan = c.get_transport().open_session()
            chan.exec_command(help_session.probe_command())
            out = chan.makefile().read().decode()
        finally:
            c.close()
        self.assertIn(help_session.PROBE_MARK, out)

    def test_any_other_exec_command_is_refused(self):
        """exec 只提供那条探针；真正的活是交互式 shell。"""
        c = self._client()
        try:
            chan = c.get_transport().open_session()
            # 被拒的请求在客户端就是一次异常，不是一个 exit status。
            with self.assertRaises(paramiko.ssh_exception.SSHException):
                chan.exec_command("echo pwned")
        finally:
            c.close()

    def test_failed_auths_reach_the_session(self):
        import paramiko
        for _ in range(2):
            with self.assertRaises(paramiko.AuthenticationException):
                self._client(pw="wrong-password-here")
        self.assertEqual(len([e for e in self.events if e["event"] == "auth_fail"]), 2)

    @unittest.skipUnless(os.name == "posix",
                         "pty 是 POSIX 的：Windows 的 powershell pty 路仍是未验边界"
                         "（docs/remote_help.md 已如实标注）")
    def test_an_interactive_shell_runs_a_command(self):
        """交互式那条路（pty 请求 → 起平台 shell → 双向泵）。

        这是支持者真正要用的形态，而它此前**从没被驱动过**：单元测试只走 exec 探针，
        于是 `channel.get_terminal_type()` 这个不存在的 API 一路活到真机（2026-07-12
        实测才暴露）。这条测试就是那次教训的守卫。
        """
        import time as _t
        c = self._client()
        try:
            chan = c.get_transport().open_session()
            chan.get_pty(term="xterm-256color", width=100, height=30)
            chan.invoke_shell()
            _t.sleep(0.8)
            chan.send("echo SHELL-OK-$((6*7))\n")
            out, deadline = "", _t.time() + 8
            while _t.time() < deadline:
                if chan.recv_ready():
                    out += chan.recv(4096).decode(errors="replace")
                    if "SHELL-OK-42" in out:
                        break
                _t.sleep(0.1)
            self.assertIn("SHELL-OK-42", out, f"交互式 shell 没有回话：{out[:200]!r}")
            chan.send("exit\n")
        finally:
            c.close()

    def test_stop_closes_the_port(self):
        import socket
        self.server.stop()
        s = socket.socket()
        try:
            self.assertRaises(OSError, s.connect, ("127.0.0.1", self.port))
        finally:
            s.close()

    def test_stop_forgets_the_credentials(self):
        self.server.stop()
        self.assertEqual(self.server.check(self.user, self.pw), False)


class RealListenerWiringTests(unittest.TestCase):
    """enable() really opens a port, and close() really closes it."""

    def test_the_session_owns_the_listener(self):
        import socket
        s = help_session.build_session(portal_opener=lambda minutes: 19000,
                                       tunnel=lambda lp, rp, add: None)
        s.enable(minutes=5)
        port = s.local_port
        self.assertGreater(port, 0)
        probe = socket.create_connection(("127.0.0.1", port), timeout=2)
        probe.close()
        s.close("user")
        s2 = socket.socket()
        try:
            self.assertRaises(OSError, s2.connect, ("127.0.0.1", port))
        finally:
            s2.close()


if __name__ == "__main__":
    unittest.main()


class HeartbeatTests(unittest.TestCase):
    """每 20 秒一跳：本地判到期 + 读门户的 stop。

    两件事的顺序不能反：先 `tick()`，这样即使门户不可达，窗口也会在自己的截止时刻
    关掉——"到期"是客户与界面都看得见的承诺，不该依赖一次网络往返。
    """

    def setUp(self):
        self.now = 1_000_000.0
        self.beats = []
        self.events = []

    def _session(self, reply=None, raises=False, interval=0.02):
        def beat(port, connections):
            self.beats.append((port, connections))
            if raises:
                raise RuntimeError("portal unreachable")
            return reply if reply is not None else {"stop": False}

        return help_session.HelpSession(
            opener=lambda minutes: 19000, tunnel=lambda lp, rp, add: None,
            listener=lambda: 34567, clock=lambda: self.now,
            on_event=self.events.append, server=_FakeServer(),
            beat=beat, beat_interval=interval)

    def _wait_for(self, cond, timeout=2.0):
        import time as _t
        end = _t.time() + timeout
        while _t.time() < end:
            if cond():
                return True
            _t.sleep(0.01)
        return False

    def test_it_beats_with_the_port_and_the_connection_count(self):
        s = self._session()
        s.enable(minutes=30)
        self.assertTrue(self._wait_for(lambda: len(self.beats) >= 2))
        self.assertEqual(self.beats[0][0], 19000)
        s.close("user")

    def test_a_stop_reply_closes_it(self):
        s = self._session(reply={"stop": True})
        s.enable(minutes=30)
        self.assertTrue(self._wait_for(lambda: s.state == help_session.CLOSED))
        self.assertEqual(s.close_reason, "portal")

    def test_an_unreachable_portal_does_not_close_it(self):
        """一次网络抖动不能把人正在用的协助窗口掐掉。"""
        s = self._session(raises=True)
        s.enable(minutes=30)
        self.assertTrue(self._wait_for(lambda: len(self.beats) >= 2))
        self.assertEqual(s.state, help_session.OPEN)
        s.close("user")

    def test_expiry_closes_it_even_with_a_dead_portal(self):
        s = self._session(raises=True)
        s.enable(minutes=1)
        self.now += 61
        self.assertTrue(self._wait_for(lambda: s.state == help_session.CLOSED))
        self.assertEqual(s.close_reason, "expired")

    def test_closing_stops_the_beats(self):
        s = self._session()
        s.enable(minutes=30)
        self.assertTrue(self._wait_for(lambda: len(self.beats) >= 1))
        s.close("user")
        count = len(self.beats)
        import time as _t
        _t.sleep(0.1)
        self.assertEqual(len(self.beats), count, "关闭后不该再跳")

    def test_no_beat_no_thread(self):
        """没有 beat 协作者时（纯本地会话）不该起线程。"""
        s = help_session.HelpSession(opener=lambda m: 1, tunnel=lambda a, b, c: None,
                                     listener=lambda: 2, clock=lambda: self.now,
                                     server=_FakeServer())
        s.enable(minutes=5)
        self.assertIsNone(s._beat_thread, "没有 beat 却起了心跳线程")
        s.close("user")
