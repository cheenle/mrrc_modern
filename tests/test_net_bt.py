"""Bluetooth pairing for the remote (AD-028): the settings-page path.

The page exists so a customer never needs a shell.  Two things decide whether
it is honest:

* the **verdict** it shows is "the keys work" (a keyboard input device from
  that address exists), not "bluez said paired" — the field defect was a remote
  that reported ``Paired: yes`` with an unencrypted link and dead keys;
* every bluez call goes through the injectable runner, so none of this needs a
  radio, a controller, or root.
"""

import unittest
from pathlib import Path

import net_bt


def runner_for(stdout="", rc=0, stderr=""):
    calls = []

    def run(argv):
        calls.append(argv)
        return rc, stdout, stderr

    run.calls = calls          # type: ignore[attr-defined]
    return run


SCAN_OUT = """\x1b[0;92m[NEW]\x1b[0m Device 0C:F3:CE:B0:88:75 CMCC_Voice_Remote
\x1b[0;92m[NEW]\x1b[0m Device AA:BB:CC:DD:EE:01 AA-BB-CC-DD-EE-01
[NEW] Device 0C:F3:CE:B0:88:75 CMCC_Voice_Remote
"""

INFO_OUT = """Device 0C:F3:CE:B0:88:75 (public)
\tName: CMCC_Voice_Remote
\tAlias: CMCC_Voice_Remote
\tPaired: yes
\tBonded: yes
\tTrusted: yes
\tConnected: no
"""


class ScanTests(unittest.TestCase):
    def test_parses_named_devices_deduped_and_strips_colour(self):
        devices = net_bt.parse_devices(SCAN_OUT)
        self.assertEqual([d["address"] for d in devices],
                         ["0C:F3:CE:B0:88:75", "AA:BB:CC:DD:EE:01"])
        self.assertEqual(devices[0]["name"], "CMCC_Voice_Remote")
        self.assertEqual(devices[1]["name"], "")     # bluez prints a dashed echo

    def test_scan_uses_a_bredr_inquiry(self):
        run = runner_for(SCAN_OUT)
        net_bt.scan(seconds=3, runner=run)
        self.assertEqual(run.calls[0][:3], ["bluetoothctl", "--timeout", "3"])
        self.assertIn("bredr", run.calls[0])

    def test_scan_failure_yields_no_devices(self):
        self.assertEqual(net_bt.scan(runner=runner_for("", rc=1, stderr="no controller")), [])


class InfoTests(unittest.TestCase):
    def test_flags_are_booleans(self):
        info = net_bt.parse_info(INFO_OUT)
        self.assertEqual(info["name"], "CMCC_Voice_Remote")
        self.assertTrue(info["paired"])
        self.assertTrue(info["bonded"])
        self.assertFalse(info["connected"])


class PairTests(unittest.TestCase):
    def test_pair_then_trust_then_read_back(self):
        run = runner_for(INFO_OUT)
        info = net_bt.pair("0C:F3:CE:B0:88:75", runner=run)
        verbs = [call[3] if call[1] == "--timeout" else call[1] for call in run.calls]
        self.assertEqual(verbs, ["pair", "trust", "info"])
        self.assertTrue(info["paired"])

    def test_forget_power_cycles_the_controller(self):
        run = runner_for()
        net_bt.power_cycle(runner=run)
        self.assertEqual([call[2] for call in run.calls], ["off", "on"])


class VerdictTests(unittest.TestCase):
    """The judgement the page acts on: do the keys arrive?"""

    def _proc(self, tmp, block):
        path = Path(tmp) / "devices"
        path.write_text(block, encoding="utf-8")
        return str(path)

    def test_keyboard_device_from_the_remote_counts(self):
        import tempfile
        block = ('I: Bus=0005 Vendor=0416 Product=0300 Version=0001\n'
                 'U: Uniq=0c:f3:ce:b0:88:75\n'
                 'N: Name="CMCC_Voice_Remote Keyboard"\n'
                 'H: Handlers=sysrq kbd event3 leds\n\n')
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(net_bt.keyboard_device_present("0C:F3:CE:B0:88:75", self._proc(tmp, block)))

    def test_paired_but_no_device_is_the_failed_case(self):
        import tempfile
        block = ('I: Bus=0003 Vendor=0D8C Product=0013\n'
                 'N: Name="USB Audio Device"\n'
                 'H: Handlers=kbd event2\n\n')
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(net_bt.keyboard_device_present("0C:F3:CE:B0:88:75", self._proc(tmp, block)))

    def test_state_combines_flags_and_verdict(self):
        state = net_bt.state("0C:F3:CE:B0:88:75", runner=runner_for(INFO_OUT))
        self.assertTrue(state["paired"])
        self.assertIn("keys_work", state)
        self.assertIsInstance(state["keys_work"], bool)
        self.assertEqual(set(state), {"address", "name", "paired", "bonded",
                                      "connected", "trusted", "keys_work"})


class SettingsPageTests(unittest.TestCase):
    """The page is the whole point: no SSH for the customer."""

    def setUp(self):
        self.html = (Path(__file__).resolve().parent.parent
                     / "static" / "settings.html").read_text(encoding="utf-8")

    def test_the_panel_and_its_buttons_exist(self):
        for hook in ("bt-scan", "bt-list", "bt-msg"):
            self.assertIn(f'id="{hook}"', self.html)
        self.assertIn("/api/box/bt/scan", self.html)
        self.assertIn("/api/box/bt/pair", self.html)
        self.assertIn("/api/box/bt/forget", self.html)

    def test_it_shows_the_verdict_not_just_paired(self):
        self.assertIn("keys_work", self.html)

    def test_the_page_still_avoids_innerhtml(self):
        self.assertNotIn("innerHTML", self.html)


if __name__ == "__main__":
    unittest.main()
