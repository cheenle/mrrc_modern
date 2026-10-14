"""Cloud Hub onboarding from inside the app.

These tests exist because the tenant-side flow used to be a shell script that had to be debugged on
a real Windows machine, three times, in one night. What is asserted here is what those debugging
sessions taught:

* the certificate must carry the entry name the hub verifies (it refuses any other),
* the tunnel config must be ASCII with no byte order mark, because frpc's TOML parser rejects a BOM
  outright and reports it at column 1 of line 1,
* the certificate paths must land in the launcher's own config file, not only in user-scope
  variables, because a process started by Explorer keeps the environment block Explorer had,
* and nothing may be written at all before the operator has approved the application.
"""

import json
import os
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import cloud_hub


class _FakePortal(BaseHTTPRequestHandler):
    """Just enough portal: /apply, /status, /enroll, with the facts the tests assert on."""

    state = {
        "status": "applied",
        "label": "bg9zzz",
        "port": 18877,
        "enroll_secret": "s" * 32,
        "hub_token": "tok-frps",
        "token": "t0k",
        "enroll_ok": True,
    }
    seen: dict = {}

    def log_message(self, format: str, *args) -> None:    # noqa: A002 - matches the base class
        """Silence the per-request logging; the assertions read the recorded fields instead."""

    def _fields(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode()
        import urllib.parse
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def do_POST(self):                             # noqa: N802
        fields = self._fields()
        if self.path == "/apply":
            _FakePortal.seen["apply"] = fields
            self._json(200, {"callsign": fields.get("callsign", ""), "status": "applied",
                             "request_token": self.state["token"]})
        elif self.path == "/status":
            if fields.get("token") != self.state["token"]:
                self._json(403, {"error": "申请令牌无效"})
                return
            granted = self.state["status"] == "granted"
            self._json(200, {
                "callsign": fields.get("callsign", ""),
                "status": self.state["status"],
                "label": self.state["label"] if granted else "",
                "port": self.state["port"] if granted else 0,
                "enroll_secret": self.state["enroll_secret"] if granted else "",
                "hub_token": self.state["hub_token"] if granted else "",
                "entry": f"https://{self.state['label']}.mrrc.vlsc.net/" if granted else "",
            })
        elif self.path == "/claim":
            _FakePortal.seen["claim"] = fields
            if fields.get("secret") != self.state["enroll_secret"]:
                self._json(403, {"error": "登记口令无效或未获授权"})
                return
            self._json(200, {"callsign": fields.get("callsign", ""), "status": "granted",
                             "label": self.state["label"], "port": self.state["port"],
                             "enroll_secret": self.state["enroll_secret"],
                             "hub_token": self.state["hub_token"],
                             "entry": f"https://{self.state['label']}.mrrc.vlsc.net/",
                             "request_token": "adopted-token"})
        elif self.path == "/enroll":
            _FakePortal.seen["enroll"] = fields
            if not self.state.get("enroll_ok", True):
                self._json(503, {"error": "portal 暂时不可用"})
                return
            if fields.get("secret") != self.state["enroll_secret"]:
                self._json(403, {"error": "登记口令无效或未获授权"})
                return
            from cryptography import x509
            cert = x509.load_pem_x509_certificate(fields.get("cert", "").encode())
            cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
            expected = f"{self.state['label']}.mrrc.vlsc.net"
            _FakePortal.seen["cert_cn"] = cn
            if cn != expected:
                self._json(400, {"error": f"证书名字不符：期望 {expected}，实得 {cn}"})
                return
            self._json(200, {"callsign": fields.get("callsign", ""), "names": [cn]})
        else:
            self._json(404, {"error": "no such route"})

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)



class _RecordingTunnel(cloud_hub.TunnelProcess):
    """Stands in for TunnelProcess so the assertions work on every platform.

    The real thing runs frpc; a test that waits for a real process can only do so where a shell
    script is executable, which is not Windows - and it was Windows that turned this test red the
    first time. It subclasses the real class rather than duplicating its shape, so the type check
    keeps proving that connect() is handed something it can actually use.
    """

    def __init__(self):
        super().__init__(Path("unused-frpc"), Path("unused.toml"))
        self.started = False
        self._running = False

    def start(self):
        self.started = True
        self._running = True

    def stop(self):
        self._running = False

    @property
    def running(self) -> bool:
        return self._running


class CloudHubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakePortal)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.portal = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        _FakePortal.state.update({"status": "applied", "enroll_ok": True})
        _FakePortal.seen.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = self.root / "mrrc_modern.env"
        self.cert_dir = self.root / "certs"
        self.data_dir = self.root / "fleet"
        self.fleet = self.root / "fleetbin"
        self.fleet.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_frpc(self) -> Path:
        """An executable that records that it ran, then stays alive like the real one."""
        marker = self.root / "frpc-started"
        script = self.fleet / ("frpc.exe" if os.name == "nt" else "frpc")
        script.write_text(f"#!/bin/sh\ntouch {marker}\nwhile true; do sleep 1; done\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return script

    def test_apply_returns_the_token_the_app_must_keep(self):
        reply = cloud_hub.apply(self.portal, "BG9ZZZ", "op@example.com")
        self.assertEqual(reply["request_token"], "t0k")
        self.assertEqual(_FakePortal.seen["apply"]["callsign"], "BG9ZZZ")

    def test_claim_adopts_an_approved_application(self):
        """凭运维给的口令直接认领已批准的申请，拿到接入信息与一个可用的申请令牌。"""
        _FakePortal.state["status"] = "granted"
        reply = cloud_hub.claim(self.portal, "BG9AAA", "s" * 32)
        self.assertEqual(reply["label"], "bg9zzz")
        self.assertEqual(reply["port"], 18877)
        self.assertTrue(reply["request_token"])
        self.assertEqual(_FakePortal.seen["claim"]["callsign"], "BG9AAA")
        with self.assertRaises(cloud_hub.CloudHubError):
            cloud_hub.claim(self.portal, "BG9AAA", "wrong")

    def test_status_refuses_a_wrong_token(self):
        with self.assertRaises(cloud_hub.CloudHubError):
            cloud_hub.status(self.portal, "BG9ZZZ", "not-the-token")

    def test_nothing_is_written_before_approval(self):
        result = cloud_hub.connect(self.portal, "BG9ZZZ", "t0k", config_path=self.cfg,
                                   cert_dir=self.cert_dir, fleet_dir=self.fleet,
                                   data_dir=self.data_dir)
        self.assertFalse(result["connected"])
        self.assertFalse(self.cfg.exists())
        self.assertFalse(self.cert_dir.exists())

    def test_connect_signs_enrolls_configures_and_tunnels(self):
        _FakePortal.state["status"] = "granted"
        # connect() only starts a tunnel when the frpc binary is there - a sensible guard. The stub
        # never executes it, so a placeholder is enough.
        (self.fleet / ("frpc.exe" if os.name == "nt" else "frpc")).write_text("", encoding="utf-8")
        tunnel = _RecordingTunnel()
        result = cloud_hub.connect(self.portal, "BG9ZZZ", "t0k", config_path=self.cfg,
                                  cert_dir=self.cert_dir, fleet_dir=self.fleet,
                                  data_dir=self.data_dir, tunnel=tunnel)

        self.assertTrue(result["connected"], result)
        self.assertEqual(result["entry"], "https://bg9zzz.mrrc.vlsc.net/")
        self.assertTrue(tunnel.started, "the tunnel was not started")
        self.assertEqual(str(tunnel.conf), result["tunnel_config"])

        # the certificate carries the name the hub verifies
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(Path(result["cert"]).read_bytes())
        cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
        self.assertEqual(cn, "bg9zzz.mrrc.vlsc.net")
        self.assertEqual(_FakePortal.seen["cert_cn"], "bg9zzz.mrrc.vlsc.net")

        # the config file the launcher reads got the certificate, and kept what was there
        self.cfg.write_text("MRRC_RADIO_MODEL=ft710\nMRRC_SSL_CERT=/old/path.pem\n", encoding="utf-8")
        cloud_hub.connect(self.portal, "BG9ZZZ", "t0k", config_path=self.cfg, cert_dir=self.cert_dir,
                          fleet_dir=self.fleet, data_dir=self.data_dir, tunnel=tunnel)
        text = self.cfg.read_text(encoding="utf-8")
        self.assertIn("MRRC_RADIO_MODEL=ft710", text)
        self.assertIn(f"MRRC_SSL_CERT={result['cert']}", text)
        self.assertNotIn("/old/path.pem", text)
        self.assertEqual(text.count("MRRC_SSL_CERT="), 1)

        # the TOML frpc reads: ASCII, no BOM, one tcp proxy
        raw = Path(result["tunnel_config"]).read_bytes()
        self.assertNotEqual(raw[:3], b"\xef\xbb\xbf", "frpc rejects a BOM at line 1 column 1")
        raw.decode("ascii")
        conf_text = raw.decode()
        # A backslash in a TOML basic string starts an escape: a raw Windows path makes frpc refuse
        # the file ("non-hex character" at the \U of C:\Users) and the tunnel never starts.
        self.assertNotIn("\\", conf_text, "unescaped backslash in the TOML frpc has to parse")
        self.assertIn('auth.token = "tok-frps"', conf_text)
        self.assertIn("remotePort = 18877", conf_text)
        self.assertIn("localPort = 8888", conf_text)

    def test_a_failed_second_enroll_never_orphans_the_pinned_certificate(self):
        """Regression (2026-10-15, live instance `bg7kxr`): the hub pins what ``/enroll`` got.

        connect() used to sign a fresh certificate every time. When a second one was signed and
        its enroll never reached the portal, the file the process serves was replaced by a
        document the hub had never seen: nginx kept verifying against the pinned one and the
        entry answered 502 for good — restarting only made the process serve the newer file
        again. Measured on that machine: two signatures three seconds apart, exactly one enroll
        POST, permanent 502. Reusing the pair makes the failure harmless, and the retry that does
        get through re-enrolls exactly the certificate the hub already holds.
        """
        _FakePortal.state["status"] = "granted"
        first = cloud_hub.connect(self.portal, "BG9ZZZ", "t0k", config_path=self.cfg,
                                  cert_dir=self.cert_dir, fleet_dir=self.fleet,
                                  data_dir=self.data_dir)
        pinned = _FakePortal.seen["enroll"]["cert"]
        cert_path = Path(first["cert"])

        _FakePortal.state["enroll_ok"] = False       # the portal cannot be reached right now
        with self.assertRaises(cloud_hub.CloudHubError):
            cloud_hub.connect(self.portal, "BG9ZZZ", "t0k", config_path=self.cfg,
                              cert_dir=self.cert_dir, fleet_dir=self.fleet,
                              data_dir=self.data_dir)
        self.assertEqual(cert_path.read_text(encoding="utf-8"), pinned,
                         "the certificate the hub pinned was replaced by one it never saw")

        _FakePortal.state["enroll_ok"] = True        # the retry, hours later
        cloud_hub.connect(self.portal, "BG9ZZZ", "t0k", config_path=self.cfg,
                          cert_dir=self.cert_dir, fleet_dir=self.fleet,
                          data_dir=self.data_dir)
        self.assertEqual(_FakePortal.seen["enroll"]["cert"], pinned,
                         "the retry enrolled a certificate the hub can verify but nobody serves")

    @unittest.skipIf(os.name == "nt", "needs an executable shell script, which Windows has not")
    def test_the_supervisor_really_starts_a_process(self):
        """The recording stub above proves the call; this proves the process starts, on POSIX."""
        _FakePortal.state["status"] = "granted"
        marker = self.root / "frpc-started"
        script = self.fleet / "frpc"
        script.write_text(f"#!/bin/sh\ntouch {marker}\nwhile true; do sleep 1; done\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        tunnel = cloud_hub.TunnelProcess(script, self.root / "unused.toml")
        try:
            cloud_hub.connect(self.portal, "BG9ZZZ", "t0k", config_path=self.cfg,
                              cert_dir=self.cert_dir, fleet_dir=self.fleet,
                              data_dir=self.data_dir, tunnel=tunnel)
            for _ in range(60):
                if marker.exists():
                    break
                time.sleep(0.1)
            self.assertTrue(marker.exists(), "frpc was not started")
            self.assertTrue(tunnel.running)
        finally:
            tunnel.stop()


class PortalPathTests(unittest.TestCase):
    """One portal, one address. The two-path era ended when everything moved onto one machine."""

    def test_default_is_the_merged_address(self):
        self.assertEqual(cloud_hub.PORTAL_DEFAULT, "https://portal.mrrc.vlsc.net")

    def test_a_config_from_before_the_merge_is_sent_to_the_new_address(self):
        for legacy in ("https://portal.mrrc.vlsc.net:8899", "https://www.vlsc.net/mrrc_portal",
                       "https://portal.mrrc.vlsc.net/mrrc_portal"):
            seen = []
            real = cloud_hub._post_once

            def fake(base, route, payload, timeout):
                seen.append(base)
                return {"status": "granted"}

            cloud_hub._post_once = fake
            try:
                out = cloud_hub._post(legacy, "/status", {})
            finally:
                cloud_hub._post_once = real
            self.assertEqual(out["status"], "granted")
            self.assertEqual(seen, [cloud_hub.PORTAL_DEFAULT], legacy)

    def test_a_custom_portal_is_used_as_given(self):
        seen = []
        real = cloud_hub._post_once

        def fake(base, route, payload, timeout):
            seen.append(base)
            return {"ok": True}

        cloud_hub._post_once = fake
        try:
            cloud_hub._post("https://example.invalid/portal/", "/status", {})
        finally:
            cloud_hub._post_once = real
        self.assertEqual(seen, ["https://example.invalid/portal/"])

    def test_an_unreachable_portal_raises_a_cloud_hub_error(self):
        real = cloud_hub._post_once

        def fake(base, route, payload, timeout):
            raise cloud_hub._Unreachable("connection refused")

        cloud_hub._post_once = fake
        try:
            with self.assertRaises(cloud_hub.CloudHubError):
                cloud_hub._post(cloud_hub.PORTAL_DEFAULT, "/status", {})
        finally:
            cloud_hub._post_once = real

    def test_the_entry_carries_no_port(self):
        """The entries are on 443 with everything else; :9988 is gone."""
        pkg = Path(cloud_hub.__file__).read_text(encoding="utf-8")
        self.assertNotIn(":9988", pkg)


class StaleTunnelTests(unittest.TestCase):
    """One instance must never accumulate frpc processes.

    Field evidence, 2026-10-04: a Windows box that had started the app six times in two days was
    left holding six frpc.exe processes, all naming one config. Five of them could never register
    their proxy name, so they retried every 30 seconds - 7143 "proxy already exists" warnings on
    the hub in a single day - and nothing on the machine said so: the cleanup below used ``wmic``,
    which Windows 11 no longer ships, and its ``except`` swallowed the failure silently.

    Field evidence on macOS, 2026-10-06: the same six orphans, from six app starts in half an hour,
    all naming one config and all retrying against one proxy name. The Windows fix never reached
    here - the sweep began with ``if os.name != "nt": return``, so there was no listing, no
    warning, and no cleanup at all.
    """

    CONFIG = r"C:\Users\cheen\AppData\Local\MRRC-Modern\fleet\frpc-ba4eg.toml"

    #: The shape ``_enumerate_frpc`` returns: pid, a tab, the command line. Two instances on one
    #: machine, which is a supported shape - the other one must survive.
    OUTPUT = ("4242\t\"C:\\Program Files\\MRRC Modern\\fleet\\frpc.exe\" -c " + CONFIG + "\n"
              "1717\t\"C:\\Program Files\\MRRC Modern\\fleet\\frpc.exe\" -c "
              "C:\\Users\\cheen\\AppData\\Local\\MRRC-Modern\\fleet\\frpc-bg9zzz.toml\n")

    #: A macOS command line: "/" separators, and the path spelled the way the filesystem has it.
    POSIX_CONFIG = "/Users/cheen/Library/Application Support/MRRC-Modern/fleet/frpc-ba4eg.toml"
    POSIX_OUTPUT = (
        "4242\t/Applications/MRRC-Modern.app/Contents/Frameworks/payload/frpc -c "
        + POSIX_CONFIG + "\n"
        "1717\t/Applications/MRRC-Modern.app/Contents/Frameworks/payload/frpc -c "
        "/Users/cheen/Library/Application Support/MRRC-Modern/fleet/frpc-bg9zzz.toml\n")

    #: What ``ps -eo pid=,command=`` prints off Windows: a pid, then the command line, no header.
    #: The third line is the hazard - a text editor with this instance's config open names the
    #: config too, and the sweep must not take it for an frpc.
    PS_OUTPUT = (
        " 4242 /Applications/MRRC-Modern.app/Contents/Frameworks/payload/frpc -c "
        + POSIX_CONFIG + "\n"
        " 1717 vim " + POSIX_CONFIG + "\n"
        " 9999 /opt/homebrew/bin/frpc -c /tmp/other.toml\n"
        " 1234 /Applications/MRRC-Modern.app/Contents/MacOS/MRRC-Modern-Server\n")

    def test_a_stale_pid_that_has_already_exited_is_not_an_error(self):
        """The listing is a snapshot: the process can exit between the ``ps`` and the kill, and
        ``os.kill`` raises when it has. This runs from ``TunnelProcess.__init__``, so letting that
        out would take the tunnel setup - and the endpoint that triggers it - down with it, which
        is a worse outcome than a sweep with nothing left to do.
        """
        with mock.patch.object(cloud_hub.os, "name", "posix"), \
                mock.patch.object(cloud_hub, "_enumerate_frpc", lambda: self.POSIX_OUTPUT), \
                mock.patch.object(cloud_hub.os, "kill",
                                  side_effect=ProcessLookupError(3, "No such process")):
            cloud_hub._kill_stale_frpc(Path(self.POSIX_CONFIG))

    def test_a_kill_that_times_out_is_not_an_error_either(self):
        """``taskkill`` gets 10 seconds; a loaded Windows box can take longer to answer, and
        TimeoutExpired is a SubprocessError, not an OSError - so it would sail past a guard that
        only caught OSError and out of ``TunnelProcess.__init__`` with the tunnel.
        """
        with mock.patch.object(cloud_hub.os, "name", "nt"), \
                mock.patch.object(cloud_hub, "_enumerate_frpc", lambda: self.OUTPUT), \
                mock.patch.object(cloud_hub.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired("taskkill", 10)), \
                self.assertLogs("cloud_hub", level="WARNING") as logged:
            cloud_hub._kill_stale_frpc(Path(self.CONFIG))
        self.assertIn("cannot terminate a stale frpc", " ".join(logged.output))

    def test_process_listing_uses_ps_off_windows(self):
        """PowerShell is the Windows half of this function and macOS does not have it, so the
        listing - and with it the whole sweep - raised there and was swallowed by the warning
        path, which is how the orphans went unnoticed on this machine."""
        seen = []

        def fake_run(cmd, **kwargs):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=self.PS_OUTPUT, stderr="")

        with mock.patch.object(cloud_hub.os, "name", "posix"), \
                mock.patch.object(cloud_hub.subprocess, "run", fake_run):
            listed = cloud_hub._enumerate_frpc()
        self.assertEqual(seen, [["ps", "-eo", "pid=,command="]])
        self.assertEqual(
            listed,
            "4242\t/Applications/MRRC-Modern.app/Contents/Frameworks/payload/frpc -c "
            + self.POSIX_CONFIG + "\n"
            "9999\t/opt/homebrew/bin/frpc -c /tmp/other.toml\n")

    def test_the_stale_frpc_is_terminated_off_windows_too(self):
        """The sweep was ``if os.name != "nt": return`` - dead code on macOS, where the tunnel moved
        into the app itself. Measured here 2026-10-06: six frpc orphans from six app starts, all
        naming one config, all retrying against one proxy name, and the hub's log full of
        "proxy already exists" while nothing on this machine said so.

        Only this instance's own config is touched: the other pid in the listing names a different
        one and must survive, or a second instance would lose its tunnel to this one's sweep.
        """
        killed = []
        with mock.patch.object(cloud_hub.os, "name", "posix"), \
                mock.patch.object(cloud_hub, "_enumerate_frpc", lambda: self.POSIX_OUTPUT), \
                mock.patch.object(cloud_hub.os, "kill",
                                  lambda pid, sig: killed.append((pid, sig))):
            cloud_hub._kill_stale_frpc(Path(self.POSIX_CONFIG))
        self.assertEqual(killed, [(4242, signal.SIGTERM)])

    def test_the_path_match_ignores_case_and_slashes(self):
        """What this replaced was ``str(config_path) in line``: case-sensitive, so a Windows path
        that differed only in case matched nothing and the stale process was never killed."""
        for written in (self.CONFIG,
                        r"c:\users\CHEEN\appdata\local\mrrc-modern\fleet\frpc-BA4EG.toml",
                        self.CONFIG.replace("\\", "/")):
            self.assertEqual(cloud_hub._stale_frpc_pids(self.OUTPUT, Path(written)), [4242], written)

    def test_another_instances_frpc_is_left_alone(self):
        self.assertEqual(cloud_hub._stale_frpc_pids(self.OUTPUT, Path(r"C:\x\frpc-other.toml")), [])
        self.assertEqual(cloud_hub._stale_frpc_pids("", Path(self.CONFIG)), [])
        self.assertEqual(cloud_hub._stale_frpc_pids("no tab and no pid\n", Path(self.CONFIG)), [])

    def test_the_stale_process_is_the_only_thing_killed(self):
        calls = []
        with mock.patch.object(cloud_hub.os, "name", "nt"), \
                mock.patch.object(cloud_hub, "_enumerate_frpc", lambda: self.OUTPUT), \
                mock.patch.object(cloud_hub.subprocess, "run", lambda cmd, **kw: calls.append(cmd)):
            cloud_hub._kill_stale_frpc(Path(self.CONFIG))
        self.assertEqual(calls, [["taskkill", "/PID", "4242", "/F"]])

    def test_a_lookup_that_fails_says_so(self):
        """Swallowing this is what cost two days: the symptom appears on the hub, never here."""
        def boom():
            raise OSError("powershell: not found")

        with mock.patch.object(cloud_hub.os, "name", "nt"), \
                mock.patch.object(cloud_hub, "_enumerate_frpc", boom), \
                self.assertLogs("cloud_hub", level="WARNING") as logged:
            cloud_hub._kill_stale_frpc(Path(self.CONFIG))
        self.assertIn("cannot list frpc processes", " ".join(logged.output))


class HelpTunnelConfigTests(unittest.TestCase):
    """The tunnel side of `call help`: an admin API to reload through, and a second
    proxy for the SSH port — both added to a config that must keep working."""

    def test_the_admin_block_is_loopback_and_authenticated(self):
        text = cloud_hub.frpc_config_text("bg7ifp", "hk", 8888, 18802,
                                          admin_port=7400, admin_password="s3cret")
        self.assertIn('webServer.addr = "127.0.0.1"', text)
        self.assertIn("webServer.port = 7400", text)
        self.assertIn('webServer.user = "mrrc"', text)
        self.assertIn('webServer.password = "s3cret"', text)
        self.assertNotIn("0.0.0.0", text)

    def test_no_admin_block_without_a_port(self):
        """既有调用点（不传 admin_port）产生的配置必须与从前逐字一样。"""
        text = cloud_hub.frpc_config_text("bg7ifp", "hk", 8888, 18802)
        self.assertNotIn("webServer", text)
        self.assertEqual(text.count("[[proxies]]"), 1)

    def test_no_help_proxy_by_default(self):
        text = cloud_hub.frpc_config_text("bg7ifp", "hk", 8888, 18802,
                                          admin_port=7400, admin_password="x")
        self.assertNotIn("mrrc-help-", text)

    def test_the_help_proxy_is_named_per_instance(self):
        """两个实例共用一个 frps，代理名撞了就是互相抢（2026-10-11 那次事故）。"""
        text = cloud_hub.frpc_config_text("bg7ifp", "hk", 8888, 18802,
                                          admin_port=7400, admin_password="x",
                                          help_local_port=41000, help_remote_port=18877)
        self.assertIn('name = "mrrc-help-bg7ifp"', text)
        self.assertIn("localPort = 41000", text)
        self.assertIn("remotePort = 18877", text)
        self.assertEqual(text.count("[[proxies]]"), 2)
        self.assertIn('name = "bg7ifp"', text, "主代理还在")

    @unittest.skipUnless(os.name == "posix",
                         "0600 是 POSIX 文件模式；Windows 的等价物是 ACL")
    def test_the_config_file_is_0600(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "frpc-bg7ifp.toml"
            cloud_hub._write_frpc_config(conf, "auth.token = \"secret\"\n")
            self.assertEqual(stat.S_IMODE(conf.stat().st_mode), 0o600)

    def test_the_admin_settings_read_back(self):
        """reload 要用它们，而它们的唯一存放处就是这个文件。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "c.toml"
            cloud_hub._write_frpc_config(
                conf, cloud_hub.frpc_config_text("bg7ifp", "hk", 8888, 18802,
                                                 admin_port=7400, admin_password="p@ss"))
            self.assertEqual(cloud_hub.read_admin_settings(conf), (7400, "mrrc", "p@ss"))

    def test_a_config_without_an_admin_block_reads_as_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "c.toml"
            conf.write_text(cloud_hub.frpc_config_text("bg7ifp", "hk", 8888, 18802),
                            encoding="ascii")
            self.assertEqual(cloud_hub.read_admin_settings(conf), (0, "", ""))

    def test_connect_reuses_the_admin_block_instead_of_minting_a_new_one(self):
        """管理段只铸一次。

        frpc 只在启动时读一次配置：每次 connect() 换一个端口，就等于让配置与**正在跑的**
        frpc 分叉，reload 打到一个没人听的端口上（2026-10-12 首次真跑实测：配置写 53235，
        活着的 frpc 在 53189）。
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "frpc-bg7ifp.toml"
            cloud_hub._write_frpc_config(conf, cloud_hub.frpc_config_text(
                "bg7ifp", "tok", 8888, 18802, admin_port=7400, admin_password="first"))
            settings = cloud_hub.read_tunnel_settings(conf)
            self.assertEqual(settings["admin_port"], 7400)
            self.assertEqual(settings["admin_password"], "first")

    def test_a_missing_config_reads_as_empty(self):
        self.assertEqual(cloud_hub.read_admin_settings(Path("/nonexistent/x.toml")),
                         (0, "", ""))


class ReloadTests(unittest.TestCase):
    """`frpc reload` over the admin API — no CLI, no shell, no binary on PATH."""

    def _fake(self, status=200, body=b"{}"):
        """A stand-in for **frpc's** admin API, not for an idea of it.

        The first version implemented only do_POST — and the real frpc answers
        `POST /api/reload` with 405, because it wants GET (measured against the
        binary, 2026-10-12). A mock that agrees with whatever it is asked agrees
        with a bug too, so this one implements GET and answers POST the way frpc
        does.
        """
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        seen = {}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["path"] = self.path
                seen["auth"] = self.headers.get("Authorization", "")
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):                       # frpc: 405, see the docstring
                self.send_response(405)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, seen

    def test_the_method_is_get_because_frpc_says_so(self):
        """405 立即失败，不被 wait_s 拖成"慢失败"——错的方法不该被重试。"""
        import time as _t
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(405)
                self.end_headers()

            def do_POST(self):
                self.send_response(405)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            start = _t.monotonic()
            with self.assertRaises(cloud_hub.CloudHubError) as ctx:
                cloud_hub.reload_frpc(srv.server_port, "mrrc", "pw", timeout=3, wait_s=10)
            elapsed = _t.monotonic() - start
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertIn("405", str(ctx.exception))
        self.assertLess(elapsed, 2.0, "405 不该被 wait_s 拖成慢失败")

    def test_reload_posts_to_the_admin_api_with_basic_auth(self):
        import base64
        srv, seen = self._fake()
        try:
            cloud_hub.reload_frpc(srv.server_port, "mrrc", "s3cret", timeout=3)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(seen["path"], "/api/reload")
        want = base64.b64encode(b"mrrc:s3cret").decode()
        self.assertEqual(seen["auth"], f"Basic {want}")

    def test_a_non_200_is_an_error(self):
        srv, _ = self._fake(status=500, body=b'{"error":"bad config"}')
        try:
            with self.assertRaises(cloud_hub.CloudHubError) as ctx:
                cloud_hub.reload_frpc(srv.server_port, "mrrc", "s3cret", timeout=3)
        finally:
            srv.shutdown()
            srv.server_close()
            srv.server_close()
        self.assertIn("500", str(ctx.exception))
        self.assertIn("bad config", str(ctx.exception))

    def test_an_unreachable_admin_port_is_an_error(self):
        """不能静默退回重启：那会把一次可诊断的失败变成一次看不见的隧道抖动。"""
        with self.assertRaises(cloud_hub.CloudHubError) as ctx:
            cloud_hub.reload_frpc(1, "mrrc", "s3cret", timeout=1)   # 1 号端口没人听
        self.assertIn("unreachable", str(ctx.exception))


class SetHelpProxyTests(unittest.TestCase):
    """加/删协助代理：主代理必须原样活下来，reload 必须被调用。"""

    def _conf(self, tmp):
        conf = Path(tmp) / "frpc-bg7ifp.toml"
        cloud_hub._write_frpc_config(conf, cloud_hub.frpc_config_text(
            "bg7ifp", "TOKEN", 8888, 18802, log_file="/tmp/f.log",
            admin_port=7400, admin_password="pw"))
        return conf

    def test_adding_keeps_the_web_proxy_and_reloads(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            conf = self._conf(tmp)
            with mock.patch.object(cloud_hub, "reload_frpc") as reload_mock:
                cloud_hub.set_help_proxy(conf, help_local_port=41000, help_remote_port=18877)
            reload_mock.assert_called_once()
            self.assertEqual(reload_mock.call_args.args[0], 7400)
            text = conf.read_text(encoding="ascii")
            self.assertIn('name = "bg7ifp"', text)
            self.assertIn('name = "mrrc-help-bg7ifp"', text)
            self.assertIn("localPort = 8888", text)
            self.assertIn("remotePort = 18802", text)
            self.assertIn('auth.token = "TOKEN"', text, "token 从文件读回并写回")

    def test_removing_leaves_the_web_proxy_alone(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            conf = self._conf(tmp)
            with mock.patch.object(cloud_hub, "reload_frpc"):
                cloud_hub.set_help_proxy(conf, help_local_port=41000, help_remote_port=18877)
                cloud_hub.set_help_proxy(conf, remove=True)
            text = conf.read_text(encoding="ascii")
            self.assertNotIn("mrrc-help-", text)
            self.assertIn("localPort = 8888", text)
            self.assertEqual(text.count("[[proxies]]"), 1)

    def test_it_refuses_a_config_with_no_web_proxy(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "empty.toml"
            cloud_hub._write_frpc_config(conf, 'serverAddr = "x"\n')
            with self.assertRaises(cloud_hub.CloudHubError):
                cloud_hub.set_help_proxy(conf, help_local_port=1, help_remote_port=2)


class TunnelStartSweepTests(unittest.TestCase):
    """构造时用的配置不一定是启动时用的那一份。

    懒创建走的是占位符（`pending.toml`），而 `connect()` 会在调用 `start()` 之前把
    真实路径塞进来。清扫若只按占位符做，前一次运行留下的 frpc 就会活着、占着代理名
    —— 真实后果是新一轮 frpc 报 `proxy [bg1ifp] already exists`，而 hub 只看见旧的那个
    （2026-10-12 实测：两个 frpc、两个管理端、隧道是旧的在工作）。
    """

    def test_start_sweeps_the_config_it_actually_runs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "frpc-bg1ifp.toml"
            real.write_text("serverAddr = \"x\"\n", encoding="ascii")
            tunnel = cloud_hub.TunnelProcess(Path("/bin/true"), Path(tmp) / "pending.toml")
            swept = []
            with mock.patch.object(cloud_hub, "_kill_stale_frpc", lambda p: swept.append(Path(p))):
                tunnel.conf = real          # connect() 就是这么做的
                tunnel.start()
                tunnel.stop()
            assert swept == [real], swept
