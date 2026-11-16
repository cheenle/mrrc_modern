"""RX 音频循环的节拍与追平：一个慢 tick 不许把音频丢掉。

2026-11-16 现场（W103D + SCU-LAN10，手机经公网入口听）：客户端只收到
**37.9 帧/秒**，而单元产出 50 帧/秒 —— 差的 24% 被 `read_rx_chunk` 的
「读时裁到 5 块」丢掉了，听感就是持续卡顿。两条断言在这里钉住：

* `_drain_rx_chunks` 一次把积压读光（有上限），不是一次只取一块；
* 循环按墙钟排节拍（`interval - 已用时`），不是「干完活再固定睡 20 ms」。
"""
import time
import unittest

import server


class DrainTests(unittest.TestCase):
    def queue_of(self, blocks):
        q = list(blocks)
        def read():
            return q.pop(0) if q else None
        return q, read

    def test_one_queued_block_comes_back_alone(self):
        _q, read = self.queue_of([b"a" * 960])
        self.assertEqual(server._drain_rx_chunks(read), b"a" * 960)

    def test_an_empty_queue_is_none(self):
        _q, read = self.queue_of([])
        self.assertIsNone(server._drain_rx_chunks(read))

    def test_a_backlog_is_taken_in_order_and_up_to_the_cap(self):
        blocks = [bytes([i]) * 10 for i in range(8)]
        q, read = self.queue_of(blocks)
        out = server._drain_rx_chunks(read)
        self.assertEqual(out, b"".join(blocks[:server.RX_DRAIN_MAX_CHUNKS]))
        self.assertEqual(len(q), 8 - server.RX_DRAIN_MAX_CHUNKS,
                         "超出上限的部分留给下一个 tick，不能一次发一大坨")

    def test_the_cap_is_a_short_burst_not_a_second_of_audio(self):
        self.assertLessEqual(server.RX_DRAIN_MAX_CHUNKS * 20, 100,
                             "追平上限超过 100 ms 就是又加了一层延迟")


class PacingTests(unittest.TestCase):
    def test_the_loop_sleeps_to_the_next_tick_not_a_fixed_tail(self):
        import inspect
        source = inspect.getsource(server._audio_rx_loop)
        self.assertIn("interval - (time.monotonic() - _tick_started)", source,
                      "节拍要按墙钟扣掉已用时间，否则周期变成 work+20ms，比实时慢")
        self.assertNotIn("await asyncio.sleep(interval)\n", source.replace(" ", " "),
                         "尾部固定 sleep 会把慢 tick 拖成丢音频")

    def test_the_loop_reads_through_the_draining_helper(self):
        import inspect
        source = inspect.getsource(server._audio_rx_loop)
        self.assertIn("_drain_rx_chunks(audio.read_rx_chunk)", source)
        self.assertNotIn("pcm = audio.read_rx_chunk()", source)


if __name__ == "__main__":
    unittest.main()
