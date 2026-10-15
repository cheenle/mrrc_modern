"""P3 / Slice 1 — the simulator, and the curve that decides whether P3 is worth it.

The FT8 ML-enhancement track (``docs/superpowers/specs/2026-10-14-ft8-ml-enhancement-design.md``)
starts, deliberately, *without* any machine learning.  Its first question is a
falsifiable one that costs an hour instead of a week:

*Where does the decoder's recall fall off with SNR, and how much of the real
corpus sits on that cliff?*

If the real corpus is nowhere near the cliff, enhancement has nothing to win and
the track stops here — an outcome this module is designed to be able to report.

The other half of the prerequisite is exact and already available: the ABI's
``wsjt_ft8_encode`` turns a message into the *ideal* 12.64 s waveform, so a
synthetic signal's ground truth (message, frequency, dt, nominal SNR) is known
to the sample.  Noise comes from real slots, not white noise, so the SNR is
defined against the box's own band conditions.

dt convention (calibrated, not assumed): a signal placed at offset ``S`` seconds
into the 15 s slot is reported by the decoder as ``dt = S - 0.5``; see
``tests/ft8/test_ml_simulate.py::test_dt_convention``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Tools live two levels under the app root; importing ``ft8`` means that root is
# on the path — the same bootstrap ``decode_matrix.py`` performs, done once at
# module scope so the module is usable as a library too.
_APP_ROOT = Path(__file__).resolve().parents[2]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from ft8.core.binding import CoreBinding
from ft8.core.models import (
    FT8_RX_RATE,
    FT8_RX_SAMPLES,
    FT8_TX_RATE,
    FT8_TX_SAMPLES,
    DecodeConfig,
    DecodePath,
)
from ft8.core.worker import default_library_path

# WSJT-X reports SNR against this reference bandwidth, and its decoder estimates
# the noise floor locally — so the honest simulation measures the noise in the
# same 2500 Hz window, centred on the signal, rather than over the whole band.
REFERENCE_BANDWIDTH = 2500.0

# FT8 lives in roughly 200-2900 Hz of the 0-6000 Hz audio band at 12 kHz.
PLACEMENT_LOW = 300.0
PLACEMENT_HIGH = 2800.0

# The decoder's own dt search window, with the tolerance used to call a
# synthesized signal "found" (the union in cascade.py matches (frequency, dt)
# the same way).
DT_TOLERANCE = 0.25
FREQUENCY_TOLERANCE = 15.0

_DECIMATION = FT8_TX_RATE // FT8_RX_RATE  # 4
_ANTIALIAS_TAPS = 127

# The transmitted waveform is 12.64 s inside a 15 s window, so a signal placed
# at dt is complete only while ``0 <= dt + 0.5`` and ``dt + 0.5 + 12.64 <= 15``.
# Real slots do contain earlier signals (the box's capture starts a little late,
# so the leading milliseconds are clipped); that is a separate condition, and
# mixing it into the baseline curve would confound "too weak" with "clipped".
DT_MIN = -0.5
DT_MAX = 1.7


def quiet_frequency(pcm: np.ndarray, generator: np.random.Generator) -> float:
    """Return a passband frequency where the bed is quiet.

    A synthesized signal must not land on a real one: the decoder would lock
    onto the louder of the two, and the trial would count as "found" whatever the
    synthetic SNR was.  Quieter-than-median bins are chosen from, so the trial
    measures *this* signal rather than the band's activity.
    """

    frequencies, power = _bin_powers(pcm)
    inside = (frequencies >= PLACEMENT_LOW) & (frequencies <= PLACEMENT_HIGH)
    candidates = frequencies[inside]
    powers = power[inside]
    # Judge a candidate by the group it will occupy, not by its single bin: a
    # neighbour's signal one tone away is just as much in the way.  The
    # percentile must be taken over the passband slice — taken over the whole
    # spectrum it selects the deepest notches instead, which are digitally
    # silent (measured: a tone-group power of 3 against a band median of ~1500)
    # and make "nominal SNR" meaningless.
    span = int(round(TONE_GROUP_HZ / (frequencies[1] - frequencies[0]))) + 1
    groups = np.convolve(powers, np.ones(span) / span, mode="same")
    floor = np.median(groups)
    # Stay near the floor: quieter than -6 dB below it is a notch, not a channel.
    eligible = (groups < np.percentile(groups, 50.0)) & (groups > floor * 0.25)
    quiet = candidates[eligible]
    if quiet.size == 0:
        quiet = candidates[groups < np.percentile(groups, 75.0)]
    if quiet.size == 0:
        quiet = candidates
    return float(generator.choice(quiet))


def _antialias_kernel() -> np.ndarray:
    """Return the windowed-sinc low-pass used before decimating 48 kHz to 12 kHz.

    Cutoff at 0.45 of the *output* Nyquist rate (2.7 kHz): FT8 never occupies
    more than ~3 kHz of the 6 kHz band, and a generous transition band keeps the
    passband flat — the decoder recovers the synthesized frequency exactly.
    """

    taps = np.arange(_ANTIALIAS_TAPS) - (_ANTIALIAS_TAPS - 1) / 2
    cutoff = 0.45 / _DECIMATION  # cycles/sample at 48 kHz
    kernel = np.sinc(2 * cutoff * taps) * np.hamming(_ANTIALIAS_TAPS)
    return (kernel / kernel.sum()).astype(np.float32)


_KERNEL = _antialias_kernel()


def clean_waveform_12k(binding: CoreBinding, message: str, frequency: float) -> np.ndarray:
    """Return the ideal FT8 waveform for ``message`` at 12 kHz, in int16 units."""

    waveform = np.zeros(FT8_TX_SAMPLES, dtype=np.float32)
    binding.encode(message, frequency, FT8_TX_RATE, waveform)
    filtered = np.convolve(waveform, _KERNEL, mode="same")
    return filtered[:: _DECIMATION].astype(np.float32) * 32767.0


class WaveformBank:
    """Cache analytic FT8 waveforms so a new frequency costs a multiply, not an encode.

    Generating one waveform takes ~13 ms (encode at 48 kHz plus the anti-alias
    decimation), which was the dominant cost of building a training pair — three
    signals per slot made data generation slower than the training step it fed.
    An FT8 burst is a sum of tones at ``f0 + k*6.25 Hz``, so shifting it is exact
    when done on the analytic signal (a real-valued multiply would fold a mirror
    image back into the band).  :meth:`waveform` is checked against the encoder
    in the tests: an accelerator that is merely *close* would move every label.
    """

    def __init__(
        self,
        binding: CoreBinding,
        messages: tuple[str, ...],
        base_frequency: float = 1000.0,
        capacity: int = 192,
    ) -> None:
        self._binding = binding
        self._base = base_frequency
        self._capacity = capacity
        self._waveforms: dict[str, np.ndarray] = {}
        for message in messages:
            self._waveforms[message] = self._encode(message)
        self._time = np.arange(FT8_TX_SAMPLES // _DECIMATION, dtype=np.float64) / FT8_RX_RATE

    def _encode(self, message: str) -> np.ndarray:
        """Encode one message at the base frequency as an analytic signal."""

        raw = clean_waveform_12k(self._binding, message, self._base).astype(np.float64)
        spectrum = np.fft.fft(raw)
        spectrum[1 : raw.size // 2] *= 2.0  # analytic: double the positive half
        spectrum[raw.size // 2 + 1 :] = 0.0
        return np.fft.ifft(spectrum).astype(np.complex128)

    def _lookup(self, message: str) -> np.ndarray:
        """Return the cached waveform, encoding on demand and evicting the oldest.

        On demand because a training bed carries *real* stations (arbitrary
        callsigns, thousands of them across a corpus); bounded because each
        analytic waveform is 2.4 MB and a corpus has far more messages than fit
        in memory.  The beds are revisited cyclically, so an LRU far smaller than
        the corpus keeps the hit rate high.
        """

        cached = self._waveforms.get(message)
        if cached is not None:
            self._waveforms[message] = self._waveforms.pop(message)
            return cached
        waveform = self._encode(message)
        self._waveforms[message] = waveform
        if len(self._waveforms) > self._capacity:
            self._waveforms.pop(next(iter(self._waveforms)))
        return waveform

    def analytic(self, message: str, frequency: float) -> np.ndarray:
        """Return the complex analytic waveform (needed to shift or to fade)."""

        base = self._lookup(message)
        if abs(frequency - self._base) < 1e-9:
            return base
        shift = np.exp(2j * np.pi * (frequency - self._base) * self._time[: base.size])
        return base * shift

    def waveform(self, message: str, frequency: float) -> np.ndarray:
        """Return the same samples :func:`clean_waveform_12k` would, at ``frequency``."""

        return self.analytic(message, frequency).real.astype(np.float32)


# Bed spectra are reused across every example drawn from the same slot (a
# training run sees the same few dozen beds thousands of times), and each
# computation is a 180 000-point FFT — four per example, which made data
# generation slower than the training step it fed.  Keyed by ``id`` with the
# array kept alive by the entry itself, so an id cannot be recycled underneath.
_POWER_CACHE: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
_POWER_CACHE_LIMIT = 256


def waveform_for(
    binding: CoreBinding, signal: "SyntheticSignal", waves: "WaveformBank | None" = None
) -> np.ndarray:
    """Return the waveform for ``signal``, perturbations included.

    Both the mix and the clean target go through here, so a faded signal cannot
    end up in one and an ideal one in the other — the drift a second copy would
    introduce is exactly what a training target must not have.
    """

    if waves is not None:
        analytic = waves.analytic(signal.message, signal.frequency)
    else:
        raw = clean_waveform_12k(binding, signal.message, signal.frequency).astype(np.float64)
        spectrum = np.fft.fft(raw)
        spectrum[1 : raw.size // 2] *= 2.0
        spectrum[raw.size // 2 + 1 :] = 0.0
        analytic = np.fft.ifft(spectrum)
    if not signal.variant:
        return analytic.real.astype(np.float32)
    generator = np.random.default_rng(signal.variant)
    depth = float(generator.uniform(0.0, 0.4))          # fading depth
    fade_hz = float(generator.uniform(0.02, 0.6))       # fade rate
    phase = float(generator.uniform(0.0, 2 * np.pi))
    drift_hz = float(generator.uniform(-0.6, 0.6))      # oscillator / Doppler
    time = np.arange(analytic.size, dtype=np.float64) / FT8_RX_RATE
    envelope = 1.0 - depth * (1.0 - np.cos(2 * np.pi * fade_hz * time + phase)) / 2.0
    return (analytic * envelope * np.exp(2j * np.pi * drift_hz * time)).real.astype(np.float32)


def _bin_powers(pcm: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (frequencies, per-bin power) of one slot's periodogram.

    The slot is transformed whole, so a bin here is ~0.067 Hz — finer than the
    decoder's own 6.25 Hz spectrogram bin.  That is deliberate: the group a
    signal occupies is then measured precisely rather than at the resolution the
    answer is later expressed in, and every conversion is by frequency
    (``width / resolution``), never by bin count.
    """

    key = id(pcm)
    cached = _POWER_CACHE.get(key)
    if cached is not None and cached[0] is pcm:
        return cached[1], cached[2]
    spectrum = np.fft.rfft(pcm.astype(np.float64))
    frequencies = np.fft.rfftfreq(pcm.size, 1.0 / FT8_RX_RATE)
    power = 2.0 * np.abs(spectrum) ** 2 / (pcm.size**2)
    if len(_POWER_CACHE) >= _POWER_CACHE_LIMIT:
        _POWER_CACHE.pop(next(iter(_POWER_CACHE)))
    _POWER_CACHE[key] = (pcm, frequencies, power)
    return frequencies, power


# One FT8 tone is 6.25 Hz wide and the 8-FSK alphabet spans 7 spacings, so a
# signal occupies [f, f + 43.75] Hz.  The decoder estimates noise from exactly
# that group: ``ft8bvar.f90:2331`` sets ``xnoi = (sum(s8(0:7,i)**2) - xsig)/7``
# — the mean of the seven tones the signal is *not* using, in the same symbol.
#
# Calibration note (measured, not assumed): nominal SNR equals the decoder's
# reported SNR to within 1 dB on a white or spectrally-shaped Gaussian bed, but
# on real beds the decoder reports ~+3 dB.  The reason is Jensen's inequality on
# non-stationary (bursty) noise: the decoder averages per-symbol ratios
# (favouring the quiet moments, ``ft8bvar.f90:2345``) while the nominal figure
# divides by a whole-slot mean.  Neither is wrong; comparisons against the real
# corpus must therefore use the decoder's own number, which is what the corpus
# logs.
TONE_SPACING = 6.25
TONE_GROUP_HZ = 7 * TONE_SPACING  # 43.75


def tone_group_power(pcm: np.ndarray, center: float) -> float:
    """Return the mean per-bin power in the tone group a signal at ``center`` uses.

    This is the decoder's own noise reference: not the band, not a wide window —
    the 43.75 Hz the signal could be sitting in (``ft8bvar.f90:2331`` computes
    ``xnoi`` from the seven tones the signal is *not* using, in the same symbol).
    A wider window over-estimates the noise wherever placement sought a quiet
    spot, which over-drives the synthesized signal.
    """

    frequencies, power = _bin_powers(pcm)
    resolution = frequencies[1] - frequencies[0]
    group = (frequencies >= center) & (frequencies <= center + TONE_GROUP_HZ)
    if not np.any(group):
        return 1e-30
    return max(float(np.mean(power[group])), 1e-30)


def noise_floor_power(pcm: np.ndarray, center: float, width: float = REFERENCE_BANDWIDTH) -> float:
    """Return the noise power in ``width`` Hz, referred to the signal's own group.

    The per-bin power is the noise power per 6.25 Hz, so the reference-bandwidth
    value is that per-bin power times ``width / 6.25`` — the same conversion the
    decoder makes with its ``-26.5 dB`` correction (``10*log10(2500/6.25)``).
    """

    frequencies, _ = _bin_powers(pcm)
    resolution = frequencies[1] - frequencies[0]
    return tone_group_power(pcm, center) * (width / resolution)


def slot_pcm(path: Path) -> np.ndarray:
    """Read one dumped slot as float32 int16 units."""

    raw = np.frombuffer(path.read_bytes(), dtype=np.int16)
    if raw.size != FT8_RX_SAMPLES:
        raise ValueError(f"{path.name}: expected {FT8_RX_SAMPLES} samples, got {raw.size}")
    return raw.astype(np.float32)


@dataclass(frozen=True, slots=True)
class SyntheticSignal:
    """One synthesized signal's exact ground truth."""

    message: str
    frequency: float
    dt: float
    snr_db: float
    # 0 means "the ideal waveform the encoder produces".  Anything else applies a
    # deterministic propagation-like perturbation (fading, phase wander, a
    # fraction of a Hz of drift) derived from this value — training on ideal
    # bursts only is what taught the first model to treat real, faded signals as
    # noise (Task 10b).
    variant: int = 0

    @property
    def offset_samples(self) -> int:
        return int(round((self.dt + 0.5) * FT8_RX_RATE))


@dataclass(frozen=True, slots=True)
class Trial:
    """One simulated slot: its PCM and the signals deliberately placed in it."""

    slot_id: int
    pcm: np.ndarray
    signals: tuple[SyntheticSignal, ...]
    bed: str


def synthesize(
    binding: CoreBinding,
    bed: np.ndarray,
    signals: tuple[SyntheticSignal, ...],
    *,
    allow_clipping: bool = False,
    waves: "WaveformBank | None" = None,
) -> np.ndarray:
    """Mix ``signals`` into ``bed`` at their nominal SNR, returning int16 PCM.

    Each signal is scaled so its power over the noise it will compete with —
    the tone group it occupies, the same reference the decoder estimates from —
    matches ``snr_db``.  Measuring there rather than over the band matters: the
    box's spectrum is not flat, and a signal in a quiet part of it is genuinely
    easier to hear.
    """

    mixed = bed.astype(np.float64).copy()
    for signal in signals:
        waveform = waveform_for(binding, signal, waves)
        gain = signal_gain(bed, waveform, signal)
        start = signal.offset_samples
        stop = start + waveform.size
        clipped = start < 0 or stop > mixed.size
        if clipped and not allow_clipping:
            raise ValueError(
                f"signal at dt {signal.dt:+.2f} does not fit the slot "
                f"(needs samples {start}..{stop}, slot holds 0..{mixed.size})"
            )
        low, high = max(start, 0), min(stop, mixed.size)
        if high <= low:
            raise ValueError(f"signal at dt {signal.dt:+.2f} lies entirely outside the slot")
        mixed[low:high] += waveform[low - start : high - start].astype(np.float64) * gain
    return np.clip(np.round(mixed), -32768.0, 32767.0).astype(np.int16)


def signal_gain(bed: np.ndarray, waveform: np.ndarray, signal: SyntheticSignal) -> float:
    """Return the amplitude factor that puts ``waveform`` at ``signal``'s nominal SNR.

    The single source of truth for the scaling law: ``synthesize`` applies it, and
    the ML track reuses it to build the *clean* component of a training pair
    (same placement, same gain, no bed) without re-deriving the arithmetic — a
    second copy is how a training target silently drifts from its input.
    """

    noise = noise_floor_power(bed, signal.frequency)
    if noise <= 1e-18:
        # A silent bed has no SNR to speak of, and scaling against it would
        # quietly multiply the signal by ~zero.  Fail loudly instead: this is
        # the mistake that makes a whole sweep meaningless while still
        # printing plausible percentages.
        raise ValueError(
            f"bed has no measurable noise near {signal.frequency:.1f} Hz "
            f"(tone-group power {noise:.3g}); nominal SNR is undefined"
        )
    signal_power = float(np.mean(waveform.astype(np.float64) ** 2))
    return math.sqrt(noise * 10.0 ** (signal.snr_db / 10.0) / signal_power)


def found(results: object, signal: SyntheticSignal) -> bool:
    """Return whether a decode batch contains ``signal`` (frequency + dt match)."""

    for result in results:  # type: ignore[union-attr]
        if (
            abs(result.frequency - signal.frequency) <= FREQUENCY_TOLERANCE
            and abs(result.dt - signal.dt) <= DT_TOLERANCE
        ):
            return True
    return False


@dataclass(frozen=True, slots=True)
class Config:
    """One decoder configuration under test."""

    name: str
    config: DecodeConfig


FAST = Config("fast", DecodeConfig(path=DecodePath.FAST, threads=1))
DEEP = Config(
    # Exactly what the host runs in production since 2026-10-14 (Task 8d).
    "p1c3lt-s3",
    DecodeConfig(
        path=DecodePath.IMPROVED,
        profile=1,
        cycles=3,
        sensitivity=3,
        low_threshold=True,
        threads=4,
    ),
)
CONFIGS = {"fast": FAST, "p1c3lt-s3": DEEP}


def sweep(
    binding: CoreBinding,
    beds: list[Path],
    *,
    snr_values: tuple[float, ...],
    trials: int,
    configs: tuple[Config, ...],
    seed: int = 20261014,
) -> dict[str, object]:
    """Run the recall-vs-SNR sweep and return a JSON-ready report."""

    generator = np.random.default_rng(seed)
    messages = _message_pool()
    report: dict[str, object] = {
        "snr_values": list(snr_values),
        "trials": trials,
        "seed": seed,
        "beds": [path.name for path in beds],
        "configs": {},
    }
    for config in configs:
        by_snr: dict[str, list[float]] = {}
        reported: dict[str, list[int]] = {}
        for snr in snr_values:
            hits = 0
            observed: list[int] = []
            for trial_index in range(trials):
                bed = beds[(trial_index * 7 + int(snr)) % len(beds)]
                frequency = quiet_frequency(slot_pcm(bed), generator)
                dt = float(generator.uniform(DT_MIN, DT_MAX))
                signal = SyntheticSignal(
                    message=str(generator.choice(messages)),
                    frequency=frequency,
                    dt=dt,
                    snr_db=snr,
                )
                pcm = synthesize(binding, slot_pcm(bed), (signal,))
                batch = binding.decode(pcm, config.config, 20_000 + trial_index)
                if found(batch.results, signal):
                    hits += 1
                    for result in batch.results:
                        if abs(result.frequency - frequency) <= FREQUENCY_TOLERANCE:
                            observed.append(int(result.snr))
                            break
            by_snr[f"{snr:+.0f}"] = [hits, trials]
            reported[f"{snr:+.0f}"] = observed
            print(
                f"  {config.name:<12} {snr:+5.0f} dB  {hits:>3}/{trials}  "
                f"({100.0 * hits / trials:5.1f}%)"
                + (f"  reported {min(observed)}..{max(observed)} dB" if observed else ""),
                flush=True,
            )
        report["configs"][config.name] = {"recall": by_snr, "reported_snr": reported}  # type: ignore[index]
    return report


def _message_pool() -> tuple[str, ...]:
    """Return the messages the sweep draws from.

    Plain callsigns and grids only: a hashed callsign (``<...>``) would make the
    "did it decode?" question ambiguous, and that ambiguity is exactly what the
    precision work in Slice 3 has to reason about separately.
    """

    return (
        "CQ BA4EG PM00",
        "CQ JA1VAT QN06",
        "BA4EG JA1VAT -12",
        "JA1VAT BA4EG R-09",
        "BA4EG JA1VAT RR73",
        "CQ DL1ABC JO31",
        "DL1ABC BA4EG -18",
        "BA4EG DL1ABC R-15",
        "CQ W1AW FN31",
        "W1AW BA4EG -20",
        "BA4EG W1AW 73",
        "CQ VK3ABC QF22",
        "BA4EG VK3ABC -16",
    )


def _parse_snr(text: str) -> tuple[float, ...]:
    return tuple(float(part) for part in text.split(",") if part.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FT8 ML-enhancement Slice 1: baseline curve")
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--slots", type=int, default=40, help="how many beds to use")
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--snr", default="-26,-23,-20,-18,-16,-14,-12,-10")
    parser.add_argument("--configs", default="fast,p1c3lt-s3")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--library", default=None)
    args = parser.parse_args(argv)

    beds = sorted(args.corpus.glob("*.raw"))[: args.slots]
    if not beds:
        print(f"no slots under {args.corpus}")
        return 2
    binding = CoreBinding(args.library or default_library_path())
    configs = tuple(CONFIGS[name] for name in args.configs.split(","))
    print(f"beds: {len(beds)} slots   configs: {', '.join(c.name for c in configs)}")
    report = sweep(
        binding,
        beds,
        snr_values=_parse_snr(args.snr),
        trials=args.trials,
        configs=configs,
    )
    if args.json:
        args.json.write_text(json.dumps(report, indent=1, sort_keys=True))
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
