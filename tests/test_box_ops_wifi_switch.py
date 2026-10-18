"""The Wi-Fi switch: the one action that can lose the box.

The board has a single radio, so joining a network is not an addition, it is a
replacement — and if the operator is reaching the box *over* that radio, the
replacement takes their own connection away mid-request. Everything in this file
is about what happens on the other side of that.

Two lessons from the retired supervisor are pinned here, because both were paid
for on real hardware:

* a profile this module writes must state `connection.autoconnect` explicitly.
  The hotspot's profile defaulted to yes, came up on every boot, and took the
  board's Wi-Fi down with it.
* nmcli returns as soon as it has *associated*, which is well before DHCP has
  landed. Believing that return value is how a switch reports success and leaves
  a box with no address.

`sleep` and `now` are injected, so a thirty-second budget costs no wall clock.
"""
from __future__ import annotations

import unittest

import box_ops
import net_wifi
from tests.test_net_wifi import FakeNmcli


class Fake( object ):
    """A runner plus the address nmcli will report, changing over time."""

    def __init__(self, table=None, default=None, addresses=None):
        self.inner = FakeNmcli(table, default)
        #: One entry per `IP4.ADDRESS` poll. The last entry repeats.
        self.addresses = list(addresses or [""])
        self.polls = 0

    def __call__(self, argv):
        argv = list(argv)
        if "IP4.ADDRESS" in argv:
            index = min(self.polls, len(self.addresses) - 1)
            self.polls += 1
            return net_wifi.NmResult(0, self.addresses[index], "")
        return self.inner(argv)

    def count(self, key):
        return self.inner.count(key)

    def joined(self):
        return self.inner.joined()


def clock():
    """A monotonic clock that only moves when `sleep` is called."""
    state = {"t": 0.0}

    def now():
        return state["t"]

    def sleep(seconds):
        state["t"] += seconds

    return now, sleep


class StateTests(unittest.TestCase):
    def test_a_fresh_state_is_idle(self):
        snapshot = box_ops.switch_state(box_ops.SwitchState())
        self.assertEqual(snapshot["phase"], box_ops.PHASE_IDLE)
        self.assertEqual(snapshot["ssid"], "")
        self.assertEqual(snapshot["reason"], "")

    def test_the_snapshot_has_nowhere_to_put_a_password(self):
        """The whole reason the state is a small fixed dict rather than a
        free-form one: a PSK must have no key to land in."""
        snapshot = box_ops.switch_state(box_ops.SwitchState())
        self.assertEqual(set(snapshot),
                         {"phase", "ssid", "reason", "address", "since", "saved"})


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.now, self.sleep = clock()

    def run_switch(self, runner, **kwargs):
        return box_ops.perform_wifi_switch(
            "NewNet", "hunter2secret", runner=runner, state=box_ops.SwitchState(),
            sleep=self.sleep, now=self.now, **kwargs)

    def test_a_successful_switch_reports_the_address_it_got(self):
        runner = Fake({"connection up NewNet": net_wifi.NmResult(0, "", ""),
                       "connection show": net_wifi.NmResult(
                           0, "home:802-11-wireless:yes\n", "")},
                      addresses=["", "192.168.1.217/24"])
        result = self.run_switch(runner)
        self.assertTrue(result["ok"])
        self.assertEqual(result["address"], "192.168.1.217/24")

    def test_it_polls_because_connecting_is_not_getting_an_address(self):
        """nmcli's success means associated, not addressed. The first poll here
        is empty and the switch must keep waiting rather than report success
        with no address."""
        runner = Fake({"connection up NewNet": net_wifi.NmResult(0, "", "")},
                      addresses=["", "", "192.168.1.217/24"])
        result = self.run_switch(runner)
        self.assertTrue(result["ok"])
        self.assertEqual(runner.polls, 3)

    def test_it_does_not_believe_association_on_its_own(self):
        runner = Fake({"connection up NewNet": net_wifi.NmResult(0, "", "")},
                      addresses=[""])
        result = self.run_switch(runner)
        self.assertFalse(result["ok"])

    def test_the_password_never_appears_in_the_result(self):
        """It travels in nmcli's argv and nowhere else — and the failure reason
        is exactly where it would leak, because nmcli's own error text is the one
        place a credential can ride along."""
        runner = Fake({
            "connection up NewNet": net_wifi.NmResult(
                1, "", "Error: Secrets were required, but not provided. "
                       "hunter2secret rejected."),
        })
        result = self.run_switch(runner)
        self.assertFalse(result["ok"])
        self.assertNotIn("hunter2secret", result["reason"])
        self.assertNotIn("hunter2secret", str(result))

    def test_the_password_reaches_nmcli_and_only_there(self):
        runner = Fake({"connection up NewNet": net_wifi.NmResult(0, "", "")},
                      addresses=["192.168.1.217/24"])
        self.run_switch(runner)
        self.assertIn("hunter2secret", runner.joined())

    def test_a_failure_is_reported_with_its_reason(self):
        runner = Fake({
            "connection up NewNet": net_wifi.NmResult(
                1, "", "Error: No network with SSID 'NewNet' found."),
        })
        result = self.run_switch(runner)
        self.assertFalse(result["ok"])
        self.assertIn("NewNet", result["reason"])

    def test_a_failed_connect_tears_down_the_same_way_a_timeout_does(self):
        """F1: a failed activation is a failure, and the failure paths share
        one teardown. `net_wifi.connect_wifi` writes the profile *before* it
        brings it up, so a failed join always leaves one behind — and a profile
        left at `autoconnect=yes` retries at every boot, which is the retired
        hotspot's harm in reverse. Show the argv, not "a rollback happened"."""
        runner = Fake({
            "connection up NewNet": net_wifi.NmResult(
                1, "", "Error: Secrets were required, but not provided."),
            "connection show": net_wifi.NmResult(
                0, "home:802-11-wireless:yes\n", ""),
        })
        result = self.run_switch(runner)
        self.assertFalse(result["ok"])
        self.assertIn("connection modify NewNet connection.autoconnect no",
                      runner.joined())
        self.assertIn("connection up home", runner.joined())

    def test_a_network_that_is_not_here_is_saved_and_left_armed(self):
        """客户现场才有 AP 的那种盒子必须能预置。

        切换失败是因为**不在范围内**，不是配置错——此时把 profile 停用
        （`autoconnect no`）等于把操作员刚敲的那两行扔掉：到了客户家里永远不会连。
        2026-10-17 实测就是这么坏的：profile 落地了，但 `autoconnect=false`。
        """
        runner = Fake({
            "connection up NewNet": net_wifi.NmResult(
                1, "", "错误：超时时间已到（25 秒）"),
            "connection show": net_wifi.NmResult(
                0, "home:802-11-wireless:yes\n", ""),
        })
        result = self.run_switch(runner)
        self.assertFalse(result["ok"])
        self.assertNotIn("connection.autoconnect no", runner.joined())
        self.assertIn("已保存", result["reason"])
        self.assertIn("自动连接", result["reason"])
        self.assertTrue(result.get("saved"))
        # 旧网络的下场必须写出来：这条消息可能是操作员在刚刚断掉的 Wi-Fi 上读到的。
        self.assertIn("home", result["reason"])

    def test_a_rejected_credential_still_disables_the_profile(self):
        """密码错了是另一回事：每次开机重试一个永远不会成功的网络没有意义。"""
        runner = Fake({
            "connection up NewNet": net_wifi.NmResult(
                1, "", "Error: Connection activation failed: (7) Secrets were required"),
        })
        result = self.run_switch(runner)
        self.assertIn("connection modify NewNet connection.autoconnect no",
                      runner.joined())
        self.assertFalse(result.get("saved"))

    def test_a_failed_connect_does_not_claim_a_timeout(self):
        """The reason is a parameter because the two failure paths must not say
        the same thing — a connect that returned an error did not spend the
        budget, and the operator reading over a dead network is owed the truth."""
        runner = Fake({
            "connection up NewNet": net_wifi.NmResult(
                1, "", "Error: No network with SSID 'NewNet' found."),
            "connection show": net_wifi.NmResult(
                0, "home:802-11-wireless:yes\n", ""),
        })
        result = self.run_switch(runner)
        self.assertNotIn("秒内没有拿到地址", result["reason"])

    def test_a_zero_poll_returns_instead_of_spinning(self):
        """F2: with an injected clock, `poll=0` never advances time, so the
        polling loop would never end. A non-positive interval is refused before
        any poll happens; the poll count proves it did not spin."""
        runner = Fake({"connection up NewNet": net_wifi.NmResult(0, "", "")},
                      addresses=[""])
        result = self.run_switch(runner, poll=0)
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])
        self.assertEqual(runner.polls, 0)

    def test_a_negative_poll_is_a_dict_not_a_raise(self):
        """`time.sleep(-1)` raises `ValueError`, which would escape the
        always-returns-a-dict contract the `asyncio.to_thread` call site
        depends on."""
        runner = Fake({"connection up NewNet": net_wifi.NmResult(0, "", "")},
                      addresses=[""])
        result = self.run_switch(runner, poll=-1.0)
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])

    def test_the_state_moves_through_running_to_a_terminal_phase(self):
        runner = Fake({"connection up NewNet": net_wifi.NmResult(0, "", "")},
                      addresses=["192.168.1.217/24"])
        state = box_ops.SwitchState()
        seen = []
        # The hook wraps the callable the module is handed, rather than an
        # instance attribute named `__call__`: `runner(argv)` resolves through
        # the type's slot, so assigning `runner.__call__` on the instance would
        # never be consulted and `seen` would stay empty.
        def watching(argv):
            seen.append(box_ops.switch_state(state)["phase"])
            return runner(argv)

        box_ops.perform_wifi_switch("NewNet", "pw", runner=watching, state=state,
                                    sleep=self.sleep, now=self.now)
        self.assertIn(box_ops.PHASE_SWITCHING, seen)
        self.assertEqual(box_ops.switch_state(state)["phase"], box_ops.PHASE_OK)

    def test_a_second_switch_is_refused_while_one_is_running(self):
        state = box_ops.SwitchState()
        state.phase = box_ops.PHASE_SWITCHING
        result = box_ops.perform_wifi_switch(
            "Other", "pw", runner=Fake({}), state=state,
            sleep=self.sleep, now=self.now)
        self.assertFalse(result["ok"])


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.now, self.sleep = clock()

    def test_a_timeout_puts_the_old_network_back(self):
        """The budget is spent, no address ever arrived, and the profile we came
        from is what the box has to end up on — otherwise it is off the network
        with nobody able to reach it."""
        runner = Fake({
            "connection up NewNet": net_wifi.NmResult(0, "", ""),
            "connection show": net_wifi.NmResult(
                0, "home:802-11-wireless:yes\n", ""),
        }, addresses=[""])
        result = box_ops.perform_wifi_switch(
            "NewNet", "pw", runner=runner, state=box_ops.SwitchState(),
            sleep=self.sleep, now=self.now)
        self.assertFalse(result["ok"])
        self.assertIn("connection up home", runner.joined())

    def test_the_failed_profile_does_not_come_back_on_its_own(self):
        """`autoconnect yes` on a network that just failed is a box that loses
        its Wi-Fi at every boot. The retired hotspot's profile did the mirror
        image of this."""
        runner = Fake({
            "connection up NewNet": net_wifi.NmResult(0, "", ""),
            "connection show": net_wifi.NmResult(
                0, "home:802-11-wireless:yes\n", ""),
        }, addresses=[""])
        box_ops.perform_wifi_switch("NewNet", "pw", runner=runner,
                                    state=box_ops.SwitchState(),
                                    sleep=self.sleep, now=self.now)
        self.assertIn("connection.autoconnect no", runner.joined())

    def test_the_budget_is_actually_spent_not_ignored(self):
        runner = Fake({"connection up NewNet": net_wifi.NmResult(0, "", "")},
                      addresses=[""])
        box_ops.perform_wifi_switch("NewNet", "pw", runner=runner,
                                    state=box_ops.SwitchState(), budget=5.0,
                                    poll=1.0, sleep=self.sleep, now=self.now)
        self.assertEqual(self.now(), 5.0)

    def test_the_reason_says_which_old_network_it_went_back_to(self):
        runner = Fake({
            "connection up NewNet": net_wifi.NmResult(0, "", ""),
            "connection show": net_wifi.NmResult(
                0, "home:802-11-wireless:yes\n", ""),
        }, addresses=[""])
        result = box_ops.perform_wifi_switch(
            "NewNet", "pw", runner=runner, state=box_ops.SwitchState(),
            sleep=self.sleep, now=self.now)
        self.assertIn("home", result["reason"])

    def test_no_old_network_is_said_out_loud(self):
        runner = Fake({"connection up NewNet": net_wifi.NmResult(0, "", "")},
                      addresses=[""])
        result = box_ops.perform_wifi_switch(
            "NewNet", "pw", runner=runner, state=box_ops.SwitchState(),
            sleep=self.sleep, now=self.now)
        self.assertFalse(result["ok"])
        self.assertTrue(result["reason"])

    def test_the_reason_names_only_the_network_that_actually_came_back(self):
        """F3: `connection up <old>`'s result used to be ignored while the reason
        read "已回退到 <old>". This string is what the operator reads over a dead
        network; it may not claim a restore that did not happen."""
        runner = Fake({
            "connection up NewNet": net_wifi.NmResult(0, "", ""),
            "connection show": net_wifi.NmResult(
                0, "home:802-11-wireless:yes\n"
                   "office:802-11-wireless:yes\n", ""),
            "connection up home": net_wifi.NmResult(
                1, "", "Error: Connection 'home' is not available"),
        }, addresses=[""])
        result = box_ops.perform_wifi_switch(
            "NewNet", "pw", runner=runner, state=box_ops.SwitchState(),
            sleep=self.sleep, now=self.now)
        self.assertFalse(result["ok"])
        self.assertIn("office", result["reason"])
        self.assertNotIn("home", result["reason"])


if __name__ == "__main__":
    unittest.main()
