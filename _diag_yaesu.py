#!/usr/bin/env python3
"""Read-only field self-check for the Yaesu ASCII-CAT models.

Purpose (spec 2026-09-12 §7): a field owner of an FTDX10 / FTDX101D / FTDX101MP / FTX-1F
runs this against the radio and pastes the report back.  It closes the
`TODO(hw-verify)` gaps the design could not: the `ID;` answer, whether the
documented commands answer at all, and — with an explicit opt-in — whether PTT
and the TX path work.

Nothing is written to the radio unless --tx-check --allow-tx is given, and PTT
is released in a finally block even then.

Usage:
    python3 _diag_yaesu.py --model ftx1 --port /dev/ttyUSB0
    python3 _diag_yaesu.py --model ftdx10 --port COM5 --tx-check --allow-tx
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import List, Optional, Tuple

from backends.yaesu.cat_core import YaesuCatController
from backends.yaesu.yaesu_profiles import get_profile

# (command, timeout) — read-only queries only.
PROBES: Tuple[Tuple[str, float], ...] = (
    ("ID", 0.6), ("FA", 0.6), ("FB", 0.6), ("VS", 0.6), ("MD0", 0.6),
    ("SH0", 0.6), ("TX", 0.6), ("SM0", 0.6), ("PC", 0.6), ("AG0", 0.6),
    ("RG0", 0.6), ("SQ0", 0.6), ("PA0", 0.6), ("RA0", 0.6), ("GT0", 0.6),
)


def evaluate_identity(observed: Optional[str], expected: str,
                      display_name: str) -> Tuple[bool, str]:
    """Compare the observed `ID;` answer with the profile's expectation."""
    if observed is None:
        return False, f"no answer to ID; from {display_name}"
    if not expected:
        return True, (f"model ID observed: {observed} (no expectation recorded "
                      f"for {display_name} yet — please report this value)")
    if observed.upper() == expected.upper():
        return True, f"model ID matches the profile ({observed})"
    return False, (f"model ID mismatch: radio answered {observed}, profile "
                   f"expects {expected} — report this and check the selected model")


def tx_check_permitted(tx_check: bool, allow_tx: bool) -> bool:
    """A key-up needs both switches; either one alone is not enough."""
    return bool(tx_check and allow_tx)


def format_report(**kw) -> str:
    """Paste-ready Markdown report."""
    lines: List[str] = []
    ok = "PASS" if kw["identity_ok"] else "FAIL"
    lines.append(f"# Yaesu field report — {kw['display_name']} (`{kw['model']}`)")
    lines.append("")
    lines.append(f"- port: `{kw['port']}` @ {kw['baud']} baud")
    lines.append(f"- `ID;` observed: **{kw.get('model_id') or 'no answer'}** — {ok}")
    lines.append(f"- identity: {kw['identity_note']}")
    lines.append(f"- TX check: {kw['tx_check']}")
    lines.append("")
    lines.append("| command | answer | ok |")
    lines.append("| --- | --- | --- |")
    for cmd, answer, good in kw["probes"]:
        lines.append(f"| `{cmd}` | `{answer or ''}` | {'yes' if good else '**FAIL**'} |")
    if kw.get("notes"):
        lines.append("")
        lines.append("## Notes")
        for note in kw["notes"]:
            lines.append(f"- {note}")
    return "\n".join(lines) + "\n"


async def run(args) -> int:
    profile = get_profile(args.model)
    ctrl = YaesuCatController(args.port, args.baud, profile=profile)
    notes: List[str] = []
    if not await ctrl.connect():
        print(f"cannot open {args.port}", file=sys.stderr)
        return 2
    try:
        model_id = await ctrl.get_model_id()
        identity_ok, identity_note = evaluate_identity(
            model_id, profile.id_answer, profile.display_name)

        probes: List[Tuple[str, Optional[str], bool]] = []
        for cmd, timeout in PROBES:
            answer = await ctrl.query(cmd, timeout=timeout)
            probes.append((cmd, answer, answer is not None))

        tx_check = "not requested"
        if tx_check_permitted(args.tx_check, args.allow_tx):
            try:
                await ctrl.set_ptt(True)
                await asyncio.sleep(0.2)
                readback = await ctrl.get_ptt(timeout=0.6)
                tx_check = (f"keyed 0.2 s, TX; read back {readback}"
                            if readback == 1 else
                            f"PTT did not report TX (read back {readback})")
            finally:
                await ctrl.set_ptt(False)
                await asyncio.sleep(0.1)
                released = await ctrl.get_ptt(timeout=0.6)
                tx_check += f"; released (TX; = {released})"
        elif args.tx_check:
            tx_check = "refused: --tx-check also needs --allow-tx"

        for meter in profile.unverified_meters:
            notes.append(f"{meter}: no hardware-verified curve yet — compare with "
                         f"the radio's display and report")
        notes.append("audio rate assumption is TODO(hw-verify): the design assumed "
                     "44.1 kHz for this model family")
        print(format_report(model=profile.model_key,
                            display_name=profile.display_name,
                            port=args.port, baud=ctrl.baudrate,
                            model_id=model_id, identity_ok=identity_ok,
                            identity_note=identity_note, probes=probes,
                            tx_check=tx_check, notes=notes))
        return 0 if identity_ok else 1
    finally:
        await ctrl.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True,
                        choices=("ftdx10", "ftdx101d", "ftdx101mp", "ftx1"))
    parser.add_argument("--port", required=True, help="CAT serial port")
    parser.add_argument("--baud", type=int, default=None,
                        help="defaults to the model profile (38400)")
    parser.add_argument("--tx-check", action="store_true",
                        help="key the transmitter briefly (needs --allow-tx)")
    parser.add_argument("--allow-tx", action="store_true",
                        help="explicit consent for the TX check")
    return parser


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
