"""The password red line, one test per carrier it must never reach.

Why a scan rather than a unit assertion: the risk here is not "a function returned the
wrong value". It is "the right value appeared somewhere nobody looked" — a log line,
the tunnel config, a portal request body, the support bundle. Those are four different
files written by four different modules, so the only honest test is one that drives a
whole session and then goes looking.

The design's rule (2026-10-12 §6, guard 2): the password lives in this process's memory
while the window is open, and reaches exactly one place outside it — the customer's
eyes, via ``password_for_display()``. Everything else must come back empty.
"""
from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

import cloud_hub
import help_session


class _Sink(logging.Handler):
    """Collects every record a run produces, formatted, so a scan can see it."""

    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        # Handler.format() already runs the formatter's traceback path when the record
        # carries exc_info — no need (and no way) to call formatException here, which
        # lives on Formatter, not Handler.
        self.lines.append(self.format(record))


def _drive_a_whole_session(password: str) -> tuple[help_session.HelpSession, _Sink]:
    """One full window, exercising **every path that logs**.

    A scan over a silent run is a scan over nothing — that is what the first version of
    this file did, and its own "did anything get written?" assertion caught it. So the
    session here has a tunnel that fails on removal (a warning), a heartbeat whose portal
    is unreachable (an info) and an audit sink that throws (another warning), because
    those are the three lines this feature can actually emit.
    """

    def flaky_tunnel(local_port, remote_port, add):
        if not add:
            raise RuntimeError("reload failed while removing the proxy")

    def dead_portal(port, connections):
        raise RuntimeError("portal unreachable")

    def bad_audit(event):
        raise RuntimeError("audit sink down")

    sink = _Sink()
    logging.getLogger().addHandler(sink)
    logging.getLogger().setLevel(logging.DEBUG)
    try:
        session = help_session.HelpSession(
            opener=lambda minutes: 19000,
            tunnel=flaky_tunnel,
            listener=lambda: 34567,
            clock=lambda: 1_000_000.0,
            server=help_session.SshHelpServer(),
            beat=dead_portal,
            beat_interval=0.01,
            on_event=bad_audit,
        )
        session.enable(minutes=30, username="zhang-san", password=password)
        for _ in range(3):
            session.note_auth_failure()
        session.note_connection(duration_s=12.5)
        import time
        time.sleep(0.05)                      # 让心跳至少跳一次（打一条 info）
        session.close("user")
        return session, sink
    finally:
        logging.getLogger().removeHandler(sink)


class LogTests(unittest.TestCase):
    def test_no_log_line_carries_the_password(self):
        password = "correct-horse-battery-staple"
        _, sink = _drive_a_whole_session(password)
        self.assertTrue(sink.lines, "这一轮什么都没记下来，扫描就是空转")
        hits = [line for line in sink.lines if password in line]
        self.assertEqual(hits, [], f"口令进了日志：{hits}")

    def test_a_failing_audit_sink_still_does_not_leak(self):
        """审计回调抛异常时走的是 warning 分支——那条也不能带上口令。"""
        seen: list[str] = []

        def bad_sink(event):
            seen.append(repr(event))
            raise RuntimeError("audit sink down")

        session = help_session.HelpSession(
            opener=lambda minutes: 19000, tunnel=lambda lp, rp, add: None,
            listener=lambda: 34567, server=help_session.SshHelpServer(),
            on_event=bad_sink)
        with self.assertLogs("help_session", level="WARNING") as logged:
            session.enable(minutes=30, username="zhang-san", password="pw-1234567890")
            session.close("user")
        pw = "pw-1234567890"
        self.assertNotIn(pw, " ".join(logged.output))
        self.assertNotIn(pw, repr(seen))


class StatusTests(unittest.TestCase):
    def test_status_has_no_password_field_at_all(self):
        """不是"为空"，而是**根本没有这个键**：没有键就没有序列化路径。"""
        session = help_session.HelpSession(
            opener=lambda minutes: 19000, tunnel=lambda lp, rp, add: None,
            listener=lambda: 34567, server=help_session.SshHelpServer())
        session.enable(minutes=30, password="pw-1234567890")
        self.assertNotIn("password", session.status())
        self.assertNotIn("password", json.dumps(session.status()))
        # 报文里也不能出现那个值本身
        self.assertNotIn("pw-1234567890", json.dumps(session.status()))
        session.close("user")


class CarrierTests(unittest.TestCase):
    """The two carriers outside the process: the tunnel config and the portal request."""

    def test_the_tunnel_config_never_carries_the_help_password(self):
        password = "pw-abcdefghijkl"
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "frpc-bg7ifp.toml"
            # 用带管理段的真实生成器写一份（管理段自己那个口令是另一个秘密，
            # 与客户给的协助口令无关——这里要证的正是后者不会跑进来）。
            cloud_hub._write_frpc_config(conf, cloud_hub.frpc_config_text(
                "bg7ifp", "hub-token", 8888, 18802,
                admin_port=7400, admin_password="admin-pw",
                help_local_port=41000, help_remote_port=18877))
            text = conf.read_text(encoding="ascii")
            self.assertNotIn(password, text)
            self.assertNotIn("zhang-san", text)

    def test_the_portal_request_bodies_have_no_password_slot(self):
        """白名单：门户那边接收的字段恰好这几个，多一个都要先改这里。"""
        seen = []

        def fake_post(portal, route, payload, timeout=None):
            seen.append((route, set(payload)))
            return {"port": 18877, "stop": False, "ok": True}

        original = cloud_hub._post
        cloud_hub._post = fake_post
        try:
            cloud_hub.help_request("https://p", "BG7IFP", "tok", 60, "help-ab12")
            cloud_hub.help_heartbeat("https://p", "BG7IFP", "tok", 18877, 2)
            cloud_hub.help_close("https://p", "BG7IFP", "tok")
        finally:
            cloud_hub._post = original
        allowed = {"callsign", "token", "minutes", "username", "port", "connections"}
        for route, fields in seen:
            with self.subTest(route=route):
                self.assertTrue(fields <= allowed, f"{route} 多了字段：{fields - allowed}")
                self.assertNotIn("password", fields)

    def test_the_support_bundle_has_nothing_to_redact_because_it_is_not_written(self):
        """支持包与这条通道的交集只有审计事件；审计事件里没有口令。

        这条守的是"以后的自己"：哪天有人把 sessions 落进某个诊断快照，这里会红。
        """
        events = []
        session = help_session.HelpSession(
            opener=lambda minutes: 19000, tunnel=lambda lp, rp, add: None,
            listener=lambda: 34567, server=help_session.SshHelpServer(),
            on_event=events.append)
        session.enable(minutes=30, username="zhang-san", password="pw-1234567890")
        session.note_connection(duration_s=1.0)
        session.close("user")
        blob = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("pw-1234567890", blob)
        self.assertNotIn("password", blob)


if __name__ == "__main__":
    unittest.main()
