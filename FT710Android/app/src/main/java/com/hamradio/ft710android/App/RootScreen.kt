package com.hamradio.ft710android.App

import android.Manifest
import android.app.Activity
import android.os.Build
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.core.content.ContextCompat
import com.hamradio.ft710android.Data.SettingsStore
import com.hamradio.ft710android.UI.LoginScreen
import com.hamradio.ft710android.UI.MainScreen
import com.hamradio.ft710android.UI.MrrcColors
import com.hamradio.ft710android.UI.SettingsScreen
import com.hamradio.ft710android.UI.UiPrefs
import com.hamradio.ft710android.UI.UiPrefsFlow
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.launch

@Composable
fun RootScreen(vm: MainViewModel, settings: SettingsStore) {
    var loggedIn by rememberSaveable { mutableStateOf(false) }
    var showSettings by rememberSaveable { mutableStateOf(false) }
    var immersive by rememberSaveable { mutableStateOf(false) }
    val prefs by UiPrefsFlow(settings).collectAsState(initial = UiPrefs())
    // 单一真相源：启动、重连、用户在设置里改档，三条路都只经过这里。
    // 偏好变了就推给连接层（已在传时它会在同一条 socket 上补发 caps，不重连）。
    LaunchedEffect(prefs.spectrumProfile) { vm.setSpectrumProfile(prefs.spectrumProfile) }
    val userOff by vm.userOff.collectAsState()
    val scope = rememberCoroutineScope()
    val context = LocalContext.current

    // 登录成功后一次性申请：
    //  - RECORD_AUDIO：没有它 TX 采集静默失败 → 电台键控但无调制 → "出不去功率"（2026-10-05 真机事故）
    //  - POST_NOTIFICATIONS（Android 13+）：后台 RX 常驻通知
    val notifLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }
    val micLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (!granted) {
            vm.showNotice("未授予麦克风权限：发射没有话音（系统设置 → 应用 → MRRC Modern → 权限 → 麦克风）")
        }
    }
    LaunchedEffect(loggedIn) {
        if (!loggedIn) return@LaunchedEffect
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) !=
            android.content.pm.PackageManager.PERMISSION_GRANTED
        ) {
            micLauncher.launch(Manifest.permission.RECORD_AUDIO)
        }
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) !=
            android.content.pm.PackageManager.PERMISSION_GRANTED
        ) {
            notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    AppSetup(keepScreenOn = prefs.keepScreenOn, onTxRelease = { vm.onPttRelease() })
    ImmersiveEffect(immersive)

    // 系统返回键/手势：在设置页时回主屏，而不是直接退出 App
    BackHandler(enabled = loggedIn && showSettings) { showSettings = false }

    // 尊重系统窗口插入区：顶部保住状态栏、底部避开导航条/手势条（Android 15 强制 edge-to-edge）
    Box(Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing)) {
        if (!loggedIn) {
            LoginScreen(vm, settings) { loggedIn = true }
        } else if (showSettings) {
            SettingsScreen(vm, settings, prefs, onBack = { showSettings = false }) {
                loggedIn = false; showSettings = false
            }
        } else {
            MainScreen(
                vm = vm,
                prefs = prefs,
                onOpenSettings = { showSettings = true },
                fullscreen = immersive,
                onToggleFullscreen = { immersive = !immersive },
                onAfVol = { v -> scope.launch { settings.putAfVol(v) } },
            )
        }
        if (loggedIn && userOff) DisconnectedOverlay(vm)
    }
}

/** 顶栏 ⛶ 的沉浸式全屏（M6）。 */
@Composable
private fun ImmersiveEffect(immersive: Boolean) {
    val view = LocalView.current
    val context = LocalContext.current
    LaunchedEffect(immersive) {
        val window = (context as? Activity)?.window ?: return@LaunchedEffect
        val controller = WindowCompat.getInsetsController(window, view)
        if (immersive) {
            controller.systemBarsBehavior =
                WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            controller.hide(WindowInsetsCompat.Type.systemBars())
        } else {
            controller.show(WindowInsetsCompat.Type.systemBars())
        }
    }
}

/** M7：用户断开后的遮罩 + 一键重连。 */
@Composable
private fun DisconnectedOverlay(vm: MainViewModel) {
    Box(
        Modifier.fillMaxSize().background(Color(0xCC000000)),
        contentAlignment = Alignment.Center,
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text("已断开（Web 连接关闭）", color = MrrcColors.TextPrimary, fontSize = 16.sp)
            Spacer(Modifier.height(14.dp))
            Box(
                Modifier.border(1.dp, MrrcColors.Accent, RoundedCornerShape(8.dp))
                    .clickable { vm.reconnect() }
                    .padding(horizontal = 22.dp, vertical = 10.dp)
            ) {
                Text("重新连接", color = MrrcColors.Accent, fontSize = 14.sp)
            }
        }
    }
}
