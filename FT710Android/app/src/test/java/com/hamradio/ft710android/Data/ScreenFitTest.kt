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

    /**
     * 频谱高度是**确定值**，不再自动吃掉屏幕余量。
     *
     * 2026-10-07 用户看过荣耀档位截图后要求"瀑布区再减小 30%"：
     * 旧的 bonus 机制会把省下的空间立刻灌回频谱（荣耀上 118+80=198dp），
     * 只改比例是无效的，所以整个机制被移除。现在紧凑档 = 133dp、标准档 = 166dp，
     * 荣耀上相当于 198 → 133（**-33%**）。剩余高度就是留白（内容顶对齐，不是被裁切）。
     */
    @Test fun `spectrum height is deterministic, with no leftover refill`() {
        // 紧凑档：int((40+110) * 0.78) + 16 = 117 + 16
        assertEquals(133, ScreenFit.spectrumHeight(compact = true, fftH = 40, wfH = 110))
        // 标准档（平板）：150 * 1.0 + 16
        assertEquals(166, ScreenFit.spectrumHeight(compact = false, fftH = 40, wfH = 110))
        // 预算里的频谱项就是它本身，没有任何附加
        mainland.forEach { (name, _, h) ->
            val b = ScreenFit.budget(360, h, 40, 110, true, true, true)
            assertEquals("$name：预算频谱项应等于确定高度",
                ScreenFit.spectrumHeight(ScreenFit.isCompact(h), 40, 110), b.spectrum)
        }
        // 用户在设置里调 Spec H / WF H 仍然生效（比例不变，绝对高度跟着走）
        assertTrue(ScreenFit.spectrumHeight(true, 20, 200) > ScreenFit.spectrumHeight(true, 40, 110))
    }

    @Test fun `every profile fits with headroom now that the spectrum is shorter`() {
        mainland.forEach { (name, w, h) ->
            val b = ScreenFit.budget(w, h, 40, 110, hasAtr = true, hasVdId = true, bottomBarVisible = true)
            assertTrue("$name：总高 ${b.total} > 可用 $h", b.total <= h)
            // 支持下限（360×728）也应还有一点余量，不能刚好顶满
            if (h == 728) assertTrue("支持下限余量过小：${h - b.total}", h - b.total >= 8)
        }
    }

    @Test fun `meter width tracks screen width within bounds`() {
        assertEquals(132, ScreenFit.meterWidth(300))    // 下限
        assertEquals(151, ScreenFit.meterWidth(360))
        assertEquals(183, ScreenFit.meterWidth(432))
        assertEquals(240, ScreenFit.meterWidth(900))    // 上限
    }

    /**
     * 状态行 v1.1.27 起住在**显示屏内部**（主频上方），可用宽度是显示屏内容宽
     * （360dp 屏 → 167dp），不是页宽 344dp —— 所以折行判断必须用 [ScreenFit.bezelContentWidth]。
     */
    @Test fun `status row fits one line inside the display bezel on every supported profile`() {
        mainland.forEach { (name, w, _) ->
            assertTrue("$name 显示屏内容宽只有 ${ScreenFit.bezelContentWidth(w)}dp，太窄",
                ScreenFit.bezelContentWidth(w) >= 150)
            assertEquals("$name 常规状态应一行", 1, ScreenFit.statusLines(w, 0))
            assertEquals("$name 叠加两个条件芯片仍应一行", 1, ScreenFit.statusLines(w, 2))
        }
        // 内容估宽：RX/TX 22 + 速率 41 + 状态点 8 + 2×间距 ≈ 85
        assertEquals(85, ScreenFit.STATUS_CONTENT_W)
    }

    @Test fun `status row wraps instead of clipping on absurdly narrow screens`() {
        // 不支持的极窄屏（S 表宽度触到 132dp 下限后，显示屏内容宽跌破 85dp）：
        // 折行而不是裁切；顶栏会按行数长高（headerHeight(statusLines)）
        assertTrue("250dp 应折行，实际内容宽 ${ScreenFit.bezelContentWidth(250)}dp",
            ScreenFit.statusLines(250, 0) >= 2)
        // 280dp 仍是一行（内容宽 106dp ≥ 85dp）——折行只发生在远低于支持下限时
        assertEquals(1, ScreenFit.statusLines(280, 0))
        val one = ScreenFit.headerHeight(true, 360, 1)
        val two = ScreenFit.headerHeight(true, 360, 2)
        assertEquals("折一行应正好长高一行 + 间距",
            one + ScreenFit.statusItemHeight(true) + 2, two)
    }

    @Test fun `memory grid is a single row of six in compact mode`() {
        assertEquals(6, ScreenFit.memoryColumns(true))
        assertEquals(1, ScreenFit.memoryRows(true))
        assertEquals(3, ScreenFit.memoryColumns(false))
        assertEquals(2, ScreenFit.memoryRows(false))
    }

    // ── 记忆格文字：紧凑档 6 格一行，每格只有约 47dp，字号必须按宽度反推 ──

    @Test fun `memory cell width matches what six cells plus the manage button leave`() {
        val w360 = ScreenFit.memoryCellWidth(360, true)
        // 360dp 屏：内容 344 − 卡片左右内边距 16 − ⋯ 22 − 6 个间隙 24 = 282，÷6
        assertEquals(47f, w360, 0.6f)
        assertTrue("标准档 3 格应宽得多", ScreenFit.memoryCellWidth(800, false) > 200f)
        assertTrue("屏越宽格子越宽", ScreenFit.memoryCellWidth(432, true) > w360)
    }

    /**
     * 2026-10-07 起记忆格**只显示频率**（用户要求：标签太挤会变形）。
     * 单行之后空间宽松，字号上限从 7.5sp 放开到 11sp（标准档 13sp）。
     */
    @Test fun `frequency text takes the full size when short and shrinks when long`() {
        val w = ScreenFit.memoryCellWidth(360, true)
        // 空槽占位 "M1"：2 字符 → 47/(2×0.62)=37.9 → 上限 11sp
        assertEquals(11f, ScreenFit.memoryFreqFontSize("M1", w, true), 0.001f)
        // "7.117"：5 字符 → 47/(5×0.62)=15.2 → 上限 11sp
        assertEquals(11f, ScreenFit.memoryFreqFontSize("7.117", w, true), 0.001f)
        // "438.500"（UHF 7 字符）：47/(7×0.62)=10.8 → 真的缩一档
        assertEquals(10.84f, ScreenFit.memoryFreqFontSize("438.500", w, true), 0.05f)
        // "1234.567"（8 字符，极端）：47/(8×0.62)=9.5
        assertTrue(ScreenFit.memoryFreqFontSize("1234.567", w, true) < 10f)
        // 标准档格子宽得多 → 一律拿满上限 13sp
        assertEquals(13f, ScreenFit.memoryFreqFontSize("438.500", ScreenFit.memoryCellWidth(800, false), false), 0.001f)
    }

    @Test fun `every memory text either fits its cell or falls back to the ellipsis floor`() {
        // 这是"不变形"的真正保证：要么放得下，要么已经到下限（此时 UI 用 Ellipsis 收尾，
        // 且 maxLines=1 + softWrap=false 保证绝不换行把固定高度的格子顶变形）
        val texts = listOf(
            "M1", "M6", "7.117", "14.270", "438.500", "1234.567", "0.137", "50.313",
        )
        listOf(360 to true, 384 to true, 411 to true, 432 to true, 800 to false).forEach { (w, compact) ->
            val cell = ScreenFit.memoryCellWidth(w, compact)
            texts.forEach { text ->
                val fs = ScreenFit.memoryFreqFontSize(text, cell, compact)
                val need = text.length * 0.62f * fs          // 等宽数字/字母，恒半角
                assertTrue(
                    "${w}dp/$text：${fs}sp 需要 ${"%.1f".format(need)}dp > 格宽 ${"%.1f".format(cell)}dp",
                    need <= cell + 0.5f || fs <= ScreenFit.MEMORY_LABEL_MIN_SP + 0.001f,
                )
                assertTrue("字号不得低于下限", fs >= ScreenFit.MEMORY_LABEL_MIN_SP)
            }
        }
    }
}