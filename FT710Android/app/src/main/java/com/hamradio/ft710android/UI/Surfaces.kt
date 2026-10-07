package com.hamradio.ft710android.UI

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 表面/容器令牌与组件 —— 主屏的"质感"统一从这里来，不要在各处就地写死颜色与圆角。
 *
 * 三层表面（由暗到亮）：
 *  - [DisplayBezel] 内凹显示屏（主频、频谱）：最暗 + 顶部一丝高光，像玻璃罩
 *  - [Panel] 卡片面板（仪表/控制/记忆分组）：中间层，1px 描边
 *  - [KeySurface] 按键/芯片常态：最亮，按下去有反馈
 */
object MrrcSurfaces {
    /** 卡片面（比 BgPrimary 亮一档）。 */
    val Panel = Color(0xFF212121)

    /** 按键常态面（比卡片再亮一档，做出"凸起"）。 */
    val Key = Color(0xFF2B2B2B)

    /** 内凹显示屏底色。 */
    val Inset = Color(0xFF131313)

    /** 1px 顶部高光（白 6%）：让面板有厚度。 */
    val Hairline = Color(0x0FFFFFFF)

    /** 描边：比 Border 更淡，避免满屏灰线。 */
    val Stroke = Color(0x22FFFFFF)

    val CardRadius = RoundedCornerShape(12.dp)
    val KeyRadius = RoundedCornerShape(8.dp)
    val BezelRadius = RoundedCornerShape(10.dp)
}

/** 卡片面板：一组控件装在一个面里（仪表 / 控制 / 调谐 / 记忆）。 */
@Composable
fun Panel(
    padH: androidx.compose.ui.unit.Dp = 10.dp,
    padV: androidx.compose.ui.unit.Dp = 9.dp,
    spacing: androidx.compose.ui.unit.Dp = 4.dp,
    modifier: Modifier = Modifier,
    content: @Composable ColumnScope.() -> Unit,
) {
    Column(
        modifier
            .fillMaxWidth()
            .clip(MrrcSurfaces.CardRadius)
            .background(MrrcSurfaces.Panel)
            // 顶部一丝高光 + 底部压暗：面板有厚度，不是贴纸
            .background(
                Brush.verticalGradient(
                    listOf(MrrcSurfaces.Hairline, Color.Transparent, Color(0x0A000000)),
                ),
                MrrcSurfaces.CardRadius,
            )
            .border(1.dp, MrrcSurfaces.Stroke, MrrcSurfaces.CardRadius)
            .padding(horizontal = padH, vertical = padV),
        // 卡内节奏统一由这里给：调用方**不要**再写 padding(top=…)（否则 ScreenFit 的预算就和真实布局脱节）
        verticalArrangement = Arrangement.spacedBy(spacing),
        content = content,
    )
}

/** 内凹显示屏（主频 / 频谱）：暗底 + 玻璃高光 + 描边。 */
@Composable
fun DisplayBezel(
    modifier: Modifier = Modifier,
    contentAlignment: Alignment = Alignment.CenterStart,
    content: @Composable BoxScope.() -> Unit,
) {
    Box(
        modifier
            .clip(MrrcSurfaces.BezelRadius)
            .background(MrrcSurfaces.Inset)
            .background(
                Brush.verticalGradient(listOf(Color.White.copy(alpha = 0.045f), Color.Transparent)),
                MrrcSurfaces.BezelRadius,
            )
            .border(1.dp, MrrcSurfaces.Stroke, MrrcSurfaces.BezelRadius),
        contentAlignment = contentAlignment,
        content = content,
    )
}

/** 分区标题：小字距 + 淡化，右侧可挂操作（如"管理"）。 */
@Composable
fun SectionLabel(
    text: String,
    modifier: Modifier = Modifier,
    trailing: (@Composable () -> Unit)? = null,
) {
    Row(modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text(
            text,
            color = MrrcColors.TextMuted,
            fontSize = 9.5.sp,
            fontWeight = FontWeight.SemiBold,
            letterSpacing = 1.2.sp,
        )
        Spacer(Modifier.weight(1f))
        trailing?.invoke()
    }
}

/** 面板之间的统一竖向节奏。 */
@Composable
fun Gap(dp: androidx.compose.ui.unit.Dp = 8.dp) = Spacer(Modifier.height(dp))

/** 横向间距（行内分组用）。 */
@Composable
fun HGap(dp: androidx.compose.ui.unit.Dp = 6.dp) = Spacer(Modifier.width(dp))
