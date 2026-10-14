"""mrrc-decode — the hub side of the FT8 cloud deep path (design A1).

One HTTP endpoint, one worker, one decode at a time.  The request is a
``decode_remote`` frame (base64 in ``X-MRRC-Config``) with the slot as the
body; the answer is the worker's own ``decode_ok``, forwarded verbatim so the
box parses it with the code it already trusts.

Two decisions worth stating, because both are deliberate:

* **The box's deadline is ignored.**  ``deadline_monotonic`` is a monotonic
  clock reading from another machine and means nothing here; the hub bounds a
  request with its own timeout.  The field travels only because the frame
  schema requires it.
* **Busy answers 503 instead of queueing.**  The box has a local deep path to
  fall back on, so waiting would only convert a fast failure into a slow one —
  and on a 1-vCPU entry box a queue is how the entry path starts to stutter.

The service runs the same ``ft8/core`` worker the box runs, from the same
source tree, so a decode performed here is the decode the box would have
performed — that is the whole claim this feature rests on.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path

# The app root must be importable before anything from ft8 is imported: this
# file is deployed beside the tree it serves, not inside it.
_APP_ROOT = os.environ.get("MRRC_DECODE_APP_ROOT", "/opt/mrrc-decode")
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from ft8.core.models import DecodeConfig  # noqa: E402
from ft8.core.protocol import (  # noqa: E402
    SLOT_PCM_BYTES,
    FrameError,
    decode_frame,
    encode_frame,
)
from ft8.engine.dsp_decode import decode_config_frame, decode_config_from_frame  # noqa: E402
from ft8.engine.orchestrator import SLOT_SAMPLES_NBYTES  # noqa: E402

log = logging.getLogger("mrrc.hub.decode")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8990
DEFAULT_TIMEOUT_S = 10.0
# One core, and the box's own thread count was chosen for four A53s.  The cap
# is defence in depth: the box already sends threads=1, and a future caller
# that forgets must not be able to oversubscribe the entry box.
DEFAULT_MAX_THREADS = 1
PATH = "/mrrc-decode"


class DecodeService:
    """The request logic, with the HTTP layer deliberately absent.

    Every decision that matters — who may decode, what counts as one slot, how
    many decodes may be in flight, whose clock bounds the worker — lives here,
    so the tests can drive it without a socket.
    """

    def __init__(
        self,
        worker,
        *,
        token: str,
        max_threads: int = DEFAULT_MAX_THREADS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        monotonic=time.monotonic,
    ) -> None:
        if not token:
            raise ValueError("the decode service needs a token")
        if max_threads < 1:
            raise ValueError("max_threads must be positive")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._worker = worker
        self._token = token
        self._max_threads = int(max_threads)
        self._timeout_s = float(timeout_s)
        self._monotonic = monotonic
        # Non-blocking: the second caller is answered "busy", never parked.
        self._busy = threading.Lock()
        self._segment: SharedMemory | None = None

    # -- request handling --------------------------------------------------

    def authorized(self, authorization: str | None) -> bool:
        """Whether this caller may decode at all.

        Exposed separately so the HTTP layer can refuse **before** reading a
        360 KB body: an unauthenticated client does not get to decide how much
        this process reads.
        """

        return authorization == f"Bearer {self._token}"

    def handle(
        self, frame: dict[str, object], pcm: bytes, *, authorization: str | None
    ) -> tuple[int, bytes]:
        """Answer one request: ``(status, body)``; the body is empty unless 200."""

        if not self.authorized(authorization):
            return 401, b""
        if len(pcm) != SLOT_PCM_BYTES:
            return 400, b""
        try:
            encode_frame(frame)  # the same exact schema the worker enforces
        except FrameError as error:
            log.info("rejected a malformed request: %s", error)
            return 400, b""
        if frame.get("type") != "decode_remote":
            log.info("rejected a %r frame", frame.get("type"))
            return 400, b""

        if not self._busy.acquire(blocking=False):
            return 503, b""
        try:
            return self._decode(frame, pcm)
        finally:
            self._busy.release()

    def _decode(self, frame: dict[str, object], pcm: bytes) -> tuple[int, bytes]:
        try:
            config = decode_config_from_frame(frame["config"])  # type: ignore[arg-type]
        except (TypeError, ValueError, KeyError) as error:
            log.info("rejected a malformed config: %s", error)
            return 400, b""
        config = config.replace(threads=min(config.threads, self._max_threads))
        slot_id = int(frame["slot_id"])  # type: ignore[arg-type]

        segment = self._ensure_segment()
        segment.buf[: len(pcm)] = pcm
        worker_frame = {
            "type": "decode",
            "slot_id": slot_id,
            # The hub's own clock — see the module docstring.
            "deadline_monotonic": self._monotonic() + self._timeout_s,
            "shm": {
                "name": segment.name,
                "dtype": "<i2",
                "shape": [SLOT_SAMPLES_NBYTES // 2],
                "nbytes": SLOT_SAMPLES_NBYTES,
            },
            "config": decode_config_frame(config),
        }
        started = self._monotonic()
        try:
            response = self._worker.request(worker_frame, self._timeout_s)
        except Exception:  # noqa: BLE001 — a worker fault is the box's problem to solve
            log.exception("the worker failed on slot %s", slot_id)
            return 500, b""
        elapsed = self._monotonic() - started
        if not isinstance(response, dict) or response.get("type") != "decode_ok":
            log.error("the worker answered with %r", None if not isinstance(response, dict) else response.get("type"))
            return 500, b""
        log.info(
            "slot %s: profile %d cycles %d threads %d -> %d result(s) in %.3fs (cpu %.3fs)",
            slot_id,
            config.profile,
            config.cycles,
            config.threads,
            len(response.get("results") or []),
            elapsed,
            float(response.get("cpu_seconds") or 0.0),
        )
        return 200, encode_frame(response)

    def _ensure_segment(self) -> SharedMemory:
        if self._segment is None:
            self._segment = SharedMemory(create=True, size=SLOT_PCM_BYTES)
        return self._segment

    def close(self) -> None:
        segment, self._segment = self._segment, None
        if segment is not None:
            segment.close()
            segment.unlink()


def make_handler(service: DecodeService):
    """Build the HTTP handler bound to one service instance."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "mrrc-decode/1"
        # HTTP/1.1, because the box keeps the connection open between slots and
        # BaseHTTPRequestHandler defaults to 1.0, which closes after every
        # response.  Behind nginx (the hub deployment) this never showed — nginx
        # holds its own upstream connection — but a direct host deployment
        # lost every other escalation to a socket that was already closed
        # (measured 2026-10-14 on ham.vlsc.net: cloud, cloud, unavailable,
        # unavailable).
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
            if self.path != PATH:
                self.send_error(404)
                return
            if not service.authorized(self.headers.get("Authorization")):
                # Before the body: see DecodeService.authorized.
                self.close_connection = True
                self.send_error(401)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length != SLOT_PCM_BYTES:
                # Do not read 360 KB to answer "no": the length is the claim.
                self.close_connection = True
                self.send_error(400)
                return
            pcm = self.rfile.read(length)
            try:
                header = base64.b64decode(self.headers.get("X-MRRC-Config") or "", validate=True)
                frame = json.loads(header)
            except Exception:  # noqa: BLE001 — any malformed header is a 400
                self.send_error(400)
                return
            status, body = service.handle(
                frame, pcm, authorization=self.headers.get("Authorization")
            )
            self.send_response(status)
            if body:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 — base class signature
            log.debug("%s %s", self.address_string(), format % args)

    return Handler


def make_server(host: str, port: int, handler):
    """A ThreadingHTTPServer that can bind an IPv6 (dual-stack) address.

    The hub deployment hides behind nginx on 127.0.0.1; a host/LAN deployment
    (``--host ::``) terminates TLS itself, and ``HTTPServer`` is IPv4-only
    unless the address family is set here.
    """

    family = socket.AF_INET6 if ":" in host else socket.AF_INET

    class _Server(ThreadingHTTPServer):
        address_family = family

    return _Server((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="mrrc-decode: the FT8 cloud deep path")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token-file", default="/etc/mrrc-decode/token")
    parser.add_argument("--library", default="")
    parser.add_argument("--app-root", default=_APP_ROOT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--max-threads", type=int, default=DEFAULT_MAX_THREADS)
    parser.add_argument("--cert", default="", help="TLS certificate (PEM); needs --key")
    parser.add_argument("--key", default="", help="TLS private key (PEM); needs --cert")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        log.error("%s is empty", args.token_file)
        return 2

    from ft8.core.supervisor import WorkerSupervisor
    from ft8.core.worker import default_library_path

    library = args.library or default_library_path()
    if not Path(library).is_file():
        log.error("the native library is not there: %s", library)
        return 2

    # Production runs the decoder from the app root: the vendored Fortran
    # resolves ALLCALL7.TXT relative to the cwd (prog_args.f90:4), exactly as
    # the box's service unit does.
    os.chdir(args.app_root)
    supervisor = WorkerSupervisor(library_path=library, stop_timeout=5.0)
    supervisor.start()
    service = DecodeService(
        supervisor, token=token, max_threads=args.max_threads, timeout_s=args.timeout
    )
    server = make_server(args.host, args.port, make_handler(service))
    if args.cert or args.key:
        if not (args.cert and args.key):
            log.error("--cert and --key go together")
            return 2
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.cert, args.key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        log.info("TLS enabled with %s", args.cert)
    log.info("listening on %s:%d (library %s)", args.host, args.port, library)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
        supervisor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
