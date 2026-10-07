package com.hamradio.ft710android.UI

import android.app.Application
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.printToLog
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.getOrNull
import androidx.compose.ui.test.onChildren
import androidx.compose.ui.test.onNodeWithTag
import com.hamradio.ft710android.Data.ScreenFit
import org.robolectric.annotation.Config

/**
 * 主屏「能组合、能布局」的冒烟测试 —— 补上 JVM 单测与 lint 都盖不到的那一块。
 *
 * **为什么需要它**：布局期的异常在 CI 上是隐形的，只会在真机上表现为「装完打开就退出」。
 * v1.1.16 就是这么发出去的：顶栏写了 `Row(Modifier.height(IntrinsicSize.Min))`，而子项里有
 * `BoxWithConstraints`（SubcomposeLayout，不支持 intrinsic 测量）→ 组合即抛
 * `IllegalStateException`。当时 135 个测试全绿、lint 干净、assembleRelease 成功，
 * 只有用户的手机发现。有了这条测试，同类问题在 `gradlew test` 阶段就红。
 *
 * 用**真实 MainViewModel**（可选依赖全 null）+ 真实 fullState，所以状态行、S 表弧、
 * 频谱显示屏、仪表卡、控制卡、记忆格、底栏 PTT 都会被真正 measure/layout 一遍。
 */
@RunWith(RobolectricTestRunner::class)
// application 用框架自带的：真的 FT710App.onCreate → ServiceLocator → RxAudioPlayer
// → OpusBridge.<clinit> 会 System.loadLibrary("opus_jni")，JVM 测试里没有这个 native 库
@Config(sdk = [33], qualifiers = "w411dp-h892dp-xhdpi", application = Application::class)
class MainScreenComposeTest {
    @get:Rule
    val rule = createComposeRule()

    @Test
    fun `main screen composes and lays out without crashing`() {
        val scope = CoroutineScope(Dispatchers.Unconfined)
        // VM 必须在 setContent **外面**建：写在里面每次重组都会新建一个
        val vm = fixtureVm(scope)
        assertEquals("fullState 必须真的应用了", 7116950L, vm.state.vfoAFreq)
        rule.setContent {
            AppTheme {
                ProvideScreenMetrics {
                    MainScreen(vm = vm, prefs = UiPrefs(), onOpenSettings = {})
                }
            }
        }
        rule.waitForIdle()
        // 主频：fmtMhz(7116950) = "07.116.95"（末两位 10Hz 是独立 span，但语义文本是整串）
        rule.onNodeWithText("07.116.95", useUnmergedTree = true).assertExists()
        // 显示屏上沿：波段 · 模式 读数（band_name + mode_name 两个字段都要有）
        rule.onNodeWithText("40m · USB", useUnmergedTree = true).assertExists()
        // 频谱下那行 VFO 红字已删（与主频重复）→ 7.117 只剩记忆格 M1 一处
        rule.onAllNodesWithText("7.117", useUnmergedTree = true).assertCountEquals(1)
        // 分区标题全部去掉（用户要求：各区域功能已知，标题白占空间）
        rule.onAllNodesWithText("仪表", useUnmergedTree = true).assertCountEquals(0)
        rule.onAllNodesWithText("控制", useUnmergedTree = true).assertCountEquals(0)
        rule.onAllNodesWithText("调谐", useUnmergedTree = true).assertCountEquals(0)
        // 记忆管理入口必须还在：v1.1.18 把它藏在分区标题的 trailing 里，
        // 紧凑档标题一隐藏，手机上 MemoryManagerDialog 就再也打不开了
        rule.onNodeWithText("⋯", useUnmergedTree = true).assertExists()
        // 音量行的 "Vol" 标签也去掉了
        rule.onAllNodesWithText("Vol", useUnmergedTree = true).assertCountEquals(0)
        // 底栏 PTT
        rule.onNodeWithText("PTT", useUnmergedTree = true).assertExists()
        // TUNE 两处：底栏那个 + ATR 行那个（本用例 atr1000Enabled=true）
        rule.onAllNodesWithText("TUNE", useUnmergedTree = true).assertCountEquals(2)
    }

    @Test
    fun `main screen composes when the ATR option is not configured`() {
        // 没装 ATR-1000 的部署：fullState.atr1000Enabled=false → ATR 行不渲染，且不能崩
        val scope = CoroutineScope(Dispatchers.Unconfined)
        val vm = fixtureVm(scope, atrEnabled = false)
        assertFalse(vm.atr1000Enabled.value)
        rule.setContent {
            AppTheme {
                ProvideScreenMetrics {
                    MainScreen(vm = vm, prefs = UiPrefs(), onOpenSettings = {})
                }
            }
        }
        rule.waitForIdle()
        rule.onNodeWithText("TUNE", useUnmergedTree = true).assertExists()   // 电台内置 ATU 的 TUNE 仍在
        rule.onNodeWithText("ATR 离线", useUnmergedTree = true).assertDoesNotExist()
    }

    @Test
    fun `main screen composes in listen-only and on a narrow screen`() {
        // 只读登录（发射类 UI 隐藏）+ 极窄屏（FlowRow 折行）都不该崩
        val scope = CoroutineScope(Dispatchers.Unconfined)
        val vm = fixtureVm(scope)
        vm.onListenOnly()
        assertTrue(vm.listenOnly.value)
        rule.setContent {
            AppTheme {
                ProvideScreenMetrics {
                    MainScreen(vm = vm, prefs = UiPrefs(), onOpenSettings = {})
                }
            }
        }
        rule.waitForIdle()
        rule.onNodeWithText("只读", useUnmergedTree = true).assertIsDisplayed()
    }

    /**
     * 记忆格"不变形"的实测保证（2026-10-07 起**只显示频率**）：
     * 紧凑档一行 6 格、每格只有约 47dp。量真实布局：
     * ①格宽与模型一致 ②每格**只有一个**文本节点（标签确实不显示了）
     * ③文字宽度 ≤ 格宽 ④必须单行（格高固定，换行会顶变形）。
     * 只靠字号公式不够——公式错了或有人改了 `maxLines`，这里会红。
     */
    @Test
    @Config(qualifiers = "w360dp-h728dp-xxhdpi")
    fun `memory cells show only the frequency and never overflow their cell`() {
        val scope = CoroutineScope(Dispatchers.Unconfined)
        val vm = fixtureVm(scope)
        rule.setContent {
            AppTheme { ProvideScreenMetrics { MainScreen(vm = vm, prefs = UiPrefs(), onOpenSettings = {}) } }
        }
        rule.waitForIdle()

        // 360dp 屏、紧凑档：模型算出每格 47dp 左右
        val expected = ScreenFit.memoryCellWidth(360, compact = true)
        val density = rule.density.density
        // fixture：M1=7.117  M2=14.270  M3=438.500（7 字符，最长）  M4~M6 空
        val expectText = listOf("7.117", "14.270", "438.500", "M4", "M5", "M6")

        for (index in 0 until 6) {
            val cell = rule.onNodeWithTag("memCell$index").fetchSemanticsNode().boundsInRoot
            val cellW = cell.width / density
            assertEquals("memCell$index 宽度与模型不符", expected, cellW, 1.5f)

            val texts = cellChildrenTexts(index)
            assertEquals("memCell$index 应当只有频率一行（标签不再显示）：$texts", 1, texts.size)
            val (text, widthDp, lines) = texts[0]
            assertEquals("memCell$index 内容不对", expectText[index], text)
            assertTrue(
                "memCell$index 的 \"$text\" 宽 ${"%.1f".format(widthDp)}dp 超出格宽 ${"%.1f".format(cellW)}dp",
                widthDp <= cellW + 0.5f,
            )
            assertEquals("\"$text\" 必须单行（换行会顶变形）", 1, lines)
        }
        // 标签（含中文）在主屏上一处都不该出现——只在记忆管理对话框里看得到
        rule.onAllNodesWithText("40m SSB", substring = true, useUnmergedTree = true).assertCountEquals(0)
        rule.onAllNodesWithText("中文标签测试", useUnmergedTree = true).assertCountEquals(0)
    }

    /** 取某个记忆格内所有文本节点的宽度（dp）与行数。 */
    private fun cellChildrenTexts(index: Int): List<Triple<String, Float, Int>> {
        val density = rule.density.density
        return rule.onNodeWithTag("memCell$index", useUnmergedTree = true)
            .onChildren().fetchSemanticsNodes()
            .map { n ->
                val text = n.config.getOrNull(SemanticsProperties.Text)?.joinToString("") { it.text }.orEmpty()
                val lines = n.config.getOrNull(SemanticsProperties.Text)?.size ?: 0
                Triple(text, n.boundsInRoot.width / density, lines)
            }
            .filter { it.first.isNotEmpty() }
    }
}