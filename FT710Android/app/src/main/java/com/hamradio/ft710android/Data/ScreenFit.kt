package com.hamradio.ft710android.Data

import kotlin.math.ceil

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

    /** 状态行行高（StatusItem）与上边距。 */
    const val STATUS_ITEM_H = 26
    const val STATUS_TOP_PAD = 5

    /**
     * 状态行内容估宽（dp）：☰ ⏺ RX/TX 速率 状态点 ⛶ ⏻ + 间距。
     * 用来估"会不会折成两行"。v1.1.21 瘦身：去掉 RTT·J（移到诊断行）、
     * "Serial" 文字（只留状态点）、"录音"两字（改 ⏺ 图标）→ 从 269 降到 210。
     */
    const val STATUS_CONTENT_W = 210

    /** 只读/无声等条件芯片的追加宽度。 */
    const val STATUS_EXTRA_CHIP_W = 30

    fun isCompact(screenHeightDp: Int): Boolean = screenHeightDp <= COMPACT_MAX_HEIGHT_DP

    /** 主屏可用内容宽度（去掉左右内边距）。 */
    fun contentWidth(screenWidthDp: Int): Int = screenWidthDp - PAGE_PAD_H * 2

    fun meterWidth(screenWidthDp: Int): Int =
        (contentWidth(screenWidthDp) * METER_WIDTH_RATIO).toInt().coerceIn(METER_MIN_W, METER_MAX_W)

    /** 顶栏高度 = S 表高度（主频显示屏与它等高）。 */
    fun headerHeight(compact: Boolean, screenWidthDp: Int): Int {
        val w = meterWidth(screenWidthDp)
        return if (compact) (w * 0.50f).toInt().coerceIn(74, 116) else (w * 0.64f).toInt().coerceIn(96, 168)
    }

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
    fun memoryCellHeight(compact: Boolean): Int = if (compact) 40 else 46

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

    /** CJK 字符比半角宽得多（约 1.0em vs 0.62em），混排时按最宽的算，避免溢出。 */
    private fun emPerChar(text: String): Float = if (text.any { it.code >= 0x2E80 }) 1.0f else 0.62f

    /**
     * 记忆格标签字号：**按可用宽度反推**，夹在 [MEMORY_LABEL_MIN_SP] 与档位上限之间。
     *
     * 到下限还放不下时由 UI 侧的 `TextOverflow.Ellipsis` 兜底（显示 `40m S…`），
     * 绝不换行、绝不裁半个字 —— 单元格高度是固定的，换行会把整格顶变形。
     */
    const val MEMORY_LABEL_MIN_SP = 5.5f

    fun memoryLabelFontSize(text: String, cellWidthDp: Float, compact: Boolean): Float {
        val max = if (compact) 8.5f else 10f
        if (text.isEmpty()) return max
        val fit = cellWidthDp / (text.length * emPerChar(text))
        return fit.coerceIn(MEMORY_LABEL_MIN_SP, max)
    }

    /** 记忆格频率行字号（`%.3f` 恒为 5~7 字符，按同样的宽度约束夹一下）。 */
    fun memoryFreqFontSize(text: String, cellWidthDp: Float, compact: Boolean): Float {
        val max = if (compact) 7.5f else 9f
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

    /** 状态行占几行（窄屏 FlowRow 会折行）。 */
    fun statusLines(screenWidthDp: Int, extraChips: Int): Int {
        val avail = contentWidth(screenWidthDp)
        val need = STATUS_CONTENT_W + extraChips * STATUS_EXTRA_CHIP_W
        return ceil(need.toDouble() / avail.coerceAtLeast(1)).toInt().coerceAtLeast(1)
    }

    fun statusHeight(compact: Boolean, screenWidthDp: Int, extraChips: Int): Int =
        statusLines(screenWidthDp, extraChips) * STATUS_ITEM_H + STATUS_TOP_PAD

    /**
     * 竖向预算（dp）。`scrollArea` 是滚动列内容总高，`total` 含底部固定栏。
     * 用于测试"某机型一屏放得下"，也是给 UI 取尺寸的同一套常量。
     */
    data class Budget(
        val header: Int,
        val status: Int,
        val spectrum: Int,
        val meters: Int,
        val controls: Int,
        val tuning: Int,
        val memory: Int,
        val gaps: Int,
        val pagePad: Int,
        val bottomBar: Int,
    ) {
        val scrollArea: Int get() = header + status + spectrum + meters + controls + tuning + memory + gaps + pagePad
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
            header = headerHeight(c, screenWidthDp),
            status = statusHeight(c, screenWidthDp, extraStatusChips),
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
            // 7 个子项之间 6 个间隔
            gaps = gap(c) * 6,
            pagePad = PAGE_PAD_V * 2,
            bottomBar = bottomBarHeight(c, bottomBarVisible),
        )
    }
}
