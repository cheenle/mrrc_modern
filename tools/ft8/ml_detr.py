"""P4 / Slice 1 — the set-prediction data pipeline for a transformer FT8 decoder.

Design: ``docs/superpowers/specs/2026-10-15-ft8-transformer-decoder-design.md``.
The model sees one slot's complex time-frequency plane and predicts a *set* of
signals — for each: presence, frequency, dt, and the **174-bit codeword**.  Two
decisions make that a decoder we can trust rather than hope for:

* **Bits, never text.**  The codeword's first 91 bits are the information bits
  (the FT8 LDPC code is systematic: ``encode174_91.f90:60`` writes
  ``codeword(1:K)=message``), so a prediction can be *checked* — the parity
  relations must hold, and the 77 message bits unpack with the standard rules —
  instead of being believed.  A model that hallucinates a callsign fails the
  check; it cannot invent one.
* **Labels come from the encoder itself.**  The 174 bits are recovered from the
  tones of a clean, freshly encoded burst (exact: no noise, no demodulation
  error), not from a second implementation of the packing rules — a copy of the
  standard is exactly how a training target drifts away from what is on the air.

Slice 1's bar is deliberately low: prove the pipeline and that a small model can
learn the tone structure at all.  The value question is Slice 3, where the same
instrument as P3 — the recall-vs-SNR curve — decides.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_APP_ROOT = Path(__file__).resolve().parents[2]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from ft8.core.binding import CoreBinding
from ft8.core.models import FT8_RX_RATE, FT8_RX_SAMPLES
from ft8.core.worker import default_library_path

from tools.ft8 import ml_dataset as D
from tools.ft8 import ml_simulate as M

# The 7x3 sync pattern and where it sits in the 79 symbols (genft8.f90:14,33-35).
SYNC_SEQUENCE = (3, 1, 4, 0, 6, 5, 2)
SYNC_POSITIONS = tuple(range(0, 7)) + tuple(range(36, 43)) + tuple(range(72, 79))
DATA_POSITIONS = tuple(k for k in range(79) if k not in SYNC_POSITIONS)  # 58 symbols

# Three bits per data symbol, Gray-mapped (genft8.f90:41-42).
GRAYMAP = (0, 1, 3, 2, 5, 6, 4, 7)
CODEWORD_BITS = 174
INFO_BITS = 91
MESSAGE_BITS = 77

SYMBOL_SAMPLES = 1920  # 0.16 s at 12 kHz
TONE_SPACING_HZ = 6.25


def _inverse_gray() -> dict[int, int]:
    return {tone: index for index, tone in enumerate(GRAYMAP)}


def tones_from_waveform(waveform: np.ndarray, frequency: float) -> np.ndarray:
    """Recover the 79 tones from a *clean* burst, exactly.

    Exact only because the input is noise-free: a discrete Fourier coefficient is
    taken at each of the eight tone frequencies per symbol and the largest wins.
    On a noisy burst this would be the classical demodulator — which is the
    thing being replaced — so it is never used to build a label from anything
    but the encoder's own output.
    """

    values = np.asarray(waveform, dtype=np.float64)
    needed = 79 * SYMBOL_SAMPLES
    if values.size < needed:
        raise ValueError(f"a burst is 79 symbols ({needed} samples), got {values.size}")
    tones = np.empty(79, dtype=np.int16)
    sample_index = np.arange(SYMBOL_SAMPLES)
    for symbol in range(79):
        block = values[symbol * SYMBOL_SAMPLES : (symbol + 1) * SYMBOL_SAMPLES]
        best_tone, best_power = 0, -1.0
        for tone in range(8):
            phase = 2.0 * np.pi * (frequency + tone * TONE_SPACING_HZ) * sample_index / FT8_RX_RATE
            power = abs(complex(np.dot(block, np.exp(-1j * phase))))
            if power > best_power:
                best_tone, best_power = tone, power
        tones[symbol] = best_tone
    return tones


def codeword_from_tones(tones: np.ndarray) -> np.ndarray:
    """Invert the Gray mapping: 58 data tones -> 174 bits, in order."""

    inverse = _inverse_gray()
    bits = np.zeros(CODEWORD_BITS, dtype=np.uint8)
    for index, position in enumerate(DATA_POSITIONS):
        symbol = inverse[int(tones[position])]
        for offset in range(3):
            bits[3 * index + offset] = (symbol >> (2 - offset)) & 1
    return bits


def tones_from_codeword(codeword: np.ndarray) -> np.ndarray:
    """The inverse of :func:`codeword_from_tones` — used to verify labels."""

    bits = np.asarray(codeword, dtype=np.int64).reshape(-1)
    if bits.size != CODEWORD_BITS:
        raise ValueError(f"a codeword is {CODEWORD_BITS} bits, got {bits.size}")
    tones = np.empty(79, dtype=np.int16)
    for index, position in enumerate(SYNC_POSITIONS):
        tones[position] = SYNC_SEQUENCE[index % 7]
    for index, position in enumerate(DATA_POSITIONS):
        value = int(bits[3 * index]) * 4 + int(bits[3 * index + 1]) * 2 + int(bits[3 * index + 2])
        tones[position] = GRAYMAP[value]
    return tones


def codeword_for(binding: CoreBinding, message: str, frequency: float = 1000.0) -> np.ndarray:
    """Return the 174-bit codeword the encoder transmits for ``message``."""

    waveform = M.clean_waveform_12k(binding, message, frequency)
    tones = tones_from_waveform(waveform, frequency)
    if not np.array_equal(tones_from_codeword(codeword_from_tones(tones)), tones):
        raise AssertionError(f"tone/codeword mapping is not invertible for {message!r}")
    return codeword_from_tones(tones)


@dataclass(frozen=True, slots=True)
class Target:
    """One signal the model must find and decode."""

    frequency: float
    dt: float
    snr_db: float
    codeword: np.ndarray
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "frequency": round(self.frequency, 2),
            "dt": round(self.dt, 3),
            "snr_db": round(self.snr_db, 1),
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class DetrSample:
    """One synthetic slot: the model's input and the set of signals in it."""

    index: int
    features: np.ndarray  # [3, F, T]: log magnitude + unit phase (cos, sin)
    targets: tuple[Target, ...]
    bed: str


def features(pcm: np.ndarray) -> np.ndarray:
    """Return the in-band complex plane as three real channels.

    Phase is kept — at the SNRs this track exists for, the判决 comes from coherent
    integration, and discarding phase would throw away half the available energy.
    """

    spectrum = D.stft(pcm)[D.BAND]
    magnitude = np.abs(spectrum)
    median = float(np.median(magnitude)) + 1e-9
    phase = spectrum / np.maximum(magnitude, 1e-9)
    return np.stack(
        [
            np.log(magnitude / median + 1e-3),
            phase.real.astype(np.float32),
            phase.imag.astype(np.float32),
        ]
    ).astype(np.float32)


def sample_for(
    binding: CoreBinding,
    beds: list[D.Bed],
    index: int,
    *,
    seed: int = 424242,
    count: int = 1,
    snr_span: tuple[float, float] = (-26.0, -11.0),
    waves: "M.WaveformBank | None" = None,
) -> DetrSample:
    """Generate training sample ``index`` — a pure function of ``(seed, index, beds)``."""

    generator = np.random.default_rng([seed, index])
    bed_index = int(generator.integers(0, len(beds)))
    bed = beds[bed_index]
    messages = D._messages()
    signals: list[M.SyntheticSignal] = []
    targets: list[Target] = []
    taken: list[float] = []
    attempts = 0
    while len(signals) < count and attempts < 60:
        attempts += 1
        frequency = M.quiet_frequency(bed.noise, generator)
        if any(abs(frequency - other) < 60.0 for other in taken):
            continue
        taken.append(frequency)
        message = str(generator.choice(messages))
        signal = M.SyntheticSignal(
            message=message,
            frequency=frequency,
            # dt quantised to the frame grid (40 ms).  The STFT's frames are a
            # fixed grid while a symbol is four frames long, so an arbitrary dt
            # makes every 4-frame group straddle two symbols — measured: even at
            # +10 dB a plain argmax over the tokens found 0 of 58 tones, which
            # says nothing about the model and everything about the grid.
            # Slice 1 is the well-posed probe; removing this (a dt hint plus a
            # one-frame token grid) is Slice 2's job, not a silent assumption.
            dt=float(generator.integers(int(M.DT_MIN / 0.04), int(M.DT_MAX / 0.04) + 1) * 0.04),
            snr_db=float(generator.uniform(*snr_span)),
            variant=int(generator.integers(1, 2**31 - 1)),
        )
        signals.append(signal)
        targets.append(
            Target(
                frequency=frequency,
                dt=signal.dt,
                snr_db=signal.snr_db,
                codeword=codeword_for(binding, message),
                message=message,
            )
        )
    pcm = M.synthesize(binding, bed.pcm, tuple(signals), waves=waves)
    return DetrSample(index, features(pcm), tuple(targets), f"bed{bed_index:04d}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P4 Slice 1: set-prediction samples")
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--sample", type=int, default=2)
    parser.add_argument("--signals", type=int, default=2)
    parser.add_argument("--library", default=None)
    args = parser.parse_args(argv)

    binding = CoreBinding(args.library or default_library_path())
    beds = D.training_beds(args.corpus, binding, limit=32)
    waves = M.WaveformBank(binding, D._messages())
    for index in range(args.sample):
        sample = sample_for(binding, beds, index, count=args.signals, waves=waves)
        print(f"sample {index}: 特征 {sample.features.shape}  信号 {len(sample.targets)} 个")
        for target in sample.targets:
            ones = int(np.asarray(target.codeword).sum())
            print(
                f"   {target.message:<20} f={target.frequency:7.1f} dt={target.dt:+.2f} "
                f"snr={target.snr_db:+.1f} dB  码字 174 位（{ones} 个 1，"
                f"校验位 {int(np.asarray(target.codeword)[91:].sum())}/83）"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# --- Slice 1 model: the "given a candidate, decode it" probe ---------------
#
# This deliberately does *not* answer the value question: with the signal
# already found and aligned, the classical chain is a matched filter plus an
# LDPC decoder, and both are near-optimal in white noise.  What it answers is
# whether a transformer can learn the code's structure from examples at all —
# the thing every later slice depends on and the cheapest one to falsify.

CROP_HALF_WIDTH_HZ = 50.0
DATA_POSITIONS_TENSOR = None  # filled on first use; torch is optional at import
# One burst: 79 symbols of 0.16 s, at 40 ms per frame.
BURST_FRAMES = 79 * (SYMBOL_SAMPLES // D.HOP)


def crop(
    features: np.ndarray,
    frequency: float,
    dt: float | None = None,
    half_width_hz: float = CROP_HALF_WIDTH_HZ,
) -> np.ndarray:
    """Return the time-frequency window a signal occupies, aligned to it.

    Both alignments matter and for the same reason: this slice's probe is handed
    a *candidate* (the classical demodulator is handed exactly the same thing by
    its search), so leaving dt random would ask the model to solve the search as
    well — and misaligned symbols are what the token grid reads.  Measured: with
    a random dt the probe sat at ~65% bit accuracy after 400 steps, which says
    nothing about the architecture and everything about the experiment.
    """

    frequencies = np.fft.rfftfreq(D.NFFT, 1.0 / FT8_RX_RATE)[D.BAND]
    if dt is not None:
        start = int(round((dt + 0.5) * FT8_RX_RATE))
        features = features[:, :, max(0, int(round(start / D.HOP))) :]
    centre = int(np.argmin(np.abs(frequencies - frequency)))
    half = int(round(half_width_hz / (frequencies[1] - frequencies[0])))
    low, high = max(centre - half, 0), min(centre + half + 1, features.shape[1])
    window = features[:, low:high, :]
    # Pad rather than shift when the signal sits at the band edge: a shifted
    # window would move the tones and turn "decode" into "blind deconvolution".
    if window.shape[1] < 2 * half + 1:
        window = np.pad(window, ((0, 0), (0, 2 * half + 1 - window.shape[1]), (0, 0)))
    # A fixed window, so batches stack: the burst the encoder produced, padded
    # with noise-free zeros if the slot ends first (only very late signals).
    if window.shape[2] < BURST_FRAMES:
        window = np.pad(window, ((0, 0), (0, 0), (0, BURST_FRAMES - window.shape[2])))
    return np.ascontiguousarray(window[:, :, :BURST_FRAMES])


def build_probe_model(d_model: int = 192, layers: int = 4, heads: int = 6):
    """A small conv+transformer that maps a candidate's plane to 174 bit logits.

    The frequency axis is *collapsed* before attention, so the sequence the
    transformer reads is the symbol sequence — which is where the code's
    structure lives (58 data symbols, 21 sync) — instead of a flattened
    frequency x time grid, which cost 93 ms a batch for no extra information.
    """

    import torch
    import torch.nn.functional as F

    class Probe(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.stem = torch.nn.Sequential(
                torch.nn.Conv2d(3, 48, 3, padding=1), torch.nn.GELU(),
                torch.nn.Conv2d(48, d_model, 3, padding=1), torch.nn.GELU(),
            )
            # One token per symbol: collapse frequency, then pool time by the
            # frames a 0.16 s symbol occupies (1920 samples / 480 hop = 4).
            self.to_token = torch.nn.Conv2d(d_model, d_model, (1, 4), stride=(1, 4))
            self.collapse = torch.nn.Linear(d_model, d_model)
            encoder_layer = torch.nn.TransformerEncoderLayer(
                d_model, heads, dim_feedforward=4 * d_model, batch_first=True,
                dropout=0.0, activation="gelu", norm_first=True,
            )
            self.encoder = torch.nn.TransformerEncoder(encoder_layer, layers)
            # Per-symbol head: three bits per symbol, at that symbol's token.
            # A pooled head predicts 174 bits from one vector, which throws away
            # *which* symbol each bit came from — measured: it stalled at 65% bit
            # accuracy with a pipeline whose sync pattern reads back 79/79.
            self.head = torch.nn.Sequential(
                torch.nn.LayerNorm(d_model), torch.nn.Linear(d_model, 3)
            )

        def forward(self, x):  # type: ignore[no-untyped-def]
            h = self.to_token(self.stem(x))                     # [B, d, F, T']
            h = h.mean(dim=2).transpose(1, 2)                   # [B, T', d]
            h = self.encoder(self.collapse(h))
            return self.head(h)  # [B, symbols, 3]

    return Probe()


def data_bit_targets(codeword: np.ndarray) -> np.ndarray:
    """The 174 bits, as 58 groups of three, in symbol order."""

    bits = np.asarray(codeword, dtype=np.float32)
    return bits.reshape(len(DATA_POSITIONS), 3)


def data_positions_tensor():
    """The symbol positions the loss applies to — the sync symbols are known."""

    return np.asarray(DATA_POSITIONS, dtype=np.int64)


def probe_batch(
    binding: CoreBinding,
    beds: list[D.Bed],
    waves: "M.WaveformBank",
    indices: list[int],
    *,
    seed: int,
    snr_span: tuple[float, float],
) -> tuple[np.ndarray, np.ndarray]:
    """Render aligned candidates and their codewords — the probe's training pair."""

    inputs, targets = [], []
    for index in indices:
        sample = sample_for(binding, beds, index, seed=seed, count=1,
                            snr_span=snr_span, waves=waves)
        target = sample.targets[0]
        inputs.append(crop(sample.features, target.frequency, target.dt))
        targets.append(data_bit_targets(target.codeword))
    return np.stack(inputs), np.stack(targets).astype(np.float32)


def train_probe(
    binding: CoreBinding,
    beds: list[D.Bed],
    waves: "M.WaveformBank",
    *,
    steps: int,
    batch: int,
    lr: float = 3e-4,
    seed: int = 111,
    snr_span: tuple[float, float] = (-12.0, 0.0),
    log_every: int = 25,
    out: Path | None = None,
) -> dict[str, object]:
    """Slice 1's gate: can the network learn the code at all?"""

    import torch

    global DATA_POSITIONS_TENSOR
    if DATA_POSITIONS_TENSOR is None:
        DATA_POSITIONS_TENSOR = torch.from_numpy(data_positions_tensor())
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = build_probe_model().to(device)
    optimiser = torch.optim.Adam(model.parameters(), lr=lr)
    generator = np.random.default_rng(seed)
    history: list[dict[str, float]] = []
    model.train()
    for step in range(1, steps + 1):
        indices = [int(value) for value in generator.integers(0, 10**7, batch)]
        inputs, targets = probe_batch(binding, beds, waves, indices, seed=seed,
                                      snr_span=snr_span)
        x = torch.from_numpy(inputs).to(device)
        y = torch.from_numpy(targets).to(device)
        logits = model(x)[:, DATA_POSITIONS_TENSOR.to(device), :]
        loss = torch.nn.functional.binary_cross_entropy_with_logits(logits, y)
        optimiser.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
        optimiser.step()
        if step % log_every == 0 or step == steps:
            with torch.no_grad():
                predicted = (logits > 0).float()
                bit_accuracy = float((predicted == y).float().mean())
                exact = float(
                    (predicted == y).reshape(predicted.shape[0], -1).all(dim=1).float().mean()
                )
            history.append(
                {"step": step, "loss": float(loss.detach().cpu()),
                 "bit_accuracy": bit_accuracy, "exact_rate": exact}
            )
            print(f"  step {step:>5}/{steps}  loss {float(loss.detach().cpu()):.4f}  "
                  f"比特准确率 {bit_accuracy:.4f}  整码字全对 {exact:.3f}", flush=True)
    if out is not None:
        torch.save({"model": model.state_dict(), "history": history}, out)
        print(f"wrote {out}")
    return {"device": device, "history": history}
