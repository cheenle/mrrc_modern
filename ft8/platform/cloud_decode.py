"""Cloud deep-path client: the same decode call, executed on the hub.

Design: ``docs/superpowers/specs/2026-10-14-ft8-cloud-escalation-design.md``.

The transport is a plain HTTPS POST through the existing nginx — no new
package, no new port, and the existing certificate verifies against the system
store, so nothing is pinned.  The request frame is ``decode_remote`` and the
reply is the worker's own ``decode_ok``, so a cloud batch is parsed by exactly
the code that parses a local one.

The hub is not asked how long to take: ``deadline_monotonic`` travels because
the frame schema requires it, but a monotonic clock means nothing across
machines.  The deadline lives here, as a socket timeout (plus a second of
slack so the failure is reported as a timeout rather than as a broken
connection) and, in the cascade, as an ``asyncio.wait_for``.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import json
import logging
import ssl
import time
from urllib.parse import urlsplit

from ft8.core.models import CloudUnavailable, DecodeBatch, DecodeConfig, DecodePath
log = logging.getLogger("mrrc.ft8.cloud_decode")

from ft8.core.protocol import PROTOCOL_VERSION, FrameError, decode_frame
from ft8.engine.dsp_decode import (
    ApContextProvider,
    batch_from_decode_ok,
    decode_config_frame,
)
from ft8.engine.orchestrator import SLOT_SAMPLES_NBYTES

# The default target is the 1-vCPU hub: measured 2026-10-14, one deep pass at
# profile 3 costs 0.476 s at threads=1 and 0.583 / 0.644 s at threads=2 / 3, so
# the box's own thread count (chosen for four A53 cores) is the wrong number to
# send there.  A host deployment with real cores overrides it with
# ``MRRC_FT8_CLOUD_DECODE_THREADS``; the service caps whatever arrives at its
# own ``--max-threads``.
CLOUD_THREADS = 1

DEFAULT_CYCLES = 3
DEFAULT_DEADLINE_S = 2.5
_SOCKET_SLACK_S = 1.0


class CloudDecoder:
    """A ``decode_path`` provider backed by the hub's decode service."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        cycles: int = DEFAULT_CYCLES,
        deadline_s: float = DEFAULT_DEADLINE_S,
        config: DecodeConfig | None = None,
        allow_plain_http: bool = False,
        ca_file: str = "",
        threads: int = CLOUD_THREADS,
        trace: bool = False,
        clock=time.monotonic,
        ap_context: ApContextProvider | None = None,
    ) -> None:
        parts = urlsplit(url)
        secure = parts.scheme == "https"
        if not secure and not (parts.scheme == "http" and allow_plain_http):
            raise ValueError("cloud decode url must be https://host[:port]/path")
        if not parts.hostname or not parts.path:
            raise ValueError("cloud decode url must be https://host[:port]/path")
        if not token:
            raise ValueError("cloud decode token must not be empty")
        self._host = parts.hostname
        self._port = parts.port
        self._path = parts.path
        self._tls = secure
        self._token = token
        self._config = config or DecodeConfig()
        self._clock = clock
        self.cycles = int(cycles)
        self.threads = max(1, int(threads))
        self.deadline_s = float(deadline_s)
        # An **extra** trust anchor, not a replacement: the default context keeps
        # the system store, so a private host can be added (a LAN decoder with a
        # self-signed certificate) without breaking the box's other outbound TLS
        # (portal, update checks).  Loaded lazily so a bad path becomes
        # CloudUnavailable on the first attempt — the cascade falls back to the
        # local deep path — rather than an exception that stops the engine.
        self._tracing = bool(trace)
        self._ca_file = str(ca_file or "")
        self._ap_context = ap_context
        self._context: ssl.SSLContext | None = None
        self._connection: http.client.HTTPConnection | None = None

    def _tls_context(self) -> ssl.SSLContext:
        if self._context is None:
            context = ssl.create_default_context()
            if self._ca_file:
                context.load_verify_locations(cafile=self._ca_file)
            self._context = context
        return self._context

    # -- wire --------------------------------------------------------------

    def _request_frame(self, slot_id: int, pcm_bytes: int) -> dict[str, object]:
        changes: dict[str, object] = {
            "path": DecodePath.IMPROVED,
            "threads": self.threads,
            "cycles": self.cycles,
        }
        if self._ap_context is not None:
            # The host runs the same native decoder, so the same a-priori helps
            # there too -- and it costs nothing extra on the wire: the config is
            # already sent in full with every request.
            changes.update(self._ap_context())
        config = self._config.replace(**changes)
        return {
            "v": PROTOCOL_VERSION,
            "type": "decode_remote",
            "generation": 0,
            "request_id": 0,
            "slot_id": slot_id,
            "deadline_monotonic": self._clock() + self.deadline_s,
            "config": decode_config_frame(config),
            "pcm_bytes": pcm_bytes,
        }

    def _post(self, slot_id: int, samples: bytes) -> dict[str, object]:
        # Timing marks for the one question that needs them: the box's own
        # latency budget.  Off by default — a switch, not a habit — because the
        # interesting number (how much of a 15 s slot the host's answer costs)
        # can only be read from inside the live process.
        marks: dict[str, float] = {}
        started = time.monotonic()
        frame = self._request_frame(slot_id, len(samples))
        header = base64.b64encode(
            json.dumps(
                frame, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).decode("ascii")
        timeout = self.deadline_s + _SOCKET_SLACK_S
        try:
            if self._connection is None:
                factory = (
                    http.client.HTTPSConnection if self._tls
                    else http.client.HTTPConnection
                )
                self._connection = factory(
                    self._host, self._port, timeout=timeout,
                    **({"context": self._tls_context()} if self._tls else {}),
                )
                marks["connect"] = time.monotonic()
            connection = self._connection
            connection.timeout = timeout
            connection.request(
                "POST",
                self._path,
                body=samples,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "X-MRRC-Config": header,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(samples)),
                    "Connection": "keep-alive",
                },
            )
            marks["sent"] = time.monotonic()
            response = connection.getresponse()
            marks["first_byte"] = time.monotonic()
            payload = response.read()
            marks["received"] = time.monotonic()
            status = response.status
        except Exception as error:  # noqa: BLE001 — every failure means "use the box"
            self._drop()  # never reuse a half-dead socket
            raise CloudUnavailable(f"cloud decode request failed: {error}") from error
        if status != 200:
            # 5xx says the service is unhappy and 503 says it is busy; 4xx says
            # the request was wrong (token, schema).  Retrying helps none of
            # them, and the box has a local fallback.
            self._drop()
            raise CloudUnavailable(f"cloud decode returned HTTP {status}")
        # The same strict schema as the worker's reply.  A body we cannot trust
        # is not a protocol error to propagate — it means "use the box", exactly
        # like a refused connection would.
        try:
            framed = decode_frame(payload)
        except FrameError as error:
            raise CloudUnavailable(f"cloud decode returned an invalid frame: {error}") from error
        if framed.get("type") != "decode_ok":
            raise CloudUnavailable(f"cloud decode returned {framed.get('type')!r}")
        self._trace(slot_id, framed, started, marks)
        return framed

    def _trace(
        self, slot_id: int, framed: dict[str, object], started: float, marks: dict[str, float]
    ) -> None:
        """Report the timing marks — and never be able to break the decode.

        The box fell back to its local path for an afternoon because the first
        version of this called an undefined logger: a NameError here, a silent
        ``cloud_unavailable`` there, and nothing in the logs to say why
        (2026-10-15).  A diagnostic that can fail what it measures gets retired
        on the spot.
        """

        if not self._tracing:
            return
        try:
            self._trace_log(slot_id, framed, started, marks)
        except Exception:  # noqa: BLE001 - see above
            self._tracing = False

    def _trace_log(
        self, slot_id: int, framed: dict[str, object], started: float, marks: dict[str, float]
    ) -> None:
        sent = marks.get("sent", started)
        first = marks.get("first_byte", sent)
        received = marks.get("received", first)
        log.info(
            "cloud trace slot %s: connect %.0f ms, send %.0f ms, "
            "wait %.0f ms (host decode %.0f ms), receive %.0f ms, total %.0f ms",
            slot_id,
            (marks.get("connect", sent) - started) * 1000.0,
            (sent - marks.get("connect", sent)) * 1000.0,
            (first - sent) * 1000.0,
            float(framed.get("elapsed_seconds") or 0.0) * 1000.0,
            (received - first) * 1000.0,
            (received - started) * 1000.0,
        )

    def _drop(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass

    # -- provider protocol -------------------------------------------------

    async def decode_path(
        self, slot_id: int, samples: bytes, path: DecodePath, *, threads: int | None = None
    ) -> DecodeBatch:
        """Decode one slot on the hub; ``path``/``threads`` belong to the box."""

        if len(samples) != SLOT_SAMPLES_NBYTES:
            raise ValueError(f"decode requires exactly {SLOT_SAMPLES_NBYTES} bytes")
        response = await asyncio.to_thread(self._post, slot_id, samples)
        return batch_from_decode_ok(response)

    def close(self) -> None:
        self._drop()


def cloud_decoder_from_environ(
    environ, ap_context: "ApContextProvider | None" = None
) -> CloudDecoder | None:
    """Build a decoder from ``MRRC_FT8_CLOUD_DECODE_*``; ``None`` when disabled.

    An empty or absent URL means the feature does not exist — which is the
    shipped default and the state every offline box stays in.
    """

    url = (environ.get("MRRC_FT8_CLOUD_DECODE_URL") or "").strip()
    if not url:
        return None
    token = (environ.get("MRRC_FT8_CLOUD_DECODE_TOKEN") or "").strip()
    if not token:
        raise ValueError("MRRC_FT8_CLOUD_DECODE_URL is set but the token is not")
    cycles = int(environ.get("MRRC_FT8_CLOUD_DECODE_CYCLES") or DEFAULT_CYCLES)
    deadline = float(
        environ.get("MRRC_FT8_CLOUD_DECODE_DEADLINE_S") or DEFAULT_DEADLINE_S
    )
    ca_file = (environ.get("MRRC_FT8_CLOUD_DECODE_CA") or "").strip()
    threads = int(environ.get("MRRC_FT8_CLOUD_DECODE_THREADS") or CLOUD_THREADS)
    trace = (environ.get("MRRC_FT8_CLOUD_DECODE_TRACE") or "").strip() not in ("", "0", "false")
    return CloudDecoder(
        url, token, cycles=cycles, deadline_s=deadline, ca_file=ca_file,
        threads=threads, trace=trace, ap_context=ap_context,
    )
