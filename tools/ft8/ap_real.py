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


def survey(binding: CoreBinding, slots: list[tuple[int, Path]]) -> dict[str, object]:
    """List the real QSO pairs in this corpus that an AP test could adopt."""

    pairs: dict[tuple[str, str], list[Sighting]] = defaultdict(list)
    grids: dict[str, str] = {}
    total = 0
    for slot_id, path in slots:
        for result in decode_slot(binding, load_pcm(path), slot_id).results:
            total += 1
            parsed = parse_message(result.text)
            if parsed.from_call and parsed.grid:
                grids.setdefault(parsed.from_call, parsed.grid)
            if parsed.is_cq or not parsed.to_call or not parsed.from_call:
                continue
            pairs[(parsed.to_call, parsed.from_call)].append(
                Sighting(slot_id, result.text, float(result.frequency), int(result.snr),
                         int(result.ap_type), float(result.quality))
            )

    by_pair: Counter = Counter()
    for (a, b), rows in pairs.items():
        by_pair[frozenset((a, b))] += len(rows)
    mutual = []
    for key, count in by_pair.items():
        a, b = tuple(key)
        if (a, b) in pairs and (b, a) in pairs:
            mutual.append((count, a, b))
    mutual.sort(reverse=True)

    print(f"扫描 {len(slots)} 槽，报文 {total} 条；双向互答的对 {len(mutual)} 个\n")
    for count, a, b in mutual[:12]:
        rows = sorted(pairs[(a, b)] + pairs[(b, a)], key=lambda s: s.slot_id)
        parities = sorted({s.slot_id % 2 for s in rows if a in s.text and b in s.text})
        print(f"  {a} <-> {b}   {count} 条   grid({b})={grids.get(b, '?')} "
              f"grid({a})={grids.get(a, '?')}   发射奇偶={parities}")
        for row in rows[:4]:
            print(f"      slot {row.slot_id}  {row.text!r}  f={row.frequency:.1f} snr={row.snr}")
    return {"slots": len(slots), "messages": total, "mutual_pairs": len(mutual)}


# ---------------------------------------------------------------------------
# paired measurement
# ---------------------------------------------------------------------------


def run_pair(
    binding: CoreBinding,
    slots: list[tuple[int, Path]],
    *,
    my_call: str,
    dx_call: str,
    dx_grid: str,
    progress: int = 2,
    corroborate_slots: int = 6,
) -> dict[str, object]:
    """Measure AP-off vs AP-fed on the slots where ``dx_call`` was transmitting.

    Two passes.  First pass decodes everything AP-off and collects where the
    partner was heard; from that we infer the partner's transmit parity, which
    tells us *every* slot they transmitted in -- including the ones nobody
    decoded.  Second pass re-decodes only those slots, AP-off and AP-fed, with
    the a-priori that ``my_call`` would legitimately have had.
    """

    # Pass 1: where was the partner heard, and on which parity?
    sightings: dict[int, list[Sighting]] = {}
    partner: list[Sighting] = []
    cache: dict[int, list[Sighting]] = {}
    for slot_id, path in slots:
        rows = [
            Sighting(slot_id, r.text, float(r.frequency), int(r.snr),
                     int(r.ap_type), float(r.quality))
            for r in decode_slot(binding, load_pcm(path), slot_id).results
        ]
        cache[slot_id] = rows
        sightings[slot_id] = rows
        for row in rows:
            parsed = parse_message(row.text)
            if parsed.from_call.upper() == dx_call.upper():
                partner.append(row)

    if not partner:
        print(f"{dx_call} 从未在这 {len(slots)} 槽里被解出 —— 换一对")
        return {"error": "partner never decoded"}

    parities = Counter(s.slot_id % 2 for s in partner)
    parity, parity_count = parities.most_common(1)[0]
    print(f"{dx_call} 被解出 {len(partner)} 次，发射奇偶 = {parity}"
          f"（{parity_count}/{len(partner)} 一致）")

    # The slots the partner transmitted in, whether or not anyone decoded them.
    expected = [s for s, _p in slots if s % 2 == parity]
    print(f"⇒ 按奇偶推断 {dx_call} 在 {len(expected)} 个槽里发过"
          f"（其中只有 {len({s.slot_id for s in partner})} 个被解出过）\n")

    path_by_slot = dict(slots)
    gained: list[dict[str, object]] = []
    lost: list[dict[str, object]] = []
    both_hits = off_only = neither = 0
    ap_types: Counter = Counter()

    # Every text ever seen anywhere in this corpus, for corroboration.
    all_texts: Counter = Counter()
    for rows in cache.values():
        for row in rows:
            all_texts[row.text.strip()] += 1

    for slot_id in expected:
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

        off = decode_slot(binding, pcm, slot_id)
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

        off_set, fed_set = partner_texts(off), partner_texts(fed)
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
            row = next(r for r in off.results if r.text.strip() == text)
            lost.append({"slot_id": slot_id, "text": text, "snr": int(row.snr)})

    corroborated = sum(1 for g in gained if g["seen_in_other_slots"] > 0)
    print(f"配对槽数 {len([s for s in expected if any(p.slot_id < s for p in partner)])}"
          f"   两臂都解出 {both_hits}   只有 AP-off {len(lost)}   只有 AP-fed {len(gained)}"
          f"   都没解出 {neither}")
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
        "both": both_hits, "off_only": lost, "fed_only": gained, "neither": neither,
        "corroborated": corroborated, "ap_types": dict(ap_types),
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
    elif args.my_call and args.dx_call:
        report = run_pair(
            binding, slots, my_call=args.my_call, dx_call=args.dx_call,
            dx_grid=args.dx_grid, progress=args.progress,
        )
    else:
        print("需要 --survey，或 --my-call 与 --dx-call 两者")
        return 2

    if args.json:
        args.json.write_text(json.dumps(report, indent=1, sort_keys=True, default=str))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
