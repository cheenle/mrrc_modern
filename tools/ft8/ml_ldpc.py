"""P4 / Slice 3 — a learned decoder for the FT8 LDPC code, on the code's own graph.

Why this stage exists, measured (S2 gate, 30 synthetic slots per point):

    SNR    transformer demodulator    classical chain
    -20 dB   0/30 messages            30/30
    -24 dB   0/30                     22/30
    -26 dB   0/30                     13/30

The demodulator reached 93.7% bit accuracy at -20 dB and produced *no* messages:
0.937**174 is 1e-5.  The classical chain's low-SNR performance is the code's
coding gain, and a per-bit objective cannot learn it.  So the division of labour
is the literature's: a demapper produces soft values, a decoder supplies the
gain.  This module is the decoder, and unlike the demapper it has a clean
baseline — the same LLRs through belief propagation.

The graph is the point.  FT8's code is the (174, 91) LDPC code whose parity
checks we already read out of the encoder's own generator (ml_detr), so the
attention can be masked by the Tanner graph: bits that share a check attend to
each other, which is exactly the message passing a decoder performs.  That is
the Error Correction Code Transformer's idea, and it means the model starts from
the code's structure instead of having to infer it from examples.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np

_APP_ROOT = Path(__file__).resolve().parents[2]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from tools.ft8.ml_detr import CODEWORD_BITS, INFO_BITS, generator_rows, parity_check_matrix

PARITY_BITS = CODEWORD_BITS - INFO_BITS  # 83
# The decoder's own Tanner graph (3 checks per bit), from the file the Fortran
# decoder includes.  H = [G | I] is a *dense* basis of the same code: perfect for
# verifying a codeword, useless for belief propagation, which is why the sparse
# graph is read from where the decoder gets it.
PARITY_SOURCE = Path(__file__).resolve().parents[2] / "ft8" / "dsp" / "vendor" / "ft8" / "ldpc_174_91_c_parity.f90"
CHECKS_PER_BIT = 3


def _parse_fortran_table(text: str, name: str) -> list[int]:
    body = text[text.index(f"data {name}/") :]
    body = body[body.index("/") + 1 :]
    body = body[: body.index("/")]
    return [int(value) for value in re.findall(r"\d+", body)]


def sparse_edges() -> tuple[np.ndarray, np.ndarray]:
    """``(checks, bits)`` for every edge of the code's Tanner graph.

    Read from ``ldpc_174_91_c_parity.f90`` — the same tables the Fortran decoder
    runs on — and cross-checked against H, so the two representations of the code
    cannot drift apart without a test failing.
    """

    text = PARITY_SOURCE.read_text(encoding="utf-8", errors="ignore")
    checks_per_bit = _parse_fortran_table(text, "Mn")
    if len(checks_per_bit) != CODEWORD_BITS * CHECKS_PER_BIT:
        raise RuntimeError(
            f"{PARITY_SOURCE.name}: expected {CODEWORD_BITS * CHECKS_PER_BIT} entries, "
            f"found {len(checks_per_bit)}"
        )
    bit_index = np.repeat(np.arange(CODEWORD_BITS), CHECKS_PER_BIT)
    check_index = np.asarray(checks_per_bit, dtype=np.int64) - 1  # Fortran is 1-based
    order = np.lexsort((bit_index, check_index))
    return check_index[order], bit_index[order]


def encode(messages: np.ndarray) -> np.ndarray:
    """``[B, 91]`` message bits -> ``[B, 174]`` codewords (the code is systematic)."""

    messages = np.asarray(messages, dtype=np.uint8)
    if messages.ndim == 1:
        messages = messages[None]
    parity = (messages @ generator_rows().T) % 2
    return np.concatenate([messages, parity.astype(np.uint8)], axis=1)


def tanner_adjacency() -> np.ndarray:
    """``[174, 174]``: 1 where two bits share a parity check (or are the same bit).

    This is the attention mask.  Building it from H means the model's receptive
    field *is* the code's structure, rather than something it has to discover
    from data.
    """

    check = parity_check_matrix().astype(np.float32)
    adjacency = (check.T @ check) > 0
    np.fill_diagonal(adjacency, True)
    return adjacency.astype(np.float32)


def transmit(codewords: np.ndarray, sigma: float, generator: np.random.Generator) -> np.ndarray:
    """BPSK over AWGN, returning the per-bit LLRs ``2 y / sigma^2``."""

    symbols = 1.0 - 2.0 * np.asarray(codewords, dtype=np.float64)  # 0 -> +1, 1 -> -1
    received = symbols + generator.normal(0.0, sigma, symbols.shape)
    return 2.0 * received / (sigma**2)


def _edges() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The code's graph as a flat edge list: ``(check, bit)`` pairs, by check.

    Sparse (three checks per bit), because min-sum decoding is defined on the
    code's Tanner graph.  Running it on the dense ``[G | I]`` instead — the first
    attempt — fixes syndromes while *increasing* the bit error rate (measured:
    check weight 42 -> 36, BER 12% -> 16%), which is what a decoder looks like
    when its graph is not the one the code was built from.
    """

    rows, columns = sparse_edges()
    starts = np.searchsorted(rows, np.arange(PARITY_BITS))
    return rows, columns, starts


_EDGES: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None


def edges() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    global _EDGES
    if _EDGES is None:
        _EDGES = _edges()
    return _EDGES


def belief_propagation(llrs: np.ndarray, iterations: int = 50, scale: float = 0.75) -> np.ndarray:
    """Normalised min-sum decoding — the baseline the learned decoder must beat.

    Min-sum rather than sum-product: it is what hardware decoders run, and
    comparing against a weaker baseline would only flatter the learned model.
    """

    llrs = np.atleast_2d(np.asarray(llrs, dtype=np.float64))
    rows, columns, starts = edges()
    batch = llrs.shape[0]
    message = np.zeros((batch, columns.size))          # check -> bit, per edge
    total = llrs.copy()
    for _ in range(iterations):
        incoming = total[:, columns] - message         # what each check hears
        magnitude = np.abs(incoming)
        sign = np.where(incoming < 0, -1.0, 1.0)
        smallest = np.minimum.reduceat(magnitude, starts, axis=1)
        product = np.multiply.reduceat(sign, starts, axis=1)
        # "Exclude self": wherever this edge *is* the smallest, use the second
        # smallest instead.  Ties exclude both, which is the usual approximation.
        excluded = np.where(magnitude <= smallest[:, rows], np.inf, magnitude)
        second = np.minimum.reduceat(excluded, starts, axis=1)
        chosen = np.where(magnitude <= smallest[:, rows], second[:, rows], smallest[:, rows])
        message = scale * product[:, rows] * sign * chosen
        total = llrs + _scatter(message, columns, total)
    return total


def _scatter(message: np.ndarray, columns: np.ndarray, like: np.ndarray) -> np.ndarray:
    """Sum the edge messages into their bits."""

    out = np.zeros_like(like)
    for index in range(message.shape[0]):
        np.add.at(out[index], columns, message[index])
    return out


def _hard_decision(llrs: np.ndarray) -> np.ndarray:
    return (llrs < 0).astype(np.uint8)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P4 Slice 3: the LDPC code as a graph")
    parser.add_argument("--check", action="store_true", help="self-check the code utilities")
    args = parser.parse_args(argv)
    if args.check:
        adjacency = tanner_adjacency()
        print(f"Tanner 邻接矩阵 {adjacency.shape}  平均每行连接 {adjacency.sum(1).mean():.1f}")
        generator = np.random.default_rng(0)
        messages = generator.integers(0, 2, (4, INFO_BITS)).astype(np.uint8)
        codewords = encode(messages)
        syndrome = (parity_check_matrix().astype(np.uint8) @ codewords.T) % 2
        print(f"编码 {codewords.shape}  所有码字满足校验: {bool(syndrome.sum() == 0)}")
        llrs = transmit(codewords, sigma=0.7, generator=generator)
        print(f"LLR 形状 {llrs.shape}  σ=0.7 时的误比特率 {float((_hard_decision(llrs) != codewords).mean()):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def tanner_mask() -> np.ndarray:
    """Attention mask ``[174, 174]``: bits that share a check (plus self)."""

    rows, columns = sparse_edges()
    adjacency = np.zeros((CODEWORD_BITS, CODEWORD_BITS), dtype=bool)
    for check in range(int(rows.max()) + 1):
        bits = columns[rows == check]
        adjacency[np.ix_(bits, bits)] = True
    np.fill_diagonal(adjacency, True)
    return adjacency


def build_decoder_model(d_model: int = 96, layers: int = 3, heads: int = 4):
    """A transformer decoder whose attention follows the code's Tanner graph.

    The mask is the point: bits that share a parity check attend to each other,
    which is exactly what belief propagation passes along, so the model starts
    from the code's structure instead of having to infer it from examples — and
    a demapper that could not learn the code at all (P4 S2, measured) becomes a
    decoder that only has to *refine* it.
    """

    import torch

    mask = torch.from_numpy(tanner_mask())

    class GraphDecoder(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            # A buffer, not a captured tensor: it has to move with the model
            # (a CPU mask against an MPS batch is a device error, not a wrong
            # answer, but it fails at the first training step).
            negative = torch.zeros(mask.shape, dtype=torch.float).masked_fill(
                ~mask, float("-inf")
            )
            self.register_buffer("attention_mask", negative)
            self.embed = torch.nn.Linear(1, d_model)
            layer = torch.nn.TransformerEncoderLayer(
                d_model, heads, dim_feedforward=4 * d_model, batch_first=True,
                dropout=0.0, activation="gelu", norm_first=True,
            )
            self.encoder = torch.nn.TransformerEncoder(layer, layers)
            self.head = torch.nn.Sequential(
                torch.nn.LayerNorm(d_model), torch.nn.Linear(d_model, 1)
            )

        def forward(self, llrs):  # type: ignore[no-untyped-def]
            # The input is the channel LLR itself; the output is a correction to
            # it, so an untrained model is already a reasonable decoder rather
            # than something that has to learn the identity first.
            hidden = self.encoder(self.embed(llrs.unsqueeze(-1)), mask=self.attention_mask)
            return llrs + self.head(hidden).squeeze(-1)

    return GraphDecoder()


def _sigma_to_ebn0(sigma: float) -> float:
    """BPSK Eb/N0 in dB for a given noise standard deviation (rate 91/174)."""

    return 10.0 * np.log10((91.0 / 174.0) / (2.0 * sigma**2))


def train_neural_decoder(
    *,
    steps: int = 4000,
    batch: int = 256,
    lr: float = 3e-4,
    seed: int = 7,
    sigma_span: tuple[float, float] = (0.55, 1.1),
    log_every: int = 250,
    out: Path | None = None,
) -> dict[str, object]:
    """Train on random codewords over a range of noise levels."""

    import torch

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = build_decoder_model().to(device)
    optimiser = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-5)
    generator = np.random.default_rng(seed)
    history: list[dict[str, float]] = []
    model.train()
    for step in range(1, steps + 1):
        messages = generator.integers(0, 2, (batch, INFO_BITS)).astype(np.uint8)
        codewords = encode(messages)
        sigma = float(generator.uniform(*sigma_span))
        llrs = transmit(codewords, sigma=sigma, generator=generator)
        x = torch.from_numpy(llrs.astype(np.float32) / 2.0).to(device)
        # LLR convention: positive means bit 0 (log P(0)/P(1)).  The BCE target is
        # therefore the *complement* of the codeword, so that "logit > 0" and
        # "llr > 0" agree.  Training with the raw bits teaches the model to invert
        # the channel — measured: BER 95.2% against a raw 4.8%, i.e. 1 - raw,
        # which is what a sign convention error looks like in a results table.
        y = torch.from_numpy((1 - codewords).astype(np.float32)).to(device)
        logits = model(x)
        loss = torch.nn.functional.binary_cross_entropy_with_logits(logits, y)
        optimiser.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
        optimiser.step()
        if step % log_every == 0 or step == steps:
            with torch.no_grad():
                # Measured against the codeword, in the LLR convention.
                hard = (logits < 0).float().cpu().numpy()
                record = {
                    "step": step,
                    "loss": float(loss.detach().cpu()),
                    "bit_accuracy": float((hard == codewords).mean()),
                    "word_accuracy": float((hard == codewords).all(axis=1).mean()),
                }
            history.append(record)
            print(f"  step {step:>5}/{steps}  loss {record['loss']:.4f}  "
                  f"比特 {record['bit_accuracy']:.4f}  整码字 {record['word_accuracy']:.3f}  "
                  f"σ={sigma:.2f}", flush=True)
    if out is not None:
        torch.save({"model": model.state_dict(), "history": history}, out)
        print(f"wrote {out}")
    return {"device": device, "history": history}


def evaluate_decoders(
    model,
    *,
    sigmas: tuple[float, ...],
    blocks: int = 400,
    seed: int = 11,
    bp_iterations: int = 50,
) -> dict[str, object]:
    """Neural decoder vs belief propagation on identical blocks and noise."""

    import torch

    device = next(iter(model.parameters())).device
    generator = np.random.default_rng(seed)
    model.eval()
    report: dict[str, object] = {"blocks": blocks, "by_sigma": {}}
    print(f"{'σ':>6} {'Eb/N0':>7} | {'裸判决':>8} | {'BP':>8} | {'神经译码器':>10}")
    for sigma in sigmas:
        codewords = encode(generator.integers(0, 2, (blocks, INFO_BITS)).astype(np.uint8))
        llrs = transmit(codewords, sigma=sigma, generator=generator)
        raw = error_rates(llrs, codewords)
        bp = error_rates(belief_propagation(llrs, iterations=bp_iterations), codewords)
        with torch.no_grad():
            refined = model(torch.from_numpy(llrs.astype(np.float32) / 2.0).to(device))
        neural = error_rates(refined.cpu().numpy(), codewords)
        report["by_sigma"][f"{sigma:.2f}"] = {
            "raw": raw, "bp": bp, "neural": neural, "ebn0_db": _sigma_to_ebn0(sigma),
        }
        print(f"{sigma:>6.2f} {_sigma_to_ebn0(sigma):>6.1f}dB | {raw[0]:>8.4f} | {bp[0]:>8.4f} | "
              f"{neural[0]:>10.4f}     整码字: BP {bp[1]:.3f} 神经 {neural[1]:.3f}", flush=True)
    return report


def error_rates(llrs: np.ndarray, codewords: np.ndarray) -> tuple[float, float]:
    """``(bit error rate, exact-codeword rate)`` for a set of LLRs."""

    hard = _hard_decision(np.asarray(llrs))
    return float((hard != codewords).mean()), float((hard == codewords).all(axis=1).mean())
