import json
from pathlib import Path
import unittest

import server
from radio_state import RadioState


class FakeCat:
    def __init__(self, connected=True):
        self.connected = connected
        self.frequency_calls = []
        self.mode_calls = []
        self.calls = []

    async def set_frequency(self, freq_hz, vfo="A"):
        self.frequency_calls.append((freq_hz, vfo))
        self.calls.append(("freq", freq_hz, vfo))
        return True

    async def set_mode(self, mode_num):
        self.mode_calls.append(mode_num)
        self.calls.append(("mode", mode_num))
        return True


class FakeScheduler:
    def __init__(self):
        self.noted = 0
        self.skips = []

    def note_user_command(self):
        self.noted += 1

    def skip_next_poll(self, field, duration=2.0):
        self.skips.append((field, duration))


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


class MemoryRecallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig_cat = server.cat
        self.orig_radio = server.radio
        self.orig_scheduler = server.scheduler

        server.radio = RadioState(active_vfo="B", vfo_a_freq=14_200_000, vfo_b_freq=7_050_000, mode=2)
        server.scheduler = FakeScheduler()

    def tearDown(self):
        server.cat = self.orig_cat
        server.radio = self.orig_radio
        server.scheduler = self.orig_scheduler

    async def test_mem_recall_defaults_to_active_vfo(self):
        fake_cat = FakeCat()
        server.cat = fake_cat
        ws = FakeWebSocket()

        await server._handle_ws_message(
            ws,
            json.dumps({"type": "memRecall", "freq": 7_270_000, "mode": "LSB"}),
        )

        self.assertEqual(fake_cat.frequency_calls, [(7_270_000, "B"), (7_270_000, "B")])
        self.assertEqual(fake_cat.mode_calls, [1])
        self.assertEqual(
            fake_cat.calls,
            [("freq", 7_270_000, "B"), ("mode", 1), ("freq", 7_270_000, "B")],
        )
        self.assertEqual(server.radio.vfo_b_freq, 7_270_000)
        self.assertEqual(server.radio.vfo_a_freq, 14_200_000)
        self.assertEqual(server.radio.active_vfo, "B")

    async def test_mem_recall_reports_error_when_radio_not_connected(self):
        server.cat = None
        ws = FakeWebSocket()

        await server._handle_ws_message(
            ws,
            json.dumps({"type": "memRecall", "freq": 7_270_000, "mode": "LSB"}),
        )

        self.assertEqual(ws.sent, [{"type": "error", "message": "Radio not connected"}])


class MemoryButtonSourceTests(unittest.TestCase):
    def test_memory_recall_uses_active_vfo_and_suppresses_long_press_click(self):
        ui_source = Path("static/ft710_ui.js").read_text(encoding="utf-8")
        memory_section = ui_source.split("// Memory buttons", 1)[1]
        memory_section = memory_section.split("// Menu", 1)[0]

        self.assertIn("longPressHandled", memory_section)
        self.assertIn("if (longPressHandled)", memory_section)
        self.assertNotIn("vfo: 'A'", memory_section)
