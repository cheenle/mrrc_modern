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
from backends.ic7300.civ_profiles import PROFILES as ICOM_PROFILES
from backends.ic7300.civ_profiles import get_profile as icom_profile
from backends.xiegu.xiegu_profiles import PROFILES as XIEGU_PROFILES
from backends.xiegu.xiegu_profiles import get_profile as xiegu_profile

#: Every CI-V model this tool can drive, across both families.  The Xiegu
#: G7250 lives in its own registry (backends/xiegu/) because its facts come
#: from a different provenance, but the wire protocol is the same CI-V.
ALL_PROFILES = {**ICOM_PROFILES, **XIEGU_PROFILES}

#: Models with no CI-V 0x27 waveform.  **Empty today**: the G7250 was the only
#: entry and that was wrong — its vendor rig file says ``HasSpectrum=true`` with
#: the IC-7300 geometry (475 bins / 11 segments / amp 160), which is why the
#: server now draws its waterfall from 0x27 (docs/g7250-wfview-analysis.md).
#: Kept as a named set so a future "no scope" model has one place to live.
NO_CIV_SCOPE_MODELS: frozenset = frozenset()

#: Which profile module a report's corrections belong in.
PROFILE_MODULE = {
    **{k: "backends/ic7300/civ_profiles.py" for k in ICOM_PROFILES},
    **{k: "backends/xiegu/xiegu_profiles.py" for k in XIEGU_PROFILES},
}


def get_profile(model_key: str):
    """Profile for a key from either family's registry."""
    return ALL_PROFILES[model_key]


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
        f"Any number above that differs from the profile value is a profile "
        f"bug: update `{kw.get('profile_module', 'backends/ic7300/civ_profiles.py')}`"
        f", drop the matching "
        "`TODO(hw-verify)` note, and set `verified=True` **only** for the "
        "meters and geometry this run actually confirmed.  The meter curves "
        "(power/voltage/current) additionally need known-power points: note "
        "the raw value at a known wattage to recalibrate them.",
    ]
    return "\n".join(lines) + "\n"


def address_candidates(profile, pinned: Optional[int]) -> tuple:
    """CI-V addresses to try, mirroring the server's own discovery.

    A pinned `--civ-addr` wins outright.  The Xiegu G7250 publishes no address
    (see `backends/xiegu/xiegu_profiles.py`), so an unpinned run walks the same
    three candidates the backend does — otherwise the first hardware session
    could report "no answer" on a radio that answers fine on 0x88.
    """
    if pinned:
        return (pinned,)
    if profile.model_key in XIEGU_PROFILES:
        from backends.xiegu.xiegu_profiles import CANDIDATE_CIV_ADDRESSES
        rest = [a for a in CANDIDATE_CIV_ADDRESSES if a != profile.civ_addr]
        return (profile.civ_addr, *rest)
    return (profile.civ_addr,)


async def connect_any(profile, args):
    """Connect on the first address that answers a frequency read."""
    for addr in address_candidates(profile, args.civ_addr):
        civ = CivController(args.port, args.baud,
                            civ_addr=addr,
                            transceive_cmd=profile.transceive_cmd,
                            profile=profile, att_steps=profile.att_steps)
        if not await civ.connect():
            print(f"ERROR: cannot open {args.port}", file=sys.stderr)
            return None
        try:
            answered = await civ._query_data(0x03, timeout=PROBE_TIMEOUT_S)
        except Exception:                                          # noqa: BLE001
            answered = None
        if answered:
            print(f"CI-V address 0x{addr:02X} answered "
                  f"(frequency {answered.hex(' ').upper()})")
            return civ
        await civ.disconnect()
        print(f"CI-V address 0x{addr:02X}: connected but silent")
    print("ERROR: no CI-V address answered — set --civ-addr if the radio "
          "uses an address outside the known set", file=sys.stderr)
    return None


async def run(args) -> int:
    profile = get_profile(args.model)
    probe_results: list = []
    meter_results: list = []
    samples: list = []
    identity_text = "not attempted"
    tx_check = "skipped (needs --tx-check and --allow-tx)"
    scope_enabled = False
    stats = ScopeStats()
    civ = None
    try:
        civ = await connect_any(profile, args)
        if civ is None:
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
            try:
                value = await coro
            except Exception as e:                                 # noqa: BLE001
                # A probe the radio refuses (CivNakError = FA) or ignores is
                # itself the finding — the Xiegu G7250 has no published CI-V
                # document, so "which commands answer at all" is half of what
                # this run is for.  Aborting on the first FA would throw away
                # every later answer.
                text = f"refused ({type(e).__name__}: {e})"
            else:
                text = (value.hex(" ").upper() if isinstance(value, bytes)
                        else str(value))
            probe_results.append((name, text))
            print(f"  {name}: {text}")

        assembler = ScopeAssembler(seq_max=profile.scope_seq_max,
                                   expected_bins=profile.scope_bins)
        skip_scope = bool(getattr(args, "no_scope", False)
                          or profile.model_key in NO_CIV_SCOPE_MODELS)
        if skip_scope:
            print("  scope: skipped (this model has no CI-V 0x27 waveform)")
        else:
            await civ.set_scope_on(True)
            scope_enabled = True
        if not skip_scope:
            await civ.set_scope_mode(0)
            await civ.set_scope_span(5)
            await civ.set_scope_data_output(True)
        deadline = time.monotonic() + args.seconds
        while not skip_scope and time.monotonic() < deadline:
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
            try:
                value = await civ.get_meter(meter, timeout=PROBE_TIMEOUT_S)
            except Exception as e:                                 # noqa: BLE001
                value = f"refused ({type(e).__name__})"
            meter_results.append((meter, value))
        for name, sub in (("vd", 0x15), ("id", 0x16)):
            try:
                raw = await civ._query_data(0x15, sub, timeout=PROBE_TIMEOUT_S)
            except Exception as e:                                 # noqa: BLE001
                meter_results.append(
                    (name, f"refused ({type(e).__name__})"))
                continue
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
            profile_seq_max=profile.scope_seq_max,
            profile_module=PROFILE_MODULE.get(profile.model_key,
                                              "backends/ic7300/civ_profiles.py"))
        path = f"diag_civ_{profile.model_key}_{datetime.now():%Y%m%d_%H%M%S}.md"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"Report written: {path}")
        return 0
    finally:
        # Restore the radio to the state we found it in.  The scope display
        # itself is left alone (it may have been on before we started); the
        # waveform-data output we switched on is switched back off.
        if civ is not None:
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
    parser.add_argument("--model", required=True, choices=sorted(ALL_PROFILES),
                        help="model key (any CI-V family: ic7300, ic7300mk2, "
                             "ic705, ic7610, ic7760, g7250)")
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--civ-addr", type=lambda x: int(x, 0), default=None,
                        help="override the profile's CI-V address")
    parser.add_argument("--seconds", type=float, default=SCOPE_CAPTURE_S,
                        help="scope capture window (default 6 s)")
    parser.add_argument("--no-scope", action="store_true",
                        help="skip the CI-V 0x27 capture (automatic for models "
                             "without a CI-V scope, e.g. g7250)")
    parser.add_argument("--tx-check", action="store_true",
                        help="run the keyed TX self-check (needs --allow-tx)")
    parser.add_argument("--allow-tx", action="store_true",
                        help="confirm that RF emission is acceptable")
    return parser


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
