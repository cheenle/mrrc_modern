import argparse
import asyncio
import unittest
from unittest import mock

import _diag_sunsdr as diag


class SunSDRDiagnosticTests(unittest.TestCase):
    def test_tx_requires_both_switches(self):
        self.assertFalse(diag.tx_check_permitted(False, False))
        self.assertFalse(diag.tx_check_permitted(True, False))
        self.assertFalse(diag.tx_check_permitted(False, True))
        self.assertTrue(diag.tx_check_permitted(True, True))

    def test_parser_is_read_only_by_default(self):
        args = diag.build_parser().parse_args(["--host", "radio.local"])
        self.assertFalse(args.tx_check)
        self.assertFalse(args.allow_tx)

    def test_tx_failure_still_releases_and_disconnects(self):
        calls = []

        class Engine:
            connected = True
            host = "127.0.0.1"
            local_host = "127.0.0.1"
            control_port = 50001
            iq_port = 50002
            sample_rate = 78125
            recv_packets = 0
            recv_bytes = 0
            bad_packets = 0
            rx_drops = 0
            tx_drops = 0
            underruns = 0
            ptt_requested = False
            ptt_confirmed = False
            latest_telemetry = None
            s_meter = None

            async def start(self):
                return True

            async def set_ptt(self, tx):
                calls.append(("ptt", tx))
                if tx:
                    raise OSError("injected")

            async def stop(self):
                calls.append(("stop", True))

            def get_spectrum(self):
                return [0] * 850

        args = argparse.Namespace(host="127.0.0.1", local_host="127.0.0.1",
                                  control_port=50001, iq_port=50002,
                                  sample_rate=78125, wait=0.0,
                                  tx_check=True, allow_tx=True)
        with mock.patch.object(diag, "SunSDREngine", return_value=Engine()), \
                mock.patch("builtins.print"):
            result = asyncio.run(diag.run(args))
        self.assertEqual(result, 1)
        self.assertIn(("ptt", False), calls)
        self.assertEqual(calls[-1], ("stop", True))


if __name__ == "__main__":
    unittest.main()
