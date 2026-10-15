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
import sys
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
    # Required, not decoration: dataclasses resolves cls.__module__ through
    # sys.modules, so a module loaded by path without this line dies the moment
    # it declares one (the service grew a PortfolioEntry dataclass).
    sys.modules[spec.name] = module
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


def test_the_connection_survives_between_slots() -> None:
    """The box keeps one connection open and reuses it every 15 s.

    ``BaseHTTPRequestHandler`` defaults to HTTP/1.0, which closes after every
    response; the service speaks 1.1 so the client's connection — and its TLS
    session — survives between slots.  (This test passes either way: http.client
    reconnects transparently when it sees the close.  It is here as the
    behavioural contract, not as the regression that explains the 2026-10-14
    host-deployment failures — those were a box running a tree without the CA
    option, and the log line now carries the error text that says so.)
    """

    import http.client

    worker = FakeWorker()
    with _LiveService(_service(worker)) as live:
        host, port = live._server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            for attempt in range(3):
                connection.request(
                    "POST", "/mrrc-decode", body=bytes(360_000),
                    headers={
                        "Authorization": "Bearer secret",
                        "X-MRRC-Config": live.config_header,
                        "Content-Type": "application/octet-stream",
                    },
                )
                response = connection.getresponse()
                body = response.read()
                assert response.status == 200, f"request {attempt} answered {response.status}"
                assert decode_frame(body)["type"] == "decode_ok"
        finally:
            connection.close()

    assert len(worker.seen) == 3


def test_a_thread_count_beyond_the_abi_is_clamped_not_rejected() -> None:
    """The number is a hint: asking for 16 on a 16-thread host must still decode.

    Measured 2026-10-14: the worker answered 'error' for threads=16 (the ABI
    caps at 12), the service turned that into HTTP 500, and the box fell back
    to its local deep path — a working host refusing work over a number.
    """

    worker = FakeWorker()
    status, _ = _service(worker, max_threads=16).handle(
        _frame(config={**CONFIG, "threads": 16}), bytes(360_000),
        authorization="Bearer secret",
    )
    assert status == 200
    assert worker.seen[0]["config"]["threads"] == 12


# ── the portfolio: spend the host's idle CPU on recall ───────────────────


class _ByConfigWorker:
    """Answers a different set of messages per (profile, cycles)."""

    def __init__(self, by_config: dict[tuple[int, int], list[tuple[str, float, float]]]) -> None:
        self._by_config = by_config
        self.seen: list[dict[str, object]] = []

    def request(self, frame, timeout):
        self.seen.append(frame)
        profile = frame["config"]["profile"]
        cycles = frame["config"]["cycles"]
        results = [
            {"slot_id": frame["slot_id"], "sync": 1.0, "snr": -20, "dt": dt,
             "frequency": frequency, "text": text, "ap_type": 0, "quality": 0.0, "flags": 0}
            for text, frequency, dt in self._by_config.get((profile, cycles), [])
        ]
        return {
            "v": PROTOCOL_VERSION, "type": "decode_ok", "generation": 0, "request_id": 0,
            "slot_id": frame["slot_id"], "path": "improved", "results": results,
            "overflow": False, "elapsed_seconds": 0.5, "cpu_seconds": 1.0,
            "deadline_missed": False,
        }


def test_the_portfolio_grammar() -> None:
    service = _load_service()
    assert [entry.name for entry in service.parse_portfolio("p1c3lt,p3c3")] == ["p1c3lt", "p3c3"]
    assert service.parse_portfolio("p1c3")[0].low_threshold is False
    with pytest.raises(ValueError, match="p1c3lt"):
        service.parse_portfolio("garbage")
    with pytest.raises(ValueError, match="empty"):
        service.parse_portfolio("   ")


def test_a_portfolio_runs_every_entry_and_unions_the_answers() -> None:
    """Two configurations, two different signals — the caller gets both."""

    service_module = _load_service()
    worker = _ByConfigWorker({
        (1, 3): [("CQ AAA", 1500.0, 0.10)],
        (3, 3): [("CQ BBB", 1600.0, 0.20)],
    })
    service = service_module.DecodeService(
        worker, token="secret", monotonic=lambda: 500.0,
        portfolio=service_module.parse_portfolio("p1c3lt,p3c3"),
    )

    status, body = service.handle(_frame(), bytes(360_000), authorization="Bearer secret")

    assert status == 200
    decoded = decode_frame(body)
    assert {r["text"] for r in decoded["results"]} == {"CQ AAA", "CQ BBB"}
    assert decoded["cpu_seconds"] == 2.0, "the union reports what it cost"
    assert [(f["config"]["profile"], f["config"]["low_threshold"]) for f in worker.seen] == [
        (1, True), (3, False),
    ]


def test_the_union_folds_by_signal_not_by_text() -> None:
    """The same signal at the same (freq, dt) must appear once, not twice."""

    service_module = _load_service()
    worker = _ByConfigWorker({
        (1, 3): [("CQ AAA", 1500.0, 0.10)],
        (3, 3): [("CQ AAA", 1500.4, 0.12)],  # 0.4 Hz / 0.02 s apart: one signal
    })
    service = service_module.DecodeService(
        worker, token="secret", monotonic=lambda: 500.0,
        portfolio=service_module.parse_portfolio("p1c3,p3c3"),
    )

    status, body = service.handle(_frame(), bytes(360_000), authorization="Bearer secret")

    assert status == 200
    assert len(decode_frame(body)["results"]) == 1


def test_one_failing_configuration_does_not_lose_the_others() -> None:
    """A partial answer is the whole reason for running several."""

    service_module = _load_service()

    class HalfBroken(_ByConfigWorker):
        def request(self, frame, timeout):
            if frame["config"]["profile"] == 3:   # the p3 entry dies; p1 survives
                raise RuntimeError("this worker died")
            return super().request(frame, timeout)

    worker = HalfBroken({(1, 3): [("CQ AAA", 1500.0, 0.10)]})
    service = service_module.DecodeService(
        worker, token="secret", monotonic=lambda: 500.0,
        portfolio=service_module.parse_portfolio("p1c3,p3c3"),
    )

    status, body = service.handle(_frame(), bytes(360_000), authorization="Bearer secret")

    assert status == 200
    assert [r["text"] for r in decode_frame(body)["results"]] == ["CQ AAA"]


def test_without_a_portfolio_the_callers_config_is_used_verbatim() -> None:
    worker = FakeWorker()
    status, body = _service(worker).handle(
        _frame(), bytes(360_000), authorization="Bearer secret"
    )
    assert status == 200
    assert worker.seen[0]["config"]["cycles"] == 1  # the request's own cycles
    assert worker.seen[0]["config"]["profile"] == 3


class FakeEnhancer:
    """Stands in for the mask model: returns a distinguishable slot, or nothing."""

    def __init__(self, produces: bool = True) -> None:
        self.produces = produces
        self.calls = 0

    def enhance(self, pcm: bytes) -> bytes | None:
        self.calls += 1
        return b"\x01\x00" * 180_000 if self.produces else None


def test_enhancement_decodes_the_slot_twice_and_unions_the_answers() -> None:
    """The mask is an extra input, never a replacement.

    Two decodes of the same slot, and the answer is their union — which is what
    makes a mask that damages a message cost nothing: the plain copy still heard
    it.  (Measured on real slots at alpha=0.7: +0.5-0.9% net with 2.4% lost
    inside the enhanced copy alone.)
    """

    module = _load_service()

    class Worker:
        def __init__(self) -> None:
            self.seen: list[dict[str, object]] = []

        def request(self, frame, timeout):
            self.seen.append(frame)
            text = "CQ AAA" if len(self.seen) == 1 else "CQ BBB"
            return {
                "v": PROTOCOL_VERSION, "type": "decode_ok", "generation": 0,
                "request_id": 0, "slot_id": frame["slot_id"],
                "path": frame["config"]["path"], "overflow": False,
                "elapsed_seconds": 0.5, "cpu_seconds": 0.5, "deadline_missed": False,
                "results": [
                    {"slot_id": frame["slot_id"], "sync": 0.9, "snr": -10, "dt": 0.1,
                     "frequency": 1000.0 + (0.0 if text == "CQ AAA" else 500.0),
                     "text": text, "ap_type": 0, "quality": 1.0, "flags": 0}
                ],
            }

    worker = Worker()
    enhancer = FakeEnhancer()
    service = module.DecodeService(
        worker, token="secret", monotonic=lambda: 500.0, enhancer=enhancer
    )
    status, body = service.handle(_frame(), bytes(360_000), authorization="Bearer secret")

    assert status == 200
    texts = {result["text"] for result in decode_frame(body)["results"]}
    assert texts == {"CQ AAA", "CQ BBB"}, "the answer must be the union of both decodes"
    assert enhancer.calls == 1
    assert len(worker.seen) == 2
    # Two different segments: the workers run in parallel, so neither may be
    # overwritten while the other is still decoding.
    assert worker.seen[0]["shm"]["name"] != worker.seen[1]["shm"]["name"]


def test_a_failed_enhancement_still_answers_from_the_plain_decode() -> None:
    module = _load_service()
    worker = FakeWorker()
    enhancer = FakeEnhancer(produces=False)
    status, body = _service(worker, enhancer=enhancer).handle(
        _frame(), bytes(360_000), authorization="Bearer secret"
    )

    assert status == 200
    assert decode_frame(body)["type"] == "decode_ok"
    assert enhancer.calls == 1
    assert len(worker.seen) == 1, "an unavailable mask means one decode, as before"


def test_the_enhancer_is_disabled_once_when_its_model_is_missing(tmp_path, caplog) -> None:
    """No torch, no model: the host keeps decoding, and says so exactly once."""

    import logging as _logging

    module = _load_service()
    enhancer = module.Enhancer(tmp_path / "absent.pt", 0.7)
    with caplog.at_level(_logging.WARNING, logger="mrrc.hub.decode"):
        assert enhancer.enhance(bytes(360_000)) is None
        assert enhancer.enhance(bytes(360_000)) is None
    warnings = [r for r in caplog.records if "enhancement disabled" in r.getMessage()]
    assert len(warnings) == 1


def test_enhancement_requires_a_plausible_alpha(tmp_path) -> None:
    module = _load_service()
    for alpha in (0.0, -0.5, 1.5):
        with pytest.raises(ValueError):
            module.Enhancer(tmp_path / "model.pt", alpha)
