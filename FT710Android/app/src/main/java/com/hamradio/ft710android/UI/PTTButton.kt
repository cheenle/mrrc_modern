package com.hamradio.ft710android.UI

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateDpAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.padding
import androidx.compose.ui.draw.shadow
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.material3.ripple
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.hamradio.ft710android.PTT.PTTManager

/**
 * 触按保持 PTT —— 底部栏的主操作，视觉上必须最重。
 *
 * 质感：常态是暗红渐变凸起键；按住进入发射后**外发光 + 提亮 + 白描边**，
 * 松手/系统取消手势都走 `finally { release() }`（PTT 安全铁律，不可退化）。
 */
@Composable
fun PTTButton(manager: PTTManager, modifier: Modifier = Modifier) {
    val isTX = manager.isTX
    val shape = RoundedCornerShape(16.dp)
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()

    val glow by animateDpAsState(
        targetValue = if (isTX) 18.dp else 0.dp,
        animationSpec = tween(120),
        label = "pttGlow",
    )
    val face by animateColorAsState(
        targetValue = if (isTX) Color(0xFFFF5A4E) else Color(0xFFDC2626),
        animationSpec = tween(120),
        label = "pttFace",
    )
    val label by animateColorAsState(
        targetValue = if (isTX) MrrcColors.Danger else Color(0xFF9AA0A6),
        animationSpec = tween(120),
        label = "pttLabel",
    )

    Box(
        modifier = modifier
            // 发射时的外发光（红晕），常态收起；spotColor 在 API28+ 生效，低版本退化为普通阴影
            .shadow(glow, shape, ambientColor = MrrcColors.DangerGlow, spotColor = MrrcColors.DangerGlow)
            .graphicsLayer {
                val k = if (pressed || isTX) 0.985f else 1f
                scaleX = k; scaleY = k
            }
            .clip(shape)
            .background(
                Brush.verticalGradient(
                    listOf(
                        face.copy(alpha = if (isTX) 1f else 0.92f),
                        if (isTX) Color(0xFFB91C1C) else Color(0xFF8F1D1D),
                    ),
                ),
                shape,
            )
            .background(Brush.verticalGradient(listOf(Color.White.copy(alpha = 0.10f), Color.Transparent)), shape)
            .border(
                if (isTX) 2.dp else 1.dp,
                if (isTX) Color.White.copy(alpha = 0.55f) else Color.White.copy(alpha = 0.14f),
                shape,
            )
            .pointerInput(Unit) {
                detectTapGestures(
                    onPress = {
                        manager.press()
                        try { tryAwaitRelease() } finally { manager.release() }
                    },
                )
            },
        contentAlignment = Alignment.Center,
    ) {
        Text(
            if (isTX) "发射中" else "PTT",
            color = Color.White,
            fontSize = if (isTX) 23.sp else 22.sp,
            fontWeight = FontWeight.ExtraBold,
            letterSpacing = 2.sp,
            textAlign = TextAlign.Center,
        )
        if (!isTX) {
            Text(
                "按住说话",
                color = label,
                fontSize = 10.sp,
                letterSpacing = 1.sp,
                modifier = Modifier.align(Alignment.BottomCenter).padding(bottom = 7.dp),
            )
        }
    }
}
