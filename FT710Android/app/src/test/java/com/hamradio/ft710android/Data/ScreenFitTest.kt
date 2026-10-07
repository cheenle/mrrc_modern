package com.hamradio.ft710android.Data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 屏幕适配预算测试：用**真机分辨率档位**验证"主屏一屏放得下"，
 * 而不是靠眼睛看截图。
 *
 * 说明：`LocalConfiguration.screenHeightDp` 给的是**已扣除状态栏/导航栏**的可用高度，
 * 所以下面的档位直接按"可用高度"填（手势导航约扣 24+28dp，三键导航约扣 48+28dp）。
 */
class ScreenFitTest {
    /** 名称, 可用宽 dp, 可用高 dp。 */
    private val mainland = listOf(
        Triple("小米/红米 6.5\" 1080×2400 手势导航", 360, 744),
        Triple("小米/红米 6.5\" 三键导航", 360, 728),
        Triple("荣耀 6.7\" 1200×2664", 400, 832),
        Triple("华为 Mate 6.7\" 1260×2720", 432, 880),
        Triple("小米12 / Pixel 6.1\" 1080×2400@2.75", 393, 817),
        Triple("三星 / Pixel 412×892", 412, 892),
    )

    private fun fits(w: Int, h: Int, atr: Boolean = true, extraChips: Int = 0) =
        ScreenFit.budget(w, h, fftH = 40, wfH = 110, hasAtr = atr, hasVdId = true,
            bottomBarVisible = true, extraStatusChips = extraChips).total <= h

    @Test fun `mainstream mainland phones fit the whole main screen without scrolling`() {
        mainland.forEach { (name, w, h) ->
            val b = ScreenFit.budget(w, h, 40, 110, hasAtr = true, hasVdId = true, bottomBarVisible = true)
            assertTrue("$name：总高 ${b.total}dp > 可用 ${h}dp（滚动区 ${b.scrollArea} + 底栏 ${b.bottomBar}）",
                b.total <= h)
        }
    }

    @Test fun `still fits with the extra conditional status chips`() {
        // 只读登录 + 无声告警同时出现时状态行会变宽，窄屏可能折行
        mainland.forEach { (name, w, h) ->
            assertTrue("$name（+2 条件芯片）放不下", fits(w, h, extraChips = 2))
        }
    }

    @Test fun `still fits without the ATR row`() {
        mainland.forEach { (name, w, h) -> assertTrue("$name（无 ATR）放不下", fits(w, h, atr = false)) }
    }

    @Test fun `phones use the compact profile and tablets do not`() {
        assertTrue(ScreenFit.isCompact(744))
        assertTrue(ScreenFit.isCompact(892))
        assertTrue(!ScreenFit.isCompact(1200))
        assertTrue(ScreenFit.budget(800, 1200, 40, 110, true, true, true).total <= 1200)
    }

    @Test fun `compact profile really is shorter than the standard one`() {
        val compact = ScreenFit.budget(360, 744, 40, 110, true, true, true)
        // 同样内容按标准档算（假装屏高够）
        val std = ScreenFit.budget(360, 1200, 40, 110, true, true, true)
        assertTrue("紧凑档 ${compact.total} 应明显小于标准档 ${std.total}", compact.total + 100 < std.total)
        // 分区标题已整体移除：预算里不含 label 项（守卫见 MainScreenComposeTest 的标题数断言）
    }

    /**
     * 支持下限 = 360×728（5.5" 直板机 + 三键导航）。
     * 5" 及以下（360×584）不在适配范围（用户 2026-10-06 明确），滚动兜底即可。
     */
    @Test fun `the supported floor is a 360x728 handset and it still fits`() {
        val b = ScreenFit.budget(360, 728, 40, 110, hasAtr = true, hasVdId = true, bottomBarVisible = true)
        assertTrue("支持下限 360×728 放不下：${b.total}", b.total <= 728)
        assertEquals(728, ScreenFit.SUPPORTED_MIN_HEIGHT_DP)
    }

    @Test fun `leftover height goes to the spectrum instead of dead space`() {
        // 华为 Mate 432×880：紧凑档固定预算后还剩很多 → 全部（封顶 80dp）给频谱
        val mate = ScreenFit.budget(432, 880, 40, 110, true, true, true)
        assertEquals(ScreenFit.SPECTRUM_BONUS_CAP_DP, mate.spectrumBonus)
        assertTrue("给了余量后仍必须一屏：${mate.total} vs 880", mate.total <= 880)
        // 小米/红米 360×728（三键导航）是支持下限：bonus 未被封顶时，
        // 剩下的空白应**正好等于安全边界**（既填满一屏，又留一点吸收模型误差）
        val tight = ScreenFit.budget(360, 728, 40, 110, true, true, true)
        assertTrue("紧机型 bonus 应很小：${tight.spectrumBonus}", tight.spectrumBonus <= 30)
        assertTrue("必须一屏：${tight.total} vs 728", tight.total <= 728)
        assertEquals("未被封顶时剩余空白 = 安全边界",
            ScreenFit.BONUS_SAFETY_DP, 728 - tight.total)
    }

    @Test fun `spectrum bonus is clamped and never makes the screen scroll`() {
        assertEquals(0, ScreenFit.spectrumBonus(availHeightDp = 700, fixedTotalDp = 720))
        assertEquals(0, ScreenFit.spectrumBonus(availHeightDp = 720, fixedTotalDp = 720))
        // 余量不足安全边界时给 0（宁可留白也不顶出滚动条）
        assertEquals(0, ScreenFit.spectrumBonus(availHeightDp = 729, fixedTotalDp = 720))
        assertEquals(10, ScreenFit.spectrumBonus(availHeightDp = 740, fixedTotalDp = 720))
        assertEquals(ScreenFit.SPECTRUM_BONUS_CAP_DP,
            ScreenFit.spectrumBonus(availHeightDp = 1200, fixedTotalDp = 700))
        // 所有主流档位：加了 bonus 之后总高仍 ≤ 可用高度
        mainland.forEach { (_, w, h) ->
            val b = ScreenFit.budget(w, h, 40, 110, true, true, true)
            assertTrue("$w×$h 加余量后超出：${b.total}", b.total <= h)
        }
    }

    @Test fun `meter width tracks screen width within bounds`() {
        assertEquals(132, ScreenFit.meterWidth(300))    // 下限
        assertEquals(151, ScreenFit.meterWidth(360))
        assertEquals(183, ScreenFit.meterWidth(432))
        assertEquals(240, ScreenFit.meterWidth(900))    // 上限
    }

    @Test fun `status row is one line on mainstream widths`() {
        assertEquals(1, ScreenFit.statusLines(360, 0))
        assertEquals(1, ScreenFit.statusLines(412, 0))
        // 320dp 窄屏：可用 304dp 仍装得下 269dp 内容 → 一行
        assertEquals(1, ScreenFit.statusLines(320, 0))
        // 加两个条件芯片（只读 + 无声）后，360dp 仍是一行
        assertEquals(1, ScreenFit.statusLines(360, 2))
        // v1.1.21 瘦身（去 RTT·J / Serial 文字 / 录音二字）后内容宽 210dp：
        // 只有窄到 226dp 以下才折行（没有这种真机；FlowRow 兜底不裁切）
        assertEquals(2, ScreenFit.statusLines(220, 0))
    }

    @Test fun `memory grid is a single row of six in compact mode`() {
        assertEquals(6, ScreenFit.memoryColumns(true))
        assertEquals(1, ScreenFit.memoryRows(true))
        assertEquals(3, ScreenFit.memoryColumns(false))
        assertEquals(2, ScreenFit.memoryRows(false))
    }
}
