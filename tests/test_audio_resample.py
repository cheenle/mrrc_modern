import unittest
import numpy as np

from audio_profile import AUDIO_TAG_PCM16
from audio_resample import Pcm16RxEncoder, StreamingPcmResampler


def tone(rate, freq, seconds=1.0):
    t = np.arange(round(rate * seconds), dtype=np.float64) / rate
    return (np.sin(2 * np.pi * freq * t) * 30000).astype("<i2")


class StreamingResamplerTests(unittest.TestCase):
    def test_chunking_matches_one_shot_stream(self):
        pcm = tone(48000, 1000).astype("<i2").tobytes()
        whole = StreamingPcmResampler(48000, 16000)
        expected = whole.process(pcm) + whole.flush()
        chunked = StreamingPcmResampler(48000, 16000)
        actual = bytearray()
        for offset in range(0, len(pcm), 1782):
            actual.extend(chunked.process(pcm[offset:offset + 1782]))
        actual.extend(chunked.flush())
        self.assertEqual(bytes(actual), expected)
        self.assertEqual(len(expected), 16000 * 2)

    def test_downsample_rejects_out_of_band_tone(self):
        source = tone(48000, 12000)
        rs = StreamingPcmResampler(48000, 16000)
        out = np.frombuffer(rs.process(source.astype("<i2").tobytes()) + rs.flush(), dtype="<i2")
        self.assertLess(np.sqrt(np.mean(out[1600:].astype(np.float64) ** 2)), 100.0)

    def test_pcm16_encoder_emits_only_complete_tagged_frames(self):
        enc = Pcm16RxEncoder()
        frames = []
        pcm = tone(48000, 1000).astype("<i2").tobytes()
        for offset in range(0, len(pcm), 1920):
            frames.extend(enc.push(pcm[offset:offset + 1920]))
        frames.extend(enc.flush())
        self.assertEqual(len(frames), 50)
        for frame in frames:
            self.assertEqual(len(frame), 641)
            self.assertEqual(frame[0], AUDIO_TAG_PCM16)


if __name__ == "__main__":
    unittest.main()
