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
import androidx.compose.ui.unit.TextUnit
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
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.material3.ripple
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.graphics.Shadow
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.ui.unit.Dp
import androidx.compose.foundation.layout.defaultMinSize
import com.hamradio.ft710android.Data.ScreenFit
import androidx.compose.ui.platform.testTag
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
    val spanHz = remember(stateVersion) { caps.spanHz(state.scopeSpan) }
    // 屏幕档位：紧凑（大陆主流手机）/ 标准（平板、折叠屏展开）
    val m = LocalScreenMetrics.current

    Column(Modifier.fillMaxSize().background(MrrcColors.BgPrimary)) {
        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState())
                .testTag("mainScroll")
                .padding(horizontal = ScreenFit.PAGE_PAD_H.dp, vertical = ScreenFit.PAGE_PAD_V.dp),
            verticalArrangement = Arrangement.spacedBy(m.gap),
        ) {
            // ── 顶栏：主频显示屏（上沿带波段/模式/VFO 读数）+ S 表独立区域 ────
            // S 表尺寸只看屏幕宽度（不受主频行高限制），窄屏自动缩、平板放大
            BoxWithConstraints(Modifier.fillMaxWidth()) {
                // 尺寸全部来自 ScreenFit（单一数据源）；不要在这里就地算比例
                val meterH = m.headerHeight
                // 等高靠**显式高度**（meterH 已由屏宽算出），不能用 IntrinsicSize：
                // 子项里的 BoxWithConstraints 是 SubcomposeLayout，问它 intrinsic 会直接抛异常（启动即崩）
                Row(Modifier.fillMaxWidth().height(meterH).testTag("secHeader")) {
                    DisplayBezel(
                        modifier = Modifier.weight(1f).fillMaxHeight().clickable { showFreqInput = true },
                    ) {
                        Column(Modifier.fillMaxSize().padding(horizontal = 10.dp, vertical = 5.dp)) {
                            // 显示屏上沿：波段 · 模式（左）｜VFO（右，点按切 A/B）
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(
                                    "${state.bandName.ifEmpty { "—" }} · ${state.modeName.ifEmpty { "—" }}",
                                    color = MrrcColors.TextMuted, fontSize = 9.sp,
                                    letterSpacing = 0.8.sp, maxLines = 1,
                                )
                                Spacer(Modifier.weight(1f))
                                Text(
                                    "VFO-${state.activeVfo}",
                                    color = MrrcColors.Accent, fontSize = 9.sp,
                                    fontWeight = FontWeight.Bold, letterSpacing = 0.6.sp,
                                    modifier = Modifier.clickable {
                                        vm.sendSet("vfo", if (state.activeVfo == "A") "B" else "A")
                                    },
                                )
                            }
                            // 主频（琥珀辉光，末两位 10Hz 淡化）
                            Box(Modifier.fillMaxWidth().weight(1f), contentAlignment = Alignment.Center) {
                                BoxWithConstraints(Modifier.fillMaxWidth()) {
                                    val fit = ((maxWidth.value - 6f) / 6.3f).coerceIn(18f, 58f)
                                    FreqText(hz = state.activeFrequency, fitSp = fit)
                                }
                            }
                        }
                    }
                    Spacer(Modifier.width(6.dp))
                    SmeterArc(
                        raw = state.sMeter, sUnit = state.sUnit, levelDb = state.sMeterDbm,
                        alcPct = state.alcPct, transmitting = state.isTransmitting,
                        modifier = Modifier.width(m.meterWidth).height(meterH).align(Alignment.CenterVertically),
                    )
                }
            }

            // ── 状态行：全宽**单行**（波段/模式/VFO 已进显示屏，这里只剩状态）──
            // 字体 8.5~9sp、行高 26dp；FlowRow 只作极窄屏兜底，正常一行装得下
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(5.dp),
                verticalArrangement = Arrangement.spacedBy(2.dp),
                modifier = Modifier.fillMaxWidth().padding(top = 5.dp).testTag("secStatus"),
            ) {
                StatusItem {
                    Text("☰", color = MrrcColors.TextSecondary, fontSize = 13.sp,
                        modifier = Modifier.clickable { onOpenSettings() }.padding(horizontal = 3.dp))
                }
                if (listenOnly) {
                    StatusItem {
                        Text("只读", color = MrrcColors.Accent, fontSize = 9.sp, fontWeight = FontWeight.Bold,
                            modifier = Modifier.clickable {
                                vm.showNotice("只读登录（listen-only）：发射与设备设置已被服务端禁用")
                            })
                    }
                }
                if (recAvailable && !listenOnly) {
                    StatusItem {
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            modifier = Modifier
                                .background(
                                    if (rec.recording) MrrcColors.Danger.copy(alpha = 0.15f) else MrrcSurfaces.Key,
                                    RoundedCornerShape(percent = 50),
                                )
                                .border(
                                    1.dp,
                                    if (rec.recording) MrrcColors.Danger else MrrcSurfaces.Stroke,
                                    RoundedCornerShape(percent = 50),
                                )
                                .clickable { showRecPanel = true }
                                .padding(horizontal = 7.dp, vertical = 2.dp),
                        ) {
                            if (rec.recording) {
                                Box(Modifier.size(5.dp).background(MrrcColors.Danger, CircleShape))
                                Spacer(Modifier.width(4.dp))
                            }
                            // 图标入口（省掉"录音"两个字）；录音中显示时长
                            Text(
                                if (rec.recording) fmtSeconds(rec.duration) else "⏺",
                                color = if (rec.recording) Color(0xFFFF6B6B) else MrrcColors.TextSecondary,
                                fontSize = if (rec.recording) 9.sp else 12.sp,
                                fontWeight = FontWeight.SemiBold,
                            )
                        }
                    }
                }
                StatusItem {
                    Text(
                        when (state.txStatus) { 2 -> "TUNE"; 1 -> "TX"; else -> "RX" },
                        color = if (state.isTransmitting) MrrcColors.Danger else MrrcColors.TextSecondary,
                        fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 0.4.sp,
                    )
                }
                StatusItem {
                    // 发射时这一段换成麦克风峰值（RX 此时本来就没有码率）
                    val pk = vm.txPeak()
                    Text(
                        if (state.txStatus != 0) "TX pk:$pk" else "↓${rxKbps}↑${txKbps}",
                        color = if (state.txStatus != 0 && pk < 400) MrrcColors.Warning else MrrcColors.TextMuted,
                        fontSize = 8.5.sp, fontFamily = MonoFont,
                    )
                }
                if (state.rxAudioSilent) {
                    StatusItem {
                        Text(
                            "无声", color = MrrcColors.Danger, fontSize = 9.sp, fontWeight = FontWeight.Bold,
                            modifier = Modifier.clickable {
                                vm.showNotice("RX 音频持续全零——电台 USB 音频可能卡死，请重启电台或重插 USB")
                            },
                        )
                    }
                }
                // 连接状态点（点按出文字说明；不再常驻 "Serial" 三个字）
                StatusItem {
                    Box(
                        Modifier.size(8.dp)
                            .background(if (connected) MrrcColors.Success else MrrcColors.TextMuted, CircleShape)
                            .clickable {
                                vm.showNotice(
                                    if (connected) "电台串口已连接"
                                    else "电台串口未连接（服务端与电台之间）"
                                )
                            },
                    )
                }
                PadBtn("⛶", active = fullscreen, fontSize = 10.sp, btnHeight = m.statusItemHeight) { onToggleFullscreen() }
                PadBtn("⏻", active = !userOff, danger = userOff, fontSize = 10.sp, btnHeight = m.statusItemHeight) {
                    if (userOff) vm.reconnect() else vm.disconnect()
                }
            }

            error?.let { msg ->
                Text(msg, color = MrrcColors.Danger, fontSize = 11.sp, modifier = Modifier.padding(top = 4.dp))
                LaunchedEffect(msg) { delay(4000); vm.clearError() }
            }
            notice?.let { msg ->
                Text(msg, color = MrrcColors.Accent, fontSize = 11.sp, modifier = Modifier.padding(top = 4.dp))
                LaunchedEffect(msg) { delay(5000); vm.clearNotice() }
            }

            // ── 频谱显示屏（独立子组件：只有它订阅 waterfall/fft）──────────
            SpectrumPanel(vm, prefs, spanHz, state.activeFrequency)

            Panel(m.panelPadH, m.panelPadV, m.innerGap, tag = "secMeters") {
                // ── 仪表：PWR/ALC 两列，SWR/Id/Vd 三列 ───────────────────
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    MeterCell("PWR", state.powerWatts, 100f, MrrcColors.TextPrimary, "%.1f W".format(Locale.US, state.powerWatts), Modifier.weight(1f))
                    MeterCell("ALC", state.alcPct, 100f, MrrcColors.TextPrimary, "%.0f".format(Locale.US, state.alcPct), Modifier.weight(1f))
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    MeterCell("SWR", state.swrRatio, 3f, MrrcColors.Success, "%.1f".format(Locale.US, state.swrRatio), Modifier.weight(1f))
                    if (caps.hasVdIdMeters) {
                        MeterCell("Id", state.idAmps, 25f, MrrcColors.Cyan, "%.1f A".format(Locale.US, state.idAmps), Modifier.weight(1f))
                        MeterCell("Vd", state.vdVolts, 16f, MrrcColors.Purple, "%.1f V".format(Locale.US, state.vdVolts), Modifier.weight(1f))
                    }
                }

                // ── ATR1000 行（web atr-row；未启用时整行隐藏；天调参数并进本行，不再单独一行）──
                if (atrEnabled) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        MeterCell("ATR", atr?.power ?: 0.0, 120f, MrrcColors.Warning,
                            "%.0f W".format(Locale.US, atr?.power ?: 0.0), Modifier.weight(1f))
                        MeterCell("SWR", atr?.swr ?: 0.0, 5f, MrrcColors.Success,
                            if ((atr?.swr ?: 0.0) > 0) "%.1f".format(Locale.US, atr!!.swr) else "-", Modifier.weight(1f))
                        Box(Modifier.weight(1.1f).height(m.meterCellHeight), contentAlignment = Alignment.Center) {
                            Text(
                                if (atr == null || !atr!!.connected) "ATR 离线"
                                else "${if (atr!!.sw == 1) "CL" else "LC"} L=${atr!!.ind} C=${atr!!.cap}${if (atr!!.tuning) " ⋯" else ""}",
                                color = MrrcColors.TextSecondary, fontSize = 9.sp, fontFamily = MonoFont,
                                textAlign = TextAlign.Center, maxLines = 2, lineHeight = 10.sp,
                            )
                        }
                        Box(
                            Modifier.weight(1f).height(m.meterCellHeight)
                                .background(MrrcColors.BgSecondary, RoundedCornerShape(6.dp))
                                .border(1.dp, MrrcColors.Border, RoundedCornerShape(6.dp))
                                .clickable(enabled = !atrTuning) { vm.atrTune() },
                            contentAlignment = Alignment.Center,
                        ) {
                            Text(if (atrTuning) "···" else "TUNE", color = MrrcColors.Accent,
                                fontSize = 11.sp, fontWeight = FontWeight.Bold)
                        }
                    }
                }
            }

            Panel(m.panelPadH, m.panelPadV, m.innerGap, tag = "secControls") {
                // ── 五键行：模式 / 波段 / 滤波 / ATT / PRE ────────────────
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(5.dp)) {
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
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(5.dp)) {
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
            }

            Panel(m.panelPadH, m.panelPadV, m.innerGap, tag = "secTuning") {
                // ── 音量 ─────────────────────────────────────────────────
                // ── 音量（本机播放音量，web 🔊 Vol 语义）──────────────────
                VolumeRow(prefs.afVol, onAfVol)

                // ── 步进：◀◀ ◀ [100Hz] ▶ ▶▶ ──────────────────────────────
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                    PadBtn("◀◀", Modifier.weight(1f)) { vm.setFrequencyStep(-stepHz * 10) }
                    PadBtn("◀", Modifier.weight(1f)) { vm.setFrequencyStep(-stepHz) }
                    PadBtn(fmtStep(stepHz), Modifier.weight(1.5f), active = true) {
                        stepHz = when (stepHz) { 10L -> 100L; 100L -> 1_000L; 1_000L -> 10_000L; else -> 10L }
                    }
                    PadBtn("▶", Modifier.weight(1f)) { vm.setFrequencyStep(stepHz) }
                    PadBtn("▶▶", Modifier.weight(1f)) { vm.setFrequencyStep(stepHz * 10) }
                }

                // ── VFO 行：VFO-A / VFO-B / A=B / SPLIT ──────────────────
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                    PadBtn("VFO-A", Modifier.weight(1f), active = state.activeVfo == "A") { vm.sendSet("vfo", "A") }
                    PadBtn("VFO-B", Modifier.weight(1f), active = state.activeVfo == "B") { vm.sendSet("vfo", "B") }
                    PadBtn("A=B", Modifier.weight(1f)) { vm.sendSet("vfo_b_freq", state.activeFrequency) }
                    PadBtn("SPLIT", Modifier.weight(1f), active = state.split, danger = state.split) { vm.sendSet("split", !state.split) }
                }
            }

            // ── 记忆频道 3×2 ─────────────────────────────────────────
            Panel(m.panelPadH, m.panelPadV, m.innerGap, tag = "secMemory") {
                for (row in 0 until m.memoryRowsCount) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                        for (col in 0 until m.memoryColumns) {
                            val index = row * m.memoryColumns + col
                            val ch = mem.getOrNull(index)
                            val memShape = RoundedCornerShape(10.dp)
                            val memInteraction = remember(index) { MutableInteractionSource() }
                            val memPressed by memInteraction.collectIsPressedAsState()
                            Box(
                                Modifier.weight(1f).height(m.memoryCellHeight)
                                    .graphicsLayer {
                                        val k = if (memPressed) 0.97f else 1f
                                        scaleX = k; scaleY = k
                                    }
                                    .clip(memShape)
                                    .background(if (ch != null) MrrcColors.AccentDim.copy(alpha = 0.10f) else MrrcSurfaces.Key)
                                    .background(
                                        Brush.verticalGradient(listOf(MrrcSurfaces.Hairline, Color.Transparent)),
                                        memShape,
                                    )
                                    .border(
                                        1.dp,
                                        if (ch != null) MrrcColors.Accent.copy(alpha = 0.55f) else MrrcSurfaces.Stroke,
                                        memShape,
                                    )
                                    .combinedClickable(
                                        interactionSource = memInteraction,
                                        indication = ripple(),
                                        onClick = { vm.recallMemory(index) },
                                        onLongClick = { vm.saveMemory(index) },
                                    ),
                                contentAlignment = Alignment.Center,
                            ) {
                                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                    if (ch != null) {
                                        Text(ch.label.ifEmpty { "M${index + 1}" }, color = MrrcColors.Accent,
                                            fontSize = if (m.compact) 8.5.sp else 10.sp,
                                            fontWeight = FontWeight.Bold, letterSpacing = 0.3.sp, maxLines = 1)
                                        Text(
                                            "%.3f".format(Locale.US, ch.freq / 1e6),
                                            color = MrrcColors.TextPrimary,
                                            fontSize = if (m.compact) 7.5.sp else 9.sp,
                                            fontFamily = MonoFont, maxLines = 1,
                                        )
                                    } else {
                                        Text("M${index + 1}", color = MrrcColors.TextMuted,
                                            fontSize = if (m.compact) 8.5.sp else 10.sp, letterSpacing = 0.3.sp)
                                    }
                                }
                            }
                        }
                        // 记忆管理入口：图标，不占文字（原先藏在被删掉的分区标题里）
                        if (row == 0) {
                            Box(
                                Modifier.width(26.dp).height(m.memoryCellHeight)
                                    .clip(RoundedCornerShape(10.dp))
                                    .background(MrrcSurfaces.Key)
                                    .border(1.dp, MrrcSurfaces.Stroke, RoundedCornerShape(10.dp))
                                    .clickable { showMemManager = true },
                                contentAlignment = Alignment.Center,
                            ) {
                                Text("⋯", color = MrrcColors.Accent, fontSize = 15.sp, fontWeight = FontWeight.Bold)
                            }
                        } else {
                            Spacer(Modifier.width(26.dp))
                        }
                    }
                }
            }
        }

        // ── 底部固定：PTT / CQ / TUNE（listen-only 整栏隐藏）──────────
        if (!listenOnly) {
            Column(
                Modifier.fillMaxWidth()
                    .background(Brush.verticalGradient(listOf(Color(0xFF232323), MrrcColors.BgSecondary)))
                    .drawWithContent {
                        drawContent()
                        drawLine(MrrcSurfaces.Hairline, Offset(0f, 0.5f), Offset(size.width, 0.5f), 1f)
                    }
                    .padding(horizontal = 10.dp, vertical = m.bottomBarPadV),
            ) {
            Row(
                Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                vm.pttManager?.let { PTTButton(it, Modifier.weight(1f).height(m.pttHeight)) }
                Spacer(Modifier.width(8.dp))
                if (cqAvailable) {
                    val calling = cq?.state == "calling"
                    val cqShape = RoundedCornerShape(14.dp)
                    val cqInteraction = remember { MutableInteractionSource() }
                    val cqPressed by cqInteraction.collectIsPressedAsState()
                    Box(
                        Modifier.width(m.auxButtonSize).height(m.auxButtonSize)
                            .graphicsLayer { val k = if (cqPressed) 0.95f else 1f; scaleX = k; scaleY = k }
                            .clip(cqShape)
                            .background(
                                if (calling) Brush.verticalGradient(listOf(MrrcColors.Success.copy(alpha = 0.32f), MrrcColors.Success.copy(alpha = 0.12f)))
                                else Brush.verticalGradient(listOf(MrrcSurfaces.Key, MrrcSurfaces.Panel)),
                                cqShape,
                            )
                            .border(1.dp, MrrcColors.Success.copy(alpha = if (calling) 1f else 0.55f), cqShape)
                            .clickable(interactionSource = cqInteraction, indication = ripple()) { if (calling) vm.abortCq() else vm.startCq() },
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
                val tuneShape = RoundedCornerShape(14.dp)
                val tuneInteraction = remember { MutableInteractionSource() }
                val tunePressed by tuneInteraction.collectIsPressedAsState()
                val tuning = state.tunerStatus != 0
                Box(
                    Modifier.width(m.auxButtonSize).height(m.auxButtonSize)
                        .graphicsLayer { val k = if (tunePressed) 0.95f else 1f; scaleX = k; scaleY = k }
                        .clip(tuneShape)
                        .background(
                            Brush.verticalGradient(
                                if (tuning) listOf(MrrcColors.Warning, Color(0xFFB45309))
                                else listOf(MrrcColors.Warning.copy(alpha = 0.85f), MrrcColors.Warning.copy(alpha = 0.55f))
                            ),
                            tuneShape,
                        )
                        .border(1.dp, MrrcColors.Warning, tuneShape)
                        .clickable(interactionSource = tuneInteraction, indication = ripple()) { vm.sendSet("tune", !tuning) },
                    contentAlignment = Alignment.Center,
                ) {
                    Text("TUNE", color = Color.Black, fontSize = 13.sp, fontWeight = FontWeight.Bold)
                }
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
    fontSize: TextUnit = 11.sp,
    btnHeight: Dp? = null,
    onLongClick: (() -> Unit)? = null,
    onClick: () -> Unit,
) {
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    val bg = when {
        danger -> MrrcColors.Danger.copy(alpha = 0.16f)
        active -> MrrcColors.AccentDim
        else -> MrrcSurfaces.Key
    }
    val bd = when {
        danger -> MrrcColors.Danger.copy(alpha = 0.85f)
        active -> MrrcColors.Accent
        else -> MrrcSurfaces.Stroke
    }
    val fg = when {
        danger -> MrrcColors.Danger
        active -> MrrcColors.Accent
        else -> MrrcColors.TextPrimary
    }
    Box(
        modifier.height(btnHeight ?: LocalScreenMetrics.current.padBtnHeight).defaultMinSize(minWidth = 30.dp)
            // 按下去：轻微缩小 + 变暗，做出物理键的手感
            .graphicsLayer {
                val k = if (pressed) 0.96f else 1f
                scaleX = k; scaleY = k; alpha = if (pressed) 0.82f else 1f
            }
            .clip(MrrcSurfaces.KeyRadius)
            .background(bg)
            .background(Brush.verticalGradient(listOf(MrrcSurfaces.Hairline, Color.Transparent)), MrrcSurfaces.KeyRadius)
            .border(1.dp, bd, MrrcSurfaces.KeyRadius)
            .then(
                if (onLongClick != null) {
                    Modifier.combinedClickable(
                        interactionSource = interaction, indication = ripple(),
                        onClick = onClick, onLongClick = onLongClick,
                    )
                } else {
                    Modifier.clickable(interactionSource = interaction, indication = ripple()) { onClick() }
                }
            ),
        contentAlignment = Alignment.Center,
    ) {
        Text(label, color = fg, fontSize = fontSize, fontWeight = FontWeight.SemiBold,
            letterSpacing = 0.4.sp, maxLines = 1)
    }
}

@Composable
private fun SmallChip(label: String, on: Boolean, modifier: Modifier = Modifier, onClick: () -> Unit) {
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    val pill = RoundedCornerShape(percent = 50)
    Box(
        modifier.height(LocalScreenMetrics.current.chipHeight)
            .graphicsLayer {
                val k = if (pressed) 0.95f else 1f
                scaleX = k; scaleY = k
            }
            .clip(pill)
            .background(if (on) MrrcColors.AccentDim else MrrcSurfaces.Key)
            .border(1.dp, if (on) MrrcColors.Accent.copy(alpha = 0.9f) else MrrcSurfaces.Stroke, pill)
            .clickable(interactionSource = interaction, indication = ripple()) { onClick() },
        contentAlignment = Alignment.Center,
    ) {
        Text(
            label,
            color = if (on) MrrcColors.Accent else MrrcColors.TextSecondary,
            fontSize = 10.sp,
            fontWeight = if (on) FontWeight.Bold else FontWeight.SemiBold,
            letterSpacing = 0.6.sp,
        )
    }
}

/**
 * 频谱显示屏：瀑布 + 频率标尺 + VFO 红标，一体装在内凹玻璃罩里。
 *
 * **单独成组件是性能要求**：`waterfall`/`fft` 每帧（20~30Hz）都变，若在主屏顶层
 * `collectAsState()`，每帧都会重组整个主屏（状态行 FlowRow、S 表弧、仪表、记忆格…）——
 * 中端机（荣耀）就是这么卡到把音频线程饿死的。收进子组件后每帧只重组这一块。
 */
@Composable
private fun SpectrumPanel(vm: MainViewModel, prefs: UiPrefs, spanHz: Long, vfoFreq: Long) {
    val waterfall by vm.waterfall.collectAsState()
    val fft by vm.fft.collectAsState()
    val m = LocalScreenMetrics.current
    DisplayBezel(
        modifier = Modifier.fillMaxWidth()
            .height(m.spectrumHeight(prefs.fftHeight, prefs.wfHeight))
            .padding(top = ScreenFit.PAGE_PAD_V.dp)
            .testTag("secSpectrum"),
    ) {
        Column(Modifier.fillMaxSize()) {
            Box(Modifier.weight(1f)) {
                WaterfallCanvas(
                    rows = waterfall, fft = fft,
                    theme = prefs.scopeTheme, floor = prefs.scopeFloor, ceil = prefs.scopeCeil,
                    fftFraction = prefs.fftHeight.toFloat() / (prefs.fftHeight + prefs.wfHeight).coerceAtLeast(1),
                    modifier = Modifier.fillMaxSize(),
                    onQsyFraction = { vm.qsy(it) },
                )
            }
            Box(Modifier.fillMaxWidth().height(1.dp).background(MrrcSurfaces.Hairline))
            // 标尺本身就标了 VFO（红线+三角），不再重复一行频率数字
            FreqScaleCanvas(vfoFreq, spanHz, Modifier.fillMaxWidth().height(15.dp))
        }
    }
}

/** FlowRow 里的小组件：统一 32dp 行高并垂直居中（与 PadBtn 对齐）。 */
@Composable
private fun StatusItem(content: @Composable () -> Unit) {
    Box(
        Modifier.height(LocalScreenMetrics.current.statusItemHeight)
            .wrapContentHeight(Alignment.CenterVertically)
    ) { content() }
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
        modifier.background(Brush.verticalGradient(listOf(Color(0xFF1C1C1C), Color(0xFF101010))), RoundedCornerShape(6.dp))
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
            val tickH = (h * 0.085f).coerceAtLeast(3f)
            val labelTop = baseY + tickH + h * 0.025f
            val railW = (h * 0.05f).coerceIn(3f, 7f)
            fun pt(t: Float) = Offset(SMeter.arcX(t, arcW), SMeter.arcY(t, baseY, ctrlY))

            // 轨道（暗弧）
            val track = Path().apply { moveTo(0f, baseY); quadraticBezierTo(arcW / 2f, ctrlY, arcW, baseY) }
            drawPath(track, Color.White.copy(alpha = 0.09f), style = Stroke(railW, cap = StrokeCap.Round))

            // 已走部分：Web S 表同款渐变（绿→黄→橙→红）沿弧铺开
            val frac = SMeter.fraction(raw)
            if (frac > 0.002f) {
                val sub = Path()
                val n = 40
                for (i in 0..n) {
                    val tt = frac * i / n
                    val q = pt(tt)
                    if (i == 0) sub.moveTo(q.x, q.y) else sub.lineTo(q.x, q.y)
                }
                drawPath(
                    sub,
                    Brush.horizontalGradient(
                        0f to Color(0xFF22C55E), 0.35f to Color(0xFF22C55E), 0.62f to Color(0xFFEAB308),
                        0.82f to Color(0xFFF59E0B), 1f to Color(0xFFEF4444), startX = 0f, endX = arcW,
                    ),
                    style = Stroke(railW, cap = StrokeCap.Round),
                )
            }

            // 刻度：8 个主刻度（标签处，长且亮）+ 8 个次刻度（短且暗）
            SMeter.MARKERS.forEachIndexed { i, m ->
                val q = pt(m / SMeter.RAW_MAX.toFloat())
                val major = i % 2 == 0
                drawLine(
                    Color.White.copy(alpha = if (major) 0.5f else 0.2f),
                    q, Offset(q.x, q.y + tickH * (if (major) 1f else 0.5f)),
                    strokeWidth = if (major) 1.3f else 1f,
                )
            }

            // 指针：红点压弧 + 白芯红线（细，不喧宾夺主）
            val np = pt(frac)
            drawLine(
                MrrcColors.Danger, np, Offset(np.x, np.y + tickH * 1.15f),
                strokeWidth = (railW * 0.4f).coerceIn(1.4f, 2.4f), cap = StrokeCap.Round,
            )
            drawCircle(MrrcColors.Danger, radius = railW * 0.62f, center = np)
            drawCircle(Color.White.copy(alpha = 0.85f), radius = railW * 0.22f, center = np)

            // 8 个标签 + 末尾 dB
            anchors.forEachIndexed { i, markIdx ->
                val q = pt(SMeter.MARKERS[markIdx] / SMeter.RAW_MAX.toFloat())
                val lay = layouts[i]
                drawText(lay, topLeft = Offset(
                    (q.x - lay.size.width / 2f).coerceIn(0f, (arcW - lay.size.width).coerceAtLeast(0f)),
                    labelTop,
                ))
            }
            val dbLay = layouts.last()
            drawText(dbLay, topLeft = Offset(w - dbLay.size.width, labelTop))

            // COMP 细条（面板第二行；发射时用 ALC 百分比），圆角 + 暗底
            val compH = (h * 0.05f).coerceAtLeast(3f)
            val compTop = h - compH
            drawRoundRect(
                Color.White.copy(alpha = 0.07f),
                topLeft = Offset(0f, compTop), size = Size(w, compH),
                cornerRadius = CornerRadius(compH / 2f, compH / 2f),
            )
            val filled = (if (transmitting) (alcPct / 100.0).toFloat() else 0f).coerceIn(0f, 1f) * w
            if (filled > compH) {
                drawRoundRect(
                    MrrcColors.Warning, topLeft = Offset(0f, compTop), size = Size(filled, compH),
                    cornerRadius = CornerRadius(compH / 2f, compH / 2f),
                )
            }

        // 读数：左上 S 值（大）、右上相对 S9 的 dB
            drawText(valueLayout, topLeft = Offset(0f, 0f))
            drawText(dbLayout, topLeft = Offset(w - dbLayout.size.width, 0f))
        }
        Text(
            if (transmitting) "COMP" else "",
            color = MrrcColors.TextMuted, fontSize = (labelFs * 0.78f).sp, fontFamily = MonoFont,
            modifier = Modifier.align(Alignment.BottomEnd),
        )
    }
}

@Composable
private fun MeterCell(label: String, value: Double, max: Float, color: Color, text: String, modifier: Modifier = Modifier) {
    Row(
        modifier.height(LocalScreenMetrics.current.meterCellHeight)
            .clip(MrrcSurfaces.KeyRadius)
            .background(MrrcSurfaces.Key)
            .border(1.dp, MrrcSurfaces.Stroke, MrrcSurfaces.KeyRadius)
            .padding(horizontal = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(label, color = MrrcColors.TextSecondary, fontSize = 9.sp, fontWeight = FontWeight.SemiBold,
            letterSpacing = 0.8.sp, modifier = Modifier.width(30.dp))
        Canvas(Modifier.weight(1f).height(6.dp)) {
            val r = size.height / 2f
            val frac = (value.toFloat() / max).coerceIn(0f, 1f)
            drawRoundRect(MrrcSurfaces.Inset, cornerRadius = CornerRadius(r, r))
            if (frac > 0f) {
                drawRoundRect(
                    brush = Brush.horizontalGradient(listOf(color.copy(alpha = 0.6f), color)),
                    size = Size((size.width * frac).coerceAtLeast(size.height), size.height),
                    cornerRadius = CornerRadius(r, r),
                )
            }
        }
        Spacer(Modifier.width(7.dp))
        Text(text, color = MrrcColors.TextPrimary, fontSize = 10.sp, fontFamily = MonoFont,
            textAlign = TextAlign.End, modifier = Modifier.width(46.dp))
    }
}

@Composable
private fun VolumeRow(afGain: Int, onCommit: (Int) -> Unit) {
    var local by remember { mutableStateOf<Float?>(null) }
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.fillMaxWidth().height(LocalScreenMetrics.current.sliderRowHeight),
    ) {
        Slider(
            value = local ?: afGain.toFloat(),
            onValueChange = { local = it },
            onValueChangeFinished = { local?.let { v -> onCommit(v.toInt()) }; local = null },
            valueRange = 0f..255f,
            modifier = Modifier.weight(1f).padding(start = 4.dp, end = 8.dp),
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

/**
 * 主频读数：内凹显示屏 + 琥珀辉光。
 * 末两位（10Hz）淡化缩小，模仿真机面板的读数层次；字号由调用方按可用宽度算好传进来。
 */
@Composable
private fun FreqText(hz: Long, fitSp: Float, modifier: Modifier = Modifier) {
    val s = fmtMhz(hz)
    val cut = s.lastIndexOf('.') + 1
    Box(modifier) {
        Text(
            buildAnnotatedString {
                withStyle(
                    SpanStyle(
                        color = MrrcColors.Accent, fontSize = fitSp.sp,
                        fontWeight = FontWeight.Bold, letterSpacing = 0.5.sp,
                    )
                ) { append(s.substring(0, cut)) }
                withStyle(
                    SpanStyle(
                        color = MrrcColors.Accent.copy(alpha = 0.5f), fontSize = (fitSp * 0.86f).sp,
                        fontWeight = FontWeight.Bold, letterSpacing = 0.5.sp,
                    )
                ) { append(s.substring(cut)) }
            },
            fontFamily = MonoFont,
            style = TextStyle(shadow = Shadow(color = MrrcColors.AccentGlow, blurRadius = 18f)),
            maxLines = 1,
            softWrap = false,
        )
    }
}

// ── 纯函数格式化（Web 同款）─────────────────────────────────────────

/** Web 主频显示：2 位 MHz . 3 位 kHz . 2 位十Hz（例 07.013.50）。 */
internal fun fmtMhz(hz: Long): String =
    "%02d.%03d.%02d".format(Locale.US, hz / 1_000_000, (hz / 1000) % 1000, (hz % 1000) / 10)


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
