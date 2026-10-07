package com.hamradio.ft710android.UI

import android.app.Application
import android.graphics.Bitmap
import android.graphics.Canvas
import androidx.activity.ComponentActivity
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File

/**
 * **给开发者装眼睛**：把主屏渲染成 PNG 存到 `app/build/screenshots/`，
 * 这样改 UI 之后能真的看一眼，而不是靠真机用户回报"卡顿/太小/不够精致"。
 *
 * 不是断言型测试（截图没有金标准可比），而是**给人看的取证工具**（当前模型不支持图像输入，
 * 开发者自己也看不了 png；它的价值是让**用户/评审**不装 APK 就能看设计）：
 * 跑 `gradlew :app:testDebugUnitTest --tests ScreenshotTest` 后，打开
 * `app/build/screenshots/` 目录里的 png。
 *
 * 需要 `@GraphicsMode(NATIVE)`（Robolectric 的真实渲染后端）；LEGACY 模式画出来是空白。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33], qualifiers = "w411dp-h892dp-xhdpi", application = Application::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class ScreenshotTest {
    @get:Rule
    val rule = createAndroidComposeRule<ComponentActivity>()

    private fun shot(name: String, content: @androidx.compose.runtime.Composable () -> Unit) {
        rule.setContent { AppTheme { ProvideScreenMetrics { content() } } }
        rule.waitForIdle()
        val view = rule.activity.window.decorView
        val w = view.width.coerceAtLeast(1)
        val h = view.height.coerceAtLeast(1)
        val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        view.draw(Canvas(bmp))
        val out = File("build/screenshots").apply { mkdirs() }
            .resolve("$name-${w}x$h.png")
        out.outputStream().use { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }
        println("SCREENSHOT ${out.absolutePath} (${out.length()} bytes)")
    }

    // ComposeTestRule 每个测试只允许一次 setContent，所以一张图一个 @Test
    @Test
    fun `capture the main screen in RX`() {
        val scope = CoroutineScope(Dispatchers.Unconfined)
        shot("main-rx") { MainScreen(vm = fixtureVm(scope), prefs = UiPrefs(), onOpenSettings = {}) }
    }

    @Test
    fun `capture the main screen while transmitting`() {
        val scope = CoroutineScope(Dispatchers.Unconfined)
        shot("main-tx") { MainScreen(vm = fixtureVm(scope, tx = true), prefs = UiPrefs(), onOpenSettings = {}) }
    }
}
