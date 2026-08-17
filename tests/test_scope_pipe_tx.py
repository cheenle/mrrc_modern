"""Tests for the scope_pipe TX-pause control channel.

The FT-710 garbles its scope stream during TX.  The scope producer
(backends/ft710/scope_producer.py) notifies scope_pipe over stdin
(TX:1/TX:0); the pipe pauses SPI reads while TX is active and re-syncs
once on the TX→RX transition.
"""
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from backends.ft710.scope_pipe import apply_control_line
import backends.ft710.scope_producer as scope_producer
from backends.ft710.scope_producer import (
    FT710ScopeProducer, terminate_process_tree_sync,
)


class ApplyControlLineTests(unittest.TestCase):
    def setUp(self):
        self.state = {"tx_active": False, "tx_resync": False}

    def test_tx_on_marks_active(self):
        self.assertTrue(apply_control_line("TX:1\n", self.state))
        self.assertTrue(self.state["tx_active"])
        self.assertFalse(self.state["tx_resync"])

    def test_tx_off_arms_one_shot_resync(self):
        apply_control_line("TX:1", self.state)
        self.assertTrue(apply_control_line("TX:0", self.state))
        self.assertFalse(self.state["tx_active"])
        self.assertTrue(self.state["tx_resync"])

    def test_tx_off_without_prior_on_does_not_resync(self):
        apply_control_line("TX:0", self.state)
        self.assertFalse(self.state["tx_active"])
        self.assertFalse(self.state["tx_resync"])

    def test_repeated_tx_on_is_idempotent(self):
        apply_control_line("TX:1", self.state)
        apply_control_line("TX:1", self.state)
        self.assertTrue(self.state["tx_active"])
        self.assertFalse(self.state["tx_resync"])

    def test_unknown_lines_are_ignored(self):
        self.assertFalse(apply_control_line("HELLO", self.state))
        self.assertFalse(apply_control_line("", self.state))
        self.assertFalse(apply_control_line("TX:", self.state))
        self.assertFalse(self.state["tx_active"])
        self.assertFalse(self.state["tx_resync"])

    def test_case_and_whitespace_tolerated(self):
        self.assertTrue(apply_control_line("  tx:1 \r\n", self.state))
        self.assertTrue(self.state["tx_active"])


class FakeStdin:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        return None


class FakeProc:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.stdin = FakeStdin()


class NotifyScopePipeTxTests(unittest.IsolatedAsyncioTestCase):
    def _producer(self, proc=None, last_tx=None) -> FT710ScopeProducer:
        p = FT710ScopeProducer(Path("."), scope=None)
        p._proc = proc
        p._last_tx = last_tx
        return p

    async def test_writes_tx1_on_transition_to_tx(self):
        proc = FakeProc()
        p = self._producer(proc, last_tx=False)
        p.notify_tx(True)
        await asyncio.sleep(0)  # let the scheduled drain run
        self.assertEqual(bytes(proc.stdin.buf), b"TX:1\n")

    async def test_writes_tx0_on_transition_to_rx(self):
        proc = FakeProc()
        p = self._producer(proc, last_tx=True)
        p.notify_tx(False)
        await asyncio.sleep(0)
        self.assertEqual(bytes(proc.stdin.buf), b"TX:0\n")

    async def test_unchanged_state_writes_nothing(self):
        proc = FakeProc()
        p = self._producer(proc, last_tx=False)
        p.notify_tx(False)
        await asyncio.sleep(0)
        self.assertEqual(bytes(proc.stdin.buf), b"")

    async def test_force_resends_current_state(self):
        proc = FakeProc()
        p = self._producer(proc, last_tx=True)
        p.notify_tx(True, force=True)
        await asyncio.sleep(0)
        self.assertEqual(bytes(proc.stdin.buf), b"TX:1\n")

    async def test_dead_pipe_is_not_written(self):
        proc = FakeProc(returncode=1)
        p = self._producer(proc, last_tx=False)
        p.notify_tx(True)
        await asyncio.sleep(0)
        self.assertEqual(bytes(proc.stdin.buf), b"")


class TerminateProcessTreeTests(unittest.TestCase):
    def test_windows_uses_taskkill_tree(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)

            class Result:
                returncode = 0

            return Result()

        with (
            patch.object(scope_producer.sys, "platform", "win32"),
            patch.object(scope_producer.subprocess, "run", fake_run),
        ):
            terminate_process_tree_sync(4321)
        self.assertEqual(calls, [["taskkill", "/PID", "4321", "/T", "/F"]])

    def test_posix_falls_back_to_sigterm(self):
        killed = []

        def fake_kill(pid, sig):
            killed.append((pid, sig))

        with (
            patch.object(scope_producer.sys, "platform", "darwin"),
            patch.object(scope_producer.os, "kill", fake_kill),
        ):
            terminate_process_tree_sync(4321)
        self.assertEqual(killed, [(4321, 15)])


if __name__ == "__main__":
    unittest.main()
