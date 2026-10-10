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
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.graphics.Path

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
            // ── 顶栏：一行"工具+状态"（全宽）+ 下面主频显示屏与 S 表 ──────────
            // 用户 2026-10-07 定案：频率上方**只要一行**，RX/TX 等状态紧跟波段·模式之后，
            // 所有图标与信息都在这一行。它必须**横跨页宽**（344dp）——若塞进显示屏内部
            // 只有 bezelContentWidth≈167dp，装不下 ~280dp 的内容。
            Row(
                Modifier.fillMaxWidth().height(m.headerRowHeight).testTag("secToolRow"),
                horizontalArrangement = Arrangement.spacedBy(3.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                // 菜单在最左
                HeaderIconButton(HeaderIconKind.MENU, "设置") { onOpenSettings() }
                // 信息组：波段·模式 + 全部状态，紧跟在 ☰ 之后（用户要求这个顺序）。
                // 外层 weight(1f) 吃掉剩余宽度 → 右侧动作区自然贴右边；
                // 组内只有"波段·模式"带 weight(fill=false)，**空间不够时它先让路出省略号**，
                // 而不是把 ⏺⛶⏻/VFO 挤出屏幕。
                Row(
                    Modifier.weight(1f),
                    horizontalArrangement = Arrangement.spacedBy(3.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        "${state.bandName.ifEmpty { "—" }} · ${state.modeName.ifEmpty { "—" }}",
                        color = MrrcColors.TextMuted, fontSize = 9.sp, letterSpacing = 0.2.sp,
                        maxLines = 1, softWrap = false, overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f, fill = false),
                    )
                    Text(
                        when (state.txStatus) { 2 -> "TUNE"; 1 -> "TX"; else -> "RX" },
                        color = if (state.isTransmitting) MrrcColors.Danger else MrrcColors.TextSecondary,
                        fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 0.3.sp,
                        maxLines = 1,
                    )
                    val pk = vm.txPeak()
                    Text(
                        if (state.txStatus != 0) "TX pk:$pk" else "↓${rxKbps}↑${txKbps}",
                        color = if (state.txStatus != 0 && pk < 400) MrrcColors.Warning else MrrcColors.TextMuted,
                        fontSize = 8.5.sp, fontFamily = MonoFont, maxLines = 1,
                    )
                    if (rec.recording) {
                        Text(
                            fmtSeconds(rec.duration),
                            color = Color(0xFFFF6B6B), fontSize = 9.sp, fontFamily = MonoFont,
                            fontWeight = FontWeight.Bold, maxLines = 1,
                        )
                    }
                    if (listenOnly) {
                        Text("只读", color = MrrcColors.Accent, fontSize = 9.sp, fontWeight = FontWeight.Bold,
                            maxLines = 1, modifier = Modifier.clickable {
                                vm.showNotice("只读登录（listen-only）：发射与设备设置已被服务端禁用")
                            })
                    }
                    if (state.rxAudioSilent) {
                        Text("无声", color = MrrcColors.Danger, fontSize = 9.sp, fontWeight = FontWeight.Bold,
                            maxLines = 1, modifier = Modifier.clickable {
                                vm.showNotice("RX 音频持续全零——电台 USB 音频可能卡死，请重启电台或重插 USB")
                            })
                    }
                    // 串口状态点（点按出说明）
                    Box(
                        Modifier.size(7.dp)
                            .background(if (connected) MrrcColors.Success else MrrcColors.TextMuted, CircleShape)
                            .clickable {
                                vm.showNotice(
                                    if (connected) "电台串口已连接" else "电台串口未连接（服务端与电台之间）"
                                )
                            },
                    )
                }
                // 右侧动作区：录音 / 全屏 / 电源（全部 Canvas 自绘）+ VFO 读数
                if (recAvailable && !listenOnly) {
                    HeaderIconButton(
                        HeaderIconKind.RECORD,
                        if (rec.recording) "停止录音（录音中）" else "录音",
                        danger = rec.recording, filled = rec.recording,
                    ) { showRecPanel = true }
                }
                HeaderIconButton(HeaderIconKind.FULLSCREEN, "全屏", active = fullscreen) {
                    onToggleFullscreen()
                }
                HeaderIconButton(
                    HeaderIconKind.POWER,
                    if (userOff) "连接电台" else "断开电台",
                    danger = userOff,
                ) { if (userOff) vm.reconnect() else vm.disconnect() }
                Text(
                    "VFO-${state.activeVfo}",
                    color = MrrcColors.Accent, fontSize = 9.sp, fontWeight = FontWeight.Bold,
                    letterSpacing = 0.2.sp, maxLines = 1, softWrap = false,
                    modifier = Modifier
                        .clip(RoundedCornerShape(4.dp))
                        .clickable { vm.sendSet("vfo", if (state.activeVfo == "A") "B" else "A") }
                        .padding(horizontal = 2.dp),
                )
            }

            // ── 主频显示屏 + S 表（等高，尺寸只看屏幕宽度）──────────────
            BoxWithConstraints(Modifier.fillMaxWidth()) {
                val meterH = m.headerHeight
                // 等高靠**显式高度**，不能用 IntrinsicSize：子项里的 BoxWithConstraints 是
                // SubcomposeLayout，问它 intrinsic 会抛异常 = 启动即崩（v1.1.16 事故）
                Row(Modifier.fillMaxWidth().height(meterH).testTag("secHeader")) {
                    DisplayBezel(
                        modifier = Modifier.weight(1f).fillMaxHeight()
                            .clickable { showFreqInput = true }
                            .testTag("freqBezel"),
                        contentAlignment = Alignment.Center,
                    ) {
                        // 显示屏里只有主频（工具/状态都在上面那一行），字号吃满宽高两个约束。
                        // ⚠️ `BoxWithConstraints` 的默认 contentAlignment 是 **TopStart**：
                        // 不显式给 Center，主频就会贴在左上角（v1.1.26~28 的实际状态，
                        // 用户报"频率上下居中"）。外面再包一层 Box(Center) 也没用——
                        // 子项 fillMaxSize 会把它填满，居中无从生效。
                        BoxWithConstraints(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                            val byWidth = (maxWidth.value - 8f) / 6.3f
                            val byHeight = (maxHeight.value - 4f) / 1.32f
                            val fit = minOf(byWidth, byHeight).coerceIn(16f, 62f)
                            FreqText(hz = state.activeFrequency, fitSp = fit)
                        }
                    }
                    Spacer(Modifier.width(ScreenFit.BEZEL_GAP.dp))
                    SmeterArc(
                        raw = state.sMeter, sUnit = state.sUnit, levelDb = state.sMeterDbm,
                        modifier = Modifier.width(m.meterWidth).height(meterH).align(Alignment.CenterVertically),
                    )
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
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(m.memoryInnerGap)) {
                        for (col in 0 until m.memoryColumns) {
                            val index = row * m.memoryColumns + col
                            val ch = mem.getOrNull(index)
                            val memShape = RoundedCornerShape(10.dp)
                            val memInteraction = remember(index) { MutableInteractionSource() }
                            val memPressed by memInteraction.collectIsPressedAsState()
                            Box(
                                Modifier.weight(1f).height(m.memoryCellHeight).testTag("memCell$index")
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
                                // 只显示频率（用户 2026-10-07 定：标签不要了，太挤会变形）。
                                // 单行之后每格只剩一个字符串，字号可以放开到 11sp（原来两行只有 7.5sp）。
                                // 仍然强制 maxLines=1 + softWrap=false + Ellipsis：
                                // 格高固定，一换行整格就顶变形。
                                val text = if (ch != null) "%.3f".format(Locale.US, ch.freq / 1e6)
                                             else "M${index + 1}"
                                Text(
                                    text,
                                    color = if (ch != null) MrrcColors.Accent else MrrcColors.TextMuted,
                                    fontSize = m.memoryFreqFont(text).sp,
                                    fontFamily = MonoFont,
                                    fontWeight = if (ch != null) FontWeight.SemiBold else FontWeight.Normal,
                                    maxLines = 1, softWrap = false,
                                    overflow = TextOverflow.Ellipsis,
                                )
                            }
                        }
                        // 记忆管理入口：图标，不占文字（原先藏在被删掉的分区标题里）
                        if (row == 0) {
                            Box(
                                Modifier.width(m.memoryManageWidth).height(m.memoryCellHeight)
                                    .clip(RoundedCornerShape(10.dp))
                                    .background(MrrcSurfaces.Key)
                                    .border(1.dp, MrrcSurfaces.Stroke, RoundedCornerShape(10.dp))
                                    .clickable { showMemManager = true },
                                contentAlignment = Alignment.Center,
                            ) {
                                Text("⋯", color = MrrcColors.Accent, fontSize = 15.sp, fontWeight = FontWeight.Bold)
                            }
                        } else {
                            Spacer(Modifier.width(m.memoryManageWidth))
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
 * `collectAsState()`，每帧都会重组整个主屏（顶栏、S 表弧、仪表、记忆格…）——
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

/** 顶栏工具行的图标种类。 */
internal enum class HeaderIconKind { MENU, FULLSCREEN, POWER, RECORD }

/**
 * 顶栏工具行的图标按钮：**Canvas 自绘**，不用字体字符。
 *
 * 为什么不用 `☰ ⛶ ⏻`：这些符号在不少机型上没有对应字形，会走字体回退，
 * 渲染出来粗细不一、笔画缺失（用户报的"关闭 icon 变形"）。自绘的好处是
 * 线宽随图标尺寸按比例算，任何 dpi/字体设置下都一致。
 *
 * 尺寸跟着屏幕档位走（[ScreenFit.headerIconTap] 点击区 / [ScreenFit.headerIconGlyph] 图形），
 * 与同一行的 9sp 文字视觉齐平；`contentDescription` 兼顾无障碍与测试可查。
 */
@Composable
private fun HeaderIconButton(
    kind: HeaderIconKind,
    description: String,
    active: Boolean = false,
    danger: Boolean = false,
    filled: Boolean = false,
    onClick: () -> Unit,
) {
    val m = LocalScreenMetrics.current
    val color = when {
        danger -> MrrcColors.Danger
        active -> MrrcColors.Accent
        else -> MrrcColors.TextSecondary
    }
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    Box(
        Modifier.size(m.headerIconTap)
            .clip(RoundedCornerShape(6.dp))
            .clickable(interactionSource = interaction, indication = ripple(), onClick = onClick)
            .semantics { contentDescription = description }
            .graphicsLayer { alpha = if (pressed) 0.55f else 1f },
        contentAlignment = Alignment.Center,
    ) {
        Canvas(Modifier.size(m.headerIconGlyph)) {
            val g = size.width
            // 线宽按图形尺寸的比例走：小图标不会糊成一团，大图标不会细成发丝
            val st = (g * 0.13f).coerceAtLeast(1.1f)
            val i = st / 2f
            when (kind) {
                HeaderIconKind.MENU -> {
                    val gap = g * 0.27f
                    for (k in -1..1) {
                        val y = g / 2f + k * gap
                        drawLine(color, Offset(i, y), Offset(g - i, y), strokeWidth = st, cap = StrokeCap.Round)
                    }
                }
                HeaderIconKind.FULLSCREEN -> {
                    // 四角括号（比"方框"更像全屏，且在 10dp 下不糊）
                    val arm = g * 0.34f
                    val l = i; val r = g - i; val t = i; val b = g - i
                    drawLine(color, Offset(l, t), Offset(l + arm, t), st, cap = StrokeCap.Round)
                    drawLine(color, Offset(l, t), Offset(l, t + arm), st, cap = StrokeCap.Round)
                    drawLine(color, Offset(r, t), Offset(r - arm, t), st, cap = StrokeCap.Round)
                    drawLine(color, Offset(r, t), Offset(r, t + arm), st, cap = StrokeCap.Round)
                    drawLine(color, Offset(l, b), Offset(l + arm, b), st, cap = StrokeCap.Round)
                    drawLine(color, Offset(l, b), Offset(l, b - arm), st, cap = StrokeCap.Round)
                    drawLine(color, Offset(r, b), Offset(r - arm, b), st, cap = StrokeCap.Round)
                    drawLine(color, Offset(r, b), Offset(r, b - arm), st, cap = StrokeCap.Round)
                }
                HeaderIconKind.POWER -> {
                    // 标准电源符号：顶部留缺口的圆环 + 中间竖线
                    // 0° = 三点钟方向、顺时针；缺口开在正上方（-120°~-60°）
                    drawArc(
                        color = color, startAngle = -60f, sweepAngle = 300f, useCenter = false,
                        topLeft = Offset(i, i), size = Size(g - st, g - st),
                        style = Stroke(width = st, cap = StrokeCap.Round),
                    )
                    drawLine(color, Offset(g / 2f, i * 0.6f), Offset(g / 2f, g * 0.52f),
                        strokeWidth = st, cap = StrokeCap.Round)
                }
                HeaderIconKind.RECORD -> {
                    // 录音：圆环（待机）→ 实心（录音中，配 danger 红色）。
                    // 同样是自绘：`⏺`(U+23FA) 在不少机型没有字形，会走字体回退而变形
                    drawCircle(color, radius = g * 0.34f, style = Stroke(width = st))
                    if (filled) drawCircle(color, radius = g * 0.19f)
                }
            }
        }
    }
}

/**
 * S 表 —— 按用户给的 `smeter-s7.svg` 参考重画（2026-10-07）。
 *
 * 参考图要点，逐条对上：
 *  - **弧线两段两色**：S1–S9 段近白，+20/+40/+60 段红（参考图 `#e01b1b` → 用主题
 *    [MrrcColors.Danger]），分界正是参考图注释里的 "S9+10 mark" = `SMeter.MARKERS[9]`
 *  - **刻度从弧线向外（向上）辐射**，两端略外倾（模仿半径方向）；主刻度长、次刻度短；
 *    S 区刻度近白、dB 区刻度红
 *  - **数字在刻度上方**并跟着弧度走：`1 3 5 7 9 +20 +40 +60`
 *  - **指针**：一条细线从弧上当前位置垂到面板底部（略外倾），不画彩色填充带
 *
 * 与参考图**有意不同**（用户明确要求）：
 *  - 参考图把 `S7` 放在 lime 黄圆角底牌（`#c9d92b`）里 → **去掉底牌**，只留文字
 *  - 文字用本 App 的橘色 [MrrcColors.Accent]，与主界面色调一致
 * 参考图左上/右上的大 `S` 与 `dB` 在这里由**实际读数**取代（S 值 + 相对 S9 的 dB），
 * 占位相同但信息更多。ALC/COMP 由仪表卡那一格负责，此处不重复。
 */
@Composable
private fun SmeterArc(
    raw: Int,
    sUnit: String,
    levelDb: Double,
    modifier: Modifier = Modifier,
) {
    BoxWithConstraints(
        modifier.background(MrrcSurfaces.Inset, RoundedCornerShape(6.dp))
            .border(1.dp, MrrcSurfaces.Stroke, RoundedCornerShape(6.dp))
            .padding(horizontal = 5.dp, vertical = 4.dp)
    ) {
        // 缩放基准取短边：手机与平板共用一份代码
        val base = minOf(maxWidth.value, maxHeight.value)
        val labelFs = (base * 0.095f).coerceIn(7.5f, 11.5f)
        val readFs = (base * 0.155f).coerceIn(11f, 20f)
        val measurer = rememberTextMeasurer(cacheSize = 16)
        // 数字近白（参考图是纯白）但压一档透明度，让 S 值读数当主角
        val labelStyle = TextStyle(
            color = Color.White.copy(alpha = 0.78f), fontSize = labelFs.sp, fontFamily = MonoFont,
        )
        val labels = SMeter.LABELS.map { if (it.startsWith("S")) it.drop(1) else it }
        val anchors = SMeter.MARKERS.indices.filter { it % 2 == 0 }
        val layouts = remember(labelFs) { labels.map { measurer.measure(AnnotatedString(it), labelStyle) } }
        val valueStyle = TextStyle(
            color = MrrcColors.Accent, fontSize = readFs.sp,
            fontFamily = MonoFont, fontWeight = FontWeight.Bold,
        )
        val dbStyle = TextStyle(
            color = MrrcColors.TextSecondary, fontSize = (readFs * 0.6f).sp, fontFamily = MonoFont,
        )
        val valueLayout = remember(readFs, sUnit) {
            measurer.measure(AnnotatedString(if (sUnit.isEmpty()) "S0" else sUnit), valueStyle)
        }
        val dbLayout = remember(readFs, levelDb) {
            measurer.measure(AnnotatedString("%.0f dB".format(Locale.US, levelDb)), dbStyle)
        }

        Canvas(Modifier.fillMaxSize()) {
            val w = size.width
            val h = size.height
            val readH = maxOf(valueLayout.size.height, dbLayout.size.height).toFloat()
            val labelH = layouts[0].size.height.toFloat()
            val tickH = (h * 0.085f).coerceAtLeast(3f)
            val gap = labelFs * 0.25f

            // 竖向分配（自上而下）：读数 → 数字 → 刻度 → 弧 → 指针垂落区
            val arcPeakY = readH + gap + labelH + tickH
            val needleRoom = h * 0.16f
            // 弧度比参考图（rise/width≈0.118）稍浅，小面板上更耐看，且不挤掉指针区
            val rise = minOf(w * 0.10f, (h - needleRoom - arcPeakY).coerceAtLeast(2f))
            val baseY = arcPeakY + rise              // 弧两端的 y（最低点）
            val ctrlY = 2f * arcPeakY - baseY        // 让贝塞尔顶点正好落在 arcPeakY
            fun pt(t: Float) = Offset(SMeter.arcX(t, w), SMeter.arcY(t, baseY, ctrlY))

            val strokeW = (h * 0.035f).coerceIn(2.2f, 6f)
            val splitT = SMeter.MARKERS[9] / SMeter.RAW_MAX.toFloat()

            fun arc(from: Float, to: Float): Path = Path().apply {
                val n = 36
                for (k in 0..n) {
                    val q = pt(from + (to - from) * k / n)
                    if (k == 0) moveTo(q.x, q.y) else lineTo(q.x, q.y)
                }
            }
            // 两段弧：S 区近白、dB 区红
            drawPath(arc(0f, splitT), Color.White.copy(alpha = 0.85f),
                style = Stroke(strokeW, cap = StrokeCap.Round))
            drawPath(arc(splitT, 1f), MrrcColors.Danger,
                style = Stroke(strokeW, cap = StrokeCap.Round))

            // 刻度：向外（向上）辐射，两端外倾；S 区近白 / dB 区红
            SMeter.MARKERS.forEachIndexed { i, m ->
                val t = m / SMeter.RAW_MAX.toFloat()
                val q = pt(t)
                val major = i % 2 == 0
                val len = tickH * (if (major) 1f else 0.55f)
                val tilt = (t - 0.5f) * len * 0.4f
                drawLine(
                    if (i <= 8) Color.White.copy(alpha = if (major) 0.72f else 0.34f)
                    else MrrcColors.Danger.copy(alpha = if (major) 0.95f else 0.5f),
                    q, Offset(q.x + tilt, q.y - len),
                    strokeWidth = if (major) 1.3f else 1f,
                    cap = StrokeCap.Round,
                )
            }

            // 数字：刻度上方，跟着弧度走
            anchors.forEachIndexed { i, markIdx ->
                val t = SMeter.MARKERS[markIdx] / SMeter.RAW_MAX.toFloat()
                val q = pt(t)
                val lay = layouts[i]
                drawText(lay, topLeft = Offset(
                    (q.x - lay.size.width / 2f).coerceIn(0f, (w - lay.size.width).coerceAtLeast(0f)),
                    q.y - tickH - lay.size.height - 1f,
                ))
            }

            // 指针：从弧上当前位置垂到底部，略外倾
            val frac = SMeter.fraction(raw)
            val np = pt(frac)
            drawLine(
                MrrcColors.Accent, np,
                Offset(np.x + (frac - 0.5f) * h * 0.10f, h),
                strokeWidth = (strokeW * 0.42f).coerceIn(1.2f, 2.2f),
                cap = StrokeCap.Round,
            )

            // 读数：左上 S 值（橘、粗、**无底牌**），右上相对 S9 的 dB（灰）
            drawText(valueLayout, topLeft = Offset(0f, 0f))
            drawText(dbLayout, topLeft = Offset(w - dbLayout.size.width, 0f))
        }
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
