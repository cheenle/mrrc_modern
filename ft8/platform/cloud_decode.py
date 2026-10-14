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
import time
from urllib.parse import urlsplit

from ft8.core.models import CloudUnavailable, DecodeBatch, DecodeConfig, DecodePath
from ft8.core.protocol import PROTOCOL_VERSION, FrameError, decode_frame
from ft8.engine.dsp_decode import batch_from_decode_ok, decode_config_frame
from ft8.engine.orchestrator import SLOT_SAMPLES_NBYTES

# The hub is a 1-vCPU box: measured 2026-10-14, one deep pass at profile 3
# costs 0.476 s at threads=1 and 0.583 / 0.644 s at threads=2 / 3 — the box's
# thread count was chosen for four A53 cores and is the wrong number to send
# here.  This is the only config override the cloud path makes.
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
        clock=time.monotonic,
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
        self.deadline_s = float(deadline_s)
        self._connection: http.client.HTTPConnection | None = None

    # -- wire --------------------------------------------------------------

    def _request_frame(self, slot_id: int, pcm_bytes: int) -> dict[str, object]:
        config = self._config.replace(
            path=DecodePath.IMPROVED,
            threads=CLOUD_THREADS,
            cycles=self.cycles,
        )
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
                self._connection = factory(self._host, self._port, timeout=timeout)
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
            response = connection.getresponse()
            payload = response.read()
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
        return framed

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


def cloud_decoder_from_environ(environ) -> CloudDecoder | None:
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
    return CloudDecoder(url, token, cycles=cycles, deadline_s=deadline)
