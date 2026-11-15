"""SmartSDR text codec: ``C``/``R``/``S`` lines, object names, ``info``.

Pure stdlib, no I/O — the whole text protocol is exercised from strings
(``tests/test_flex_protocol.py``).  Facts: ``docs/flexradio-smartsdr-analysis.md`` §2-§3.

Three things this module exists to get right, each of which silently breaks a
naive parser:

1. **Object names are multi-word.**  ``S<h>|slice 0 RF_frequency=14.074 mode=USB``
   and ``S<h>|interlock band 9 state=READY`` split at the **last space before
   the first ``=``**, not at the first space.
2. **``meter`` is the exception**: its key/value separator is ``#``, and every
   token is prefixed with the meter id (``7.src=SLC#7.num=0#7.nam=LEVEL``).
3. **The frequency key is ``RF_frequency``**, and AF gain is ``audio_level`` —
   ``freq`` / ``audio_gain`` do not exist (analysis §2.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

__all__ = [
    "ProtocolError", "ERROR_OK", "ERROR_UNKNOWN_COMMAND", "ERROR_LICENSE",
    "ERROR_BAD_FIELD", "ERROR_OUT_OF_RANGE", "ERROR_PARAM_COUNT",
    "ERROR_UNKNOWN_MODE", "ERROR_UDPPORT_QUIRK", "error_name",
    "Response", "Status", "build_command", "parse_response", "parse_status",
    "split_object", "parse_kv", "unquote", "parse_hello",
    "MeterDefinition", "parse_meter_definitions", "scale_meter", "meter_text",
    "parse_info", "parse_slice_list", "parse_discovery_payload",
]

# ── response codes (analysis §2.3) ───────────────────────────────────
ERROR_OK = 0x0
ERROR_LICENSE = 0x50000003
ERROR_UNKNOWN_COMMAND = 0x50000016
ERROR_PARAM_COUNT = 0x5000002C
ERROR_UNKNOWN_MODE = 0x50000032
ERROR_OUT_OF_RANGE = 0x50000033
ERROR_BAD_FIELD = 0x5000002D
#: ``client udpport`` answers this on v1.4.0.0 even though the port was
#: registered by the one-byte UDP packet; it is expected, not a failure.
ERROR_UDPPORT_QUIRK = 0x50001000

_ERROR_NAMES = {
    ERROR_OK: "ok",
    ERROR_LICENSE: "license check failed",
    ERROR_UNKNOWN_COMMAND: "unknown/malformed command",
    ERROR_PARAM_COUNT: "wrong parameter count",
    ERROR_UNKNOWN_MODE: "unrecognised mode",
    ERROR_OUT_OF_RANGE: "parameter out of range",
    ERROR_BAD_FIELD: "bad field",
    ERROR_UDPPORT_QUIRK: "udpport quirk (expected)",
}


def error_name(code: int) -> str:
    """Human name for a response code; unknown codes print as hex."""
    return _ERROR_NAMES.get(code, f"0x{code:08X}")


class ProtocolError(ValueError):
    """A line that cannot be parsed as a SmartSDR message."""


# ── commands and responses ───────────────────────────────────────────

def build_command(seq: int, command: str) -> bytes:
    """``C<seq>|<command>`` terminated the way the radio expects (LF)."""
    text = str(command).strip()
    if not text:
        raise ProtocolError("refusing to send an empty command")
    if "\n" in text or "\r" in text:
        raise ProtocolError("command must not contain line terminators")
    try:
        sequence = int(seq)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"bad command sequence {seq!r}") from exc
    return f"C{sequence}|{text}\n".encode("ascii")


@dataclass(frozen=True)
class Response:
    """``R<seq>|<hex code>|<message>[|<debug>]``."""

    seq: int
    code: int
    body: str = ""
    debug: str = ""

    @property
    def ok(self) -> bool:
        return self.code == ERROR_OK


def parse_response(line: str) -> Response:
    """Parse one ``R`` line.  Raises ``ProtocolError`` on anything else."""
    if not line.startswith("R"):
        raise ProtocolError(f"not a response line: {line!r}")
    head, _, rest = line.partition("|")
    try:
        seq = int(head[1:])
    except ValueError as exc:
        raise ProtocolError(f"bad response sequence in {head!r}") from exc
    parts = rest.split("|")
    if len(parts) < 2:
        raise ProtocolError(f"response without a code field: {line!r}")
    code_text, body = parts[0].strip(), parts[1]
    try:
        code = int(code_text, 16)
    except ValueError as exc:
        raise ProtocolError(f"bad response code {code_text!r}") from exc
    debug = "|".join(parts[2:]) if len(parts) > 2 else ""
    return Response(seq=seq, code=code, body=body, debug=debug)


# ── status lines ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Status:
    """``S<handle>|<object name> <key=value …>``."""

    handle: int
    obj: str
    kv: Mapping[str, str]

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self.kv.get(key, default)

    def number(self, key: str) -> Optional[float]:
        """Float value of ``key``, or None when absent/unparseable."""
        raw = self.kv.get(key)
        if raw is None:
            return None
        try:
            return float(raw)
        except Exception:  # noqa: BLE001 - any failure means "field absent"
            return None


def split_object(text: str) -> Tuple[str, str]:
    """Split ``"slice 0 RF_frequency=1"`` into ``("slice 0", "RF_frequency=1")``.

    The boundary is the **last space before the first ``=``** — object names are
    multi-word, values are not.
    """
    body = text.strip()
    first_eq = body.find("=")
    if first_eq < 0:
        return body, ""
    boundary = body.rfind(" ", 0, first_eq)
    if boundary < 0:
        return "", body
    return body[:boundary].strip(), body[boundary + 1:].strip()


def unquote(value: str) -> str:
    """Strip Flex's quoting/escaping from a status value."""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return text.replace("\\", "")


def parse_kv(text: str) -> Dict[str, str]:
    """Space-separated ``k=v`` pairs → dict (unquoted, blanks skipped)."""
    out: Dict[str, str] = {}
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            continue
        out[key] = unquote(value)
    return out


def parse_status(line: str) -> Status:
    """Parse one ``S`` line.  Raises ``ProtocolError`` on anything else."""
    if not line.startswith("S"):
        raise ProtocolError(f"not a status line: {line!r}")
    head, sep, rest = line.partition("|")
    if not sep:
        raise ProtocolError(f"status without a body: {line!r}")
    handle_text = head[1:].strip()
    try:
        handle = int(handle_text, 16) if handle_text else 0
    except ValueError as exc:
        raise ProtocolError(f"bad status handle {handle_text!r}") from exc
    obj, kv_text = split_object(rest)
    if not obj:
        raise ProtocolError(f"status without an object name: {line!r}")
    if obj == "meter":
        return Status(handle=handle, obj=obj, kv={"_raw": kv_text})
    return Status(handle=handle, obj=obj, kv=parse_kv(kv_text))


def parse_hello(line: str) -> Tuple[str, str]:
    """Classify a connection prologue line: ``("version", "1.4.0.0")`` or
    ``("handle", "6F4EC23D")``."""
    text = line.strip()
    if not text:
        raise ProtocolError("empty prologue line")
    if text[0] == "V":
        return "version", text[1:].strip()
    if text[0] == "H":
        return "handle", text[1:].strip()
    raise ProtocolError(f"not a prologue line: {line!r}")


# ── meters ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MeterDefinition:
    meter_id: int
    src: str = ""
    num: int = 0
    name: str = ""
    unit: str = ""
    low: float = 0.0
    hi: float = 0.0
    fps: int = 0
    desc: str = ""


def meter_text(status: Status) -> str:
    """The raw ``id.k=v#…`` body of a ``meter`` status line."""
    return status.kv.get("_raw", "")


def parse_meter_definitions(text: str) -> Dict[int, MeterDefinition]:
    """Parse ``7.src=SLC#7.num=0#7.nam=LEVEL#7.unit=dBm#…`` → definitions.

    Tokens whose id or value is unusable are skipped rather than raised on: a
    meter table is diagnostic data and must never stop a session.
    """
    fields: Dict[int, Dict[str, str]] = {}
    for token in text.split("#"):
        left, sep, value = token.partition("=")
        if not sep:
            continue
        ident, dot, key = left.partition(".")
        if not dot or not key:
            continue
        try:
            meter_id = int(ident.strip())
        except ValueError:
            continue
        fields.setdefault(meter_id, {})[key.strip()] = unquote(value)

    out: Dict[int, MeterDefinition] = {}
    for meter_id, kv in fields.items():
        def _num(key: str, default: float = 0.0) -> float:
            try:
                return float(kv.get(key, default))
            except ValueError:
                return default

        try:
            num = int(kv.get("num", 0))
        except ValueError:
            num = 0
        try:
            fps = int(float(kv.get("fps", 0)))
        except ValueError:
            fps = 0
        out[meter_id] = MeterDefinition(
            meter_id=meter_id,
            src=kv.get("src", ""),
            num=num,
            name=kv.get("nam", ""),
            unit=kv.get("unit", ""),
            low=_num("low"),
            hi=_num("hi"),
            fps=fps,
            desc=kv.get("desc", ""),
        )
    return out


#: Raw meter units and their divisors (analysis §5.5, from FlexLib Meter.cs):
#: dBm/dB/dBFS/SWR are 1/128, volts/amps 1/1024, temperatures 1/64.
_METER_DIVISORS = {
    "dbm": 128.0, "db": 128.0, "dbfs": 128.0, "swr": 128.0,
    "volts": 1024.0, "amps": 1024.0,
    "degf": 64.0, "degc": 64.0,
}


def scale_meter(raw: int, unit: str) -> float:
    """Convert a raw meter value to its engineering unit.

    Unknown units pass through unchanged (they are already integer counters),
    which is what a diagnostic reader wants to see rather than a made-up scale.
    """
    divisor = _METER_DIVISORS.get((unit or "").strip().lower())
    if divisor is None:
        return float(raw)
    return raw / divisor


# ── info / slice list / discovery ────────────────────────────────────

def parse_info(body: str) -> Dict[str, str]:
    """``info`` reply → dict.

    The reply is **comma**-separated ``key=value`` (``model=FLEX-6400,
    chassis_serial=…,software_ver=…``), values possibly wrapped in quotes with
    backslash escaping.  Unknown keys are kept: the radio adds fields over
    firmware versions and a settings page should not need a parser change.
    """
    out: Dict[str, str] = {}
    for pair in body.split(","):
        key, sep, value = pair.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        out[key] = unquote(value)
    return out


def parse_slice_list(body: str) -> List[int]:
    """``slice list`` reply (``"0 1 3 4"``) → ``[0, 1, 3, 4]``."""
    out: List[int] = []
    for token in body.split():
        try:
            out.append(int(token))
        except ValueError:
            continue
    return out


def parse_discovery_payload(text: str) -> Dict[str, str]:
    """Discovery broadcast text → dict.

    ``model=FLEX-6400 serial=… version=4.1.5 name=My_Radio callsign=BG7XXX
    ip=192.168.1.10 port=4992`` — a space-separated status-style string, with
    spaces inside ``name`` replaced by underscores (analysis §1).
    """
    out: Dict[str, str] = {}
    for token in text.replace("\x00", " ").split():
        key, sep, value = token.partition("=")
        if sep and key:
            out[key] = value
    name = out.get("name")
    if name:
        out["name"] = name.replace("_", " ")
    return out
