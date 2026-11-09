package com.hamradio.ft710android.App

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.content.Intent
import android.os.PowerManager
import android.view.KeyEvent
import android.view.accessibility.AccessibilityEvent
import com.hamradio.ft710android.PTT.HardwarePtt

/**
 * 后台 / 息屏下的物理 PTT 捕获。
 *
 * 为什么需要它：Android 的按键只会送到**焦点窗口**，App 退到后台或屏幕熄灭后
 * `Activity.dispatchKeyEvent` 再也收不到——这正是 RCForb 安卓端"息屏后外部键失效"
 * 的原因（它只在前台捕获）。无障碍服务的 `FLAG_REQUEST_FILTER_KEY_EVENTS` 是唯一
 * 不 root 的全局按键通路。
 *
 * 两个必须知道的系统限制（2026-07 真机实测结论，见 docs/hardware-ptt-hytera.md）：
 *  - `android:canRequestFilterKeyEvents="true"` 是承重行，缺了它 `onKeyEvent` 永远不派发；
 *  - 全系统同时只允许**一个**按键过滤服务，装了两个就只有一个生效。
 *
 * 边界：只认"已学习的那一个 keyCode"，其余按键原样放行；音量键即使被绑成 PTT 也
 * **不全局拦截**（用户在任何界面都要能调音量），前台时由 Activity 自己吞掉。
 */
class PttAccessibilityService : AccessibilityService() {

    override fun onServiceConnected() {
        super.onServiceConnected()
        // XML 里已声明，这里再兜一次：某些 ROM 会把服务信息重置成默认值
        serviceInfo = (serviceInfo ?: AccessibilityServiceInfo()).apply {
            flags = flags or AccessibilityServiceInfo.FLAG_REQUEST_FILTER_KEY_EVENTS
        }
    }

    override fun onKeyEvent(event: KeyEvent): Boolean {
        val consumed = when (event.action) {
            KeyEvent.ACTION_DOWN ->
                HardwarePtt.router.onKeyDown(event.keyCode, event.scanCode, event.repeatCount)
            KeyEvent.ACTION_UP -> HardwarePtt.router.onKeyUp(event.keyCode, event.scanCode)
            else -> false
        }
        if (!consumed) return false
        if (event.action == KeyEvent.ACTION_DOWN &&
            event.repeatCount == 0 &&
            HardwarePtt.router.config.wakeAndFocus
        ) {
            wakeAndFocus()
        }
        // 音量键不全局吞：别的界面还要用它调音量。前台时 Activity 会自己消费掉。
        return !isVolumeKey(event.keyCode)
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit

    /** 系统收回服务（用户关掉无障碍 / 内存压力）：把可能按着的那颗键放掉。 */
    override fun onInterrupt() {
        HardwarePtt.router.reset()
    }

    override fun onDestroy() {
        HardwarePtt.router.reset()
        super.onDestroy()
    }

    private fun wakeAndFocus() {
        runCatching {
            val pm = getSystemService(PowerManager::class.java) ?: return
            @Suppress("DEPRECATION")
            val lock = pm.newWakeLock(
                PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP,
                "mrrc:hwptt",
            )
            lock.acquire(5_000) // 自动释放：只负责把屏幕点亮这一下
            val launch = packageManager.getLaunchIntentForPackage(packageName) ?: return
            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            startActivity(launch)
        }
    }

    private fun isVolumeKey(keyCode: Int): Boolean =
        keyCode == KeyEvent.KEYCODE_VOLUME_UP ||
            keyCode == KeyEvent.KEYCODE_VOLUME_DOWN ||
            keyCode == KeyEvent.KEYCODE_VOLUME_MUTE
}
