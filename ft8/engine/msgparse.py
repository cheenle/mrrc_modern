"""Standard FT8/FT4 message parsing (37-char decode text → structured fields).

Mirrors the token logic of wsjtx-3.0.2 ``Decoder/decodedtext.cpp``, keeping
only the minimal set needed for a standard HF QSO (SDD AD-002, UC-004/005).
Anything outside the supported shapes is reported as non-standard free text
and never reaches the sequencer state transitions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Maidenhead 4-character locator, e.g. FN42.
_GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}$", re.IGNORECASE)
# Signal report: -08 / +03 / R-10 / R+05.
_REPORT_RE = re.compile(r"^R?[+-]?\d{1,2}$")
# Lenient standard callsign: 1-3 alnum prefix + digit + 1-4 alpha suffix,
# optionally with a /P /M-style suffix.
_CALL_RE = re.compile(r"^[A-Z0-9]{1,3}[0-9][A-Z]{1,4}(/[A-Z0-9]{1,4})?$")
# Directed-CQ target between "CQ" and the calling station, mirroring the
# wsjtx-3.0.2 ``DecodedText::CQersCall`` optional group
# (``Decoder/decodedtext.cpp:110``): continents and activity tags are pure
# alpha (NA/SA/EU/AF/AS/OC, DX, FD, POTA…), contest zones three digits.
# Standard callsigns always carry a digit, so the two sets never overlap.
_CQ_DIRECTIVE_RE = re.compile(r"^(?:[A-Z]{1,4}|\d{3})$")

EOM = {"73", "RR73", "RRR"}


@dataclass
class ParsedMessage:
    """Structured view of one decoded message."""

    text: str
    is_cq: bool = False
    cq_directive: str = ""   # directed-CQ target ("OC", "DX", "POTA", …), if any
    to_call: str = ""        # destination callsign (first token pair member)
    from_call: str = ""      # originating callsign
    grid: str = ""
    report_db: int | None = None
    has_roger: bool = False  # R-nn report or RR73/RRR
    is_eom: bool = False     # 73 / RR73 / RRR
    eom: str = ""            # the exact end-of-message token, if any
    is_standard: bool = True

    @property
    def is_free_text(self) -> bool:
        return not self.is_standard


def _is_report(tok: str) -> bool:
    if not _REPORT_RE.match(tok):
        return False
    return tok.lstrip("R").lstrip("+-").isdigit()


def parse_message(text: str) -> ParsedMessage:
    """Parse one 37-character message text.

    Supported standard shapes::

        CQ [DIRECTIVE] CALL [GRID]   (DIRECTIVE = DX/FD/continent/tag/zone)
        CQDX CALL [GRID]             (glued DX form)
        CALL1 CALL2 [GRID]         (call/answer)
        CALL1 CALL2 -08            (signal report)
        CALL1 CALL2 R-10           (roger + report)
        CALL1 CALL2 RR73 | RRR | 73

    Everything else is returned as non-standard free text.
    """

    clean = " ".join(text.split())
    tokens = clean.split(" ")
    msg = ParsedMessage(text=clean)

    if not tokens or not tokens[0]:
        msg.is_standard = False
        return msg

    if tokens[0] == "CQ" or tokens[0] == "CQDX":
        msg.is_cq = True
        idx = 1
        if tokens[0] == "CQDX":
            msg.cq_directive = "DX"
        elif len(tokens) > 1 and _CQ_DIRECTIVE_RE.match(tokens[1]):
            # "CQ OC LZ936BA BG15B": OC addresses Oceania, LZ936BA is the
            # calling station — never let the directive become the call.
            msg.cq_directive = tokens[1].upper()
            idx = 2
        if len(tokens) > idx:
            msg.from_call = tokens[idx]
        if len(tokens) > idx + 1 and _GRID_RE.match(tokens[idx + 1]):
            msg.grid = tokens[idx + 1].upper()
        if not msg.from_call or not _CALL_RE.match(msg.from_call):
            msg.is_standard = False
        return msg

    if len(tokens) >= 2:
        to_call, from_call = tokens[0], tokens[1]
        # Hash callsigns <...> are not handled by the standard subset.
        if to_call.startswith("<") or from_call.startswith("<"):
            msg.is_standard = False
            msg.to_call, msg.from_call = to_call, from_call
            return msg
        if not (_CALL_RE.match(to_call) and _CALL_RE.match(from_call)):
            msg.is_standard = False
            return msg
        msg.to_call, msg.from_call = to_call, from_call
        tail = tokens[2:]
    else:
        msg.is_standard = False
        return msg

    for tok in tail:
        # Order matters: "73" satisfies the report regex and "RR73" the grid
        # regex, so end-of-message tokens must be matched first.
        if tok in EOM:
            msg.is_eom = True
            msg.eom = tok
            if tok in {"RR73", "RRR"}:
                msg.has_roger = True
        elif _GRID_RE.match(tok):
            msg.grid = tok.upper()
        elif _is_report(tok):
            msg.has_roger = tok.startswith("R")
            try:
                msg.report_db = int(tok.lstrip("R"))
            except ValueError:
                pass

    return msg


def base_call(call: str) -> str:
    """Strip /P-style suffixes and <> wrapping for callsign comparison."""

    return call.strip("<>").split("/")[0].upper()


def addressed_to(msg: ParsedMessage, my_call: str) -> bool:
    """Whether the message is addressed to my_call.

    CQ calls address everyone and return False here; callers special-case
    them (candidate selection never feeds CQ through the QSO transitions).
    """

    if not my_call or msg.is_cq or not msg.to_call:
        return False
    return base_call(msg.to_call) == base_call(my_call)
