"""The cloud decoder must behave like a decode that happens to be elsewhere.

The wire format is under test here, not a mocked object: the fake hub is a real
HTTP server on a loopback port, because what this module can get wrong is
exactly the part a mock would paper over — the header the config travels in,
the body the slot travels in, and what "unavailable" means for each way the
far end can fail.
"""
from __future__ import annotations

import asyncio
import base64
import http.server
import json
import ssl
import threading
from typing import Any

import pytest

from ft8.core.models import DecodePath
from ft8.core.protocol import PROTOCOL_VERSION, encode_frame
from ft8.platform.cloud_decode import (
    CLOUD_THREADS,
    CloudDecoder,
    CloudUnavailable,
    cloud_decoder_from_environ,
)

SLOT = bytes(360_000)


def _ok_body(slot_id: int) -> bytes:
    return encode_frame(
        {
            "v": PROTOCOL_VERSION, "type": "decode_ok", "generation": 0,
            "request_id": 0, "slot_id": slot_id, "path": "improved",
            "results": [{
                "slot_id": slot_id, "sync": 1.0, "snr": -20, "dt": 0.1,
                "frequency": 1500.0, "text": "CQ N0CALL FN42",
                "ap_type": 0, "quality": 0.0, "flags": 0,
            }],
            "overflow": False, "elapsed_seconds": 1.2,
            "cpu_seconds": 1.2, "deadline_missed": False,
        }
    )


class _FakeHub:
    """A real HTTP server: the wire format is under test, not a mock."""

    def __init__(self, handler):
        self.seen: list[dict[str, Any]] = []
        self._handler = handler
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._make())

    def _make(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.seen.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "config": json.loads(base64.b64decode(self.headers["X-MRRC-Config"])),
                    "body": body,
                })
                status, payload = outer._handler()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # keep the test output readable
                pass

        return Handler

    def __enter__(self):
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/mrrc-decode"


def _decoder(server: _FakeHub, **kwargs) -> CloudDecoder:
    # The fake hub speaks plain HTTP on a loopback port; production requires
    # https (asserted below) and the flag has no other use.
    kwargs.setdefault("allow_plain_http", True)
    return CloudDecoder(server.url, "secret-token", cycles=3, **kwargs)


def test_a_clean_call_returns_the_batch_and_sends_the_slot() -> None:
    with _FakeHub(lambda: (200, _ok_body(7))) as hub:
        batch = asyncio.run(_decoder(hub).decode_path(7, SLOT, DecodePath.IMPROVED))

    assert batch.slot_id == 7
    assert batch.results[0].text == "CQ N0CALL FN42"
    seen = hub.seen[0]
    assert seen["path"] == "/mrrc-decode"
    assert seen["auth"] == "Bearer secret-token"
    assert seen["body"] == SLOT
    assert seen["config"]["type"] == "decode_remote"
    assert seen["config"]["pcm_bytes"] == 360_000
    assert seen["config"]["slot_id"] == 7
    # The box's 3 threads are for a 4xA53; the hub is one core.
    assert seen["config"]["config"]["threads"] == CLOUD_THREADS == 1
    assert seen["config"]["config"]["cycles"] == 3


@pytest.mark.parametrize("status", [401, 404, 500, 503])
def test_a_non_200_answer_is_unavailable_not_an_exception_leak(status: int) -> None:
    with _FakeHub(lambda: (status, b"{}")) as hub:
        with pytest.raises(CloudUnavailable, match=str(status)):
            asyncio.run(_decoder(hub).decode_path(7, SLOT, DecodePath.IMPROVED))


def test_a_dead_endpoint_is_unavailable() -> None:
    decoder = CloudDecoder(
        "http://127.0.0.1:9/mrrc-decode", "t", cycles=3, allow_plain_http=True
    )
    with pytest.raises(CloudUnavailable):
        asyncio.run(decoder.decode_path(7, SLOT, DecodePath.IMPROVED))


def test_a_body_the_strict_schema_rejects_is_unavailable() -> None:
    # The hub answered with JSON, but not a decode_ok frame.
    with _FakeHub(lambda: (200, b'{"error": "boom"}')) as hub:
        with pytest.raises(CloudUnavailable):
            asyncio.run(_decoder(hub).decode_path(7, SLOT, DecodePath.IMPROVED))


def test_a_wrong_length_slot_is_refused_before_the_network() -> None:
    with _FakeHub(lambda: (200, _ok_body(7))) as hub:
        with pytest.raises(ValueError, match="360000"):
            asyncio.run(_decoder(hub).decode_path(7, SLOT[:-2], DecodePath.IMPROVED))
    assert hub.seen == []


def test_the_url_must_be_https_in_production() -> None:
    with pytest.raises(ValueError, match="https"):
        CloudDecoder("http://hub.example/mrrc-decode", "t")


def test_an_empty_environment_configures_nothing() -> None:
    assert cloud_decoder_from_environ({}) is None
    assert cloud_decoder_from_environ({"MRRC_FT8_CLOUD_DECODE_URL": "  "}) is None


def test_a_url_without_a_token_is_refused_loudly() -> None:
    with pytest.raises(ValueError, match="token"):
        cloud_decoder_from_environ({
            "MRRC_FT8_CLOUD_DECODE_URL": "https://www.vlsc.net/mrrc-decode",
        })


def test_the_environment_builds_a_decoder_with_the_documented_defaults() -> None:
    decoder = cloud_decoder_from_environ({
        "MRRC_FT8_CLOUD_DECODE_URL": "https://www.vlsc.net/mrrc-decode",
        "MRRC_FT8_CLOUD_DECODE_TOKEN": "tok",
    })
    assert decoder is not None
    assert decoder.cycles == 3
    assert decoder.deadline_s == 2.5


class _TlsFakeHub(_FakeHub):
    """The same loopback server, behind TLS with a self-signed certificate."""

    def __init__(self, handler, cert, key):
        super().__init__(handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)

    @property
    def url(self) -> str:
        port = self._server.server_address[1]
        return f"https://localhost:{port}/mrrc-decode"


@pytest.fixture(scope="module")
def self_signed(tmp_path_factory):
    """A certificate for ``localhost``, made by the product's own signing path."""

    from ssl_bootstrap import sign_for

    directory = tmp_path_factory.mktemp("tls")
    pair = sign_for("localhost", directory)
    if pair is None:
        pytest.skip("cryptography is not installed")
    return pair


def test_a_self_signed_host_is_trusted_only_when_its_ca_is_given(self_signed) -> None:
    """MRRC_FT8_CLOUD_DECODE_CA is how a private (LAN) decoder is trusted."""

    cert, key = self_signed
    with _TlsFakeHub(lambda: (200, _ok_body(7)), cert, key) as hub:
        decoder = CloudDecoder(hub.url, "secret-token", cycles=3, ca_file=str(cert))
        batch = asyncio.run(decoder.decode_path(7, SLOT, DecodePath.IMPROVED))
        assert batch.slot_id == 7


def test_without_the_ca_the_same_host_is_unavailable(self_signed) -> None:
    cert, key = self_signed
    with _TlsFakeHub(lambda: (200, _ok_body(7)), cert, key) as hub:
        decoder = CloudDecoder(hub.url, "secret-token", cycles=3)
        with pytest.raises(CloudUnavailable):
            asyncio.run(decoder.decode_path(7, SLOT, DecodePath.IMPROVED))


def test_a_bad_ca_path_is_unavailable_not_a_startup_crash() -> None:
    """A misconfigured CA must cost the slot, not the station's FT8 engine."""

    tls = CloudDecoder("https://127.0.0.1:9/mrrc-decode", "t", ca_file="/nonexistent/ca.pem")
    with pytest.raises(CloudUnavailable):
        asyncio.run(tls.decode_path(7, SLOT, DecodePath.IMPROVED))


def test_the_environment_passes_the_ca_file_through(tmp_path) -> None:
    ca = tmp_path / "ham-ca.pem"
    ca.write_text("x")
    decoder = cloud_decoder_from_environ({
        "MRRC_FT8_CLOUD_DECODE_URL": "https://ham.vlsc.net:8990/mrrc-decode",
        "MRRC_FT8_CLOUD_DECODE_TOKEN": "tok",
        "MRRC_FT8_CLOUD_DECODE_CA": str(ca),
    })
    assert decoder is not None
    assert decoder._ca_file == str(ca)


def test_trace_reports_the_timing_and_can_never_break_the_decode(caplog) -> None:
    """The diagnostic switch, and the bug that motivated the guard around it.

    Tracing was added to answer "where do the seconds go inside the live
    process"; its first version called an undefined logger and raised, which the
    cascade faithfully turned into "use the local path" — silently, for hours.
    So: the line must appear, and a broken trace must not be able to fail a
    decode that already succeeded.
    """

    import logging as _logging

    from ft8.platform.cloud_decode import CloudDecoder

    decoder = CloudDecoder("https://example.invalid/decode", "token", trace=True)
    with caplog.at_level(_logging.INFO, logger="mrrc.ft8.cloud_decode"):
        decoder._trace(42, {"elapsed_seconds": 1.5}, 100.0, {
            "connect": 100.1, "sent": 100.2, "first_byte": 101.9, "received": 102.0,
        })
    records = [r for r in caplog.records if "cloud trace" in r.getMessage()]
    assert records, "the trace line did not reach the logger"
    assert "host decode 1500 ms" in records[0].getMessage()

    # And a trace that raises is retired rather than propagated.
    decoder._trace_log = lambda *_: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
    decoder._trace(1, {}, 0.0, {})  # must not raise
    assert decoder._tracing is False
