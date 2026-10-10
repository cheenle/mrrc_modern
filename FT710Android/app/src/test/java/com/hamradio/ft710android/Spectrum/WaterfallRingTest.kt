package com.hamradio.ft710android.Spectrum

import org.junit.Assert.assertEquals
import org.junit.Test

class WaterfallRingTest {
    private fun row(v: Int) = IntArray(4) { v }

    @Test fun `first frame counts every row as new`() {
        val rows = listOf(row(1), row(2), row(3))
        assertEquals(3, WaterfallRing.newRowCount(null, rows))
        // prev 为空 = 一帧都还没画过 → 全部算新行（换 LUT 后重建也是这条路径）
        assertEquals(3, WaterfallRing.newRowCount(emptyList(), rows))
    }

    @Test fun `unchanged list has no new rows`() {
        val rows = listOf(row(1), row(2), row(3))
        assertEquals(0, WaterfallRing.newRowCount(rows, rows))
        // 内容相同但是不同对象 → 也算新行（引用相等语义）
        assertEquals(3, WaterfallRing.newRowCount(rows, listOf(row(1), row(2), row(3))))
    }

    @Test fun `deque shift by one counts exactly one new row`() {
        // SpectrumProcessor: addLast(new) + removeFirst() → 列表整体左移一格
        val a = row(1); val b = row(2); val c = row(3); val d = row(4)
        val prev = listOf(a, b, c)
        val now = listOf(b, c, d)
        assertEquals(1, WaterfallRing.newRowCount(prev, now))
    }

    @Test fun `batched frames count every appended row`() {
        val a = row(1); val b = row(2); val c = row(3); val d = row(4); val e = row(5)
        assertEquals(2, WaterfallRing.newRowCount(listOf(a, b, c), listOf(c, d, e)))
        assertEquals(3, WaterfallRing.newRowCount(listOf(a, b), listOf(a, b, c, d, e)))
    }

    @Test fun `first ring index is the oldest row while filling and the write head once full`() {
        // 未满：head=3, filled=3 → 最旧是 0
        assertEquals(0, WaterfallRing.firstRingIndex(head = 3, filled = 3, capacity = 120))
        // 已满：head 就是下一个要被覆盖的格子 = 最旧
        assertEquals(57, WaterfallRing.firstRingIndex(head = 57, filled = 120, capacity = 120))
        // 回绕未满：head=2, filled=5, capacity=4 → 只有 4 格，最旧 = (2-4+4)%4 = 2
        assertEquals(2, WaterfallRing.firstRingIndex(head = 2, filled = 4, capacity = 4))
    }

    @Test fun `segments split at the ring wrap and always sum to filled`() {
        // 不回绕：first=0, filled=120, cap=120 → (120, 0)
        assertEquals(120 to 0, WaterfallRing.segments(first = 0, filled = 120, capacity = 120))
        // 回绕：first=100, filled=120, cap=120 → 尾段 20 + 回绕段 100
        assertEquals(20 to 100, WaterfallRing.segments(first = 100, filled = 120, capacity = 120))
        // 未满且不回绕：first=0, filled=30 → (30, 0)
        assertEquals(30 to 0, WaterfallRing.segments(first = 0, filled = 30, capacity = 120))
        // 未满且回绕：head=5, filled=120(cap=120) 之外的情况 → first=110, filled=15, cap=120
        assertEquals(10 to 5, WaterfallRing.segments(first = 110, filled = 15, capacity = 120))
    }

    @Test fun `a full scroll cycle visits every slot exactly once`() {
        val cap = 120
        var head = 0
        val writes = IntArray(cap)
        repeat(cap * 3) {
            writes[head]++
            head = (head + 1) % cap
        }
        assertEquals(3, writes.min())
        assertEquals(3, writes.max())
        // 写满后 first 恒等于 head（最旧正是下一个要被覆盖的）
        assertEquals(head, WaterfallRing.firstRingIndex(head, cap, cap))
    }
}
