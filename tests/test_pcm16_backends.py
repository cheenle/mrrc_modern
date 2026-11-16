"""PCM16 TX through the network audio adapters (AD-038).

A negotiated PCM16 socket says what rate its bytes are (`feed_tx_audio(pcm,
input_rate=16000)`), and every adapter has to honour that without moving the
rate on the wire: Icom and SCU-LAN delegate to a sound-card handler, Flex
resamples 16 → 24 kHz (the radio only ever takes Opus 24 kHz stereo), SunSDR
resamples 16 → 48 kHz before its existing DSP engine. The legacy default stays
byte-for-byte the pre-feature call — these four tests are exactly the four
shapes, plus the one rule they share: a streaming resampler belongs to a TX
session, so it is dropped when the session starts and stops.

Fakes only: no radio, no socket, no sound card.
"""
import threading
import unittest
from unittest import mock

import numpy as np

from backends.flex.audio import FlexAudioHandler
from backends.icom_net.audio import IcomNetAudioHandler
from backends.sunsdr.audio import SunSDRAudioHandler
from backends.yaesu.scu_lan.audio import ScuLanAudioHandler


class Pcm16BackendTests(unittest.TestCase):
    def test_delegates_preserve_legacy_and_forward_pcm16(self):
        for cls in (IcomNetAudioHandler, ScuLanAudioHandler):
            with self.subTest(cls=cls.__name__):
                tx = mock.Mock()
                handler = cls.__new__(cls)
                handler._tx = tx
                handler.feed_tx_audio(b"48")
                tx.feed_tx_audio.assert_called_once_with(b"48")
                handler.feed_tx_audio(b"16", input_rate=16000)
                tx.feed_tx_audio.assert_called_with(b"16", input_rate=16000)

    def test_flex_pcm16_uses_a_16_to_24k_stream(self):
        handler = FlexAudioHandler.__new__(FlexAudioHandler)
        handler._closed = False
        handler._engine = mock.Mock()
        handler._engine.tx_stream_ready = True
        handler._tx_encoder = mock.Mock()
        handler._tx_encoder.push.return_value = []
        handler._down2 = mock.Mock()
        handler._tx_pcm16_resampler = None
        handler._tx_lock = threading.Lock()
        handler._tx_written = handler._tx_write_err = handler._tx_peak = 0
        pcm = np.zeros(320, dtype="<i2").tobytes()
        with mock.patch("backends.flex.audio.StreamingPcmResampler") as cls:
            cls.return_value.process.return_value = np.zeros(480, dtype="<i2").tobytes()
            handler.feed_tx_audio(pcm, input_rate=16000)
        cls.assert_called_once_with(16000, 24000)
        handler._tx_encoder.push.assert_called_once()
        handler._down2.process.assert_not_called()

    def test_sunsdr_pcm16_converts_before_engine(self):
        handler = SunSDRAudioHandler.__new__(SunSDRAudioHandler)
        handler._closed = False
        handler._engine = mock.Mock()
        handler._tx_pcm16_resampler = None
        handler._tx_lock = threading.Lock()
        handler._tx_written = handler._tx_peak = 0
        converted = b"\x01\x00" * 960
        with mock.patch("backends.sunsdr.audio.StreamingPcmResampler") as cls:
            cls.return_value.process.return_value = converted
            handler.feed_tx_audio(b"\x00\x00" * 320, input_rate=16000)
        cls.assert_called_once_with(16000, 48000)
        handler._engine.feed_tx_audio.assert_called_once_with(converted)

    def test_network_tx_resampler_reset_helper(self):
        for cls in (FlexAudioHandler, SunSDRAudioHandler):
            with self.subTest(cls=cls.__name__):
                handler = cls.__new__(cls)
                handler._tx_pcm16_resampler = mock.Mock()
                handler._reset_tx_pcm16_resampler()
                self.assertIsNone(handler._tx_pcm16_resampler)


if __name__ == "__main__":
    unittest.main()
