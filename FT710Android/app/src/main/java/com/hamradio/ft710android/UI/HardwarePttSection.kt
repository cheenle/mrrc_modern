package com.hamradio.ft710android.UI

import android.accessibilityservice.AccessibilityServiceInfo
import android.content.Context
import android.content.Intent
import android.provider.Settings
import android.view.KeyEvent
import android.view.accessibility.AccessibilityManager
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
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.hamradio.ft710android.App.PttAccessibilityService
import com.hamradio.ft710android.Data.SettingsStore
import com.hamradio.ft710android.PTT.HardwarePtt
import com.hamradio.ft710android.PTT.HardwarePttMode
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch

/**
 * 设置页「硬件 PTT」卡片：侧键 / 音量键 / 蓝牙手咪。
 *
 * 这里只做配置与学习，真正的按键处理在 `PTT/HardwarePtt.kt`（前台 `MainActivity.onKeyDown/onKeyUp`
 * + 后台 `PttAccessibilityService`）。学习期间路由处于"只识别、绝不发射"状态。
 */
@Composable
fun HardwarePttSection(
    settings: SettingsStore,
    prefs: UiPrefs,
    scope: CoroutineScope,
    onNotice: (String) -> Unit,
) {
    val context = LocalContext.current
    val hw = prefs.hwPtt
    val a11yOn = rememberAccessibilityEnabled()
    var showLearn by remember { mutableStateOf(false) }

    Section("硬件 PTT")
    SwitchRowLocal("侧键 / 音量键发射", hw.enabled) { on ->
        scope.launch { settings.putHwPttEnabled(on) }
        if (on && !hw.hasKey) showLearn = true // 先开却没有键：直接把学习页弹出来
    }

    Row(
        Modifier.fillMaxWidth().padding(vertical = 3.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text("按键", fontSize = 13.sp, modifier = Modifier.weight(1f))
        Text(
            keyLabel(hw.keyCode, hw.scanCode),
            color = if (hw.hasKey) MrrcColors.Accent else MrrcColors.TextMuted,
            fontSize = 12.sp,
        )
        Spacer(Modifier.width(8.dp))
        Box(
            Modifier.border(1.dp, MrrcColors.Border, RoundedCornerShape(6.dp))
                .clickable { showLearn = true }
                .padding(horizontal = 10.dp, vertical = 5.dp),
        ) { Text(if (hw.hasKey) "重新学习" else "学习", color = MrrcColors.Accent, fontSize = 12.sp) }
    }

    Text("模式", fontSize = 11.sp, color = MrrcColors.TextSecondary, modifier = Modifier.fillMaxWidth())
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        FilterChip(
            selected = hw.mode == HardwarePttMode.Hold,
            onClick = { scope.launch { settings.putHwPttMode(HardwarePttMode.Hold) } },
            label = { Text("按住说话", fontSize = 10.sp) },
        )
        FilterChip(
            selected = hw.mode == HardwarePttMode.Tap,
            onClick = { scope.launch { settings.putHwPttMode(HardwarePttMode.Tap) } },
            label = { Text("点按切换", fontSize = 10.sp) },
        )
    }
    if (hw.mode == HardwarePttMode.Tap) {
        Text(
            "点按模式：离开界面/息屏后可能再也解不开，只在对端设了 TX 超时兜底时使用",
            color = MrrcColors.Warning, fontSize = 10.sp, modifier = Modifier.padding(top = 2.dp),
        )
    }

    // 无障碍只能由用户在系统里开，这里如实显示状态并跳转，绝不在 UI 上假装已开启
    Row(
        Modifier.fillMaxWidth().padding(vertical = 4.dp).clickable {
            runCatching {
                context.startActivity(
                    Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                )
            }
        },
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text("后台与息屏生效", fontSize = 13.sp)
            Text(
                if (a11yOn) "无障碍服务已开启 ✓" else "未开启 — 目前只有 App 在前台时认这个键",
                color = if (a11yOn) MrrcColors.Accent else MrrcColors.TextMuted,
                fontSize = 10.sp,
            )
        }
        Box(
            Modifier.border(1.dp, MrrcColors.Border, RoundedCornerShape(6.dp))
                .padding(horizontal = 10.dp, vertical = 5.dp),
        ) { Text(if (a11yOn) "系统设置" else "去开启", color = MrrcColors.Accent, fontSize = 12.sp) }
    }

    SwitchRowLocal("按下时唤醒并切到前台", hw.wakeAndFocus) { on ->
        scope.launch { settings.putHwPttWake(on) }
    }

    Text(
        "支持侧键、音量键、蓝牙手咪与车机方向盘键；键值由「学习」现抓，不认固定码表。",
        color = MrrcColors.TextMuted, fontSize = 10.sp, modifier = Modifier.padding(top = 2.dp),
    )

    if (showLearn) {
        LearnKeyDialog(
            onDismiss = {
                showLearn = false
                HardwarePtt.cancelLearn()
            },
            onConfirm = { keyCode, scanCode ->
                scope.launch {
                    settings.putHwPttKey(keyCode, scanCode)
                    settings.putHwPttEnabled(true)
                }
                showLearn = false
                onNotice("硬件 PTT 已绑定：${keyLabel(keyCode, scanCode)}（按一下侧键试试）")
            },
        )
    }
}

@Composable
private fun LearnKeyDialog(onDismiss: () -> Unit, onConfirm: (Int, Int) -> Unit) {
    val learned by HardwarePtt.learned.collectAsState()
    val owner = LocalLifecycleOwner.current
    // 打开即进入「只识别、不发射」状态。退到后台必须退出学习：否则无障碍服务会在**别的 App 里**
    // 接着吞掉下一个按键（它收到的键比应用早），用户莫名其妙丢一个键。
    DisposableEffect(owner) {
        HardwarePtt.learn()
        val observer = LifecycleEventObserver { _, e ->
            when (e) {
                Lifecycle.Event.ON_RESUME -> HardwarePtt.learn()
                Lifecycle.Event.ON_STOP -> HardwarePtt.cancelLearn()
                else -> Unit
            }
        }
        owner.lifecycle.addObserver(observer)
        onDispose {
            owner.lifecycle.removeObserver(observer)
            HardwarePtt.cancelLearn()
        }
    }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("学习 PTT 按键") },
        text = {
            Column {
                Text(
                    "现在按一下要当 PTT 的物理键：侧键、音量键、蓝牙手咪或车机方向盘键。",
                    fontSize = 13.sp,
                )
                Spacer(Modifier.height(10.dp))
                Text(
                    learned?.let { "识别到：${keyLabel(it.keyCode, it.scanCode)}" } ?: "等待按键…",
                    color = if (learned != null) MrrcColors.Accent else MrrcColors.TextMuted,
                    fontSize = 13.sp,
                )
                Spacer(Modifier.height(6.dp))
                Text("学习期间不会发射。返回键/主页键不会被绑定。", color = MrrcColors.TextMuted, fontSize = 10.sp)
            }
        },
        confirmButton = {
            TextButton(
                enabled = learned != null,
                onClick = { learned?.let { onConfirm(it.keyCode, it.scanCode) } },
            ) {
                Text("确定", color = if (learned != null) MrrcColors.Accent else MrrcColors.TextMuted)
            }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消", color = MrrcColors.TextSecondary) } },
    )
}

/** KEYCODE_F1 · 扫描码 87 —— 两者都显示，现场排查"按了没反应"时一眼能对。 */
internal fun keyLabel(keyCode: Int, scanCode: Int): String {
    if (keyCode == 0) return "未设置"
    val name = runCatching { KeyEvent.keyCodeToString(keyCode) }.getOrNull() ?: keyCode.toString()
    return "$name·$scanCode"
}

/** 无障碍服务是否真的被系统启用（不猜、不信本地开关）。 */
@Composable
private fun rememberAccessibilityEnabled(): Boolean {
    val context = LocalContext.current
    val owner = LocalLifecycleOwner.current
    var on by remember { mutableStateOf(isPttServiceEnabled(context)) }
    DisposableEffect(owner) {
        val observer = LifecycleEventObserver { _, e ->
            if (e == Lifecycle.Event.ON_RESUME) on = isPttServiceEnabled(context)
        }
        owner.lifecycle.addObserver(observer)
        on = isPttServiceEnabled(context)
        onDispose { owner.lifecycle.removeObserver(observer) }
    }
    return on
}

private fun isPttServiceEnabled(context: Context): Boolean {
    val am = context.getSystemService(AccessibilityManager::class.java) ?: return false
    return am.getEnabledAccessibilityServiceList(AccessibilityServiceInfo.FEEDBACK_ALL_MASK)
        .any { it.resolveInfo.serviceInfo?.name?.endsWith(PttAccessibilityService::class.java.simpleName) == true }
}

@Composable
private fun SwitchRowLocal(label: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(label, fontSize = 13.sp, modifier = Modifier.weight(1f))
        Switch(checked = checked, onCheckedChange = onChange)
    }
}
