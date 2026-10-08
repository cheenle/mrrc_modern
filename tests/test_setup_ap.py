"""The resident supervisor: when the open window exists, and when it must not.

Three decisions here fail silently, so each is asserted from several directions:

* our own hotspot reports `connected` in `nmcli device status`, so a supervisor
  that trusts that column raises the AP and tears it down again every tick;
* the timeout latch lives for the *process*, not on disk. Persisting it would
  turn "you missed the 30-minute window" into "the box is unreachable until you
  reflash it" — the design's recovery path is a power cycle, and a power cycle
  only helps if the latch dies with the process;
* a supervisor restarted mid-window finds an AP it did not raise and has no
  deadline for it, so without adopting one the open network never closes.

`FakeRadio` models the fact every decision turns on — whether an AP is up — and
flips it when the supervisor asks nmcli to raise or lower one, so a supervisor
that does them in the wrong order is caught rather than tolerated.

`Harness`'s fake sleep **advances the fake clock**: a sleep that does not is how
a settle-window test turns into a hang.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import net_wifi
from linux import setup_ap
from net_wifi import NmResult


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRadio:
    """A fake nmcli that tracks whether the AP is up and answers accordingly."""

    def __init__(self, wired: bool = False, sta: bool = False,
                 ap_up: bool = False, hotspot_fails: bool = False,
                 connecting: bool = False):
        self.wired = wired
        self.sta = sta
        self.ap_up = ap_up
        self.hotspot_fails = hotspot_fails
        self.connecting = connecting
        self.calls: list = []
        self.raise_count = 0
        self.down_count = 0

    def __call__(self, argv):
        joined = " ".join(argv)
        self.calls.append(joined)

        if "-f RUNNING general" in joined:
            return NmResult(0, "running\n")

        if "-f NAME,TYPE connection show --active" in joined:
            rows = []
            if self.ap_up:
                rows.append("Hotspot:802-11-wireless")
            if self.sta:
                rows.append("Home:802-11-wireless")
            return NmResult(0, "".join(row + "\n" for row in rows))

        if "-f UUID connection show --active" in joined:
            return NmResult(0, "")

        if "802-11-wireless.mode" in joined:
            is_ap = self.ap_up and argv[-1] == "Hotspot"
            return NmResult(0, ("ap" if is_ap else "infrastructure") + "\n")

        if "-f UUID,NAME connection show" in joined:
            return NmResult(0, "")

        if "-f DEVICE,TYPE,STATE,CONNECTION device status" in joined:
            eth = "connected" if self.wired else "unavailable"
            if self.connecting:
                wifi = "ip-config:"
            elif self.ap_up:
                wifi = "connected:Hotspot"
            elif self.sta:
                wifi = "connected:Home"
            else:
                wifi = "disconnected:"
            return NmResult(0, f"eth0:ethernet:{eth}:\nwlan0:wifi:{wifi}\n")

        if "device wifi hotspot" in joined:
            if self.hotspot_fails:
                return NmResult(1, "", "Error: Device not suitable for hotspot mode")
            self.ap_up = True
            self.raise_count += 1
            return NmResult(0, "Successfully activated a Hotspot network\n", "")

        if "connection down" in joined:
            self.ap_up = False
            self.down_count += 1
            return NmResult(0, "Connection successfully deactivated\n", "")

        if "-g IP4.ADDRESS device show" in joined:
            if self.ap_up:
                return NmResult(0, "10.42.0.1/24\n")
            if self.wired or self.sta:
                return NmResult(0, "192.168.1.77/24\n")
            return NmResult(0, "")

        return NmResult(0, "", "")

    def count(self, key: str) -> int:
        return sum(1 for call in self.calls if key in call)


class CapturingLog:
    """Collects (level, rendered message) so log *volume* can be asserted."""

    def __init__(self):
        self.records: list = []

    def _record(self, level, fmt, *args):
        self.records.append((level, fmt % args if args else str(fmt)))

    def info(self, fmt, *args):
        self._record("info", fmt, *args)

    def warning(self, fmt, *args):
        self._record("warning", fmt, *args)

    def error(self, fmt, *args):
        self._record("error", fmt, *args)

    def exception(self, fmt, *args):
        self._record("error", fmt, *args)

    def messages(self, level=None):
        return [text for lvl, text in self.records if level is None or lvl == level]


class Harness:
    """One supervisor, wired to a fake radio, a fake clock and a temp dir."""

    def __init__(self, radio=None, timeout_min=30, env=None, clock=None,
                 stop_after=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        self.radio = radio if radio is not None else FakeRadio()
        self.clock = clock or FakeClock()
        self.sleeps: list = []
        self.log = CapturingLog()
        self._stop_after = stop_after
        base = {"MRRC_SETUP_AP_STATE_DIR": str(self.state_dir),
                "MRRC_SETUP_AP_TIMEOUT_MIN": str(timeout_min)}
        base.update(env or {})
        self.sup = setup_ap.Supervisor(
            setup_ap.settings(base), runner=self.radio, clock=self.clock,
            sleep=self._sleep, log=self.log)

    def _sleep(self, seconds: float) -> None:
        """Record it, advance the clock, and stop the loop when asked.

        Advancing the clock is not a convenience: `_wait_for_nm` loops on
        `clock() < deadline`, so a sleep that does not move time never returns.
        """
        self.sleeps.append(seconds)
        self.clock.advance(seconds)
        if self._stop_after is not None and len(self.sleeps) >= self._stop_after:
            self.sup.stop()

    def cleanup(self):
        self.tmp.cleanup()

    def state(self) -> net_wifi.ApState:
        return net_wifi.read_state(self.state_dir)

    def write_claim(self, **fields):
        fields.setdefault("action", "connect")
        fields.setdefault("heartbeat", self.clock())
        net_wifi.write_wizard(net_wifi.WizardClaim(**fields), self.state_dir)

    def clear_claim(self):
        """What a finished switch does: the mailbox entry goes away."""
        try:
            (self.state_dir / net_wifi.WIZARD_NAME).unlink()
        except OSError:
            pass


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.cleanup)

    def test_no_network_at_all_opens_the_window(self):
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.radio.raise_count, 1)
        state = self.h.state()
        self.assertEqual(state.mode, net_wifi.MODE_HOTSPOT)
        self.assertEqual(state.ssid, net_wifi.DEFAULT_SSID)
        self.assertEqual(state.gateway, "10.42.0.1/24")
        self.assertEqual(state.network, "10.42.0.0/24")
        self.assertEqual(state.url, "https://10.42.0.1:8888/setup")
        self.assertEqual(state.deadline, self.h.clock() + 30 * 60)
        self.assertEqual(state.heartbeat, self.h.clock())

    def test_the_published_state_is_what_opens_the_gate(self):
        """Tasks 3 and 4 joined: the file the supervisor writes must be the file
        the server's gate believes. If these ever diverge the wizard is
        unreachable with both halves looking correct."""
        self.h.sup.tick()
        now = self.h.clock()
        self.assertTrue(net_wifi.gate_is_open(self.h.state(), "10.42.0.57", now))
        self.assertFalse(net_wifi.gate_is_open(self.h.state(), "192.168.1.50", now))

    def test_a_cable_means_no_hotspot(self):
        self.h.radio.wired = True
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.radio.raise_count, 0)
        self.assertEqual(self.h.state().reason, "uplink")

    def test_a_saved_wifi_that_connected_means_no_hotspot(self):
        self.h.radio.sta = True
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.radio.raise_count, 0)

    def test_a_cable_with_no_dhcp_still_gets_a_hotspot(self):
        """`connected (site only)`: link-local, no gateway. Reachable by nobody,
        which is exactly what the hotspot is for."""
        original = self.h.radio.__call__

        def site_only(argv):
            joined = " ".join(argv)
            if "-f DEVICE,TYPE,STATE,CONNECTION device status" in joined:
                return NmResult(0, "eth0:ethernet:connected (site only):\n"
                                   "wlan0:wifi:disconnected:\n")
            return original(argv)

        self.h.sup.runner = site_only
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)

    def test_its_own_hotspot_is_not_an_uplink(self):
        """The infinite self-teardown bug (constraint 12).

        The AP is up and nothing else is connected: the supervisor must conclude
        "still no uplink" and leave it alone. Counting the AP as an uplink would
        take it down; failing to recognise it as up would re-raise it.
        """
        self.h.radio.ap_up = True
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.radio.raise_count, 0, "must not re-raise a live AP")
        self.assertEqual(self.h.radio.down_count, 0, "must not tear down its own AP")

    def test_plugging_a_cable_in_while_the_hotspot_is_up_takes_it_down(self):
        """Design R1's second half, and D-6's fence 1."""
        self.h.sup.tick()
        self.assertTrue(self.h.radio.ap_up)
        self.h.radio.wired = True
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.radio.down_count, 1)
        self.assertFalse(self.h.radio.ap_up)

    def test_a_saved_wifi_associating_also_takes_it_down(self):
        self.h.sup.tick()
        self.h.radio.sta = True
        self.h.radio.ap_up = False       # NM dropped the AP in order to associate
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.state().reason, "uplink")

    def test_waiting_for_networkmanager_is_reported_not_guessed(self):
        original = self.h.radio.__call__

        def no_nm(argv):
            if "-f RUNNING general" in " ".join(argv):
                return NmResult(0, "starting\n")
            return original(argv)

        self.h.sup.runner = no_nm
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.state().reason, "waiting for NetworkManager")
        self.assertEqual(self.h.radio.raise_count, 0,
                         "raising an AP before NM is up just fails")


class TimeoutTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(timeout_min=1)        # 60 s, so the test stays short
        self.addCleanup(self.h.cleanup)

    def test_the_window_closes_on_its_own(self):
        self.h.sup.tick()
        self.assertTrue(self.h.radio.ap_up)
        self.h.clock.advance(61)
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.radio.down_count, 1)
        self.assertEqual(self.h.state().reason, "timeout")
        self.assertTrue(any("power-cycle" in m for m in self.h.log.messages("warning")),
                        "the operator has to be told how to get back in")

    def test_the_window_stays_open_before_the_deadline(self):
        self.h.sup.tick()
        self.h.clock.advance(59)
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.radio.down_count, 0)

    def test_a_closed_window_does_not_reopen_in_the_same_boot(self):
        """D-6 fence 2. Without the latch the supervisor would re-raise it five
        seconds later and the timeout would be decoration."""
        self.h.sup.tick()
        self.h.clock.advance(61)
        self.h.sup.tick()
        self.assertEqual(self.h.radio.raise_count, 1)
        for _ in range(5):
            self.h.clock.advance(5)
            self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.radio.raise_count, 1, "the latch must hold")
        self.assertEqual(self.h.state().reason, "timeout")

    def test_a_power_cycle_reopens_it(self):
        """The documented recovery path: the latch is per-process, so a reboot
        gives the operator another window. This is why it must never be
        persisted — a persisted latch bricks the onboarding."""
        self.h.sup.tick()
        self.h.clock.advance(61)
        self.h.sup.tick()
        self.assertEqual(self.h.radio.raise_count, 1)

        self.h.radio.ap_up = False
        rebooted = Harness(radio=self.h.radio, timeout_min=1, clock=self.h.clock)
        self.addCleanup(rebooted.cleanup)
        self.assertEqual(rebooted.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.radio.raise_count, 2)

    def test_an_uplink_clears_the_latch(self):
        """A box that got a network and then lost it again is a fresh situation,
        not a continuation of the missed window."""
        self.h.sup.tick()
        self.h.clock.advance(61)
        self.h.sup.tick()
        self.h.radio.wired = True
        self.h.sup.tick()
        self.h.radio.wired = False
        self.h.radio.ap_up = False
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)

    def test_a_supervisor_restarted_mid_window_arms_a_deadline(self):
        """It finds an AP it did not raise. Without adopting a deadline the
        timeout can never fire, and an open network stays up forever."""
        self.h.radio.ap_up = True
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.radio.raise_count, 0)
        self.assertEqual(self.h.state().deadline, self.h.clock() + 60)
        self.h.clock.advance(61)
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.radio.down_count, 1)


class WizardHandoffTests(unittest.TestCase):
    """Design D-3: one radio, so the supervisor must stand down while the server
    performs the AP→STA switch."""

    def setUp(self):
        self.h = Harness(timeout_min=1)
        self.addCleanup(self.h.cleanup)

    def test_it_stands_down_during_a_switch(self):
        self.h.sup.tick()                       # raises the AP, deadline t0+60
        self.h.clock.advance(10)
        self.h.write_claim(state=net_wifi.WIZARD_SWITCHING, ssid="Home")
        self.h.radio.calls.clear()
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        self.assertEqual(self.h.state().reason, "wizard switching")
        self.assertEqual(self.h.radio.calls, [],
                         "standing down means not touching nmcli at all")

    def test_the_window_is_compensated_for_the_time_spent_standing_down(self):
        """A slow switch must not eat the operator's minutes: two 10 s
        stand-downs push a 60 s window out to 80 s, so at t0+70 the AP is still
        up. Without compensation this tears it down mid-retry."""
        self.h.sup.tick()                                    # t0, deadline t0+60
        self.h.clock.advance(10)
        self.h.write_claim(state=net_wifi.WIZARD_SWITCHING, ssid="Home")
        self.h.sup.tick()                                    # +10 → deadline t0+70
        self.h.clock.advance(10)
        self.h.write_claim(state=net_wifi.WIZARD_SWITCHING, ssid="Home")
        self.h.sup.tick()                                    # +10 → deadline t0+80

        self.h.clock.advance(50)                             # t0+70 < t0+80
        self.h.clear_claim()
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.radio.down_count, 0,
                         "the compensated deadline must not have passed")

    def test_a_successful_switch_leaves_the_hotspot_down(self):
        self.h.sup.tick()
        self.h.radio.ap_up = False        # the server took the AP down to switch
        self.h.radio.sta = True
        self.h.write_claim(state=net_wifi.WIZARD_OK, ssid="Home",
                           address="192.168.1.77/24")
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_STA)
        self.assertEqual(self.h.radio.raise_count, 1, "must not re-raise the AP")
        self.assertEqual(self.h.state().gateway, "192.168.1.77/24")
        self.assertEqual(self.h.state().url, "https://192.168.1.77:8888/setup")

    def test_a_failed_switch_that_restored_the_ap_restarts_the_window(self):
        """Design §6: a failure returns to the hotspot *and* says why. Retrying
        must be possible without a power cycle, so one fresh window per nonce."""
        self.h.sup.tick()
        self.h.clock.advance(61)                       # let the window expire
        self.h.sup.tick()
        self.assertEqual(self.h.state().reason, "timeout")

        self.h.radio.ap_up = True                      # the server put it back
        self.h.write_claim(state=net_wifi.WIZARD_FAILED, ssid="Home",
                           error="Secrets were required", nonce="n1")
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.state().deadline, self.h.clock() + 60)
        self.assertEqual(self.h.radio.down_count, 1, "only the timeout's own teardown")

    def test_the_same_nonce_cannot_buy_a_second_window(self):
        """Otherwise a page that retries on a timer would keep an open network
        alive indefinitely — the exact thing fence 2 exists to prevent."""
        self.h.sup.tick()
        self.h.write_claim(state=net_wifi.WIZARD_FAILED, ssid="Home",
                           error="nope", nonce="n1")
        self.h.sup.tick()
        first_deadline = self.h.state().deadline
        self.h.clock.advance(5)
        self.h.write_claim(state=net_wifi.WIZARD_FAILED, ssid="Home",
                           error="nope", nonce="n1")
        self.h.sup.tick()
        self.assertEqual(self.h.state().deadline, first_deadline)

    def test_a_different_nonce_buys_one(self):
        self.h.sup.tick()
        self.h.write_claim(state=net_wifi.WIZARD_FAILED, ssid="Home",
                           error="nope", nonce="n1")
        self.h.sup.tick()
        first = self.h.state().deadline
        self.h.clock.advance(5)
        self.h.write_claim(state=net_wifi.WIZARD_FAILED, ssid="Home",
                           error="nope", nonce="n2")
        self.h.sup.tick()
        self.assertEqual(self.h.state().deadline, self.h.clock() + 60)
        self.assertGreater(self.h.state().deadline, first)

    def test_a_failed_switch_that_could_not_restore_the_ap_is_repaired(self):
        """The server tried to put the hotspot back and failed. Without this the
        box sits unreachable with nobody able to tell the operator why."""
        self.h.sup.tick()
        self.h.radio.ap_up = False
        self.h.write_claim(state=net_wifi.WIZARD_FAILED, ssid="Home",
                           error="nope", nonce="n1")
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.radio.raise_count, 2)
        self.assertTrue(self.h.radio.ap_up)

    def test_a_stale_claim_is_ignored(self):
        """The server died mid-switch. The supervisor has to take the radio back
        or the box is left with neither an AP nor an uplink, forever."""
        self.h.sup.tick()
        self.h.radio.ap_up = False
        self.h.write_claim(state=net_wifi.WIZARD_SWITCHING, ssid="Home",
                           heartbeat=self.h.clock() - 300)
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_HOTSPOT)
        self.assertEqual(self.h.radio.raise_count, 2)


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(radio=FakeRadio(hotspot_fails=True))
        self.addCleanup(self.h.cleanup)

    def test_a_hotspot_that_will_not_start_is_reported_in_the_state(self):
        """R6: the reason has to be readable without ssh, because the whole point
        is that there may be no way to ssh in."""
        self.assertEqual(self.h.sup.tick(), net_wifi.MODE_OFF)
        state = self.h.state()
        self.assertTrue(state.reason.startswith("start failed:"), state.reason)
        self.assertIn("not suitable", state.reason)

    def test_it_keeps_trying(self):
        """Risk R-1: whether this radio can do AP mode at all is unverified on
        real hardware, so the supervisor must retry rather than give up after
        one answer."""
        for _ in range(3):
            self.h.sup.tick()
            self.h.clock.advance(5)
        self.h.sup.tick()
        self.assertEqual(self.h.radio.count("device wifi hotspot"), 4)

    def test_a_repeating_failure_does_not_fill_the_console(self):
        """The unit forwards to the HDMI console; five identical lines a minute
        would bury the one message the operator needs."""
        for _ in range(20):
            self.h.sup.tick()
            self.h.clock.advance(5)
        errors = self.h.log.messages("error")
        self.assertEqual(len(errors), 2, f"expected rate limiting, got {errors}")


class RunLoopTests(unittest.TestCase):
    def test_run_stops_when_told_and_cleans_up(self):
        h = Harness(stop_after=3)
        self.addCleanup(h.cleanup)
        h.sup.tick()                            # raise the AP so there is
        self.assertTrue(h.radio.ap_up)          # something to clean up
        self.assertEqual(h.sup.run(), 0)
        self.assertFalse(h.radio.ap_up, "stopping the service must take the AP down")
        self.assertEqual(h.state().reason, "service stopped")

    def test_the_loop_sleeps_for_the_configured_poll_interval(self):
        h = Harness(env={"MRRC_SETUP_AP_POLL_S": "7"}, stop_after=3)
        self.addCleanup(h.cleanup)
        h.sup.run()
        loop_sleeps = h.sleeps[1:]              # the first is the settle grace
        self.assertTrue(loop_sleeps)
        self.assertTrue(all(1.0 <= s <= 7.0 for s in loop_sleeps), h.sleeps)

    def test_run_survives_a_tick_that_raises(self):
        """A resident service that dies on one bad nmcli answer stops being able
        to open the window at all, and the only symptom the operator sees is
        "no hotspot ever appeared"."""
        h = Harness(stop_after=3)
        self.addCleanup(h.cleanup)

        def explode(argv):
            raise RuntimeError("nmcli exploded")

        h.sup.runner = explode
        self.assertEqual(h.sup.run(), 0)
        self.assertTrue(any("tick failed" in m for m in h.log.messages("error")))
        self.assertGreaterEqual(len(h.sleeps), 3, "and it must keep ticking")


class SettleTests(unittest.TestCase):
    """D-6's criterion is "no saved WiFi that *connects*" — and connecting takes
    time, so deciding at t=0 would open a window on a box that was about to have
    a network."""

    def test_an_existing_uplink_returns_at_once(self):
        h = Harness(radio=FakeRadio(wired=True))
        self.addCleanup(h.cleanup)
        h.sup._wait_for_nm()
        self.assertEqual(h.sleeps, [])

    def test_it_waits_while_networkmanager_is_still_working(self):
        radio = FakeRadio(connecting=True)
        h = Harness(radio=radio, env={"MRRC_SETUP_AP_SETTLE_S": "5"})
        self.addCleanup(h.cleanup)
        h.sup._wait_for_nm()
        self.assertGreaterEqual(len(h.sleeps), 2,
                                "a device in ip-config is not a settled answer")
        self.assertEqual(radio.raise_count, 0)

    def test_it_gives_one_last_grace_once_nm_has_settled(self):
        h = Harness(env={"MRRC_SETUP_AP_SETTLE_S": "30"})
        self.addCleanup(h.cleanup)
        h.sup._wait_for_nm()
        self.assertIn(setup_ap.GRACE_AFTER_IDLE_S, h.sleeps,
                      "a DHCP handshake in flight is not 'no network'")

    def test_it_stops_waiting_when_the_settle_window_expires(self):
        h = Harness(radio=FakeRadio(connecting=True),
                    env={"MRRC_SETUP_AP_SETTLE_S": "3"})
        self.addCleanup(h.cleanup)
        h.sup._wait_for_nm()
        self.assertLessEqual(sum(h.sleeps), 4.0,
                             "it must not wait out the whole window plus grace")


class SettingsTests(unittest.TestCase):
    def test_the_defaults_match_the_design(self):
        cfg = setup_ap.settings({})
        self.assertEqual(cfg["timeout_s"], 30 * 60)     # D-6: 30 minutes
        self.assertEqual(cfg["poll_s"], setup_ap.DEFAULT_POLL_S)
        self.assertEqual(cfg["settle_s"], setup_ap.DEFAULT_SETTLE_S)
        self.assertEqual(cfg["ssid"], net_wifi.DEFAULT_SSID)

    def test_the_timeout_is_configurable_in_minutes(self):
        cfg = setup_ap.settings({"MRRC_SETUP_AP_TIMEOUT_MIN": "5"})
        self.assertEqual(cfg["timeout_s"], 300)

    def test_a_zero_or_negative_timeout_falls_back(self):
        """0 would close the window the instant it opened, i.e. no onboarding at
        all — and it is exactly what a typo produces."""
        for bad in ("0", "-5", "abc", "", None):
            with self.subTest(bad=bad):
                cfg = setup_ap.settings({"MRRC_SETUP_AP_TIMEOUT_MIN": bad})
                self.assertEqual(cfg["timeout_s"], 30 * 60)

    def test_the_ssid_is_configurable(self):
        cfg = setup_ap.settings({"MRRC_SETUP_AP_SSID": "HB9XYZ-box"})
        self.assertEqual(cfg["ssid"], "HB9XYZ-box")


if __name__ == "__main__":
    unittest.main()
