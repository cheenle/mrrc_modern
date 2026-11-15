"""`_diag_scu_lan.py` end to end against the scripted unit.

The self-check is what a customer will run on a real radio, so its failure
modes matter: it must reach the session, print a report a human can read, and
never key the transmitter unless it was asked to twice (`--tx --allow-tx`).
This drives the whole path — argparse → client → seven phases → CAT probe →
sweep decode → report — with no hardware and no server.
"""
import argparse
import os
import tempfile
import unittest

import _diag_scu_lan as diag
from tests.test_scu_lan_client import FakeScuLan


def make_args(**overrides) -> argparse.Namespace:
    base = dict(
        host="127.0.0.1", user="defaultuser", password="defaultuser",
        port=0, model="ftdx101mp", seconds=0.4, csv="", wav="", spans=False,
        tx=False, allow_tx=False, tx_seconds=0.1, report="",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class DiagScriptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # A wrong-password run is silent by design; do not spend 10 s proving it.
        self._saved = (diag.CONNECT_TIMEOUT_S, diag.AUTH_TIMEOUT_S,
                       diag.CAT_TIMEOUT_S)
        diag.CONNECT_TIMEOUT_S, diag.AUTH_TIMEOUT_S = 1.2, 0.6
        diag.CAT_TIMEOUT_S = 0.15
        self.addCleanup(self._restore_timeouts)
        self.fake = FakeScuLan()
        self.base_port = await self.fake.start()

    def _restore_timeouts(self) -> None:
        (diag.CONNECT_TIMEOUT_S, diag.AUTH_TIMEOUT_S,
         diag.CAT_TIMEOUT_S) = self._saved

    async def asyncTearDown(self) -> None:
        await self.fake.stop()

    async def run_diag(self, **overrides) -> tuple[int, str]:
        args = make_args(port=self.base_port, **overrides)
        with tempfile.TemporaryDirectory() as tmp:
            if args.report:
                args.report = os.path.join(tmp, "report.md")
            if args.csv:
                args.csv = os.path.join(tmp, "bins.csv")
            code = await diag.run(args)
            text = ""
            if args.report:
                with open(args.report, encoding="utf-8") as handle:
                    text = handle.read()
            if args.csv:
                self.csv_path = args.csv
                self.csv_lines = []
                if os.path.exists(args.csv):
                    with open(args.csv, encoding="utf-8") as handle:
                        self.csv_lines = handle.read().splitlines()
        return code, text

    async def test_it_reaches_the_session_and_reports_every_section(self):
        code, report = await self.run_diag(report="-")
        self.assertEqual(code, 0, report)
        self.assertIn("## 1. Session — OK", report)
        self.assertIn("session id", report)
        self.assertIn("## 2. CAT over the tunnel", report)
        self.assertIn("## 3. Spectrum", report)
        self.assertIn("## 4. RX audio", report)

    async def test_it_decodes_a_sweep_and_names_the_marker(self):
        _code, report = await self.run_diag(report="-")
        self.assertIn("boundary marker at bin 1900", report)
        self.assertIn("usable bins 950", report)

    async def test_the_cat_table_shows_the_answer_it_got(self):
        _code, report = await self.run_diag(report="-")
        self.assertIn("`FA;` | `FA007007000;`", report)

    async def test_it_writes_the_native_bins_when_asked(self):
        await self.run_diag(report="-", csv="bins.csv")
        lines = self.csv_lines
        self.assertTrue(lines, "the CSV was not written")
        self.assertEqual(lines[0], "bin,value")
        self.assertEqual(len(lines), 1 + 950)          # marker 1900 ÷ 2

    async def test_ptt_is_never_keyed_without_the_second_flag(self):
        _code, report = await self.run_diag(report="-", tx=True)
        self.assertIn("PTT read-back — SKIPPED", report)
        self.assertNotIn("after TX1", report)
        self.assertEqual(self.fake.cat_requests and
                         [text for _seq, text in self.fake.cat_requests
                          if text.startswith("TX")], [])

    async def test_a_wrong_password_is_reported_as_such(self):
        args = make_args(port=self.base_port, password="wrong", report="")
        code = await diag.run(args)
        self.assertEqual(code, 2)

    async def test_it_reports_a_dead_scope_stream_instead_of_hanging(self):
        self.fake.SCOPE_PUSH = 0
        code, report = await self.run_diag(report="-")
        self.assertIn("no scope bodies arrived", report)
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
