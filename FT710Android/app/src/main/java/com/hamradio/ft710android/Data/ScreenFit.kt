package com.hamradio.ft710android.Data

import kotlin.math.ceil

/**
 * 屏幕适配的**单一数据源**：尺寸档位 + 竖向空间预算（纯算术，JVM 可测）。
 *
 * 目标机型：大陆主流直板机（小米 / 红米 / 华为 / 荣耀 / vivo / OPPO）——
 * 宽 360~440dp、高 720~960dp，挖孔屏 + 手势导航（安全区上下约 24~40dp）。
 * 这些机器上走**紧凑档**：每段高度收紧，让主屏尽量不滚动就看全；
 * 平板 / 折叠屏展开态（屏高 > [COMPACT_MAX_HEIGHT_DP]）走标准档。
 *
 * UI 侧通过 `UI/ScreenMetrics.kt`（CompositionLocal）取这里的值，
 * 不要在 Composable 里就地写死 dp —— 否则这里的预算测试就和真实布局脱节了。
 */
object ScreenFit {
    /** 屏高 ≤ 此值走紧凑档（覆盖 360×780、412×892 等主流手机）。 */
    const val COMPACT_MAX_HEIGHT_DP = 900

    /** S 表占屏宽比例与上下限。 */
    const val METER_WIDTH_RATIO = 0.44f
    const val METER_MIN_W = 132
    const val METER_MAX_W = 240

    /** 主屏左右内边距（每侧）。 */
    const val PAGE_PAD_H = 8

    /** 滚动区上下内边距（每侧）。 */
    const val PAGE_PAD_V = 6

    /** 频谱显示屏除瀑布外的固定开销：标尺 15 + VFO 红字 15 + 分隔 1。 */
    const val SPECTRUM_CHROME_DP = 31

    /** 状态行行高（StatusItem）与上边距。 */
    const val STATUS_ITEM_H = 26
    const val STATUS_TOP_PAD = 5

    /**
     * 状态行内容估宽（dp）：☰ 录音 RX/TX 速率 RTT·J 状态点 ⛶ ⏻ + 间距。
     * 用来估"会不会折成两行"——8.5~9sp 下实测约 269dp（见 CHANGELOG v1.1.16）。
     */
    const val STATUS_CONTENT_W = 269

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

    /** 频谱区高度：紧凑档把用户设的 FFT/瀑布高度按比例压一档（仍尊重设置里的滑条）。 */
    fun spectrumScale(compact: Boolean): Float = if (compact) 0.68f else 1f

    fun spectrumHeight(compact: Boolean, fftH: Int, wfH: Int): Int =
        ((fftH + wfH) * spectrumScale(compact)).toInt() + SPECTRUM_CHROME_DP + PAGE_PAD_V

    fun meterCellHeight(compact: Boolean): Int = if (compact) 26 else 30
    fun padBtnHeight(compact: Boolean): Int = if (compact) 32 else 34
    fun chipHeight(compact: Boolean): Int = if (compact) 26 else 28
    fun sliderRowHeight(compact: Boolean): Int = if (compact) 34 else 40

    /** 记忆格：紧凑档 6 格排一行，标准档 3×2。 */
    fun memoryColumns(compact: Boolean): Int = if (compact) 6 else 3
    fun memoryRows(compact: Boolean): Int = 6 / memoryColumns(compact)
    fun memoryCellHeight(compact: Boolean): Int = if (compact) 40 else 46

    fun pttHeight(compact: Boolean): Int = if (compact) 74 else 96
    fun auxButtonSize(compact: Boolean): Int = if (compact) 58 else 66

    fun panelPadH(compact: Boolean): Int = if (compact) 8 else 10
    fun panelPadV(compact: Boolean): Int = if (compact) 7 else 9

    /** 紧凑档不显示分区标题（省 4×(13+内间距)dp），靠卡片分组本身区分。 */
    fun showSectionLabels(compact: Boolean): Boolean = !compact

    /** 卡片之间的竖向节奏。 */
    fun gap(compact: Boolean): Int = if (compact) 4 else 8

    /** 卡片**内部**子项间距（Panel 的 verticalArrangement）。 */
    fun innerGap(compact: Boolean): Int = if (compact) 4 else 5

    /** 分区标题占用高度（含字号行高）；紧凑档不显示标题。 */
    fun sectionLabelH(compact: Boolean): Int = if (showSectionLabels(compact)) 13 else 0

    /** 底部固定栏的竖直内边距（每侧）。 */
    fun bottomBarPadV(compact: Boolean): Int = if (compact) 7 else 8

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
        val label = sectionLabelH(c)
        val padV2 = panelPadV(c) * 2
        val ig = innerGap(c)
        val meterRows = 2 + (if (hasAtr) 1 else 0)
        val cell = meterCellHeight(c)
        // 卡片 = 上下内边距 + [标题] + N 个子项 + (N-1) × 内间距
        fun card(items: Int, heightOf: (Int) -> Int): Int {
            val n = items + (if (label > 0) 1 else 0)
            return padV2 + label + (1..items).sumOf { heightOf(it) } + (n - 1) * ig
        }
        return Budget(
            header = headerHeight(c, screenWidthDp),
            status = statusHeight(c, screenWidthDp, extraStatusChips),
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
