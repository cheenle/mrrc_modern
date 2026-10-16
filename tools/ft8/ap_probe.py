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
import sys
from pathlib import Path

# Runnable both as `python tools/ft8/ap_probe.py` and `python -m tools.ft8.ap_probe`
# (the same bootstrap ml_detr.py uses): a direct script run puts tools/ft8 on
# sys.path, not the repo root, so `import ft8` would fail.
_APP_ROOT = Path(__file__).resolve().parents[2]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from ft8.core.binding import CoreBinding
from ft8.core.models import DecodeConfig, DecodePath
from ft8.core.worker import default_library_path

from tools.ft8.ml_simulate import (  # noqa: F401  (re-exported for callers)
    DT_MAX,
    DT_MIN,
    DT_TOLERANCE,
    FREQUENCY_TOLERANCE,
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
    """Return (found, ap_type, quality) for a decode that *is* the synthesized signal.

    The truth is known here, so the criterion is the text, not the neighbourhood:
    a real bed holds other stations, and a hand-written 25 Hz / 1.5 s window
    credited their decodes to our signal (that is how the first paired run
    reported an impossible 4/20 at -30 dB).  Tolerances come from ml_simulate,
    which is where they were validated against the decoder's own estimates.
    """

    for result in batch.results:
        if (
            abs(result.frequency - signal.frequency) <= FREQUENCY_TOLERANCE
            and abs(result.dt - signal.dt) <= DT_TOLERANCE
            and str(result.text).strip() == signal.message
        ):
            return True, int(getattr(result, "ap_type", 0) or 0), float(
                getattr(result, "quality", 1.0) or 1.0
            )
    return False, 0, 0.0


def _wrong_text_at_signal(batch, signal: SyntheticSignal) -> str:
    """Return the text of a decode sitting on our (freq, dt) that is *not* our message.

    This is the AP false-decode detector: a8 picks the best of ~127 hypotheses,
    so it can return a neighbouring report value that still passes CRC.  An
    empty string means nothing wrong was produced at our location.
    """

    for result in batch.results:
        if (
            abs(result.frequency - signal.frequency) <= FREQUENCY_TOLERANCE
            and abs(result.dt - signal.dt) <= DT_TOLERANCE
            and str(result.text).strip() != signal.message
        ):
            return str(result.text).strip()
    return ""


def _invented(batch, signal: SyntheticSignal) -> str | None:
    """Return the text AP produced at the expected frequency, if any.

    In the falsification arm the truth is a CQ from somebody else, so any decode
    that matches the *expected* message is an AP invention.
    """

    for result in batch.results:
        if (
            abs(result.frequency - signal.frequency) <= FREQUENCY_TOLERANCE
            and abs(result.dt - signal.dt) <= DT_TOLERANCE
        ):
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
            # Per-trial generator; see the note in paired_probe for why a shared
            # stream makes runs with different --snr lists incomparable.
            rng = np.random.default_rng([seed, int(snr) + 1_000, trial])
            frequency = quiet_frequency(pcm_bed, rng)
            # DT_MIN/DT_MAX come from the simulator, not from a literal here: a
            # hand-written range produced dt=-0.98, which synthesize correctly
            # rejects because the signal would start before the slot.
            dt = float(rng.uniform(DT_MIN, DT_MAX))
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
    parser.add_argument(
        "--paired",
        action="store_true",
        help="P5 measurement: same PCM decoded AP-off then AP-fed, dt inside a8's reach",
    )
    parser.add_argument("--paired-progress", type=int, default=2)
    parser.add_argument(
        "--stale",
        action="store_true",
        help="precision arm: feed a QSO a-priori while the air carries something else",
    )
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--library", default=None)
    args = parser.parse_args(argv)

    beds = sorted(args.corpus.glob("*.raw"))[: args.slots]
    if not beds:
        print(f"no slots under {args.corpus}")
        return 2

    binding = CoreBinding(args.library or default_library_path())
    snr_values = tuple(float(part) for part in args.snr.split(","))
    print(f"beds: {len(beds)} slots   my={MY_CALL} dx={DX_CALL} grid={DX_GRID}")

    if args.paired or args.stale:
        if args.stale:
            report = stale_probe(
                binding,
                beds,
                snr_values=snr_values,
                trials=args.trials,
                progress=args.paired_progress,
            )
        else:
            report = paired_probe(
                binding,
                beds,
                snr_values=snr_values,
                trials=args.trials,
                progress=args.paired_progress,
            )
        if args.json:
            args.json.write_text(json.dumps(report, indent=1, sort_keys=True))
            print(f"\nwrote {args.json}")
        return 0

    progress_values = tuple(int(part) for part in args.progress.split(","))

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



# ---------------------------------------------------------------------------
# P5: the paired measurement
# ---------------------------------------------------------------------------
#
# The first probe (above) scattered dt uniformly over [DT_MIN, DT_MAX] = [0, 2.5]
# and found no AP gain.  Reading ft8_a8d explains why that measurement could not
# have seen anything: a8 searches lag in [-200, +200] at its 200 Hz internal rate
# with `j = i + lag + 100`, i.e. it only reaches dt in [-0.5, +1.5] s.  Roughly
# 40% of those trials placed the signal outside the one AP path able to find a
# *new* message.
#
# getmsg() (vendor/ft8/ft8_a7.f90:377) also bounds the hypothesis space: base
# "mycall hiscall" plus RRR/RR73/73, "CQ hiscall hisgrid", "mycall hiscall grid",
# and reports isnr = -50 + (i-7)/2 for i = 7..206 with `abs(isnr) > 30` dropped
# -- about 127 live messages, log2(127) ~ 7.0 bits.  That is the arithmetic of
# docs/ft8-dl-speculative-notes.md section 2, read off the source instead of
# estimated.
#
# This second measurement is paired: the *same* PCM is decoded AP-off and then
# AP-fed, so the comparison cannot be confounded by which bed a trial drew.  It
# also checks the decoded TEXT, because a8 picks the best of ~127 hypotheses and
# can therefore return a *different* report value that still passes CRC -- the
# one failure mode that would make AP dangerous rather than merely useless.
A8_MESSAGE = "BA4EG JA1VAT -12"  # getmsg i=83: isnr = -50 + (83-7)/2 = -12
A8_DT_MAX = 1.4  # inside a8's [-0.5, +1.5] s reach, with margin


def paired_probe(
    binding: CoreBinding,
    beds: list[Path],
    *,
    snr_values: tuple[float, ...],
    trials: int,
    progress: int = 2,
    seed: int = 20261016,
) -> dict[str, object]:
    """Decode each slot twice -- AP off, then AP fed -- and report the pairing.

    The number that matters is ``gained / ap_off_missed``: a8 is suppressed by
    ``clear_a8_near_rx`` whenever anything decodes within 3 Hz of rx_frequency,
    so it can only ever help on the slots the classical path missed.  Measuring
    it over all slots dilutes the effect by the classical path's own success
    rate, which is what the first probe did.
    """

    import numpy as np

    report: dict[str, object] = {
        "message": A8_MESSAGE,
        "dt_max": A8_DT_MAX,
        "progress": progress,
        "trials": trials,
        "by_snr": {},
    }

    print(f"paired probe: {A8_MESSAGE!r}  dt<= {A8_DT_MAX}  progress={progress}")
    for snr in snr_values:
        both = only_off = only_fed = neither = 0
        ap8 = 0
        wrong_text = 0
        gained_texts: list[str] = []
        # P5b instrumentation.  §4.2 could only say "13 gains, 3 of them carry
        # ap_type=8, so at least 10 came from something else" -- inferring a
        # mechanism by subtraction is not evidence.  Record the actual ap_type
        # and quality of every gained decode, because the two questions that
        # decide what to do next both need them: which AP path does the work
        # (notes §6.1), and is `quality` usable as a gate (§7.1 criterion 3).
        gained_ap_types: dict[int, int] = {}
        gained_qualities: list[list[float]] = []
        both_ap_types: dict[int, int] = {}
        for trial in range(trials):
            # A fresh generator per (snr, trial), not one shared stream.  With a
            # shared stream, *how many SNR levels you ask for* shifts every later
            # draw -- so a 2-level run and a 4-level run sample different beds and
            # cannot be compared (that is exactly what made the progress scan in
            # notes §4.5 non-comparable to the run above it).  Deriving the seed
            # from (seed, snr, trial) makes one cell reproducible on its own,
            # which is what the isolated-process check in §6(a) needs.
            rng = np.random.default_rng([seed, int(snr) + 1_000, trial])
            bed = beds[(trial * 7 + int(snr)) % len(beds)]
            pcm_bed = slot_pcm(bed)
            frequency = quiet_frequency(pcm_bed, rng)
            dt = float(rng.uniform(DT_MIN, A8_DT_MAX))
            signal = SyntheticSignal(
                message=A8_MESSAGE, frequency=frequency, dt=dt, snr_db=snr
            )
            pcm = synthesize(binding, pcm_bed, (signal,))
            slot_id = abs(hash((snr, trial))) % 90_000

            off = _hit(
                _decode(binding, pcm, _config(ap=False), slot_id), signal
            )
            fed_batch = _decode(
                binding,
                pcm,
                _config(
                    ap=True,
                    my_call=MY_CALL,
                    dx_call=DX_CALL,
                    dx_grid=DX_GRID,
                    qso_progress=progress,
                    rx_frequency=int(round(frequency)),
                    tx_frequency=int(round(frequency)),
                ),
                slot_id + 1,
            )
            fed = _hit(fed_batch, signal)

            both += int(off[0] and fed[0])
            only_off += int(off[0] and not fed[0])
            only_fed += int(fed[0] and not off[0])
            neither += int(not off[0] and not fed[0])
            if fed[1] == 8:
                ap8 += 1
            if fed[0]:
                bucket = gained_ap_types if not off[0] else both_ap_types
                bucket[fed[1]] = bucket.get(fed[1], 0) + 1
                if not off[0]:
                    gained_qualities.append([float(fed[1]), round(float(fed[2]), 4)])
            # Precision: a8 chooses the best of ~127 hypotheses, so a neighbouring
            # report ("-14" for "-12") still passes CRC.  Count those separately
            # from the gains, and keep the offending text for inspection.
            bad = _wrong_text_at_signal(fed_batch, signal)
            if bad:
                wrong_text += 1
                gained_texts.append(bad if not off[0] else f"(off also had it) {bad}")

        missed = only_fed + neither
        types = ", ".join(f"ap{k}:{v}" for k, v in sorted(gained_ap_types.items()))
        print(
            f"  {snr:+5.0f} dB   both {both:>3}  only_off {only_off:>3}  "
            f"only_fed {only_fed:>3}  neither {neither:>3}   "
            f"ap_type=8: {ap8:>3}   wrong_text: {wrong_text:>3}"
            + (f"   [{', '.join(gained_texts[:3])}]" if gained_texts else "")
            + (f"\n{'':11}赚到的 ap_type 分布: {types or '—'}"
               f"   both 的分布: "
               f"{', '.join(f'ap{k}:{v}' for k, v in sorted(both_ap_types.items())) or '—'}"),
            flush=True,
        )
        report["by_snr"][f"{snr:+.0f}"] = {  # type: ignore[index]
            "both": both,
            "only_off": only_off,
            "only_fed": only_fed,
            "neither": neither,
            "ap8": ap8,
            "wrong_text": wrong_text,
            "gained_over_missed": f"{only_fed}/{missed}",
            "gained_texts": gained_texts,
            "gained_ap_types": gained_ap_types,
            "gained_ap_type_quality": gained_qualities,
            "both_ap_types": both_ap_types,
        }

    return report



# ---------------------------------------------------------------------------
# P5 precision arm: what happens when the a-priori is stale
# ---------------------------------------------------------------------------
#
# docs/ft8-dl-speculative-notes.md section 7.1 refuses to ship AP until this is
# measured, and section 8 lists it as the biggest untested gap.  The two earlier
# runs both had a *consistent* a-priori (run 1) or a *contradicting signal*
# (run 2: the air carried somebody else's CQ).  Neither covers the case that
# actually happens on air: the a-priori is correct for a QSO that is no longer
# happening -- the partner changed frequency, the QSO ended, or the grid came
# from a decode many slots old.
#
# So: synthesize an UNRELATED message at an unrelated frequency, while feeding
# the decoder my_call/dx_call/dx_grid as if a QSO were in progress.  The
# question is not "does AP decode the unrelated message" (it should, it is a
# normal decode) but "does AP ALSO invent the expected QSO message?" -- because
# an invented message enters the sequencer, and the sequencer keys the radio.
STALE_CASES = (
    # (what is actually on the air, what the a-priori claims)
    ("CQ VK3ABC QF22", "an unrelated CQ from another station"),
    ("JA1VAT BA4EG R-09", "the partner, but a *later* QSO stage than claimed"),
    ("BA4EG JA1VAT QN06", "the partner's grid message, not the report we expect"),
)


def stale_probe(
    binding: CoreBinding,
    beds: list[Path],
    *,
    snr_values: tuple[float, ...],
    trials: int,
    progress: int = 2,
    seed: int = 20261017,
) -> dict[str, object]:
    """Feed a QSO a-priori while the air carries something else, and count inventions.

    An invention is a decode whose text is one of a8's 128 hypotheses (i.e. it
    names MY_CALL and DX_CALL) at a place where no such signal was synthesized.
    That is the failure mode that matters: it is a message nobody sent, produced
    because we told the decoder to expect it.
    """

    import numpy as np

    generator = np.random.default_rng(seed)
    report: dict[str, object] = {"progress": progress, "trials": trials, "cases": {}}

    for message, note in STALE_CASES:
        print(f"\n── stale a-priori: air carries {message!r}  ({note})", flush=True)
        case: dict[str, object] = {"note": note}
        for snr in snr_values:
            truth_hits = inventions = ap8 = 0
            invented_texts: list[str] = []
            for trial in range(trials):
                bed = beds[(trial * 7 + int(snr)) % len(beds)]
                pcm_bed = slot_pcm(bed)
                # The real signal sits somewhere AP was NOT told to look, so any
                # decode naming both calls is an invention rather than a lucky hit.
                rng = np.random.default_rng([seed, int(snr) + 1_000, trial])
                frequency = quiet_frequency(pcm_bed, rng)
                decoy = frequency + 800.0 if frequency < 2000.0 else frequency - 800.0
                dt = float(rng.uniform(DT_MIN, A8_DT_MAX))
                signal = SyntheticSignal(
                    message=message, frequency=decoy, dt=dt, snr_db=snr
                )
                pcm = synthesize(binding, pcm_bed, (signal,))
                slot_id = abs(hash((message, snr, trial))) % 90_000
                batch = _decode(
                    binding,
                    pcm,
                    _config(
                        ap=True,
                        my_call=MY_CALL,
                        dx_call=DX_CALL,
                        dx_grid=DX_GRID,
                        qso_progress=progress,
                        rx_frequency=int(round(frequency)),
                        tx_frequency=int(round(frequency)),
                    ),
                    slot_id,
                )
                if _hit(batch, signal)[0]:
                    truth_hits += 1
                for result in batch.results:
                    if getattr(result, "ap_type", 0) == 8:
                        ap8 += 1
                    text = str(result.text).strip()
                    # An invention names both calls but was never synthesized.
                    if MY_CALL in text and DX_CALL in text and text != message:
                        inventions += 1
                        if text not in invented_texts:
                            invented_texts.append(text)
                        break
            print(
                f"  {snr:+5.0f} dB   truth {truth_hits:>2}/{trials}   "
                f"inventions {inventions:>2}   ap_type=8 {ap8:>2}"
                + (f"   {invented_texts[:2]}" if invented_texts else ""),
                flush=True,
            )
            case[f"{snr:+.0f}"] = {  # type: ignore[index]
                "truth_hits": truth_hits,
                "inventions": inventions,
                "ap8": ap8,
                "invented_texts": invented_texts,
            }
        report["cases"][message] = case  # type: ignore[index]

    return report


if __name__ == "__main__":
    raise SystemExit(main())
