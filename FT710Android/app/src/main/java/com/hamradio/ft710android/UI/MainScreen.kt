package com.hamradio.ft710android.UI

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.itemsIndexed
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.hamradio.ft710android.Data.MemoryChannel
import com.hamradio.ft710android.Spectrum.WaterfallCanvas
import com.hamradio.ft710android.ViewModel.MainViewModel
import java.util.Locale

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun MainScreen(vm: MainViewModel) {
    // RadioState 是可变的普通类：订阅 version 版本号触发重组，随后读 vm.state.* 即拿到最新值
    vm.version.collectAsState()
    val state = vm.state
    val bands by vm.bands.collectAsState()
    val modes by vm.modes.collectAsState()
    val waterfall by vm.waterfall.collectAsState()
    val fft by vm.fft.collectAsState()
    val connected by vm.connected.collectAsState()
    val mem by vm.memChannels.collectAsState()
    val scopeSpanHz = when (state.scopeSpan) { 0 -> 100000L; 1 -> 1000000L; 2 -> 50000L; else -> 100000L }

    Column(Modifier.fillMaxSize().padding(8.dp)) {
        // 顶栏：连接点 + 频率
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            ConnDot(connected, "CTRL")
            Spacer(Modifier.weight(1f))
            Text(formatFreq(state.activeFrequency), fontFamily = FontFamily.Monospace, fontSize = 40.sp)
            Spacer(Modifier.weight(1f))
            Text("${state.modeName} ${state.bandName}", fontSize = 12.sp)
        }
        // VFO A/B + 步进
        Row(verticalAlignment = Alignment.CenterVertically) {
            listOf("A", "B").forEach { v ->
                FilterChip(selected = state.activeVfo == v, onClick = { vm.sendSet("vfo", v) }, label = { Text(v) })
                Spacer(Modifier.width(4.dp))
            }
            Spacer(Modifier.weight(1f))
            TextButton(onClick = { vm.setFrequencyStep(1000) }) { Text("+1k") }
            TextButton(onClick = { vm.setFrequencyStep(-1000) }) { Text("-1k") }
        }
        // 瀑布 + FFT
        WaterfallCanvas(rows = waterfall, fft = fft,
            modifier = Modifier.fillMaxWidth().height(180.dp))
        // 模式/波段/滤波/ATT/PRE 行
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.fillMaxWidth()) {
            TextButton(onClick = { vm.setMode(modes.getOrElse(1) { "USB" }) }) { Text("模式") }
            TextButton(onClick = { vm.cycleFilter() }) { Text("滤波 ${state.filterHz}") }
            TextButton(onClick = { vm.sendSet("att", (state.attenuator + 1) % 4) }) { Text("ATT ${state.attenuatorLabel}") }
            TextButton(onClick = { vm.sendSet("preamp", (state.preamp + 1) % 3) }) { Text("PRE ${state.preampLabel}") }
        }
        // DSP 开关
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            DspChip(state.noiseReduction, { vm.sendSet("nr", it) }, "NR")
            DspChip(state.noiseBlanker, { vm.sendSet("nb", it) }, "NB")
            DspChip(state.autoNotch, { vm.sendSet("an", it) }, "AN")
            DspChip(state.compressor, { vm.sendSet("comp", it) }, "COMP")
        }
        // 仪表
        SMeterBar(state.sMeter, state.sMeterDbm)
        MeterRow("PWR", state.powerWatts, 100f)
        MeterRow("SWR", state.swrRatio, 3f)
        MeterRow("ALC", state.alcPct, 100f)
        // 记忆频道 6 槽
        LazyVerticalGrid(GridCells.Fixed(3), modifier = Modifier.weight(1f)) {
            itemsIndexed(mem) { i, c ->
                MemCell(i, c, onClick = { vm.recallMemory(i) }, onLong = { vm.saveMemory(i) })
            }
        }
        // 底部：TUNE + PTT
        Row(Modifier.fillMaxWidth().height(72.dp), verticalAlignment = Alignment.CenterVertically) {
            Button(onClick = { vm.sendSet("tune", state.tunerStatus == 0) },
                modifier = Modifier.weight(0.4f).fillMaxSize()) { Text("TUNE") }
            Spacer(Modifier.width(8.dp))
            vm.pttManager?.let { PTTButton(it, Modifier.weight(0.6f).fillMaxSize()) }
        }
    }
}

@Composable private fun ConnDot(on: Boolean, label: String) {
    Box(Modifier.size(8.dp).background(if (on) Color(0xFF22C55E) else Color(0xFF6B7280)))
    Text(" $label", fontSize = 10.sp)
}

@Composable private fun DspChip(on: Boolean, onToggle: (Boolean) -> Unit, label: String) {
    FilterChip(selected = on, onClick = { onToggle(!on) }, label = { Text(label) })
}

@Composable private fun SMeterBar(sMeter: Int, dbm: Double) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text("S", fontSize = 12.sp)
        LinearProgressIndicator(progress = (sMeter / 32f).coerceIn(0f, 1f), modifier = Modifier.weight(1f).height(10.dp))
        Text("%.1f dBm".format(Locale.US, dbm), fontSize = 11.sp)
    }
}

@Composable private fun MeterRow(label: String, value: Double, max: Float) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(label, fontSize = 11.sp, modifier = Modifier.width(40.dp))
        LinearProgressIndicator(progress = (value.toFloat() / max).coerceIn(0f, 1f), modifier = Modifier.weight(1f).height(8.dp))
        Text("%.1f".format(Locale.US, value), fontSize = 11.sp, modifier = Modifier.width(48.dp), textAlign = TextAlign.End)
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable private fun MemCell(index: Int, c: MemoryChannel?, onClick: () -> Unit, onLong: () -> Unit) {
    Box(Modifier.padding(2.dp).fillMaxWidth().combinedClickable(onClick = onClick, onLongClick = onLong),
        contentAlignment = Alignment.Center) {
        Text(c?.label ?: "空", fontSize = 11.sp)
    }
}

private fun formatFreq(hz: Long): String = "%,d".format(Locale.US, hz).replace(',', ' ') + " Hz"
