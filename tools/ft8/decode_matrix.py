"""Decode a corpus of real slots under a matrix of configurations.

A research instrument, not a product path.  Its output is the raw material for
two questions that decide where decoding research should spend its time:

* **How findable is each message?**  For every (slot, text) pair, how many
  independent configurations decoded it.  A message found by exactly one
  configuration is on the edge of what the decoder can do; a message found by
  all of them is not where the recall is lost.
* **Which configuration finds what?**  Per-configuration recall against the
  union, so a portfolio (P1) can be sized by evidence rather than by taste.

Runs locally and needs no network: the corpus is a directory of ``slot_*.raw``
files (``MRRC_FT8_SLOT_DUMP_DIR`` on the box), the decoders are the same
``WorkerSupervisor`` production uses, and every configuration differs only in
its ``DecodeConfig``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

SLOT_BYTES = 360_000


@dataclass(frozen=True, slots=True)
class Config:
    """One decoding configuration, named for the report."""

    name: str
    path: str        # "fast" | "improved"
    profile: int
    cycles: int
    sensitivity: int = 2
    low_threshold: bool = False


# The first matrix is deliberately small and ladder-shaped: the same three
# knobs production already uses (path, cycles, profile), one axis at a time.
DEFAULT_MATRIX = (
    Config("fast", "fast", 3, 1),
    Config("imp-c1-p3", "improved", 3, 1),
    Config("imp-c2-p3", "improved", 3, 2),
    Config("imp-c3-p3", "improved", 3, 3),
    Config("imp-c3-p1", "improved", 1, 3),
    Config("imp-c3-p4", "improved", 4, 3),
    Config("imp-c3-p3-lowthr", "improved", 3, 3, low_threshold=True),
    # The second round: what the first one found to be missing.  Profile 1 beat
    # profile 3 on recall and low_threshold beat it on cost, so the combinations
    # are the obvious next question — and a portfolio is only as good as the
    # diversity of what it runs.
    Config("imp-c3-p1-lowthr", "improved", 1, 3, low_threshold=True),
    Config("imp-c2-p1-lowthr", "improved", 1, 2, low_threshold=True),
    Config("imp-c3-p2-lowthr", "improved", 2, 3, low_threshold=True),
)


def load_slot(path: Path) -> tuple[int, bytes]:
    path = Path(path)
    samples = path.read_bytes()
    if len(samples) != SLOT_BYTES:
        raise ValueError(f"a slot is exactly {SLOT_BYTES} bytes: {path} has {len(samples)}")
    return int(path.stem.split("_")[1]), samples


async def decode_corpus(
    slots: list[tuple[int, bytes]],
    *,
    app_root: str,
    matrix: tuple[Config, ...],
    threads: int,
) -> dict[int, dict[str, list[dict[str, object]]]]:
    """Return ``{slot_id: {config_name: [results]}}`` for the whole corpus."""

    # The app root comes first: this tool is deployed beside the tree it decodes
    # with, not inside it (the same rule the field twin follows).
    os.chdir(app_root)
    if app_root not in sys.path:
        sys.path.insert(0, app_root)

    from ft8.core.models import DecodeConfig, DecodePath
    from ft8.core.supervisor import WorkerSupervisor
    from ft8.core.worker import default_library_path
    from ft8.engine.dsp_decode import SupervisorDecoder
    supervisor = WorkerSupervisor(library_path=default_library_path(), stop_timeout=5.0)
    supervisor.start()
    out: dict[int, dict[str, list[dict[str, object]]]] = {}
    try:
        for config in matrix:
            decoder_config = DecodeConfig(
                path=DecodePath(config.path),
                profile=config.profile,
                cycles=config.cycles,
                sensitivity=config.sensitivity,
                low_threshold=config.low_threshold,
                threads=threads,
            )
            started = time.monotonic()
            # One bad configuration (an out-of-range profile, say) must not throw
            # away the whole run: this is a research instrument, and a matrix row
            # that cannot run is a finding, not a crash.
            try:
                with SupervisorDecoder(supervisor, decoder_config) as decoder:
                    for slot_id, samples in slots:
                        batch = await decoder.decode_path(
                            slot_id, samples, decoder_config.path, threads=threads
                        )
                        out.setdefault(slot_id, {})[config.name] = [
                            {"text": r.text, "snr": r.snr, "dt": r.dt, "frequency": r.frequency}
                            for r in batch.results
                        ]
            except Exception as error:  # noqa: BLE001
                print(f"  {config.name:<20} FAILED: {error}", flush=True)
                continue
            print(
                f"  {config.name:<20} {len(slots)} slot(s) in {time.monotonic() - started:.1f}s",
                flush=True,
            )
    finally:
        supervisor.stop()
    return out


def findability(
    corpus: dict[int, dict[str, list[dict[str, object]]]]
) -> list[dict[str, object]]:
    """One row per message: how many configurations found it, and its details."""

    rows: list[dict[str, object]] = []
    for slot_id, per_config in corpus.items():
        seen: dict[str, dict[str, object]] = {}
        for config_name, results in per_config.items():
            for result in results:
                text = str(result["text"])
                row = seen.setdefault(text, {"slot": slot_id, "text": text, "found_by": [], "snr": [], "dt": [], "freq": []})
                row["found_by"].append(config_name)          # type: ignore[union-attr]
                row["snr"].append(result["snr"])             # type: ignore[union-attr]
                row["dt"].append(result["dt"])               # type: ignore[union-attr]
                row["freq"].append(result["frequency"])      # type: ignore[union-attr]
        rows.extend(seen.values())
    return rows


def summarize(corpus: dict[int, dict[str, list[dict[str, object]]]], matrix) -> str:
    rows = findability(corpus)
    union = len(rows)
    counts = Counter(len(row["found_by"]) for row in rows)   # type: ignore[arg-type]
    lines = [
        f"slots: {len(corpus)}   union of messages: {union}",
        "",
        "found by N configurations:",
    ]
    for n in sorted(counts, reverse=True):
        lines.append(f"  {n:>2} config(s): {counts[n]:>5} message(s)  ({100 * counts[n] / union:.1f}%)")
    lines.append("")
    lines.append("per-configuration recall against the union:")
    for config in matrix:
        hits = sum(1 for row in rows if config.name in row["found_by"])  # type: ignore[operator]
        lines.append(f"  {config.name:<22} {hits:>5} / {union}  ({100 * hits / union:.1f}%)")
    lines.append("")
    lines.append("the hardest messages (found by one configuration only):")
    lonely = [row for row in rows if len(row["found_by"]) == 1]           # type: ignore[arg-type]
    for row in sorted(lonely, key=lambda r: min(r["snr"]))[:15]:          # type: ignore[arg-type]
        lines.append(
            f"  slot {row['slot']}: {row['text']:<22} snr {min(row['snr']):>4} "
            f"dt {min(row['dt']):>6.2f} f {min(row['freq']):>7.1f}  <- {row['found_by'][0]}"  # type: ignore[index]
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slots", required=True)
    parser.add_argument("--app-root", default="/opt/mrrc-decode")
    parser.add_argument("--limit", type=int, default=0, help="first N slots (0 = all)")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)

    paths = sorted(Path(args.slots).glob("slot_*.raw"))
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        print(f"no slot_*.raw in {args.slots}", file=sys.stderr)
        return 2
    print(f"corpus: {len(paths)} slot(s) from {args.slots}")
    slots = [load_slot(path) for path in paths]
    print(f"matrix: {', '.join(c.name for c in DEFAULT_MATRIX)}")
    started = time.monotonic()
    corpus = asyncio.run(
        decode_corpus(slots, app_root=args.app_root, matrix=DEFAULT_MATRIX, threads=args.threads)
    )
    report = summarize(corpus, DEFAULT_MATRIX)
    print()
    print(report)
    print(f"\ntotal {time.monotonic() - started:.1f}s")
    if args.json:
        Path(args.json).write_text(
            json.dumps({"corpus": {str(k): v for k, v in corpus.items()}}, ensure_ascii=False),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
