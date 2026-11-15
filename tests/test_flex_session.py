"""SmartSDR session tests (P2, design §4.3 / §7.3 / §9.1).

Real loopback sockets against ``tests/flex_fake_radio.py``: what is asserted is
the wire behaviour — handshake order, the one-byte UDP port registration,
reply matching, timeouts, ownership filtering and the interlock state machine.
"""

import asyncio
import unittest

from backends.flex import protocol
from backends.flex.session import (
    INTERLOCK_STATES, SmartSDRSession, SessionClosed, SessionTimeout,
    next_backoff,
)

from tests.flex_fake_radio import FakeRadio


class SessionCase(unittest.IsolatedAsyncioTestCase):
    """Base: a connected session against a fake radio."""

    async def asyncSetUp(self):
        self.radio = await FakeRadio.create()
        self.session = SmartSDRSession("127.0.0.1", self.radio.port,
                                       program="MRRC", station="Box")
        self.statuses = []
        self.session._on_status = self.statuses.append
        self.transitions = []
        self.session._on_interlock = self.transitions.append
        self.assertTrue(await self.session.connect())

    async def asyncTearDown(self):
        await self.session.close()
        self.radio.close()


class HandshakeTests(SessionCase):
    async def test_prologue_is_parsed(self):
        self.assertEqual(self.session.version, "1.4.0.0")
        self.assertEqual(self.session.handle, 0xDE300001)

    async def test_subscriptions_and_client_identity_come_in_order(self):
        order = [c.split()[0] + " " + (c.split()[1] if len(c.split()) > 1 else "")
                 for c in self.radio.commands[:9]]
        self.assertEqual(order[0], "sub slice")
        self.assertEqual(order[1], "sub pan")
        self.assertEqual(order[2], "sub tx")
        self.assertEqual(order[3], "sub atu")
        self.assertEqual(order[4], "sub meter")
        self.assertEqual(order[5], "sub audio")
        self.assertEqual(self.radio.commands[6], "client gui")
        self.assertEqual(self.radio.commands[7], "client program MRRC")
        self.assertEqual(self.radio.commands[8], "client station Box")
        self.assertTrue(self.radio.commands[9].startswith("client udpport "))

    async def test_udp_port_is_registered_by_one_byte_packet(self):
        """The byte is the registration; ``client udpport`` only trails it."""
        self.assertEqual(self.radio.udp_payloads[0], b"\x00")
        self.assertIsNotNone(self.radio.udp_from)
        assert self.radio.udp_from is not None
        self.assertEqual(self.radio.udp_from[1], self.session.local_udp_port)
        self.assertTrue(self.session.udp_registered)

    async def test_udpport_error_is_accepted_as_expected(self):
        """A 0x50001000 answer must not abort the handshake."""
        self.assertEqual(self.session.last_error, "")
        self.assertTrue(self.session.connected)


class CommandTests(SessionCase):
    async def test_send_returns_the_body(self):
        reply = await self.session.send("slice list")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.body, "0")

    async def test_error_code_marks_the_command_rejected(self):
        self.radio.set_reply("slice create", code=protocol.ERROR_LICENSE)
        self.assertFalse(await self.session.send_ok("slice create freq=14.074"))

    async def test_sequence_numbers_increase(self):
        first = await self.session.send("slice list")
        second = await self.session.send("slice list")
        self.assertGreater(second.seq, first.seq)

    async def test_timeout_raises_and_is_not_fatal(self):
        self.radio.swallow.add("ping")
        with self.assertRaises(SessionTimeout):
            await self.session.send("ping", timeout=0.1)
        self.radio.swallow.clear()
        self.assertTrue((await self.session.send("slice list")).ok)

    async def test_close_fails_an_outstanding_command(self):
        self.radio.swallow.add("ping")
        task = asyncio.create_task(self.session.send("ping", timeout=5.0))
        await asyncio.sleep(0.05)
        await self.session.close()
        with self.assertRaises(SessionClosed):
            await task


class StatusTests(SessionCase):
    async def test_status_lines_reach_the_callback(self):
        await self.radio.push_status("slice 0 RF_frequency=14.074000 mode=USB")
        for _ in range(50):
            if self.statuses:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.statuses)
        status = self.statuses[-1]
        self.assertEqual(status.obj, "slice 0")
        self.assertEqual(status.get("mode"), "USB")

    async def test_partial_lines_are_reassembled(self):
        await self.radio.push_raw(b"S00000001|slice 0 mo")
        await self.radio.push_raw(b"de=LSB\n")
        for _ in range(50):
            if self.statuses:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.statuses[-1].get("mode"), "LSB")

    async def test_interlock_state_and_transitions(self):
        for state in ("READY", "PTT_REQUESTED", "TRANSMITTING"):
            await self.radio.push_status(f"interlock state={state} reason= source=SW")
            for _ in range(50):
                if self.session.interlock_state == state:
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(self.session.interlock_state, "TRANSMITTING")
        self.assertEqual([t.state for t in self.transitions],
                         ["READY", "PTT_REQUESTED", "TRANSMITTING"])

    async def test_unknown_interlock_state_is_still_recorded(self):
        await self.radio.push_status("interlock state=BOGUS")
        for _ in range(50):
            if self.session.interlock_state:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.session.interlock_state, "BOGUS")
        self.assertNotIn("BOGUS", INTERLOCK_STATES)

    async def test_wait_interlock_sees_a_late_transition(self):
        async def later():
            await asyncio.sleep(0.05)
            await self.radio.push_status("interlock state=TRANSMITTING")
        asyncio.create_task(later())
        self.assertTrue(await self.session.wait_interlock({"TRANSMITTING"}, 1.0))

    async def test_wait_interlock_times_out(self):
        self.assertFalse(await self.session.wait_interlock({"TRANSMITTING"}, 0.1))


class OwnershipTests(SessionCase):
    async def test_own_status_is_owned(self):
        status = protocol.parse_status(
            f"S{self.session.handle:08X}|slice 0 client_handle=0x{self.session.handle:08X}")
        self.assertTrue(self.session.owns(status))

    async def test_foreign_status_is_not_owned(self):
        status = protocol.parse_status("S00000000|slice 1 client_handle=0x11111111")
        self.assertFalse(self.session.owns(status))

    async def test_status_without_owner_is_owned_by_default(self):
        """Early status arrives before the handle field does."""
        status = protocol.parse_status("S00000000|slice 0 mode=USB")
        self.assertTrue(self.session.owns(status))


class BackoffTests(unittest.TestCase):
    def test_backoff_doubles_then_caps(self):
        self.assertEqual([next_backoff(n) for n in range(6)],
                         [1.0, 2.0, 4.0, 8.0, 16.0, 30.0])

    def test_negative_and_huge_attempts_are_clamped(self):
        self.assertEqual(next_backoff(-3), 1.0)
        self.assertEqual(next_backoff(99), 30.0)


class ConnectFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_refused_connection_reports_false(self):
        radio = await FakeRadio.create()
        port = radio.port
        radio.close()
        session = SmartSDRSession("127.0.0.1", port)
        self.assertFalse(await session.connect())
        self.assertIn("Error", session.last_error)
        await session.close()

    async def test_empty_host_reports_false(self):
        session = SmartSDRSession("")
        self.assertFalse(await session.connect())
        self.assertEqual(session.last_error, "no radio host configured")


if __name__ == "__main__":
    unittest.main()
