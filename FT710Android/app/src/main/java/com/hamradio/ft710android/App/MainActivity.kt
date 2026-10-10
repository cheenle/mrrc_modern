package com.hamradio.ft710android.App

import android.graphics.Color
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.core.view.WindowCompat
import com.hamradio.ft710android.Data.SettingsStore
import com.hamradio.ft710android.UI.AppTheme
import androidx.compose.material3.LocalMinimumInteractiveComponentSize
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.unit.dp
import com.hamradio.ft710android.UI.ProvideScreenMetrics

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
        setContent {
            AppTheme {
                // 屏幕档位（紧凑/标准）由 Configuration 算出，向下用 CompositionLocal 下发；
                // 尺寸数值全部来自 Data/ScreenFit（ScreenFitTest 用真机档位验证过"一屏放得下"）
                // M3 的 Slider/Switch 默认强制 48dp 最小交互高度，会把紧凑档的音量行撑爆预算
                // （ScreenFit.sliderRowHeight=34）；主屏是密集仪表盘，按档位高度走
                CompositionLocalProvider(LocalMinimumInteractiveComponentSize provides 0.dp) {
                    ProvideScreenMetrics { RootScreen(vm = holder.vm, settings = settings) }
                }
            }
        }
    }
    // 回前台恢复频谱；退后台停掉频谱省带宽（PTT 仍按安全铁律立即释放）
    override fun onStart() {
        super.onStart()
        holder.vm.onAppForeground(true)
    }

    override fun onStop() {
        holder.vm.onPttRelease()
        holder.vm.onAppForeground(false)
        super.onStop()
    }
}
