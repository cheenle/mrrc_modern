"""Pure logic of the on-box Bluetooth microphone (remote_mic.py).

The daemon itself needs BlueZ, bleak and the remote in pairing mode; the ADCPM
decoder, the up-sampler and the caps parser are the parts that can be pinned
without hardware — exactly the parts where a wrong nibble order or a wrong
sample rate would otherwise only show up as "sounds like noise" in the field.

``remote_mic`` imports bleak/dbus-fast at module scope, which are Linux-only
requirements, so the whole module skips cleanly on a test host that lacks them.
"""

import unittest

import numpy as np

try:  # noqa: SIM105 - an import guard is the clearest way to express the skip
    import remote_mic as rm
except Exception:  # noqa: BLE001 - absent bleak/dbus_fast on macOS/Windows
    rm = None


@unittest.skipIf(rm is None, "bleak/dbus-fast not installed (Linux-only deps)")
class TestAdpcmDecoder(unittest.TestCase):
    def test_silence_is_silence(self):
        dec = rm.Decoder()
        dec.reset(0, 0)
        self.assertFalse(dec.decode(bytes(120)).any())

    def test_known_nibbles(self):
        # 0x77 -> nibble 7 twice.  With predictor 0 / step index 0 the standard
        # IMA tables give 11 then 41; this is the vector the macOS reference and
        # audioop.adpcm2lin both produce.
        dec = rm.Decoder()
        dec.reset(0, 0)
        self.assertEqual(dec.decode(b"\x77").tolist(), [11, 41])

    def test_reset_clamps_the_predictor_and_index(self):
        dec = rm.Decoder()
        dec.reset(999999, 999)
        self.assertEqual(dec.predictor, 32767)
        self.assertEqual(dec.step_index, 88)
        dec.reset(-999999, -5)
        self.assertEqual(dec.predictor, -32768)
        self.assertEqual(dec.step_index, 0)


@unittest.skipIf(rm is None, "bleak/dbus-fast not installed (Linux-only deps)")
class TestUpsampler(unittest.TestCase):
    def test_three_times_the_length_and_continuous_across_frames(self):
        up = rm.Upsampler(3)
        first = up.process(np.array([0, 30000], dtype=np.int16))
        self.assertEqual(len(first), 6)
        self.assertEqual(int(first[-1]), 30000)
        second = up.process(np.array([0], dtype=np.int16))
        self.assertEqual(len(second), 3)
        # continues 30000 -> 0 instead of restarting from 0
        self.assertLessEqual(abs(int(second[0]) - 20000), 1)
        self.assertEqual(int(second[-1]), 0)

    def test_ratio_for_eight_khz_caps(self):
        up = rm.Upsampler(6)
        self.assertEqual(len(up.process(np.array([1, 2], dtype=np.int16))), 12)


@unittest.skipIf(rm is None, "bleak/dbus-fast not installed (Linux-only deps)")
class TestCapabilities(unittest.TestCase):
    def test_v1_caps_select_16khz_and_the_wire_frame_size(self):
        mic = rm.RemoteMic()
        mic._parse_caps(bytes.fromhex("0b0100020300780000"))
        self.assertEqual(mic.version, 0x0100)
        self.assertEqual(mic.sample_rate, 16000)
        self.assertEqual(mic.frame_size, 120)
        self.assertEqual(mic.upsampler.ratio, 3)

    def test_older_caps_fall_back_to_eight_khz(self):
        mic = rm.RemoteMic()
        mic._parse_caps(bytes.fromhex("0b0000010100780000"))
        self.assertEqual(mic.sample_rate, 8000)
        self.assertEqual(mic.upsampler.ratio, 6)


if __name__ == "__main__":
    unittest.main()
