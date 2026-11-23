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


class LoopShapeTests(unittest.TestCase):
    """循环的**形状**（一 tick 带多少音频）必须能被看出来。

    2026-11-23：在一条健康 LAN 链路上量到——盒子每 ~100 ms 突发送 5 帧，平均
    仍是 50 帧/秒。所有计数器都是平均，所以这个失真在日志里完全不可见；发现它靠
    的是一台客户端在节点上量到达节奏。这条线把它变成一行 INFORMATION。
    """

    def test_a_realtime_loop_says_so(self):
        line = server._rx_loop_shape(3000, 60.0, 60.0, 1.2, 8.0, 3.0)
        self.assertIn("50.0 ticks/s", line)
        self.assertIn("20 ms of audio per tick", line)
        self.assertIn("real-time", line)

    def test_a_quantized_loop_is_named(self):
        # 现场那一份：10 tick/s、每 tick 100 ms、读占 80 ms。
        line = server._rx_loop_shape(600, 60.0, 60.0, 80.0, 95.0, 100.0)
        self.assertIn("10.0 ticks/s", line)
        self.assertIn("100 ms of audio per tick", line)
        self.assertIn("QUANTIZED burst", line)

    def test_a_backlog_is_named_too(self):
        # 一 tick 半秒音频：循环已经比电台慢了，不是拍而是积压。
        line = server._rx_loop_shape(60, 60.0, 30.0, 300.0, 400.0, 500.0)
        self.assertIn("500 ms of audio per tick", line)
        self.assertIn("backlog", line)

    def test_it_never_divides_by_zero(self):
        self.assertIn("0 ms of audio per tick", server._rx_loop_shape(0, 0.0, 0.0, 0, 0, 0))

    def test_the_loop_reports_its_shape(self):
        import inspect
        src = inspect.getsource(server._audio_rx_loop)
        self.assertIn('logger.info("%s", _rx_loop_shape(', src,
                      "形状行没接线，下次又只能靠客户端量")
        self.assertGreater(src.index('logger.info("%s", _rx_loop_shape('),
                           src.index("_drain_rx_chunks(audio.read_rx_chunk)"),
                           "读的计时必须包住真实的那次读（"
                           "报告点得在它后面）")


if __name__ == "__main__":
    unittest.main()
