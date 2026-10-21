"""CAT 波特率自动适配（2026-10-21 现场：FT-891 出厂 4800，配置写的是 38400）。

现场链条（包 `20261021-123324-81ab`）：FT-891 被启动器误写成 ft710 → 之后用户改正机型，
但 `MRRC_BAUD_RATE` 仍是 38400，而电台菜单 05-06 `CAT RATE` 出厂值是 **4800**（Yaesu 手册，
西班牙语/俄语版本同表：4800/9600/19200/38400，默认 4800）。结果是
`No ID response from radio on COM6` —— 电台对错误波特率是**完全沉默**，不是乱码。

所以判据很干净：配置速率下 `ID;` 一个字节都不回，就说明速率不对（或电台没开）。
本文件守住三件事：
 ① 配置的速率能应答时，**不做任何额外探测**（正常路径不能变慢）；
 ② 配置的速率沉默时，按电台菜单的四个值依次试，命中后本会话就用它，并留一条可照做的告警；
 ③ 四个值都不应答时，端口仍按原速率打开（上层继续重连），日志给出菜单/线缆/端口三件事。
"""
import unittest
from unittest import mock

from backends.yaesu import cat_core
from backends.yaesu.cat_core import YaesuCatController
from backends.yaesu.yaesu_profiles import get_profile


class _RateAwareSerial:
    """pyserial stand-in that only answers `ID;` at ``answer_at`` baud."""

    def __init__(self, port, baudrate, answer_at=None, **kwargs):
        self.port, self.baudrate = port, baudrate
        self.answer_at = answer_at
        self._rx = bytearray()
        self.is_open = True

    # -- the pyserial surface the core touches --
    @property
    def in_waiting(self):
        return len(self._rx)

    def read(self, n):
        chunk = bytes(self._rx[:n])
        del self._rx[:n]
        return chunk

    def write(self, data):
        if self.answer_at == self.baudrate and bytes(data).startswith(b"ID"):
            self._rx.extend(b"ID0680;")
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def close(self):
        self.is_open = False


class CatRateAdoptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.profile = get_profile("ft891")
        self.port = "/dev/fake"

    def _install(self, answer_at):
        """Patch pyserial with a fake that answers only at ``answer_at``."""
        opened: list = []

        def factory(port, baudrate, **kwargs):
            opened.append(baudrate)
            return _RateAwareSerial(port, baudrate, answer_at=answer_at)

        patcher = mock.patch.object(cat_core.serial, "Serial", factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        return YaesuCatController(port=self.port, baudrate=38400, profile=self.profile), opened

    async def test_adopts_the_rate_the_radio_answers_at(self):
        ctrl, opened = self._install(answer_at=4800)
        self.assertTrue(await ctrl.connect())
        self.assertEqual(ctrl.baudrate, 4800, "应当采用电台真正应答的速率")
        self.assertEqual(opened, [38400, 4800], "只应在沉默时才换速率")
        await ctrl.disconnect()

    async def test_no_probing_when_the_configured_rate_answers(self):
        ctrl, opened = self._install(answer_at=38400)
        self.assertTrue(await ctrl.connect())
        self.assertEqual(ctrl.baudrate, 38400)
        self.assertEqual(opened, [38400], "正常路径不得多开一次端口")
        await ctrl.disconnect()

    async def test_stays_on_configured_rate_when_nothing_answers(self):
        ctrl, opened = self._install(answer_at=None)
        self.assertTrue(await ctrl.connect())
        self.assertEqual(ctrl.baudrate, 38400, "都不应答时应回到配置值，交给重连逻辑")
        self.assertEqual(opened[0], 38400)
        self.assertEqual(opened[-1], 38400)
        for rate in (4800, 9600, 19200):
            self.assertIn(rate, opened, "四个菜单档位都应试一遍")
        await ctrl.disconnect()


if __name__ == "__main__":
    unittest.main()
