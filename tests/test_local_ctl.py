"""Unit tests for local_ctl (SDD AD-028): remote key → radio command.

Everything here is the pure layer — no evdev node, no WebSocket.  The wire
format is pinned because it is the only part that cannot be re-derived from
the source: 24-byte ``input_event`` structs on 64-bit Linux.
"""

import json
import os
import struct
import tempfile
import unittest
from pathlib import Path

import local_ctl as lc

#: 控制 socket 用 AF_UNIX（Unix 域套接字）+ 路径权限，两者都只存在于 POSIX。
_POSIX = os.name == "posix"


def _ev(ev_type: int, code: int, value: int) -> bytes:
    return lc.INPUT_EVENT.pack(0, 0, ev_type, code, value)


def digit(d: str) -> tuple:
    """Usage id for a digit key on the measured remote (HID 0x1E..0x27)."""
    usage = 0x70027 if d == "0" else 0x7001E + (int(d) - 1)
    return (f"u:0x{usage:x}",)


def tap(ctl, ids, now: float = 0.0):
    """One real key press: down, then up.

    The Xiaomi remote's PRESS repeat only exists while the key is **down**
    (value 1, every ~250 ms).  "Press the same key three times" therefore has
    to be modelled as three PRESS/RELEASE pairs — three PRESSes with no
    release in between is the firmware's own repeat, which must be ignored
    (see ``test_a_held_band_key_switches_one_band_not_the_whole_table``).
    """
    messages = ctl.on_key(ids, True, now=now)
    ctl.on_key(ids, False, now=now + 0.01)
    return messages


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

    def test_xiaomi_voice_remote_usages_are_mapped(self):
        # measured 2026-11-10 on a Xiaomi Bluetooth Remote 2 (ATVV voice
        # remote): OK/Back/Home are keyboard-page usages, not the consumer
        # codes the CMCC remote sends.
        km = lc.KeyMap()
        self.assertEqual(km.action_for(("u:0x70028", "k:28")), "enter")
        self.assertEqual(km.action_for(("u:0x700f1", "k:158")), "cancel")
        self.assertEqual(km.action_for(("u:0x7004a", "k:102")), "home")
        self.assertEqual(km.action_for(("u:0x70080", "k:115")), "vol_up")
        self.assertEqual(km.action_for(("u:0x70081", "k:114")), "vol_down")

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

    def test_channel_keys_walk_the_fixed_presets(self):
        # 频道 +/− 的落点是固定的（现场约定 2026-10-18）：遥控器没有显示屏，
        # 同一个波段里“按相对位置跳”会落在哪儿取决于从哪儿来。
        self.assertEqual(lc.next_channel(3_850_000, True), (7_050_000, "LSB"))
        self.assertEqual(lc.next_channel(14_270_000, True), (21_240_000, "USB"))
        self.assertEqual(lc.next_channel(29_600_000, True), (3_850_000, "LSB"))   # 到底回绕
        self.assertEqual(lc.next_channel(3_850_000, False), (29_600_000, "FM"))

    def test_channel_keys_start_from_the_nearest_preset(self):
        # 当前不在任何一个信道频点上时，先归到最近的，再走一步
        self.assertEqual(lc.next_channel(7_100_000, True), (10_100_000, "USB"))
        self.assertEqual(lc.next_channel(14_074_000, False), (10_100_000, "USB"))
        self.assertEqual(lc.next_channel(50_500_000, True), (3_850_000, "LSB"))

    def test_channel_modes_follow_the_band_plan(self):
        # 10 MHz 以下 LSB、以上 USB，29.600 用 FM
        expected = {
            3_850_000: "LSB", 7_050_000: "LSB", 10_100_000: "USB",
            14_270_000: "USB", 21_240_000: "USB", 28_500_000: "USB",
            29_600_000: "FM",
        }
        self.assertEqual({hz: mode for hz, mode in lc.PRESET_CHANNELS}, expected)

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

    def test_enter_without_digits_goes_to_7050_lsb(self):
        """确定键没有待提交的数字时，一步回到 40 m 常用频点（2026-11-14 现场要求）。

        频率和模式必须**一条**命令下发（`qsy`）：分成两次 set 时，中间那段半套
        状态会被轮询抓走，模式被波段栈改回旧的。
        """
        ctl = self._ctl(freq_hz=14_270_000, mode="USB")
        self.assertEqual(
            ctl.on_key(("u:0x70028",), True),
            [{"type": "set", "field": "qsy",
              "value": {"freq": 7_050_000, "mode": "LSB"}}])
        self.assertEqual((ctl.freq_hz, ctl.mode), (7_050_000, "LSB"))

    def test_menu_toggles_vfo_and_channels(self):
        """菜单键切「VFO / 频道」；频道模式下 ▲▼ 直接换保存的频道。

        麦克风遥控器没有屏幕，所以「现在在哪个模式」只能由下一次 ▲▼ 的行为体现；
        切换本身不改频率 —— 按错了不会丢掉频点。
        """
        ctl = self._ctl(freq_hz=7_050_000, mode="LSB")
        ctl.channels = ((0, 7_050_000, "LSB"), (2, 3_850_000, "LSB"),
                        (3, 14_270_000, "USB"))
        self.assertEqual(tap(ctl, ("k:127",)), [])
        self.assertTrue(ctl.channel_mode)
        self.assertEqual(tap(ctl, ("u:0x70052",)),
                         [{"type": "set", "field": "mem", "value": 2}])
        self.assertEqual((ctl.freq_hz, ctl.mode), (3_850_000, "LSB"))
        self.assertEqual(tap(ctl, ("u:0x70052",)),
                         [{"type": "set", "field": "mem", "value": 3}])
        # 到底回绕
        self.assertEqual(tap(ctl, ("u:0x70051",)),
                         [{"type": "set", "field": "mem", "value": 2}])
        # 再按一次菜单回到 VFO：▲ 又只是步进
        tap(ctl, ("k:127",))
        self.assertFalse(ctl.channel_mode)
        self.assertEqual(tap(ctl, ("u:0x70052",)),
                         [{"type": "set", "field": "freq", "value": 3_851_000}])

    def test_channels_are_vfo_when_no_channel_is_saved(self):
        ctl = self._ctl(freq_hz=7_050_000, mode="LSB")
        ctl.on_key(("k:127",), True)
        self.assertEqual(ctl.on_key(("u:0x70052",), True),
                         [{"type": "set", "field": "freq", "value": 7_051_000}])

    def test_home_starts_a_cq_and_a_second_press_aborts_it(self):
        """主页键 = 一键 CQ；呼叫窗口内再按一次是中止（遥控器没有屏幕）。"""
        ctl = self._ctl()
        self.assertEqual(ctl.on_key(("u:0x7004a",), True, now=100.0),
                         [{"type": "set", "field": "cq", "value": True}])
        self.assertEqual(ctl.on_key(("u:0x7004a",), True, now=103.0),
                         [{"type": "set", "field": "cq", "value": False}])
        self.assertEqual(ctl.on_key(("u:0x7004a",), True,
                                    now=103.0 + lc.CQ_ABORT_WINDOW_S + 1),
                         [{"type": "set", "field": "cq", "value": True}])

    def test_band_key_cycles_the_radios_own_band_table(self):
        """TV 键换波段：用服务端下发的 `backend.bands`，不是写死的一张表。

        FT-891 只有 HF+6 m、IC-705 还有 VHF/UHF —— 同一份代码在两台地方必须走
        两张表，所以波段表来自电台自己。
        """
        ctl = self._ctl(freq_hz=7_050_000,
                        bands=(("160m", 1_900_000), ("80m", 3_800_000),
                               ("40m", 7_050_000)))
        self.assertEqual(ctl.on_key(("u:0x70035",), True),
                         [{"type": "set", "field": "band", "value": "160m"}])
        self.assertEqual(ctl.freq_hz, 1_900_000)
        # A real press is PRESS → RELEASE → PRESS.  The release is what says
        # "the operator lifted the key"; without it the second PRESS is the
        # remote's own repeat (see the held-key tests below).
        ctl.on_key(("u:0x70035",), False)
        self.assertEqual(ctl.on_key(("u:0x70035",), True),
                         [{"type": "set", "field": "band", "value": "80m"}])

    def test_a_held_band_key_switches_one_band_not_the_whole_table(self):
        """2026-11-14 现场（盒子 217，小米遥控器 2）：按住电视键换掉了整张波段表。

        实测日志（12:57:04–12:57:07）：12 条 `key u:0x70035 → band_next`，服务端
        12 条 `Band change applied … OK`，电台从 10.14 MHz 一路跳到 1.8455 MHz。
        每条的 msgs 都非空 ⇒ 那不是内核 autorepeat（value=2 会在 REPEATABLE 检查
        处返回空表），而是**固件自己以 PRESS(value=1) 连发**，约 250 ms 一次。

        边沿动作（换波段、QSY、频道回放、模式循环…）必须“按一下执行一次”，
        ==“按住多少次就执行多少次”。
        """
        ctl = self._ctl(freq_hz=7_050_000,
                        bands=(("40m", 7_050_000), ("20m", 14_270_000),
                               ("15m", 21_400_000)))
        self.assertEqual(ctl.on_key(("u:0x70035",), True, now=100.0),
                         [{"type": "set", "field": "band", "value": "20m"}])
        # The firmware repeats PRESS every ~250 ms while the key is down.
        self.assertEqual(ctl.on_key(("u:0x70035",), True, now=100.25), [])
        self.assertEqual(ctl.on_key(("u:0x70035",), True, now=100.50), [])
        self.assertEqual(ctl.freq_hz, 14_270_000)

    def test_the_next_press_after_a_release_is_a_new_press(self):
        """去抖不能把键锁死：松手再按必须照样生效。

        人手连按（100–200 ms 一下）也是这个形状 —— 所以去抖只能看“有没有
        收到 RELEASE”，不能用一个时间窗把人手的快速连按吞掉。
        """
        ctl = self._ctl(freq_hz=7_050_000,
                        bands=(("40m", 7_050_000), ("20m", 14_270_000),
                               ("15m", 21_400_000)))
        self.assertEqual(len(ctl.on_key(("u:0x70035",), True, now=100.0)), 1)
        ctl.on_key(("u:0x70035",), False, now=100.05)
        self.assertEqual(len(ctl.on_key(("u:0x70035",), True, now=100.10)), 1)

    def test_a_remote_that_never_sends_a_release_does_not_lock_the_key(self):
        """只发 PRESS 不发的固件不能把键永久锁死。

        两半都要成立：按住时的连发被挡掉，而超过 `HELD_STALE_S` 之后同一个
        动作仍能再次生效（当作上一次的 RELEASE 丢了）。
        """
        ctl = self._ctl(freq_hz=7_050_000,
                        bands=(("40m", 7_050_000), ("20m", 14_270_000),
                               ("15m", 21_400_000)))
        self.assertEqual(len(ctl.on_key(("u:0x70035",), True, now=100.0)), 1)
        self.assertEqual(ctl.on_key(("u:0x70035",), True, now=100.25), [])
        self.assertEqual(
            len(ctl.on_key(("u:0x70035",), True, now=100.0 + lc.HELD_STALE_S + 0.1)), 1)

    def test_a_held_digit_key_types_the_digit_once(self):
        """数字键同样不能因为固件连发把同一个数字按两遍。"""
        ctl = self._ctl()
        ctl.on_key(digit("7"), True, now=100.0)
        ctl.on_key(digit("7"), True, now=100.25)
        ctl.on_key(digit("0"), True, now=100.50)
        self.assertEqual(ctl.numeric.pending, "70")

    def test_holding_the_dial_key_still_steps_continuously(self):
        """保护性用例：方向键是**连续**动作，按住必须继续走（ADR-028）。

        去抖只适用于“按一下执行一次”的动作；把这一条也管住就等于把长按调频
        功能删掉了。
        """
        ctl = self._ctl(freq_hz=7_050_000)
        first = ctl.on_key(("u:0x70052",), True, now=200.0)
        repeat = ctl.on_key(("u:0x70052",), True, now=200.05, repeat=True)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(repeat), 1)

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

    def test_a_link_drop_does_not_swallow_the_next_ptt_press(self):
        """掉线时 `release_ptt()` 清掉按下记忆，重连后第一下 PTT 立刻生效。"""
        ctl = self._ctl()
        ctl.on_key(("u:0x7003e",), True, now=100.0)
        ctl.release_ptt()
        self.assertEqual(ctl.on_key(("u:0x7003e",), True, now=100.05),
                         [{"type": "set", "field": "ptt", "value": True}])

    def test_freq_keys_step_by_current_step(self):
        ctl = self._ctl(freq_hz=7_050_000, step_index=1)
        self.assertEqual(ctl.on_key(("u:0x70052",), True),
                         [{"type": "set", "field": "freq", "value": 7_051_000}])
        self.assertEqual(ctl.on_key(("u:0x70051",), True),
                         [{"type": "set", "field": "freq", "value": 7_050_000}])

    def test_step_keys_change_ladder(self):
        ctl = self._ctl(step_index=0)
        tap(ctl, ("u:0x7004f",))                # ▶ coarser
        self.assertEqual(ctl.step_hz, 1000)
        for _ in range(3):
            tap(ctl, ("u:0x7004f",))
        self.assertEqual(ctl.step_hz, lc.STEP_LADDER_HZ[-1])
        tap(ctl, ("u:0x70050",))                # ◀ finer
        self.assertEqual(ctl.step_hz, lc.STEP_LADDER_HZ[-2])

    def test_channel_key_sets_frequency_and_mode(self):
        ctl = self._ctl(freq_hz=7_050_000, mode="USB")
        self.assertEqual(ctl.on_key(("u:0x7004b",), True),
                         [{"type": "set", "field": "freq", "value": 10_100_000},
                          {"type": "set", "field": "mode", "value": "USB"}])
        self.assertEqual(ctl.freq_hz, 10_100_000)
        self.assertEqual(ctl.mode, "USB")

    def test_channel_key_brings_its_own_mode(self):
        ctl = self._ctl(freq_hz=28_500_000, mode="USB")
        self.assertEqual(ctl.on_key(("u:0x7004b",), True)[-1],
                         {"type": "set", "field": "mode", "value": "FM"})
        self.assertEqual(ctl.mode, "FM")
        ctl = self._ctl(freq_hz=3_850_000, mode="USB")
        self.assertEqual(ctl.on_key(("u:0x7004b",), True)[-1],
                         {"type": "set", "field": "mode", "value": "LSB"})

    def test_channel_key_clears_a_half_typed_frequency(self):
        ctl = self._ctl(freq_hz=7_050_000)
        ctl.on_key(("u:0x7001e",), True, now=1.0)          # 1
        ctl.on_key(("u:0x7004b",), True, now=1.1)          # 频道 +
        self.assertFalse(ctl.numeric.pending)

    def test_mode_key_cycles_available_modes(self):
        ctl = self._ctl(mode="LSB", modes=("LSB", "USB", "FT8"))
        self.assertEqual(tap(ctl, ("u:0xc00eb",)),
                         [{"type": "set", "field": "mode", "value": "USB"}])
        self.assertEqual(tap(ctl, ("u:0xc00eb",)),
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

    def test_volume_keys_move_the_rx_level_when_the_radio_has_no_af_gain(self):
        # SunSDR2DX reports has_af_gain=False; sending af_gain is refused by the
        # server, so the same key steps the box RX level (the web UI's 🔊 Vol).
        ctl = self._ctl(af_gain_supported=False)
        self.assertEqual(ctl.on_key(("u:0xffff", "k:115"), True),
                         [{"type": "set", "field": "rx_db", "value": 3.0}])
        self.assertEqual(ctl.on_key(("u:0xffff", "k:114"), True),
                         [{"type": "set", "field": "rx_db", "value": 0.0}])

    def test_rx_level_steps_by_three_db_and_clamps_at_both_ends(self):
        ctl = self._ctl(af_gain_supported=False)
        for _ in range(20):
            ctl.on_key(("u:0xffff", "k:115"), True)
        self.assertEqual(ctl.rx_db, lc.RX_DB_LIMITS[1])
        self.assertEqual(ctl.on_key(("u:0xffff", "k:115"), True),
                         [{"type": "set", "field": "rx_db", "value": 30.0}])
        for _ in range(30):
            last = ctl.on_key(("u:0xffff", "k:114"), True)
        self.assertEqual(last,
                         [{"type": "set", "field": "rx_db", "value": -20.0}])

    def test_level_actions_are_repeatable(self):
        ctl = self._ctl(af_gain_supported=False)
        self.assertEqual(ctl.on_key(("u:0xffff", "k:115"), False, repeat=True),
                         [{"type": "set", "field": "rx_db", "value": 3.0}])

    def test_cancel_drops_half_typed_frequency(self):
        ctl = self._ctl(freq_hz=14_000_000)
        ctl.on_key(("u:0x7001e",), True, now=1.0)
        ctl.on_key(("u:0xc0224",), True, now=1.1)
        # 返回之后那半个数字不算数：确定键走「回 7.050 LSB」，不会提交成 1 kHz。
        self.assertEqual(ctl.on_key(("u:0xc0041",), True, now=1.2),
                         [{"type": "set", "field": "qsy",
                           "value": {"freq": 7_050_000, "mode": "LSB"}}])
        self.assertEqual(ctl.freq_hz, 7_050_000)

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

    def test_state_line_carries_the_af_gain_capability_and_rx_level(self):
        """The volume key's meaning is the backend's answer, not a model name."""
        ctl = lc.Controller(volume=50)
        io = lc.LocalCtl(ctl)
        io._absorb({"type": "state", "freq": 7_074_000, "mode": "USB",
                    "af_gain_supported": False, "rx_db": -6.0})
        self.assertFalse(ctl.af_gain_supported)
        self.assertEqual(ctl.rx_db, -6.0)
        self.assertEqual(ctl.on_key(("u:0xffff", "k:115"), True),
                         [{"type": "set", "field": "rx_db", "value": -3.0}])
        # A reply from a Yaesu/Icom (or a reconnect) flips it back to af_gain.
        io._absorb({"ok": True, "af_gain_supported": True, "rx_db": -3.0})
        self.assertEqual(ctl.on_key(("u:0xffff", "k:115"), True),
                         [{"type": "set", "field": "af_gain", "value": 140}])

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


@unittest.skipUnless(_POSIX, "控制 socket 是 AF_UNIX + 路径权限，Windows 上没有")
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

    #: A box with a USB audio codec (Bus=0003, phantom keyboard), the IR receiver
    #: and one arbitrary BLE HID remote — the shape that matters for "can the
    #: local control surface read a remote nobody configured by name".
    MIXED = (
        'I: Bus=0003 Vendor=0d8c Product=0013 Version=0100\n'
        'N: Name="C-Media Electronics Inc. USB Audio Device"\n'
        'B: EV=1b\n'
        'H: Handlers=kbd event1 \n\n'
        'I: Bus=0019 Vendor=0000 Product=0000 Version=0000\n'
        'N: Name="meson-ir"\n'
        'B: EV=100003\n'
        'H: Handlers=kbd event2 \n\n'
        'I: Bus=0005 Vendor=2717 Product=32b8 Version=0010\n'
        'N: Name="Xiaomi Bluetooth Remote 2 Keyboard"\n'
        'B: EV=100013\n'
        'H: Handlers=sysrq kbd event5 leds \n\n'
    )

    def _write(self, tmp, text):
        path = Path(tmp) / "devices"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_a_bluetooth_device_with_keys_is_found_without_a_name_match(self):
        """遥控器叫什么名字由厂商决定 —— 认“蓝牙 + 有按键”比认名字可靠。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, self.MIXED)
            self.assertEqual(lc.find_device("Voice_Remote", path), "/dev/input/event5")
            self.assertEqual(lc.find_device("xiaomi", path), "/dev/input/event5")

    def test_the_configured_name_still_wins_over_the_fallback(self):
        both = self.MIXED + (
            'I: Bus=0005 Vendor=0416 Product=0300 Version=0001\n'
            'N: Name="CMCC_Voice_Remote Keyboard"\n'
            'B: EV=100013\n'
            'H: Handlers=sysrq kbd event7 leds \n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(lc.find_device("Voice_Remote", self._write(tmp, both)),
                             "/dev/input/event7")

    def test_a_bluetooth_device_without_key_events_is_not_a_remote(self):
        """蓝牙音箱、心率带之类也在 Bus=0005 上 —— 没有 EV_KEY 就不是按键设备。"""
        speaker = (
            'I: Bus=0005 Vendor=0001 Product=0002 Version=0001\n'
            'N: Name="Speaker"\n'
            'B: EV=100001\n'
            'H: Handlers=event9 \n\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, speaker)
            self.assertIsNone(lc.find_device("Voice_Remote", path))

    def test_a_usb_keyboard_is_not_picked_as_the_remote(self):
        """USB 那一路（Bus=0003）不进回落 —— 盒子上就是那颗录音声卡的 kbd 节点。"""
        usb = self.MIXED.split('I: Bus=0019')[0]
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, usb)
            self.assertIsNone(lc.find_device("Voice_Remote", path))


if __name__ == "__main__":
    unittest.main()
