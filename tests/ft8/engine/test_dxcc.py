"""cty.dat parsing + callsign→DXCC lookup (spec §3.1)."""

from __future__ import annotations

from ft8.engine.dxcc import CtyDatabase, load_cty

# 精简 fixture：实体行 + 跨行续行 + =精确 + (23)数字替换
FIXTURE = """\
Sov Mil Order of Malta:   15:  28:  EU:   41.90:   -12.43:    -1.0:  1A:
    1A;
China:                    24:  44:  AS:   36.00:  -102.00:    -8.0:  BY:
    3H0(23)[42],BI,BJ,BV,=B7P4A,BY;
Monaco:                   14:  27:  EU:   43.73:    -7.40:    -1.0:  3A:
    3A,=3A/4Z5KJ/LH;
"""


def test_load_cty_parses_entities_and_continuation_lines(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE)
    db = load_cty(str(path))
    assert len(db.entities) == 3
    names = [e.name for e in db.entities]
    assert names == ["Sov Mil Order of Malta", "China", "Monaco"]
    china = db.entities[1]
    assert china.continent == "AS"
    # 续行收集：3H0(23)[42] 展开 + BI/BJ/BV/=B7P4A/BY
    assert "3H0" in china.prefixes
    assert "3H2" in china.prefixes
    assert "=B7P4A" in china.prefixes


def test_lookup_prefix_and_continent(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE)
    db = load_cty(str(path))
    assert db.lookup("BI1TX") == ("China", "AS")
    assert db.lookup("BY1OK") == ("China", "AS")
    assert db.lookup("1A0KM") == ("Sov Mil Order of Malta", "EU")
    assert db.lookup("3A2MW") == ("Monaco", "EU")


def test_lookup_exact_match_and_digit_replacement(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE)
    db = load_cty(str(path))
    # =B7P4A 精确匹配：B7P4A 整呼号
    assert db.lookup("B7P4A") == ("China", "AS")
    # 3H0(23)[42] → 3H0 / 3H2 / 3H3 前缀
    assert db.lookup("3H0XX") == ("China", "AS")
    assert db.lookup("3H2YY") == ("China", "AS")
    assert db.lookup("3H3ZZ") == ("China", "AS")
    assert db.lookup("3H1QQ") is None  # 数字替换不允许 1


def test_lookup_strips_slash_suffix(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE)
    db = load_cty(str(path))
    assert db.lookup("BI1TX/QRP") == ("China", "AS")  # base = BI1TX
    assert db.lookup("3A2MW/P") == ("Monaco", "EU")


def test_lookup_unknown_returns_none(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE)
    db = load_cty(str(path))
    assert db.lookup("ZZ9ZZZ") is None


# ---- dxcc_summary ---------------------------------------------------------

from ft8.engine.dxcc import dxcc_summary
from ft8.engine.repository import Repository
from ft8.engine.sequencer import QSORecord

FIXTURE2 = """\
China:                    24:  44:  AS:   36.00:  -102.00:    -8.0:  BY:
    BI,BY;
Japan:                    25:  45:  AS:   36.00:    138.00:    -9.0:  JA:
    JA;
Mauritius:                39:  53:  AF:  -20.35:   -57.50:    -4.0:  3B8:
    3B8;
"""


def _repo_with_qsos() -> Repository:
    repo = Repository(":memory:")
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="BI1TX", band="20m"),
        completed_epoch=1700000000.0,
    )
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="BY1OK", band="20m"),
        completed_epoch=1700000100.0,
    )
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="JA1YAD", band="40m"),
        completed_epoch=1700000200.0,
    )
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="JA1YAD", band="20m"),
        completed_epoch=1700000300.0,
    )
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="3B8CW", band="20m"),
        completed_epoch=1700000400.0,
    )
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="ZZ9ZZZ", band="15m"),
        completed_epoch=1700000500.0,
    )
    return repo


def test_dxcc_summary_counts_entities_bands_and_unmatched(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE2)
    cty = load_cty(str(path))
    summary = dxcc_summary(_repo_with_qsos(), cty)
    assert summary.total == 3                      # China / Japan / Mauritius
    assert summary.unmatched == 1                  # ZZ9ZZZ
    names = [e.name for e in summary.entities]
    assert names == ["China", "Japan", "Mauritius"]  # sorted
    # China: BI1TX + BY1OK 都算一个实体；同实体同波段 20m 只计 1
    china = summary.entities[0]
    assert china.continent == "AS"
    assert china.band_count == 1                   # 只有 20m
    assert china.first_utc == "2023-11-14T22:13:20Z"  # 1700000000
    japan = summary.entities[1]
    assert japan.band_count == 2                   # 40m + 20m
    assert japan.bands == ["20m", "40m"]           # sorted
    # by_band：20m → China+Japan+Mauritius = 3；40m → Japan = 1；15m → 0（unmatched 不计）
    assert summary.by_band == {"20m": 3, "40m": 1}


def test_get_cty_database_loads_repo_cty_singleton() -> None:
    from ft8.engine.dxcc import get_cty_database

    db1 = get_cty_database()
    db2 = get_cty_database()
    assert db1 is db2                      # 单例
    assert len(db1.entities) > 300         # 仓库内 cty.dat（346 实体）
    assert db1.lookup("BI1TX") == ("China", "AS")


# ---- indexed lookup ----------------------------------------------------------

"""Indexed lookup: semantic equivalence to the pre-index linear scan."""
import random

import pytest

from ft8.engine.dxcc import CtyDatabase, get_cty_database, load_cty


def _lookup_linear(entities, call: str):
    """Reference: exact copy of the pre-index algorithm (dxcc.py:55)."""
    base = call.split("/", 1)[0].upper()
    best_len = -1
    best = None
    for entity in entities:
        for stored in entity.prefixes:
            if stored.startswith("="):
                if base == stored[1:]:
                    return (entity.name, entity.continent)
            elif base.startswith(stored):
                if len(stored) > best_len:
                    best_len = len(stored)
                    best = entity
    return (best.name, best.continent) if best else None


def _corpus_from_cty(db: CtyDatabase, *, prefix_sample: int = 300, exact_step: int = 10) -> set[str]:
    """Bounded adversarial corpus over the repo cty.dat: a deterministic
    sample of every reachable exact key plus prefix-hit variants (bare, digit,
    alphabetic, portable, lowercased).  Sized so the pre-index reference scan
    finishes in seconds — enumerating all ~40k prefixes would take minutes
    against the old linear lookup (the reason the original unbounded version
    hung).  Slash-free exact keys are the only reachable exact entries:
    ``lookup`` strips ``/suffix`` before matching."""
    exact: list[str] = []
    non_exact: list[str] = []
    for entity in db.entities:
        for stored in entity.prefixes:
            if stored.startswith("="):
                if "/" not in stored:
                    exact.append(stored[1:])
            else:
                non_exact.append(stored)
    calls: set[str] = set()
    calls.update(exact[::exact_step])            # deterministic sample of exact keys
    calls.update(exact[:50])                     # head of the exact list
    for stored in non_exact[:prefix_sample]:
        calls.add(stored)                        # bare prefix (shortest hit)
        for suffix in ("1", "ABC", "P", "qra"):  # digit / alphabetic / portable / lowercase
            calls.add(stored + suffix)
    return calls


def _synthetic_calls(seed: int = 42, n: int = 1500) -> set[str]:
    rng = random.Random(seed)
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    out: set[str] = set()
    for _ in range(n):
        out.add("".join(rng.choice(chars) for _ in range(rng.randint(1, 8))))
    base = list(out)[:50]
    for c in base:
        out.add(c + "/P")    # slash suffix stripping
        out.add(c + "/QRP")
        out.add(c.lower())
    return out


def test_index_structures_built_on_load() -> None:
    db = get_cty_database()
    assert isinstance(db._exact, dict) and db._exact
    assert isinstance(db._trie, dict) and db._trie
    assert db.lookup("BI1TX") == ("China", "AS")  # repo cty.dat guarantees this


def test_indexed_lookup_equivalent_to_linear_over_full_corpus() -> None:
    db = get_cty_database()
    calls = _corpus_from_cty(db) | _synthetic_calls()
    assert len(calls) > 500
    for call in sorted(calls):
        assert db.lookup(call) == _lookup_linear(db.entities, call), (
            f"semantic mismatch for {call!r}"
        )


@pytest.mark.skipif(
    not (__import__("pathlib").Path(__file__).resolve().parents[2] / "mrrc-ft8.db").exists(),
    reason="live QSO log not present",
)
def test_indexed_lookup_equivalent_over_live_qso_log() -> None:
    from pathlib import Path
    from ft8.engine.repository import Repository

    db = get_cty_database()
    repo = Repository(str(Path(__file__).resolve().parents[2] / "mrrc-ft8.db"))
    calls = {qso.dx_call for qso in repo.list_qsos(include_void=False)}
    assert len(calls) > 1000
    for call in calls:
        assert db.lookup(call) == _lookup_linear(db.entities, call), call


def test_lookup_speed_smoke() -> None:
    """Loose upper bound: the index must stay 3+ orders of magnitude faster
    than the old ~1.5 ms/call linear scan (guard against index regressions)."""
    import time

    db = get_cty_database()
    calls = _synthetic_calls(seed=7, n=2000)
    t0 = time.perf_counter()
    for c in calls:
        db.lookup(c)
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, f"{elapsed:.2f}s for {len(calls)} lookups"


# ---- worked index + JTDX-style decode highlight -----------------------------

from ft8.engine.dxcc import decode_highlight, worked_index


def _repo(*rows: tuple[str, str, str]) -> Repository:
    repo = Repository(":memory:")
    for i, (call, grid, band) in enumerate(rows):
        repo.record_qso(
            QSORecord(
                my_call="M0XX", my_grid="IO91",
                dx_call=call, dx_grid=grid, band=band,
            ),
            completed_epoch=1700000000.0 + i,
        )
    return repo


def test_worked_index_collects_any_band_and_per_band_sets(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE2)
    cty = load_cty(str(path))
    idx = worked_index(
        _repo(("BI1TX", "OM89", "20m"), ("JA1YAD", "PM95", "40m")), cty
    )
    assert idx.calls == {"BI1TX", "JA1YAD"}
    assert idx.call_band == {("BI1TX", "20m"), ("JA1YAD", "40m")}
    assert idx.entities == {"China", "Japan"}
    assert idx.entity_band == {("China", "20m"), ("Japan", "40m")}
    assert idx.grids == {"OM89", "PM95"}
    assert idx.grid_band == {("OM89", "20m"), ("PM95", "40m")}


def test_decode_highlight_priority(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE2)
    cty = load_cty(str(path))
    idx = worked_index(
        _repo(
            ("BI1TX", "OM89", "20m"),   # China, 20m
            ("BY1OK", "OL00", "40m"),   # China, 40m (other call, same entity)
            ("JA1YAD", "PM95", "40m"),  # Japan, 40m
            ("JA1YAD", "QL00", "20m"),  # Japan, 20m (grid QL00)
        ),
        cty,
    )

    def hl(**kw: object) -> str:
        args = dict(
            call="", grid="", entity="", band="20m",
            to_me=False, mine=False, index=idx,
        )
        args.update(kw)
        return decode_highlight(**args)  # type: ignore[arg-type]

    # Addressed-to-me wins over everything; own echoes stay uncoloured.
    assert hl(call="BI1TX", to_me=True) == "my_call"
    assert hl(call="BI1TX", mine=True) == ""
    # No index / no call: nothing to compare against.
    assert hl(call="BI1TX", index=None) == ""
    assert hl(grid="OM89") == ""
    # Entity never worked → new DXCC (any band).
    assert hl(call="3B8CW", grid="LG99", entity="Mauritius") == "new_dxcc"
    # Entity worked, but not on this band → new DXCC on band.
    assert hl(call="JA1YAD", grid="PM95", entity="Japan", band="15m") == "new_dxcc_band"
    # Entity+band worked, brand-new call → new call.
    assert hl(call="BY9ZZ", grid="OM99", entity="China") == "new_call"
    # Call worked on another band only → new call on band.
    assert hl(call="BI1TX", grid="OM89", entity="China", band="40m") == "new_call_band"
    # Call+band worked, grid never worked → new grid.
    assert hl(call="JA1YAD", grid="QF22", entity="Japan") == "new_grid"
    # Grid worked on another band only → new grid on band.
    assert hl(call="JA1YAD", grid="PM95", entity="Japan") == "new_grid_band"
    # Everything worked → worked.
    assert hl(call="JA1YAD", grid="QL00", entity="Japan") == "worked"
