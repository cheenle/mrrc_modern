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
import stat
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
                "entry": f"https://{self.state['label']}.mrrc.vlsc.net:9988/" if granted else "",
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
                             "entry": f"https://{self.state['label']}.mrrc.vlsc.net:9988/",
                             "request_token": "adopted-token"})
        elif self.path == "/enroll":
            _FakePortal.seen["enroll"] = fields
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
        _FakePortal.state.update({"status": "applied"})
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
    def test_cleanup_is_a_no_op_off_windows(self):
        with mock.patch.object(cloud_hub.os, "name", "posix"):
            cloud_hub._kill_stale_frpc(Path("/tmp/nope.toml"))
