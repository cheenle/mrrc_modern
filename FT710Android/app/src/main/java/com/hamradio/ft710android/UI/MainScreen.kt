package com.hamradio.ft710android.UI

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
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
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.hamradio.ft710android.Data.BandCycle
import com.hamradio.ft710android.Data.MemoryChannel
import com.hamradio.ft710android.Data.SMeter
import com.hamradio.ft710android.Spectrum.WaterfallCanvas
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.delay
import java.util.Locale
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.text.drawText
import com.hamradio.ft710android.Data.FreqScale
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.wrapContentHeight
import androidx.compose.ui.graphics.Path

/**
 * 主屏 —— 布局/交互对齐**手机端 Web**（static/index.html + ft710_ui.js）：
 * 顶栏(频率/VFO/全屏/连接) → 状态行(波段·模式·TX/TUNE·统计·无声) → 瀑布+标尺（点击 QSY）→
 * S 表 → 仪表（+ ATR 行）→ 五键行 → 芯片行 → 音量 → 步进 → VFO 行 → 录音入口 → 记忆格；
 * 底部固定 PTT/CQ/TUNE。
 */
@OptIn(ExperimentalFoundationApi::class, ExperimentalLayoutApi::class)
@Composable
fun MainScreen(
    vm: MainViewModel,
    prefs: UiPrefs,
    onOpenSettings: () -> Unit,
    fullscreen: Boolean = false,
    onToggleFullscreen: () -> Unit = {},
    onAfVol: (Int) -> Unit = {},
) {
    // RadioState 是可变普通类：必须读出 version 才会订阅（否则只靠瀑布流带着重组）
    val stateVersion by vm.version.collectAsState()
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
    val diag by vm.diag.collectAsState()

    var showRecPanel by remember { mutableStateOf(false) }
    var showFreqInput by remember { mutableStateOf(false) }
    var showBandPicker by remember { mutableStateOf(false) }
    var showModePicker by remember { mutableStateOf(false) }
    var showMemManager by remember { mutableStateOf(false) }
    var stepHz by remember { mutableStateOf(1_000L) }
    val spanHz = remember(stateVersion) { caps.spanHz(state.scopeSpan) }

    Column(Modifier.fillMaxSize().background(MrrcColors.BgPrimary)) {
        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState())
                .padding(horizontal = 8.dp, vertical = 6.dp)
        ) {
            // ── 顶栏两列：左列 = ☰ + 主频 + 状态行；右列 = S 表独立区域 ────────
            // S 表尺寸只看屏幕宽度（不受主频行高限制），窄屏自动缩、平板放大
            BoxWithConstraints(Modifier.fillMaxWidth()) {
                val meterW = (maxWidth * 0.44f).coerceIn(140.dp, 240.dp)
                val meterH = (meterW * 0.64f).coerceIn(96.dp, 168.dp)
                Row(Modifier.fillMaxWidth()) {
                    Column(Modifier.weight(1f)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            BoxWithConstraints(Modifier.weight(1f).clickable { showFreqInput = true }) {
                                // 频率字号翻倍（32→64sp），并按可用宽度自适应：手机不溢出，平板拿满 64sp
                                val fit = (maxWidth.value / 10f / 0.62f).coerceAtMost(64f)
                                Text(
                                    fmtMhz(state.activeFrequency),
                                    color = MrrcColors.Accent, fontFamily = MonoFont,
                                    fontSize = fit.sp, maxLines = 1, softWrap = false,
                                )
                            }
                        }
                // ── 状态行：波段 · 模式徽章 · RX/TX · 连接点（FlowRow：窄屏自动折行，不溢出）──
                        FlowRow(
                    horizontalArrangement = Arrangement.spacedBy(7.dp),
                    verticalArrangement = Arrangement.spacedBy(3.dp),
                    modifier = Modifier.padding(top = 3.dp),
                ) {
                    StatusItem {
                    Text("☰", color = MrrcColors.TextSecondary, fontSize = 19.sp,
                        modifier = Modifier.clickable { onOpenSettings() }.padding(horizontal = 4.dp))
                }
                StatusItem { Text(state.bandName.ifEmpty { "—" }, color = MrrcColors.TextSecondary, fontSize = 11.sp) }
                    StatusItem {
                        Box(
                            Modifier.background(MrrcColors.Accent, RoundedCornerShape(4.dp))
                                .padding(horizontal = 8.dp, vertical = 1.dp)
                        ) {
                            Text(state.modeName.ifEmpty { "—" }, color = MrrcColors.BgPrimary, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                        }
                    }
                    PadBtn("VFO-${state.activeVfo}", active = true) {
                        vm.sendSet("vfo", if (state.activeVfo == "A") "B" else "A")
                    }
                    StatusItem {
                        Text(
                            when (state.txStatus) { 2 -> "TUNE"; 1 -> "TX"; else -> "RX" },
                            color = if (state.isTransmitting) MrrcColors.Danger else MrrcColors.TextSecondary,
                            fontSize = 11.sp, fontWeight = FontWeight.Bold,
                        )
                    }
                    StatusItem {
                        Text(
                            "↓${rxKbps}K ↑${txKbps}K",
                            color = MrrcColors.TextSecondary, fontSize = 10.sp, fontFamily = MonoFont,
                        )
                    }
                    if (state.txStatus != 0) {
                        val pk = vm.txPeak()
                        StatusItem {
                            Text(
                                "TX pk:$pk",
                                color = if (pk >= 400) MrrcColors.Success else MrrcColors.Warning,
                                fontSize = 10.sp, fontFamily = MonoFont,
                            )
                        }
                    }
                    StatusItem {
                        Text(
                            "RTT ${rttMs ?: "--"} J${vm.audioBufferMs()}",
                            color = MrrcColors.TextMuted, fontSize = 10.sp, fontFamily = MonoFont,
                        )
                    }
                    if (state.rxAudioSilent) {
                        StatusItem {
                            Text(
                                "无声", color = MrrcColors.Danger, fontSize = 11.sp, fontWeight = FontWeight.Bold,
                                modifier = Modifier.clickable {
                                    vm.showNotice("RX 音频持续全零——电台 USB 音频可能卡死，请重启电台或重插 USB")
                                },
                            )
                        }
                    }
                    StatusItem {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Box(Modifier.size(6.dp).background(if (connected) MrrcColors.Success else MrrcColors.TextMuted, CircleShape))
                            Spacer(Modifier.width(4.dp))
                            Text("Serial", color = MrrcColors.TextSecondary, fontSize = 10.sp)
                        }
                    }
                    PadBtn("⛶", active = fullscreen) { onToggleFullscreen() }
                    PadBtn("⏻", active = !userOff, danger = userOff) {
                        if (userOff) vm.reconnect() else vm.disconnect()
                    }
                }                    }
                    Spacer(Modifier.width(6.dp))
                    SmeterArc(
                        raw = state.sMeter, sUnit = state.sUnit, levelDb = state.sMeterDbm,
                        alcPct = state.alcPct, transmitting = state.isTransmitting,
                        modifier = Modifier.width(meterW).height(meterH).align(Alignment.CenterVertically),
                    )
                }
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
            // 设备侧诊断行（v1.1.2 真机无声取证；A=播放器 F=音频帧 D=解码样本 J=抖动 T=AudioTrack W/E=写）
            if (diag.isNotEmpty()) {
                Text(diag, color = MrrcColors.TextMuted, fontSize = 8.5.sp,
                    fontFamily = MonoFont, maxLines = 1)
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
            FreqScaleCanvas(state.activeFrequency, spanHz, Modifier.padding(top = 2.dp))
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
            // ── 音量（本机播放音量，web 🔊 Vol 语义）──────────────────
            VolumeRow(prefs.afVol, onAfVol)

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
                vm.pttManager?.let { PTTButton(it, Modifier.weight(1f).height(96.dp)) }
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

/** FlowRow 里的小组件：统一 32dp 行高并垂直居中（与 PadBtn 对齐）。 */
@Composable
private fun StatusItem(content: @Composable () -> Unit) {
    Box(Modifier.height(32.dp).wrapContentHeight(Alignment.CenterVertically)) { content() }
}

/**
 * FT-710 面板样式的 S 表：弧形刻度 `1 3 5 7 9 +20 +40 +60 dB` + 红针，
 * 底部 COMP 细条（面板那根压缩表，发射时用 ALC 驱动）。
 *
 * **尺寸完全跟着给它的区域走**（字体/刻度/线宽/读数都按宽高比例缩放），
 * 因此放在右上角独立区域里可以随屏幕变大变小，不受主频行高限制。
 * 刻度位置沿用 Web 的 `SMeter.MARKERS`（raw 0..255），标签取 8 个偶数刻度。
 */
@Composable
private fun SmeterArc(
    raw: Int,
    sUnit: String,
    levelDb: Double,
    alcPct: Double,
    transmitting: Boolean,
    modifier: Modifier = Modifier,
) {
    BoxWithConstraints(
        modifier.background(Color(0xFF141414), RoundedCornerShape(6.dp))
            .border(1.dp, MrrcColors.Border, RoundedCornerShape(6.dp))
            .padding(horizontal = 5.dp, vertical = 4.dp)
    ) {
        // 缩放基准：短边。手机 150×96 → 标签 9.5sp；平板 260×168 → 12sp（上限）
        val base = minOf(maxWidth.value, maxHeight.value)
        val labelFs = (base * 0.098f).coerceIn(8f, 12f)
        val readFs = (base * 0.135f).coerceIn(10.5f, 17f)
        val measurer = rememberTextMeasurer(cacheSize = 16)
        val labelStyle = TextStyle(color = MrrcColors.TextSecondary, fontSize = labelFs.sp, fontFamily = MonoFont)
        val labels = SMeter.LABELS.map { if (it.startsWith("S")) it.drop(1) else it }
        val anchors = SMeter.MARKERS.indices.filter { it % 2 == 0 }
        val layouts = remember(labelFs) { (labels + "dB").map { measurer.measure(AnnotatedString(it), labelStyle) } }
        val valueStyle = TextStyle(color = MrrcColors.Accent, fontSize = readFs.sp, fontFamily = MonoFont, fontWeight = FontWeight.Bold)
        val dbStyle = TextStyle(color = MrrcColors.TextSecondary, fontSize = (readFs * 0.72f).sp, fontFamily = MonoFont)
        val valueLayout = remember(readFs, sUnit) { measurer.measure(AnnotatedString(if (sUnit.isEmpty()) "S0" else sUnit), valueStyle) }
        val dbLayout = remember(readFs, levelDb) { measurer.measure(AnnotatedString("%.0f dB".format(Locale.US, levelDb)), dbStyle) }

        Canvas(Modifier.fillMaxSize()) {
            val w = size.width
            val h = size.height
            val dbW = layouts.last().size.width.toFloat()
            // 弧区右端让出 `dB`（面板上 dB 在 +60 右边），避免两个标签叠在一起
            val arcW = (w - dbW - labelFs * 0.6f).coerceAtLeast(1f)
            val baseY = h * 0.60f
            val ctrlY = baseY - h * 0.60f
            val tickH = (h * 0.09f).coerceAtLeast(3f)
            val labelTop = baseY + tickH + h * 0.02f
            fun pt(t: Float) = Offset(
                SMeter.arcX(t, arcW),
                SMeter.arcY(t, baseY, ctrlY),
            )
            // 弧线
            val path = Path()
            path.moveTo(0f, baseY)
            path.quadraticBezierTo(arcW / 2f, ctrlY, arcW, baseY)
            drawPath(path, MrrcColors.TextMuted, style = Stroke(width = 1.5f))
            // 16 条刻度
            SMeter.MARKERS.forEach { m ->
                val p = pt(m / SMeter.RAW_MAX.toFloat())
                drawLine(Color.White.copy(alpha = 0.35f), p, Offset(p.x, p.y + tickH), strokeWidth = 1f)
            }
            // 8 个标签 + 末尾 dB
            anchors.forEachIndexed { i, markIdx ->
                val p = pt(SMeter.MARKERS[markIdx] / SMeter.RAW_MAX.toFloat())
                val lay = layouts[i]
                drawText(lay, topLeft = Offset(
                    (p.x - lay.size.width / 2f).coerceIn(0f, (arcW - lay.size.width).coerceAtLeast(0f)),
                    labelTop,
                ))
            }
            val dbLay = layouts.last()
            drawText(dbLay, topLeft = Offset(w - dbLay.size.width, labelTop))
            // 红针（当前位置）
            val np = pt(SMeter.fraction(raw))
            drawLine(MrrcColors.Danger, np, Offset(np.x, baseY + tickH * 0.6f), strokeWidth = 2f)
            drawCircle(MrrcColors.Danger, radius = labelFs * 0.22f, center = np)
            // COMP 细条（面板第二行；发射时用 ALC 百分比）
            val compH = (h * 0.055f).coerceAtLeast(3f)
            val compTop = h - compH
            drawRect(Color.White.copy(alpha = 0.08f), topLeft = Offset(0f, compTop), size = Size(w, compH))
            val filled = (if (transmitting) (alcPct / 100.0).toFloat() else 0f).coerceIn(0f, 1f) * w
            if (filled > 0f) drawRect(MrrcColors.Warning, topLeft = Offset(0f, compTop), size = Size(filled, compH))
            // 读数：左上 S 值（大）、右上相对 S9 的 dB
            drawText(valueLayout, topLeft = Offset(0f, 0f))
            drawText(dbLayout, topLeft = Offset(w - dbLayout.size.width, 0f))
        }
        Text(
            "COMP",
            color = MrrcColors.TextMuted, fontSize = (labelFs * 0.8f).sp, fontFamily = MonoFont,
            modifier = Modifier.align(Alignment.BottomEnd),
        )
    }
}

@Composable
private fun SMeterBar(sMeter: Int, sUnit: String, levelDb: Double) {
    Column(Modifier.padding(top = 6.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Canvas(Modifier.weight(1f).height(14.dp)) {
                val pos = SMeter.fraction(sMeter) * size.width
                // 已填充：Web 的横向渐变（绿→黄→橙→红），按信号位置裁剪
                if (pos > 0f) {
                    drawRect(
                        brush = Brush.horizontalGradient(
                            0f to Color(0xFF22C55E),
                            0.3f to Color(0xFF22C55E),
                            0.5f to Color(0xFFEAB308),
                            0.7f to Color(0xFFF59E0B),
                            1f to Color(0xFFEF4444),
                        ),
                        size = Size(pos, size.height),
                    )
                }
                // 未填充部分（Web: rgba(255,255,255,0.05)）
                drawRect(
                    Color.White.copy(alpha = 0.05f),
                    topLeft = Offset(pos, 0f),
                    size = Size((size.width - pos).coerceAtLeast(0f), size.height),
                )
                // 刻度线（Web 的 marks 数组）
                SMeter.MARKERS.forEach { m ->
                    val x = m / SMeter.RAW_MAX.toFloat() * size.width
                    drawLine(Color.White.copy(alpha = 0.3f), Offset(x, 0f), Offset(x, size.height), strokeWidth = 1f)
                }
                drawRect(Color.White.copy(alpha = 0.15f), style = Stroke(width = 1f))
            }
            Spacer(Modifier.width(6.dp))
            // 服务端 s_unit 直接就是显示串（"S9" / "+20" / "+60"）
            Text(
                sUnit.ifEmpty { "S0" }, color = MrrcColors.Accent, fontSize = 12.sp,
                fontWeight = FontWeight.Bold, fontFamily = MonoFont,
                textAlign = TextAlign.End, modifier = Modifier.width(34.dp),
            )
            Spacer(Modifier.width(4.dp))
            // s_meter_dbm 是相对 S9 的 dB（S9 = 0 dB 参考），不是 dBm
            Text(
                "%.0f dB".format(Locale.US, levelDb), color = MrrcColors.TextSecondary,
                fontSize = 10.sp, fontFamily = MonoFont,
            )
        }
        Row(Modifier.fillMaxWidth()) {
            SMeter.LABELS.forEach { l ->
                Text(l, color = MrrcColors.TextMuted, fontSize = 8.sp,
                    textAlign = TextAlign.Center, modifier = Modifier.weight(1f))
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

/**
 * 频谱标尺：刻度按**真实频率位置**画（Web `renderFreqScale`），范围恒为 VFO ± span/2。
 * 用 `scope_start_freq` 会滞后（服务端恒 CENTER 模式，该字段不跟手）。
 */
@Composable
internal fun FreqScaleCanvas(vfoFreq: Long, spanHz: Long, modifier: Modifier = Modifier) {
    if (spanHz <= 0) return
    val measurer = rememberTextMeasurer()
    val ticks = remember(vfoFreq, spanHz) { FreqScale.ticks(vfoFreq, spanHz) }
    val step = remember(spanHz) { FreqScale.step(spanHz) }
    val tickStyle = remember { TextStyle(color = MrrcColors.Accent, fontSize = 9.sp, fontFamily = MonoFont) }
    Canvas(modifier.fillMaxWidth().height(14.dp)) {
        val w = size.width
        ticks.forEach { (f, frac) ->
            val x = frac * w
            drawLine(MrrcColors.Border, Offset(x, 0f), Offset(x, 4.dp.toPx()), 1f)
            val layout = measurer.measure(AnnotatedString(FreqScale.label(f, step)), tickStyle)
            drawText(layout, topLeft = Offset((x - layout.size.width / 2f).coerceIn(0f, (w - layout.size.width).coerceAtLeast(0f)), 4.dp.toPx()))
        }
    }
}
