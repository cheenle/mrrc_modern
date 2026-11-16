"""
FT-710 Audio Resampler
======================
Stateless 44.1 kHz ↔ 48 kHz sample-rate conversion using numpy linear
interpolation.  The FT-710 USB audio interface natively runs at 44.1 kHz
but Opus requires 48 kHz (44.1k is not a supported Opus rate).  These
functions bridge the two domains.

Algorithm: numpy.interp (float32, linear).  Chunk sizes are small (<4000
samples) so the overhead is negligible (~µs per call on modern CPUs).

The ratio 48000/44100 = 160/147 ≈ 1.0884.  Conveniently, exactly 882
samples @ 44.1k = 960 samples @ 48k = 20 ms, so nominal frame boundaries
stay perfectly aligned across resampling — no fractional accumulation.
"""

import numpy as np

HW_RATE = 44100       # FT-710 native USB audio rate
OPUS_RATE = 48000     # Opus codec rate (48 kHz is mandatory for Opus)


def resample_pcm(pcm_bytes: bytes, in_rate: int, out_rate: int) -> bytes:
    """Resample Int16 PCM bytes between two sample rates.

    Uses linear interpolation on float32 arrays.  Handles edge cases:
    empty input, single sample, odd byte count (drops trailing byte).

    Args:
        pcm_bytes: Raw Int16 PCM data (little-endian).
        in_rate: Source sample rate in Hz.
        out_rate: Destination sample rate in Hz.

    Returns:
        Resampled Int16 PCM bytes at out_rate.
    """
    if not pcm_bytes or len(pcm_bytes) < 2:
        return b""

    # Drop trailing byte if odd (broken Int16 boundary)
    byte_len = len(pcm_bytes) & ~1
    if byte_len == 0:
        return b""

    pcm_slice = pcm_bytes[:byte_len]
    in_samples = byte_len // 2
    out_samples = max(1, int(in_samples * out_rate / in_rate + 0.5))

    # Convert to float32 for interpolation
    src = np.frombuffer(pcm_slice, dtype=np.int16).astype(np.float32)

    # Time axes (normalised so dt matches each domain)
    t_in = np.arange(in_samples, dtype=np.float32) / in_rate
    t_out = np.arange(out_samples, dtype=np.float32) / out_rate

    interp = np.interp(t_out, t_in, src).astype(np.float32)

    # Clamp and convert back to int16
    out_i16 = np.clip(np.round(interp), -32768, 32767).astype(np.int16)
    return out_i16.tobytes()


def resample_441_to_48(pcm_bytes: bytes) -> bytes:
    """Resample FT-710 native 44.1 kHz PCM → 48 kHz for Opus encoding."""
    return resample_pcm(pcm_bytes, HW_RATE, OPUS_RATE)


def resample_48_to_441(pcm_bytes: bytes) -> bytes:
    """Resample Opus-decoded 48 kHz PCM → 44.1 kHz for FT-710 playback."""
    return resample_pcm(pcm_bytes, OPUS_RATE, HW_RATE)


from audio_profile import AUDIO_TAG_PCM16, PCM16_FRAME_BYTES


class StreamingPcmResampler:
    def __init__(self, in_rate: int, out_rate: int):
        if in_rate <= 0 or out_rate <= 0 or in_rate == out_rate:
            raise ValueError("SRC requires two different positive rates")
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self._carry = b""
        self._stream = None
        self.reset()

    def reset(self):
        import soxr
        # LQ is required because QQ aliases 48→16 kHz: measured, a full-scale
        # 12 kHz sine comes back at RMS ~0.7 under QQ and ~0.0004 under LQ.
        self._stream = soxr.ResampleStream(
            self.in_rate, self.out_rate, 1, dtype="float32", quality="LQ")
        self._carry = b""

    def _convert(self, pcm: bytes, last: bool) -> bytes:
        if self._carry:
            pcm = self._carry + pcm
            self._carry = b""
        usable = len(pcm) - len(pcm) % 2
        if not last:
            self._carry = pcm[usable:]
        samples = np.frombuffer(pcm[:usable], dtype="<i2")
        audio = self._stream.resample_chunk(samples.astype(np.float32), last=last)
        return np.clip(np.rint(audio), -32768, 32767).astype("<i2").tobytes()

    def process(self, pcm: bytes) -> bytes:
        return self._convert(pcm, last=False)

    def flush(self) -> bytes:
        out = self._convert(b"", last=True)
        # A lone carried byte can never complete a sample; keep the wire even
        # instead of fabricating one half of an Int16.
        self._carry = b""
        return out


class Pcm16RxEncoder:
    def __init__(self):
        self._resampler = StreamingPcmResampler(48000, 16000)
        self._tail = b""

    def _frames(self, converted: bytes) -> list[bytes]:
        buf = self._tail + converted
        frames = []
        offset = 0
        while len(buf) - offset >= PCM16_FRAME_BYTES:
            frames.append(bytes([AUDIO_TAG_PCM16]) + buf[offset:offset + PCM16_FRAME_BYTES])
            offset += PCM16_FRAME_BYTES
        self._tail = buf[offset:]
        return frames

    def push(self, pcm: bytes) -> list[bytes]:
        return self._frames(self._resampler.process(pcm))

    def flush(self) -> list[bytes]:
        return self._frames(self._resampler.flush())

    def reset(self):
        self._resampler.reset()
        self._tail = b""
