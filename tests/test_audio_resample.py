import random
import unittest
import numpy as np

from audio_profile import AUDIO_TAG_PCM16, PCM16_FRAME_BYTES
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

    def test_odd_byte_chunks_lose_nothing_and_match_whole_stream(self):
        # One second of 997 Hz with a lone trailing odd byte.  Legacy per-call
        # truncation dropped ~42 bytes here and desynchronised the output; with
        # the carry every input byte reaches the converter exactly once.
        pcm = tone(48000, 997).astype("<i2").tobytes() + b"\x7f"
        whole = StreamingPcmResampler(48000, 16000)
        expected = whole.process(pcm[:-1]) + whole.flush()
        chunked = StreamingPcmResampler(48000, 16000)
        actual = bytearray()
        rng = random.Random(20261117)
        offset = 0
        while offset < len(pcm):
            size = rng.choice((1, 3, 5, 641, 1919, 1921))
            actual.extend(chunked.process(pcm[offset:offset + size]))
            offset += size
        tail = chunked.flush()
        # flush must not fabricate an Int16 sample from a lone carried byte
        self.assertEqual(len(tail) % 2, 0)
        actual.extend(tail)
        self.assertEqual(bytes(actual), expected)
        self.assertEqual(len(expected), 16000 * 2)

    def test_reset_clears_carried_byte(self):
        rs = StreamingPcmResampler(48000, 16000)
        rs.process(b"\x01\x02\x03")                       # odd length → one byte held
        self.assertEqual(rs._carry, b"\x03")
        fresh = StreamingPcmResampler(48000, 16000)       # ctor path
        self.assertEqual(fresh._carry, b"")
        rs.reset()
        self.assertEqual(rs._carry, b"")                  # reset clears it too

    def test_arbitrary_chunking_recovers_every_frame_in_order(self):
        # 500 real 20 ms @ 48 kHz blocks (960 samples each) = exactly 10 s of
        # audio = exactly 500 complete PCM16 frames at 16 kHz.
        blocks = [tone(48000, 400 + 80 * (i % 12), 0.02).tobytes()
                  for i in range(500)]
        self.assertEqual(len(blocks), 500)
        stream = b"".join(blocks)
        reference = Pcm16RxEncoder()
        expected = reference.push(stream) + reference.flush()
        self.assertEqual(len(expected), 500)

        enc = Pcm16RxEncoder()
        got = []
        emitted_per_push = []
        rng = random.Random(20261117)
        offset = 0
        while offset < len(stream):
            size = rng.choice((3, 1919, 1920, 1921, 3840, 641))
            frames = enc.push(stream[offset:offset + size])
            emitted_per_push.append(len(frames))
            got.extend(frames)
            offset += size
        got.extend(enc.flush())

        # Proof this is BUFFERED DELAY, not lost audio: priming makes many push
        # calls answer [] (the [0,1,1,…] pattern the reviewer saw), later calls
        # answer more than one frame — list length is NOT a tick count — yet all
        # 500 complete frames come back, in order, byte-identical to the
        # whole-stream result.
        self.assertIn(0, emitted_per_push)
        self.assertGreater(max(emitted_per_push), 1)
        self.assertEqual(len(got), 500)
        for frame in got:
            self.assertEqual(len(frame), PCM16_FRAME_BYTES + 1)
            self.assertEqual(frame[0], AUDIO_TAG_PCM16)
        self.assertEqual(b"".join(got), b"".join(expected))


if __name__ == "__main__":
    unittest.main()
