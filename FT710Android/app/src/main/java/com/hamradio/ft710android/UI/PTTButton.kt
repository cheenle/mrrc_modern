package com.hamradio.ft710android.UI

import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.unit.dp
import com.hamradio.ft710android.PTT.PTTManager

/** 触按保持 PTT。finally 保证手势被系统取消时也一定 release（修掉 iOS onEnded 竞态）。 */
@Composable
fun PTTButton(manager: PTTManager, modifier: Modifier = Modifier) {
    val isTX = manager.isTX
    Box(
        modifier = modifier
            .background(if (isTX) Color(0xFFDC2626) else Color(0xFF16A34A), RoundedCornerShape(16.dp))
            .pointerInput(Unit) {
                detectTapGestures(onPress = {
                    manager.press()
                    try { tryAwaitRelease() } finally { manager.release() }
                })
            },
        contentAlignment = Alignment.Center,
    ) {
        Text(if (isTX) "发射" else "PTT", color = Color.White, style = MaterialTheme.typography.titleLarge)
    }
}
