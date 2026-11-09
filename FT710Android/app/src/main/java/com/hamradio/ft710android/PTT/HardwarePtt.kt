package com.hamradio.ft710android.PTT

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * 物理（外部）PTT：侧键 / 音量键 / 蓝牙手咪 / 车机方向盘键。
 *
 * 背景（2026-11 调研，见 `docs/hardware-ptt-hytera.md`）：
 *  - Android **没有**标准 PTT keycode（API 35 的 319 个 KEYCODE 里没有 `KEYCODE_PTT`），
 *    厂商各自用私有码或把标准键重映射（228 / 301 / 131 / 520–522 / 1015 …）。
 *    所以这里**不写死任何码表**，一律由用户「学习」。
 *  - 海能达 PDC760 这类 PDT/DMR 多模机的 PTT 归窄带引擎，第三方 App 拿不到；
 *    PNC 系 PoC 机（PNC370/380/550…）能拿到，走的就是普通按键通路。
 *  - 前台靠 `Activity.dispatchKeyEvent`；后台/息屏靠无障碍服务
 *    （`canRequestFilterKeyEvents=true`，全系统同时只允许一个按键过滤服务）。
 *    两条路共用同一个 [HardwarePttRouter] 实例，靠 `down` 标志去重。
 *
 * 本文件全部是纯 Kotlin（不 import android.*），JVM 可单测。
 */

enum class HardwarePttMode {
    /** 按住发射、松手停止（推荐，误触风险最低）。 */
    Hold,

    /** 点一下锁住发射、再点一下松开。**离开界面后可能再也解不开**，只给特殊场景。 */
    Tap;

    companion object {
        fun parse(raw: String?): HardwarePttMode =
            if (raw == "tap") Tap else Hold
    }

    val key: String get() = if (this == Tap) "tap" else "hold"
}

/** 硬件 PTT 的配置快照。`keyCode == 0` 表示还没学习过按键。 */
data class HardwarePttConfig(
    val enabled: Boolean = false,
    val keyCode: Int = 0,
    val scanCode: Int = 0,
    val mode: HardwarePttMode = HardwarePttMode.Hold,
    val wakeAndFocus: Boolean = true,
) {
    val hasKey: Boolean get() = keyCode != 0
}

/** 学习结果：同一个物理键在 Activity 与无障碍服务里 keyCode/scanCode 一致，scanCode 只用于展示与排查。 */
data class LearnedKey(val keyCode: Int, val scanCode: Int)

/**
 * 按键 → `PTTManager.press()/forceRelease()` 的路由与去重。
 *
 * 不变量（任何一条退化都是空中事故）：
 *  - 学习模式下**绝不**发射：用户在学习时按的就是那颗物理键。
 *  - 按下与松开必须成对：`ACTION_UP` 一定走 [onKeyUp] → `release`。
 *  - 自动重复（`repeatCount > 0`）一律吞掉，不能反复 press。
 *  - 同一次物理按下可能被 Activity 与无障碍服务**各收到一次**，`down` 标志保证只生效一次。
 */
class HardwarePttRouter(
    private val isKeyed: () -> Boolean,
    private val press: () -> Unit,
    private val release: () -> Unit,
) {
    @Volatile
    var config: HardwarePttConfig = HardwarePttConfig()

    /** 正在等用户按一下要绑定的键。 */
    @Volatile
    var learning: Boolean = false
        private set

    private var learnSink: ((Int, Int) -> Unit)? = null

    /** 已按下（Hold 模式下用于去重 + 保证成对释放）。 */
    private var down = false

    /** 学习期间这些键照常下发（否则用户想按返回键退出，反而被绑成 PTT）。 */
    private val learnIgnored = setOf(KEYCODE_HOME, KEYCODE_BACK, KEYCODE_APP_SWITCH)

    /** 进入学习：先把可能残留的"按住"状态放掉，再接管下一次按键。 */
    fun startLearn(sink: (keyCode: Int, scanCode: Int) -> Unit) {
        if (down) {
            down = false
            release()
        }
        learning = true
        learnSink = sink
    }

    fun cancelLearn() {
        learning = false
        learnSink = null
    }

    /**
     * @return true = 事件已被消费（前台路径不要再交给 super，无障碍路径据此决定是否拦截）。
     */
    fun onKeyDown(keyCode: Int, scanCode: Int, repeatCount: Int): Boolean {
        learnSink?.let { sink ->
            if (repeatCount == 0 && keyCode !in learnIgnored) {
                learning = false
                learnSink = null
                sink(keyCode, scanCode)
                return true
            }
            return keyCode !in learnIgnored
        }
        val c = config
        if (!c.enabled || !c.hasKey || keyCode != c.keyCode) return false
        if (repeatCount > 0) return true // 按住不放：已经处理过，别再 press
        when (c.mode) {
            HardwarePttMode.Hold -> if (!down) {
                down = true
                press()
            }
            HardwarePttMode.Tap -> if (!down) {
                down = true
                if (isKeyed()) release() else press()
            }
        }
        return true
    }

    fun onKeyUp(keyCode: Int, scanCode: Int): Boolean {
        if (learnSink != null) return true
        val c = config
        if (!c.enabled || !c.hasKey || keyCode != c.keyCode) return false
        when (c.mode) {
            HardwarePttMode.Hold -> if (down) {
                down = false
                release()
            }
            HardwarePttMode.Tap -> down = false
        }
        return true
    }

    /**
     * 丢弃本地状态（服务销毁 / 无障碍被关 / 切到学习模式）。
     * [releaseHeld] 为 true 且确实按着时补一次释放——服务被杀时物理键还在"按住"，
     * 不补这一下就是长发射。
     */
    fun reset(releaseHeld: Boolean = true) {
        val wasDown = down
        down = false
        learning = false
        learnSink = null
        if (wasDown && releaseHeld) release()
    }

    companion object {
        // AOSP KeyEvent 的稳定取值（这里不 import android.* 以保持纯 Kotlin 可测）
        private const val KEYCODE_HOME = 3
        private const val KEYCODE_BACK = 4
        private const val KEYCODE_APP_SWITCH = 187
    }
}

/**
 * 进程级单例：Activity、无障碍服务、设置页三方共用**同一个** router，
 * 这样前台与后台两条捕获路径天然去重（同一次按下只 press 一次）。
 *
 * 纯 Kotlin：`bind` 由 `ServiceLocator` 接到 `MainViewModel`，`configure` 由 DataStore 偏好推过来。
 */
object HardwarePtt {
    private var pressFn: () -> Unit = {}
    private var releaseFn: () -> Unit = {}
    private var keyedFn: () -> Boolean = { false }

    val router = HardwarePttRouter(
        isKeyed = { keyedFn() },
        press = { pressFn() },
        release = { releaseFn() },
    )

    private val _learned = MutableStateFlow<LearnedKey?>(null)

    /** 学习页监听：用户按下的第一个键。 */
    val learned: StateFlow<LearnedKey?> = _learned

    fun bind(onPress: () -> Unit, onRelease: () -> Unit, isKeyed: () -> Boolean) {
        pressFn = onPress
        releaseFn = onRelease
        keyedFn = isKeyed
    }

    fun configure(config: HardwarePttConfig) {
        router.config = config
    }

    /** 开始学习；捕获后写进 [learned] 供对话框展示，**此时还没有落盘**。 */
    fun learn() {
        _learned.value = null
        router.startLearn { keyCode, scanCode -> _learned.value = LearnedKey(keyCode, scanCode) }
    }

    fun cancelLearn() {
        router.cancelLearn()
        _learned.value = null
    }

    /** 仅供 JVM 测试复位（单例有状态）。 */
    fun resetForTest() {
        router.reset(releaseHeld = false)
        router.config = HardwarePttConfig()
        _learned.value = null
        pressFn = {}
        releaseFn = {}
        keyedFn = { false }
    }
}
