package com.hamradio.ft710android.UI

import android.app.Application
import android.graphics.Bitmap
import android.graphics.Canvas
import androidx.activity.ComponentActivity
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import com.hamradio.ft710android.Data.LoginCredentials
import com.hamradio.ft710android.Data.SettingsStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow

/**
 * 登录页**可读性**测试：把界面真渲染成位图，在文字所在矩形里采样像素，
 * 按 WCAG 相对亮度算对比度。
 *
 * 为什么需要它：登录页原先既没铺背景（露出窗口色 `#0B0B0C`）、文字也没给 `color`
 * （落到 M3 默认的 `LocalContentColor` = 黑）→ **黑字压近黑底，用户反馈"看不见下边的小字"**。
 * 这类问题 build / lint / 组合冒烟测试全都发现不了：布局是对的、节点也在，
 * 只有量像素才知道看不见。小字按 WCAG AA 要求 ≥ 4.5:1。
 */
/** 只满足登录页所需的四样，不碰 AndroidKeyStore。 */
private class FakeCredentials : LoginCredentials {
    override val host: Flow<String> = flowOf(SettingsStore.DEFAULT_HOST)
    override val port: Flow<String> = flowOf(SettingsStore.DEFAULT_PORT)
    override suspend fun savedPassword(): String? = null
    override suspend fun save(host: String, port: String, password: String) = Unit
}

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33], qualifiers = "w411dp-h892dp-xhdpi", application = Application::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class LoginScreenContrastTest {
    @get:Rule
    val rule = createAndroidComposeRule<ComponentActivity>()

    /** sRGB 相对亮度（WCAG 定义）。 */
    private fun luminance(argb: Int): Double {
        fun ch(v: Int): Double {
            val c = v / 255.0
            return if (c <= 0.03928) c / 12.92 else ((c + 0.055) / 1.055).pow(2.4)
        }
        return 0.2126 * ch(argb shr 16 and 0xFF) +
            0.7152 * ch(argb shr 8 and 0xFF) +
            0.0722 * ch(argb and 0xFF)
    }

    private fun contrast(a: Int, b: Int): Double {
        val la = luminance(a)
        val lb = luminance(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)
    }

    /** 在节点矩形内采样：背景 = 出现最多的颜色，文字 = 与背景对比最强的颜色。 */
    private fun measureContrast(bmp: Bitmap, nodeText: String): Triple<Double, Int, Int> {
        val node = rule.onNodeWithText(nodeText, substring = true, useUnmergedTree = true)
            .fetchSemanticsNode()
        val b = node.boundsInRoot
        val x0 = b.left.toInt().coerceIn(0, bmp.width - 1)
        val x1 = b.right.toInt().coerceIn(x0 + 1, bmp.width)
        val y0 = b.top.toInt().coerceIn(0, bmp.height - 1)
        val y1 = b.bottom.toInt().coerceIn(y0 + 1, bmp.height)

        val hist = HashMap<Int, Int>()
        for (y in y0 until y1) {
            for (x in x0 until x1) {
                val p = bmp.getPixel(x, y)
                hist[p] = (hist[p] ?: 0) + 1
            }
        }
        assertTrue("$nodeText 区域没有采到像素", hist.isNotEmpty())
        val bg = hist.maxByOrNull { it.value }!!.key
        val fg = hist.keys.maxByOrNull { contrast(it, bg) }!!
        return Triple(contrast(fg, bg), bg, fg)
    }

    private fun hex(argb: Int) = "#%06X".format(argb and 0xFFFFFF)

    @Test
    fun `login screen text is legible against its background`() {
        // 用假实现：真 SettingsStore 一构造就要 AndroidKeyStore，Robolectric 没有
        val vm = fixtureVm(CoroutineScope(Dispatchers.Unconfined))
        rule.setContent { AppTheme { LoginScreen(vm, FakeCredentials(), onLoggedIn = {}) } }
        rule.waitForIdle()

        val view = rule.activity.window.decorView
        val bmp = Bitmap.createBitmap(
            view.width.coerceAtLeast(1), view.height.coerceAtLeast(1), Bitmap.Config.ARGB_8888,
        )
        view.draw(Canvas(bmp))
        File("build/screenshots").apply { mkdirs() }.resolve("login.png")
            .outputStream().use { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }

        // 逐条量：标题、副标题、连接提示（就是用户说"看不见"的那行）、按钮文字
        val targets = listOf(
            "MRRC Modern" to 3.0,                                   // 大字 WCAG AA 3:1
            "服务器证书未验证" to 4.5,                                // 小字 4.5:1
            "局域网：主机" to 4.5,                                    // 用户报的那行小字
            "连接" to 3.0,
        )
        val report = StringBuilder()
        for ((text, minRatio) in targets) {
            val (ratio, bg, fg) = measureContrast(bmp, text)
            report.append("\n  %-14s 对比 %5.2f:1  底 %s  字 %s  要求 ≥%.1f %s".format(
                text, ratio, hex(bg), hex(fg), minRatio, if (ratio >= minRatio) "✓" else "✗ 不合格",
            ))
            assertTrue(
                "「$text」对比度 ${"%.2f".format(ratio)}:1 < ${minRatio}:1（底 ${hex(bg)} / 字 ${hex(fg)}）$report",
                ratio >= minRatio,
            )
        }
        println("LOGIN-CONTRAST$report")
    }
}
