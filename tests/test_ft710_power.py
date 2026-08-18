"""Standalone FT-710 power script (ft710_power.py) — power_off success semantics.

Field evidence (2026-08-17): a live FT-710 powered off via PS0 answered
neither PS0 nor a follow-up PS query — the radio goes CAT-silent when it
powers down, exactly like a radio sitting in standby.  The old success
criterion (require a PS0 ACK) therefore reported a *successful* power-off
as 关机失败.  The reference server implementation (server.py /
cat_controller.py set()) treats PS0 as write-only fire-and-forget and
never requires a response.

So the correct criterion is:
  * radio answers PS1 after the PS0s  -> off FAILED (radio still on)
  * radio answers PS0 / goes silent   -> off succeeded (powered down)

Runs without hardware; `open_port` is mocked with a scripted serial.
"""
import unittest
from unittest.mock import patch

import ft710_power


class _ScriptedSerial:
    """Fake pyserial: returns the next scripted byte-string per read_until."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.writes = []

    @property
    def timeout(self):
        return getattr(self, "_timeout", 1.0)

    @timeout.setter
    def timeout(self, value):
        self._timeout = value

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(data)

    def read_until(self, terminator):
        if self._responses:
            return self._responses.pop(0)
        return b""


class _PortFactory:
    """open_port() stand-in: hands out one _ScriptedSerial per call.

    Each positional arg is the response list for one serial connection
    (one entry per CAT command sent on it).
    """

    def __init__(self, *response_lists):
        self._scripts = [list(r) for r in response_lists]
        self.instances = []

    def __call__(self):
        if self._scripts:
            s = _ScriptedSerial(self._scripts.pop(0))
        else:
            s = _ScriptedSerial([])
        self.instances.append(s)
        return s


class PowerOffTests(unittest.TestCase):
    def setUp(self):
        self._sleep_patch = patch.object(ft710_power.time, "sleep")
        self._sleep_patch.start()
        ft710_power._boot_until = 0.0

    def tearDown(self):
        self._sleep_patch.stop()

    def test_off_succeeds_when_radio_goes_silent(self):
        # Real-world case: PS0 gets no ACK (radio powers down), and a
        # follow-up PS query is silent (radio is off).  Must report success.
        factory = _PortFactory([b"", b""], [b""])
        with patch.object(ft710_power, "open_port", factory):
            self.assertTrue(ft710_power.power_off())

    def test_off_succeeds_on_explicit_ps0_ack(self):
        # Radio explicitly ACKs both PS0s.
        factory = _PortFactory([b"PS0;", b"PS0;"], [b""])
        with patch.object(ft710_power, "open_port", factory):
            self.assertTrue(ft710_power.power_off())

    def test_off_fails_when_radio_still_answers_ps1(self):
        # The one real failure mode: PS0 ignored, radio still on.
        factory = _PortFactory([b"", b""], [b"PS1;"])
        with patch.object(ft710_power, "open_port", factory):
            self.assertFalse(ft710_power.power_off())

    def test_off_rejected_during_boot_window(self):
        # PS0 must not be sent inside the post-PS1 protection window.
        ft710_power._boot_until = 1_000_000.0
        factory = _PortFactory([])
        with patch.object(ft710_power, "open_port", factory):
            self.assertFalse(ft710_power.power_off())
        self.assertEqual(factory.instances, [])  # no serial connection opened


if __name__ == "__main__":
    unittest.main()
