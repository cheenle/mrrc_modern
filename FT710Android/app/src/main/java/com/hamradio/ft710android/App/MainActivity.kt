package com.hamradio.ft710android.App

import android.graphics.Color
import android.os.Bundle
import android.view.KeyEvent
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.core.view.WindowCompat
import com.hamradio.ft710android.Data.SettingsStore
import com.hamradio.ft710android.PTT.HardwarePtt
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

    /**
     * 物理 PTT 的前台捕获路径（后台/息屏由 `PttAccessibilityService` 接管，两者共用同一个
     * router，所以同一次按下被两边各收到一次也只 press 一次）。
     *
     * 用 `onKeyDown/onKeyUp` 而不是 `dispatchKeyEvent`：后者在 `ComponentActivity` 上是
     * `@RestrictTo`（lint `RestrictedApi` 直接报错），而 Activity 级按键本来就是同一套语义 —— 
     * 界面没消费的键会冒泡到这里。
     *
     * 学习模式下任何键都只用来「识别」：用户这时按的就是那颗物理键，绝不能发射。
     */
    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean {
        if (HardwarePtt.router.onKeyDown(keyCode, event.scanCode, event.repeatCount)) return true
        return super.onKeyDown(keyCode, event)
    }

    override fun onKeyUp(keyCode: Int, event: KeyEvent): Boolean {
        if (HardwarePtt.router.onKeyUp(keyCode, event.scanCode)) return true
        return super.onKeyUp(keyCode, event)
    }

    override fun onStop() {
        // 旋转属于"配置变更"，不是用户离开：这时候松手会把正在说的话掐断（2026-10-22
        // 放开方向锁时新加的闸）。判定见 RotationPolicy.kt，真正的退后台仍然无条件释放。
        if (shouldReleaseOnStop(isChangingConfigurations)) {
            holder.vm.onPttRelease()
            holder.vm.onAppForeground(false)
        }
        super.onStop()
    }
}
