"""End-to-end CAT round trips against a fake Yaesu radio on a pty.

This is the closest thing to a live radio available to this project (spec §8):
the production `YaesuCatController` opens a real device path with pyserial and
talks to a peer that answers the documented ASCII protocol.  Nothing here is
mocked — the framing, the serial round trip and the profile tables are all
exercised together.
"""
import asyncio
import os
import pty
import shutil
import subprocess
import unittest

from backends.yaesu.cat_core import YaesuCatController
from backends.yaesu.yaesu_profiles import get_profile


class FakeYaesuRadio:
    """Minimal ASCII-CAT peer on the master side of a pty pair."""

    def __init__(self, model="ftdx10", freq=14_074_000, mode="2"):
        self.model = model
        self.freq = freq
        self.freq_b = freq - 1000
        self.mode = mode
        self.filter_slot = 2
        self.vfo = "A"
        self.tx = 0
        self.power_w = 25
        self.commands = []
        self._master, self._slave = pty.openpty()
        self.port = os.ttyname(self._slave)
        self._task = None

    async def _serve(self):
        """Answer CAT commands until the peer closes.

        Uses ``loop.add_reader`` rather than an executor thread: a blocking
        ``os.read`` on the pty master keeps a pool thread parked, and
        cancelling the coroutine does not unblock it, so the event loop's
        executor shutdown would hang the whole test run (found while
        executing this plan).
        """
        loop = asyncio.get_running_loop()
        self._buf = bytearray()
        closed = loop.create_future()

        def _on_readable():
            try:
                chunk = os.read(self._master, 256)
            except OSError:
                chunk = b""
            if not chunk:
                if not closed.done():
                    closed.set_result(None)
                return
            self._buf.extend(chunk)
            while b";" in self._buf:
                idx = self._buf.index(b";")
                cmd = bytes(self._buf[:idx]).decode("ascii", "replace")
                del self._buf[:idx + 1]
                answer = self._handle(cmd)
                if answer:
                    os.write(self._master, answer.encode("ascii"))

        loop.add_reader(self._master, _on_readable)
        try:
            await closed
        finally:
            loop.remove_reader(self._master)

    def _handle(self, cmd: str) -> str:
        self.commands.append(cmd)
        p = get_profile(self.model)
        if cmd == "ID":
            return f"ID{p.id_answer or '0000'};"
        if cmd == "FA":
            return f"FA{self.freq:09d};"
        if cmd == "FB":
            return f"FB{self.freq_b:09d};"
        if cmd.startswith("FA") and len(cmd) > 2:
            self.freq = int(cmd[2:]); return ""
        if cmd.startswith("FB") and len(cmd) > 2:
            self.freq_b = int(cmd[2:]); return ""
        if cmd == "VS":
            return f"VS{'0' if self.vfo == 'A' else '1'};"
        if cmd == "MD0":
            return f"MD0{self.mode};"
        if cmd.startswith("MD0"):
            self.mode = cmd[3]
            return ""
        if cmd == "SH0":
            return f"SH00{self.filter_slot:02d};"
        if cmd.startswith("SH00"):
            self.filter_slot = int(cmd[4:])
            return ""
        if cmd == "TX":
            return f"TX{self.tx};"
        if cmd.startswith("TX"):
            self.tx = int(cmd[2])
            return ""
        if cmd == "SM0":
            return "SM0128;"
        if cmd == "PC":
            return f"PC{self.power_w:03d};" if self.model != "ftx1" else f"PC2{self.power_w:03d};"
        if cmd.startswith("PC"):
            self.power_w = int(cmd[2:])
            return ""
        if cmd == "VM":
            return "VM0;"                      # never in memory mode here
        return ""

    async def __aenter__(self):
        self._task = asyncio.create_task(self._serve())
        return self

    async def __aexit__(self, *exc):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        os.close(self._master)
        os.close(self._slave)


class FakeRadioRoundTripTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.radio = await FakeYaesuRadio(model="ftdx10").__aenter__()
        self.ctrl = YaesuCatController(self.radio.port, profile=get_profile("ftdx10"))
        self.assertTrue(await self.ctrl.connect(),
                        "pyserial must be able to open the pty device path")

    async def asyncTearDown(self):
        await self.ctrl.disconnect()
        await self.radio.__aexit__(None, None, None)

    async def test_frequency_round_trip(self):
        self.assertEqual(await self.ctrl.get_frequency("A"), 14_074_000)
        self.assertTrue(await self.ctrl.set_frequency(7_050_000, vfo="A"))
        self.assertEqual(await self.ctrl.get_frequency("A"), 7_050_000)

    async def test_mode_round_trip(self):
        self.assertEqual(await self.ctrl.get_mode(), 0x2)
        await self.ctrl.set_mode(0xC)
        self.assertEqual(await self.ctrl.get_mode(), 0xC)

    async def test_filter_slot_round_trip(self):
        self.assertEqual(await self.ctrl.get_filter_width(), 2)
        await self.ctrl.set_filter_width(3)
        self.assertEqual(await self.ctrl.get_filter_width(), 3)

    async def test_ptt_round_trip(self):
        await self.ctrl.set_ptt(True)
        self.assertEqual(await self.ctrl.get_ptt(), 1)
        await self.ctrl.set_ptt(False)
        self.assertEqual(await self.ctrl.get_ptt(), 0)

    async def test_model_id(self):
        self.assertEqual(await self.ctrl.get_model_id(), "0000")   # no expectation


class FakeFTX1RoundTripTests(unittest.IsolatedAsyncioTestCase):
    async def test_ftx1_power_detection_from_the_answer_shape(self):
        async with FakeYaesuRadio(model="ftx1", freq=50_313_000) as radio:
            ctrl = YaesuCatController(radio.port, profile=get_profile("ftx1"))
            self.assertTrue(await ctrl.connect())
            self.assertEqual(await ctrl.detect_power_config(), ("PC2", 100))
            await ctrl.disconnect()

    async def test_ftx1_identity_is_the_documented_value(self):
        async with FakeYaesuRadio(model="ftx1") as radio:
            ctrl = YaesuCatController(radio.port, profile=get_profile("ftx1"))
            await ctrl.connect()
            self.assertEqual(await ctrl.get_model_id(), "0840")
            await ctrl.disconnect()


HAMLIB_SIM = os.path.expanduser("~/hamlib/Hamlib-4.7.2/simulators/simftdx101")


@unittest.skipUnless(os.access(HAMLIB_SIM, os.X_OK) and shutil.which("socat"),
                     "optional: build ~/hamlib/Hamlib-4.7.2/simulators and install socat "
                     "(see tests/README.md) to run the third-party protocol check")
class HamlibSimulatorPeerTests(unittest.IsolatedAsyncioTestCase):
    """Cross-implementation check: our core against Hamlib's FTDX101 simulator.

    The simulator emulates the radio side of the same ASCII protocol, so this
    catches table mistakes that a self-written fake peer would repeat.
    """

    async def test_frequency_and_mode_against_the_simulator(self):
        proc = subprocess.Popen(
            ["socat", "-d", "-d", "pty,raw,echo=0,link=/tmp/mrrc_sim_rig",
             "pty,raw,echo=0,link=/tmp/mrrc_sim_radio"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            await asyncio.sleep(0.5)
            sim = subprocess.Popen([HAMLIB_SIM, "/tmp/mrrc_sim_radio"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                await asyncio.sleep(0.5)
                ctrl = YaesuCatController("/tmp/mrrc_sim_rig",
                                          profile=get_profile("ftdx101d"))
                self.assertTrue(await ctrl.connect())
                # The simulator boots at 14074000 Hz in mode 0xc (DATA-U).
                self.assertEqual(await ctrl.get_frequency("A"), 14_074_000)
                self.assertEqual(await ctrl.get_mode(), 0xC)   # simulator boots in DATA-U
                await ctrl.set_frequency(21_074_000, vfo="A")
                self.assertEqual(await ctrl.get_frequency("A"), 21_074_000)
                await ctrl.disconnect()
            finally:
                sim.terminate()
        finally:
            proc.terminate()
