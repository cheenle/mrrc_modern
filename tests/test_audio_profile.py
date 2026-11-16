import json
import unittest

import audio_profile
from opus_rx import AUDIO_TAG_OPUS, AUDIO_TAG_PCM, AUDIO_TAG_PCM16


class AudioCapsTests(unittest.TestCase):
    def test_pcm16_constants(self):
        self.assertEqual(AUDIO_TAG_PCM16, 0x02)
        self.assertEqual(len({AUDIO_TAG_PCM, AUDIO_TAG_OPUS, AUDIO_TAG_PCM16}), 3)
        self.assertEqual(audio_profile.PCM16_RATE, 16000)
        self.assertEqual(audio_profile.PCM16_FRAME_BYTES, 640)

    def test_only_the_exact_pcm16_caps_are_accepted(self):
        text = '{"type":"audioCaps","profile":"pcm16"}'
        self.assertEqual(audio_profile.parse_caps(text), audio_profile.PROFILE_PCM16)
        for bad in (None, b"{}", "not json", "{}",
                    '{"type":"audioCaps","profile":"pcm48"}',
                    '{"type":"other","profile":"pcm16"}'):
            self.assertIsNone(audio_profile.parse_caps(bad))

    def test_accepted_response_describes_the_wire_format(self):
        payload = json.loads(audio_profile.accepted_caps())
        self.assertEqual(payload, {
            "type": "audioCaps", "profile": "pcm16", "accepted": True,
            "rate": 16000, "channels": 1, "format": "s16le", "frameMs": 20,
        })

    def test_rejected_response_names_the_reason(self):
        payload = json.loads(audio_profile.rejected_caps("pcm16-tx-unsupported"))
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["reason"], "pcm16-tx-unsupported")


if __name__ == "__main__":
    unittest.main()
