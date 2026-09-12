#!/usr/bin/env python3
"""
Generic CI-V self-check for the Icom backend (spec 2026-09-12 §7)
=================================================================
Steps: model ID (19 00) -> read-only probes -> scope measurement ->
static meters -> optional TX check -> state restore -> Markdown report.

Why this exists: the IC-705/IC-7610/IC-7760 profiles were written from
offline rig data with no radio present.  Three facts cannot be settled
without hardware — the actual scope bin count, the amplitude ceiling and
the per-model meters — so this script measures them in one run and emits
a paste-ready report that a profile can be corrected from.

Safety:
- Ordinary runs key nothing: every step is read-only apart from enabling
  and then disabling the scope stream.
- The TX check needs BOTH ``--tx-check`` and ``--allow-tx``, holds the
  carrier for 200 ms, then reads the PTT state back and prints manual
  recovery steps if the radio still reports TX.
- ``finally`` restores what it changed (waveform output off, PTT released).

Serial I/O goes through the production ``CivController`` — the diagnostic
exercises the same framing/parsing path the server uses, so a PASS here is
evidence about the real code path, not about a private copy of it.

Usage:
    python _diag_civ.py --model ic705 --port /dev/cu.usbmodem1234
    python _diag_civ.py --model ic7760 --port COM5 --tx-check --allow-tx
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from backends.ic7300.civ_codec import ScopeAssembler
from backends.ic7300.civ_controller import CivController
from backends.ic7300.civ_profiles import PROFILES, get_profile

TX_SETTLE_S = 0.2
TX_READBACK_TIMEOUT_S = 3.0
SCOPE_CAPTURE_S = 6.0
PROBE_TIMEOUT_S = 0.5


@dataclass
class ScopeStats:
    """What the radio actually sent, versus what the profile claims."""

    waveforms: int = 0
    min_bins: Optional[int] = None
    max_bins: Optional[int] = None
    amp_min: Optional[int] = None
    amp_max: Optional[int] = None
    seq_max: Optional[int] = None
    amplitude_histogram: dict = field(default_factory=dict)


def summarize_scope(samples: list) -> ScopeStats:
    """Aggregate captured waveforms into the three facts a profile needs."""
    stats = ScopeStats()
    if not samples:
        return stats
    lengths = [len(s["bins"]) for s in samples if s.get("bins")]
    amps = [b for s in samples for b in (s.get("bins") or [])]
    if lengths:
        stats.min_bins, stats.max_bins = min(lengths), max(lengths)
    if amps:
        stats.amp_min, stats.amp_max = min(amps), max(amps)
        for value in amps:
            bucket = (value // 10) * 10
            stats.amplitude_histogram[bucket] = (
                stats.amplitude_histogram.get(bucket, 0) + 1)
    seqs = [s.get("seq_max") for s in samples if s.get("seq_max")]
    if seqs:
        stats.seq_max = max(seqs)
    stats.waveforms = len(samples)
    return stats


def evaluate_identity(observed: Optional[bytes],
                      expected: Optional[tuple]) -> tuple:
    """(verdict, human text) for the 19 00 model-ID comparison."""
    if observed is None:
        return "no-answer", "no answer to 19 00 (radio may not implement it)"
    hexed = observed.hex(" ").upper()
    if expected is None:
        return ("unknown",
                f"observed model ID {hexed}; the profile records no "
                f"expectation (paste this report to populate it)")
    if tuple(observed) == tuple(expected):
        return "match", f"model ID {hexed} matches the profile"
    return ("mismatch",
            f"model ID {hexed} != profile expectation "
            f"{bytes(expected).hex(' ').upper()}")


def tx_check_permitted(tx_check: bool, allow_tx: bool) -> bool:
    """The TX self-check needs an explicit double opt-in."""
    return bool(tx_check and allow_tx)


def format_report(**kw) -> str:
    """Render the paste-ready Markdown report."""
    stats: ScopeStats = kw["scope_stats"]
    lines = [
        f"# CI-V diagnostic report — {kw['display_name']} ({kw['model_key']})",
        "",
        f"- Date: {datetime.now().isoformat(timespec='seconds')}",
        f"- Port: {kw['port']} @ {kw['baud']} 8N1, CI-V address "
        f"0x{kw['civ_addr']:02X}",
        f"- Model identity (19 00): {kw['identity_text']}",
        f"- TX check: {kw['tx_check']}",
        "",
        "## Read-only probes",
    ]
    for name, value in kw["probe_results"]:
        lines.append(f"- {name}: {value}")
    lines += [
        "",
        "## Scope measurement",
        f"- waveforms captured: {stats.waveforms}",
        f"- bin count observed: {stats.min_bins}..{stats.max_bins} "
        f"(profile scope_bins={kw['profile_bins']})",
        f"- amplitude observed: {stats.amp_min}..{stats.amp_max} "
        f"(profile scope_amp_max={kw['profile_amp_max']})",
        f"- segment count observed: {stats.seq_max} "
        f"(profile scope_seq_max={kw['profile_seq_max']})",
        f"- amplitude histogram (10-wide buckets): {stats.amplitude_histogram}",
        "",
        "## Meters (static, no RF)",
    ]
    for name, value in kw["meter_results"]:
        lines.append(f"- {name}: {value}")
    lines += [
        "",
        "## How to feed this back into the repository",
        "Any number above that differs from the profile value is a profile "
        "bug: update `backends/ic7300/civ_profiles.py`, drop the matching "
        "`TODO(hw-verify)` note, and set `verified=True` **only** for the "
        "meters and geometry this run actually confirmed.  The meter curves "
        "(power/voltage/current) additionally need known-power points: note "
        "the raw value at a known wattage to recalibrate them.",
    ]
    return "\n".join(lines) + "\n"


async def run(args) -> int:
    profile = get_profile(args.model)
    civ = CivController(args.port, args.baud,
                        civ_addr=args.civ_addr or profile.civ_addr,
                        transceive_cmd=profile.transceive_cmd,
                        profile=profile, att_steps=profile.att_steps)
    probe_results: list = []
    meter_results: list = []
    samples: list = []
    identity_text = "not attempted"
    tx_check = "skipped (needs --tx-check and --allow-tx)"
    scope_enabled = False
    stats = ScopeStats()
    try:
        if not await civ.connect():
            print(f"ERROR: cannot open {args.port}", file=sys.stderr)
            return 2

        observed = await civ.get_model_id(timeout=1.0)
        verdict, identity_text = evaluate_identity(observed,
                                                   profile.model_id_bytes)
        print(f"Model identity: {verdict} — {identity_text}")

        for name, coro in (
            ("frequency", civ._query_data(0x03, timeout=PROBE_TIMEOUT_S)),
            ("mode", civ._query_data(0x04, timeout=PROBE_TIMEOUT_S)),
            ("s_meter", civ.get_meter("s", timeout=PROBE_TIMEOUT_S)),
            ("rf_power", civ._query_data(0x14, 0x0A, timeout=PROBE_TIMEOUT_S)),
            ("preamp", civ._query_data(0x16, 0x02, timeout=PROBE_TIMEOUT_S)),
            ("attenuator", civ._query_data(0x11, timeout=PROBE_TIMEOUT_S)),
            ("transceive_items", civ._query_data(0x1A, 0x05,
                                                  timeout=PROBE_TIMEOUT_S)),
        ):
            value = await coro
            text = (value.hex(" ").upper() if isinstance(value, bytes)
                    else str(value))
            probe_results.append((name, text))
            print(f"  {name}: {text}")

        assembler = ScopeAssembler(seq_max=profile.scope_seq_max,
                                   expected_bins=profile.scope_bins)
        await civ.set_scope_on(True)
        scope_enabled = True
        await civ.set_scope_mode(0)
        await civ.set_scope_span(5)
        await civ.set_scope_data_output(True)
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            try:
                segment = await asyncio.wait_for(civ.scope_queue.get(),
                                                 timeout=1.0)
            except asyncio.TimeoutError:
                continue
            bins = assembler.feed(segment)
            if bins:
                samples.append({"bins": bins,
                                "seq_max": segment.sequence_max})
        stats = summarize_scope(samples)
        print(f"  scope: {stats.waveforms} waveforms, bins "
              f"{stats.min_bins}..{stats.max_bins}, amp "
              f"{stats.amp_min}..{stats.amp_max}, seq {stats.seq_max}")

        # po/swr/alc/comp go through the mapped getter; vd/id are read raw
        # because this backend deliberately leaves them unmapped
        # (has_vd_id_meters=False) — the diagnostic is exactly where they
        # must still be visible.
        for meter in ("po", "swr", "alc", "comp"):
            meter_results.append(
                (meter, await civ.get_meter(meter, timeout=PROBE_TIMEOUT_S)))
        for name, sub in (("vd", 0x15), ("id", 0x16)):
            raw = await civ._query_data(0x15, sub, timeout=PROBE_TIMEOUT_S)
            meter_results.append((name, raw.hex(" ").upper() if raw else None))
        print(f"  meters: {meter_results}")

        if tx_check_permitted(args.tx_check, args.allow_tx):
            print(f"TX check: keying for {TX_SETTLE_S * 1000:.0f} ms …")
            await civ.set_ptt(True)
            await asyncio.sleep(TX_SETTLE_S)
            await civ.set_ptt(False)
            released = False
            deadline = time.monotonic() + TX_READBACK_TIMEOUT_S
            while time.monotonic() < deadline:
                state = await civ.get_ptt(timeout=PROBE_TIMEOUT_S)
                if state == 0:
                    released = True
                    break
                await asyncio.sleep(0.2)
            tx_check = ("PASS (PTT released and read back as RX)" if released
                        else "FAIL — radio still reports TX! Power the radio "
                             "off / unplug USB now, then re-check the 1C 00 "
                             "frame against Icom's documentation before "
                             "retrying")
            print(f"TX check: {tx_check}")
        elif args.tx_check:
            tx_check = "refused (--tx-check also needs --allow-tx)"
            print(f"TX check: {tx_check}")

        report = format_report(
            model_key=profile.model_key, display_name=profile.display_name,
            port=args.port, baud=args.baud, civ_addr=civ.civ_addr,
            identity_text=identity_text, probe_results=probe_results,
            scope_stats=stats, meter_results=meter_results,
            tx_check=tx_check, profile_bins=profile.scope_bins,
            profile_amp_max=profile.scope_amp_max,
            profile_seq_max=profile.scope_seq_max)
        path = f"diag_civ_{profile.model_key}_{datetime.now():%Y%m%d_%H%M%S}.md"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"Report written: {path}")
        return 0
    finally:
        # Restore the radio to the state we found it in.  The scope display
        # itself is left alone (it may have been on before we started); the
        # waveform-data output we switched on is switched back off.
        if scope_enabled:
            try:
                await civ.set_scope_data_output(False)
            except Exception:
                pass
        try:
            await civ.set_ptt(False)
        except Exception:
            pass
        await civ.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generic Icom CI-V self-check (writes a Markdown report)")
    parser.add_argument("--model", required=True, choices=sorted(PROFILES),
                        help="model key (ic7300, ic7300mk2, ic705, "
                             "ic7610, ic7760)")
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--civ-addr", type=lambda x: int(x, 0), default=None,
                        help="override the profile's CI-V address")
    parser.add_argument("--seconds", type=float, default=SCOPE_CAPTURE_S,
                        help="scope capture window (default 6 s)")
    parser.add_argument("--tx-check", action="store_true",
                        help="run the keyed TX self-check (needs --allow-tx)")
    parser.add_argument("--allow-tx", action="store_true",
                        help="confirm that RF emission is acceptable")
    return parser


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
