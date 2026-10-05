package com.hamradio.ft710android.UI

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.hamradio.ft710android.Data.BandCycle
import com.hamradio.ft710android.Data.MemoryChannel
import com.hamradio.ft710android.Spectrum.WaterfallCanvas
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.delay
import java.util.Locale

/**
 * 主屏 —— 布局/交互对齐**手机端 Web**（static/index.html + ft710_ui.js）：
 * 顶栏(频率/VFO/全屏/连接) → 状态行(波段·模式·TX/TUNE·统计·无声) → 瀑布+标尺（点击 QSY）→
 * S 表 → 仪表（+ ATR 行）→ 五键行 → 芯片行 → 音量 → 步进 → VFO 行 → 录音入口 → 记忆格；
 * 底部固定 PTT/CQ/TUNE。
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun MainScreen(
    vm: MainViewModel,
    prefs: UiPrefs,
    onOpenSettings: () -> Unit,
    fullscreen: Boolean = false,
    onToggleFullscreen: () -> Unit = {},
) {
    // RadioState 是可变普通类：订阅 version 触发重组
    vm.version.collectAsState()
    val state = vm.state
    val bands by vm.bands.collectAsState()
    val modes by vm.modes.collectAsState()
    val waterfall by vm.waterfall.collectAsState()
    val fft by vm.fft.collectAsState()
    val connected by vm.connected.collectAsState()
    val listenOnly by vm.listenOnly.collectAsState()
    val mem by vm.memChannels.collectAsState()
    val rec by vm.recordingState.collectAsState()
    val recAvailable by vm.recordingsAvailable.collectAsState()
    val cq by vm.cq.collectAsState()
    val cqAvailable by vm.cqAvailable.collectAsState()
    val error by vm.error.collectAsState()
    val notice by vm.notice.collectAsState()
    val userOff by vm.userOff.collectAsState()
    val caps by vm.caps.collectAsState()
    val displayName by vm.displayName.collectAsState()
    val atrEnabled by vm.atr1000Enabled.collectAsState()
    val atr by vm.atrState.collectAsState()
    val atrTuning by vm.atrTuning.collectAsState()
    val rttMs by vm.rttMs.collectAsState()
    val rxKbps by vm.rxKbps.collectAsState()
    val txKbps by vm.txKbps.collectAsState()

    var showRecPanel by remember { mutableStateOf(false) }
    var showFreqInput by remember { mutableStateOf(false) }
    var showBandPicker by remember { mutableStateOf(false) }
    var showModePicker by remember { mutableStateOf(false) }
    var showMemManager by remember { mutableStateOf(false) }
    var stepHz by remember { mutableStateOf(1_000L) }
    val spanHz = caps.spanHz(state.scopeSpan)

    Column(Modifier.fillMaxSize().background(MrrcColors.BgPrimary)) {
        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState())
                .padding(horizontal = 8.dp, vertical = 6.dp)
        ) {
            // ── 顶栏：☰ · 频率 · VFO 切换 ────────────────────────────
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("☰", color = MrrcColors.TextSecondary, fontSize = 20.sp,
                    modifier = Modifier.clickable { onOpenSettings() }.padding(horizontal = 4.dp))
                Spacer(Modifier.width(6.dp))
                Text(
                    fmtMhz(state.activeFrequency),
                    color = MrrcColors.Accent, fontFamily = MonoFont, fontSize = 32.sp,
                    maxLines = 1,
                    modifier = Modifier.weight(1f).clickable { showFreqInput = true },
                )
                PadBtn("⛶", active = fullscreen) { onToggleFullscreen() }
                Spacer(Modifier.width(4.dp))
                PadBtn("⏻", active = !userOff, danger = userOff) {
                    if (userOff) vm.reconnect() else vm.disconnect()
                }
                Spacer(Modifier.width(4.dp))
                PadBtn("VFO-${state.activeVfo}", active = true) {
                    vm.sendSet("vfo", if (state.activeVfo == "A") "B" else "A")
                }
            }

            // ── 状态行：波段 · 模式徽章 · RX/TX · 连接点 ──────────────
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 3.dp)) {
                Text(state.bandName.ifEmpty { "—" }, color = MrrcColors.TextSecondary, fontSize = 11.sp)
                Spacer(Modifier.width(8.dp))
                Box(
                    Modifier.background(MrrcColors.Accent, RoundedCornerShape(4.dp))
                        .padding(horizontal = 8.dp, vertical = 1.dp)
                ) {
                    Text(state.modeName.ifEmpty { "—" }, color = MrrcColors.BgPrimary, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                }
                Spacer(Modifier.width(8.dp))
                Text(
                    when (state.txStatus) { 2 -> "TUNE"; 1 -> "TX"; else -> "RX" },
                    color = if (state.isTransmitting) MrrcColors.Danger else MrrcColors.TextSecondary,
                    fontSize = 11.sp, fontWeight = FontWeight.Bold,
                )
                Spacer(Modifier.width(8.dp))
                Text(
                    "↓${rxKbps}K ↑${txKbps}K",
                    color = MrrcColors.TextSecondary, fontSize = 10.sp, fontFamily = MonoFont,
                )
                Text(
                    "  RTT ${rttMs ?: "--"} J${vm.audioBufferMs()}",
                    color = MrrcColors.TextMuted, fontSize = 10.sp, fontFamily = MonoFont,
                )
                if (state.rxAudioSilent) {
                    Text(
                        " 无声", color = MrrcColors.Danger, fontSize = 11.sp, fontWeight = FontWeight.Bold,
                        modifier = Modifier.clickable {
                            vm.showNotice("RX 音频持续全零——电台 USB 音频可能卡死，请重启电台或重插 USB")
                        },
                    )
                }
                Spacer(Modifier.weight(1f))
                Box(Modifier.size(6.dp).background(if (connected) MrrcColors.Success else MrrcColors.TextMuted, CircleShape))
                Spacer(Modifier.width(4.dp))
                Text("Serial", color = MrrcColors.TextSecondary, fontSize = 10.sp)
            }

            // 机型 + 未验证徽章（web applyCapabilityBadges）
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 2.dp)) {
                Text(displayName, color = MrrcColors.TextMuted, fontSize = 9.sp)
                if (!caps.verified) {
                    Spacer(Modifier.width(6.dp))
                    Box(
                        Modifier.background(Color(0xFF78350F), RoundedCornerShape(8.dp))
                            .padding(horizontal = 6.dp, vertical = 1.dp)
                    ) {
                        Text(
                            "实验性", color = Color(0xFFFBBF24), fontSize = 10.sp,
                            modifier = Modifier.clickable {
                                vm.showNotice(
                                    if (caps.txGated) "该机型未硬件实测 — 发射已禁用（MRRC_ALLOW_UNVERIFIED_TX=1 可放行）"
                                    else "该机型未硬件实测 — 发射已由环境变量放行"
                                )
                            },
                        )
                    }
                }
            }

            if (listenOnly) {
                Text("只读登录（listen-only）：发射与设备设置已被服务端禁用",
                    color = MrrcColors.Accent, fontSize = 11.sp, modifier = Modifier.padding(top = 4.dp))
            }
            error?.let { msg ->
                Text(msg, color = MrrcColors.Danger, fontSize = 11.sp, modifier = Modifier.padding(top = 4.dp))
                LaunchedEffect(msg) { delay(4000); vm.clearError() }
            }
            notice?.let { msg ->
                Text(msg, color = MrrcColors.Accent, fontSize = 11.sp, modifier = Modifier.padding(top = 4.dp))
                LaunchedEffect(msg) { delay(5000); vm.clearNotice() }
            }

            // ── 瀑布 + 频率标尺 + 中心红标 ────────────────────────────
            Box(
                Modifier.fillMaxWidth().height((prefs.fftHeight + prefs.wfHeight).dp).padding(top = 4.dp)
                    .border(1.dp, MrrcColors.Border, RoundedCornerShape(8.dp))
            ) {
                WaterfallCanvas(
                    rows = waterfall, fft = fft,
                    theme = prefs.scopeTheme, floor = prefs.scopeFloor, ceil = prefs.scopeCeil,
                    fftFraction = prefs.fftHeight.toFloat() / (prefs.fftHeight + prefs.wfHeight).coerceAtLeast(1),
                    modifier = Modifier.fillMaxSize(),
                    onQsyFraction = { vm.qsy(it) },
                )
            }
            Row(Modifier.fillMaxWidth().padding(top = 2.dp)) {
                val labels = rulerLabels(state.scopeStartFreq, spanHz)
                labels.forEachIndexed { i, l ->
                    Text(
                        l, color = MrrcColors.Accent, fontSize = 9.sp, fontFamily = MonoFont,
                        textAlign = when (i) { 0 -> TextAlign.Start; labels.lastIndex -> TextAlign.End; else -> TextAlign.Center },
                        modifier = Modifier.weight(1f),
                    )
                }
            }
            Text(
                fmtMhzShort(state.activeFrequency), color = MrrcColors.Danger,
                fontSize = 10.sp, fontFamily = MonoFont, textAlign = TextAlign.Center,
                modifier = Modifier.fillMaxWidth(),
            )

            // ── S 表（分段渐变 + 刻度）───────────────────────────────
            SMeterBar(state.sMeter, state.sUnit, state.sMeterDbm)

            // ── 仪表：PWR/ALC 两列，SWR/Id/Vd 三列 ───────────────────
            Row(Modifier.fillMaxWidth().padding(top = 5.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                MeterCell("PWR", state.powerWatts, 100f, MrrcColors.TextPrimary, "%.1f W".format(Locale.US, state.powerWatts), Modifier.weight(1f))
                MeterCell("ALC", state.alcPct, 100f, MrrcColors.TextPrimary, "%.0f".format(Locale.US, state.alcPct), Modifier.weight(1f))
            }
            Row(Modifier.fillMaxWidth().padding(top = 4.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                MeterCell("SWR", state.swrRatio, 3f, MrrcColors.Success, "%.1f".format(Locale.US, state.swrRatio), Modifier.weight(1f))
                if (caps.hasVdIdMeters) {
                    MeterCell("Id", state.idAmps, 25f, MrrcColors.Cyan, "%.1f A".format(Locale.US, state.idAmps), Modifier.weight(1f))
                    MeterCell("Vd", state.vdVolts, 16f, MrrcColors.Purple, "%.1f V".format(Locale.US, state.vdVolts), Modifier.weight(1f))
                }
            }

            // ── ATR1000 行（web atr-row；未启用时整行隐藏）───────────
            if (atrEnabled) {
                Row(Modifier.fillMaxWidth().padding(top = 4.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    MeterCell("ATR", atr?.power ?: 0.0, 120f, MrrcColors.Warning,
                        "%.0f W".format(Locale.US, atr?.power ?: 0.0), Modifier.weight(1f))
                    MeterCell("SWR", atr?.swr ?: 0.0, 5f, MrrcColors.Success,
                        if ((atr?.swr ?: 0.0) > 0) "%.1f".format(Locale.US, atr!!.swr) else "-", Modifier.weight(1f))
                    Box(
                        Modifier.weight(1f).height(26.dp)
                            .background(MrrcColors.BgSecondary, RoundedCornerShape(6.dp))
                            .border(1.dp, MrrcColors.Border, RoundedCornerShape(6.dp))
                            .clickable(enabled = !atrTuning) { vm.atrTune() },
                        contentAlignment = Alignment.Center,
                    ) {
                        Text(if (atrTuning) "···" else "TUNE", color = MrrcColors.Accent,
                            fontSize = 11.sp, fontWeight = FontWeight.Bold)
                    }
                }
                Text(
                    if (atr == null || !atr!!.connected) "ATR 离线"
                    else "${if (atr!!.sw == 1) "CL" else "LC"} L=${atr!!.ind} C=${atr!!.cap}${if (atr!!.tuning) " ⋯" else ""}",
                    color = MrrcColors.TextSecondary, fontSize = 10.sp, fontFamily = MonoFont,
                    modifier = Modifier.padding(top = 2.dp),
                )
            }

            // ── 五键行：模式 / 波段 / 滤波 / ATT / PRE ────────────────
            Row(Modifier.fillMaxWidth().padding(top = 6.dp), horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                PadBtn("模式", Modifier.weight(1f), onLongClick = { showModePicker = true }) {
                    if (modes.isNotEmpty()) {
                        val idx = modes.indexOf(state.modeName)
                        vm.setMode(modes[(idx + 1).mod(modes.size)])
                    }
                }
                PadBtn("波段", Modifier.weight(1f), onLongClick = { showBandPicker = true }) {
                    vm.setBand(BandCycle.next(state.bandName).defaultFreq)
                }
                PadBtn(fmtFilter(state.filterHz), Modifier.weight(1f)) { vm.cycleFilter() }
                PadBtn("ATT${state.attenuatorLabel}", Modifier.weight(1f)) { vm.sendSet("att", (state.attenuator + 1) % 4) }
                PadBtn("PRE${state.preampLabel}", Modifier.weight(1f)) { vm.sendSet("preamp", (state.preamp + 1) % 3) }
            }

            // ── 芯片行：NR / NB / AN / COMP / ATU ────────────────────
            Row(Modifier.fillMaxWidth().padding(top = 5.dp), horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                SmallChip("NR", state.noiseReduction, Modifier.weight(1f)) { vm.sendSet("nr", !state.noiseReduction) }
                SmallChip("NB", state.noiseBlanker, Modifier.weight(1f)) { vm.sendSet("nb", !state.noiseBlanker) }
                if (caps.hasAutoNotch) {
                    SmallChip("AN", state.autoNotch, Modifier.weight(1f)) { vm.sendSet("an", !state.autoNotch) }
                }
                SmallChip("COMP", state.compressor, Modifier.weight(1f)) { vm.sendSet("comp", !state.compressor) }
                if (!listenOnly && caps.hasAtu) {
                    SmallChip("ATU", state.tunerStatus != 0, Modifier.weight(1f)) {
                        vm.sendSet("tuner", if (state.tunerStatus == 0) 1 else 0)
                    }
                }
            }

            // ── 音量 ─────────────────────────────────────────────────
            VolumeRow(state.afGain) { vm.sendSet("af_gain", it) }

            // ── 步进：◀◀ ◀ [100Hz] ▶ ▶▶ ──────────────────────────────
            Row(Modifier.fillMaxWidth().padding(top = 5.dp), horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                PadBtn("◀◀", Modifier.weight(1f)) { vm.setFrequencyStep(-stepHz * 10) }
                PadBtn("◀", Modifier.weight(1f)) { vm.setFrequencyStep(-stepHz) }
                PadBtn(fmtStep(stepHz), Modifier.weight(1.5f), active = true) {
                    stepHz = when (stepHz) { 10L -> 100L; 100L -> 1_000L; 1_000L -> 10_000L; else -> 10L }
                }
                PadBtn("▶", Modifier.weight(1f)) { vm.setFrequencyStep(stepHz) }
                PadBtn("▶▶", Modifier.weight(1f)) { vm.setFrequencyStep(stepHz * 10) }
            }

            // ── VFO 行：VFO-A / VFO-B / A=B / SPLIT ──────────────────
            Row(Modifier.fillMaxWidth().padding(top = 5.dp), horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                PadBtn("VFO-A", Modifier.weight(1f), active = state.activeVfo == "A") { vm.sendSet("vfo", "A") }
                PadBtn("VFO-B", Modifier.weight(1f), active = state.activeVfo == "B") { vm.sendSet("vfo", "B") }
                PadBtn("A=B", Modifier.weight(1f)) { vm.sendSet("vfo_b_freq", state.activeFrequency) }
                PadBtn("SPLIT", Modifier.weight(1f), active = state.split, danger = state.split) { vm.sendSet("split", !state.split) }
            }

            // ── 录音入口（Web .record-button 样式）───────────────────
            if (recAvailable && !listenOnly) {
                Row(Modifier.fillMaxWidth().padding(top = 6.dp)) {
                    Box(
                        Modifier.height(34.dp)
                            .background(if (rec.recording) MrrcColors.Danger.copy(alpha = 0.15f) else MrrcColors.BgCard, RoundedCornerShape(10.dp))
                            .border(1.dp, if (rec.recording) MrrcColors.Danger else MrrcColors.Border, RoundedCornerShape(10.dp))
                            .clickable { showRecPanel = true }
                            .padding(horizontal = 12.dp),
                        contentAlignment = Alignment.Center,
                    ) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            if (rec.recording) {
                                Box(Modifier.size(8.dp).background(MrrcColors.Danger, CircleShape))
                                Spacer(Modifier.width(6.dp))
                            }
                            Text(
                                if (rec.recording) "REC ${fmtSeconds(rec.duration)}" else "录音",
                                color = if (rec.recording) Color(0xFFFF6B6B) else MrrcColors.TextPrimary,
                                fontSize = 12.sp, fontWeight = FontWeight.Bold,
                            )
                        }
                    }
                }
            }

            // ── 记忆频道 3×2 ─────────────────────────────────────────
            Row(Modifier.fillMaxWidth().padding(top = 8.dp, bottom = 3.dp), verticalAlignment = Alignment.CenterVertically) {
                Text("记忆频道（长按保存 · 点按调用）", color = MrrcColors.TextMuted, fontSize = 10.sp)
                Spacer(Modifier.weight(1f))
                Text("管理", color = MrrcColors.Accent, fontSize = 10.sp,
                    modifier = Modifier.clickable { showMemManager = true }.padding(horizontal = 4.dp))
            }
            for (row in 0..1) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    for (col in 0..2) {
                        val index = row * 3 + col
                        val ch = mem.getOrNull(index)
                        Box(
                            Modifier.weight(1f).height(44.dp)
                                .background(MrrcColors.BgSecondary, RoundedCornerShape(6.dp))
                                .border(1.dp, if (ch != null) MrrcColors.Accent.copy(alpha = 0.45f) else MrrcColors.Border, RoundedCornerShape(6.dp))
                                .combinedClickable(
                                    onClick = { vm.recallMemory(index) },
                                    onLongClick = { vm.saveMemory(index) },
                                ),
                            contentAlignment = Alignment.Center,
                        ) {
                            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                if (ch != null) {
                                    Text(ch.label.ifEmpty { "M${index + 1}" }, color = MrrcColors.Accent, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                                    Text(
                                        "%.3f".format(Locale.US, ch.freq / 1e6),
                                        color = MrrcColors.TextPrimary, fontSize = 10.sp, fontFamily = MonoFont,
                                    )
                                } else {
                                    Text("M${index + 1}", color = MrrcColors.TextMuted, fontSize = 11.sp)
                                    Text("空", color = MrrcColors.TextMuted, fontSize = 9.sp)
                                }
                            }
                        }
                    }
                }
                Spacer(Modifier.height(4.dp))
            }
            Spacer(Modifier.height(6.dp))
        }

        // ── 底部固定：PTT / CQ / TUNE（listen-only 整栏隐藏）──────────
        if (!listenOnly) {
            Row(
                Modifier.fillMaxWidth().background(MrrcColors.BgSecondary)
                    .border(width = 1.dp, color = MrrcColors.Border, shape = RoundedCornerShape(0.dp))
                    .padding(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                vm.pttManager?.let { PTTButton(it, Modifier.weight(1f).height(64.dp)) }
                Spacer(Modifier.width(8.dp))
                if (cqAvailable) {
                    val calling = cq?.state == "calling"
                    Box(
                        Modifier.width(64.dp).height(64.dp)
                            .background(if (calling) MrrcColors.Success.copy(alpha = 0.2f) else MrrcColors.BgTertiary, RoundedCornerShape(14.dp))
                            .border(1.dp, MrrcColors.Success, RoundedCornerShape(14.dp))
                            .clickable { if (calling) vm.abortCq() else vm.startCq() },
                        contentAlignment = Alignment.Center,
                    ) {
                        Text(
                            if (calling) "%.0fs".format(Locale.US, cq?.elapsedS ?: 0.0) else "CQ",
                            color = MrrcColors.Success, fontSize = 13.sp, fontWeight = FontWeight.Bold,
                            textAlign = TextAlign.Center,
                        )
                    }
                    Spacer(Modifier.width(8.dp))
                }
                Box(
                    Modifier.width(64.dp).height(64.dp)
                        .background(MrrcColors.Warning, RoundedCornerShape(10.dp))
                        .clickable { vm.sendSet("tune", state.tunerStatus == 0) },
                    contentAlignment = Alignment.Center,
                ) {
                    Text("TUNE", color = Color.Black, fontSize = 13.sp, fontWeight = FontWeight.Bold)
                }
            }
        }
    }

    if (showRecPanel) RecordingPanel(vm) { showRecPanel = false }
    if (showFreqInput) {
        FrequencyInputDialog(
            currentHz = state.activeFrequency,
            onDismiss = { showFreqInput = false },
            onSubmit = { hz -> vm.sendFreqHz(hz); showFreqInput = false },
        )
    }
    if (showBandPicker) {
        BandPickerDialog(bands, state.bandName, onDismiss = { showBandPicker = false }) { b ->
            vm.sendSet("freq", b.defaultFreq)
            showBandPicker = false
        }
    }
    if (showModePicker) {
        ModePickerDialog(modes, state.modeName, onDismiss = { showModePicker = false }) { m ->
            vm.setMode(m); showModePicker = false
        }
    }
    if (showMemManager) {
        MemoryManagerDialog(mem, onDismiss = { showMemManager = false }) { i -> vm.clearMemory(i) }
    }
}

// ── 复用小组件（对齐 Web 的 .pad-btn / .chip / 仪表样式）────────────

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun PadBtn(
    label: String,
    modifier: Modifier = Modifier,
    active: Boolean = false,
    danger: Boolean = false,
    onLongClick: (() -> Unit)? = null,
    onClick: () -> Unit,
) {
    val border = when { danger -> MrrcColors.Danger; active -> MrrcColors.Accent; else -> MrrcColors.Border }
    val text = when { danger -> MrrcColors.Danger; active -> MrrcColors.Accent; else -> MrrcColors.TextPrimary }
    Box(
        modifier.height(32.dp)
            .background(if (danger) MrrcColors.Danger.copy(alpha = 0.15f) else MrrcColors.BgSecondary, RoundedCornerShape(6.dp))
            .border(1.dp, border, RoundedCornerShape(6.dp))
            .then(
                if (onLongClick != null) Modifier.combinedClickable(onClick = onClick, onLongClick = onLongClick)
                else Modifier.clickable { onClick() }
            ),
        contentAlignment = Alignment.Center,
    ) {
        Text(label, color = text, fontSize = 11.sp, fontWeight = FontWeight.SemiBold, maxLines = 1)
    }
}

@Composable
private fun SmallChip(label: String, on: Boolean, modifier: Modifier = Modifier, onClick: () -> Unit) {
    Box(
        modifier.height(28.dp)
            .background(if (on) MrrcColors.AccentDim else MrrcColors.BgSecondary, RoundedCornerShape(6.dp))
            .border(1.dp, if (on) MrrcColors.Accent else MrrcColors.Border, RoundedCornerShape(6.dp))
            .clickable { onClick() },
        contentAlignment = Alignment.Center,
    ) {
        Text(label, color = if (on) MrrcColors.Accent else MrrcColors.TextSecondary,
            fontSize = 10.5.sp, fontWeight = if (on) FontWeight.Bold else FontWeight.Normal)
    }
}

@Composable
private fun SMeterBar(sMeter: Int, sUnit: Int, dbm: Double) {
    val frac = (sMeter / 32f).coerceIn(0f, 1f)
    Column(Modifier.padding(top = 6.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Canvas(Modifier.weight(1f).height(12.dp)) {
                val n = 28
                val gap = 2f
                val w = (size.width - gap * (n - 1)) / n
                val lit = (frac * n).toInt()
                for (i in 0 until n) {
                    val f = i / (n - 1f)
                    val col = when {
                        f < 0.5f -> MrrcColors.Success
                        f < 0.8f -> MrrcColors.Warning
                        else -> Color(0xFFE08A2A)
                    }
                    drawRect(if (i < lit) col else MrrcColors.BgTertiary,
                        topLeft = Offset(i * (w + gap), 0f), size = Size(w, size.height))
                }
            }
            Spacer(Modifier.width(6.dp))
            Text("S${sUnit.coerceIn(0, 9)}", color = MrrcColors.Accent, fontSize = 11.sp,
                fontWeight = FontWeight.Bold, fontFamily = MonoFont)
            Spacer(Modifier.width(6.dp))
            Text("%.0f dBm".format(Locale.US, dbm), color = MrrcColors.TextSecondary, fontSize = 10.sp, fontFamily = MonoFont)
        }
        Row(Modifier.fillMaxWidth()) {
            listOf("S1", "S3", "S5", "S7", "S9", "+20", "+40").forEach { l ->
                Text(l, color = MrrcColors.TextMuted, fontSize = 8.sp, textAlign = TextAlign.Center, modifier = Modifier.weight(1f))
            }
        }
    }
}

@Composable
private fun MeterCell(label: String, value: Double, max: Float, color: Color, text: String, modifier: Modifier = Modifier) {
    Row(
        modifier.height(26.dp).background(MrrcColors.BgSecondary, RoundedCornerShape(6.dp))
            .padding(horizontal = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(label, color = MrrcColors.TextSecondary, fontSize = 10.sp, modifier = Modifier.width(34.dp))
        Canvas(Modifier.weight(1f).height(8.dp)) {
            drawRect(MrrcColors.BgTertiary, size = size)
            drawRect(color, size = Size(size.width * (value.toFloat() / max).coerceIn(0f, 1f), size.height))
        }
        Spacer(Modifier.width(6.dp))
        Text(text, color = MrrcColors.TextPrimary, fontSize = 10.sp, fontFamily = MonoFont,
            textAlign = TextAlign.End, modifier = Modifier.width(44.dp))
    }
}

@Composable
private fun VolumeRow(afGain: Int, onCommit: (Int) -> Unit) {
    var local by remember { mutableStateOf<Float?>(null) }
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 4.dp)) {
        Text("Vol", color = MrrcColors.TextSecondary, fontSize = 11.sp)
        Slider(
            value = local ?: afGain.toFloat(),
            onValueChange = { local = it },
            onValueChangeFinished = { local?.let { v -> onCommit(v.toInt()) }; local = null },
            valueRange = 0f..255f,
            modifier = Modifier.weight(1f).padding(horizontal = 8.dp),
            colors = SliderDefaults.colors(
                thumbColor = MrrcColors.Accent,
                activeTrackColor = MrrcColors.Accent,
                inactiveTrackColor = MrrcColors.Border,
            ),
        )
        Text("${(local ?: afGain.toFloat()).toInt()}", color = MrrcColors.Accent, fontSize = 11.sp,
            fontFamily = MonoFont, textAlign = TextAlign.End, modifier = Modifier.width(32.dp))
    }
}

// ── 纯函数格式化（Web 同款）─────────────────────────────────────────

/** Web 主频显示：2 位 MHz . 3 位 kHz . 2 位十Hz（例 07.013.50）。 */
internal fun fmtMhz(hz: Long): String =
    "%02d.%03d.%02d".format(Locale.US, hz / 1_000_000, (hz / 1000) % 1000, (hz % 1000) / 10)

internal fun fmtMhzShort(hz: Long): String = "%.3f".format(Locale.US, hz / 1e6)

internal fun fmtFilter(hz: Int): String =
    if (hz >= 1000) "%.1fk".format(Locale.US, hz / 1000f) else "${hz}Hz"

internal fun fmtStep(hz: Long): String =
    if (hz >= 1000) "${hz / 1000}k" else "${hz}Hz"

/** 标尺 6 个刻度（含两端）。 */
internal fun rulerLabels(startHz: Long, spanHz: Long): List<String> =
    (0..5).map { i ->
        val f = startHz + spanHz * i / 5
        if (spanHz >= 500_000) "%.2f".format(Locale.US, f / 1e6) else "%.3f".format(Locale.US, f / 1e6)
    }
