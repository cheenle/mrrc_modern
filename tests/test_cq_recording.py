"""CQ 录音：校验、峰值归一、原子写，以及它落到界面与路由上的样子。

规格 `docs/superpowers/specs/2026-10-26-cq-recording-design.md`。这里的断言分两层：

* **纯逻辑**（`cq_asset.py` + `cq_player.py`）——不碰硬件、不碰网络；
* **源码级契约**——四个路由、两道 409 闸门、`settings.html` 的卡片与模块 pin。
  真正的录音要麦克风权限与一台浏览器，那部分只能在设备上手验，不在套件里冒充。
"""
from __future__ import annotations

import io
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

import cq_asset
from cq_player import CQPlayer

REPO = Path(__file__).resolve().parents[1]


def make_wav(seconds=2.0, rate=48_000, channels=1, amplitude=10_000,
             freq=440.0) -> bytes:
    """A sine-wave WAV with a known peak — the peak is what we assert on."""
    n = int(rate * seconds)
    t = np.arange(n, dtype=np.float64) / rate
    wave_data = (np.sin(2 * np.pi * freq * t) * amplitude).astype("<i2")
    if channels == 2:
        wave_data = np.repeat(wave_data, 2)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(wave_data.tobytes())
    return out.getvalue()


def peak_of(blob: bytes) -> int:
    with wave.open(io.BytesIO(blob), "rb") as w:
        raw = w.readframes(w.getnframes())
    return int(np.abs(np.frombuffer(raw, dtype="<i2").astype(np.int32)).max())


class InspectTests(unittest.TestCase):
    def test_accepts_mono_44k(self):
        info = cq_asset.inspect_wav(make_wav(rate=44_100))
        self.assertEqual(info["channels"], 1)
        self.assertEqual(info["rate"], 44_100)
        self.assertAlmostEqual(info["duration_s"], 2.0, places=1)

    def test_accepts_stereo_48k(self):
        info = cq_asset.inspect_wav(make_wav(channels=2))
        self.assertEqual(info["channels"], 2)

    def test_rejects_non_wav(self):
        with self.assertRaises(cq_asset.CQAssetError) as ctx:
            cq_asset.inspect_wav(b"ID3\x03\x00not a wav at all")
        self.assertIn("WAV", str(ctx.exception))

    def test_rejects_8_bit(self):
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(1)
            w.setframerate(48_000)
            w.writeframes(b"\x80" * 48_000)
        with self.assertRaises(cq_asset.CQAssetError) as ctx:
            cq_asset.inspect_wav(out.getvalue())
        self.assertIn("16-bit", str(ctx.exception))

    def test_rejects_too_short(self):
        with self.assertRaises(cq_asset.CQAssetError) as ctx:
            cq_asset.inspect_wav(make_wav(seconds=0.2))
        self.assertIn("太短", str(ctx.exception))

    def test_rejects_too_long(self):
        with self.assertRaises(cq_asset.CQAssetError) as ctx:
            cq_asset.inspect_wav(make_wav(seconds=31.0))
        self.assertIn("太长", str(ctx.exception))

    def test_rejects_silence(self):
        with self.assertRaises(cq_asset.CQAssetError) as ctx:
            cq_asset.inspect_wav(make_wav(amplitude=0))
        self.assertIn("静音", str(ctx.exception))

    def test_rejects_too_quiet(self):
        # -60 dBFS is below the -40 dBFS floor.
        with self.assertRaises(cq_asset.CQAssetError) as ctx:
            cq_asset.inspect_wav(make_wav(amplitude=33))
        self.assertIn("音量太低", str(ctx.exception))

    def test_rejects_empty_body(self):
        with self.assertRaises(cq_asset.CQAssetError):
            cq_asset.inspect_wav(b"")

    def test_rejects_oversized_body(self):
        with self.assertRaises(cq_asset.CQAssetError) as ctx:
            cq_asset.inspect_wav(b"RIFF" + b"\x00" * (cq_asset.MAX_BYTES + 1))
        self.assertIn("太大", str(ctx.exception))


class NormalizeTests(unittest.TestCase):
    def test_boosts_a_quiet_take_to_the_target(self):
        blob = make_wav(amplitude=4_100)          # ≈ -18 dBFS
        out, info = cq_asset.normalize_wav(blob)
        self.assertAlmostEqual(info["recorded_peak_dbfs"], -18.0, delta=0.5)
        self.assertAlmostEqual(info["stored_peak_dbfs"], -3.0, delta=0.5)
        self.assertAlmostEqual(info["applied_gain_db"], 15.0, delta=0.5)
        self.assertAlmostEqual(peak_of(out) / cq_asset.FULL_SCALE,
                               10 ** (-3.0 / 20), delta=0.01)

    def test_never_attenuates_a_hot_take(self):
        blob = make_wav(amplitude=30_000)         # ≈ -0.8 dBFS
        out, info = cq_asset.normalize_wav(blob)
        self.assertEqual(info["applied_gain_db"], 0.0)
        self.assertEqual(peak_of(out), peak_of(blob))

    def test_output_survives_the_players_own_validator(self):
        out, info = cq_asset.normalize_wav(make_wav(amplitude=2_000))
        again = cq_asset.inspect_wav(out)
        self.assertEqual(again["channels"], info["channels"])
        self.assertEqual(again["rate"], info["rate"])


class WriteAtomicTests(unittest.TestCase):
    def test_creates_the_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "cq.wav"
            cq_asset.write_atomic(target, b"hello")
            self.assertEqual(target.read_bytes(), b"hello")

    def test_a_failed_write_leaves_the_previous_file_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cq.wav"
            target.write_bytes(b"previous")
            with mock.patch("cq_asset.os.replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    cq_asset.write_atomic(target, b"next")
            self.assertEqual(target.read_bytes(), b"previous")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["cq.wav"])


class PlayerIntegrationTests(unittest.TestCase):
    def test_player_reports_the_stored_peak(self):
        out, info = cq_asset.normalize_wav(make_wav(amplitude=4_100))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cq.wav"
            path.write_bytes(out)
            player = CQPlayer(asset_path=path)
            self.assertTrue(player.load(), player.unavailable_reason)
            status = player.status()
            self.assertTrue(status["ready"])
            self.assertAlmostEqual(status["peak_dbfs"], info["stored_peak_dbfs"], delta=0.3)
            self.assertAlmostEqual(status["duration_s"], 2.0, delta=0.05)

    def test_set_asset_switches_the_source(self):
        a, _ = cq_asset.normalize_wav(make_wav(seconds=1.0))
        b, _ = cq_asset.normalize_wav(make_wav(seconds=3.0))
        with tempfile.TemporaryDirectory() as tmp:
            pa, pb = Path(tmp) / "a.wav", Path(tmp) / "b.wav"
            pa.write_bytes(a)
            pb.write_bytes(b)
            player = CQPlayer(asset_path=pa)
            self.assertTrue(player.load())
            self.assertAlmostEqual(player.duration_s, 1.0, delta=0.05)
            player.set_asset(pb)
            self.assertTrue(player.load())
            self.assertAlmostEqual(player.duration_s, 3.0, delta=0.05)

    def test_a_missing_asset_is_a_clean_refusal(self):
        player = CQPlayer(asset_path=Path("/nonexistent/cq.wav"))
        self.assertFalse(player.load())
        self.assertIn("not found", player.unavailable_reason)


class ServerContractTests(unittest.TestCase):
    """The four routes, the two 409 gates, and the custom-over-shipped order."""

    def setUp(self):
        self.src = (REPO / "server.py").read_text(encoding="utf-8")

    def test_routes_exist(self):
        for route in ('@app.get("/api/cq"', '@app.get("/api/cq/audio"',
                      '@app.post("/api/cq"', '@app.delete("/api/cq"'):
            self.assertIn(route, self.src)

    def test_custom_asset_wins_over_the_shipped_one(self):
        self.assertIn("CQ_CUSTOM_PATH", self.src)
        block = self.src.split("def _cq_effective_path()", 1)[1].split("\n\n", 1)[0]
        self.assertIn("CQ_CUSTOM_PATH.is_file()", block)
        self.assertIn("CQ_ASSET_PATH", block)

    def test_upload_is_refused_while_transmitting_or_calling(self):
        block = self.src.split("async def api_cq_upload(", 1)[1].split("@app.", 1)[0]
        self.assertIn("_cq_write_refusal()", block)
        self.assertIn("409", block)
        guard = self.src.split("def _cq_write_refusal()", 1)[1].split("\n\n\n", 1)[0]
        self.assertIn("is_transmitting", guard)
        self.assertIn("_cq_player.is_calling", guard)

    def test_upload_writes_atomically_and_takes_effect_without_a_restart(self):
        block = self.src.split("async def api_cq_upload(", 1)[1].split("@app.", 1)[0]
        self.assertIn("cq_asset.normalize_wav(", block)
        self.assertIn("cq_asset.write_atomic(", block)
        self.assertIn("_reload_cq_asset()", block)
        self.assertIn("_broadcast_cq_state()", block)


class SettingsPageTests(unittest.TestCase):
    def setUp(self):
        self.html = (REPO / "static" / "settings.html").read_text(encoding="utf-8")

    def test_page_carries_the_card_and_the_module(self):
        self.assertIn("CQ 呼叫录音", self.html)
        self.assertIn("modules/cq_recorder.js?v=1", self.html)

    def test_module_exists_and_exposes_the_recorder(self):
        module = (REPO / "static" / "modules" / "cq_recorder.js").read_text(encoding="utf-8")
        self.assertIn("CQRecorder", module)
        self.assertIn("getUserMedia", module)
        self.assertIn("echoCancellation", module)
        self.assertIn("RIFF", module)          # the WAV header it writes

    def test_page_talks_to_the_cq_api(self):
        for call in ("/api/cq", "/api/cq/audio"):
            self.assertIn(call, self.html)


if __name__ == "__main__":
    unittest.main()
