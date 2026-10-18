"""Unit tests for local_ctl (SDD AD-028): remote key → radio command.

Everything here is the pure layer — no evdev node, no WebSocket.  The wire
format is pinned because it is the only part that cannot be re-derived from
the source: 24-byte ``input_event`` structs on 64-bit Linux.
"""

import json
import struct
import tempfile
import unittest
from pathlib import Path

import local_ctl as lc


def _ev(ev_type: int, code: int, value: int) -> bytes:
    return lc.INPUT_EVENT.pack(0, 0, ev_type, code, value)


def digit(d: str) -> tuple:
    """Usage id for a digit key on the measured remote (HID 0x1E..0x27)."""
    usage = 0x70027 if d == "0" else 0x7001E + (int(d) - 1)
    return (f"u:0x{usage:x}",)


class TestEventParsing(unittest.TestCase):
    def test_struct_size_is_24_bytes(self):
        self.assertEqual(lc.INPUT_EVENT.size, 24)

    def test_decodes_multiple_events(self):
        blob = _ev(lc.EV_MSC, lc.MSC_SCAN, 0x7001E) + _ev(lc.EV_KEY, 2, 1)
        events = list(lc.parse_events(blob))
        self.assertEqual([(e.type, e.code, e.value) for e in events],
                         [(lc.EV_MSC, lc.MSC_SCAN, 0x7001E), (lc.EV_KEY, 2, 1)])

    def test_partial_tail_is_ignored(self):
        blob = _ev(lc.EV_KEY, 2, 1) + b"\x00" * 10
        self.assertEqual(len(list(lc.parse_events(blob))), 1)

    def test_key_ids_prefers_usage_then_keycode(self):
        self.assertEqual(lc.key_ids(0x7001E, 2), ("u:0x7001e", "k:2"))
        self.assertEqual(lc.key_ids(None, 63), ("k:63",))


class TestKeyMap(unittest.TestCase):
    def test_default_maps_measured_remote(self):
        km = lc.KeyMap()
        self.assertEqual(km.action_for(("u:0x7003e", "k:63")), "ptt")
        self.assertEqual(km.action_for(("u:0x70066", "k:116")), "power")
        # three buttons share keycode 240, only the usage tells them apart
        self.assertEqual(km.action_for(("u:0xc00eb", "k:240")), "mode_next")
        self.assertEqual(km.action_for(("u:0xc00f0", "k:240")), "aux")
        self.assertEqual(km.action_for(("u:0x700cc", "k:240")), "hash")

    def test_enter_is_a_consumer_usage_with_keycode_fallback(self):
        # measured 2026-10-17: 确定 sends 0xc0041 (consumer page), not 0x70058
        self.assertEqual(lc.KeyMap().action_for(("u:0xc0041", "k:353")), "enter")
        self.assertEqual(lc.KeyMap().action_for(("u:0xdead", "k:353")), "enter")

    def test_volume_uses_are_measured_consumer_codes(self):
        self.assertEqual(lc.KeyMap().action_for(("u:0xc00e9", "k:115")), "vol_up")
        self.assertEqual(lc.KeyMap().action_for(("u:0xc00ea", "k:114")), "vol_down")

    def test_keycode_fallback_for_unknown_usage(self):
        self.assertEqual(lc.KeyMap().action_for(("u:0xffff", "k:115")), "vol_up")

    def test_unmapped_key_returns_none(self):
        self.assertIsNone(lc.KeyMap().action_for(("u:0x7ffff", "k:199")))

    def test_json_override_wins_and_bad_file_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.json"
            path.write_text(json.dumps({"u:0x70066": "ptt"}), encoding="utf-8")
            self.assertEqual(lc.KeyMap.load(str(path)).action_for(("u:0x70066",)), "ptt")
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(lc.KeyMap.load(str(path)).action_for(("u:0x70066",)), "power")


class TestNumericEntry(unittest.TestCase):
    def test_khz_typing_commits_to_hz(self):
        ne = lc.NumericEntry()
        for d in "7050":
            ne.push(d, now=1.0)
        self.assertEqual(ne.commit(now=1.2), 7_050_000)

    def test_commit_clears_buffer(self):
        ne = lc.NumericEntry()
        for d in "7050":
            ne.push(d, now=1.0)
        self.assertIsNotNone(ne.commit(now=1.1))
        self.assertEqual(ne.pending, "")
        self.assertIsNone(ne.commit(now=1.2))

    def test_stale_buffer_is_discarded(self):
        ne = lc.NumericEntry()
        ne.push("7", now=1.0)
        self.assertIsNone(ne.commit(now=1.0 + lc.NumericEntry.TIMEOUT_S + 0.1))

    def test_new_digit_after_timeout_starts_over(self):
        ne = lc.NumericEntry()
        ne.push("7", now=1.0)
        ne.push("0", now=1.0 + lc.NumericEntry.TIMEOUT_S + 0.5)
        self.assertEqual(ne.pending, "0")

    def test_out_of_range_is_rejected(self):
        ne = lc.NumericEntry()
        for d in "999999":
            ne.push(d, now=1.0)
        self.assertIsNone(ne.commit(now=1.1))

    def test_max_digits_enforced(self):
        ne = lc.NumericEntry()
        for d in "1234567890":
            ne.push(d, now=1.0)
        self.assertEqual(len(ne.pending), lc.NumericEntry.MAX_DIGITS)


class TestTuning(unittest.TestCase):
    def test_step_within_band(self):
        self.assertEqual(lc.step_freq(7_050_000, 1000, True), 7_051_000)
        self.assertEqual(lc.step_freq(7_050_000, 1000, False), 7_049_000)

    def test_step_wraps_inside_the_band(self):
        self.assertEqual(lc.step_freq(7_300_000, 1000, True), 7_000_000)
        self.assertEqual(lc.step_freq(7_000_000, 1000, False), 7_300_000)

    def test_step_outside_any_band_still_moves(self):
        self.assertEqual(lc.step_freq(60_000_000, 1000, False), 59_999_000)

    def test_band_jump_keeps_relative_position(self):
        # middle of 40 m (7.15 MHz) → middle of the next band up (30 m)
        jumped = lc.next_band(7_150_000, True)
        self.assertTrue(10_100_000 <= jumped <= 10_150_000)

    def test_band_wraps_around(self):
        self.assertTrue(lc.next_band(50_500_000, True) < 2_000_000)

    def test_mode_cycle_wraps_and_handles_unknown(self):
        self.assertEqual(lc.next_mode("USB"), "CW")
        self.assertEqual(lc.next_mode("FT8"), "LSB")
        self.assertEqual(lc.next_mode("???"), "LSB")


class TestController(unittest.TestCase):
    def _ctl(self, **kw):
        return lc.Controller(**kw)

    def test_digits_then_enter_sets_frequency(self):
        ctl = self._ctl()
        for d in "1407":
            ctl.on_key(digit(d), True, now=1.0)
        msgs = ctl.on_key(("u:0xc0041",), True, now=1.1)
        self.assertEqual(msgs, [{"type": "set", "field": "freq", "value": 1_407_000}])

    def test_enter_without_digits_does_nothing(self):
        self.assertEqual(self._ctl().on_key(("u:0xc0041",), True), [])

    def test_ptt_press_and_release_are_edge_triggered(self):
        ctl = self._ctl()
        self.assertEqual(ctl.on_key(("u:0x7003e",), True),
                         [{"type": "set", "field": "ptt", "value": True}])
        self.assertEqual(ctl.on_key(("u:0x7003e",), True), [])       # autorepeat
        self.assertEqual(ctl.on_key(("u:0x7003e",), False),
                         [{"type": "set", "field": "ptt", "value": False}])
        self.assertEqual(ctl.on_key(("u:0x7003e",), False), [])

    def test_release_ptt_is_idempotent(self):
        ctl = self._ctl()
        ctl.on_key(("u:0x7003e",), True)
        self.assertEqual(len(ctl.release_ptt()), 1)
        self.assertEqual(ctl.release_ptt(), [])

    def test_freq_keys_step_by_current_step(self):
        ctl = self._ctl(freq_hz=7_050_000, step_index=1)
        self.assertEqual(ctl.on_key(("u:0x70052",), True),
                         [{"type": "set", "field": "freq", "value": 7_051_000}])
        self.assertEqual(ctl.on_key(("u:0x70051",), True),
                         [{"type": "set", "field": "freq", "value": 7_050_000}])

    def test_step_keys_change_ladder(self):
        ctl = self._ctl(step_index=0)
        ctl.on_key(("u:0x7004f",), True)          # ▶ coarser
        self.assertEqual(ctl.step_hz, 1000)
        for _ in range(3):
            ctl.on_key(("u:0x7004f",), True)
        self.assertEqual(ctl.step_hz, lc.STEP_LADDER_HZ[-1])
        ctl.on_key(("u:0x70050",), True)          # ◀ finer
        self.assertEqual(ctl.step_hz, lc.STEP_LADDER_HZ[-2])

    def test_band_keys_jump_band(self):
        ctl = self._ctl(freq_hz=7_100_000)
        msgs = ctl.on_key(("u:0x7004b",), True)
        self.assertEqual(len(msgs), 1)
        self.assertTrue(10_100_000 <= msgs[0]["value"] <= 10_150_000)

    def test_mode_key_cycles_available_modes(self):
        ctl = self._ctl(mode="LSB", modes=("LSB", "USB", "FT8"))
        self.assertEqual(ctl.on_key(("u:0xc00eb",), True),
                         [{"type": "set", "field": "mode", "value": "USB"}])
        self.assertEqual(ctl.on_key(("u:0xc00eb",), True),
                         [{"type": "set", "field": "mode", "value": "FT8"}])

    def test_volume_keys_span_the_radios_whole_af_gain_register(self):
        # ``af_gain`` is a 0-255 register (``radio_state.af_gain``, Yaesu
        # ``AG0xxx``), while the remote's step is a percentage.  Sending the
        # percentage raw capped the radio at 100/255 = 39 % of full scale
        # (2026-10-18: "音量键按到底也不够大声").
        ctl = self._ctl(volume=95)
        self.assertEqual(ctl.on_key(("u:0xffff", "k:115"), True),
                         [{"type": "set", "field": "af_gain", "value": 255}])
        self.assertEqual(ctl.volume, 100)
        # already at the top: still a full-scale write, never a bare 100
        self.assertEqual(ctl.on_key(("u:0xffff", "k:115"), True),
                         [{"type": "set", "field": "af_gain", "value": 255}])

    def test_volume_step_is_a_percentage_of_the_register(self):
        ctl = self._ctl(volume=50)
        self.assertEqual(ctl.on_key(("u:0xffff", "k:115"), True),
                         [{"type": "set", "field": "af_gain", "value": 140}])
        self.assertEqual(ctl.on_key(("u:0xc00ea", "k:114"), True),
                         [{"type": "set", "field": "af_gain", "value": 128}])

    def test_volume_bottom_is_silence(self):
        ctl = self._ctl(volume=2)
        self.assertEqual(ctl.on_key(("u:0xc00ea", "k:114"), True),
                         [{"type": "set", "field": "af_gain", "value": 0}])

    def test_cancel_drops_half_typed_frequency(self):
        ctl = self._ctl(freq_hz=7_000_000)
        ctl.on_key(("u:0x7001e",), True, now=1.0)
        ctl.on_key(("u:0xc0224",), True, now=1.1)
        self.assertEqual(ctl.on_key(("u:0xc0041",), True, now=1.2), [])
        self.assertEqual(ctl.freq_hz, 7_000_000)

    def test_holding_the_dial_key_keeps_stepping(self):
        # the field report "不太灵敏": a held key's autorepeat was thrown away,
        # so only the first step ever happened.
        ctl = self._ctl(freq_hz=7_050_000, step_index=0)
        first = ctl.on_key(("u:0x70052",), True, now=1.00)
        second = ctl.on_key(("u:0x70052",), False, now=1.10, repeat=True)
        self.assertEqual(first, [{"type": "set", "field": "freq", "value": 7_050_100}])
        self.assertEqual(second, [{"type": "set", "field": "freq", "value": 7_050_200}])

    def test_repeat_is_rate_limited_but_the_measured_rate_passes(self):
        # the field remote repeats every ~44 ms; a 50 ms floor swallowed every
        # repeat and holding the key did nothing at all
        ctl = self._ctl(step_index=0)
        ctl.on_key(("u:0x70052",), False, now=1.00, repeat=True)
        self.assertEqual(len(ctl.on_key(("u:0x70052",), False, now=1.044, repeat=True)), 1)
        self.assertEqual(ctl.on_key(("u:0x70052",), False, now=1.050, repeat=True), [])

    def test_only_continuous_actions_repeat(self):
        ctl = self._ctl()
        self.assertEqual(ctl.on_key(("u:0xc00eb",), False, now=1.0, repeat=True), [])   # 设置
        self.assertEqual(ctl.on_key(("u:0x7003e",), False, now=1.0, repeat=True), [])   # 语音
        self.assertEqual(ctl.on_key(digit("7"), False, now=1.0, repeat=True), [])        # 数字
        self.assertEqual(ctl.on_key(("u:0x7004f",), False, now=1.0, repeat=True), [])   # 步进

    def test_repeat_never_unkeys(self):
        ctl = self._ctl()
        ctl.on_key(("u:0x7003e",), True)                       # keyed
        ctl.on_key(("u:0x7003e",), False, repeat=True)         # held: nothing
        self.assertTrue(ctl.ptt_held)

    def test_unknown_key_is_inert(self):
        self.assertEqual(self._ctl().on_key(("u:0x0",), True), [])


class TestIOLayerAttributes(unittest.TestCase):
    """The I/O layer's attribute names must exist (a field test caught
    ``self.keymap`` where only ``self.controller.keymap`` did)."""

    def test_localctl_exposes_keymap(self):
        ctl = lc.Controller()
        io = lc.LocalCtl(ctl)
        self.assertIs(io.keymap, ctl.keymap)

    def test_socket_path_has_a_default(self):
        io = lc.LocalCtl(lc.Controller())
        self.assertTrue(io.socket_path.endswith(".sock"))


class TestStateAbsorption(unittest.TestCase):
    """The daemon must follow the radio, not a stale default: the operator may
    be tuning from the web at the same time.  The server sends one ``state``
    line when the control socket opens."""

    def test_state_line_moves_the_dial(self):
        ctl = lc.Controller(freq_hz=7_000_000, mode="USB")
        lc.LocalCtl(ctl)._absorb({"type": "state", "freq": 14_074_000, "mode": "cw"})
        self.assertEqual(ctl.freq_hz, 14_074_000)
        self.assertEqual(ctl.mode, "CW")

    def test_state_line_brings_the_volume_to_where_the_radio_is(self):
        # The daemon's own default (50 %) used to overwrite whatever the
        # operator had set on the radio on the first key press.
        ctl = lc.Controller(volume=50)
        io = lc.LocalCtl(ctl)
        io._absorb({"type": "state", "freq": 14_074_000, "mode": "USB",
                    "af_gain": 255})
        self.assertEqual(ctl.volume, 100)
        io._absorb({"type": "state", "af_gain": 0})
        self.assertEqual(ctl.volume, 0)
        io._absorb({"type": "state", "af_gain": 200})
        self.assertEqual(ctl.volume, 78)          # round(200 / 255 * 100)

    def test_state_line_ignores_a_junk_volume(self):
        ctl = lc.Controller(volume=60)
        io = lc.LocalCtl(ctl)
        for gain in ("x", None, True, 1.5):
            io._absorb({"type": "state", "af_gain": gain})
        self.assertEqual(ctl.volume, 60)

    def test_junk_is_ignored(self):
        ctl = lc.Controller(freq_hz=7_000_000)
        io = lc.LocalCtl(ctl)
        for msg in ({"type": "state"}, {"type": "state", "freq": "x"},
                    {"type": "other", "freq": 1}, None, []):
            io._absorb(msg) if isinstance(msg, dict) else None
        self.assertEqual(ctl.freq_hz, 7_000_000)


class TestControlSocket(unittest.TestCase):
    """End-to-end over a real Unix socket: what the box actually does."""

    def _serve(self, path, received, reply=b'{"ok":true}\n'):
        async def handler(reader, writer):
            writer.write(b'{"type":"state","freq":14070000,"mode":"USB"}\n')
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    break
                received.append(json.loads(line.decode()))
                writer.write(reply)
                await writer.drain()
            writer.close()

        async def serve():
            server = await lc.asyncio.start_unix_server(handler, path=path)
            return server
        return serve

    def test_commands_go_over_the_socket_as_json_lines(self):
        import asyncio
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ctl.sock")
            received: list = []

            async def run():
                server = await self._serve(path, received)()
                ctl = lc.Controller(freq_hz=7_000_000)
                io = lc.LocalCtl(ctl, socket_path=path)
                await io.send([{"type": "set", "field": "freq", "value": 7_051_000}])
                await io.send([{"type": "set", "field": "ptt", "value": True}])
                io._drop()
                server.close()
                await server.wait_closed()

            asyncio.run(run())
            self.assertEqual(received, [{"field": "freq", "value": 7_051_000},
                                        {"field": "ptt", "value": True}])

    def test_state_line_is_absorbed_on_connect(self):
        import asyncio
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ctl.sock")
            received: list = []

            async def run():
                server = await self._serve(path, received)()
                ctl = lc.Controller(freq_hz=7_000_000)
                io = lc.LocalCtl(ctl, socket_path=path)
                await io._connect()
                io._drop()
                server.close()
                await server.wait_closed()
                return ctl

            ctl = asyncio.run(run())
            self.assertEqual(ctl.freq_hz, 14_070_000)
            self.assertEqual(ctl.mode, "USB")

    def test_a_reply_brings_the_gain_back(self):
        # The operator can also turn the knob on the radio itself.  Every reply
        # carries the AF gain, so the daemon re-syncs instead of stepping from
        # a percentage that has since gone stale.
        import asyncio
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ctl.sock")
            received: list = []

            async def run():
                serve = self._serve(path, received, reply=b'{"ok":true,"af_gain":255}\n')
                server = await serve()
                ctl = lc.Controller(volume=10)
                io = lc.LocalCtl(ctl, socket_path=path)
                await io.send([{"type": "set", "field": "af_gain", "value": 26}])
                io._drop()
                server.close()
                await server.wait_closed()
                return ctl

            self.assertEqual(asyncio.run(run()).volume, 100)

    def test_missing_socket_is_not_fatal(self):
        import asyncio
        io = lc.LocalCtl(lc.Controller(), socket_path="/nonexistent/ctl.sock")
        asyncio.run(io.send([{"type": "set", "field": "freq", "value": 1}]))  # no raise


class TestDeviceLookup(unittest.TestCase):
    def test_finds_event_node_by_name(self):
        sample = (
            'I: Bus=0005 Vendor=0416 Product=0300 Version=0001\n'
            'N: Name="CMCC_Voice_Remote Keyboard"\n'
            'H: Handlers=sysrq kbd event3 leds \n\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "devices"
            path.write_text(sample, encoding="utf-8")
            self.assertEqual(lc.find_device("Voice_Remote", str(path)), "/dev/input/event3")
            self.assertIsNone(lc.find_device("Nothing", str(path)))


if __name__ == "__main__":
    unittest.main()
