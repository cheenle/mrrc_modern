package com.hamradio.ft710android.Data


/**
 * 屏幕适配的**单一数据源**：尺寸档位 + 竖向空间预算（纯算术，JVM 可测）。
 *
 * 目标机型：大陆主流直板机（小米 / 红米 / 华为 / 荣耀 / vivo / OPPO）——
 * 宽 360~440dp、高 720~960dp，挖孔屏 + 手势导航（安全区上下约 24~40dp）。
 * **支持下限 = 360×728**（5.5" 直板机 + 三键导航）；5" 及以下（360×584）不在适配范围，
 * 靠滚动兜底即可（用户 2026-10-06 明确"这个不用考虑"）。
 * 这些机器上走**紧凑档**：每段高度收紧，让主屏尽量不滚动就看全；
 * 平板 / 折叠屏展开态（屏高 > [COMPACT_MAX_HEIGHT_DP]）走标准档。
 *
 * UI 侧通过 `UI/ScreenMetrics.kt`（CompositionLocal）取这里的值，
 * 不要在 Composable 里就地写死 dp —— 否则这里的预算测试就和真实布局脱节了。
 */
object ScreenFit {
    /** 屏高 ≤ 此值走紧凑档（覆盖 360×780、412×892 等主流手机）。 */
    const val COMPACT_MAX_HEIGHT_DP = 900

    /** 适配下限：5.5" 直板机 + 三键导航。更小的屏（5" 及以下）不做保证，滚动兜底。 */
    const val SUPPORTED_MIN_HEIGHT_DP = 728

    /** S 表占屏宽比例与上下限。 */
    const val METER_WIDTH_RATIO = 0.44f
    const val METER_MIN_W = 132
    const val METER_MAX_W = 240

    /** 主屏左右内边距（每侧）。 */
    const val PAGE_PAD_H = 8

    /** 滚动区上下内边距（每侧）。 */
    const val PAGE_PAD_V = 6

    /** 频谱显示屏除瀑布外的固定开销：标尺 15 + 分隔 1（VFO 红字已删，与主频重复）。 */
    const val SPECTRUM_CHROME_DP = 16

    /** 显示屏内边距（每侧）与显示屏/S 表之间的间隙。 */
    const val BEZEL_PAD_H = 10
    const val BEZEL_GAP = 6

    /**
     * 显示屏内容宽（主频真正可用的宽度）。
     * 页宽 344dp 减去 S 表与间隙后只剩 ~167dp —— 算主频字号、判断工具行能不能
     * 放进显示屏时都必须用**这个**，用页宽会严重高估。
     */
    fun bezelContentWidth(screenWidthDp: Int): Int =
        contentWidth(screenWidthDp) - meterWidth(screenWidthDp) - BEZEL_GAP - BEZEL_PAD_H * 2

    /** 只读/无声等条件芯片的追加宽度（含间距）。 */
    const val STATUS_EXTRA_CHIP_W = 30

    /** 顶栏图标的点击区（不小于 22dp，保证拇指可按）；它也是顶栏那一行的高度。 */
    fun headerIconTap(compact: Boolean): Int = if (compact) 22 else 26

    /** 顶栏图标的图形尺寸（与同行 9sp 文字视觉齐平）。 */
    fun headerIconGlyph(compact: Boolean): Int = if (compact) 11 else 13

    fun isCompact(screenHeightDp: Int): Boolean = screenHeightDp <= COMPACT_MAX_HEIGHT_DP

    /** 主屏可用内容宽度（去掉左右内边距）。 */
    fun contentWidth(screenWidthDp: Int): Int = screenWidthDp - PAGE_PAD_H * 2

    fun meterWidth(screenWidthDp: Int): Int =
        (contentWidth(screenWidthDp) * METER_WIDTH_RATIO).toInt().coerceIn(METER_MIN_W, METER_MAX_W)

    /**
     * 顶栏那一行（工具 + 全部状态）的高度：由图标点击区决定。
     * v1.1.28 起它是**全宽独立一行**，位于主频显示屏上方。
     */
    fun headerRowHeight(compact: Boolean): Int = headerIconTap(compact)

    /**
     * 主频显示屏（= S 表）高度。显示屏里只有主频一行，所以回到 0.50 比例
     * （v1.1.27 曾为容纳工具行+状态行提到 0.58，v1.1.28 那两行搬出去了）。
     */
    fun headerHeight(compact: Boolean, screenWidthDp: Int): Int {
        val w = meterWidth(screenWidthDp)
        return if (compact) (w * 0.50f).toInt().coerceIn(74, 116)
               else (w * 0.64f).toInt().coerceIn(96, 168)
    }

    /**
     * 顶栏一行的内容估宽（dp）：☰22 + 波段·模式~44 + RX/TX22 + 速率41 + 状态点7
     * + ⏺⛶⏻66 + VFO-A28 + 8×间距2 ≈ **246**。
     * 这一行是**定高 Row、不折行**，所以必须断言它装得下；真挤不下时由
     * "波段·模式"（组内唯一 `weight(fill=false)` 项）出省略号让路，其余项不被裁。
     */
    const val TOOL_ROW_CONTENT_W = 246

    /**
     * 频谱区高度比例：用户在设置里调的 Spec H / WF H 是基准，紧凑档按比例压一档。
     *
     * 0.78 是 2026-10-07 用户看过荣耀档位截图后定的（"瀑布区再减小 30%"）：
     * 之前紧凑档是 0.68 但**屏幕余量会自动灌回频谱**（bonus 最多 80dp），
     * 荣耀上实际是 198dp；砍掉 bonus 后用 0.78 得到 133dp ≈ 原来的 0.7 倍。
     */
    fun spectrumScale(compact: Boolean): Float = if (compact) 0.78f else 1f

    /**
     * 频谱显示屏高度 = 瀑布/FFT 高度（按档位缩放）+ 标尺等固定开销。
     * 这就是它在页面里的**实际占位**（UI 用 `.height(X).padding(top=6)`，padding 在 X 内部）。
     */
    fun spectrumHeight(compact: Boolean, fftH: Int, wfH: Int): Int =
        // roundToInt 而非 toInt：`150 * 0.78f` 在 Float 下是 116.99999…，截断会白丢 1dp
        kotlin.math.round((fftH + wfH) * spectrumScale(compact)).toInt() + SPECTRUM_CHROME_DP

    fun meterCellHeight(compact: Boolean): Int = if (compact) 26 else 30
    fun padBtnHeight(compact: Boolean): Int = if (compact) 32 else 34
    fun chipHeight(compact: Boolean): Int = if (compact) 26 else 28
    fun sliderRowHeight(compact: Boolean): Int = if (compact) 34 else 40

    /** 记忆格：紧凑档 6 格排一行，标准档 3×2。 */
    fun memoryColumns(compact: Boolean): Int = if (compact) 6 else 3
    fun memoryRows(compact: Boolean): Int = 6 / memoryColumns(compact)
    /**
     * 记忆格高度。2026-10-07 起格内**只显示一行频率**，40dp 显得空 → 压到 34dp
     * （仍高于 PadBtn 的 32dp 点击下限，拇指好按）；标准档 46 → 40。
     */
    fun memoryCellHeight(compact: Boolean): Int = if (compact) 34 else 40

    /** 记忆格行末的管理入口（`⋯`）宽度。 */
    const val MEMORY_MANAGE_W = 22

    /**
     * 单个记忆格的可用宽度（dp）。
     * 紧凑档一行 6 格 + 一个 `⋯`，360dp 屏上只有约 47dp —— 标签字号必须按它反推，
     * 否则长标签（`40m SSB` / 中文 / 呼号）会被裁成半个字，看着就是"变形"。
     */
    fun memoryCellWidth(screenWidthDp: Int, compact: Boolean): Float {
        val content = contentWidth(screenWidthDp) - panelPadH(compact) * 2
        val cols = memoryColumns(compact)
        val gaps = cols * innerGap(compact)          // cells + ⋯ 之间共 cols 个间隙
        return (content - MEMORY_MANAGE_W - gaps).toFloat() / cols
    }

    /** 记忆格字号下限：到下限还放不下就由 UI 的 `TextOverflow.Ellipsis` 收尾。 */
    const val MEMORY_LABEL_MIN_SP = 5.5f

    /**
     * 记忆格字号（**只显示频率**，2026-10-07 起）：`%.3f` 恒为 5~7 个半角字符，
     * 按格宽反推后夹在 [MEMORY_LABEL_MIN_SP] 与档位上限之间。
     * 单行之后空间宽松，上限给到 11sp（紧凑）/13sp（标准），比原来两行时的 7.5sp 好读得多。
     */
    fun memoryFreqFontSize(text: String, cellWidthDp: Float, compact: Boolean): Float {
        val max = if (compact) 11f else 13f
        if (text.isEmpty()) return max
        val fit = cellWidthDp / (text.length * 0.62f)     // 等宽数字，恒半角
        return fit.coerceIn(MEMORY_LABEL_MIN_SP, max)
    }

    fun pttHeight(compact: Boolean): Int = if (compact) 74 else 96
    fun auxButtonSize(compact: Boolean): Int = if (compact) 58 else 66

    fun panelPadH(compact: Boolean): Int = if (compact) 8 else 10
    fun panelPadV(compact: Boolean): Int = if (compact) 7 else 9

    /**
     * 分区标题：**全档位都不显示**（用户 2026-10-06 明确要求：各区域功能用户都知道，
     * 标题白占一行）。卡片分组 + 控件本身的文字已经足够说明用途。
     * 保留这个函数是为了预算公式可读，以及将来若要恢复标题只需改这里。
     */
    fun showSectionLabels(compact: Boolean): Boolean = false

    /** 卡片之间的竖向节奏。 */
    fun gap(compact: Boolean): Int = if (compact) 4 else 8

    /** 卡片**内部**子项间距（Panel 的 verticalArrangement）。 */
    fun innerGap(compact: Boolean): Int = if (compact) 4 else 5

    /** 分区标题占用高度（含字号行高）；紧凑档不显示标题。 */
    fun sectionLabelH(compact: Boolean): Int = if (showSectionLabels(compact)) 13 else 0

    /** 底部固定栏的竖直内边距（每侧）。 */
    fun bottomBarPadV(compact: Boolean): Int = if (compact) 7 else 8

    // 曾经有过"屏幕余量自动灌给频谱"的机制（bonus，封顶 80dp），2026-10-07 移除：
    // 它会让"把瀑布调矮"这个需求失效——省下来的空间立刻被填回去。
    // 现在频谱高度是确定值，剩余高度就是留白（滚动列内容顶对齐，视觉上是有余量而非被裁切）。


    /** 底部固定栏（PTT/CQ/TUNE）高度：竖直内边距 + 主按钮。 */
    fun bottomBarHeight(compact: Boolean, visible: Boolean): Int =
        if (!visible) 0 else pttHeight(compact) + bottomBarPadV(compact) * 2

    /**
     * 竖向预算（dp）。`scrollArea` 是滚动列内容总高，`total` 含底部固定栏。
     * 用于测试"某机型一屏放得下"，也是给 UI 取尺寸的同一套常量。
     */
    data class Budget(
        val toolRow: Int,
        val header: Int,
        val spectrum: Int,
        val meters: Int,
        val controls: Int,
        val tuning: Int,
        val memory: Int,
        val gaps: Int,
        val pagePad: Int,
        val bottomBar: Int,
    ) {
        // 7 个分区：顶栏工具行、显示屏(主频+S表)、频谱、仪表、控制、调谐、记忆
        val scrollArea: Int get() =
            toolRow + header + spectrum + meters + controls + tuning + memory + gaps + pagePad
        val total: Int get() = scrollArea + bottomBar
    }

    fun budget(
        screenWidthDp: Int,
        screenHeightDp: Int,
        fftH: Int,
        wfH: Int,
        hasAtr: Boolean,
        hasVdId: Boolean,
        bottomBarVisible: Boolean,
        extraStatusChips: Int = 0,
    ): Budget {
        val c = isCompact(screenHeightDp)
        val padV2 = panelPadV(c) * 2
        val ig = innerGap(c)
        val meterRows = 2 + (if (hasAtr) 1 else 0)
        val cell = meterCellHeight(c)
        // 卡片 = 上下内边距 + N 个子项 + (N-1) × 内间距（无标题）
        fun card(items: Int, heightOf: (Int) -> Int): Int =
            padV2 + (1..items).sumOf { heightOf(it) } + (items - 1) * ig
        return Budget(
            toolRow = headerRowHeight(c),
            header = headerHeight(c, screenWidthDp),
            // 注意：UI 是 .height(X).padding(top=6)，footprint 就是 X（padding 在里面吃掉），
            // 所以这里**不加** PAGE_PAD_V，否则模型会比真实布局多算 6dp
            spectrum = spectrumHeight(c, fftH, wfH),
            // 仪表卡：PWR/ALC + SWR/Id/Vd (+ATR) 行
            meters = card(meterRows) { cell },
            // 控制卡：五键行 + 芯片行
            controls = card(2) { if (it == 1) padBtnHeight(c) else chipHeight(c) },
            // 调谐卡：音量 + 步进 + VFO 行
            tuning = card(3) { if (it == 1) sliderRowHeight(c) else padBtnHeight(c) },
            // 记忆卡：N 行记忆格（紧凑档 1×6，标准档 2×3）
            memory = card(memoryRows(c)) { memoryCellHeight(c) },
            // 7 个子项之间 6 个间隔（顶栏工具行 / 显示屏 / 频谱 / 仪表 / 控制 / 调谐 / 记忆）
            gaps = gap(c) * 6,
            pagePad = PAGE_PAD_V * 2,
            bottomBar = bottomBarHeight(c, bottomBarVisible),
        )
    }
}
