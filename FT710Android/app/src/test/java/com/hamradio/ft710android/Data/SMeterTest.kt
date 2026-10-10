package com.hamradio.ft710android.Data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class SMeterTest {
    @Test fun `fraction uses the full 0-255 range`() {
        assertEquals(0f, SMeter.fraction(0), 0.0001f)
        assertEquals(1f, SMeter.fraction(255), 0.0001f)
        assertEquals(0.5098f, SMeter.fraction(130), 0.0005f)   // S9 的刻度
        assertEquals(1f, SMeter.fraction(999))                 // 超界夹住
        assertEquals(0f, SMeter.fraction(-5))
    }

    @Test fun `markers match the web marks array`() {
        assertEquals(
            listOf(0, 12, 27, 40, 55, 65, 80, 95, 112, 130, 150, 172, 190, 220, 240, 255),
            SMeter.MARKERS.toList(),
        )
        assertTrue(SMeter.MARKERS.first() == 0 && SMeter.MARKERS.last() == SMeter.RAW_MAX)
    }

    @Test fun `labels are the eight web ticks`() {
        assertEquals(listOf("S1", "S3", "S5", "S7", "S9", "+20", "+40", "+60"), SMeter.LABELS)
    }

    @Test fun `arc maps ends and centre exactly`() {
        assertEquals(0f, SMeter.arcX(0f, 200f), 0.001f)
        assertEquals(200f, SMeter.arcX(1f, 200f), 0.001f)
        assertEquals(100f, SMeter.arcX(0.5f, 200f), 0.001f)
    }

    @Test fun `arc x is monotonic and y bulges upward`() {
        var prev = -1f
        for (i in 0..100) {
            val x = SMeter.arcX(i / 100f, 300f)
            assertTrue("x must increase at t=$i", x > prev)
            prev = x
        }
        val base = 20f; val ctrl = -6f
        assertTrue(SMeter.arcY(0.5f, base, ctrl) < SMeter.arcY(0.25f, base, ctrl))
        assertEquals(SMeter.arcY(0.2f, base, ctrl), SMeter.arcY(0.8f, base, ctrl), 0.001f)
        assertEquals(base, SMeter.arcY(0f, base, ctrl), 0.001f)
    }

    @Test fun `eight labels sit in order inside the panel`() {
        val xs = SMeter.labelXPositions(100f)
        assertEquals(8, xs.size)
        assertEquals(0f, xs.first(), 0.5f)
        assertTrue("last label near right edge", xs.last() > 92f)
        xs.zipWithNext().forEach { (a, b) -> assertTrue("labels must not cross: $a -> $b", b > a) }
    }

    @Test fun `no two labels overlap at panel width`() {
        // 弧区 = 面板 116dp - padding 6dp - `dB`≈9dp ≈ 101dp
        val xs = SMeter.labelXPositions(101f)
        // 6.5sp 等宽：窄标签 "1".."9" ≈ 4dp，宽标签 "+20/+40/+60" ≈ 12dp
        val widths = listOf(4f, 4f, 4f, 4f, 4f, 12f, 12f, 12f)
        xs.zipWithNext().forEachIndexed { i, (a, b) ->
            val need = (widths[i] + widths[i + 1]) / 2f
            assertTrue("label ${i}->${i + 1} gap ${b - a}dp < needed ${need}dp", b - a >= need)
        }
        // 最后一个标签（+60，刻度 240 不在弧末端）会顶到 `dB`：绘制侧按 w-labelW 夹紧，
        // 这里只要夹紧量在 1dp 内就没有可见位移
        assertTrue(xs.last() + widths.last() / 2f - 101f <= 1f)
    }
}