"""Real-domain AP measurement: does feeding the a-priori help on *real* audio?

Every number in docs/ft8-dl-speculative-notes.md §4 so far came from synthetic
signals mixed into real noise beds.  §8 names that as the biggest gap: the
signal was placed by us, so its position, strength and text were all known.
This closes it with real QSOs between real stations, offline.

The method turns two properties of FT8 into ground truth:

1. **A station transmits on alternate slots** (even or odd parity, fixed for the
   QSO).  So once we have seen a partner decode in slot N, we know *which* slots
   they transmitted in even when the decoder missed them.  That gives a recall
   denominator no synthetic sweep can offer.
2. **A QSO's message sequence is short and repetitive.**  The same text recurs
   across slots.  So a decode that AP produced and AP-off missed is *corroborated*
   if the same text shows up in a neighbouring slot, and *unexplained* if it
   never appears anywhere else.  That is the precision question, answered from
   the data rather than from a hypothesis about what we transmitted.

The identity is chosen, not assumed: the recording station was listening, not
working anyone, so we adopt one of the real pairs in the corpus (``--my-call`` /
``--dx-call``) and feed the decoder exactly what that station would have known.

Modes::

    --survey                     list the mutual pairs worth testing
    --my-call X --dx-call Y      run the paired measurement for that pair

Nothing here touches a radio or the network: it reads dumped slots and calls the
same native decoder the product uses.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parents[2]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

import numpy as np

from ft8.core.binding import CoreBinding
from ft8.core.models import DecodeConfig, DecodePath
from ft8.core.worker import default_library_path
from ft8.engine.msgparse import parse_message

# The production deep config (Task 8d), so AP is measured against what the host
# actually runs rather than against a fast path that would flatter it.
BASE = dict(
    path=DecodePath.IMPROVED,
    profile=1,
    cycles=3,
    sensitivity=3,
    low_threshold=True,
    threads=4,
)


def slot_paths(corpus: Path, limit: int | None) -> list[tuple[int, Path]]:
    """Return ``(slot_id, path)`` in chronological order.

    The dumps are named ``slot_<slot_id>.raw`` and the ids are consecutive, so
    sorting by id *is* sorting by time -- which the parity argument depends on.
    """

    out: list[tuple[int, Path]] = []
    for path in corpus.glob("*.raw"):
        try:
            out.append((int(path.stem.split("_")[1]), path))
        except (IndexError, ValueError):
            continue
    out.sort()
    return out[:limit] if limit else out


def load_pcm(path: Path) -> np.ndarray:
    """Read one dumped slot as the int16 array ``decode`` requires."""

    raw = np.frombuffer(path.read_bytes(), dtype=np.int16)
    return raw.astype(np.int16)  # a writable copy; frombuffer is read-only


@dataclass(frozen=True, slots=True)
class Sighting:
    """One decoded message, with the fields the a-priori is built from."""

    slot_id: int
    text: str
    frequency: float
    snr: int
    ap_type: int
    quality: float


def decode_slot(binding: CoreBinding, pcm: np.ndarray, slot_id: int, **ap: object):
    """Decode one slot, optionally with an a-priori."""

    return binding.decode(pcm, DecodeConfig(**{**BASE, **ap}), slot_id)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# survey
# ---------------------------------------------------------------------------


def sample_slots(items: list[int], limit: int) -> list[int]:
    """Take up to ``limit`` slots, spread across the range rather than from the start.

    Deterministic, so two runs pick the same slots.  Taking the first N would bias
    the sample toward the beginning of a QSO, where the a-priori is thinnest (the
    grid may not have been sent yet) and the partner's frequency least established.
    """

    if limit <= 0 or not items:
        return []
    if len(items) <= limit:
        return list(items)
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


def collect(
    binding: CoreBinding, slots: list[tuple[int, Path]]
) -> dict[int, list[Sighting]]:
    """One AP-off decode pass over the whole corpus, cached for every later use.

    This is the expensive part (a deep decode is ~2 s per slot) and it does not
    depend on which pair is under test -- so it runs **once** and every pair reads
    the same cache.  Without that, testing twelve pairs meant re-decoding the
    corpus twelve times, which is what made the first multi-pair run take over an
    hour before it was killed.
    """

    cache: dict[int, list[Sighting]] = {}
    for index, (slot_id, path) in enumerate(slots):
        cache[slot_id] = [
            Sighting(slot_id, r.text, float(r.frequency), int(r.snr),
                     int(r.ap_type), float(r.quality))
            for r in decode_slot(binding, load_pcm(path), slot_id).results
        ]
        if (index + 1) % 25 == 0:
            print(f"  pass 1: {index + 1}/{len(slots)} 槽", flush=True)
    return cache


def find_pairs(
    cache: dict[int, list[Sighting]],
) -> tuple[list[tuple[int, str, str]], dict[tuple[str, str], list[Sighting]], dict[str, str]]:
    """Rank the mutual pairs; return ``(ranked, directed, grids)``.

    A pair qualifies only when *both* directions were heard -- that is what makes
    it a real QSO rather than one station calling CQ into a crowd.  Ranked by
    message count, because the parity argument needs enough sightings to be
    trustworthy: a pair seen twice can be "seen twice on odd slots" by accident.
    """

    directed: dict[tuple[str, str], list[Sighting]] = defaultdict(list)
    grids: dict[str, str] = {}
    for slot_id in sorted(cache):
        for row in cache[slot_id]:
            parsed = parse_message(row.text)
            if parsed.from_call and parsed.grid:
                grids.setdefault(parsed.from_call, parsed.grid)
            if parsed.is_cq or not parsed.to_call or not parsed.from_call:
                continue
            directed[(parsed.to_call, parsed.from_call)].append(row)

    by_pair: Counter = Counter()
    for (a, b), rows in directed.items():
        by_pair[frozenset((a, b))] += len(rows)
    mutual = []
    for key, count in by_pair.items():
        a, b = tuple(key)
        if (a, b) in directed and (b, a) in directed:
            mutual.append((count, a, b))
    mutual.sort(reverse=True)
    return mutual, directed, grids


def survey(binding: CoreBinding, slots: list[tuple[int, Path]]) -> dict[str, object]:
    """List the real QSO pairs in this corpus that an AP test could adopt."""

    cache = collect(binding, slots)
    mutual, directed, grids = find_pairs(cache)
    total = sum(len(rows) for rows in cache.values())

    print(f"扫描 {len(slots)} 槽，报文 {total} 条；双向互答的对 {len(mutual)} 个\n")
    for count, a, b in mutual[:12]:
        rows = sorted(directed[(a, b)] + directed[(b, a)], key=lambda s: s.slot_id)
        parities = sorted({s.slot_id % 2 for s in rows if a in s.text and b in s.text})
        print(f"  {a} <-> {b}   {count} 条   grid({b})={grids.get(b, '?')} "
              f"grid({a})={grids.get(a, '?')}   发射奇偶={parities}", flush=True)
        for row in rows[:4]:
            print(f"      slot {row.slot_id}  {row.text!r}  f={row.frequency:.1f} snr={row.snr}")
    return {
        "slots": len(slots),
        "messages": total,
        "mutual_pairs": len(mutual),
        "pairs": [{"count": c, "a": a, "b": b} for c, a, b in mutual],
    }


# ---------------------------------------------------------------------------
# paired measurement
# ---------------------------------------------------------------------------


def run_pair(
    binding: CoreBinding,
    slots: list[tuple[int, Path]],
    cache: dict[int, list[Sighting]],
    *,
    my_call: str,
    dx_call: str,
    dx_grid: str,
    progress: int = 2,
    verbose: bool = True,
    max_slots_per_pair: int = 32,
) -> dict[str, object]:
    """Measure AP-off vs AP-fed on the slots where ``dx_call`` was transmitting.

    ``cache`` is :func:`collect`'s AP-off pass over the corpus, shared by every
    pair.  From it we infer the partner's transmit parity, which tells us *which*
    slots they transmitted in -- including the ones nobody decoded.  Those slots
    are then decoded once more, AP-fed, with the a-priori ``my_call`` would
    legitimately have had.
    """

    partner: list[Sighting] = [
        row for slot_id in sorted(cache) for row in cache[slot_id]
        if parse_message(row.text).from_call.upper() == dx_call.upper()
    ]

    if not partner:
        if verbose:
            print(f"{dx_call} 从未在这 {len(slots)} 槽里被解出 —— 换一对")
        return {"error": "partner never decoded"}

    parities = Counter(s.slot_id % 2 for s in partner)
    parity, parity_count = parities.most_common(1)[0]
    if verbose:
        print(f"{dx_call} 被解出 {len(partner)} 次，发射奇偶 = {parity}"
              f"（{parity_count}/{len(partner)} 一致）", flush=True)

    # Restrict to the QSO window -- first sighting to last.  Parity alone says
    # "they transmit on odd slots", which is true of the whole recording and false
    # of the QSO: the first version of this tool counted 50 expected slots when
    # the exchange only spanned 15, so most "neither" cells were silence rather
    # than misses and the recall question lost its denominator (§4.6).
    window_lo = min(s.slot_id for s in partner)
    window_hi = max(s.slot_id for s in partner)
    expected = [s for s, _p in slots
                if s % 2 == parity and window_lo <= s <= window_hi]
    heard = {s.slot_id for s in partner}
    if verbose:
        print(f"⇒ QSO 窗口 {window_lo}..{window_hi}，按奇偶推断 {dx_call} 在 "
              f"{len(expected)} 个槽里发过（其中 {len(heard & set(expected))} 个被解出过，"
              f"{len(expected) - len(heard & set(expected))} 个没有）", flush=True)

    path_by_slot = dict(slots)
    gained: list[dict[str, object]] = []
    lost: list[dict[str, object]] = []
    both_hits = off_only = neither = 0
    ap_types: Counter = Counter()
    # Whether AP fired *at all*, over every fed decode rather than only the gained
    # ones.  Without it a null result is uninterpretable: "0 gained" could mean AP
    # found nothing, or that it never ran (§4.4 made exactly that mistake).
    fed_ap_decodes = 0
    fed_ap_types: Counter = Counter()
    slots_measured = 0

    # Every text ever seen anywhere in this corpus, for corroboration.
    all_texts: Counter = Counter()
    for rows in cache.values():
        for row in rows:
            all_texts[row.text.strip()] += 1

    # Pass 2 decodes **only the AP-fed arm**.  The AP-off arm *is* the cache:
    # pass 1 ran BASE with no a-priori fields, i.e. today's production
    # configuration, and §4.6 verified on these very slots that a starved AP is
    # indistinguishable from ap=False (33/33, 29/29, 23/23 decodes, zero unique to
    # either arm).  Re-decoding it would double the cost to measure one thing twice.
    #
    # The budget keeps both kinds of slot in view: the ones the classical path
    # missed (where AP can gain) and the ones it caught (where AP must not lose).
    opportunities = [s for s in expected if s not in heard]
    already_decoded = [s for s in expected if s in heard]
    half = max(1, max_slots_per_pair // 2)
    take_opp = min(half, len(opportunities))
    chosen = sorted(
        sample_slots(opportunities, half)
        + sample_slots(already_decoded, max_slots_per_pair - take_opp)
    )
    if verbose:
        print(f"  应发槽 {len(expected)} = 机会槽 {len(opportunities)} + 已解出 "
              f"{len(already_decoded)}；按预算取 {len(chosen)} 槽配对"
              f"（机会 {take_opp} / 已解出 {len(chosen) - take_opp}）", flush=True)

    for slot_id in chosen:
        path = path_by_slot[slot_id]
        pcm = load_pcm(path)
        # The a-priori my_call would have had: the partner's call, their grid
        # (sent earlier in the QSO), and the frequency they were last heard on.
        previous = [s for s in partner if s.slot_id < slot_id]
        if not previous:
            continue  # nothing known yet -- exactly the idle case in production
        last = max(previous, key=lambda s: s.slot_id)
        grid = dx_grid or next(
            (parse_message(s.text).grid for s in reversed(previous)
             if parse_message(s.text).grid), ""
        )
        rx_frequency = int(round(last.frequency))

        fed = decode_slot(
            binding, pcm, slot_id,
            ap=True, my_call=my_call, dx_call=dx_call, dx_grid=grid,
            qso_progress=progress, rx_frequency=rx_frequency,
            tx_frequency=rx_frequency,
        )

        def partner_texts(batch) -> set[str]:
            return {
                r.text.strip()
                for r in batch.results
                if parse_message(r.text).from_call.upper() == dx_call.upper()
            }

        off_set = {
            row.text.strip() for row in cache.get(slot_id, ())
            if parse_message(row.text).from_call.upper() == dx_call.upper()
        }
        fed_set = partner_texts(fed)
        slots_measured += 1
        for row in fed.results:
            if row.ap_type:
                fed_ap_decodes += 1
                fed_ap_types[int(row.ap_type)] += 1

        both_hits += int(bool(off_set) and bool(fed_set))
        off_only += int(bool(off_set) and not fed_set)
        neither += int(not off_set and not fed_set)

        for text in sorted(fed_set - off_set):
            row = next(r for r in fed.results if r.text.strip() == text)
            # Corroboration: does this text appear in any *other* slot?  A real
            # transmission repeats; an invention has nothing to match.
            elsewhere = sum(
                1 for sid, rows in cache.items()
                if sid != slot_id and any(r.text.strip() == text for r in rows)
            )
            ap_types[int(row.ap_type)] += 1
            gained.append({
                "slot_id": slot_id, "text": text, "ap_type": int(row.ap_type),
                "quality": round(float(row.quality), 4), "snr": int(row.snr),
                "frequency": round(float(row.frequency), 1),
                "seen_in_other_slots": elsewhere,
                "corpus_wide_count": all_texts.get(text, 0),
            })
        for text in sorted(off_set - fed_set):
            row = next(r for r in cache.get(slot_id, ()) if r.text.strip() == text)
            lost.append({"slot_id": slot_id, "text": text, "snr": int(row.snr)})

    corroborated = sum(1 for g in gained if g["seen_in_other_slots"] > 0)
    if verbose:
        print(f"配对槽数 {slots_measured}   两臂都解出 {both_hits}   只有 AP-off {off_only}"
              f"   只有 AP-fed {len(gained)}   都没解出 {neither}")
        print(f"AP 是否开火：fed 臂里 ap_type != 0 的报文 {fed_ap_decodes} 条"
              f"   分布 {dict(sorted(fed_ap_types.items())) or '—'}")
        print(f"AP-fed 独有的报文里，**在其他槽也出现过**（=真实重发，可信）: "
              f"{corroborated}/{len(gained)}")
        print(f"增益的 ap_type 分布: {dict(sorted(ap_types.items())) or '—'}")
        for item in gained:
            flag = "可信" if item["seen_in_other_slots"] else "**无旁证**"
            print(f"  slot {item['slot_id']}  ap{item['ap_type']}  q={item['quality']:<6} "
                  f"snr={item['snr']:>3}  f={item['frequency']:<7} {flag}  {item['text']!r}")
        for item in lost:
            print(f"  [丢失] slot {item['slot_id']}  snr={item['snr']}  {item['text']!r}")

    return {
        "my_call": my_call, "dx_call": dx_call, "dx_grid": dx_grid,
        "parity": parity, "expected_slots": len(expected),
        "window": [window_lo, window_hi],
        "opportunities": len(opportunities), "already_decoded": len(already_decoded),
        "slots_measured": slots_measured, "max_slots_per_pair": max_slots_per_pair,
        "both": both_hits, "off_only": lost, "fed_only": gained, "neither": neither,
        "corroborated": corroborated, "ap_types": dict(ap_types),
        "fed_ap_decodes": fed_ap_decodes, "fed_ap_types": dict(fed_ap_types),
    }


def run_absent(
    binding: CoreBinding,
    slots: list[tuple[int, Path]],
    cache: dict[int, list[Sighting]],
    *,
    absent_call: str,
    absent_grid: str,
    my_call: str,
    slots_to_try: int = 40,
    progress: int = 2,
) -> dict[str, object]:
    """Feed the a-priori of a station who is NOT transmitting, and count inventions.

    §2.5 is why this arm exists.  When ``dx_grid`` is supplied, iaptype 31 pins all
    77 payload bits with ``apmag = maxval(abs(llra)) * 1.01`` -- stronger than any
    received soft value -- and the CRC-14 is a function of those 77 bits, so **if the
    prior wins, the CRC passes by construction**.  A repeated CQ then corroborates
    *itself*: an invented "CQ X grid" in one slot looks legitimate because the real X
    sent it in another.  So "seen elsewhere" cannot settle the precision question,
    and this arm settles it a different way -- pick a station who never appears in
    the corpus at all, so any decode naming them can only have been invented.

    ``absent_call`` must be verified absent by the caller (this function does not
    re-check); everything else about the a-priori is realistic, including pointing
    ``rx_frequency`` at a real, busy part of the band so the candidate gate at nfqso
    (``sync8var``: ±3 Hz, sync >= 1.1) has something to promote.
    """

    # Aim at real activity: pick frequencies where the classical path *did* decode
    # something, so the sync gate is satisfied and AP has a candidate to work on.
    # Aiming at an empty frequency would make the arm trivially pass.
    busy = [
        (slot_id, rows[0].frequency)
        for slot_id, rows in cache.items()
        if rows
    ]
    busy.sort()
    chosen = sample_slots([s for s, _f in busy], slots_to_try)
    freq_of = dict(busy)

    invented: list[dict[str, object]] = []
    ap_fired = 0
    ap_types: Counter = Counter()
    measured = 0

    print(f"缺席先验臂：my={my_call}  dx={absent_call}  grid={absent_grid}  "
          f"（该呼号在全语料 0 次出现）")
    print(f"  在 {len(chosen)} 个有真实活动的槽上试，rx_frequency 指向该槽已有的解码频率")

    for slot_id in chosen:
        path = dict(slots)[slot_id]
        rx_frequency = int(round(freq_of[slot_id]))
        fed = decode_slot(
            binding, load_pcm(path), slot_id,
            ap=True, my_call=my_call, dx_call=absent_call, dx_grid=absent_grid,
            qso_progress=progress, rx_frequency=rx_frequency,
            tx_frequency=rx_frequency,
        )
        measured += 1
        for row in fed.results:
            if row.ap_type:
                ap_fired += 1
                ap_types[int(row.ap_type)] += 1
            parsed = parse_message(row.text)
            names_absent = absent_call.upper() in (
                parsed.from_call.upper(), parsed.to_call.upper()
            ) or absent_call.upper() in row.text.upper()
            if names_absent:
                invented.append({
                    "slot_id": slot_id, "text": row.text.strip(),
                    "ap_type": int(row.ap_type), "quality": round(float(row.quality), 4),
                    "snr": int(row.snr), "frequency": round(float(row.frequency), 1),
                })

    print(f"  配对槽 {measured}   fed 臂 ap_type != 0 共 {ap_fired} 条   "
          f"分布 {dict(sorted(ap_types.items())) or '—'}")
    print(f"  **点名缺席呼号的解码：{len(invented)} 条**")
    for item in invented:
        print(f"    slot {item['slot_id']}  ap{item['ap_type']}  q={item['quality']}  "
              f"snr={item['snr']}  f={item['frequency']}  {item['text']!r}")

    return {
        "absent_call": absent_call, "absent_grid": absent_grid, "my_call": my_call,
        "slots_measured": measured, "fed_ap_decodes": ap_fired,
        "ap_types": dict(ap_types), "invented": invented,
    }


def run_auto(
    binding: CoreBinding,
    slots: list[tuple[int, Path]],
    *,
    pairs: int,
    progress: int = 2,
    min_messages: int = 6,
    max_slots_per_pair: int = 32,
) -> dict[str, object]:
    """Run the N most active pairs and report one aggregate number.

    One pair proves the mechanism can fire on real audio; it does not say how
    often.  A single AP-off pass feeds every pair, so the cost is one corpus
    decode plus the paired re-decodes, not N corpus decodes.
    """

    cache = collect(binding, slots)
    mutual, directed, grids = find_pairs(cache)
    usable = [(count, a, b) for count, a, b in mutual if count >= min_messages]
    print(f"\n双向互答的对 {len(mutual)} 个，其中 ≥{min_messages} 条报文的 {len(usable)} 个；"
          f"取前 {pairs} 个\n", flush=True)

    results: list[dict[str, object]] = []
    for count, a, b in usable[:pairs]:
        # Adopt the receiving side: a8's getmsg builds "mycall hiscall <info>", so
        # my_call must be the *addressee* of the partner's transmissions.  For
        # directed[(a, b)] the messages are "a b <info>", i.e. b sent to a.
        my_call, dx_call = a, b
        grid = grids.get(dx_call, "")
        print(f"── {my_call} ← {dx_call}  ({count} 条, grid={grid or '?'})", flush=True)
        outcome = run_pair(
            binding, slots, cache,
            my_call=my_call, dx_call=dx_call, dx_grid=grid,
            progress=progress, verbose=True, max_slots_per_pair=max_slots_per_pair,
        )
        if "error" not in outcome:
            outcome["pair"] = f"{my_call}/{dx_call}"
            outcome["sightings"] = count
            results.append(outcome)
        print(flush=True)

    gained = [g for r in results for g in r.get("fed_only", [])]
    lost = [g for r in results for g in r.get("off_only", [])]
    corroborated = [g for g in gained if g.get("seen_in_other_slots")]
    unexplained = [g for g in gained if not g.get("seen_in_other_slots")]
    ap_types: Counter = Counter()
    for g in gained:
        ap_types[int(g.get("ap_type", 0))] += 1
    windows = sum(int(r.get("slots_measured", 0)) for r in results)
    fired = sum(int(r.get("fed_ap_decodes", 0)) for r in results)
    opportunities = sum(int(r.get("opportunities", 0)) for r in results)

    print("=" * 72)
    print(f"汇总：{len(results)} 对，配对槽 {windows} 个，窗口内机会槽共 {opportunities} 个")
    print(f"  AP-fed 独有（增益）  {len(gained)}   其中旁证成立 {len(corroborated)}，"
          f"无旁证 {len(unexplained)}")
    print(f"  AP-off 独有（丢失）  {len(lost)}")
    print(f"  fed 臂 ap_type != 0 共 {fired} 条   增益的 ap_type 分布 "
          f"{dict(sorted(ap_types.items())) or '—'}")
    for g in gained:
        flag = "可信" if g.get("seen_in_other_slots") else "**无旁证**"
        print(f"    {g.get('slot_id')}  ap{g.get('ap_type')}  q={g.get('quality')}  "
              f"snr={g.get('snr')}  {flag}  {g.get('text')!r}")
    for g in lost:
        print(f"    [丢失] {g.get('slot_id')}  snr={g.get('snr')}  {g.get('text')!r}")

    return {
        "pairs_run": len(results),
        "slots_measured": windows,
        "opportunities": opportunities,
        "gained": gained,
        "lost": lost,
        "corroborated": len(corroborated),
        "unexplained": len(unexplained),
        "fed_ap_decodes": fired,
        "ap_types": dict(ap_types),
        "per_pair": [
            {k: r.get(k) for k in ("pair", "sightings", "window", "expected_slots",
                                   "opportunities", "already_decoded",
                                   "slots_measured", "both", "neither",
                                   "fed_ap_decodes", "corroborated")}
            for r in results
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--slots", type=int, default=None)
    parser.add_argument("--survey", action="store_true")
    parser.add_argument("--my-call", default="")
    parser.add_argument("--dx-call", default="")
    parser.add_argument("--dx-grid", default="")
    parser.add_argument("--progress", type=int, default=2)
    parser.add_argument(
        "--auto", type=int, default=0,
        help="run the N most active mutual pairs and aggregate (one corpus decode)",
    )
    parser.add_argument(
        "--min-messages", type=int, default=6,
        help="a pair with fewer sightings cannot support the parity inference",
    )
    parser.add_argument(
        "--absent", default="",
        help="precision arm: feed this (never-heard) call's a-priori and count inventions",
    )
    parser.add_argument("--absent-grid", default="QN06")
    parser.add_argument("--absent-slots", type=int, default=40)
    parser.add_argument(
        "--max-slots", type=int, default=32,
        help="paired slots per pair (budget cap; spread-sampled, not head-sampled)",
    )
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--library", default=None)
    args = parser.parse_args(argv)

    slots = slot_paths(args.corpus, args.slots)
    if not slots:
        print(f"no slots under {args.corpus}")
        return 2
    binding = CoreBinding(args.library or default_library_path())

    if args.survey:
        report: object = survey(binding, slots)
    elif args.auto:
        report = run_auto(
            binding, slots, pairs=args.auto, progress=args.progress,
            min_messages=args.min_messages, max_slots_per_pair=args.max_slots,
        )
    elif args.absent:
        cache = collect(binding, slots)
        seen = {
            parse_message(row.text).from_call.upper()
            for rows in cache.values() for row in rows
        } | {
            parse_message(row.text).to_call.upper()
            for rows in cache.values() for row in rows
        }
        if args.absent.upper() in seen:
            print(f"{args.absent} 出现在语料里 —— 缺席臂需要一个从未出现的呼号")
            return 2
        report = run_absent(
            binding, slots, cache,
            absent_call=args.absent, absent_grid=args.absent_grid,
            my_call=args.my_call or "N0CALL", slots_to_try=args.absent_slots,
            progress=args.progress,
        )
    elif args.my_call and args.dx_call:
        cache = collect(binding, slots)
        report = run_pair(
            binding, slots, cache, my_call=args.my_call, dx_call=args.dx_call,
            dx_grid=args.dx_grid, progress=args.progress,
            max_slots_per_pair=args.max_slots,
        )
    else:
        print("需要 --survey、--auto N，或 --my-call 与 --dx-call 两者")
        return 2

    if args.json:
        args.json.write_text(json.dumps(report, indent=1, sort_keys=True, default=str))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
