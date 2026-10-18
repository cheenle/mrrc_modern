"""P4 Slice 1: the set-prediction pipeline's ground truths.

Two of these exist because ad-hoc checks got them wrong first: the sync pattern
is the only unambiguous witness that the whole chain (dt quantisation, STFT
frame grid, token pooling, frequency mapping) is aligned, and the tone/codeword
round trip is what makes a label a label.  A buggy *check* cost an hour of
"the model cannot learn" that was never true.
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
from ft8.core.worker import default_library_path  # noqa: E402
from tools.ft8 import ml_dataset as D  # noqa: E402
from tools.ft8 import ml_detr as R  # noqa: E402
from tools.ft8 import ml_simulate as M  # noqa: E402


@pytest.fixture(scope="module")
def binding() -> CoreBinding:
    library = default_library_path()
    if not Path(library).exists():
        pytest.skip(f"DSP library not built: {library}")
    return CoreBinding(library)


def test_the_codeword_is_systematic_and_matches_the_encoder(binding: CoreBinding) -> None:
    """The label is the encoder's own codeword, and the first 91 bits are the message."""

    for message in ("CQ BA4EG PM00", "JA1VAT BA4EG R-09", "CQ W1AW FN31"):
        codeword = R.codeword_for(binding, message)
        assert codeword.shape == (R.CODEWORD_BITS,)
        tones = R.tones_from_codeword(codeword)
        assert np.array_equal(R.codeword_from_tones(tones), codeword)
        # And the tones are really the ones in the waveform the encoder produces.
        from_waveform = R.tones_from_waveform(M.clean_waveform_12k(binding, message, 1000.0), 1000.0)
        assert np.array_equal(tones, from_waveform)


def test_the_sync_pattern_lands_where_the_model_will_look(binding: CoreBinding) -> None:
    """The alignment witness: a clean burst's sync tones must read back exactly.

    Everything the pipeline assumes is in this test — dt quantised to the frame
    grid, the STFT's half-window padding, four frames per symbol, and the
    frequency mapping of the crop.  If any of them slips, the sync pattern (which
    is known a priori) stops appearing and nothing else will look wrong.
    """

    bed = np.random.default_rng(5).normal(0, 400, 180000).astype(np.float32)
    beds = [D.Bed(pcm=bed, noise=bed, signals=())]
    waves = M.WaveformBank(binding, D._messages())
    sample = R.sample_for(binding, beds, 0, count=1, snr_span=(10.0, 10.0), waves=waves)
    target = sample.targets[0]
    window = R.crop(sample.features, target.frequency, target.dt)
    assert window.shape[2] == R.BURST_FRAMES

    half = int(round(R.CROP_HALF_WIDTH_HZ / R.TONE_SPACING_HZ))
    for index, position in enumerate(R.SYNC_POSITIONS):
        block = window[0][:, position * 4 : (position + 1) * 4].mean(axis=1)
        expected = half + R.SYNC_SEQUENCE[index % 7]
        assert abs(int(np.argmax(block)) - expected) <= 1, (
            f"sync symbol {position}: peak at bin {int(np.argmax(block))}, expected {expected}"
        )


def test_samples_are_deterministic_and_shaped(binding: CoreBinding) -> None:
    bed = np.random.default_rng(6).normal(0, 400, 180000).astype(np.float32)
    beds = [D.Bed(pcm=bed, noise=bed, signals=())]
    waves = M.WaveformBank(binding, D._messages())
    first = R.sample_for(binding, beds, 3, count=2, waves=waves)
    again = R.sample_for(binding, beds, 3, count=2, waves=waves)
    assert first.features.shape == (3, 465, 376)
    assert len(first.targets) == 2
    assert [t.message for t in first.targets] == [t.message for t in again.targets]
    assert np.array_equal(first.features, again.features)


def test_crop_is_fixed_size_even_for_a_late_signal(binding: CoreBinding) -> None:
    bed = np.random.default_rng(7).normal(0, 400, 180000).astype(np.float32)
    beds = [D.Bed(pcm=bed, noise=bed, signals=())]
    waves = M.WaveformBank(binding, D._messages())
    sample = R.sample_for(binding, beds, 1, count=1, waves=waves)
    target = sample.targets[0]
    for dt in (target.dt, M.DT_MAX, -0.5, 1.7):
        window = R.crop(sample.features, target.frequency, dt)
        assert window.shape == (3, 2 * int(R.CROP_HALF_WIDTH_HZ / R.TONE_SPACING_HZ) + 1, R.BURST_FRAMES)
