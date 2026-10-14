"""Guards for the P3 simulator (Slice 1 of the ML-enhancement track).

Every later result — the training corpus, the enhancement's score, the claim that
it helped — rests on this module's arithmetic being right.  A simulator that is
off by 3 dB would not crash anything; it would quietly poison the training set.
So these tests pin the four things that are easy to get wrong and hard to notice:

* the **dt convention** (calibrated: offset 0.5 s is dt 0, not dt 0.5),
* the **SNR axis** (the scaling law, and its agreement with the decoder's own
  estimator on a controlled bed),
* the **noise reference** (the tone group the signal occupies, not the band),
* the **placement** (never on top of a neighbour's signal).

The native library is required for two of them; the rest are pure NumPy.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ft8.core.binding import CoreBinding  # noqa: E402
from ft8.core.models import (  # noqa: E402
    FT8_RX_RATE,
    FT8_RX_SAMPLES,
    DecodeConfig,
    DecodePath,
)
from ft8.core.worker import default_library_path  # noqa: E402
from tools.ft8 import ml_simulate as M  # noqa: E402


@pytest.fixture(scope="module")
def binding() -> CoreBinding:
    library = default_library_path()
    if not Path(library).exists():
        pytest.skip(f"DSP library not built: {library}")
    return CoreBinding(library)


def white_bed(amplitude: float = 200.0, seed: int = 5) -> np.ndarray:
    return np.random.default_rng(seed).normal(0, amplitude, FT8_RX_SAMPLES).astype(np.float32)


def test_noise_estimate_agrees_with_white_noise_theory() -> None:
    """White noise's tone-group power must match the analytic density.

    The conversion is by frequency (``width / resolution``) — a bin-count
    shortcut would be wrong by the ratio of the 15 s window to the decoder's
    1920-sample one, i.e. by ~94x, and nothing downstream would notice.
    """

    amplitude = 300.0
    bed = white_bed(amplitude)
    measured = M.noise_floor_power(bed, 1500.0)
    analytic = amplitude**2 * M.REFERENCE_BANDWIDTH / (FT8_RX_RATE / 2.0)
    assert measured == pytest.approx(analytic, rel=0.2)  # within ~0.8 dB


def test_noise_estimate_ignores_a_neighbouring_signal() -> None:
    """A station 300 Hz away must not be counted as this signal's noise floor."""

    bed = white_bed()
    loud = bed.astype(np.float64) + 3000.0 * np.sin(
        2 * np.pi * 1800.0 * np.arange(bed.size) / FT8_RX_RATE
    )
    assert M.noise_floor_power(loud.astype(np.float32), 1500.0) == pytest.approx(
        M.noise_floor_power(bed, 1500.0), rel=0.35
    )


def test_snr_scaling_is_linear_in_power() -> None:
    """10 dB more nominal SNR must put 10 dB more signal power into the mix.

    This is the law every trial's meaning depends on, so it is measured through
    ``synthesize`` rather than re-derived: the same waveform, the same bed, two
    targets, and the *added* power compared (subtracting the bed cancels it).
    """

    bed = white_bed(seed=11)
    binding = _binding_or_skip()
    added = []
    for snr in (-20.0, -10.0):
        signal = M.SyntheticSignal("CQ JA1VAT QN06", 1500.0, 0.5, snr)
        mixed = M.synthesize(binding, bed, (signal,)).astype(np.float64)
        added.append(float(np.mean((mixed - bed.astype(np.float64)) ** 2)))
    assert 10.0 * np.log10(added[1] / added[0]) == pytest.approx(10.0, abs=0.5)


def test_quiet_frequency_avoids_a_strong_tone() -> None:
    """Placement must not land inside a busy channel.

    The passband percentile was once taken over the whole spectrum instead of the
    passband slice, which selected digitally silent notches (a tone-group power
    of 3 against a band median of ~280) — and made every "nominal SNR" a
    fiction.  A loud tone is the same shape of mistake, easier to see.
    """

    bed = white_bed(seed=3)
    interferer = bed.astype(np.float64).copy()
    frequency = 1200.0
    interferer += 6000.0 * np.sin(2 * np.pi * frequency * np.arange(bed.size) / FT8_RX_RATE)
    interferer = interferer.astype(np.float32)
    generator = np.random.default_rng(2)
    picked = [M.quiet_frequency(interferer, generator) for _ in range(12)]
    loud_group = M.tone_group_power(interferer, frequency)
    assert all(M.tone_group_power(interferer, f) < loud_group / 4.0 for f in picked)


def test_placement_geometry_matches_the_dt_convention(binding: CoreBinding) -> None:
    """A signal placed at offset S is reported at dt = S - 0.5."""

    bed = white_bed(seed=23)
    for offset, expected in ((0.5, 0.0), (1.0, 0.5), (2.0, 1.5)):
        signal = M.SyntheticSignal(
            "CQ JA1VAT QN06", 1500.0, offset - 0.5, -5.0
        )
        pcm = M.synthesize(binding, bed, (signal,))
        batch = binding.decode(pcm, DecodeConfig(path=DecodePath.FAST), 1)
        hit = [r for r in batch.results if abs(r.frequency - 1500.0) <= M.FREQUENCY_TOLERANCE]
        assert hit, f"offset {offset}s did not decode"
        assert hit[0].dt == pytest.approx(expected, abs=0.05)


def test_a_synthesized_signal_decodes_as_itself(binding: CoreBinding) -> None:
    """The end-to-end premise: encode -> mix -> decode returns the same message."""

    bed = white_bed(seed=17)
    signal = M.SyntheticSignal("CQ DL1ABC JO31", 1700.0, 0.3, -8.0)
    pcm = M.synthesize(binding, bed, (signal,))
    batch = binding.decode(pcm, DecodeConfig(path=DecodePath.FAST), 1)
    assert signal.message in [result.text for result in batch.results]


def test_synthesize_rejects_a_signal_that_cannot_fit() -> None:
    """Clipping is a *condition*, not an accident: it must be asked for.

    A signal placed past the end of the window decodes with reduced performance,
    which is a different question from "how weak can a signal be" — so the
    default refuses rather than silently truncating, and the clipped case can be
    requested explicitly.
    """

    bed = white_bed()
    binding = _binding_or_skip()
    too_late = M.SyntheticSignal("CQ JA1VAT QN06", 1500.0, M.DT_MAX + 1.0, -10.0)
    with pytest.raises(ValueError):
        M.synthesize(binding, bed, (too_late,))
    clipped = M.synthesize(binding, bed, (too_late,), allow_clipping=True)
    assert clipped.size == FT8_RX_SAMPLES


def test_synthesize_refuses_a_silent_bed() -> None:
    """A bed with no noise cannot define an SNR — refuse instead of scaling to zero."""

    silent = np.zeros(FT8_RX_SAMPLES, dtype=np.float32)
    signal = M.SyntheticSignal("CQ JA1VAT QN06", 1500.0, 0.5, -20.0)
    with pytest.raises(ValueError, match="no measurable noise"):
        M.synthesize(_binding_or_skip(), silent, (signal,))


def test_output_is_slot_sized_int16() -> None:
    bed = white_bed()
    signal = M.SyntheticSignal("CQ JA1VAT QN06", 1500.0, 0.0, -30.0)
    pcm = M.synthesize(_binding_or_skip(), bed, (signal,))
    assert pcm.dtype == np.int16
    assert pcm.size == FT8_RX_SAMPLES


def _binding_or_skip() -> CoreBinding:
    library = default_library_path()
    if not Path(library).exists():
        pytest.skip(f"DSP library not built: {library}")
    return CoreBinding(library)
