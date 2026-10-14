"""The hub service is a transport, and these are the things a transport can get wrong.

The worker is faked (a real one would need the native library and a second of
CPU per case), but the frame handling is not: the service must be exactly as
strict as the worker it fronts, must not read a body it has not authenticated,
and must answer "busy" rather than queue — because the box on the other end has
a local fallback and a queue would only turn lateness into the normal case.

``tools/`` is not a package on purpose, so the module is loaded by path, the
same way ``tests/ft8/test_i9_field_twin.py`` loads the field twin.
"""
from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path

import pytest

from ft8.core.models import DecodeConfig
from ft8.core.protocol import PROTOCOL_VERSION, decode_frame
from ft8.engine.dsp_decode import decode_config_frame

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "hub_decode" / "service.py"


def _load_service():
    assert TOOL.is_file(), f"the hub service moved: {TOOL}"
    spec = importlib.util.spec_from_file_location("mrrc_hub_decode", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFIG = decode_config_frame(DecodeConfig(threads=3))


def _frame(**changes: object) -> dict[str, object]:
    frame = {
        "v": PROTOCOL_VERSION, "type": "decode_remote", "generation": 0,
        "request_id": 0, "slot_id": 7, "deadline_monotonic": 1234.5,
        "config": CONFIG, "pcm_bytes": 360_000,
    }
    frame.update(changes)
    return frame


class FakeWorker:
    """Stands in for the WorkerSupervisor: records the frame, returns decode_ok."""

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.seen: list[dict[str, object]] = []
        self.gate = gate

    def request(self, frame, timeout):
        self.seen.append(frame)
        if self.gate is not None:
            assert self.gate.wait(5.0)
        return {
            "v": PROTOCOL_VERSION, "type": "decode_ok", "generation": 0,
            "request_id": 0, "slot_id": frame["slot_id"],
            "path": frame["config"]["path"], "results": [], "overflow": False,
            "elapsed_seconds": 0.5, "cpu_seconds": 0.5, "deadline_missed": False,
        }


def _service(worker, **kwargs):
    return _load_service().DecodeService(
        worker, token="secret", monotonic=lambda: 500.0, **kwargs
    )


def test_a_valid_request_reaches_the_worker_and_returns_decode_ok() -> None:
    worker = FakeWorker()
    status, body = _service(worker).handle(
        _frame(), bytes(360_000), authorization="Bearer secret"
    )

    assert status == 200
    assert decode_frame(body)["type"] == "decode_ok"
    sent = worker.seen[0]
    assert sent["type"] == "decode"
    # The box's deadline means nothing here: the hub's own clock must bound it.
    assert sent["deadline_monotonic"] == 510.0
    assert sent["config"]["threads"] == 1
    assert sent["shm"]["nbytes"] == 360_000


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "secret", ""])
def test_a_missing_or_wrong_token_is_401_and_reads_nothing(authorization) -> None:
    worker = FakeWorker()
    status, _ = _service(worker).handle(
        _frame(), bytes(360_000), authorization=authorization
    )
    assert status == 401
    assert worker.seen == []


@pytest.mark.parametrize("length", [0, 359_998, 360_002, 720_000])
def test_a_body_that_is_not_one_slot_is_400(length: int) -> None:
    worker = FakeWorker()
    status, _ = _service(worker).handle(
        _frame(), bytes(length), authorization="Bearer secret"
    )
    assert status == 400
    assert worker.seen == []


def test_a_local_decode_frame_is_rejected() -> None:
    worker = FakeWorker()
    frame = _frame()
    frame["type"] = "decode"  # the local frame carries a shared-memory descriptor
    status, _ = _service(worker).handle(
        frame, bytes(360_000), authorization="Bearer secret"
    )
    assert status == 400
    assert worker.seen == []


def test_the_boxs_thread_choice_is_capped_to_the_hubs_cores() -> None:
    worker = FakeWorker()
    status, _ = _service(worker, max_threads=1).handle(
        _frame(), bytes(360_000), authorization="Bearer secret"
    )
    assert status == 200
    assert worker.seen[0]["config"]["threads"] == 1


def test_a_second_request_while_decoding_is_503_immediately() -> None:
    gate = threading.Event()
    worker = FakeWorker(gate=gate)
    service = _service(worker)
    first: list[int] = []
    thread = threading.Thread(
        target=lambda: first.append(
            service.handle(_frame(), bytes(360_000), authorization="Bearer secret")[0]
        )
    )
    thread.start()
    while not worker.seen:  # wait for the decode to be in flight
        time.sleep(0.002)
    started = time.monotonic()
    status, _ = service.handle(_frame(), bytes(360_000), authorization="Bearer secret")
    elapsed = time.monotonic() - started
    gate.set()
    thread.join(5.0)

    assert status == 503
    assert elapsed < 0.5, "busy must answer, not wait for the running decode"
    assert first == [200]


def test_a_wrong_class_of_frame_is_rejected_before_the_worker() -> None:
    worker = FakeWorker()
    status, _ = _service(worker).handle(
        _frame(type="decode_also_wrong"), bytes(360_000), authorization="Bearer secret"
    )
    assert status == 400
    assert worker.seen == []


# ── through a real socket: the handler is code too ───────────────────────


class _LiveService:
    """The service behind a real ThreadingHTTPServer on a loopback port."""

    def __init__(self, service) -> None:
        from http.server import ThreadingHTTPServer

        self._service = service
        self._server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _load_service().make_handler(service)
        )
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    def post(self, body: bytes, *, headers: dict[str, str]):
        import http.client
        import json as _json

        host, port = self._server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            connection.request("POST", "/mrrc-decode", body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    @property
    def config_header(self) -> str:
        import base64
        import json as _json

        return base64.b64encode(
            _json.dumps(_frame()).encode("utf-8")
        ).decode("ascii")


def test_over_a_socket_a_valid_request_returns_decode_ok() -> None:
    worker = FakeWorker()
    with _LiveService(_service(worker)) as live:
        status, body = live.post(
            bytes(360_000),
            headers={
                "Authorization": "Bearer secret",
                "X-MRRC-Config": live.config_header,
                "Content-Type": "application/octet-stream",
            },
        )

    assert status == 200
    assert decode_frame(body)["type"] == "decode_ok"
    assert worker.seen[0]["config"]["threads"] == 1


def test_over_a_socket_no_token_is_401_even_with_a_full_body() -> None:
    worker = FakeWorker()
    with _LiveService(_service(worker)) as live:
        status, _ = live.post(
            bytes(360_000),
            headers={"X-MRRC-Config": live.config_header},
        )

    assert status == 401
    assert worker.seen == []


def test_over_a_socket_a_wrong_length_is_400() -> None:
    worker = FakeWorker()
    with _LiveService(_service(worker)) as live:
        status, _ = live.post(
            bytes(10),
            headers={"Authorization": "Bearer secret", "X-MRRC-Config": live.config_header},
        )

    assert status == 400
    assert worker.seen == []


def test_over_a_socket_a_malformed_config_header_is_400() -> None:
    worker = FakeWorker()
    with _LiveService(_service(worker)) as live:
        status, _ = live.post(
            bytes(360_000),
            headers={"Authorization": "Bearer secret", "X-MRRC-Config": "not base64!"},
        )

    assert status == 400
    assert worker.seen == []
