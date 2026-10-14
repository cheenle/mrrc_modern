"""P3 / Slice 2 — the training pairs, generated rather than stored.

The ML-enhancement track needs (noisy, clean) pairs.  Two decisions shape this
module:

* **Generated on demand, keyed by index.**  A 15 s slot is 360 KB of int16, so a
  useful corpus is gigabytes on disk and stale the moment the STFT parameters
  change.  ``render_slot(index)`` is a pure function of ``(seed, index)`` instead:
  no shards, no manifest, no drift — and the training set can be as large as the
  compute allows.
* **The clean target is the signals we placed, nothing else.**  ``clean`` is the
  same waveforms at the same offsets and the same gains as ``noisy`` (the gain
  law is shared with :mod:`ml_simulate`), with no bed.  That makes the ideal mask
  exact — and it means the bed's own stations are, to the model, noise.  Training
  therefore draws beds from the corpus's *quiet* slots: teaching the model to
  suppress other people's signals would be a real defect, not a statistic.

The representation is the decoder's own: a 6.25 Hz bin is one FT8 tone, so the
mask lives on a grid where the signal is a structure the model can learn rather
than a smear.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_APP_ROOT = Path(__file__).resolve().parents[2]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from ft8.core.binding import CoreBinding
from ft8.core.models import (
    FT8_RX_RATE,
    FT8_RX_SAMPLES,
    DecodeConfig,
    DecodePath,
)
from ft8.core.worker import default_library_path

from tools.ft8 import ml_simulate as M

# 6.25 Hz at 12 kHz: exactly one FT8 tone.  4x overlap keeps the reconstruction
# clean enough that enhancement never becomes the limiting factor.
NFFT = 1920
HOP = 480
BAND_LOW_HZ = 150.0
BAND_HIGH_HZ = 3050.0
_EPS = 1e-9

# The training band: at the cliff and above it.  Slice 1 measured the decoder
# falling off between -24 and -22 dB nominal, so the interesting examples are
# there — not uniformly across a range the decoder already owns.
DEFAULT_SNR_SPAN = (-26.0, -11.0)
DEFAULT_SIGNALS_PER_SLOT = 3
BED_MAX_DECODES = 3


def _window() -> np.ndarray:
    return np.hanning(NFFT + 1)[:-1].astype(np.float32)


_WINDOW = _window()


def frame_count(samples: int = FT8_RX_SAMPLES) -> int:
    """Return the frame count :func:`stft` produces for ``samples`` input samples."""

    return 1 + samples // HOP


def stft(pcm: np.ndarray) -> np.ndarray:
    """Return the short-time spectrum of one slot, shape ``[NFFT//2+1, frames]``.

    The signal is zero-padded by half a window on each side so that every sample
    is covered by a complete window.  Without it the first and last ~1440 samples
    reconstruct with error (measured: 3988 counts peak, against 0.001 inside) —
    and dt = -0.5 places a real signal exactly there.
    """

    values = np.asarray(pcm, dtype=np.float32) / 32768.0
    padded = np.concatenate([np.zeros(NFFT // 2, dtype=np.float32), values,
                             np.zeros(NFFT // 2, dtype=np.float32)])
    count = 1 + (padded.size - NFFT) // HOP
    frames = np.lib.stride_tricks.sliding_window_view(padded, NFFT)[::HOP][:count]
    return np.fft.rfft(frames * _WINDOW, axis=-1).T.astype(np.complex64)


def istft(spectrum: np.ndarray, length: int = FT8_RX_SAMPLES) -> np.ndarray:
    """Invert :func:`stft` by weighted overlap-add, returning int16-scaled floats.

    The window's sum-of-squares is the correct overlap-add normaliser at any hop
    this module uses; with ``HOP = NFFT/4`` the reconstruction error is far below
    the int16 quantum (asserted in the tests).
    """

    frames = np.fft.irfft(spectrum.T, n=NFFT, axis=-1).astype(np.float32)
    total = length + NFFT
    output = np.zeros(total, dtype=np.float64)
    weight = np.zeros(total, dtype=np.float64)
    window_sq = (_WINDOW * _WINDOW).astype(np.float64)
    for index in range(frames.shape[0]):
        start = index * HOP
        if start + NFFT > total:
            break
        output[start : start + NFFT] += frames[index].astype(np.float64) * _WINDOW
        weight[start : start + NFFT] += window_sq
    nonzero = weight > 1e-6
    output[nonzero] /= weight[nonzero]
    # Undo stft's half-window padding.
    trimmed = output[NFFT // 2 : NFFT // 2 + length]
    return (trimmed * 32768.0).astype(np.float32)


@dataclass(frozen=True, slots=True)
class Render:
    """One generated training pair plus the ground truth behind it."""

    index: int
    noisy: np.ndarray
    clean: np.ndarray
    noise: np.ndarray
    signals: tuple[M.SyntheticSignal, ...]
    bed: str


def band_slice() -> slice:
    frequencies = np.fft.rfftfreq(NFFT, 1.0 / FT8_RX_RATE)
    inside = np.flatnonzero((frequencies >= BAND_LOW_HZ) & (frequencies <= BAND_HIGH_HZ))
    return slice(int(inside[0]), int(inside[-1]) + 1)


BAND = band_slice()


def _messages() -> tuple[str, ...]:
    """Return the message pool.

    Real-shaped and callsign-rich: the decoder's false-decode filters and the
    callsign database both behave differently for ``CQ`` than for a report, and a
    pool of one shape would let the model (and the metric) live in a corner.
    """

    return (
        "CQ BA4EG PM00", "CQ JA1VAT QN06", "CQ DL1ABC JO31", "CQ W1AW FN31",
        "CQ VK3ABC QF22", "CQ EA8TL IL18", "CQ BG4GNW PM00", "CQ RA0FZ QN06",
        "BA4EG JA1VAT -12", "JA1VAT BA4EG R-09", "BA4EG JA1VAT RR73",
        "DL1ABC BA4EG -18", "BA4EG DL1ABC R-15", "W1AW BA4EG -20",
        "BA4EG W1AW 73", "BA4EG VK3ABC -16", "VK3ABC BA4EG RR73",
    )


def _notch(pcm: np.ndarray, frequencies_hz: tuple[tuple[float, float, float], ...]) -> np.ndarray:
    """Attenuate known signals in the T-F domain, returning a signal-free bed.

    A bed's own stations are "noise" to the mask target: left alone, they teach
    the model to *suppress other people's signals* — a defect no metric in this
    pipeline would reveal, because the synthetic signals would still be
    reconstructed perfectly.  Attenuating them by ~34 dB keeps the recording's
    real noise character (which is the point of using real beds at all) while
    removing the content that would poison the target.
    """

    if not frequencies_hz:
        return pcm
    spectrum = stft(pcm)
    bin_hz = FT8_RX_RATE / NFFT
    rows = np.fft.rfftfreq(NFFT, 1.0 / FT8_RX_RATE)
    for frequency, dt, snr in frequencies_hz:
        # A decoded-but-weak signal is interference either way; only notch what
        # the decoder actually heard.
        start = int(round((dt + 0.5) * FT8_RX_RATE))
        first = max(start // HOP - 1, 0)
        last = min((start + 151_680) // HOP + 1, spectrum.shape[1])
        inside = (rows >= frequency - 3.0) & (rows <= frequency + M.TONE_GROUP_HZ + 3.0)
        spectrum[np.ix_(inside, np.arange(first, last))] *= 0.02
    del bin_hz
    return np.round(istft(spectrum)).astype(np.int16)


def training_beds(
    corpus: Path, binding: CoreBinding, limit: int | None = None
) -> list[np.ndarray]:
    """Return one usable noise bed per corpus slot, notched where necessary.

    ``quiet-beds.json`` next to the corpus caches the per-slot decode counts, so
    the expensive part (a decode per slot) happens once per corpus.
    """

    cache = corpus / "quiet-beds.json"
    paths = sorted(corpus.glob("*.raw"))
    counts: dict[str, int] = {}
    if cache.exists():
        try:
            counts = {name: int(count) for name, count in json.loads(cache.read_text())["counts"].items()}
        except (KeyError, ValueError):
            counts = {}
    if len(counts) != len(paths):
        counts = {}
        for path in paths:
            raw = np.frombuffer(path.read_bytes(), dtype=np.int16)
            batch = binding.decode(raw, DecodeConfig(path=DecodePath.FAST), 1)
            counts[path.name] = len(batch.results)
        cache.write_text(json.dumps({"max_decodes": BED_MAX_DECODES, "counts": counts}, indent=1))
    beds: list[np.ndarray] = []
    for path in paths[:limit] if limit else paths:
        raw = np.frombuffer(path.read_bytes(), dtype=np.int16)
        if counts.get(path.name, 0) <= BED_MAX_DECODES:
            beds.append(raw.copy())
            continue
        batch = binding.decode(raw, DecodeConfig(path=DecodePath.FAST), 1)
        beds.append(
            _notch(
                raw,
                tuple((result.frequency, result.dt, result.snr) for result in batch.results),
            )
        )
    return beds


def render_slot(
    binding: CoreBinding,
    beds: list[np.ndarray],
    index: int,
    *,
    seed: int = 20261014,
    snr_span: tuple[float, float] = DEFAULT_SNR_SPAN,
    count: int = DEFAULT_SIGNALS_PER_SLOT,
    waves: "M.WaveformBank | None" = None,
) -> Render:
    """Generate training pair ``index``: a pure function of ``(seed, index, beds)``."""

    generator = np.random.default_rng([seed, index])
    bed_index = int(generator.integers(0, len(beds)))
    bed = beds[bed_index]
    messages = _messages()
    signals: list[M.SyntheticSignal] = []
    # Non-overlapping tone groups: two signals 40 Hz apart would fight for the
    # same mask, and the target would encode that fight rather than a signal.
    taken: list[float] = []
    attempts = 0
    while len(signals) < count and attempts < 40:
        attempts += 1
        frequency = M.quiet_frequency(bed, generator)
        if any(abs(frequency - other) < 60.0 for other in taken):
            continue
        taken.append(frequency)
        signals.append(
            M.SyntheticSignal(
                message=str(generator.choice(messages)),
                frequency=frequency,
                dt=float(generator.uniform(M.DT_MIN, M.DT_MAX)),
                snr_db=float(generator.uniform(*snr_span)),
            )
        )
    placed = tuple(signals)
    clean = _clean_component(binding, bed, placed, waves)
    noisy = M.synthesize(binding, bed, placed, waves=waves)
    return Render(index, noisy, clean, bed, placed, f"bed{bed_index:04d}")


def _clean_component(
    binding: CoreBinding,
    bed: np.ndarray,
    signals: tuple[M.SyntheticSignal, ...],
    waves: "M.WaveformBank | None" = None,
) -> np.ndarray:
    """Return only the signals, at the same offsets and gains as the mix."""

    clean = np.zeros(FT8_RX_SAMPLES, dtype=np.float64)
    for signal in signals:
        waveform = (
            waves.waveform(signal.message, signal.frequency)
            if waves is not None
            else M.clean_waveform_12k(binding, signal.message, signal.frequency)
        )
        gain = M.signal_gain(bed, waveform, signal)
        start = signal.offset_samples
        stop = start + waveform.size
        clean[start:stop] += waveform.astype(np.float64) * gain
    return np.clip(np.round(clean), -32768.0, 32767.0).astype(np.int16)


def features(render: Render) -> tuple[np.ndarray, np.ndarray]:
    """Return the model's input and the ideal amplitude mask, both in-band.

    Input is the noisy log-magnitude normalised by its own median, so the model
    sees structure rather than level — a slot recorded 6 dB louder is the same
    problem, and the mask is what generalises across band conditions.
    """

    noisy = stft(render.noisy)[BAND]
    clean = stft(render.clean)[BAND]
    noise = stft(render.noise)[BAND]
    magnitude = np.abs(noisy)
    median = float(np.median(magnitude)) + _EPS
    model_input = np.log(magnitude / median + 1e-3).astype(np.float32)
    target = (np.abs(clean) / (np.abs(clean) + np.abs(noise) + _EPS)).astype(np.float32)
    return model_input, np.clip(target, 0.0, 1.0)


def apply_mask(render_noisy: np.ndarray, mask: np.ndarray, alpha: float = 1.0) -> np.ndarray:
    """Reconstruct a slot with ``mask`` applied in-band and mixed back by ``alpha``.

    ``alpha`` is the noise-floor lever from the design (spec §3.3): the decoder's
    candidate threshold is SNR-dependent, so removing *all* noise changes the
    problem the decoder thinks it is solving.  ``alpha < 1`` keeps a floor.
    """

    spectrum = stft(render_noisy)
    shaped = (1.0 - alpha) + alpha * np.asarray(mask, dtype=np.float32)
    spectrum[BAND] = spectrum[BAND] * shaped
    return istft(spectrum)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P3 Slice 2: generate and inspect training pairs")
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--sample", type=int, default=2, help="how many pairs to render")
    parser.add_argument("--library", default=None)
    parser.add_argument("--save", type=Path, default=None, help="write the first pair as .npz")
    args = parser.parse_args(argv)

    binding = CoreBinding(args.library or default_library_path())
    beds = training_beds(args.corpus, binding)
    raw = sum(bed.astype(np.float64).__abs__().mean() for bed in beds) / max(len(beds), 1)
    print(f"beds: {len(beds)} (slots with >{BED_MAX_DECODES} decodes are notched)  mean |x| {raw:.0f}")
    for index in range(args.sample):
        render = render_slot(binding, beds, index)
        model_input, target = features(render)
        print(
            f"pair {index}: bed {render.bed}  {len(render.signals)} signal(s) "
            f"({', '.join(f'{s.snr_db:.1f} dB @ {s.frequency:.0f} Hz' for s in render.signals)})"
        )
        print(
            f"   input {model_input.shape} [{model_input.min():.2f}, {model_input.max():.2f}]  "
            f"mask mean {target.mean():.3f}  bins>0.5 {100.0 * float((target > 0.5).mean()):.2f}%"
        )
        if args.save and index == 0:
            np.savez_compressed(
                args.save,
                input=model_input,
                target=target,
                noisy=render.noisy.astype(np.int16),
                clean=render.clean.astype(np.int16),
            )
            print(f"   wrote {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
