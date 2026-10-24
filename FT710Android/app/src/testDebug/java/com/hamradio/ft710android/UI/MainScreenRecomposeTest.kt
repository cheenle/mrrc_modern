package com.hamradio.ft710android.UI

import android.app.Application
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import com.hamradio.ft710android.Network.parseWsEvent
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * **状态更新必须重画**：服务端推一条 stateUpdate，屏幕上的读数要跟着变，
 * 不需要用户点一下、也不需要转一次屏。
 *
 * 为什么需要它：Kotlin 2.0.20 起 Compose 默认开启 strong skipping——**所有** composable
 * 都可跳过，不稳定参数按**实例相等**比较。`RadioState` 是可变的普通类，把它整个当参数
 * 传进子组件（v1.1.33 抽卡片时就是这么做的），父组件因 `vm.version` 变化而重组时，
 * 子组件看到的是「同一个 `state` 实例」→ 判定参数没变 → **跳过**，里面的
 * `state.activeFrequency` 再也不重读。表现：连上了、有声音、有频谱、能控制电台，
 * 只有频率一直 `00.000.00`，点一下或转屏后才对。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33], qualifiers = "w411dp-h892dp-xhdpi", application = Application::class)
class MainScreenRecomposeTest {
    @get:Rule
    val rule = createComposeRule()

    private fun stateUpdate(fields: String, dirty: String) =
        parseWsEvent("""{"type":"stateUpdate","fields":{$fields},"dirty":[$dirty]}""")

    @Test
    fun `a stateUpdate repaints the frequency without any other interaction`() {
        val scope = CoroutineScope(Dispatchers.Unconfined)
        val vm = fixtureVm(scope)
        rule.setContent {
            AppTheme { ProvideScreenMetrics { MainScreen(vm = vm, prefs = UiPrefs(), onOpenSettings = {}) } }
        }
        rule.waitForIdle()
        rule.onNodeWithText("07.116.95", useUnmergedTree = true).assertExists()

        // 只推一条 stateUpdate：没有点击、没有转屏、没有任何别的状态变化
        vm.onWsEvent(stateUpdate(""""vfo_a_freq":14074000,"active_freq":14074000""",
                                """"vfo_a_freq","active_freq""""))
        rule.waitForIdle()

        rule.onNodeWithText("14.074.00", useUnmergedTree = true).assertExists()
    }

    @Test
    fun `a stateUpdate repaints the meters too`() {
        val scope = CoroutineScope(Dispatchers.Unconfined)
        val vm = fixtureVm(scope)
        rule.setContent {
            AppTheme { ProvideScreenMetrics { MainScreen(vm = vm, prefs = UiPrefs(), onOpenSettings = {}) } }
        }
        rule.waitForIdle()

        vm.onWsEvent(stateUpdate(""""power_watts":99.0""", """"power_watts""""))
        rule.waitForIdle()
        rule.onNodeWithText("99.0 W", useUnmergedTree = true).assertExists()
    }
}
