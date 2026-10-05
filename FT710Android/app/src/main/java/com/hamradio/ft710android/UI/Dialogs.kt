package com.hamradio.ft710android.UI

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.hamradio.ft710android.Data.FreqInput
import com.hamradio.ft710android.Data.MemoryChannel
import com.hamradio.ft710android.Network.BandDto
import java.util.Locale

/** M2 频率输入：点频率数字弹出，解析规则同 Web commitFreq（MHz/kHz/Hz + 30k..75M clamp）。 */
@Composable
fun FrequencyInputDialog(currentHz: Long, onDismiss: () -> Unit, onSubmit: (Long) -> Unit) {
    var text by remember { mutableStateOf("%.3f".format(Locale.US, currentHz / 1e6)) }
    var invalid by remember { mutableStateOf(false) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("输入频率（MHz）") },
        text = {
            Column {
                OutlinedTextField(
                    value = text,
                    onValueChange = { text = it; invalid = false },
                    singleLine = true,
                    isError = invalid,
                    label = { Text("例如 7.050 / 7050 / 7050000") },
                )
                if (invalid) Text("无法解析该频率", color = MrrcColors.Danger, fontSize = 12.sp)
            }
        },
        confirmButton = {
            TextButton(onClick = {
                val hz = FreqInput.parse(text)
                if (hz == null) invalid = true else onSubmit(hz)
            }) { Text("调谐") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

/** S4 波段选择（Web showBandSelector 语义：名字 + 起止 + `band` 命令与默认频率）。 */
@Composable
fun BandPickerDialog(bands: List<BandDto>, current: String, onDismiss: () -> Unit, onPick: (BandDto) -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("选择波段") },
        text = {
            Column {
                bands.forEach { b ->
                    val active = b.name == current
                    Text(
                        "${b.name}  (${"%.1f".format(Locale.US, b.start / 1e6)}–${"%.1f".format(Locale.US, b.end / 1e6)} MHz)",
                        color = if (active) MrrcColors.Accent else MrrcColors.TextPrimary,
                        fontWeight = if (active) FontWeight.Bold else FontWeight.Normal,
                        fontSize = 14.sp,
                        modifier = Modifier.fillMaxWidth()
                            .clickable { onPick(b) }
                            .padding(vertical = 8.dp),
                    )
                }
            }
        },
        confirmButton = { TextButton(onClick = onDismiss) { Text("关闭") } },
    )
}

/** S4 模式选择（Web showModeSelector：fullState.modes 的权威列表）。 */
@Composable
fun ModePickerDialog(modes: List<String>, current: String, onDismiss: () -> Unit, onPick: (String) -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("选择模式") },
        text = {
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                modes.forEach { m ->
                    val active = m == current
                    Box(
                        Modifier.background(
                            if (active) MrrcColors.AccentDim else MrrcColors.BgSecondary,
                            RoundedCornerShape(6.dp))
                            .border(1.dp, if (active) MrrcColors.Accent else MrrcColors.Border, RoundedCornerShape(6.dp))
                            .clickable { onPick(m) }
                            .padding(horizontal = 10.dp, vertical = 6.dp),
                    ) { Text(m, fontSize = 13.sp, color = if (active) MrrcColors.Accent else MrrcColors.TextPrimary) }
                }
            }
        },
        confirmButton = { TextButton(onClick = onDismiss) { Text("关闭") } },
    )
}

/** S5/D4 Memory Manager（Web 语义：查看 + 清除，写回 memSave）。 */
@Composable
fun MemoryManagerDialog(mem: List<MemoryChannel?>, onDismiss: () -> Unit, onClear: (Int) -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("记忆频道") },
        text = {
            Column {
                for (i in 0 until 6) {
                    val ch = mem.getOrNull(i)
                    Row(Modifier.fillMaxWidth().padding(vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                        Text("M${i + 1}", color = MrrcColors.Accent, fontWeight = FontWeight.Bold, fontSize = 13.sp)
                        Spacer(Modifier.width(10.dp))
                        Text(
                            if (ch == null) "空" else "%.3f MHz  %s".format(Locale.US, ch.freq / 1e6, ch.label),
                            fontSize = 13.sp, modifier = Modifier.weight(1f),
                        )
                        if (ch != null) {
                            TextButton(onClick = { onClear(i) }) { Text("清除", fontSize = 12.sp) }
                        }
                    }
                }
            }
        },
        confirmButton = { TextButton(onClick = onDismiss) { Text("关闭") } },
    )
}
