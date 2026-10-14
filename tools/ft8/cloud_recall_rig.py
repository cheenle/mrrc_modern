"""Does depth buy anything? — the rig that answers it on real slots.

Design: ``docs/superpowers/specs/2026-10-14-ft8-cloud-escalation-design.md`` §12.
Plan: ``docs/superpowers/plans/2026-10-14-ft8-cloud-escalation.md`` task 8.

Three sets of decodes over the same dumped slots:

* **local** — the box's production configuration (profile 3, threads 3, cycles 1);
* **hub same depth** — the hub at cycles=1, which isolates "a different machine"
  from "a deeper search" (it should reproduce the local set);
* **hub deep** — the hub at cycles=3, the knob the A53 cannot afford inside the
  4.0 s decision cutoff (~6.5 s locally against 1.208 s measured on the hub).

The last line of the report is the go/no-go test: if **hub deep** buys nothing
over **local**, the channel is deleted and the work returns to the DSP tiers.

Slots come from ``MRRC_FT8_SLOT_DUMP_DIR`` (``ft8/platform/slot_dump.py``); the
rig never touches the radio, the network aside.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("mrrc.cloud_recall_rig")

SLOT_SAMPLES = 180_000
SLOT_BYTES = SLOT_SAMPLES * 2
_NAME = re.compile(r"slot_(\d+)\.raw$")


# ── pure parts (unit-tested; the decode step is not) ─────────────────────


def load_slot(path: Path) -> tuple[int, bytes]:
    """Return ``(slot_id, samples)`` for one dumped slot."""

    path = Path(path)
    match = _NAME.search(path.name)
    if match is None:
        raise ValueError(f"not a slot dump (expected slot_<id>.raw): {path}")
    samples = path.read_bytes()
    if len(samples) != SLOT_BYTES:
        raise ValueError(
            f"a slot is exactly {SLOT_BYTES} bytes, {path} has {len(samples)}"
        )
    return int(match.group(1)), samples


def diff(baseline: Iterable[str], candidate: Iterable[str]) -> tuple[frozenset[str], frozenset[str]]:
    """``(gained, lost)`` — the two directions, per slot, never aggregated."""

    base, cand = set(baseline), set(candidate)
    return frozenset(cand - base), frozenset(base - cand)


@dataclass(frozen=True, slots=True)
class SlotRun:
    """One slot's message sets, one per decoder configuration."""

    slot_id: int
    local: tuple[str, ...]
    hub_same: tuple[str, ...]
    hub_deep: tuple[str, ...]


def summarize(rows: Sequence[SlotRun]) -> str:
    """Per-slot sets, then the three lines that decide the channel.

    Two numbers for each net gain, because they answer different questions:
    **decodes** are what the operator would see (each one a chance to answer a
    CQ we would otherwise never have heard), **distinct** is how many different
    messages those decodes were made of.  A station calling CQ in five slots is
    five chances and one new callsign.
    """

    lines: list[str] = []
    same_decodes = deep_decodes = 0
    same_distinct: set[str] = set()
    deep_distinct: set[str] = set()
    for row in rows:
        same_gain, _same_lost = diff(row.local, row.hub_same)
        deep_gain, deep_lost = diff(row.local, row.hub_deep)
        same_decodes += len(same_gain)
        deep_decodes += len(deep_gain)
        same_distinct |= set(same_gain)
        deep_distinct |= set(deep_gain)
        lines.append(
            f"slot {row.slot_id:>10}: local {len(row.local):>3} / "
            f"hub same {len(row.hub_same):>3} / "
            f"hub deep {len(row.hub_deep):>3}  (+{len(deep_gain)} / -{len(deep_lost)})"
        )
    lines.append(f"slots: {len(rows)}")
    lines.append(
        f"net gain (hub same depth vs local): {same_decodes} decodes / "
        f"{len(same_distinct)} distinct"
    )
    lines.append(
        f"net gain (deep vs local): {deep_decodes} decodes / "
        f"{len(deep_distinct)} distinct"
    )
    return "\n".join(lines)


# ── the decode step (needs the native library or the hub) ────────────────


def _enter_app_root(path: str) -> str:
    """Put the process where production runs the decoder (see the field twin)."""

    root = os.path.abspath(path)
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


async def run_slots(
    slots: Sequence[tuple[int, bytes]],
    *,
    app_root: str,
    local_config,
    local_threads: int,
    cloud_same=None,
    cloud_deep=None,
) -> list[SlotRun]:
    """Decode every slot under each available configuration."""

    from ft8.core.models import DecodePath
    from ft8.core.supervisor import WorkerSupervisor
    from ft8.core.worker import default_library_path
    from ft8.engine.dsp_decode import SupervisorDecoder

    supervisor = WorkerSupervisor(library_path=default_library_path(), stop_timeout=5.0)
    supervisor.start()
    rows: list[SlotRun] = []
    try:
        with SupervisorDecoder(supervisor, local_config) as local:
            for slot_id, samples in slots:
                batch = await local.decode_path(
                    slot_id, samples, DecodePath.IMPROVED, threads=local_threads
                )
                local_texts = tuple(r.text for r in batch.results)
                same = local_texts
                if cloud_same is not None:
                    same = tuple(
                        r.text
                        for r in (
                            await cloud_same.decode_path(slot_id, samples, DecodePath.IMPROVED)
                        ).results
                    )
                deep = same
                if cloud_deep is not None:
                    deep = tuple(
                        r.text
                        for r in (
                            await cloud_deep.decode_path(slot_id, samples, DecodePath.IMPROVED)
                        ).results
                    )
                rows.append(
                    SlotRun(slot_id=slot_id, local=local_texts, hub_same=same, hub_deep=deep)
                )
                log.info(
                    "slot %s: local %d, hub same %d, hub deep %d",
                    slot_id, len(local_texts), len(same), len(deep),
                )
    finally:
        supervisor.stop()
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slots", required=True, help="directory of slot_<id>.raw files")
    parser.add_argument("--app-root", default="/opt/mrrc_modern")
    parser.add_argument("--hub", default="", help="decode endpoint; empty = local only")
    parser.add_argument("--token", default="")
    parser.add_argument("--cycles", type=int, default=3, help="the deep cloud depth")
    parser.add_argument("--same-cycles", type=int, default=1, help="the hub's control depth")
    # The rig is offline: it measures which messages each configuration finds, not
    # whether they arrive in time.  A live deadline here would turn the measurement
    # into a second, quieter test of the transport — and on a real band the hub's
    # cycles=3 decode is ~3 s (measured 2026-10-14: cpu 2.130 s on a 22-decode slot,
    # against 1.208 s on the synthetic five-CQ slot), i.e. well past the live
    # deadline of 2.5 s.
    parser.add_argument("--cloud-timeout", type=float, default=30.0)
    parser.add_argument("--threads", type=int, default=3, help="the box's production threads")
    parser.add_argument("--local-cycles", type=int, default=1)
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    paths = sorted(Path(args.slots).glob("slot_*.raw"))
    if not paths:
        log.error("no slot_<id>.raw files in %s — enable MRRC_FT8_SLOT_DUMP_DIR first", args.slots)
        return 2
    slots = [load_slot(path) for path in paths]
    log.info("%d slot(s) from %s", len(slots), args.slots)

    app_root = _enter_app_root(args.app_root)
    from ft8.core.models import DecodeConfig
    from ft8.platform.cloud_decode import CloudDecoder

    local_config = DecodeConfig(profile=3, threads=args.threads, cycles=args.local_cycles)
    cloud_same = cloud_deep = None
    if args.hub:
        if not args.token:
            log.error("--hub needs --token")
            return 2
        cloud_same = CloudDecoder(
            args.hub, args.token, cycles=args.same_cycles, deadline_s=args.cloud_timeout
        )
        cloud_deep = CloudDecoder(
            args.hub, args.token, cycles=args.cycles, deadline_s=args.cloud_timeout
        )

    started = time.monotonic()
    rows = asyncio.run(
        run_slots(
            slots,
            app_root=app_root,
            local_config=local_config,
            local_threads=args.threads,
            cloud_same=cloud_same,
            cloud_deep=cloud_deep,
        )
    )
    report = summarize(rows)
    print()
    print(report)
    print(f"elapsed: {time.monotonic() - started:.1f}s")
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                [
                    {
                        "slot_id": row.slot_id,
                        "local": list(row.local),
                        "hub_same": list(row.hub_same),
                        "hub_deep": list(row.hub_deep),
                    }
                    for row in rows
                ],
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
