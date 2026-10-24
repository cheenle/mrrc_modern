package com.hamradio.ft710android.UI

import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.hamradio.ft710android.Data.ScreenFit

/**
 * 当前屏幕档位的尺寸下发。
 *
 * 数值全部来自 [ScreenFit]（单一数据源，`ScreenFitTest` 用真机档位验证"一屏放得下"），
 * Composable 里不要就地写死 dp，否则预算测试会和真实布局脱节。
 */
class ScreenMetrics(
    val compact: Boolean,
    val screenWidthDp: Int,
    val screenHeightDp: Int,
    /** 横屏（宽 > 高）：主频/S 表/瀑布并排 + 下半四卡一排（2026-10-22）。 */
    val wide: Boolean = false,
) {
    val meterWidth: Dp get() = ScreenFit.meterWidth(screenWidthDp).dp

    /** 主频显示屏（= S 表）高度。 */
    val headerHeight: Dp get() = ScreenFit.headerHeight(compact, screenWidthDp).dp

    /** 顶栏那一行（工具 + 全部状态）的高度。 */
    val headerRowHeight: Dp get() = ScreenFit.headerRowHeight(compact).dp

    val meterCellHeight: Dp get() = ScreenFit.meterCellHeight(compact).dp
    val padBtnHeight: Dp get() = ScreenFit.padBtnHeight(compact).dp
    val chipHeight: Dp get() = ScreenFit.chipHeight(compact).dp
    val sliderRowHeight: Dp get() = ScreenFit.sliderRowHeight(compact).dp
    val pttHeight: Dp get() = ScreenFit.pttHeight(compact).dp
    val auxButtonSize: Dp get() = ScreenFit.auxButtonSize(compact).dp
    val memoryColumns: Int get() = if (wide) ScreenFit.WIDE_MEMORY_COLUMNS else ScreenFit.memoryColumns(compact)
    val memoryRowsCount: Int get() = if (wide) ScreenFit.WIDE_MEMORY_ROWS else ScreenFit.memoryRows(compact)
    val memoryCellHeight: Dp get() = ScreenFit.memoryCellHeight(compact).dp
    val memoryCellWidth: Float
        get() =
            if (wide) {
                // 横屏：记忆卡只占屏宽的一列，格宽必须按**卡片**算（按整屏宽会高估一倍）
                ScreenFit.memoryCellWidthInCard(
                    ScreenFit.landscapeMemoryCardWidth(screenWidthDp),
                    ScreenFit.WIDE_MEMORY_COLUMNS,
                    compact,
                )
            } else {
                ScreenFit.memoryCellWidth(screenWidthDp, compact)
            }
    val memoryInnerGap: Dp get() = ScreenFit.innerGap(compact).dp
    val memoryManageWidth: Dp get() = ScreenFit.MEMORY_MANAGE_W.dp

    /** 记忆格字号：按单元格宽度反推，保证单行不裁字（只显示频率）。 */
    fun memoryFreqFont(text: String): Float = ScreenFit.memoryFreqFontSize(text, memoryCellWidth, compact)

    val panelPadH: Dp get() = ScreenFit.panelPadH(compact).dp
    val panelPadV: Dp get() = ScreenFit.panelPadV(compact).dp
    val gap: Dp get() = ScreenFit.gap(compact).dp
    val headerIconTap: Dp get() = ScreenFit.headerIconTap(compact).dp
    val headerIconGlyph: Dp get() = ScreenFit.headerIconGlyph(compact).dp
    val innerGap: Dp get() = ScreenFit.innerGap(compact).dp
    val bottomBarPadV: Dp get() = ScreenFit.bottomBarPadV(compact).dp
    val spectrumScale: Float get() = ScreenFit.spectrumScale(compact)

    /** 用户设的频谱高度按档位缩放（紧凑档压一档，仍然尊重设置里的滑条）。 */
    fun spectrumHeight(
        fftH: Int,
        wfH: Int,
    ): Dp = ScreenFit.spectrumHeight(compact, fftH, wfH).dp

    // ── 横屏专用（wide=true 时才被用到）─────────────────────────────

    /** 横屏主频显示屏宽度（S 表与瀑布在它右边）。 */
    val landscapeFreqWidth: Dp get() = ScreenFit.landscapeFreqWidth(screenWidthDp).dp

    /** 横屏 S 表宽度（窄横屏/分屏时会让位给瀑布，见 `ScreenFit.landscapeMeterWidth`）。 */
    val landscapeMeterWidth: Dp get() = ScreenFit.landscapeMeterWidth(screenWidthDp).dp

    /** 横屏那一行的高度：主频显示屏 / S 表 / 瀑布 **三者等高** = 频谱高度。 */
    fun landscapeInstrumentHeight(
        fftH: Int,
        wfH: Int,
    ): Dp = ScreenFit.spectrumHeight(compact, fftH, wfH).dp

    /** 横屏四张卡片的相对宽度（仪表 / 控制 / 调谐 / 记忆）。 */
    val landscapeCardWeights: List<Float> get() = ScreenFit.LANDSCAPE_CARD_WEIGHTS

    /** 横屏一排卡片的显式高度（= 最高那张，通常是有音量滑块的调谐卡）。 */
    fun landscapeCardsHeight(hasAtr: Boolean): Dp = ScreenFit.landscapeCardsHeight(compact, hasAtr).dp
}

val LocalScreenMetrics = staticCompositionLocalOf { ScreenMetrics(false, 412, 892) }

/** 按当前 Configuration 建立档位并下发（在根部包一次即可）。 */
@Composable
fun ProvideScreenMetrics(content: @Composable () -> Unit) {
    val cfg = LocalConfiguration.current
    // screenHeightDp 已扣除状态栏/导航栏，正好是布局可用高度。
    // 旋转会重建 Activity 并重新取值，所以横竖屏切换不需要额外监听。
    val metrics =
        ScreenMetrics(
            compact = ScreenFit.isCompactFor(cfg.screenWidthDp, cfg.screenHeightDp),
            screenWidthDp = cfg.screenWidthDp,
            screenHeightDp = cfg.screenHeightDp,
            wide = ScreenFit.isWide(cfg.screenWidthDp, cfg.screenHeightDp),
        )
    CompositionLocalProvider(LocalScreenMetrics provides metrics, content = content)
}
