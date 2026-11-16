"""阻塞活不许待在事件循环里：每一次停顿都是操作员听到的一个空洞。

2026-11-16 现场（W103D + SCU-LAN10 + 手机）：`Event loop stall` 106–1097 ms，
约每 10 分钟一次；把日志上下文拉出来，两次都紧挨着 `/api/devices`（PortAudio
初始化 + ALSA/JACK 枚举）与 `mrrc.remotemic` 的 5 秒 D-Bus 探测。两条都在这里
钉住：它们必须在 worker 线程里跑。
"""
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class LoopBlockingTests(unittest.TestCase):
    def read(self, rel):
        return (REPO / rel).read_text(encoding="utf-8")

    def test_the_bluetooth_remote_probe_runs_off_the_loop(self):
        source = self.read("remote_mic.py")
        self.assertIn("asyncio.to_thread(_probe_remote", source,
                      "D-Bus GetManagedObjects 每 5 秒一次，留在循环里就是音频空洞")
        self.assertIn("def _probe_remote(", source)

    def test_the_device_enumeration_runs_off_the_loop(self):
        source = self.read("server.py")
        self.assertIn("await asyncio.to_thread(_list_devices)", source,
                      "PortAudio 初始化是阻塞的，别在事件循环里做")

    def test_the_retry_is_not_a_five_second_storm(self):
        source = self.read("remote_mic.py")
        self.assertNotIn("await asyncio.sleep(5)\n                continue", source)


if __name__ == "__main__":
    unittest.main()
