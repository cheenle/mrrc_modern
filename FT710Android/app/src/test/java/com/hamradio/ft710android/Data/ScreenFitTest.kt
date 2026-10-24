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
     * 顶栏那一行（工具 + 全部状态）v1.1.28 起是**全宽独立一行**，位于主频显示屏上方。
     * 它是定高 Row、**不折行** —— 装不下就会被裁切，所以必须断言宽度够。
     * 真挤不下时由"波段·模式"（组内唯一 `weight(fill=false)` 项）出省略号让路。
     */
    @Test fun `the one-line tool row fits every supported profile, even with two extra chips`() {
        mainland.forEach { (name, w, _) ->
            val avail = ScreenFit.contentWidth(w)
            val need = ScreenFit.TOOL_ROW_CONTENT_W
            assertTrue("$name：工具行 $need dp > 可用 $avail dp", need <= avail)
            // 叠加两个条件项（录音时长 + 无声 / 只读 + 无声）后仍要装得下
            val worst = need + 2 * ScreenFit.STATUS_EXTRA_CHIP_W
            assertTrue("$name：叠加两个条件项后 $worst dp > 可用 $avail dp", worst <= avail)
        }
    }

    @Test fun `tool row height is the icon tap target and the display height is separate`() {
        // 行高由图标点击区决定（文字比它矮，垂直居中）
        assertEquals(ScreenFit.headerIconTap(true), ScreenFit.headerRowHeight(true))
        assertEquals(22, ScreenFit.headerRowHeight(true))
        assertEquals(26, ScreenFit.headerRowHeight(false))
        // 显示屏（= S 表）高度只看屏宽，与工具行无关
        assertEquals(ScreenFit.headerHeight(true, 360), ScreenFit.headerHeight(true, 360))
        assertTrue(ScreenFit.headerHeight(true, 360) in 74..116)
        assertTrue(ScreenFit.headerHeight(false, 800) in 96..168)
        // 显示屏内容宽：主频字号按它算（不是页宽）
        assertEquals(167, ScreenFit.bezelContentWidth(360))
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

    // ── 横屏（2026-10-22）───────────────────────────────────────────────
    //
    // 用户定案：「一套双列（手机横屏与平板横屏共用），尽量一屏，不够就滚」。
    // 所以横屏的断言不是"必须一屏"，而是：
    //   ① 首屏**一定**看得全 主频/S 表/瀑布 那一行；
    //   ② 最紧的手机横屏超出量有上限（< 32dp，一眼能看到的滚动量）；
    //   ③ 横屏的一切开关都**不能**改变竖屏档位的取值。

    /** 名称, 宽 dp, 可用高 dp（横屏）。 */
    private val landscape = listOf(
        Triple("小米/红米 6.5\" 横屏", 728, 360),
        Triple("荣耀 6.7\" 横屏", 832, 384),
        Triple("Pixel 6.1\" 横屏", 892, 411),
        Triple("平板 1200×800 横屏", 1200, 800),
    )

    /** 最紧的手机横屏允许滚一点（用户选的 B：不够就滚），但不能滚出一大截。 */
    private val landscapeScrollAllowance = 32

    private fun wideBudget(w: Int, h: Int, atr: Boolean = true) =
        ScreenFit.wideBudget(w, h, fftH = 40, wfH = 110, hasAtr = atr, bottomBarVisible = true)

    @Test fun `landscape has its own compact ceiling so a tablet does not get phone sizes`() {
        assertTrue(ScreenFit.isWide(728, 360))
        assertFalse(ScreenFit.isWide(360, 728))
        // 手机横屏：高 360 → 紧凑档；平板竖屏仍走 900dp 那条老规则
        assertTrue(ScreenFit.isCompactFor(728, 360))
        assertTrue(ScreenFit.isCompactFor(360, 744))
        assertFalse(ScreenFit.isCompactFor(800, 1200))
        // 平板横屏高 800dp：**不能**被竖屏的 900dp 规则压成紧凑（否则平板上是手机尺寸）
        assertFalse(ScreenFit.isCompactFor(1200, 800))
        assertEquals(560, ScreenFit.LANDSCAPE_COMPACT_MAX_HEIGHT_DP)
    }

    @Test fun `the landscape axis leaves every portrait number alone`() {
        // 竖屏常量与公式一个都没变（横屏是纯增量）—— 这是七台真机"一屏放得下"的前提
        assertEquals(133, ScreenFit.spectrumHeight(compact = true, fftH = 40, wfH = 110))
        assertEquals(166, ScreenFit.spectrumHeight(compact = false, fftH = 40, wfH = 110))
        assertEquals(151, ScreenFit.meterWidth(360))
        assertEquals(132, ScreenFit.meterWidth(300))
        assertEquals(240, ScreenFit.meterWidth(900))
        assertEquals(6, ScreenFit.memoryColumns(compact = true))
        assertEquals(3, ScreenFit.memoryColumns(compact = false))
        mainland.forEach { (name, w, h) ->
            val b = ScreenFit.budget(w, h, 40, 110, hasAtr = true, hasVdId = true, bottomBarVisible = true)
            assertTrue("$name 竖屏预算被横屏改动波及：${b.total} > $h", b.total <= h)
        }
        // 记忆格宽度的重构（拆出 memoryCellWidthInCard）必须逐值等价
        listOf(360 to true, 411 to true, 800 to false).forEach { (w, c) ->
            val old = (ScreenFit.contentWidth(w) - ScreenFit.panelPadH(c) * 2
                - ScreenFit.MEMORY_MANAGE_W - ScreenFit.memoryColumns(c) * ScreenFit.innerGap(c)).toFloat() /
                ScreenFit.memoryColumns(c)
            assertEquals("${w}dp 记忆格宽度拼错了", old, ScreenFit.memoryCellWidth(w, c), 0.001f)
        }
    }

    @Test fun `landscape always shows the whole instrument row on the first screen`() {
        // 主频 / S 表 / 瀑布 必须在首屏可见：横屏的意义就是看瀑布，
        // 一进去就只看到半条瀑布等于没做。
        landscape.forEach { (name, w, h) ->
            val b = wideBudget(w, h)
            val viewport = h - b.bottomBar
            assertTrue(
                "$name：仪表行底 ${b.toolRow + b.instrument + b.pagePad}dp 超出首屏视口 ${viewport}dp",
                b.toolRow + b.instrument + b.pagePad <= viewport,
            )
        }
    }

    @Test fun `landscape overflow stays small on the tightest handset`() {
        landscape.forEach { (name, w, h) ->
            val b = wideBudget(w, h)
            val overflow = b.total - h
            assertTrue("$name：超出 ${overflow}dp 超过允许的 ${landscapeScrollAllowance}dp", overflow <= landscapeScrollAllowance)
        }
        // 最紧的 728×360 是唯一真的需要滚的档位（约 23dp），其余都应一屏放得下
        assertTrue(wideBudget(892, 411).total <= 411)
        assertTrue(wideBudget(1200, 800).total <= 800)
    }

    @Test fun `landscape cards are equal height and the memory cells stay readable`() {
        // 四卡等高（取最高的调谐卡）：手机上 120dp；平板上 136dp
        assertEquals(120, ScreenFit.landscapeCardsHeight(compact = true, hasAtr = true))
        assertEquals(136, ScreenFit.landscapeCardsHeight(compact = false, hasAtr = true))
        // 记忆卡在横屏只占屏宽的一列，格宽必须按**卡片**算，否则字号会估大一倍
        landscape.forEach { (name, w, h) ->
            val cell = ScreenFit.memoryCellWidthInCard(
                ScreenFit.landscapeMemoryCardWidth(w),
                ScreenFit.WIDE_MEMORY_COLUMNS,
                ScreenFit.isCompactFor(w, h),
            )
            assertTrue("$name：横屏记忆格只剩 ${"%.1f".format(cell)}dp，太窄", cell >= 30f)
            // "438.500" 在横屏记忆格里仍然放得下（到下限就交给省略号，但这里还没到）
            val fs = ScreenFit.memoryFreqFontSize("438.500", cell, true)
            assertTrue("$name：横屏记忆格字号 ${fs}sp 已压到下限", fs > ScreenFit.MEMORY_LABEL_MIN_SP)
        }
        // 3×2：6 个格子分两行
        assertEquals(3, ScreenFit.WIDE_MEMORY_COLUMNS)
        assertEquals(2, ScreenFit.WIDE_MEMORY_ROWS)
    }

    @Test fun `landscape card widths fill the page width`() {
        landscape.forEach { (name, w, _) ->
            val widths = ScreenFit.landscapeCardWidths(w)
            assertEquals("$name：卡片数量变了", 4, widths.size)
            val sum = widths.sum() + ScreenFit.LANDSCAPE_CARD_GAP * 3
            assertEquals(
                "$name：四卡 + 间隙 应正好等于内容宽（${ScreenFit.contentWidth(w)}dp）",
                ScreenFit.contentWidth(w).toFloat(), sum, 0.6f,
            )
            // 记忆卡最窄，但仍要放得下 3 列格子
            assertTrue("$name：记忆卡 ${widths.last()}dp 太窄", widths.last() >= 120f)
        }
    }

    @Test fun `narrow landscape (split screen) still leaves room for the waterfall`() {
        // 分屏/多窗口下横屏可能只有 400~480dp 宽：主频 182 + S 表 240 会把瀑布挤成 0 宽，
        // 所以 S 表在横屏要再夹一次（不超过剩余宽度的一半）。
        listOf(400 to 300, 480 to 320, 640 to 360, 728 to 360).forEach { (w, h) ->
            val content = ScreenFit.contentWidth(w)
            val used = ScreenFit.landscapeFreqWidth(w) + ScreenFit.landscapeMeterWidth(w) +
                ScreenFit.BEZEL_GAP * 2
            assertTrue("${w}dp 宽：主频+S表 用掉 ${used}dp ≥ 内容宽 ${content}dp", used <= content)
            val waterfall = content - used
            assertTrue("${w}dp 宽：瀑布只剩 ${waterfall}dp", waterfall >= 96)
            // 正常手机上仍是竖屏那套 240dp 上限，视觉没有缩水
            if (w >= 728) assertEquals(240, ScreenFit.landscapeMeterWidth(w))
        }
    }

    @Test fun `landscape without the ATR row is shorter, never taller`() {
        landscape.forEach { (name, w, h) ->
            assertTrue(name, wideBudget(w, h, atr = false).total <= wideBudget(w, h).total)
        }
    }
}
