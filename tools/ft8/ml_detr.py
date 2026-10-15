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
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_APP_ROOT = Path(__file__).resolve().parents[2]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from ft8.core.binding import CoreBinding
from ft8.core.models import DecodeConfig, DecodePath, FT8_RX_RATE, FT8_RX_SAMPLES
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


# Encoding a waveform and reading its tones back is ~20-40 ms, and a training
# run asks for the same handful of messages millions of times.  The label of a
# message does not depend on the slot, so it is computed once.
_CODEWORD_CACHE: dict[tuple[str, float], np.ndarray] = {}


def codeword_for(binding: CoreBinding, message: str, frequency: float = 1000.0) -> np.ndarray:
    """Return the 174-bit codeword the encoder transmits for ``message``."""

    key = (message, frequency)
    cached = _CODEWORD_CACHE.get(key)
    if cached is not None:
        return cached
    waveform = M.clean_waveform_12k(binding, message, frequency)
    tones = tones_from_waveform(waveform, frequency)
    if not np.array_equal(tones_from_codeword(codeword_from_tones(tones)), tones):
        raise AssertionError(f"tone/codeword mapping is not invertible for {message!r}")
    codeword = codeword_from_tones(tones)
    _CODEWORD_CACHE[key] = codeword
    return codeword


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
    pcm: np.ndarray = None  # type: ignore[assignment]  # the slot itself, for A/B


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
    return DetrSample(index, features(pcm), tuple(targets), f"bed{bed_index:04d}", pcm)


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


# --- S2: the training loop, with the CPU work kept off the critical path ---
#
# Measured before this existed: data generation 46 ms/sample single-threaded
# against a 161 ms MPS step for a batch of 16 — the CPU was 4.5x the model, so a
# rented GPU would have idled.  A thread pool is enough here (no fork, no
# pickling of ctypes handles) because every heavy call releases the GIL: the
# encoder through ctypes, the rest through numpy.


def prefetch(build, index_batches, *, window: int = 4, workers: int = 8):
    """Yield ``build(indices)`` results in order, generated on ``workers`` threads."""

    from collections import deque
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending: deque = deque()
        for indices in index_batches:
            pending.append(pool.submit(build, indices))
            if len(pending) > window:
                yield pending.popleft().result()
        while pending:
            yield pending.popleft().result()


# The SNR schedule: start where the decoder is comfortable and walk down.  A
# model trained only at the cliff has nothing to learn from (nearly every label
# is wrong and the gradient says nothing); one trained only at 0 dB never sees
# the regime it exists for.
def snr_schedule(steps: int, *, easy: float = -4.0, hard: float = -22.0) -> list[tuple[float, float]]:
    """A per-step (low, high) SNR draw that anneals from easy to the cliff."""

    schedule = []
    for step in range(steps):
        centre = easy + (hard - easy) * min(1.0, step / max(1, steps * 0.7))
        schedule.append((centre - 2.0, centre + 2.0))
    return schedule


def _codeword_logits(data_logits, device):
    """Scatter the per-symbol (58 x 3) logits back into 174-bit codeword order.

    The model predicts only the data symbols (the 21 sync symbols are known a
    priori), so the syndrome — which is defined over the whole codeword — needs
    the sync bits filled in before it means anything.
    """

    import torch

    batch = data_logits.shape[0]
    codeword = torch.zeros(batch, CODEWORD_BITS, device=device, dtype=data_logits.dtype)
    flattened = data_logits.reshape(batch, -1)
    # The k-th *data* symbol carries codeword bits 3k..3k+2 — its position among
    # the 79 symbols is a different number, and conflating them is how you get an
    # index out of bounds (or, worse, a silent mis-mapping).
    positions = torch.from_numpy(
        np.array([bit for k in range(len(DATA_POSITIONS)) for bit in range(3 * k, 3 * k + 3)])
    ).to(device)
    return codeword.index_copy(1, positions, flattened)


def train_decoder(
    binding: CoreBinding,
    beds: list[D.Bed],
    waves: "M.WaveformBank",
    *,
    steps: int,
    batch: int = 16,
    lr: float = 3e-4,
    seed: int = 2026,
    workers: int = 8,
    window: int = 6,
    save_every: int = 200,
    log_every: int = 50,
    out: Path | None = None,
    resume: Path | None = None,
    device: str | None = None,
    amp: bool = True,
    syndrome_weight: float = 0.0,
) -> dict[str, object]:
    """Train the probe, checkpointing so a session can be stopped and resumed.

    Two objectives exist and the default is the one that trains: the bit
    cross-entropy (the demodulator) — and a syndrome term, **off by default**,
    because both forms tried here had gradients that vanish exactly where the
    term matters.  Measured: a sign/magnitude soft-XOR returned 1e-19 for random
    logits and exactly 0.0 for a confidently wrong codeword; the LDPC tanh rule
    (values correct: 0.005 for a valid codeword, 2.9 for a one-bit flip) also
    returned 0.0 once the logits were saturated at +/-12.  The coding gain is
    therefore left to a separate decoder stage — the classical LDPC or a learned
    one — which is also how the literature splits demapping from decoding.

    ``soft_syndrome_loss`` stays because the *verification* use is sound and is
    what the deployment gate will call: a predicted codeword must satisfy
    ``H c = 0`` before anything is published.
    """

    import torch

    global DATA_POSITIONS_TENSOR
    if DATA_POSITIONS_TENSOR is None:
        DATA_POSITIONS_TENSOR = torch.from_numpy(data_positions_tensor())
    device = device or ("mps" if torch.backends.mps.is_available() else "cpu")
    model = build_probe_model().to(device)
    optimiser = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    schedule = snr_schedule(steps)
    generator = np.random.default_rng(seed)
    start_step = 0
    history: list[dict[str, float]] = []
    if resume is not None and Path(resume).exists():
        payload = torch.load(resume, map_location="cpu", weights_only=False)
        model.load_state_dict(payload["model"])
        optimiser.load_state_dict(payload["optimiser"])
        start_step = int(payload.get("step", 0))
        history = list(payload.get("history", []))
        generator = np.random.default_rng(payload.get("rng", seed))
        print(f"resumed {resume} at step {start_step}", flush=True)

    def build(indices: tuple[list[int], tuple[float, float]]):
        chunks, span = indices
        return probe_batch(binding, beds, waves, chunks, seed=seed, snr_span=span)

    def batches():
        for step in range(start_step, steps):
            span = schedule[min(step, len(schedule) - 1)]
            indices = [int(value) for value in generator.integers(0, 10**7, batch)]
            # The RNG advances in the main thread, so the order is reproducible
            # however many workers happen to be generating.
            yield (indices, span)

    def save(step: int) -> None:
        if out is None:
            return
        torch.save(
            {
                "model": model.state_dict(),
                "optimiser": optimiser.state_dict(),
                "step": step,
                "history": history,
                "rng": int(generator.integers(0, 2**31 - 1)),
            },
            out,
        )

    use_amp = amp and device in ("mps", "cuda")
    autocast = (
        torch.autocast(device_type=device, dtype=torch.bfloat16) if use_amp else torch.autocast("cpu", enabled=False)
    )
    model.train()
    step = start_step
    for (inputs, targets) in prefetch(build, batches(), window=window, workers=workers):
        step += 1
        x = torch.from_numpy(inputs).to(device)
        y = torch.from_numpy(targets).to(device)
        with autocast:
            logits = model(x)[:, DATA_POSITIONS_TENSOR.to(device), :]
            bit_loss = torch.nn.functional.binary_cross_entropy_with_logits(logits.float(), y)
        # The syndrome term is skipped, not multiplied by zero: 0 * NaN is NaN,
        # and the term's own numerics are unforgiving (see its docstring).  When
        # it is off it must not exist at all.
        if syndrome_weight:
            syndrome_loss = soft_syndrome_loss(_codeword_logits(logits.float(), device))
            loss = bit_loss + syndrome_weight * syndrome_loss
        else:
            syndrome_loss = torch.zeros((), device=bit_loss.device)
            loss = bit_loss
        optimiser.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
        optimiser.step()
        if step % log_every == 0 or step == steps:
            with torch.no_grad():
                predicted = (logits.float() > 0)
                bit_accuracy = float((predicted == y.bool()).float().mean())
                exact = float(
                    (predicted == y.bool()).reshape(predicted.shape[0], -1).all(dim=1).float().mean()
                )
            history.append(
                {"step": step, "loss": float(loss.detach().cpu()),
                 "bit_loss": float(bit_loss.detach().cpu()),
                 "syndrome_loss": float(syndrome_loss.detach().cpu()),
                 "bit_accuracy": bit_accuracy, "exact_rate": exact}
            )
            span = schedule[min(step - 1, len(schedule) - 1)]
            print(
                f"  step {step:>6}/{steps}  loss {float(loss.detach().cpu()):.4f} "
                f"(比特 {float(bit_loss.detach().cpu()):.4f} + syndrome "
                f"{float(syndrome_loss.detach().cpu()):.3f})  "
                f"比特准确 {bit_accuracy:.4f}  整码字 {exact:.3f}  SNR [{span[0]:+.0f},{span[1]:+.0f}] dB",
                flush=True,
            )
        if out is not None and step % save_every == 0:
            save(step)
    save(step)
    if out is not None:
        print(f"wrote {out}")
    return {"device": device, "steps": step, "history": history}


def evaluate_vs_classical(
    binding: CoreBinding,
    model,
    beds: list[D.Bed],
    waves: "M.WaveformBank",
    *,
    snr_values: tuple[float, ...],
    trials: int,
    seed: int = 909,
    threads: int = 4,
) -> dict[str, object]:
    """The S2 gate: the model and the classical chain on the *same* slot.

    Ground truth is exact (we placed the signal) and the classical side runs the
    live configuration, so the question is not "does the model look good" but
    "does it do what the thing it would have to accompany already does".  Below
    the classical chain it is not a candidate; at parity it is worth pushing into
    the tail; above it, Slice 3 has something to measure on real slots.
    """

    import torch

    global DATA_POSITIONS_TENSOR
    if DATA_POSITIONS_TENSOR is None:
        DATA_POSITIONS_TENSOR = torch.from_numpy(data_positions_tensor())
    device = next(iter(model.parameters())).device
    classical = DecodeConfig(
        path=DecodePath.IMPROVED, profile=1, cycles=3, threads=threads,
        sensitivity=3, low_threshold=True,
    )
    positions = DATA_POSITIONS_TENSOR.to(device)
    model.eval()
    report: dict[str, object] = {"trials": trials, "by_snr": {}}
    for snr in snr_values:
        model_hits = classical_hits = 0
        bit_scores: list[float] = []
        for trial in range(trials):
            sample = sample_for(binding, beds, 5000 + trial, seed=seed, count=1,
                                snr_span=(snr, snr), waves=waves)
            target = sample.targets[0]
            window = crop(sample.features, target.frequency, target.dt)
            with torch.no_grad():
                logits = model(torch.from_numpy(window)[None].to(device))[0]
                predicted = (logits[positions] > 0).cpu().numpy()
            truth = data_bit_targets(target.codeword).astype(bool)
            bit_scores.append(float((predicted == truth).mean()))
            model_hits += int(bool((predicted == truth).all()))
            batch = binding.decode(sample.pcm, classical, 70_000 + trial)
            classical_hits += int(M.found(batch.results, target))
        report["by_snr"][f"{snr:+.0f}"] = {
            "model_exact": model_hits,
            "classical_hits": classical_hits,
            "trials": trials,
            "mean_bit_accuracy": float(np.mean(bit_scores)) if bit_scores else 0.0,
        }
        print(
            f"  {snr:+5.0f} dB   模型整码字 {model_hits:>3}/{trials}（比特 {np.mean(bit_scores):.4f}）"
            f"   经典解码器 {classical_hits:>3}/{trials}",
            flush=True,
        )
    return report


# --- the code's own structure, as a training signal ------------------------
#
# A per-bit objective can only ever learn the demodulator: at 95% bit accuracy
# the chance that all 174 bits are right is 0.95**174, i.e. none.  The classical
# chain gets its low-SNR performance from the LDPC constraints, so the model
# needs a term that measures the same thing — the *syndrome*.  H comes straight
# from the encoder's own generator (ldpc_174_91_c_generator.f90: 83 rows of 23
# hex digits), and the code is systematic, so H = [G | I].

GENERATOR_SOURCE = Path(__file__).resolve().parents[2] / "ft8" / "dsp" / "vendor" / "ft8" / "ldpc_174_91_c_generator.f90"
GENERATOR_ROWS = 83


def _generator_hex() -> list[str]:
    """Read the generator matrix from the file the encoder was compiled from.

    Not a transcribed copy: a second copy of the standard is exactly how a
    training target drifts away from what is on the air (and the first attempt
    here proved it — a hand-typed list came to 33 rows out of 83).
    """

    text = GENERATOR_SOURCE.read_text(encoding="utf-8", errors="ignore")
    # The file's only 23-character hex strings are the generator rows.
    rows = re.findall(r'"([0-9a-fA-F]{23})"', text)
    if len(rows) != GENERATOR_ROWS:
        raise RuntimeError(
            f"{GENERATOR_SOURCE.name}: expected {GENERATOR_ROWS} generator rows, found {len(rows)}"
        )
    return rows


def generator_rows(rows: list[str] | None = None) -> np.ndarray:
    """The parity generator, ``[83, 91]`` — each hex digit is four message bits."""

    rows = _generator_hex() if rows is None else rows
    matrix = np.zeros((len(rows), INFO_BITS), dtype=np.uint8)
    for index, text in enumerate(rows):
        digits = [int(char, 16) for char in text]
        for position, digit in enumerate(digits):
            # encode174_91.f90: ibmax=4, but 3 for the last digit — and the bits
            # taken are always the top ones (btest(istr, 4-jj) with jj=1..ibmax,
            # i.e. bits 3,2,1 for the short digit).  Reading it as "the low three
            # bits" shifts the last 3 columns of every row and makes H reject the
            # encoder's own codewords.
            width = 3 if position == len(digits) - 1 else 4
            for offset in range(width):
                column = position * 4 + offset
                if column < INFO_BITS and (digit >> (4 - 1 - offset)) & 1:
                    matrix[index, column] = 1
    return matrix


def parity_check() -> np.ndarray:
    """``H = [G | I]``, ``[83, 174]`` — because the code is systematic."""

    generator = generator_rows()
    rows, info = generator.shape
    check = np.zeros((rows, CODEWORD_BITS), dtype=np.uint8)
    check[:, :info] = generator
    check[:, info:] = np.eye(rows, dtype=np.uint8)
    return check


_PARITY_CHECK: np.ndarray | None = None


def parity_check_matrix() -> np.ndarray:
    global _PARITY_CHECK
    if _PARITY_CHECK is None:
        _PARITY_CHECK = parity_check()
    return _PARITY_CHECK


def syndrome(codeword: np.ndarray) -> np.ndarray:
    """``H c mod 2`` — all zeros for a valid codeword."""

    bits = np.asarray(codeword, dtype=np.uint8).reshape(-1)
    if bits.size != CODEWORD_BITS:
        raise ValueError(f"a codeword is {CODEWORD_BITS} bits, got {bits.size}")
    return (parity_check_matrix() @ bits) % 2


def soft_syndrome_loss(logits):
    """Differentiable "every check is satisfied" — the LDPC tanh rule.

    A check is the XOR of the bits it touches, so with bits mapped to ±1 its soft
    value is ``prod tanh(L/2)``: +1 when the parity is even, -1 when odd (this is
    the same quantity belief propagation passes along the graph).  The loss is 0
    when every check is satisfied.

    A sign/magnitude form of the same quantity is *numerically* correct and
    useless: measured, it returned gradients of 1e-19 and exactly 0.0 for a
    confidently wrong codeword — a large loss with nothing to descend.  The tanh
    rule saturates gracefully instead of collapsing to a plateau.

    Penalising the checks rather than the bits is what gives the model a route to
    a valid codeword instead of 174 independent guesses: at 95% bit accuracy the
    chance all 174 bits are right is 0.95**174, i.e. none.
    """

    import torch

    check = torch.from_numpy(parity_check_matrix().astype(np.float32))
    indices = check.to(logits.device).nonzero(as_tuple=False)
    rows, columns = indices[:, 0], indices[:, 1]
    # Clamped to +/-6, not +/-30: tanh(15) is 1 - 1e-13, so a confidently wrong
    # codeword (the case the term exists for) had a gradient of exactly 0.0 —
    # measured.  The clamp makes this a *guide* rather than a hard constraint;
    # the hard check is the real syndrome, applied at inference.
    per_edge = torch.tanh(logits.index_select(1, columns).clamp(-6.0, 6.0) / 2.0)
    # A product over a check's edges: the sign is the parity, the magnitude the
    # confidence.  Summing logs would lose the sign; a scatter-add of logs with a
    # separate sign is what vanished above, so the product is taken in two halves
    # (sum of logs of |value| plus the parity of negatives) — kept as a product
    # here because the edges per check are few (~45) and the magnitudes are < 1.
    logs = torch.log(per_edge.abs().clamp_min(1e-12))
    totals = torch.zeros(logits.shape[0], check.shape[0], device=logits.device)
    totals = totals.index_add(1, rows, logs)
    negatives = torch.zeros_like(totals).index_add(
        1, rows, (per_edge < 0).to(totals.dtype)
    )
    soft = torch.pow(-1.0, negatives.remainder(2.0)) * torch.exp(totals)
    return ((1.0 - soft) / 2.0).mean()


def _codeword_logits(data_logits, device):
    """Scatter the per-symbol (58 x 3) logits back into 174-bit codeword order.

    The model predicts only the data symbols (the 21 sync symbols are known a
    priori), so the syndrome — which is defined over the whole codeword — needs
    the sync bits filled in before it means anything.
    """

    import torch

    batch = data_logits.shape[0]
    codeword = torch.zeros(batch, CODEWORD_BITS, device=device, dtype=data_logits.dtype)
    flattened = data_logits.reshape(batch, -1)
    # The k-th *data* symbol carries codeword bits 3k..3k+2 — its position among
    # the 79 symbols is a different number, and conflating them is how you get an
    # index out of bounds (or, worse, a silent mis-mapping).
    positions = torch.from_numpy(
        np.array([bit for k in range(len(DATA_POSITIONS)) for bit in range(3 * k, 3 * k + 3)])
    ).to(device)
    return codeword.index_copy(1, positions, flattened)


def train_decoder(
    binding: CoreBinding,
    beds: list[D.Bed],
    waves: "M.WaveformBank",
    *,
    steps: int,
    batch: int = 16,
    lr: float = 3e-4,
    seed: int = 2026,
    workers: int = 8,
    window: int = 6,
    save_every: int = 200,
    log_every: int = 50,
    out: Path | None = None,
    resume: Path | None = None,
    device: str | None = None,
    amp: bool = True,
    syndrome_weight: float = 0.0,
) -> dict[str, object]:
    """Train the probe, checkpointing so a session can be stopped and resumed.

    Two objectives exist and the default is the one that trains: the bit
    cross-entropy (the demodulator) — and a syndrome term, **off by default**,
    because both forms tried here had gradients that vanish exactly where the
    term matters.  Measured: a sign/magnitude soft-XOR returned 1e-19 for random
    logits and exactly 0.0 for a confidently wrong codeword; the LDPC tanh rule
    (values correct: 0.005 for a valid codeword, 2.9 for a one-bit flip) also
    returned 0.0 once the logits were saturated at +/-12.  The coding gain is
    therefore left to a separate decoder stage — the classical LDPC or a learned
    one — which is also how the literature splits demapping from decoding.

    ``soft_syndrome_loss`` stays because the *verification* use is sound and is
    what the deployment gate will call: a predicted codeword must satisfy
    ``H c = 0`` before anything is published.
    """

    import torch

    global DATA_POSITIONS_TENSOR
    if DATA_POSITIONS_TENSOR is None:
        DATA_POSITIONS_TENSOR = torch.from_numpy(data_positions_tensor())
    device = device or ("mps" if torch.backends.mps.is_available() else "cpu")
    model = build_probe_model().to(device)
    optimiser = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    schedule = snr_schedule(steps)
    generator = np.random.default_rng(seed)
    start_step = 0
    history: list[dict[str, float]] = []
    if resume is not None and Path(resume).exists():
        payload = torch.load(resume, map_location="cpu", weights_only=False)
        model.load_state_dict(payload["model"])
        optimiser.load_state_dict(payload["optimiser"])
        start_step = int(payload.get("step", 0))
        history = list(payload.get("history", []))
        generator = np.random.default_rng(payload.get("rng", seed))
        print(f"resumed {resume} at step {start_step}", flush=True)

    def build(indices: tuple[list[int], tuple[float, float]]):
        chunks, span = indices
        return probe_batch(binding, beds, waves, chunks, seed=seed, snr_span=span)

    def batches():
        for step in range(start_step, steps):
            span = schedule[min(step, len(schedule) - 1)]
            indices = [int(value) for value in generator.integers(0, 10**7, batch)]
            # The RNG advances in the main thread, so the order is reproducible
            # however many workers happen to be generating.
            yield (indices, span)

    def save(step: int) -> None:
        if out is None:
            return
        torch.save(
            {
                "model": model.state_dict(),
                "optimiser": optimiser.state_dict(),
                "step": step,
                "history": history,
                "rng": int(generator.integers(0, 2**31 - 1)),
            },
            out,
        )

    use_amp = amp and device in ("mps", "cuda")
    autocast = (
        torch.autocast(device_type=device, dtype=torch.bfloat16) if use_amp else torch.autocast("cpu", enabled=False)
    )
    model.train()
    step = start_step
    for (inputs, targets) in prefetch(build, batches(), window=window, workers=workers):
        step += 1
        x = torch.from_numpy(inputs).to(device)
        y = torch.from_numpy(targets).to(device)
        with autocast:
            logits = model(x)[:, DATA_POSITIONS_TENSOR.to(device), :]
            bit_loss = torch.nn.functional.binary_cross_entropy_with_logits(logits.float(), y)
        # The syndrome term is skipped, not multiplied by zero: 0 * NaN is NaN,
        # and the term's own numerics are unforgiving (see its docstring).  When
        # it is off it must not exist at all.
        if syndrome_weight:
            syndrome_loss = soft_syndrome_loss(_codeword_logits(logits.float(), device))
            loss = bit_loss + syndrome_weight * syndrome_loss
        else:
            syndrome_loss = torch.zeros((), device=bit_loss.device)
            loss = bit_loss
        optimiser.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
        optimiser.step()
        if step % log_every == 0 or step == steps:
            with torch.no_grad():
                predicted = (logits.float() > 0)
                bit_accuracy = float((predicted == y.bool()).float().mean())
                exact = float(
                    (predicted == y.bool()).reshape(predicted.shape[0], -1).all(dim=1).float().mean()
                )
            history.append(
                {"step": step, "loss": float(loss.detach().cpu()),
                 "bit_loss": float(bit_loss.detach().cpu()),
                 "syndrome_loss": float(syndrome_loss.detach().cpu()),
                 "bit_accuracy": bit_accuracy, "exact_rate": exact}
            )
            span = schedule[min(step - 1, len(schedule) - 1)]
            print(
                f"  step {step:>6}/{steps}  loss {float(loss.detach().cpu()):.4f} "
                f"(比特 {float(bit_loss.detach().cpu()):.4f} + syndrome "
                f"{float(syndrome_loss.detach().cpu()):.3f})  "
                f"比特准确 {bit_accuracy:.4f}  整码字 {exact:.3f}  SNR [{span[0]:+.0f},{span[1]:+.0f}] dB",
                flush=True,
            )
        if out is not None and step % save_every == 0:
            save(step)
    save(step)
    if out is not None:
        print(f"wrote {out}")
    return {"device": device, "steps": step, "history": history}


def evaluate_vs_classical(
    binding: CoreBinding,
    model,
    beds: list[D.Bed],
    waves: "M.WaveformBank",
    *,
    snr_values: tuple[float, ...],
    trials: int,
    seed: int = 909,
    threads: int = 4,
) -> dict[str, object]:
    """The S2 gate: the model and the classical chain on the *same* slot.

    Ground truth is exact (we placed the signal) and the classical side runs the
    live configuration, so the question is not "does the model look good" but
    "does it do what the thing it would have to accompany already does".  Below
    the classical chain it is not a candidate; at parity it is worth pushing into
    the tail; above it, Slice 3 has something to measure on real slots.
    """

    import torch

    global DATA_POSITIONS_TENSOR
    if DATA_POSITIONS_TENSOR is None:
        DATA_POSITIONS_TENSOR = torch.from_numpy(data_positions_tensor())
    device = next(iter(model.parameters())).device
    classical = DecodeConfig(
        path=DecodePath.IMPROVED, profile=1, cycles=3, threads=threads,
        sensitivity=3, low_threshold=True,
    )
    positions = DATA_POSITIONS_TENSOR.to(device)
    model.eval()
    report: dict[str, object] = {"trials": trials, "by_snr": {}}
    for snr in snr_values:
        model_hits = classical_hits = 0
        bit_scores: list[float] = []
        for trial in range(trials):
            sample = sample_for(binding, beds, 5000 + trial, seed=seed, count=1,
                                snr_span=(snr, snr), waves=waves)
            target = sample.targets[0]
            window = crop(sample.features, target.frequency, target.dt)
            with torch.no_grad():
                logits = model(torch.from_numpy(window)[None].to(device))[0]
                predicted = (logits[positions] > 0).cpu().numpy()
            truth = data_bit_targets(target.codeword).astype(bool)
            bit_scores.append(float((predicted == truth).mean()))
            model_hits += int(bool((predicted == truth).all()))
            batch = binding.decode(sample.pcm, classical, 70_000 + trial)
            classical_hits += int(M.found(batch.results, target))
        report["by_snr"][f"{snr:+.0f}"] = {
            "model_exact": model_hits,
            "classical_hits": classical_hits,
            "trials": trials,
            "mean_bit_accuracy": float(np.mean(bit_scores)) if bit_scores else 0.0,
        }
        print(
            f"  {snr:+5.0f} dB   模型整码字 {model_hits:>3}/{trials}（比特 {np.mean(bit_scores):.4f}）"
            f"   经典解码器 {classical_hits:>3}/{trials}",
            flush=True,
        )
    return report


# --- the code's own structure, as a training signal ------------------------
#
# A per-bit objective can only ever learn the demodulator: at 95% bit accuracy
# the chance that all 174 bits are right is 0.95**174, i.e. none.  The classical
# chain gets its low-SNR performance from the LDPC constraints, so the model
# needs a term that measures the same thing — the *syndrome*.  H comes straight
# from the encoder's own generator (ldpc_174_91_c_generator.f90: 83 rows of 23
# hex digits), and the code is systematic, so H = [G | I].

GENERATOR_SOURCE = Path(__file__).resolve().parents[2] / "ft8" / "dsp" / "vendor" / "ft8" / "ldpc_174_91_c_generator.f90"
GENERATOR_ROWS = 83


def _generator_hex() -> list[str]:
    """Read the generator matrix from the file the encoder was compiled from.

    Not a transcribed copy: a second copy of the standard is exactly how a
    training target drifts away from what is on the air (and the first attempt
    here proved it — a hand-typed list came to 33 rows out of 83).
    """

    text = GENERATOR_SOURCE.read_text(encoding="utf-8", errors="ignore")
    # The file's only 23-character hex strings are the generator rows.
    rows = re.findall(r'"([0-9a-fA-F]{23})"', text)
    if len(rows) != GENERATOR_ROWS:
        raise RuntimeError(
            f"{GENERATOR_SOURCE.name}: expected {GENERATOR_ROWS} generator rows, found {len(rows)}"
        )
    return rows


def generator_rows(rows: list[str] | None = None) -> np.ndarray:
    """The parity generator, ``[83, 91]`` — each hex digit is four message bits."""

    rows = _generator_hex() if rows is None else rows
    matrix = np.zeros((len(rows), INFO_BITS), dtype=np.uint8)
    for index, text in enumerate(rows):
        digits = [int(char, 16) for char in text]
        for position, digit in enumerate(digits):
            # encode174_91.f90: ibmax=4, but 3 for the last digit — and the bits
            # taken are always the top ones (btest(istr, 4-jj) with jj=1..ibmax,
            # i.e. bits 3,2,1 for the short digit).  Reading it as "the low three
            # bits" shifts the last 3 columns of every row and makes H reject the
            # encoder's own codewords.
            width = 3 if position == len(digits) - 1 else 4
            for offset in range(width):
                column = position * 4 + offset
                if column < INFO_BITS and (digit >> (4 - 1 - offset)) & 1:
                    matrix[index, column] = 1
    return matrix


def parity_check() -> np.ndarray:
    """``H = [G | I]``, ``[83, 174]`` — because the code is systematic."""

    generator = generator_rows()
    rows, info = generator.shape
    check = np.zeros((rows, CODEWORD_BITS), dtype=np.uint8)
    check[:, :info] = generator
    check[:, info:] = np.eye(rows, dtype=np.uint8)
    return check


_PARITY_CHECK: np.ndarray | None = None


def parity_check_matrix() -> np.ndarray:
    global _PARITY_CHECK
    if _PARITY_CHECK is None:
        _PARITY_CHECK = parity_check()
    return _PARITY_CHECK


def syndrome(codeword: np.ndarray) -> np.ndarray:
    """``H c mod 2`` — all zeros for a valid codeword."""

    bits = np.asarray(codeword, dtype=np.uint8).reshape(-1)
    if bits.size != CODEWORD_BITS:
        raise ValueError(f"a codeword is {CODEWORD_BITS} bits, got {bits.size}")
    return (parity_check_matrix() @ bits) % 2


def soft_syndrome_loss(probabilities):
    """Differentiable "every check is satisfied".

    A check is the XOR of the bits it touches, whose probability of being 1 is
    ``(1 - prod(1 - 2p)) / 2``.  The product is evaluated in sign/magnitude form
    because individual factors are negative whenever ``p > 0.5`` — a plain
    ``log`` of the product is NaN exactly where the model is most confident, and
    that is where the loss matters most.

    Penalising the *checks* rather than the bits is what gives the model a route
    to a valid codeword instead of 174 independent guesses: at 95% bit accuracy
    the chance all 174 bits are right is 0.95**174, i.e. none.
    """

    import torch

    check = torch.from_numpy(parity_check_matrix().astype(np.float32))
    indices = check.to(probabilities.device).nonzero(as_tuple=False)
    rows, columns = indices[:, 0], indices[:, 1]
    selected = probabilities.index_select(1, columns).clamp(1e-4, 1 - 1e-4)
    signed = 1.0 - 2.0 * selected
    log_abs = torch.log(signed.abs())
    negative = (signed < 0).to(probabilities.dtype)
    batch = probabilities.shape[0]
    total_log = torch.zeros(batch, check.shape[0], device=probabilities.device)
    total_log = total_log.index_add(1, rows, log_abs)
    total_neg = torch.zeros(batch, check.shape[0], device=probabilities.device)
    total_neg = total_neg.index_add(1, rows, negative)
    product = torch.pow(-1.0, total_neg) * torch.exp(total_log)
    probability_of_one = (1.0 - product) / 2.0
    # Want every check to be 0 (even parity).
    return -torch.log((1.0 - probability_of_one).clamp_min(1e-12)).mean()
