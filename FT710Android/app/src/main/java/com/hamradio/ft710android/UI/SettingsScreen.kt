package com.hamradio.ft710android.UI

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.hamradio.ft710android.BuildConfig
import com.hamradio.ft710android.Data.SettingsStore
import com.hamradio.ft710android.Spectrum.Palettes
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.launch
import java.util.Locale

/**
 * 设置页 —— 逐项对齐手机端 Web 菜单：Scope 面板（SPAN/SPD/配色/Floor/Ceil/高度）、
 * RF PWR/RF Gain/Mic Gain/🎙 Vol/🔊 Vol、NR/NB 电平、常亮/后台接收开关、机型与版本、
 * 连接设置（浏览器）与 🐞 支持入口、重连/退出。
 */
@Composable
fun SettingsScreen(
    vm: MainViewModel,
    settings: SettingsStore,
    prefs: UiPrefs,
    onBack: () -> Unit,
    onLoggedOut: () -> Unit,
) {
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    // 订阅 version 版本号触发重组，然后读 vm.state.* 拿到最新值
    vm.version.collectAsState()
    val state = vm.state
    val caps by vm.caps.collectAsState()
    val displayName by vm.displayName.collectAsState()
    var showCloud by remember { mutableStateOf(false) }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        // 返回主屏（也有系统返回键兜底，见 RootScreen.BackHandler）
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Box(
                Modifier.border(1.dp, MrrcColors.Border, RoundedCornerShape(6.dp))
                    .clickable { onBack() }
                    .padding(horizontal = 12.dp, vertical = 6.dp),
            ) { Text("← 返回", color = MrrcColors.Accent, fontSize = 13.sp) }
            Spacer(Modifier.width(10.dp))
            Text("设置", style = MaterialTheme.typography.titleMedium)
        }
        Spacer(Modifier.height(6.dp))
        Text(
            "${displayName.ifEmpty { caps.displayName }} · 客户端 v${BuildConfig.VERSION_NAME}",
            style = MaterialTheme.typography.bodySmall,
        )
        if (!caps.verified) {
            Text(
                if (caps.txGated) "实验性机型 — 发射已禁用" else "实验性机型 — 发射已由环境变量放行",
                color = MrrcColors.Warning, fontSize = 11.sp, modifier = Modifier.padding(top = 4.dp),
            )
        }
        Spacer(Modifier.height(10.dp))

        // ── Scope（S1）───────────────────────────────────────────
        Section("频谱 Scope")
        Text("SPAN", fontSize = 11.sp, color = MrrcColors.TextSecondary, modifier = Modifier.fillMaxWidth())
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            caps.spans.forEach { sp ->
                FilterChip(
                    selected = state.scopeSpan == sp.idx,
                    onClick = { vm.setScopeSpan(sp.idx) },
                    label = { Text(sp.name, fontSize = 10.sp) },
                )
            }
        }
        Spacer(Modifier.height(4.dp))
        Text("SPD", fontSize = 11.sp, color = MrrcColors.TextSecondary, modifier = Modifier.fillMaxWidth())
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            caps.speeds.forEachIndexed { idx, name ->
                FilterChip(
                    selected = state.scopeSpeed == idx,
                    onClick = { vm.setScopeSpeed(idx) },
                    label = { Text(name, fontSize = 10.sp) },
                )
            }
        }
        Spacer(Modifier.height(4.dp))
        Text("配色", fontSize = 11.sp, color = MrrcColors.TextSecondary, modifier = Modifier.fillMaxWidth())
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            Palettes.NAMES.forEach { name ->
                FilterChip(
                    selected = prefs.scopeTheme == name,
                    onClick = { scope.launch { settings.putScopeTheme(name) } },
                    label = { Text(name, fontSize = 10.sp) },
                )
            }
        }
        PrefSlider("Floor", prefs.scopeFloor.toFloat(), 0f..200f) { scope.launch { settings.putScopeFloor(it) } }
        PrefSlider("Ceil", prefs.scopeCeil.toFloat(), 50f..255f) { scope.launch { settings.putScopeCeil(it) } }
        PrefSlider("Spec H", prefs.fftHeight.toFloat(), 20f..120f) { scope.launch { settings.putFftHeight(it) } }
        PrefSlider("WF H", prefs.wfHeight.toFloat(), 30f..200f) { scope.launch { settings.putWfHeight(it) } }

        // ── 音量与增益（S2/S3；全部松手提交：D3）──────────────────
        Section("音量 / 增益")
        PrefSlider("🔊 Vol", prefs.afVol.toFloat(), 0f..255f) { scope.launch { settings.putAfVol(it) } }
        PrefSlider("🎙 Vol", prefs.micVol.toFloat(), 0f..200f) { scope.launch { settings.putMicVol(it) } }
        PrefSlider("RF PWR", state.rfPower.toFloat(), 5f..100f) { vm.setRfPower(it) }
        PrefSlider("RF Gain", (state.rfGain * 100f / 255f), 0f..100f) { vm.setRfGain((it * 255f / 100f).toInt()) }
        PrefSlider("Mic Gain", state.micGain.toFloat(), 0f..100f) { g ->
            vm.setMicGain(g)
            scope.launch { settings.putMicGain(g) }
        }
        PrefSlider("NR Level", state.nrLevel.toFloat(), 1f..15f) { vm.setNrLevel(it) }
        PrefSlider("NB Level", state.nbLevel.toFloat(), 0f..10f) { vm.setNbLevel(it) }

        // ── 本地开关（M6/L1）──────────────────────────────────────
        Section("本机")
        SwitchRow("保持屏幕常亮", prefs.keepScreenOn) { scope.launch { settings.putKeepScreenOn(it) } }
        SwitchRow("后台接收（退后台继续听）", prefs.backgroundRx) { scope.launch { settings.putBackgroundRx(it) } }

        // ── 入口（S7/S9）─────────────────────────────────────────
        Section("入口")
        LinkRow("接入云端（Cloud Hub）…") { showCloud = true }
        LinkRow("连接设置（浏览器）") {
            context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(vm.baseUrlForUi())))
        }
        LinkRow("🐞 遇到问题（浏览器）") {
            context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(vm.baseUrlForUi() + "/support.html")))
        }

        Spacer(Modifier.height(16.dp))
        TextButton(onClick = { vm.connectionManager.reconnectAll() }) { Text("重连") }
        TextButton(onClick = { onBack() }) { Text("返回主屏") }
        Button(onClick = {
            scope.launch { vm.logout(); settings.clearCredentials(); onLoggedOut() }
        }) { Text("退出登录") }
        Spacer(Modifier.height(24.dp))
    }

    if (showCloud) CloudHubDialog(vm) { showCloud = false }
}

@Composable
private fun Section(title: String) {
    Text(
        title, color = MrrcColors.Accent, fontSize = 13.sp, fontWeight = FontWeight.Bold,
        modifier = Modifier.fillMaxWidth().padding(top = 14.dp, bottom = 4.dp),
    )
}

@Composable
private fun SwitchRow(label: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(label, fontSize = 13.sp, modifier = Modifier.weight(1f))
        Switch(checked = checked, onCheckedChange = onChange)
    }
}

@Composable
private fun LinkRow(label: String, onClick: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 3.dp).clickable { onClick() }
            .border(1.dp, MrrcColors.Border, RoundedCornerShape(6.dp))
            .padding(horizontal = 10.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(label, fontSize = 13.sp, color = MrrcColors.TextPrimary)
    }
}

/** 拖动中显示本地值、松手才 onCommit（D3 的“松手提交”统一实现）。 */
@Composable
private fun PrefSlider(label: String, value: Float, range: ClosedFloatingPointRange<Float>, onCommit: (Int) -> Unit) {
    var local by remember { mutableStateOf<Float?>(null) }
    val shown = local ?: value
    Row(Modifier.fillMaxWidth().padding(top = 2.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(label, fontSize = 12.sp, color = MrrcColors.TextSecondary, modifier = Modifier.width(64.dp))
        Slider(
            value = shown.coerceIn(range.start, range.endInclusive),
            onValueChange = { local = it },
            onValueChangeFinished = {
                local?.let { onCommit(it.toInt()) }
                local = null
            },
            valueRange = range,
            modifier = Modifier.weight(1f),
            colors = SliderDefaults.colors(
                thumbColor = MrrcColors.Accent,
                activeTrackColor = MrrcColors.Accent,
                inactiveTrackColor = MrrcColors.Border,
            ),
        )
        Text(
            "%.0f".format(Locale.US, shown),
            fontSize = 11.sp, color = MrrcColors.Accent, modifier = Modifier.width(34.dp),
        )
    }
}
