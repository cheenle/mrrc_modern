"""Hardware-independent regressions for IC-7300 runtime reliability."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys
import unittest
from typing import cast
from unittest.mock import AsyncMock, patch

from audio_handler import AudioHandler
from backends.ic7300.backend import (
    DEFAULT_SCOPE_SPAN,
    IC7300Backend,
    IC7300MK2Backend,
)
from backends.ic7300.civ_controller import SETMODE_CIV_TRANSCEIVE_MK2
from backends.ic7300.civ_codec import build_frame
from backends.ic7300.civ_controller import CivController
import server


REPO_ROOT = Path(__file__).resolve().parents[1]


class BackendBaudDefaultsTests(unittest.TestCase):
    def _baud(self, model: str, **extra: str) -> int:
        env = os.environ.copy()
        for key in ("MRRC_RADIO_MODEL", "MRRC_BAUD_RATE", "FT710_BAUD_RATE"):
            env.pop(key, None)
        env["MRRC_RADIO_MODEL"] = model
        env.update(extra)
        out = subprocess.check_output(
            [sys.executable, "-c", "import config; print(config.BAUD_RATE)"],
            cwd=REPO_ROOT,
            env=env,
            text=True,
        )
        return int(out.strip())

    def test_ft710_default_baud_is_38400(self) -> None:
        self.assertEqual(self._baud("ft710"), 38400)

    def test_icom_defaults_are_115200(self) -> None:
        for model in ("ic7300", "ic7300mk2"):
            with self.subTest(model=model):
                self.assertEqual(self._baud(model), 115200)

    def test_explicit_baud_overrides_backend_default(self) -> None:
        self.assertEqual(
            self._baud("ic7300", MRRC_BAUD_RATE="57600"),
            57600,
        )


class ScopeQueueFreshnessTests(unittest.TestCase):
    def test_scope_queue_is_bounded(self) -> None:
        controller = CivController("/dev/null")
        self.assertGreater(controller.scope_queue.maxsize, 0)

    def test_full_queue_drops_oldest_and_keeps_latest(self) -> None:
        controller = CivController("/dev/null")
        for sequence in range(controller.scope_queue.maxsize + 5):
            controller._enqueue_scope_segment(sequence)

        self.assertEqual(
            controller.scope_queue.qsize(),
            controller.scope_queue.maxsize,
        )
        self.assertEqual(controller.scope_queue.get_nowait(), 5)
        newest = None
        while not controller.scope_queue.empty():
            newest = controller.scope_queue.get_nowait()
        self.assertEqual(newest, controller.scope_queue.maxsize + 4)
        self.assertEqual(controller.scope_queue_drops, 5)


class _FakeScope:
    def __init__(self, *, connected: bool, frame_count: int, payload: bytes):
        self.connected = connected
        self._frame_count = frame_count
        self._payload = payload

    def get_spectrum_binary(self) -> bytes:
        return self._payload


class SpectrumBroadcastPolicyTests(unittest.TestCase):
    def test_broadcast_target_is_30_fps(self) -> None:
        self.assertEqual(server.SPECTRUM_BROADCAST_FPS, 30)

    def test_real_scope_frame_is_not_repeated(self) -> None:
        scope = _FakeScope(connected=True, frame_count=7, payload=b"frame-7")
        payload, cursor = server._new_real_spectrum_frame(scope, -1)
        self.assertEqual((payload, cursor), (b"frame-7", 7))
        self.assertEqual(
            server._new_real_spectrum_frame(scope, cursor),
            (None, cursor),
        )

    def test_new_real_scope_frame_advances_cursor(self) -> None:
        scope = _FakeScope(connected=True, frame_count=8, payload=b"frame-8")
        self.assertEqual(
            server._new_real_spectrum_frame(scope, 7),
            (b"frame-8", 8),
        )


class _FakePyAudioForDiagnostics:
    def __init__(self, *, missing_host: bool = False):
        self._missing_host = missing_host

    @staticmethod
    def get_device_info_by_index(index: int) -> dict:
        return {
            "index": index,
            "name": "USB Audio CODEC",
            "hostApi": 0,
            "defaultSampleRate": 44100.0,
        }

    def get_host_api_info_by_index(self, index: int) -> dict:
        if self._missing_host:
            raise OSError("host API metadata unavailable")
        return {"index": index, "name": "Core Audio"}


class AudioDeviceDiagnosticsTests(unittest.TestCase):
    def test_device_summary_includes_host_api_and_rates(self) -> None:
        handler = AudioHandler.__new__(AudioHandler)
        handler._pa = _FakePyAudioForDiagnostics()
        summary = handler._device_summary(0, actual_rate=48000, channels=1)
        self.assertIn("host=Core Audio", summary)
        self.assertIn("default=44100Hz", summary)
        self.assertIn("actual=48000Hz", summary)
        self.assertIn("channels=1", summary)

    def test_device_summary_survives_missing_host_api_metadata(self) -> None:
        handler = AudioHandler.__new__(AudioHandler)
        handler._pa = _FakePyAudioForDiagnostics(missing_host=True)
        self.assertIn("host=unknown", handler._device_summary(0, 48000, 1))


class _RecordingScopeCiv:
    connected = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def set_scope_on(self, on: bool) -> None:
        self.calls.append(("display", on))

    async def set_scope_mode(self, mode: int) -> None:
        self.calls.append(("mode", mode))

    async def set_scope_span(self, span: int) -> None:
        self.calls.append(("span", span))

    async def set_scope_data_output(self, on: bool) -> None:
        self.calls.append(("data", on))


class IC7300ModelAndPowerTests(unittest.IsolatedAsyncioTestCase):
    def test_mk2_backend_selects_mk2_transceive_item(self) -> None:
        backend = IC7300MK2Backend("/dev/null")
        self.assertEqual(
            backend._civ._transceive_cmd,
            SETMODE_CIV_TRANSCEIVE_MK2,
        )

    async def test_power_health_uses_frequency_query(self) -> None:
        backend = IC7300Backend("/dev/null")
        backend._civ.get_frequency = AsyncMock(return_value=14_200_000)
        backend._civ._query_data = AsyncMock()

        self.assertIs(await backend._get_power_on(timeout=0.4), True)

        backend._civ.get_frequency.assert_awaited_once_with(timeout=0.4)
        backend._civ._query_data.assert_not_awaited()

    async def test_power_health_timeout_remains_unknown(self) -> None:
        backend = IC7300Backend("/dev/null")
        backend._civ.get_frequency = AsyncMock(return_value=None)
        self.assertIsNone(await backend._get_power_on(timeout=0.4))


class ScopeActivationTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_enables_display_before_data_output(self) -> None:
        backend = IC7300Backend("/dev/null")
        fake = _RecordingScopeCiv()
        backend._civ = cast(CivController, fake)

        await backend.init_scope()

        self.assertEqual(
            fake.calls,
            [
                ("display", True),
                ("mode", 0),
                ("span", DEFAULT_SCOPE_SPAN),
                ("data", True),
            ],
        )

    def test_diagnostic_scope_frames_match_official_order(self) -> None:
        diag = IC7300DiagnosticProtocolTests._diag()
        self.assertEqual(
            diag.scope_enable_frames(0x94),
            [
                build_frame(0x27, b"\x10\x01", to=0x94),
                build_frame(0x27, b"\x14\x00", to=0x94),
                build_frame(0x27, b"\x15\x05", to=0x94),
                build_frame(0x27, b"\x11\x01", to=0x94),
            ],
        )


class IC7300DiagnosticProtocolTests(unittest.TestCase):
    @staticmethod
    def _diag():
        with patch.object(sys, "argv", ["_diag_ic7300_scope.py"]):
            return importlib.import_module("_diag_ic7300_scope")

    def test_frequency_query_has_no_checksum_byte(self) -> None:
        diag = self._diag()
        self.assertEqual(
            diag.build_frame(0x03, to=0x94),
            bytes.fromhex("FE FE 94 E0 03 FD"),
        )

    def test_standard_radio_reply_parses_and_decodes_frequency(self) -> None:
        diag = self._diag()
        parser = diag.CivFrameParser()
        frames = parser.feed(bytes.fromhex("FE FE E0 94 03 00 40 07 14 00 FD"))
        self.assertEqual(len(frames), 1)
        self.assertEqual(diag.decode_freq_bcd(frames[0].data), 14_074_000)


if __name__ == "__main__":
    unittest.main()
