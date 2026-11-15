"""`YaesuCatController` running over the SCU-LAN10 tunnel — end to end.

This is the closest thing to hardware we can run offline: the real controller
(its write path, its 10 ms `in_waiting` read loop, its AI-frame prefix filter)
against the scripted unit, with the transport in between.  Everything below the
protocol bytes is the same code the box runs against a real FTDX101.

Two properties matter most and both are invisible in a unit test of the pieces:

* a **set** (write-only in Yaesu CAT) must not need an answer, and a **query**
  must get its answer back through a thread → loop → thread hand-off;
* a **lost session** must read as a transient serial timeout — not as a fatal
  error that latches `serial_connected=False` and tells the operator to check a
  USB cable that does not exist on this wiring.
"""
import asyncio
import unittest

from backends.yaesu.cat_core import YaesuCatController
from backends.yaesu.scu_lan import protocol as proto
from backends.yaesu.scu_lan.client import ScuLanClient
from backends.yaesu.scu_lan.transport import ScuLanCatTransport
from backends.yaesu.yaesu_profiles import get_profile
from tests.test_scu_lan_client import FakeScuLan


class CatCoreOverTunnelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fake = FakeScuLan()
        self.base_port = await self.fake.start()
        self.client = ScuLanClient("127.0.0.1", "defaultuser", "defaultuser",
                                   base_port=self.base_port, auth_timeout=2.0,
                                   keepalive_interval=0.2, link_timeout=5.0,
                                   setup_step_s=0.01)
        self.transport = ScuLanCatTransport(self.client, read_timeout=0.1)
        self.cat = YaesuCatController("tunnel", 38400,
                                      profile=get_profile("ftdx101mp"),
                                      transport=self.transport)
        await self.client.start()
        self.assertTrue(await self.client.wait_until_connected(3.0))
        self.assertTrue(await self.cat.connect())
        self.addCleanup(self.cleanup)

    async def cleanup(self) -> None:
        await self.cat.disconnect()
        await self.client.stop()
        await self.fake.stop()

    async def test_adopting_the_tunnel_does_not_open_a_serial_port(self):
        self.assertIs(self.cat._ser, self.transport)
        self.assertTrue(self.cat._ser.is_open)

    async def test_a_query_gets_its_answer_through_the_seam(self):
        answer = await self.cat.query("FA", timeout=2.0)
        # `send_command` hands back the answer without its terminator.
        self.assertEqual(answer, "FA007007000")

    async def test_an_ai_frame_does_not_masquerade_as_the_answer(self):
        """The radio pushes `RM1;…;` unasked — the prefix filter must keep it out.

        The scripted unit pushes one after every `FA;` answer, so this is not a
        hypothetical: if the filter were missing, the meter frame would come
        back as the frequency.
        """
        answer = await self.cat.query("FA", timeout=2.0)
        # `send_command` hands back the answer without its terminator.
        self.assertEqual(answer, "FA007007000")

    async def test_a_lost_session_reads_as_a_timeout_and_never_latches(self):
        """The failure has to look transient, or the UI blames a USB cable."""
        await self.fake.stop()                     # the unit disappears
        answer = await self.cat.query("FA", timeout=0.6)
        self.assertIsNone(answer, "no answer, and no exception")
        self.assertTrue(self.cat._connected,
                        "a transient failure must not latch the controller")

    async def test_the_session_coming_back_needs_no_reopen(self):
        """The client rebuilds the session under the controller's feet."""
        await self.fake.stop()
        await asyncio.sleep(0.2)
        self.fake = FakeScuLan()
        self.base_port = await self.fake.start()
        self.client._sockets.targets = {
            "ctrl": ("127.0.0.1", self.base_port),
            "cat": ("127.0.0.1", self.base_port + 1),
            "audio": ("127.0.0.1", self.base_port + 2),
            "scope": ("127.0.0.1", self.base_port + 3),
        }
        self.client._peer_ip = None
        self.assertTrue(await self.client.wait_until_connected(12.0))
        self.assertEqual(await self.cat.query("FA", timeout=2.0), "FA007007000")


if __name__ == "__main__":
    unittest.main()
