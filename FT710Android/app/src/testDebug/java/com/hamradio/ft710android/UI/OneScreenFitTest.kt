package com.hamradio.ft710android.UI

import android.app.Application
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File

/**
 * **量出来的**"一屏放得下"——不靠算术模型，靠真实布局边界。
 *
 * [com.hamradio.ft710android.Data.ScreenFit] 的预算是设计期的算术（用来推尺寸），
 * 这里量的是 Compose 实际 measure/layout 之后的结果：最后一张卡片（记忆格行末的 `⋯`）
 * 和底栏 PTT 的 `boundsInRoot.bottom` 必须落在根容器内，否则就是需要滚动 = 超出一屏。
 *
 * 两者不一致时以**这里**为准（说明 ScreenFit 的模型漂了，得回去改常量）。
 * 每次量到的数字会追加写进 `build/screenshots/one-screen-fit.txt`，方便人复核。
 *
 * qualifiers 的高度就是"可用高度"：真机上 edge-to-edge + `safeDrawing` 内边距之后，
 * MainScreen 拿到的高度正好等于 `Configuration.screenHeightDp`（已扣状态栏/导航栏）。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33], application = Application::class)
class OneScreenFitTest {
    companion object {
        private const val REPORT = "build/screenshots/one-screen-fit.txt"

        /** 每轮跑之前清空报告：追加式输出跨轮次会堆积，陈旧行会误导读的人。 */
        @JvmStatic
        @BeforeClass
        fun clearReport() {
            File(REPORT).delete()
        }
    }

    @get:Rule
    val rule = createComposeRule()

    /** 量一次：返回 (可视高度, 内容最低点, PTT 最低点)。 */
    private fun measure(profile: String): Triple<Float, Float, Float> {
        val scope = CoroutineScope(Dispatchers.Unconfined)
        val vm = fixtureVm(scope)
        rule.setContent {
            AppTheme { ProvideScreenMetrics { MainScreen(vm = vm, prefs = UiPrefs(), onOpenSettings = {}) } }
        }
        rule.waitForIdle()
        // 滚动范围是权威判据：maxValue = 内容高 − 视口高，> 0 就说明要滚动才能看全
        val scrollNode = rule.onNodeWithTag("mainScroll").fetchSemanticsNode()
        // Compose 1.7 的 ScrollAxisRange：value/maxValue 都是 () -> Float（单位 px）
        val range = scrollNode.config[SemanticsProperties.VerticalScrollAxisRange]
        val density = rule.density.density
        val viewportDp = scrollNode.boundsInRoot.height / density
        val overflowDp = range.maxValue() / density
        val lastCard = rule.onNodeWithText("⋯", useUnmergedTree = true).fetchSemanticsNode().boundsInRoot
        val parts = listOf("secHeader","secStatus","secSpectrum","secMeters","secControls","secTuning","secMemory")
            .map { t ->
                val n = rule.onNodeWithTag(t).fetchSemanticsNode().boundsInRoot
                t.removePrefix("sec") to (n.height / density)
            }
        println("PARTS $profile " + parts.joinToString(" ") { "${it.first}=${it.second.toInt()}" } +
            " 合计=${parts.sumOf { it.second.toDouble() }.toInt()}")
        val line = "%-26s 滚动视口 %.0fdp  超出 %.1fdp  末卡底 %.0fdp  %s".format(
            profile, viewportDp, overflowDp, lastCard.bottom / density,
            if (overflowDp <= 0.5f) "✅ 一屏（无需滚动）" else "❌ 需滚动 %.1fdp".format(overflowDp),
        )
        println("FIT $line")
        File(REPORT).let { f -> f.parentFile.mkdirs(); f.appendText(line + System.lineSeparator()) }
        return Triple(range.maxValue(), viewportDp * density, lastCard.bottom)
    }

    private fun assertFits(profile: String) {
        val (maxScroll, _, _) = measure(profile)
        assertTrue("$profile：主屏需要滚动 $maxScroll px 才能看全（应 ≤ 1px）", maxScroll <= 1f)
    }

    @Test
    @Config(qualifiers = "w360dp-h728dp-xxhdpi")
    fun `xiaomi 360x728 with three-button nav fits on one screen`() =
        assertFits("小米/红米 360×728 三键导航")

    @Test
    @Config(qualifiers = "w360dp-h744dp-xxhdpi")
    fun `xiaomi 360x744 with gesture nav fits on one screen`() =
        assertFits("小米/红米 360×744 手势导航")

    @Test
    @Config(qualifiers = "w400dp-h832dp-xxhdpi")
    fun `honor 400x832 fits on one screen`() = assertFits("荣耀 400×832")

    @Test
    @Config(qualifiers = "w432dp-h880dp-xxhdpi")
    fun `huawei mate 432x880 fits on one screen`() = assertFits("华为 Mate 432×880")

    @Test
    @Config(qualifiers = "w411dp-h892dp-xhdpi")
    fun `pixel class 411x892 fits on one screen`() = assertFits("小米12/Pixel 411×892")

    @Test
    @Config(qualifiers = "w384dp-h816dp-xxhdpi")
    fun `mid size 384x816 fits on one screen`() = assertFits("中档 384×816")

    @Test
    @Config(qualifiers = "w800dp-h1200dp-mdpi")
    fun `tablet 800x1200 uses the standard profile and fits`() = assertFits("平板 800×1200（标准档）")
}
