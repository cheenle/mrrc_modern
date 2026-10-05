package com.hamradio.ft710android.App

import android.graphics.Color
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.core.view.WindowCompat
import com.hamradio.ft710android.Data.SettingsStore
import com.hamradio.ft710android.UI.AppTheme

class MainActivity : ComponentActivity() {
    private val holder: MainViewModelHolder by viewModels()
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // 深色背景 → 状态栏/导航条用浅色图标（Android 15 edge-to-edge 下默认可能看不见）
        WindowCompat.getInsetsController(window, window.decorView).apply {
            isAppearanceLightStatusBars = false
            isAppearanceLightNavigationBars = false
        }
        val settings = SettingsStore(applicationContext)
        setContent { AppTheme { RootScreen(vm = holder.vm, settings = settings) } }
    }
    override fun onStop() { holder.vm.onPttRelease(); super.onStop() }
}
