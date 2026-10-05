package com.hamradio.ft710android.App

import android.app.Activity
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
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
    val userOff by vm.userOff.collectAsState()
    val scope = rememberCoroutineScope()

    AppSetup(keepScreenOn = prefs.keepScreenOn, onTxRelease = { vm.onPttRelease() })
    ImmersiveEffect(immersive)

    Box(Modifier.fillMaxSize()) {
        if (!loggedIn) {
            LoginScreen(vm, settings) { loggedIn = true }
        } else if (showSettings) {
            SettingsScreen(vm, settings, prefs) { loggedIn = false; showSettings = false }
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
