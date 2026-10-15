"""Does AP (a-priori) decoding buy recall at the weak end, and at what cost?

This is a research probe, not a product path.  It exists because of an
arithmetic result (``docs/ft8-dl-speculative-notes.md``): the classical chain's
recall cliff sits within ~1-2 dB of the finite-blocklength Shannon bound for
FT8's own code rate, so in white noise there is essentially nothing left to
gain -- *unless* the effective information rate is lowered.  AP decoding is the
only classical mechanism that does exactly that, and it is fully plumbed in
this repo (protocol fields -> worker -> binding flags -> ``ft8apset``/``ft8_a7d``/
``ft8_a8d``) while ``ft8/runtime.py`` never enables it.

The probe answers three questions, in this order, because each one gates the
next:

1. **Does AP fire at all?**  ``ap_type != 0`` on a decode.  If AP never fires,
   every later number is meaningless -- so this is checked first, per
   ``qso_progress`` value, rather than assumed from the WSJT-X documentation.
2. **Does it buy recall?**  Same bed, same synthesized signal, same SNR: AP off
   vs AP on.  The interesting region is the cliff (nominal -26..-22 dB).
3. **What does it cost in precision?**  AP is a hypothesis test, not a search,
   so a *wrong* a-priori can produce a confident decode of a message that was
   never sent.  The falsification arm synthesizes a message that contradicts the
   supplied a-priori and counts how often AP invents the expected one instead.

Run it on the host that has the corpus and the native library::

    .venv/bin/python tools/ft8/ap_probe.py --corpus corpus --trials 4

Every number it prints is a count over trials, never an extrapolation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ft8.core.binding import CoreBinding
from ft8.core.models import DecodeConfig, DecodePath
from ft8.core.worker import default_library_path

from .ml_simulate import (  # noqa: F401  (re-exported for callers)
    DT_MAX,
    DT_MIN,
    SyntheticSignal,
    quiet_frequency,
    slot_pcm,
    synthesize,
)

# The deep production config (Task 8d), so the comparison is against what the
# host actually runs -- not against a fast path that would flatter AP.
BASE = dict(
    path=DecodePath.IMPROVED,
    profile=1,
    cycles=3,
    sensitivity=3,
    low_threshold=True,
    threads=4,
)

# A QSO the box could plausibly be in: BA4EG called JA1VAT, JA1VAT answered with
# a grid, and the next expected messages are the report exchange.  Every field
# here is something the runtime already knows -- that is the point.
MY_CALL = "BA4EG"
DX_CALL = "JA1VAT"
DX_GRID = "QN06"

# Three arms, because the interesting comparison is not "AP off vs AP on".
# DecodeConfig defaults to ap=True, so production *today* already claims AP is
# on -- it just never feeds it (my_call/dx_call/dx_grid empty, qso_progress=0,
# rx_frequency=1500 rather than the QSO frequency), so ft8apset gets empty
# strings and iaptype stays 0.  Arm 2 is that state, measured rather than
# assumed.  Arm 3 is the same decoder with the a-priori the runtime already
# knows.
ARMS = (
    ("ap_off", "AP truly disabled", dict(ap=False)),
    (
        "ap_starved",
        "what production does today: ap=True, no a-priori",
        dict(ap=True),
    ),
    (
        "ap_fed",
        "ap=True plus the calls/grid/progress/frequency the box already knows",
        dict(ap=True, my_call=MY_CALL, dx_call=DX_CALL, dx_grid=DX_GRID),
    ),
)

# A message that contradicts the supplied a-priori: the truth is a CQ from
# somebody else, so any decode naming DX_CALL+MY_CALL is an AP invention.
CONTRADICTING = "CQ VK3ABC QF22"
CONSISTENT = "BA4EG JA1VAT -12"


def _config(**overrides: object) -> DecodeConfig:
    return DecodeConfig(**{**BASE, **overrides})  # type: ignore[arg-type]


def _decode(binding: CoreBinding, pcm, config: DecodeConfig, slot_id: int):
    return binding.decode(pcm, config, slot_id)


def _hit(batch, signal: SyntheticSignal) -> tuple[bool, int, float]:
    """Return (found, ap_type, quality) for the synthesized signal's slot."""

    for result in batch.results:
        if (
            abs(result.frequency - signal.frequency) <= 25.0
            and abs(result.dt - signal.dt) <= 1.5
        ):
            return True, int(getattr(result, "ap_type", 0) or 0), float(
                getattr(result, "quality", 1.0) or 1.0
            )
    return False, 0, 0.0


def _invented(batch, signal: SyntheticSignal) -> str | None:
    """Return the text AP produced at the expected frequency, if any.

    In the falsification arm the truth is a CQ from somebody else, so any decode
    that matches the *expected* message is an AP invention.
    """

    for result in batch.results:
        if abs(result.frequency - signal.frequency) <= 25.0:
            return str(result.text)
    return None


def probe(
    binding: CoreBinding,
    beds: list[Path],
    *,
    snr_values: tuple[float, ...],
    trials: int,
    progress_values: tuple[int, ...],
    seed: int = 20261015,
) -> dict[str, object]:
    """Measure the three questions above and return a JSON-ready report."""

    import numpy as np

    generator = np.random.default_rng(seed)
    report: dict[str, object] = {
        "snr_values": list(snr_values),
        "trials": trials,
        "progress_values": list(progress_values),
        "arms": {},
    }

    def run(label: str, message: str, snr: float, progress: int, overrides: dict, *, fed: bool):
        """One (arm, progress, SNR) cell: recall, how often AP fired, inventions."""

        hits = ap_fired = invented = 0
        for trial in range(trials):
            bed = beds[(trial * 7 + int(snr)) % len(beds)]
            pcm_bed = slot_pcm(bed)
            frequency = quiet_frequency(pcm_bed, generator)
            # DT_MIN/DT_MAX come from the simulator, not from a literal here: a
            # hand-written range produced dt=-0.98, which synthesize correctly
            # rejects because the signal would start before the slot.
            dt = float(generator.uniform(DT_MIN, DT_MAX))
            signal = SyntheticSignal(
                message=message, frequency=frequency, dt=dt, snr_db=snr
            )
            pcm = synthesize(binding, pcm_bed, (signal,))
            extra = dict(overrides)
            if fed:
                # a8 decodes at nfqso, so the QSO frequency has to be the one the
                # signal is actually on -- the box knows it, the default 1500 does
                # not.
                extra.update(
                    qso_progress=progress,
                    rx_frequency=int(round(frequency)),
                    tx_frequency=int(round(frequency)),
                )
            slot_id = abs(hash((label, progress, snr, trial))) % 90_000
            batch = _decode(binding, pcm, _config(**extra), slot_id)
            found, ap_type, _ = _hit(batch, signal)
            hits += int(found)
            ap_fired += int(ap_type != 0)
            if message == CONTRADICTING:
                text = _invented(batch, signal)
                if text and DX_CALL in text and MY_CALL in text:
                    invented += 1
        return hits, ap_fired, invented

    for message, purpose in (
        (CONSISTENT, "a-priori matches the air"),
        (CONTRADICTING, "a-priori contradicts the air"),
    ):
        print(f"\n── {message!r}   ({purpose})", flush=True)
        message_report: dict[str, object] = {}
        for label, _note, overrides in ARMS:
            fed = label == "ap_fed"
            for progress in (progress_values if fed else (0,)):
                per_snr: dict[str, list[int]] = {}
                for snr in snr_values:
                    hits, ap_fired, invented = run(
                        label, message, snr, progress, overrides, fed=fed
                    )
                    per_snr[f"{snr:+.0f}"] = [hits, trials, ap_fired, invented]
                    tail = f"   invented: {invented}" if message == CONTRADICTING else ""
                    print(
                        f"  {label:<11} {snr:+5.0f} dB   {hits}/{trials}   "
                        f"ap_type!=0: {ap_fired}   progress={progress}{tail}",
                        flush=True,
                    )
                message_report[f"{label}-p{progress}"] = per_snr
        report["arms"][message] = message_report  # type: ignore[index]

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--slots", type=int, default=12)
    parser.add_argument("--trials", type=int, default=4)
    parser.add_argument("--snr", default="-26,-24,-22")
    parser.add_argument("--progress", default="0,1,2,3,4,5")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--library", default=None)
    args = parser.parse_args(argv)

    beds = sorted(args.corpus.glob("*.raw"))[: args.slots]
    if not beds:
        print(f"no slots under {args.corpus}")
        return 2

    binding = CoreBinding(args.library or default_library_path())
    snr_values = tuple(float(part) for part in args.snr.split(","))
    progress_values = tuple(int(part) for part in args.progress.split(","))
    print(f"beds: {len(beds)} slots   my={MY_CALL} dx={DX_CALL} grid={DX_GRID}")

    report = probe(
        binding,
        beds,
        snr_values=snr_values,
        trials=args.trials,
        progress_values=progress_values,
    )
    if args.json:
        args.json.write_text(json.dumps(report, indent=1, sort_keys=True))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
