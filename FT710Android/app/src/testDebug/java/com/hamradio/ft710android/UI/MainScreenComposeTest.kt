package com.hamradio.ft710android.UI

import android.app.Application
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.printToLog
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import com.hamradio.ft710android.Network.ConnectionManager
import com.hamradio.ft710android.Network.parseWsEvent
import com.hamradio.ft710android.PTT.PTTManager
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
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

    private fun ptt() = PTTManager(
        sendPTT = {}, sendTXAudioStop = {}, sendHeartbeat = {},
        startTxAudio = {}, stopTxAudio = {}, serverTXStatus = { 0 },
        isCtrlConnected = { true }, onStuckTX = {}, dispatcher = Dispatchers.Unconfined,
    )

    private fun vm(scope: CoroutineScope, atrEnabled: Boolean = true): MainViewModel {
        val cm = ConnectionManager(
            OkHttpClient(), scope, {}, {}, {}, {}, {}, {}, sendOverride = {},
        )
        val vm = MainViewModel(
            authApi = null, connectionManager = cm, rxPlayer = null, txCapture = null,
            spectrumProcessor = null, memoryChannelsStore = null,
            pttManager = ptt(), scope = scope,
        )
        // 真实 fullState：bands 是对象数组、filterTables 是 [idx,hz] 数对、s_unit 是字符串
        vm.onWsEvent(
            parseWsEvent(
                """{"type":"fullState",
                   "data":{"vfo_a_freq":7116950,"mode":1,"mode_name":"USB","tx_status":0,"scope_span":6,
                           "s_meter":120,"s_unit":"S7","s_meter_dbm":-3,"band_name":"40m",
                           "power_watts":0.0,"alc_pct":0.0,"swr_ratio":1.2},
                   "bands":[{"name":"40m","start":7000000,"end":7300000,"bsr":3,"default_freq":7100000}],
                   "modes":["LSB","USB","CW-U"],
                   "memChannels":[{"freq":7116950,"mode":"LSB","label":"M1"},null,null,null,null,null],
                   "filterTables":{"voice":[[1,300],[2,500]],"narrow":[[1,50]],"narrowModes":["CW-U"]},
                   "radioDisplayName":"Yaesu FT-710",
                   "capabilities":{"model_name":"ft710","display_name":"Yaesu FT-710","verified":true,
                                   "has_atu":true,"has_auto_notch":true,"has_vd_id_meters":true,
                                   "filter_model":"width_table","att_steps":[0,6,12,18],
                                   "preamp_steps":["OFF","AMP1","AMP2"],"scope_type":"ft4222",
                                   "audio_gain_boost":10.0,
                                   "scope_spans":{"0":{"name":"1 kHz","freq":1000},
                                                  "6":{"name":"100 kHz","freq":100000},
                                                  "9":{"name":"1 MHz","freq":1000000}}},
                   "atr1000Enabled":$atrEnabled}""".trimIndent(),
            ),
        )
        return vm
    }

    @Test
    fun `main screen composes and lays out without crashing`() {
        val scope = CoroutineScope(Dispatchers.Unconfined)
        // VM 必须在 setContent **外面**建：写在里面每次重组都会新建一个
        val vm = vm(scope)
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
        // VFO 红标（7.117）与记忆格 M1 的频率同值 → 两处都在
        rule.onAllNodesWithText("7.117", useUnmergedTree = true).assertCountEquals(2)
        // 底栏 PTT
        rule.onNodeWithText("PTT", useUnmergedTree = true).assertExists()
        // TUNE 两处：底栏那个 + ATR 行那个（本用例 atr1000Enabled=true）
        rule.onAllNodesWithText("TUNE", useUnmergedTree = true).assertCountEquals(2)
    }

    @Test
    fun `main screen composes when the ATR option is not configured`() {
        // 没装 ATR-1000 的部署：fullState.atr1000Enabled=false → ATR 行不渲染，且不能崩
        val scope = CoroutineScope(Dispatchers.Unconfined)
        val vm = vm(scope, atrEnabled = false)
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
        val vm = vm(scope)
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
}
