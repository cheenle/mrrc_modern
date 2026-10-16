"""The field-acceptance judges, tested without a radio (spec §8).

`tools/power/power_acceptance.py` exists because one guard can only be measured on
the bench — whether `PS1;` wakes this radio (V2) and whether the app admits it when
it doesn't (V6) — and that reading decides whether the 「重开电台」 menu item ships
at all. The I/O half needs the radio; the *judging* half must not, or the script
would be untestable on the day it is needed.

So the judges are pure: feed them timestamped frames, read back the verdict. These
tests feed them the exact frame shapes the server broadcasts, including the
negative ones (a wake that never comes, a cancellation that still powers up, a
listen session that should have been refused) — a judge that cannot fail is
decoration.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "power_acceptance", ROOT / "tools" / "power" / "power_acceptance.py")
assert SPEC and SPEC.loader
pa = importlib.util.module_from_spec(SPEC)
sys.modules["power_acceptance"] = pa
SPEC.loader.exec_module(pa)


def _rec(*frames) -> "pa.Recorder":
    rec = pa.Recorder()
    for when, msg in frames:
        rec.frames.append((when, msg))
    return rec


def _state_update(power_on: bool) -> dict:
    return {"type": "stateUpdate", "fields": {"power_on": power_on}}


def _rescue(state: str, attempt: int) -> dict:
    return {"type": "powerRescue", "state": state, "attempt": attempt,
            "elapsed": 0.0, "maxSeconds": 900}


def _error(text: str) -> dict:
    return {"type": "error", "message": text}


class JudgeOffTests(unittest.TestCase):
    def test_accepts_a_confirmed_off(self):
        rec = _rec((1.0, _state_update(False)))
        ok, why = pa.judge_off(rec, sent_at=0.0)
        self.assertTrue(ok, why)

    def test_rejects_a_refusal(self):
        rec = _rec((1.0, _error("发射中不能关机")))
        ok, why = pa.judge_off(rec, sent_at=0.0)
        self.assertFalse(ok)
        self.assertIn("发射中", why)

    def test_requires_the_state_to_come_back(self):
        rec = _rec((1.0, {"type": "stateUpdate", "fields": {"freq_hz": 14200000}}))
        ok, why = pa.judge_off(rec, sent_at=0.0)
        self.assertFalse(ok)
        self.assertIn("power_on", why)


class JudgeRestartTests(unittest.TestCase):
    def test_records_the_round_and_the_time(self):
        """V2 的产物就是这两个数字 —— 它们是复核 2026-07『PS1 不可靠』的唯一读数。"""
        rec = _rec((1.0, _rescue("running", 1)), (2.0, _rescue("running", 2)),
                   (75.0, _rescue("woke", 3)))
        ok, why = pa.judge_restart(rec, sent_at=0.0)
        self.assertTrue(ok, why)
        self.assertIn("第 3 轮", why)
        self.assertIn("75", why)

    def test_a_timeout_is_not_a_success(self):
        rec = _rec((1.0, _rescue("running", 1)), (901.0, _rescue("timeout", 13)))
        ok, why = pa.judge_restart(rec, sent_at=0.0)
        self.assertFalse(ok)
        self.assertIn("没唤醒", why)

    def test_no_broadcast_at_all_is_a_failure(self):
        ok, why = pa.judge_restart(_rec(), sent_at=0.0)
        self.assertFalse(ok)

    def test_a_refusal_at_the_start_is_a_failure(self):
        rec = _rec((1.0, _error("FT8 正在发射，不能关机")))
        ok, why = pa.judge_restart(rec, sent_at=0.0)
        self.assertFalse(ok)
        self.assertIn("FT8", why)

    def test_a_window_refusal_after_the_start_does_not_hide_the_wake(self):
        """V4 会先自己发一条"秒后再关机"的拒绝；它不该被判成 V2 的失败。"""
        rec = _rec((5.0, _error("电台正在启动，请 12 秒后再关机")),
                   (40.0, _rescue("woke", 1)))
        ok, why = pa.judge_restart(rec, sent_at=0.0)
        self.assertTrue(ok, why)


class JudgeCancelTests(unittest.TestCase):
    def test_cancel_is_accepted_when_it_stays_off(self):
        rec = _rec((1.0, _rescue("running", 1)), (2.0, _rescue("cancelled", 1)),
                   (2.1, _state_update(False)))
        ok, why = pa.judge_cancel(rec, cancelled_at=1.5)
        self.assertTrue(ok, why)

    def test_a_wake_after_the_cancel_is_a_failure(self):
        rec = _rec((1.0, _rescue("running", 1)), (2.0, _rescue("cancelled", 1)),
                   (3.0, _rescue("woke", 1)))
        ok, why = pa.judge_cancel(rec, cancelled_at=1.5)
        self.assertFalse(ok)
        self.assertIn("唤醒", why)


class JudgeRefusalTests(unittest.TestCase):
    def test_window_refusal_needs_the_countdown_text(self):
        rec = _rec((1.0, _error("电台正在启动，请 9 秒后再关机")))
        ok, why = pa.judge_window_refusal(rec, sent_at=0.0)
        self.assertTrue(ok, why)
        self.assertFalse(pa.judge_window_refusal(_rec((1.0, _state_update(False))), 0.0)[0])

    def test_listen_refusal_needs_the_role_message(self):
        rec = _rec((1.0, _error("Listen-only session: only frequency and mode can be changed.")))
        ok, why = pa.judge_listen_refusal(rec, sent_at=0.0)
        self.assertTrue(ok, why)

    def test_listen_refusal_fails_if_a_rescue_started(self):
        rec = _rec((1.0, _error("Listen-only session: only frequency and mode can be changed.")),
                   (2.0, _rescue("running", 1)))
        ok, why = pa.judge_listen_refusal(rec, sent_at=0.0)
        self.assertFalse(ok)

    def test_tx_refusal_accepts_the_ft8_wording(self):
        rec = _rec((1.0, _error("FT8 正在发射，不能关机")))
        self.assertTrue(pa.judge_tx_refusal(rec, sent_at=0.0)[0])


class ReportTests(unittest.TestCase):
    def test_report_carries_the_table_and_the_evidence(self):
        rec = _rec((0.0, _rescue("running", 1)), (12.5, _rescue("woke", 1)),
                   (12.6, _state_update(True)))
        rec.note("V2：电台第 1 轮就醒了")
        text = pa.render_report(
            [("V2 重开", "记录第几轮", "第 1 轮，12 秒", "第 1 轮，12 秒", "✅")],
            rec, "2026-10-16 20:00", "https://127.0.0.1:8888 /WSradio")
        self.assertIn("| V2 重开 |", text)
        self.assertIn("powerRescue state=woke attempt=1", text)
        self.assertIn("V2：电台第 1 轮就醒了", text)


if __name__ == "__main__":
    unittest.main()
