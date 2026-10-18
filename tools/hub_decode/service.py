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
import re
import socket
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path

# The app root must be importable before anything from ft8 is imported: this
# file is deployed beside the tree it serves, not inside it.
_APP_ROOT = os.environ.get("MRRC_DECODE_APP_ROOT", "/opt/mrrc-decode")
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from ft8.core.models import MAX_DECODE_THREADS, DecodeConfig  # noqa: E402
from ft8.core.protocol import (  # noqa: E402
    SLOT_PCM_BYTES,
    FrameError,
    decode_frame,
    encode_frame,
)
from ft8.engine.dsp_decode import (  # noqa: E402
    batch_from_decode_ok,
    decode_config_frame,
    decode_config_from_frame,
    decode_ok_frame,
)
from ft8.engine.cascade import merge_batches  # noqa: E402
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


@dataclass(frozen=True, slots=True)
class PortfolioEntry:
    """One configuration in the service's portfolio."""

    profile: int
    cycles: int
    low_threshold: bool = False
    sensitivity: int = 2

    @property
    def name(self) -> str:
        suffix = f"-s{self.sensitivity}" if self.sensitivity != 2 else ""
        return f"p{self.profile}c{self.cycles}{'lt' if self.low_threshold else ''}{suffix}"


def parse_portfolio(spec: str) -> tuple[PortfolioEntry, ...]:
    """``p1c3lt,p3c3`` → the configurations to run for one slot, in order.

    Grammar per entry: ``p<profile>c<cycles>`` with an optional ``lt`` suffix
    (``low_threshold``).  **The first entry is the primary**: its results are
    kept whole and the others only contribute signals it did not hear, which is
    the same (frequency, dt) rule the box merges with (`merge_batches`).

    The portfolio is a *service-side* decision on purpose: how much CPU to
    spend per slot belongs to whoever owns the machine, and it is measured —
    2026-10-14, on 40 real slots, a single configuration covered 96.5-97.0% of
    what ten could find, and three diverse ones running in parallel covered
    100% inside the wall time of the slowest.
    """

    entries: list[PortfolioEntry] = []
    for raw in (spec or "").split(","):
        token = raw.strip().lower()
        if not token:
            continue
        match = re.fullmatch(r"p(\d+)c(\d+)(lt)?(?:-s([123]))?", token)
        if match is None:
            raise ValueError(f"a portfolio entry looks like p1c3lt or p1c3lt-s3: {raw!r}")
        entries.append(
            PortfolioEntry(
                profile=int(match.group(1)),
                cycles=int(match.group(2)),
                low_threshold=bool(match.group(3)),
                sensitivity=int(match.group(4) or 2),
            )
        )
    if not entries:
        raise ValueError("an empty portfolio would decode nothing")
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class _Task:
    """One decode: a configuration, the segment holding its input, and a label."""

    config: DecodeConfig
    segment: SharedMemory
    label: str = ""


class Enhancer:
    """The optional ML front-end (P3): decode the slot twice, union the answers.

    Deployment shape, fixed by measurement rather than taste: the mask is an
    *extra input*, never a replacement.  The slot is decoded plain and enhanced
    and ``merge_batches`` unions the two, so a message the mask damages costs
    nothing — the plain copy still heard it.  On real slots at ``alpha=0.7``
    that is +0.5-0.9% net for 2.4% lost inside the enhanced copy alone
    (docs/superpowers/specs/2026-10-14-ft8-ml-enhancement-design.md).

    Torch is optional at runtime as well as in the dependency list: a host
    without it, or without the model file, keeps decoding exactly as it did
    before this class existed — and says so once, in the log.
    """

    def __init__(self, model: Path, alpha: float = 0.7) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError("enhancement alpha must be in (0, 1]")
        self._model_path = Path(model)
        self.alpha = float(alpha)
        self._bundle: tuple[object, object] | None = None
        self._warned = False
        self.enhanced_slots = 0

    def _disable(self, reason: str) -> None:
        if not self._warned:
            self._warned = True
            log.warning("enhancement disabled: %s", reason)

    def _load(self):
        """Load the model once; a failure disables the stage for good."""

        if self._bundle is not None:
            return self._bundle
        try:
            import torch
        except ImportError as error:
            self._disable(f"torch is not installed ({error})")
            return None
        if not self._model_path.exists():
            self._disable(f"{self._model_path} does not exist")
            return None
        try:
            from tools.ft8.ml_enhance import build_model

            payload = torch.load(self._model_path, map_location="cpu", weights_only=False)
            model = build_model()
            model.load_state_dict(payload["model"])
            model.eval()
        except Exception as error:  # noqa: BLE001 — a broken model is not a crash
            self._disable(f"the model could not be loaded ({error})")
            return None
        self._bundle = (torch, model)
        log.info("enhancement enabled: %s at alpha=%.2f", self._model_path, self.alpha)
        return self._bundle

    def enhance(self, pcm: bytes) -> bytes | None:
        """Return the enhanced slot, or ``None`` if the stage is unavailable."""

        bundle = self._load()
        if bundle is None:
            return None
        torch, model = bundle
        try:
            import numpy as np

            from tools.ft8.ml_enhance import enhance

            samples = np.frombuffer(pcm, dtype="<i2")
            with torch.no_grad():  # type: ignore[union-attr]
                out = enhance(samples, model, self.alpha)
        except Exception:  # noqa: BLE001 — the plain answer still stands
            log.exception("enhancement failed; decoding the plain slot only")
            self._disable("the model raised")
            return None
        self.enhanced_slots += 1
        return out.tobytes()


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
        portfolio: tuple[PortfolioEntry, ...] = (),
        enhancer: "Enhancer | None" = None,
    ) -> None:
        if not token:
            raise ValueError("the decode service needs a token")
        if max_threads < 1:
            raise ValueError("max_threads must be positive")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        workers = tuple(worker) if isinstance(worker, (list, tuple)) else (worker,)
        if not workers:
            raise ValueError("the decode service needs at least one worker")
        self._workers = workers
        # Empty portfolio = decode once, with exactly the configuration the
        # caller asked for.  That is the hub's behaviour and it stays the
        # default; a host deployment opts into spending more CPU per slot.
        self._portfolio = tuple(portfolio)
        # Optional ML front-end: an extra input beside the untouched one, never a
        # replacement (see Enhancer).  None is today's behaviour.
        self._enhancer = enhancer
        self._token = token
        self._max_threads = int(max_threads)
        self._timeout_s = float(timeout_s)
        self._monotonic = monotonic
        # Non-blocking: the second caller is answered "busy", never parked.
        self._busy = threading.Lock()
        # One segment per input: the plain slot and (when enhancement is on) the
        # enhanced copy.  Two, because the two decodes run *in parallel* —
        # serialising them would cost 2x ~1.8 s and blow the box's deadline.
        self._segments: list[SharedMemory] = []

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
            requested = decode_config_from_frame(frame["config"])  # type: ignore[arg-type]
        except (TypeError, ValueError, KeyError) as error:
            log.info("rejected a malformed config: %s", error)
            return 400, b""
        # Clamp to the service's cap **and** to the ABI's own maximum: a caller
        # asking for more than the decoder supports is a hint, not an error, and
        # letting it through made the worker answer 'error' -> HTTP 500 (measured
        # 2026-10-14: threads=16 on a 16-thread host).
        threads = min(requested.threads, self._max_threads, MAX_DECODE_THREADS)
        slot_id = int(frame["slot_id"])  # type: ignore[arg-type]

        segment = self._ensure_segment(0)
        segment.buf[: len(pcm)] = pcm
        configs = [
            (requested if entry is None else requested.replace(
                profile=entry.profile,
                cycles=entry.cycles,
                low_threshold=entry.low_threshold,
                sensitivity=entry.sensitivity,
            )).replace(threads=threads)
            for entry in (self._portfolio or (None,))
        ]
        tasks = [_Task(config, segment) for config in configs]
        if self._enhancer is not None:
            enhanced = self._enhancer.enhance(pcm)
            if enhanced is not None:
                other = self._ensure_segment(1)
                other.buf[: len(enhanced)] = enhanced
                tasks += [_Task(config, other, "enh") for config in configs]

        started = self._monotonic()
        replies = self._run(tasks, slot_id)
        elapsed = self._monotonic() - started
        if not replies:
            log.error("every configuration failed for slot %s", slot_id)
            return 500, b""
        batch = batch_from_decode_ok(replies[0])
        cpu_seconds = float(replies[0].get("cpu_seconds") or 0.0)
        for reply in replies[1:]:
            batch = merge_batches(batch, batch_from_decode_ok(reply))
            cpu_seconds += float(reply.get("cpu_seconds") or 0.0)
        log.info(
            "slot %s: %s -> %d result(s) in %.3fs (cpu %.3fs total)",
            slot_id,
            " + ".join(
                f"p{c.profile}c{c.cycles}{'lt' if c.low_threshold else ''}"
                f"{f'-s{c.sensitivity}' if c.sensitivity != 2 else ''}"
                f"{' enh' if t.label else ''}"
                for t in tasks
                for c in (t.config,)
            ),
            len(batch.results),
            elapsed,
            cpu_seconds,
        )
        # The reply's base fields (v / generation / request_id) belong to the
        # transport, so decode_ok_frame does not invent them; echo the worker's,
        # which is what the caller would have seen before the union existed.
        reply_frame = decode_ok_frame(batch, cpu_seconds=cpu_seconds)
        reply_frame.update(
            {
                field: replies[0][field]
                for field in ("v", "generation", "request_id")
                if field in replies[0]
            }
        )
        return 200, encode_frame(reply_frame)

    def _run(
        self, tasks: list[_Task], slot_id: int
    ) -> list[dict[str, object]]:
        """Decode the slot once per task, across the worker pool, in parallel.

        One shared segment per input: the workers map them read-only and the busy
        lock guarantees nobody rewrites one underneath a decode that is still
        running.  A task that fails is dropped from the union rather than failing
        the request — a partial answer is the whole point of running several.
        """

        def one(index: int) -> dict[str, object] | None:
            task = tasks[index]
            config, segment = task.config, task.segment
            worker = self._workers[index % len(self._workers)]
            worker_frame = {
                "type": "decode",
                "slot_id": slot_id,
                # The hub's own clock — the caller's is a different machine's.
                "deadline_monotonic": self._monotonic() + self._timeout_s,
                "shm": {
                    "name": segment.name,
                    "dtype": "<i2",
                    "shape": [SLOT_SAMPLES_NBYTES // 2],
                    "nbytes": SLOT_SAMPLES_NBYTES,
                },
                "config": decode_config_frame(config),
            }
            try:
                reply = worker.request(worker_frame, self._timeout_s)
            except Exception:  # noqa: BLE001 — one configuration may die; the rest still count
                log.exception(
                    "p%dc%d failed on slot %s", config.profile, config.cycles, slot_id
                )
                return None
            if not isinstance(reply, dict) or reply.get("type") != "decode_ok":
                log.error("p%dc%d answered %r", config.profile, config.cycles, reply)
                return None
            return reply

        if len(tasks) == 1:
            reply = one(0)
            return [reply] if reply is not None else []
        with ThreadPoolExecutor(
            max_workers=min(len(tasks), len(self._workers))
        ) as pool:
            return [reply for reply in pool.map(one, range(len(tasks))) if reply is not None]

    def _ensure_segment(self, index: int) -> SharedMemory:
        while len(self._segments) <= index:
            self._segments.append(SharedMemory(create=True, size=SLOT_PCM_BYTES))
        return self._segments[index]

    def close(self) -> None:
        segments, self._segments = self._segments, []
        for segment in segments:
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
    parser.add_argument(
        "--portfolio", default="",
        help="e.g. p1c3lt,p3c3,p3c3lt — decode the slot with each (in parallel) and union",
    )
    parser.add_argument(
        "--portfolio-workers", type=int, default=0,
        help="workers to run the portfolio on (default: one per entry)",
    )
    parser.add_argument(
        "--enhance", default="",
        help="mask model (.pt), or model.pt:alpha, for the ML front-end: the slot is "
             "decoded plain and enhanced, in parallel, and the answers are unioned "
             "(default alpha 0.7; needs --portfolio-workers >= 2 to stay parallel)",
    )
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
    portfolio = parse_portfolio(args.portfolio) if args.portfolio else ()
    worker_count = args.portfolio_workers or max(1, len(portfolio))
    enhancer = None
    if args.enhance:
        model_text, _, alpha_text = args.enhance.partition(":")
        enhancer = Enhancer(Path(model_text), float(alpha_text) if alpha_text else 0.7)
        # Enhancement doubles the decodes per slot, and they must run *in
        # parallel*: serialised, the box's 3.2 s deadline is spent in a queue
        # and the deep answer is dropped altogether — a much worse outcome than
        # an extra worker process.
        required = 2 * max(1, len(portfolio))
        if worker_count < required:
            log.info(
                "raising the worker pool to %d: enhancement decodes each slot twice",
                required,
            )
            worker_count = required
    supervisors = [
        WorkerSupervisor(library_path=library, stop_timeout=5.0)
        for _ in range(worker_count)
    ]
    for supervisor in supervisors:
        supervisor.start()
    service = DecodeService(
        supervisors if worker_count > 1 else supervisors[0],
        token=token,
        max_threads=args.max_threads,
        timeout_s=args.timeout,
        portfolio=portfolio,
        enhancer=enhancer,
    )
    if portfolio:
        log.info(
            "portfolio: %s on %d worker(s)",
            " + ".join(entry.name for entry in portfolio),
            worker_count,
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
        for supervisor in supervisors:
            supervisor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
