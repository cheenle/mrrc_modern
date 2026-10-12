from __future__ import annotations

import pytest

from ft8.engine.msgparse import (
    addressed_to,
    base_call,
    parse_message,
)


def test_cq_with_grid() -> None:
    msg = parse_message("CQ K1ABC FN42")
    assert msg.is_cq and msg.is_standard
    assert msg.from_call == "K1ABC"
    assert msg.grid == "FN42"
    assert msg.to_call == ""
    assert msg.report_db is None
    assert not msg.is_eom


@pytest.mark.parametrize("modifier", ["CQ DX JA1AAA PM95", "CQ FD W9XYZ EM57"])
def test_cq_with_modifier(modifier: str) -> None:
    msg = parse_message(modifier)
    assert msg.is_cq and msg.is_standard
    assert msg.from_call == modifier.split()[2]
    assert msg.grid == modifier.split()[3]


@pytest.mark.parametrize(
    ("text", "directive", "call", "grid"),
    [
        # Continent-directed CQ (WSJT-X CQersCall semantics): the directive
        # addresses a region, the next token is the calling station — a reply
        # must never key on the directive ("OC" was answered as a callsign).
        ("CQ OC VK3ACF QF22", "OC", "VK3ACF", "QF22"),
        ("CQ NA K1ABC FN42", "NA", "K1ABC", "FN42"),
        ("CQ EU DL1ABC JO31", "EU", "DL1ABC", "JO31"),
        ("CQ AS JA1ABC PM95", "AS", "JA1ABC", "PM95"),
        ("CQ AF ZS6AAA KG33", "AF", "ZS6AAA", "KG33"),
        ("CQ SA PY2ABC GG87", "SA", "PY2ABC", "GG87"),
        # Activity tags and contest zones use the same slot.
        ("CQ POTA W1AW FN31", "POTA", "W1AW", "FN31"),
        ("CQ 001 W1AW FN31", "001", "W1AW", "FN31"),
        # Glued DX form.
        ("CQDX VK3ACF QF22", "DX", "VK3ACF", "QF22"),
    ],
)
def test_directed_cq_keeps_real_callsign(
    text: str, directive: str, call: str, grid: str
) -> None:
    msg = parse_message(text)
    assert msg.is_cq and msg.is_standard
    assert msg.cq_directive == directive
    assert msg.from_call == call
    assert msg.grid == grid


@pytest.mark.parametrize(
    "text",
    [
        "CQ 2E0AAA IO91",  # digit-bearing token is the callsign, not a directive
        "CQ OC",           # directive without a calling station
        "CQ OC VK3ACF",    # directed CQ without grid stays standard
    ],
)
def test_directed_cq_edges(text: str) -> None:
    msg = parse_message(text)
    if text == "CQ OC":
        assert msg.is_cq and not msg.is_standard
        assert msg.cq_directive == "OC" and msg.from_call == ""
    else:
        assert msg.is_cq and msg.is_standard
        assert msg.from_call == ("2E0AAA" if "2E0AAA" in text else "VK3ACF")


def test_cq_without_grid() -> None:
    msg = parse_message("CQ K1ABC")
    assert msg.is_cq and msg.is_standard
    assert msg.from_call == "K1ABC"
    assert msg.grid == ""


@pytest.mark.parametrize("text", ["CQ TEST", "CQ ABC", "CQ 12345 FN42"])
def test_cq_with_invalid_callsign_is_nonstandard(text: str) -> None:
    msg = parse_message(text)
    assert msg.is_cq
    assert not msg.is_standard
    assert msg.is_free_text


def test_directed_grid_message() -> None:
    msg = parse_message("N0CALL K1ABC FN42")
    assert msg.is_standard and not msg.is_cq
    assert msg.to_call == "N0CALL"
    assert msg.from_call == "K1ABC"
    assert msg.grid == "FN42"


@pytest.mark.parametrize(
    ("text", "report", "roger"),
    [
        ("N0CALL K1ABC -08", -8, False),
        ("N0CALL K1ABC +03", 3, False),
        ("N0CALL K1ABC R-10", -10, True),
        ("N0CALL K1ABC R+05", 5, True),
    ],
)
def test_report_messages(text: str, report: int, roger: bool) -> None:
    msg = parse_message(text)
    assert msg.is_standard
    assert msg.report_db == report
    assert msg.has_roger is roger
    assert not msg.is_eom


@pytest.mark.parametrize(
    ("token", "roger"),
    [("73", False), ("RR73", True), ("RRR", True)],
)
def test_end_of_message_tokens(token: str, roger: bool) -> None:
    msg = parse_message(f"N0CALL K1ABC {token}")
    assert msg.is_standard
    assert msg.is_eom
    assert msg.eom == token
    assert msg.has_roger is roger
    assert msg.report_db is None


def test_hash_callsign_is_nonstandard_but_identified() -> None:
    msg = parse_message("<W9XYZ> K1ABC FN42")
    assert not msg.is_standard
    assert msg.to_call == "<W9XYZ>"
    assert msg.from_call == "K1ABC"


@pytest.mark.parametrize("text", ["", "HELLO", "TNX BOB 73", "N0CALL"])
def test_free_text_shapes(text: str) -> None:
    msg = parse_message(text)
    assert not msg.is_standard
    assert msg.is_free_text


def test_whitespace_is_normalized() -> None:
    msg = parse_message("  CQ   K1ABC   FN42  ")
    assert msg.text == "CQ K1ABC FN42"
    assert msg.is_cq and msg.from_call == "K1ABC" and msg.grid == "FN42"


@pytest.mark.parametrize(
    "token",
    ["R", "+", "RR", "-123", "+5d", "R++5"],
)
def test_malformed_report_tokens_are_ignored(token: str) -> None:
    msg = parse_message(f"N0CALL K1ABC {token}")
    assert msg.is_standard
    assert msg.report_db is None
    assert not msg.is_eom


@pytest.mark.parametrize(
    ("call", "base"),
    [
        ("K1ABC", "K1ABC"),
        ("K1ABC/P", "K1ABC"),
        ("KH1/KH7Z", "KH1"),
        ("<W9XYZ>", "W9XYZ"),
        ("dl1bbb", "DL1BBB"),
    ],
)
def test_base_call(call: str, base: str) -> None:
    assert base_call(call) == base


def test_addressed_to_matches_base_callsign() -> None:
    msg = parse_message("N0CALL/P K1ABC FN42")
    assert addressed_to(msg, "N0CALL")
    assert not addressed_to(msg, "W9XYZ")
    assert not addressed_to(msg, "")


def test_addressed_to_rejects_cq_and_empty_target() -> None:
    assert not addressed_to(parse_message("CQ K1ABC FN42"), "N0CALL")
    assert not addressed_to(parse_message("CQ K1ABC"), "N0CALL")
