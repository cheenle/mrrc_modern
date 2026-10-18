"""SdrHub 的纯逻辑测试：无网络、无设备、无 sleep（时间由 FakeClock 推进）。"""
import unittest

from sdr_hub import (ChannelKey, SdrCapacityError, SdrChannelFull, SdrHub,
                     LOW_TIER_THRESHOLD, SPECTRUM_FULL, SPECTRUM_LOW)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakePipe:
    def __init__(self):
        self.tuned = []
        self.set = []
        self.cleared = []

    def tune(self, hz):
        self.tuned.append(hz)

    def set_channel(self, chid, offset_hz, mode, bw_hz):
        self.set.append((chid, offset_hz, mode, bw_hz))

    def clear_channel(self, chid):
        self.cleared.append(chid)


def make(max_clients=50, max_channels=8, hold_s=30.0):
    clk = FakeClock()
    pipe = FakePipe()
    hub = SdrHub(pipe=pipe, clock=clk, hold_s=hold_s,
                 max_clients=max_clients, max_channels=max_channels)
    return hub, clk, pipe


class TuningTests(unittest.TestCase):
    def test_first_tune_wins_and_second_is_refused_with_countdown(self):
        hub, clk, pipe = make()
        a = hub.attach(1)
        b = hub.attach(2)
        ok, _ = hub.tune(a, 14_074_000)
        self.assertTrue(ok)
        self.assertEqual(pipe.tuned, [14_074_000])
        clk.advance(10)
        ok, wait = hub.tune(b, 7_074_000)
        self.assertFalse(ok)
        self.assertAlmostEqual(wait, 20.0, delta=0.1)

    def test_hold_expires(self):
        hub, clk, _ = make()
        a = hub.attach(1)
        b = hub.attach(2)
        hub.tune(a, 14_074_000)
        clk.advance(31)
        ok, _ = hub.tune(b, 7_074_000)
        self.assertTrue(ok)
        self.assertEqual(hub.center_hz, 7_074_000)

    def test_owner_can_retune_while_holding(self):
        hub, _, _ = make()
        a = hub.attach(1)
        hub.tune(a, 14_074_000)
        ok, _ = hub.tune(a, 21_074_000)
        self.assertTrue(ok)

    def test_owner_disconnect_releases_the_lock(self):
        hub, _, _ = make()
        a = hub.attach(1)
        b = hub.attach(2)
        hub.tune(a, 14_074_000)
        hub.detach(a)
        ok, _ = hub.tune(b, 7_074_000)
        self.assertTrue(ok)

    def test_rebase_keeps_absolute_frequency(self):
        hub, _, pipe = make()
        a = hub.attach(1)
        a.abs_freq = 7_074_000
        hub.subscribe_channel(a, mode=1, bw_hz=3000)
        hub.tune(a, 7_000_000)               # 中心挪了 74 kHz
        self.assertEqual(a.abs_freq, 7_074_000)
        self.assertEqual(a.key.offset_hz, 74_000)

    def test_rebase_clamps_to_the_span_edge(self):
        hub, _, _ = make()
        a = hub.attach(1)
        a.abs_freq = 7_074_000
        hub.subscribe_channel(a, mode=0, bw_hz=6000)
        hub.tune(a, 9_000_000)               # 目标被挤出窗口
        self.assertLessEqual(abs(a.abs_freq - hub.center_hz), hub._span // 2)


class PoolTests(unittest.TestCase):
    def test_identical_requests_share_one_demodulator(self):
        hub, _, pipe = make()
        a = hub.attach(1)
        b = hub.attach(2)
        for sub in (a, b):
            sub.abs_freq = hub.center_hz + 1000
        ca = hub.subscribe_channel(a, mode=1, bw_hz=3000)
        cb = hub.subscribe_channel(b, mode=1, bw_hz=3000)
        self.assertEqual(ca, cb)
        self.assertEqual(hub.channel_count, 1)
        self.assertEqual(len(pipe.set), 1)      # 只下过一次 set_channel

    def test_last_unsubscribe_tears_the_demodulator_down(self):
        hub, _, pipe = make()
        a = hub.attach(1)
        b = hub.attach(2)
        for sub in (a, b):
            sub.abs_freq = hub.center_hz
        ca = hub.subscribe_channel(a, mode=0, bw_hz=6000)
        hub.subscribe_channel(b, mode=0, bw_hz=6000)
        hub.unsubscribe_channel(a)
        self.assertEqual(hub.channel_count, 1)
        hub.unsubscribe_channel(b)
        self.assertEqual(hub.channel_count, 0)
        self.assertIn(ca, pipe.cleared)

    def test_different_bandwidth_is_a_different_demodulator(self):
        hub, _, _ = make()
        a = hub.attach(1)
        b = hub.attach(2)
        for sub in (a, b):
            sub.abs_freq = hub.center_hz
        hub.subscribe_channel(a, mode=1, bw_hz=2400)
        hub.subscribe_channel(b, mode=1, bw_hz=3000)
        self.assertEqual(hub.channel_count, 2)

    def test_channel_cap_is_refused_not_silently_ignored(self):
        hub, _, _ = make(max_channels=3)
        subs = [hub.attach(i) for i in range(4)]
        for i, sub in enumerate(subs[:3]):
            sub.abs_freq = hub.center_hz + 1000 * i
            hub.subscribe_channel(sub, mode=0, bw_hz=6000)
        subs[3].abs_freq = hub.center_hz + 5000
        with self.assertRaises(SdrChannelFull):
            hub.subscribe_channel(subs[3], mode=0, bw_hz=6000)

    def test_reassign_frees_the_old_slot(self):
        hub, _, _ = make(max_channels=1)
        a = hub.attach(1)
        a.abs_freq = hub.center_hz
        first = hub.subscribe_channel(a, mode=0, bw_hz=6000)
        a.abs_freq = hub.center_hz + 2000
        second = hub.subscribe_channel(a, mode=0, bw_hz=6000)
        self.assertEqual(first, second)       # 同一个槽位被复用
        self.assertEqual(hub.channel_count, 1)


class CapacityTests(unittest.TestCase):
    def test_client_cap(self):
        hub, _, _ = make(max_clients=3)
        for i in range(3):
            hub.attach(i)
        with self.assertRaises(SdrCapacityError):
            hub.attach(99)

    def test_detach_frees_a_seat(self):
        hub, _, _ = make(max_clients=2)
        a = hub.attach(1)
        hub.attach(2)
        hub.detach(a)
        hub.attach(3)                          # 不抛

    def test_spectrum_tier_drops_when_crowded(self):
        hub, _, _ = make(max_clients=100)
        subs = [hub.attach(i) for i in range(LOW_TIER_THRESHOLD)]
        self.assertEqual(hub.snapshot()["spectrum_bins"], SPECTRUM_FULL)
        extra = hub.attach(LOW_TIER_THRESHOLD)
        self.assertEqual(hub.snapshot()["spectrum_bins"], SPECTRUM_LOW)
        hub.detach(extra) if extra else None
        self.assertEqual(hub.snapshot()["spectrum_bins"], SPECTRUM_FULL)


class FanoutTests(unittest.TestCase):
    def test_spectrum_reaches_every_subscriber(self):
        hub, _, _ = make()
        got = []
        for i in range(3):
            sub = hub.attach(i)
            sub.send = lambda frame, got=got: got.append(frame)
        hub.on_spectrum(1, b"\x01\x02")
        self.assertEqual(len(got), 3)

    def test_audio_only_reaches_owners_of_that_channel(self):
        hub, _, _ = make()
        a = hub.attach(1)
        b = hub.attach(2)
        a.abs_freq = hub.center_hz
        b.abs_freq = hub.center_hz + 50_000
        got_a, got_b = [], []
        a.send = lambda f, g=got_a: g.append(f)
        b.send = lambda f, g=got_b: g.append(f)
        ca = hub.subscribe_channel(a, mode=0, bw_hz=6000)
        hub.subscribe_channel(b, mode=0, bw_hz=6000)
        hub.on_audio(ca, 1, 0.5, b"pcm")
        self.assertEqual(len(got_a), 1)
        self.assertEqual(len(got_b), 0)

    def test_squelch_silences_quiet_channels(self):
        hub, _, _ = make()
        a = hub.attach(1)
        a.abs_freq = hub.center_hz
        got = []
        a.send = lambda f, g=got: g.append(f)
        ca = hub.subscribe_channel(a, mode=0, bw_hz=6000)
        hub.set_squelch(a, 0.5)
        hub.on_audio(ca, 1, 0.2, b"quiet")
        self.assertEqual(got, [])
        hub.on_audio(ca, 2, 0.8, b"loud")
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0][3], 0.8, delta=0.01)

    def test_state_broadcast_carries_the_snapshot(self):
        hub, _, _ = make()
        a = hub.attach(1)
        got = []
        a.send = lambda f, g=got: g.append(f)
        hub.broadcast_state()
        self.assertEqual(got[0][0], "state")
        self.assertIn("center_hz", got[0][1])


if __name__ == "__main__":
    unittest.main()


class CursorTests(unittest.TestCase):
    def test_cursor_is_stored_as_an_absolute_frequency(self):
        hub, _, _ = make()
        a = hub.attach(1)
        hub.set_cursor(a, +12_000)
        self.assertEqual(a.abs_freq, hub.center_hz + 12_000)
        hub.tune(a, hub.center_hz + 100_000)
        self.assertEqual(a.abs_freq, 7_074_000 + 12_000)   # 中心变了，绝对频率不变

    def test_cursor_is_clamped_to_the_span(self):
        hub, _, _ = make()
        a = hub.attach(1)
        hub.set_cursor(a, 9_000_000)
        self.assertEqual(abs(a.abs_freq - hub.center_hz), hub._span // 2 - 1000)

    def test_state_frame_is_per_subscriber(self):
        hub, _, _ = make()
        a = hub.attach(1)
        b = hub.attach(2)
        got_a, got_b = [], []
        a.send = lambda f, g=got_a: g.append(f)
        b.send = lambda f, g=got_b: g.append(f)
        hub.set_cursor(a, 1000)
        hub.set_cursor(b, -1000)
        hub.broadcast_state()
        self.assertEqual(got_a[-1][1]["abs_freq"], hub.center_hz + 1000)
        self.assertEqual(got_b[-1][1]["abs_freq"], hub.center_hz - 1000)
        self.assertEqual(got_a[-1][1]["center_hz"], got_b[-1][1]["center_hz"])
