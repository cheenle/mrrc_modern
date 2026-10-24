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
        // 7 个分区：工具行 / 显示屏 / 频谱 / 仪表 / 控制 / 调谐 / 记忆
        val parts = listOf(
            "secToolRow","secHeader","secSpectrum","secMeters","secControls","secTuning","secMemory",
        ).map { t ->
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

    /**
     * 横屏量一次：`(超出 dp, 可视高 dp, 仪表行底相对可视区顶 dp)`。
     *
     * 横屏**允许**滚动（用户 2026-10-22 定案 B：尽量一屏，紧的档位滚动兜底），所以判据是
     * "超出量小 + 首屏能看全主频/S 表/瀑布那一行"，而不是竖屏那条"必须一屏"。
     */
    private fun measureWide(profile: String): Triple<Float, Float, Float> {
        val scope = CoroutineScope(Dispatchers.Unconfined)
        val vm = fixtureVm(scope)
        rule.setContent {
            AppTheme { ProvideScreenMetrics { MainScreen(vm = vm, prefs = UiPrefs(), onOpenSettings = {}) } }
        }
        rule.waitForIdle()
        val scrollNode = rule.onNodeWithTag("mainScroll").fetchSemanticsNode()
        val range = scrollNode.config[SemanticsProperties.VerticalScrollAxisRange]
        val density = rule.density.density
        val viewportDp = scrollNode.boundsInRoot.height / density
        val overflowDp = range.maxValue() / density
        val scrollTop = scrollNode.boundsInRoot.top
        val instrumentBottom = rule.onNodeWithTag("secHeader").fetchSemanticsNode().boundsInRoot.bottom
        // 四张卡片必须真的并排（不是折成两行）
        val boxes = listOf("secMeters", "secControls", "secTuning", "secMemory")
            .map { rule.onNodeWithTag(it).fetchSemanticsNode().boundsInRoot }
        boxes.zipWithNext().forEach { (a, b) ->
            assertTrue("$profile：卡片没有左右排开（${a.right} vs ${b.left}）", b.left >= a.right - 1f)
            assertTrue("$profile：卡片不在同一行", kotlin.math.abs(a.top - b.top) <= 1f)
        }
        val line = "%-26s 横屏 可视 %.0fdp  超出 %.1fdp  仪表行底 %.0fdp  %s".format(
            profile, viewportDp, overflowDp, (instrumentBottom - scrollTop) / density,
            if (overflowDp <= 0.5f) "✅ 一屏" else "↕ 可滚 %.1fdp".format(overflowDp),
        )
        println("FIT $line")
        File(REPORT).let { f -> f.parentFile.mkdirs(); f.appendText(line + System.lineSeparator()) }
        return Triple(overflowDp, viewportDp, (instrumentBottom - scrollTop) / density)
    }

    private fun assertWide(profile: String) {
        val (overflow, viewport, instrumentBottom) = measureWide(profile)
        assertTrue("$profile：横屏超出 ${"%.1f".format(overflow)}dp，超过允许的 32dp", overflow <= 32f)
        assertTrue(
            "$profile：主频/S 表/瀑布 那一行底 ${"%.0f".format(instrumentBottom)}dp 超出首屏 ${"%.0f".format(viewport)}dp",
            instrumentBottom <= viewport + 0.5f,
        )
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

    // ── 横屏（2026-10-22）───────────────────────────────────────────────
    // 手机横屏 = 高度 300~450dp 的极扁屏；平板横屏 = 又宽又高。两者共用一套双列布局。

    @Test
    @Config(qualifiers = "w728dp-h360dp-xxhdpi")
    fun `landscape handset 728x360 lays out side by side`() = assertWide("小米/红米 横屏 728×360")

    @Test
    @Config(qualifiers = "w832dp-h384dp-xxhdpi")
    fun `landscape honor 832x384 fits on one screen`() = assertWide("荣耀 横屏 832×384")

    @Test
    @Config(qualifiers = "w892dp-h411dp-xhdpi")
    fun `landscape pixel 892x411 fits on one screen`() = assertWide("Pixel 横屏 892×411")

    @Test
    @Config(qualifiers = "w1200dp-h800dp-mdpi")
    fun `landscape tablet 1200x800 uses the landscape standard profile`() = assertWide("平板横屏 1200×800")
}
