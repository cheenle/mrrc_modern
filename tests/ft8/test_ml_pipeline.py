"""Guards for the P3 training pipeline (Slice 2).

Three claims hold this pipeline together, and each is a place where a silent
mistake would survive every downstream metric:

* **the representation round-trips** — a mask of 1 must reproduce the input
  sample-for-sample, otherwise "enhancement" is a lossy codec and every score
  measures the codec;
* **the training target is exact** — ``clean`` must be the same signals at the
  same gains as ``noisy``, or the model learns to reconstruct something nobody
  transmitted;
* **generation is deterministic** — pair ``index`` must be the same pair on every
  run, or a checkpoint cannot be compared with its own training set.

Torch is optional here on purpose: the shipped images and the default test
environment have no torch, and the parts that need it are skipped rather than
skipping the file.
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
from ft8.core.models import FT8_RX_SAMPLES, DecodeConfig, DecodePath  # noqa: E402
from ft8.core.worker import default_library_path  # noqa: E402
from tools.ft8 import ml_dataset as D  # noqa: E402
from tools.ft8 import ml_simulate as M  # noqa: E402


@pytest.fixture(scope="module")
def binding() -> CoreBinding:
    library = default_library_path()
    if not Path(library).exists():
        pytest.skip(f"DSP library not built: {library}")
    return CoreBinding(library)


@pytest.fixture(scope="module")
def bed() -> np.ndarray:
    return np.random.default_rng(3).normal(0, 600, FT8_RX_SAMPLES).astype(np.float32)


@pytest.fixture(scope="module")
def quiet_bed(bed: np.ndarray) -> D.Bed:
    """A bed with no stations of its own — the simplest case for the target."""

    return D.Bed(pcm=bed, noise=bed, signals=())


def test_stft_round_trips_a_slot_exactly(binding: CoreBinding, bed: np.ndarray) -> None:
    """A mask of 1 must be lossless, edges included.

    Without the half-window padding, the first and last ~1440 samples reconstruct
    with thousands of counts of error — and dt = -0.5 puts a real signal exactly
    there, so the failure would look like "enhancement hurts early signals".
    """

    reconstructed = D.istft(D.stft(bed))
    error = np.abs(reconstructed - bed.astype(np.float32))
    assert error.max() < 0.01
    assert D.istft(D.stft(bed)).shape == (FT8_RX_SAMPLES,)


def test_band_slice_covers_the_ft8_passband() -> None:
    frequencies = np.fft.rfftfreq(D.NFFT, 1.0 / 12000)
    low, high = frequencies[D.BAND][0], frequencies[D.BAND][-1]
    assert low <= 200.0 and high >= 3000.0
    assert D.BAND.step is None


def test_render_is_deterministic(binding: CoreBinding, quiet_bed: D.Bed) -> None:
    """The same index must give the same pair — a checkpoint's own data depends on it."""

    beds = [quiet_bed]
    first = D.render_slot(binding, beds, 7, seed=1234)
    second = D.render_slot(binding, beds, 7, seed=1234)
    other = D.render_slot(binding, beds, 8, seed=1234)
    assert np.array_equal(first.noisy, second.noisy)
    assert first.signals == second.signals
    assert not np.array_equal(first.noisy, other.noisy)


def test_clean_component_is_the_mix_without_the_bed(binding: CoreBinding, quiet_bed: D.Bed) -> None:
    """``clean`` must be the same signals at the same gains as ``noisy``.

    Checked by rebuilding the mix from the two components: the difference between
    ``noisy`` and ``bed + clean`` must be quantization, not signal.  A second,
    drifting copy of the gain law is exactly how a training target stops matching
    its input.
    """

    render = D.render_slot(binding, [quiet_bed], 3)
    rebuilt = quiet_bed.pcm.astype(np.float64) + render.clean.astype(np.float64)
    error = np.abs(np.clip(np.round(rebuilt), -32768, 32767) - render.noisy.astype(np.float64))
    assert error.max() <= 2.0
    placed_energy = float(np.mean(render.clean.astype(np.float64) ** 2))
    bed_energy = float(np.mean(quiet_bed.pcm.astype(np.float64) ** 2))
    # The signals must be present but not dominate the bed at these SNRs.
    assert 0.0 < placed_energy < bed_energy


def test_features_target_is_the_ideal_mask(binding: CoreBinding, quiet_bed: D.Bed) -> None:
    render = D.render_slot(binding, [quiet_bed], 5)
    model_input, target = D.features(render)
    assert model_input.shape == target.shape
    assert target.min() >= 0.0 and target.max() <= 1.0
    # The mask can only be high where the clean signal actually has energy.
    assert float(target.mean()) < 0.2


def test_apply_mask_with_alpha_one_is_lossless_for_a_unit_mask(
    binding: CoreBinding, quiet_bed: D.Bed
) -> None:
    render = D.render_slot(binding, [quiet_bed], 9)
    _, target = D.features(render)
    unit = np.ones_like(target)
    reconstructed = D.apply_mask(render.noisy, unit, alpha=1.0)
    assert np.abs(reconstructed - render.noisy.astype(np.float32)).max() < 1.0


def test_waveform_bank_matches_the_encoder(binding: CoreBinding) -> None:
    """The accelerator shifts the analytic signal; it must stay the same waveform.

    Measured over 50 Hz steps the SINAD is 34-207 dB (median 46): shifting is
    exact in principle, but the base waveform passed the decimation filter at
    1 kHz while the reference passed it at its own frequency, so the ripples
    differ.  At the decoder's weakest operating point (-24 dB nominal) a 34 dB
    distortion sits 10 dB *below* the noise, adding 0.4% to it — 0.017 dB of SNR,
    which cannot move a label.  A *silent* divergence (wrong group delay, a
    mirror image) would move every label, which is why the number is asserted.
    """

    bank = M.WaveformBank(binding, ("CQ JA1VAT QN06",))
    for frequency in (1000.0, 1500.0, 2337.5):
        reference = M.clean_waveform_12k(binding, "CQ JA1VAT QN06", frequency).astype(np.float64)
        shifted = bank.waveform("CQ JA1VAT QN06", frequency).astype(np.float64)
        count = min(reference.size, shifted.size)
        error = reference[:count] - shifted[:count]
        sinad = 10.0 * np.log10(
            float(np.mean(reference[:count] ** 2)) / max(float(np.mean(error**2)), 1e-12)
        )
        assert sinad > 30.0, f"{frequency} Hz: SINAD {sinad:.1f} dB"


def test_notch_removes_a_strong_tone() -> None:
    """A bed's own stations must not survive into the training target."""

    tone_hz, dt = 1500.0, 0.2
    samples = np.arange(FT8_RX_SAMPLES)
    # Only inside the signal's own window: a real burst lasts 12.64 s, and a tone
    # spanning the whole slot would survive in the frames the notch does not own.
    inside = (samples >= int((dt + 0.5) * 12000)) & (samples < int((dt + 0.5) * 12000) + 151_680)
    tone = np.where(inside, 6000.0 * np.sin(2 * np.pi * tone_hz * samples / 12000), 0.0)
    bed = (np.random.default_rng(4).normal(0, 300, FT8_RX_SAMPLES) + tone).astype(np.int16)
    notched = D._notch(bed, ((tone_hz, dt, 5),))
    before = M.tone_group_power(bed.astype(np.float32), tone_hz)
    after = M.tone_group_power(notched.astype(np.float32), tone_hz)
    assert after < before / 20.0


def test_bed_signals_are_regenerated_into_the_target(binding: CoreBinding) -> None:
    """A bed's own stations must be *signals* to the model, not noise.

    Leaving them out of the clean target is what taught the first model to
    suppress real signals: it lost 20% of the plain copy's decodes on real slots
    while gaining almost nothing.  This asserts the repair at its source — the
    bed's station has energy in ``clean`` at its own frequency, which the ideal
    mask then asks to be kept.
    """

    bed_pcm = np.random.default_rng(31).normal(0, 400, FT8_RX_SAMPLES).astype(np.float32)
    # A real, decodable station in the bed, with a plausible reported SNR.
    station = M.SyntheticSignal("CQ DL1ABC JO31", 1234.0, 0.3, -6.0, variant=77)
    waveform = M.waveform_for(binding, station)
    start = station.offset_samples
    bed_pcm[start : start + waveform.size] += waveform * 0.5
    bed_pcm = np.clip(np.round(bed_pcm), -32768, 32767).astype(np.int16)
    batch = binding.decode(bed_pcm, DecodeConfig(path=DecodePath.FAST), 1)
    heard = [(r.text, r.frequency, r.dt, r.snr) for r in batch.results]
    assert heard, "the synthetic station did not decode; the fixture is broken"
    bed = D.Bed(pcm=bed_pcm, noise=D._notch(bed_pcm, tuple((f, d, s) for _, f, d, s in heard)),
                signals=tuple(heard))
    render = D.render_slot(binding, [bed], 0)
    _, target = D.features(render)
    frequencies = np.fft.rfftfreq(D.NFFT, 1.0 / 12000)[D.BAND]
    column = int(np.argmin(np.abs(frequencies - station.frequency)))
    # Somewhere in the station's 12.6 s the target must ask for it to pass.
    assert float(target[column : column + 8].max()) > 0.5


def test_fading_variant_changes_the_waveform(binding: CoreBinding) -> None:
    """The propagation augmentation must actually do something, and stay legal."""

    ideal = M.SyntheticSignal("CQ JA1VAT QN06", 1400.0, 0.2, -18.0)
    faded = M.SyntheticSignal("CQ JA1VAT QN06", 1400.0, 0.2, -18.0, variant=12345)
    plain = M.waveform_for(binding, ideal)
    shaped = M.waveform_for(binding, faded)
    assert plain.shape == shaped.shape
    peak = max(float(np.abs(plain).max()), 1.0)
    assert float(np.abs(plain - shaped).max()) > 0.05 * peak
    assert M.waveform_for(binding, faded).tobytes() == shaped.tobytes()
    # And it must still be a signal the decoder can hear — an augmentation that
    # produces something undecodable would just add noise to the training set.
    bed = np.random.default_rng(9).normal(0, 300, FT8_RX_SAMPLES).astype(np.float32)
    loud = M.SyntheticSignal("CQ JA1VAT QN06", 1400.0, 0.2, -8.0, variant=12345)
    batch = binding.decode(M.synthesize(binding, bed, (loud,)), DecodeConfig(path=DecodePath.FAST), 1)
    assert "CQ JA1VAT QN06" in [result.text for result in batch.results]


def test_mask_model_shape_and_range() -> None:
    torch = pytest.importorskip("torch")
    from tools.ft8 import ml_enhance as E

    model = E.build_model()
    model.eval()
    with torch.no_grad():
        output = model(torch.zeros(2, 1, 465, 376))
    assert output.shape == (2, 1, 465, 376)
    assert float(output.min()) > 0.0 and float(output.max()) < 1.0
    assert 0.2e6 < sum(p.numel() for p in model.parameters()) < 6e6


def test_mask_model_accepts_odd_shapes() -> None:
    torch = pytest.importorskip("torch")
    from tools.ft8 import ml_enhance as E

    model = E.build_model()
    model.eval()
    with torch.no_grad():
        output = model(torch.zeros(1, 1, 37, 19))
    assert output.shape == (1, 1, 37, 19)


def test_si_snr_is_scale_invariant() -> None:
    from tools.ft8 import ml_enhance as E

    reference = np.random.default_rng(5).normal(0, 500, 20000)
    assert E.si_snr(reference * 3.0, reference) > 60.0
    noisy = reference + np.random.default_rng(6).normal(0, 500, 20000)
    # Equal signal and noise powers: SI-SNR lands near 0 dB, and the sign is not
    # the estimator's business.
    assert -3.0 < E.si_snr(noisy, reference) < 3.0
