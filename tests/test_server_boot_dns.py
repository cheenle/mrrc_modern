"""The start-up DNS hook (2026-10-25).

Every shipped box carries the two fallback resolvers on each saved profile, because
an empty resolver is not a visible fault: on 2026-10-25 a box came up before DHCP
finished, `chronyc` could not resolve its NTP pool name, the clock stayed 24 days
behind the image date, and every HTTPS call then failed with
``certificate is not yet valid`` while the page said only "连不上门户".

``net_wifi.write_profile`` covers networks joined from now on and ``preflash.sh``
covers the image, so what is left to prove here is the **upgrade path**: a box
flashed before the fallback existed runs ``_ensure_fallback_dns`` once per start-up,
on a daemon thread, and must neither raise into the boot path nor reconfigure
anything silently.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import net_wifi
import server


class FallbackDnsBootTests(unittest.TestCase):
    def test_a_change_is_reported_with_the_names_it_touched(self):
        with patch.object(net_wifi, "ensure_fallback_dns", return_value=["Home"]) as fix, \
             patch.object(server.logger, "info") as info:
            self.assertEqual(server._ensure_fallback_dns(), ["Home"])
        fix.assert_called_once_with()
        message = " ".join(str(arg) for arg in info.call_args.args)
        self.assertIn("Home", message)
        # The servers themselves belong in the sentence: an operator reading the log
        # should not have to open net_wifi.py to learn what was written.
        self.assertIn(net_wifi.FALLBACK_DNS[0], message)

    def test_nothing_to_do_is_quiet(self):
        with patch.object(net_wifi, "ensure_fallback_dns", return_value=[]), \
             patch.object(server.logger, "info") as info:
            self.assertEqual(server._ensure_fallback_dns(), [])
        info.assert_not_called()

    def test_a_failure_never_escapes_into_the_boot_path(self):
        """This runs on a daemon thread during start-up; an exception here would end
        the thread and leave a box that looks configured and is not."""
        with patch.object(net_wifi, "ensure_fallback_dns",
                          side_effect=OSError("nmcli exploded")), \
             patch.object(server.logger, "warning") as warn:
            self.assertEqual(server._ensure_fallback_dns(), [])
        self.assertIn("nmcli exploded", " ".join(str(arg) for arg in warn.call_args.args))


if __name__ == "__main__":
    unittest.main()
