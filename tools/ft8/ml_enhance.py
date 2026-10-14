"""P3 / Slice 2 — the mask model, its training loop, and the metric that counts.

The design (``docs/superpowers/specs/2026-10-14-ft8-ml-enhancement-design.md``)
fixes what this module may and may not do.  It may not touch the judge: the
decoder stays the only thing that decides whether a signal was heard, so every
claim here reduces to *the same decoder, on better input*.  It may not lose
recall either, which is why enhancement is applied as an extra input alongside
the untouched one and the results are unioned (``cascade.merge_batches``) rather
than substituted.

Slice 1 measured the target: the live decoder is at 100% down to -22 dB nominal
and 31% at -26, while a tenth of the corpus's real decodes sit at -18 dB or
below.  So the model's job is narrow and measurable — move that cliff a few dB —
and its first honest score is :func:`simulate_recall`, where the ground truth is
one's own synthesized signal.

Training pairs come from :mod:`ml_dataset` (generated, not stored).  This module
owns the representation choice's consequences: a mask in the decoder's own tone
grid (6.25 Hz x 40 ms), applied with the noisy phase, with ``alpha`` as the
noise-floor lever the design calls for.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

_APP_ROOT = Path(__file__).resolve().parents[2]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from ft8.core.binding import CoreBinding
from ft8.core.models import DecodeConfig, DecodePath
from ft8.core.worker import default_library_path

from tools.ft8 import ml_dataset as D
from tools.ft8 import ml_simulate as M


def _torch():
    """Import torch lazily so the rest of the FT8 suite never needs it."""

    try:
        import torch  # noqa: F401
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise SystemExit(
            "this track needs torch (research environment only — it is deliberately "
            "absent from requirements.txt and from every shipped image): "
            f"{error}"
        ) from error
    import torch as module

    return module


DEPTH = 4
BASE_CHANNELS = 16


def build_model(base: int = BASE_CHANNELS, depth: int = DEPTH):
    """Return a small U-Net mapping a noisy log-magnitude to an amplitude mask.

    Small on purpose: inference must be cheap enough to run on every slot
    alongside the decoder, and the structure to learn (eight tones moving in a
    fixed grid) is far simpler than speech.  Shapes are padded to a multiple of
    ``2**depth`` inside ``forward``, so odd frame counts are fine.
    """

    torch = _torch()
    nn = torch.nn

    class MaskUNet(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.depth = depth
            channels = [base * (2 ** min(level, 3)) for level in range(depth + 1)]
            self.encoder = nn.ModuleList()
            previous = 1
            for index, width in enumerate(channels):
                stride = 1 if index == 0 else 2
                self.encoder.append(
                    nn.Sequential(
                        nn.Conv2d(previous, width, 3, stride=stride, padding=1),
                        nn.GroupNorm(max(1, width // 8), width),
                        nn.LeakyReLU(0.1, inplace=True),
                        nn.Conv2d(width, width, 3, padding=1),
                        nn.GroupNorm(max(1, width // 8), width),
                        nn.LeakyReLU(0.1, inplace=True),
                    )
                )
                previous = width
            self.decoder = nn.ModuleList()
            for level in range(depth, 0, -1):
                self.decoder.append(
                    nn.Sequential(
                        nn.Conv2d(channels[level] + channels[level - 1], channels[level - 1], 3, padding=1),
                        nn.GroupNorm(max(1, channels[level - 1] // 8), channels[level - 1]),
                        nn.LeakyReLU(0.1, inplace=True),
                        nn.Conv2d(channels[level - 1], channels[level - 1], 3, padding=1),
                        nn.GroupNorm(max(1, channels[level - 1] // 8), channels[level - 1]),
                        nn.LeakyReLU(0.1, inplace=True),
                    )
                )
            self.head = nn.Conv2d(channels[0], 1, 1)

        def forward(self, x):  # type: ignore[no-untyped-def]
            multiple = 2 ** self.depth
            height, width = x.shape[-2:]
            pad_h = (-height) % multiple
            pad_w = (-width) % multiple
            if pad_h or pad_w:
                x = torch.nn.functional.pad(x, (0, pad_w, 0, pad_h), mode="replicate")
            skips = []
            for index, block in enumerate(self.encoder):
                if index:
                    skips.append(x)
                x = block(x)
            for level, block in enumerate(self.decoder):
                skip = skips.pop()
                x = torch.nn.functional.interpolate(x, size=skip.shape[-2:], mode="nearest")
                x = block(torch.cat([x, skip], dim=1))
            mask = torch.sigmoid(self.head(x))
            return mask[..., :height, :width]

    return MaskUNet()


def render_batch(
    binding: CoreBinding,
    beds: list[np.ndarray],
    bank: M.WaveformBank,
    indices: list[int],
    *,
    seed: int,
) -> tuple[np.ndarray, np.ndarray, tuple[D.Render, ...]]:
    """Render and featurize one batch, returning numpy arrays ready for torch."""

    inputs, targets, renders = [], [], []
    for index in indices:
        render = D.render_slot(binding, beds, index, seed=seed, waves=bank)
        model_input, target = D.features(render)
        inputs.append(model_input)
        targets.append(target)
        renders.append(render)
    return np.stack(inputs)[:, None], np.stack(targets)[:, None], tuple(renders)


def mask_loss(prediction, target):  # type: ignore[no-untyped-def]
    """Weighted MSE: signal-bearing bins matter more than the empty majority."""

    torch = _torch()
    weight = 1.0 + 4.0 * (target > 0.2).to(target.dtype)
    return (weight * (prediction - target) ** 2).mean()


def si_snr(estimate: np.ndarray, reference: np.ndarray) -> float:
    """Scale-invariant SNR in dB between two int16-scaled waveforms."""

    estimate = estimate.astype(np.float64)
    reference = reference.astype(np.float64)
    alpha = float(np.dot(estimate, reference) / max(float(np.dot(reference, reference)), 1e-9))
    projection = alpha * reference
    noise = estimate - projection
    return float(10.0 * np.log10(max(np.dot(projection, projection), 1e-9) / max(np.dot(noise, noise), 1e-9)))


def enhance(pcm: np.ndarray, model, alpha: float = 1.0) -> np.ndarray:  # type: ignore[no-untyped-def]
    """Run the mask over one slot, returning the reconstructed int16 PCM."""

    torch = _torch()
    spectrum = D.stft(pcm)
    band = spectrum[D.BAND]
    magnitude = np.abs(band)
    median = float(np.median(magnitude)) + 1e-9
    features = np.log(magnitude / median + 1e-3).astype(np.float32)
    tensor = torch.from_numpy(features)[None, None].to(_device_of(model))
    with torch.no_grad():
        mask = model(tensor)[0, 0].float().cpu().numpy()
    shaped = (1.0 - alpha) + alpha * mask
    spectrum[D.BAND] = band * shaped
    reconstructed = D.istft(spectrum)
    return np.clip(np.round(reconstructed), -32768.0, 32767.0).astype(np.int16)


def _device_of(model):  # type: ignore[no-untyped-def]
    torch = _torch()
    for parameter in model.parameters():
        return parameter.device
    return torch.device("cpu")


def pick_device() -> str:
    torch = _torch()
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def train(
    binding: CoreBinding,
    beds: list[np.ndarray],
    bank: M.WaveformBank,
    *,
    steps: int,
    batch: int,
    lr: float = 3e-4,
    seed: int = 4242,
    log_every: int = 25,
    device: str | None = None,
    out: Path | None = None,
) -> dict[str, object]:
    """Train the mask model, reporting the sim-domain reconstruction quality."""

    torch = _torch()
    device = device or pick_device()
    model = build_model().to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    generator = np.random.default_rng(seed)
    history: list[dict[str, float]] = []
    started = time.monotonic()
    model.train()
    for step in range(1, steps + 1):
        indices = [int(value) for value in generator.integers(0, 10_000_000, batch)]
        inputs, targets, renders = render_batch(binding, beds, bank, indices, seed=seed)
        x = torch.from_numpy(inputs).to(device)
        y = torch.from_numpy(targets).to(device)
        prediction = model(x)
        loss = mask_loss(prediction, y)
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
        optimizer.step()
        if step % log_every == 0 or step == steps:
            # Reconstruction quality on the batch just trained: the number that
            # has to move before the decoder is asked anything.
            with torch.no_grad():
                gains = []
                for render, mask_row in zip(renders, prediction.detach().cpu().numpy()[:, 0]):
                    enhanced = _reconstruct(render.noisy, mask_row)
                    gains.append(si_snr(enhanced, render.clean) - si_snr(render.noisy, render.clean))
            record = {
                "step": step,
                "loss": float(loss.detach().cpu()),
                "si_snr_gain_db": float(np.mean(gains)),
                "minutes": (time.monotonic() - started) / 60.0,
            }
            history.append(record)
            print(
                f"  step {step:>5}/{steps}  loss {record['loss']:.4f}  "
                f"SI-SNR gain {record['si_snr_gain_db']:+.2f} dB  "
                f"({record['minutes']:.1f} min)",
                flush=True,
            )
    if out is not None:
        torch.save({"model": model.state_dict(), "history": history}, out)
        print(f"wrote {out}")
    return {"device": device, "history": history}


def _reconstruct(noisy: np.ndarray, band_mask: np.ndarray) -> np.ndarray:
    spectrum = D.stft(noisy)
    spectrum[D.BAND] = spectrum[D.BAND] * np.asarray(band_mask, dtype=np.float32)
    return D.istft(spectrum)


def simulate_recall(
    binding: CoreBinding,
    model,
    beds: list[np.ndarray],
    bank: M.WaveformBank,
    *,
    snr_values: tuple[float, ...],
    trials: int,
    threads: int,
    alpha: float,
    profile: int,
    cycles: int,
    sensitivity: int,
    low_threshold: bool,
    seed: int = 99,
) -> dict[str, object]:
    """Decode synthesized signals plain and enhanced — the first honest score.

    Ground truth is exact here (the signal was placed by us), so this answers the
    narrow question the design asks before any real-domain work: *does the same
    decoder, on enhanced input, hear a weak signal more often?*  It cannot show
    that enhancement is safe on real signals — that is Slice 3's job — but it can
    cheaply show that it does nothing, which is the likely failure mode.
    """

    torch = _torch()
    device = _device_of(model)
    config = DecodeConfig(
        path=DecodePath.IMPROVED,
        profile=profile,
        cycles=cycles,
        threads=threads,
        sensitivity=sensitivity,
        low_threshold=low_threshold,
    )
    generator = np.random.default_rng(seed)
    report: dict[str, object] = {"alpha": alpha, "trials": trials, "by_snr": {}}
    for snr in snr_values:
        plain_hits = enhanced_hits = 0
        for trial in range(trials):
            bed = beds[int(generator.integers(0, len(beds)))]
            frequency = M.quiet_frequency(bed, generator)
            signal = M.SyntheticSignal(
                message=str(generator.choice(D._messages())),
                frequency=frequency,
                dt=float(generator.uniform(M.DT_MIN, M.DT_MAX)),
                snr_db=float(snr),
            )
            pcm = M.synthesize(binding, bed, (signal,), waves=bank)
            model.eval()
            with torch.no_grad():
                enhanced = enhance(pcm, model, alpha)
            plain_batch = binding.decode(pcm, config, 30_000 + trial)
            enhanced_batch = binding.decode(enhanced, config, 40_000 + trial)
            plain_hits += M.found(plain_batch.results, signal)
            enhanced_hits += M.found(enhanced_batch.results, signal)
        report["by_snr"][f"{snr:+.0f}"] = {
            "plain": plain_hits,
            "enhanced": enhanced_hits,
            "trials": trials,
        }
        print(
            f"  {snr:+5.0f} dB  plain {plain_hits:>3}/{trials}  enhanced {enhanced_hits:>3}/{trials}",
            flush=True,
        )
    return report


def real_union(
    binding: CoreBinding,
    model,
    slots: list[Path],
    *,
    alpha: float,
    threads: int,
    limit: int | None = None,
) -> dict[str, object]:
    """Decode real slots plain and enhanced, and report what the *union* gains.

    This is the decisive metric: enhancement is deployed as an extra input beside
    the untouched one (``cascade.merge_batches`` unions them), so a message the
    enhanced copy loses costs nothing — the plain copy still heard it.  What
    matters is only whether the enhanced copy hears something new, and whether
    that something survives a precision check.

    The precision check available here is deliberately weak but honest: a new
    message found only by the enhanced copy is reported for inspection rather
    than counted as a win, because a mask that manufactures a decodable signal
    would also produce "new messages".
    """

    torch = _torch()
    config = DecodeConfig(
        path=DecodePath.IMPROVED, profile=1, cycles=3, threads=threads,
        sensitivity=3, low_threshold=True,
    )
    model.eval()
    plain_total = enhanced_total = gained_total = 0
    gained: list[str] = []
    for index, path in enumerate(slots if limit is None else slots[:limit]):
        pcm = np.frombuffer(path.read_bytes(), dtype=np.int16)
        with torch.no_grad():
            enhanced = enhance(pcm, model, alpha)
        plain = {result.text for result in binding.decode(pcm, config, 50_000 + index).results}
        better = {result.text for result in binding.decode(enhanced, config, 60_000 + index).results}
        gained_here = better - plain
        plain_total += len(plain)
        enhanced_total += len(better)
        gained_total += len(gained_here)
        for text in sorted(gained_here):
            gained.append(f"{path.name}: {text}")
        if index % 10 == 0:
            print(f"  {index + 1}/{limit or len(slots)}  plain {plain_total}  enhanced {enhanced_total}  gained {gained_total}",
                  flush=True)
    print(f"plain {plain_total}   enhanced {enhanced_total}   only-enhanced {gained_total}")
    for line in gained[:40]:
        print(f"   + {line}")
    return {
        "alpha": alpha,
        "slots": limit or len(slots),
        "plain_total": plain_total,
        "enhanced_total": enhanced_total,
        "gained_total": gained_total,
        "gained": gained,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P3 Slice 2: mask model and its first score")
    parser.add_argument("command", choices=("train", "eval", "real"))
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--model", type=Path, default=None)
    parser.add_argument("--steps", type=int, default=2000)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--log-every", type=int, default=100)
    parser.add_argument("--seed", type=int, default=4242)
    parser.add_argument("--device", default=None)
    parser.add_argument("--bundles", type=int, default=64, help="how many beds to load")
    parser.add_argument("--snr", default="-26,-24,-22")
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--alpha", type=float, default=1.0)
    parser.add_argument("--slot-limit", type=int, default=50)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    binding = CoreBinding(default_library_path())
    beds = D.training_beds(args.corpus, binding, limit=args.bundles)
    bank = M.WaveformBank(binding, D._messages())
    print(f"beds: {len(beds)}   device: {pick_device() if args.device is None else args.device}")

    if args.command == "train":
        if args.model is None:
            parser.error("train needs --model <path>")
        report = train(
            binding,
            beds,
            bank,
            steps=args.steps,
            batch=args.batch,
            lr=args.lr,
            seed=args.seed,
            log_every=args.log_every,
            device=args.device,
            out=args.model,
        )
    else:
        if args.model is None:
            parser.error("eval/real need --model <path>")
        torch = _torch()
        payload = torch.load(args.model, map_location="cpu", weights_only=False)
        model = build_model()
        model.load_state_dict(payload["model"])
        model.eval()
        if args.command == "real":
            report = real_union(
                binding,
                model,
                sorted(args.corpus.glob("*.raw")),
                alpha=args.alpha,
                threads=args.threads,
                limit=args.slot_limit,
            )
            if args.json:
                args.json.write_text(json.dumps(report, indent=1, sort_keys=True))
                print(f"wrote {args.json}")
            return 0
        report = simulate_recall(
            binding,
            model,
            beds,
            bank,
            snr_values=tuple(float(part) for part in args.snr.split(",")),
            trials=args.trials,
            threads=args.threads,
            alpha=args.alpha,
            profile=1,
            cycles=3,
            sensitivity=3,
            low_threshold=True,
        )
    if args.json:
        args.json.write_text(json.dumps(report, indent=1, sort_keys=True))
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
