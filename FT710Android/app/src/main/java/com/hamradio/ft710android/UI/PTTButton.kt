package com.hamradio.ft710android.UI

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.hamradio.ft710android.PTT.PTTManager

/**
 * 触按保持 PTT（样式对齐手机端 Web `.ptt-button`：红底白字、大按钮）。
 * finally 保证手势被系统取消时也一定 release（修掉 iOS onEnded 竞态）。
 */
@Composable
fun PTTButton(manager: PTTManager, modifier: Modifier = Modifier) {
    val isTX = manager.isTX
    val shape = RoundedCornerShape(14.dp)
    Box(
        modifier = modifier
            .background(if (isTX) Color(0xFFDC2626) else MrrcColors.Danger, shape)
            .then(if (isTX) Modifier.border(2.dp, Color.White.copy(alpha = 0.4f), shape) else Modifier)
            .pointerInput(Unit) {
                detectTapGestures(onPress = {
                    manager.press()
                    try { tryAwaitRelease() } finally { manager.release() }
                })
            },
        contentAlignment = Alignment.Center,
    ) {
        Text(
            if (isTX) "发射中" else "PTT",
            color = Color.White,
            fontSize = 18.sp,
            fontWeight = FontWeight.ExtraBold,
            letterSpacing = 2.sp,
        )
    }
}
