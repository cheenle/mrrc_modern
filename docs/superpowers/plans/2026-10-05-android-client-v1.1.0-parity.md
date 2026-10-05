# MRRC Modern Android v1.1.0 全量对齐手机端 Web 实施计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把 `FT710Android/` 从 v1.0.1（连上但无状态、界面已对齐但功能面缩水）做成完整对齐手机端 Web 的 v1.1.0：先修 D0 阻断级 `fullState` 解析缺陷，再补齐 D/M/S 组 28 项差距与 L1/L2/L3 三个大项，最后签名发布 + 官网分发。

**架构：** 全部改动集中在 `FT710Android/`（Kotlin + Compose，协议逐字对齐 `server.py` @ `62f9be7`，UI 语义逐字对齐 `static/` @ `cd36dab`）。纯逻辑（协议解析、能力表、频率输入、重采样、增益、调色板、Cloud REST 解析）落到无 Android 依赖的类里做 JVM 单测；平台能力（前台服务、通知、沉浸式、AudioRecord 率探测）只在真机验收。服务端与 Web 零改动。

**技术栈：** Kotlin 2.0.21 · Compose BOM 2024.12.01 · AGP 8.7.3 · Gradle 8.9 · JDK 17 · OkHttp 4.12.0 · kotlinx-serialization 1.7.3 · DataStore 1.1.1 · NDK/libopus · minSdk 26 · targetSdk 35。

**设计文档：** `docs/superpowers/specs/2026-10-05-android-client-v1.1.0-parity-design.md`

---

## 全局约束

- **每个 shell 先 export**：`export JAVA_HOME=$(/usr/libexec/java_home -v 17)`、`export ANDROID_HOME="$HOME/Library/Android/sdk"`。
- **工作目录**：`/Users/cheenle/HAM/hub/mrrc_modern/.worktrees/android-v1.0.0`（分支 `feat/android-1.0.0`）；所有 `./gradlew` 命令都在 `FT710Android/` 下跑。
- **协议字段逐字对齐 `server.py`**：`fullState.bands` 是对象数组、`filterTables` 是 `[idx,hz]` 数对、`scope_span` 的值域由 `capabilities.scope_spans` 决定（FT-710 0..9，0/1/2 = 1k/2k/5k）；**禁止自创字段名或值域**。
- **PTT 安全铁律不退化**：`release()` 无条件发 `ptt:false`；手势 `finally` 兜底；`onStop` → `forceRelease()`；看门狗 500ms×3；txhb 500ms。本计划任何任务不得修改这些行为的语义。
- **每任务一次 commit**；提交前 `python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged` 必须 clean；每个 commit 必须让 `./gradlew :app:testDebugUnitTest` 保持全绿（UI 任务另加 `assembleDebug`）。
- **不碰**运行期/外部文件：`atr1000_tuner.json`、`mem_channels.json`、`.env`、`certs/`、`downloads/latest.json`（Windows 升级通道）。
- **UI 无法 JVM 单测**：交付标准 = `assembleRelease` 通过 + 用户真机验收；纯逻辑必须有 JVM 单测。
- **参数名一致性**（全计划复用，不得改名）：`Caps`/`RadioCaps`/`SpanChoice`、`FreqInput.parse`/`FreqInput.qsy`、`NetworkStats`、`AtrEvent`、`RxGain.target`、`Resampler.resample`/`resample882To960`、`TxFraming.applyMicVol`、`Palettes.lut`。

## 文件结构（新增 / 修改）

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `.../Network/Protocol.kt` | 改 | `BandDto`/`CapabilitiesDto`/`ScopeSpanDto`/`FilterTables` 真实形状；`AtrStateDto`/`AtrTuneResultDto`/`parseAtrEvent`；`FullStateDto` 补 `capabilities`/`radioDisplayName` |
| `.../Network/NetworkStats.kt` | 新增 | 收发字节计数 + RTT（可注入时钟，纯 JVM 可测） |
| `.../Network/ConnectionManager.kt` | 改 | ATR 事件路由、`sendAtrTune()`、stats 接线、`disconnect()`/`reconnect()` |
| `.../Network/CloudApi.kt` | 新增 | `/api/cloud/{state,apply,refresh,restart}` + 纯解析（含错误分支） |
| `.../Network/RecordingsApi.kt` | 改 | `list()` 返回含 `count`/`totalBytes` 的 DTO |
| `.../Data/Capabilities.kt` | 新增 | `RadioCaps.from(dto)`、量程表（civ27 ×2）、filter/ATT/PRE 循环与标签 |
| `.../Data/FreqInput.kt` | 新增 | Web `commitFreq` 解析 + QSY 公式 |
| `.../Data/SettingsStore.kt` | 改 | 本地偏好：afVol/micVol/micGain/scopeTheme/scopeFloor/scopeCeil/fftHeight/wfHeight/keepScreenOn/backgroundRx |
| `.../Audio/RxGain.kt` | 新增 | `target(volume, boost, transmitting)` 纯函数 |
| `.../Audio/Resampler.kt` | 新增 | 线性插值重采样 + 882↔960 |
| `.../Audio/TxFraming.kt` | 新增 | `applyMicVol` + 帧长/校验（纯函数） |
| `.../Audio/RxAudioPlayer.kt` | 改 | 增益、TX 静音、`bufferMs` 抖动统计 |
| `.../Audio/TxAudioCapture.kt` | 改 | 采集率探测 + 44.1k→48k + mic vol |
| `.../Spectrum/Palettes.kt` | 新增 | 6 套配色 + floor/ceil LUT（纯函数） |
| `.../Spectrum/WaterfallCanvas.kt` | 改 | 配色/floor/ceil、FFT/瀑布分区、`onQsyFraction` |
| `.../ViewModel/MainViewModel.kt` | 改 | caps/displayName/atr/统计/断开/动作/mic 回推/notice 通道 |
| `.../UI/MainScreen.kt` | 改 | M1–M8 |
| `.../UI/SettingsScreen.kt` | 改 | S1–S5、S8、S9、常亮/后台接收 |
| `.../UI/Dialogs.kt` | 新增 | 频率输入、波段/模式选择、Memory Manager |
| `.../UI/UiPrefs.kt` | 新增 | UI 偏好快照（`UiPrefs` 数据类 + 从 `SettingsStore` 流构建） |
| `.../UI/CloudHubDialog.kt` | 新增 | S6 向导状态机 |
| `.../UI/RecordingPanel.kt` | 改 | L3 seek + 合计 |
| `.../UI/Format.kt` | 改 | kbps/合计格式化 |
| `.../App/RxForegroundService.kt` | 新增 | 后台 RX 前台服务 + 通知 |
| `.../App/MainActivity.kt`、`AppSetup.kt`、`App/RootScreen.kt`、`App/ServiceLocator.kt`、`App/FT710App.kt` | 改 | 服务接线、通知权限、常亮偏好、沉浸式 |
| `app/src/main/AndroidManifest.xml`、`res/values/strings.xml` | 改 | FGS/通知权限、service 声明、文案 |
| `app/build.gradle.kts` | 改 | 1.1.0 / versionCode 3（任务 16） |
| `app/src/test/java/.../{Network,Data,Audio,Spectrum}` | 新增/改 | 全部纯逻辑单测 |
| `FT710Android/{CHANGELOG.md,CLAUDE.md,README,BUILD_GUIDE.md}`、`README.md`、`~/HAM/website/mrrc_modern/{index.html,zh/index.html}` | 改 | 文档与发布（任务 15/16） |

---

### 任务 0：基线与工作区证明

**文件：** 无源码改动

- [ ] **步骤 1：确认在隔离工作区、分支正确、工作区干净**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern/.worktrees/android-v1.0.0
git rev-parse --abbrev-ref HEAD     # 预期 feat/android-1.0.0
git status --short                  # 预期干净（除运行期文件外）
```

- [ ] **步骤 2：基线全绿证明**

```bash
cd FT710Android
export JAVA_HOME=$(/usr/libexec/java_home -v 17)
export ANDROID_HOME="$HOME/Library/Android/sdk"
./gradlew :app:testDebugUnitTest assembleDebug
```

预期：`BUILD SUCCESSFUL`，测试计数 = 52（`app/build/reports/tests/testDebugUnitTest/index.html` 可核对）。若基线红，先单独修基线并 commit，不得在红基线上叠加功能。

- [ ] **步骤 3：无 commit**（本任务只是证明）

---

### 任务 1：fullState 真实形状（D0）

**文件：**
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/Network/Protocol.kt`
- 测试：`FT710Android/app/src/test/java/com/hamradio/ft710android/Network/ProtocolTest.kt`
- 测试：`FT710Android/app/src/test/java/com/hamradio/ft710android/ViewModel/MainViewModelTest.kt`

- [ ] **步骤 1：写失败测试（真实形状 fixture）**

`ProtocolTest.kt`：把 `fullState parsed with bands...` 整个测试替换为以下内容（并在文件顶部确认已有 `kotlinx.serialization.json.int`/`jsonPrimitive` import）：

```kotlin
    @Test fun `real fullState shape parses bands as objects and filter tables as pairs`() {
        val text = """
        {"type":"fullState",
         "data":{"vfo_a_freq":7050000,"mode":2,"tx_status":0,"scope_span":6},
         "bands":[{"name":"40m","start":7000000,"end":7300000,"bsr":3,"default_freq":7050000}],
         "modes":["LSB","USB","CW-U"],
         "memChannels":[{"freq":7050000,"mode":"LSB","label":"M1"},null],
         "filterTables":{"voice":[[1,300],[2,400],[13,2400]],"narrow":[[1,50]],"narrowModes":["CW-U"]},
         "recording":{"recording":false,"freq_hz":0,"started_at":null,"duration":0.0,"name":null,"bytes":0,"dropped":0},
         "cq":{"state":"idle","duration_s":0.0,"elapsed_s":0.0,"frames_total":0,"frames_sent":0,"started_by":null,"reason":null,"ready":false},
         "radioModel":"ft710","radioDisplayName":"Yaesu FT-710",
         "capabilities":{"model_name":"ft710","display_name":"Yaesu FT-710","verified":true,"tx_gated":false,
           "has_atu":true,"has_auto_notch":true,"has_vd_id_meters":true,"filter_model":"width_table",
           "att_steps":[0,6,12,18],"preamp_steps":["OFF","AMP1","AMP2"],"scope_type":"ft4222",
           "scope_spans":{"6":{"name":"100 kHz","freq":100000}},
           "scope_speeds":["1","2","3","4","5"],"audio_gain_boost":10.0},
         "atr1000Enabled":true}
        """.trimIndent()
        val ev = parseWsEvent(text)
        assertTrue(ev is WsEvent.FullState)
        val f = ev as WsEvent.FullState
        assertEquals(1, f.bands.size)
        assertEquals("40m", f.bands[0].name)
        assertEquals(7050000L, f.bands[0].defaultFreq)
        assertEquals(listOf(listOf(1, 300), listOf(2, 400), listOf(13, 2400)), f.filterTables!!.voice)
        assertEquals("Yaesu FT-710", f.radioDisplayName)
        assertEquals("width_table", f.capabilities!!.filterModel)
        assertEquals(listOf(0, 6, 12, 18), f.capabilities!!.attSteps)
        assertEquals(100000L, f.capabilities!!.scopeSpans.getValue("6").freq)
        assertTrue(f.atr1000Enabled)
        // data 原样保留，供 RadioState.apply
        assertEquals(7050000, f.data["vfo_a_freq"]!!.jsonPrimitive.int)
    }

    @Test fun `legacy payload without capabilities still parses`() {
        val ev = parseWsEvent(
            """{"type":"fullState","data":{"mode":1},"bands":[{"name":"20m","default_freq":14270000}]}"""
        ) as WsEvent.FullState
        assertEquals(1, ev.bands.size)
        assertEquals(14270000L, ev.bands[0].defaultFreq)
        assertNull(ev.capabilities)
        assertNull(ev.radioDisplayName)
    }
```

`MainViewModelTest.kt`：把两个 fixture 中的 `"bands":["20m"]` / `"bands":[]` 改为对象数组（`"bands":[{"name":"20m","default_freq":14270000}]`），并新增断言：

```kotlin
        assertEquals("20m", vm.bands.value[0].name)
```

- [ ] **步骤 2：运行验证失败**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*ProtocolTest*' --tests '*MainViewModelTest*'
```

预期：编译失败（`unresolved reference: radioDisplayName` / `name`）或断言失败（`f.bands[0]` 类型不符）。

- [ ] **步骤 3：实现 DTO**

`Protocol.kt`：替换 `FilterTables` 与 `FullStateDto`，新增类型：

```kotlin
@Serializable
data class BandDto(
    val name: String = "",
    val start: Long = 0,
    val end: Long = 0,
    @SerialName("default_freq") val defaultFreq: Long = 0,
)

@Serializable
data class FilterTables(
    val voice: List<List<Int>> = emptyList(),
    val narrow: List<List<Int>> = emptyList(),
    @SerialName("narrowModes") val narrowModes: List<String> = emptyList(),
    val model: String? = null,
    val filDefaults: Map<String, List<Int>> = emptyMap(),
)

@Serializable
data class ScopeSpanDto(val name: String = "", val freq: Long = 0)

@Serializable
data class CapabilitiesDto(
    @SerialName("model_name") val modelName: String = "ft710",
    @SerialName("display_name") val displayName: String = "Yaesu FT-710",
    val verified: Boolean = true,
    @SerialName("tx_gated") val txGated: Boolean = false,
    @SerialName("has_atu") val hasAtu: Boolean = true,
    @SerialName("has_auto_notch") val hasAutoNotch: Boolean = true,
    @SerialName("has_vd_id_meters") val hasVdIdMeters: Boolean = true,
    @SerialName("filter_model") val filterModel: String = "width_table",
    @SerialName("att_steps") val attSteps: List<Int> = emptyList(),
    @SerialName("preamp_steps") val preampSteps: List<String> = emptyList(),
    @SerialName("scope_type") val scopeType: String = "ft4222",
    @SerialName("scope_spans") val scopeSpans: Map<String, ScopeSpanDto> = emptyMap(),
    @SerialName("scope_speeds") val scopeSpeeds: List<String> = emptyList(),
    @SerialName("audio_gain_boost") val audioGainBoost: Double = 10.0,
)
```

`FullStateDto` 相应改为：

```kotlin
@Serializable
data class FullStateDto(
    val type: String = "fullState",
    val data: JsonObject = JsonObject(emptyMap()),
    val bands: List<BandDto> = emptyList(),
    val modes: List<String> = emptyList(),
    val memChannels: List<JsonElement?> = emptyList(),
    @SerialName("filterTables") val filterTables: FilterTables? = null,
    @SerialName("atr1000Enabled") val atr1000Enabled: Boolean = false,
    val recording: RecordingStatusDto? = null,
    val cq: CqStatusDto? = null,
    val radioModel: String? = null,
    val radioDisplayName: String? = null,
    val capabilities: CapabilitiesDto? = null,
)
```

`WsEvent.FullState` 增加 `radioDisplayName: String?`、`capabilities: CapabilitiesDto?`；`parseWsEvent` 对应透传。`FullStateDto` 缺省值保证老服务端/缺键 payload 仍可解析。

- [ ] **步骤 4：运行验证通过（含全量回归）**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest
```

预期：`BUILD SUCCESSFUL`，全部测试通过（52 → 54）。

- [ ] **步骤 5：Commit**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern/.worktrees/android-v1.0.0
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Network/Protocol.kt \
        FT710Android/app/src/test/java/com/hamradio/ft710android/Network/ProtocolTest.kt \
        FT710Android/app/src/test/java/com/hamradio/ft710android/ViewModel/MainViewModelTest.kt
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "fix(android): parse the real fullState shape — bands are objects, filter tables are pairs

The DTOs declared List<String>/List<Int> while the server sends
{name,start,end,bsr,default_freq} dicts and [index,hz] pairs, so every
decode threw and the event was dropped: a connected v1.0.1 client showed
no state at all. Fixtures now mirror the live payload, with a legacy
no-capabilities case kept."
```

---

### 任务 2：能力表与 filter/ATT/PRE 循环（D1/D6）

**文件：**
- 创建：`FT710Android/app/src/main/java/com/hamradio/ft710android/Data/Capabilities.kt`
- 测试：`FT710Android/app/src/test/java/com/hamradio/ft710android/Data/CapabilitiesTest.kt`

- [ ] **步骤 1：写失败测试**

```kotlin
package com.hamradio.ft710android.Data

import com.hamradio.ft710android.Network.CapabilitiesDto
import com.hamradio.ft710android.Network.FilterTables
import com.hamradio.ft710android.Network.ScopeSpanDto
import org.junit.Assert.assertEquals
import org.junit.Test

class CapabilitiesTest {
    @Test fun `fallback is the ft710 table`() {
        val caps = RadioCaps.from(null)
        assertEquals(10000L, caps.spanHz(3))
        assertEquals(100000L, caps.spanHz(6))
        assertEquals(1000000L, caps.spanHz(9))
        assertEquals("100 kHz", caps.spanName(6))
        assertEquals(4, caps.attStepCount)
        assertEquals(3, caps.preampStepCount)
        assertEquals(10f, caps.audioBoost)
    }

    @Test fun `civ27 spans are half-width and doubled`() {
        val dto = CapabilitiesDto(
            scopeType = "civ27",
            scopeSpans = mapOf("0" to ScopeSpanDto("±100 kHz", 100_000)),
            attSteps = listOf(0, 12),
            preampSteps = listOf("OFF"),
            audioGainBoost = 1.0,
        )
        val caps = RadioCaps.from(dto)
        assertEquals(200000L, caps.spanHz(0))
        assertEquals("±100 kHz", caps.spanName(0))
        assertEquals(2, caps.attStepCount)
        assertEquals(1, caps.preampStepCount)
        assertEquals(1f, caps.audioBoost)
    }

    @Test fun `voice filter rotation matches the web curated list`() {
        assertEquals(13, Capabilities.nextFilter(9, "USB", null))
        assertEquals(17, Capabilities.nextFilter(13, "USB", null))
        assertEquals(23, Capabilities.nextFilter(20, "USB", null))
        assertEquals(9, Capabilities.nextFilter(23, "USB", null))
        assertEquals(9, Capabilities.nextFilter(5, "USB", null))
    }

    @Test fun `narrow modes use the narrow list from the server table`() {
        val tables = FilterTables(narrowModes = listOf("CW-U"))
        assertEquals(6, Capabilities.nextFilter(3, "CW-U", tables))
        assertEquals(10, Capabilities.nextFilter(6, "CW-U", tables))
        assertEquals(21, Capabilities.nextFilter(17, "CW-U", tables))
        assertEquals(3, Capabilities.nextFilter(21, "CW-U", tables)) // 末项回卷首项
    }

    @Test fun `fil123 rotates fil1 to fil3`() {
        assertEquals(2, Capabilities.nextFilter(1, "USB", null, "fil123"))
        assertEquals(3, Capabilities.nextFilter(2, "USB", null, "fil123"))
        assertEquals(1, Capabilities.nextFilter(3, "USB", null, "fil123"))
    }

    @Test fun `filter labels use the width tables`() {
        val tables = FilterTables(voice = listOf(listOf(13, 2400), listOf(23, 4000)))
        assertEquals("2.4k", Capabilities.filterLabel(13, "USB", tables, "width_table"))
        assertEquals("无", Capabilities.filterLabel(23, "USB", tables, "width_table"))
        assertEquals("--", Capabilities.filterLabel(7, "USB", tables, "width_table"))
    }

    @Test fun `fil123 labels use per-mode defaults`() {
        val tables = FilterTables(
            model = "fil123",
            filDefaults = mapOf("USB" to listOf(300, 2400, 3000)),
        )
        assertEquals("FIL2 2.4k", Capabilities.filterLabel(2, "USB", tables, "fil123"))
        assertEquals("FIL1 300Hz", Capabilities.filterLabel(1, "USB", tables, "fil123"))
    }
}
```

> 注：`narrow list` 是 `[3,6,10,13,17,21]`（Web `narrowList`），实现见步骤 3 的 `object Capabilities`。

- [ ] **步骤 2：运行验证失败**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*CapabilitiesTest*'
```

预期：编译失败 `unresolved reference: RadioCaps`。

- [ ] **步骤 3：实现**

```kotlin
package com.hamradio.ft710android.Data

import com.hamradio.ft710android.Network.CapabilitiesDto
import com.hamradio.ft710android.Network.FilterTables

/** 量程选项：idx 是发给服务端的 scope_span 值，hz 是**全宽**（CI-V 半幅已 ×2）。 */
data class SpanChoice(val idx: Int, val name: String, val hz: Long)

/**
 * 能力表的 JVM 侧视图：服务端 capabilities 缺失/字段缺失时逐项回退到 FT-710 值，
 * 行为与 web `applyRadioCapabilities` 的无能力分支一致（老服务端不受影响）。
 */
data class RadioCaps(
    val modelName: String = "ft710",
    val displayName: String = "Yaesu FT-710",
    val verified: Boolean = true,
    val txGated: Boolean = false,
    val hasAtu: Boolean = true,
    val hasAutoNotch: Boolean = true,
    val hasVdIdMeters: Boolean = true,
    val filterModel: String = "width_table",
    val attStepCount: Int = 4,
    val preampStepCount: Int = 3,
    val scopeType: String = "ft4222",
    val spans: List<SpanChoice> = FT710_SPANS,
    val speeds: List<String> = listOf("1", "2", "3", "4", "5"),
    val audioBoost: Float = 10f,
) {
    fun spanHz(idx: Int): Long = spans.firstOrNull { it.idx == idx }?.hz
        ?: spans.minByOrNull { Math.abs(it.idx - idx) }?.hz ?: 100_000L

    fun spanName(idx: Int): String = spans.firstOrNull { it.idx == idx }?.name ?: "100 kHz"

    companion object {
        val FT710_SPANS = listOf(
            SpanChoice(0, "1 kHz", 1_000),
            SpanChoice(1, "2 kHz", 2_000),
            SpanChoice(2, "5 kHz", 5_000),
            SpanChoice(3, "10 kHz", 10_000),
            SpanChoice(4, "20 kHz", 20_000),
            SpanChoice(5, "50 kHz", 50_000),
            SpanChoice(6, "100 kHz", 100_000),
            SpanChoice(7, "200 kHz", 200_000),
            SpanChoice(8, "500 kHz", 500_000),
            SpanChoice(9, "1 MHz", 1_000_000),
        )

        fun from(dto: CapabilitiesDto?): RadioCaps {
            if (dto == null) return RadioCaps()
            val half = dto.scopeType == "civ27"
            val spans = dto.scopeSpans.entries
                .mapNotNull { (k, v) ->
                    val idx = k.toIntOrNull() ?: return@mapNotNull null
                    if (v.freq <= 0) return@mapNotNull null
                    SpanChoice(idx, v.name.ifEmpty { "${v.freq / 1000} kHz" }, if (half) v.freq * 2 else v.freq)
                }
                .sortedBy { it.idx }
                .ifEmpty { FT710_SPANS }
            return RadioCaps(
                modelName = dto.modelName,
                displayName = dto.displayName,
                verified = dto.verified,
                txGated = dto.txGated,
                hasAtu = dto.hasAtu,
                hasAutoNotch = dto.hasAutoNotch,
                hasVdIdMeters = dto.hasVdIdMeters,
                filterModel = dto.filterModel,
                attStepCount = if (dto.attSteps.isEmpty()) 4 else dto.attSteps.size,
                preampStepCount = if (dto.preampSteps.isEmpty()) 3 else dto.preampSteps.size,
                scopeType = dto.scopeType,
                spans = spans,
                speeds = dto.scopeSpeeds.ifEmpty { listOf("1", "2", "3", "4", "5") },
                audioBoost = dto.audioGainBoost.toFloat(),
            )
        }
    }
}

/** filter/ATT/PRE 循环与标签（web `getNextFilter/getFilterLabel` 的 JVM 版）。 */
object Capabilities {
    private val LEGACY_NARROW = setOf("CW-U", "CW-L", "RTTY-L", "RTTY-U", "DATA-L", "DATA-U", "PSK")

    fun isNarrowMode(mode: String, tables: FilterTables?): Boolean {
        val list = tables?.narrowModes
        return if (!list.isNullOrEmpty()) list.contains(mode) else LEGACY_NARROW.contains(mode)
    }

    /** Web `getNextFilter`：voice[9,13,17,20,23] / narrow[3,6,10,13,17,21]，列表外/末项回卷首项。 */
    fun nextFilter(current: Int, mode: String, tables: FilterTables?, model: String = "width_table"): Int {
        if (model == "fil123") return if (current in 1..2) current + 1 else 1
        val list = if (isNarrowMode(mode, tables)) intArrayOf(3, 6, 10, 13, 17, 21)
                   else intArrayOf(9, 13, 17, 20, 23)
        val pos = list.indexOf(current)
        return if (pos < 0 || pos >= list.size - 1) list[0] else list[pos + 1]
    }

    /** Web `getFilterLabel`：width_table 查 [idx,hz] 数对（4000 = “无”）；fil123 用 filDefaults。 */
    fun filterLabel(idx: Int, mode: String, tables: FilterTables?, model: String = "width_table"): String {
        if (model == "fil123") {
            val hz = tables?.filDefaults?.get(mode)?.getOrNull(idx - 1) ?: return "FIL$idx"
            val w = if (hz >= 1000) "%.1fk".format(java.util.Locale.US, hz / 1000f) else "${hz}Hz"
            return "FIL$idx $w"
        }
        val pair = (if (isNarrowMode(mode, tables)) tables?.narrow else tables?.voice)
            ?.firstOrNull { it.size >= 2 && it[0] == idx }
        val hz = pair?.get(1) ?: return "--"
        if (hz == 4000) return "无"
        return if (hz >= 1000) "%.1fk".format(java.util.Locale.US, hz / 1000f) else "${hz}Hz"
    }
}
```

- [ ] **步骤 4：运行验证通过**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*CapabilitiesTest*'
```

预期：PASS（7 项）。

- [ ] **步骤 5：Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Data/Capabilities.kt \
        FT710Android/app/src/test/java/com/hamradio/ft710android/Data/CapabilitiesTest.kt
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(android): capability tables — scope spans (civ27 doubled), web filter rotation, att/pre counts"
```

---

### 任务 3：频率输入与 QSY 数学（M1/M2 纯函数）

**文件：**
- 创建：`FT710Android/app/src/main/java/com/hamradio/ft710android/Data/FreqInput.kt`
- 测试：`FT710Android/app/src/test/java/com/hamradio/ft710android/Data/FreqInputTest.kt`

- [ ] **步骤 1：写失败测试**

```kotlin
package com.hamradio.ft710android.Data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class FreqInputTest {
    @Test fun `mhz with dot`() = assertEquals(7_050_000L, FreqInput.parse("7.05"))
    @Test fun `khz without dot under 100000`() = assertEquals(7_050_000L, FreqInput.parse("7050"))
    @Test fun `hz over 100000`() = assertEquals(7_050_000L, FreqInput.parse("7050000"))
    @Test fun `sub-mhz`() = assertEquals(500_000L, FreqInput.parse("0.5"))
    @Test fun `blank and junk are null`() {
        assertNull(FreqInput.parse(""))
        assertNull(FreqInput.parse("   "))
        assertNull(FreqInput.parse("abc"))
    }
    @Test fun `clamps to the transceiver range`() {
        assertEquals(30_000L, FreqInput.parse("0.001"))
        assertEquals(75_000_000L, FreqInput.parse("100"))
    }
    @Test fun `qsy maps a fraction of the span around the vfo`() {
        assertEquals(14_270_000L, FreqInput.qsy(14_270_000L, 100_000L, 0.5f))
        assertEquals(14_220_000L, FreqInput.qsy(14_270_000L, 100_000L, 0f))
        assertEquals(14_320_000L, FreqInput.qsy(14_270_000L, 100_000L, 1f))
        assertEquals(30_000L, FreqInput.qsy(1_000_000L, 100_000L, 0f))
    }
}
```

- [ ] **步骤 2：运行验证失败**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*FreqInputTest*'
```

预期：编译失败 `unresolved reference: FreqInput`。

- [ ] **步骤 3：实现**

```kotlin
package com.hamradio.ft710android.Data

import kotlin.math.roundToLong

/** 频率输入与点击调频的数学，逐条对齐 web `static/ft710_ui.js:commitFreq/wireScopeQSY`。 */
object FreqInput {
    const val MIN_HZ = 30_000L
    const val MAX_HZ = 75_000_000L

    /** 解析 MHz 输入框；无法解析返回 null。 */
    fun parse(raw: String): Long? {
        val s = raw.trim()
        if (s.isEmpty()) return null
        val v = s.toDoubleOrNull() ?: return null
        var hz = v
        if (hz < 100_000 && !s.contains('.')) hz *= 1_000      // kHz
        if (hz < 1_000) hz *= 1_000_000                        // MHz
        return hz.roundToLong().coerceIn(MIN_HZ, MAX_HZ)
    }

    /** 中心模式（服务器恒 EX040200）下，瀑布 x 比例 → 目标频率。 */
    fun qsy(vfoFreq: Long, spanHz: Long, fraction: Float): Long {
        val f = fraction.coerceIn(0f, 1f)
        val hz = vfoFreq - spanHz / 2.0 + f * spanHz
        return hz.roundToLong().coerceIn(MIN_HZ, MAX_HZ)
    }
}
```

- [ ] **步骤 4：运行验证通过**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*FreqInputTest*'
```

预期：PASS（7 项）。

- [ ] **步骤 5：Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Data/FreqInput.kt \
        FT710Android/app/src/test/java/com/hamradio/ft710android/Data/FreqInputTest.kt
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(android): web-exact frequency input parse and click-to-QSY math"
```

---

### 任务 4：网络层 — ATR 事件、RTT/字节统计、连接开关（M3/M4/M7 网络部分）

**文件：**
- 创建：`FT710Android/app/src/main/java/com/hamradio/ft710android/Network/NetworkStats.kt`
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/Network/Protocol.kt`（ATR DTO + `parseAtrEvent`）
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/Network/ConnectionManager.kt`
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/App/ServiceLocator.kt`、`ViewModel/MainViewModel.kt`（最小接线，保证编译）
- 测试：`FT710Android/app/src/test/java/com/hamradio/ft710android/Network/NetworkStatsTest.kt`、`ProtocolTest.kt`（追加 ATR 用例）

- [ ] **步骤 1：写失败测试**

`NetworkStatsTest.kt`：

```kotlin
package com.hamradio.ft710android.Network

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class NetworkStatsTest {
    @Test fun `byte counters drain per second`() {
        val s = NetworkStats(nowMs = { 0 })
        s.onReceived(100); s.onReceived(50); s.onSent(20)
        assertEquals(150L, s.drainRx()); assertEquals(150L, s.drainRx())  // 自上次取走
        assertEquals(20L, s.drainTx()); assertEquals(0L, s.drainTx())
    }

    @Test fun `rtt is measured from ping to pong with the injected clock`() {
        var now = 1_000L
        val s = NetworkStats(nowMs = { now })
        s.onPingSent()
        now = 1_042L
        s.onPong()
        assertEquals(42L, s.lastRttMs)
    }

    @Test fun `pong without a ping leaves rtt untouched`() {
        val s = NetworkStats(nowMs = { 0 })
        s.onPong()
        assertNull(s.lastRttMs)
    }
}
```

`ProtocolTest.kt` 追加：

```kotlin
    @Test fun `atr state and tune result parse`() {
        val st = parseAtrEvent(
            """{"type":"atrState","connected":true,"power":12.5,"swr":1.4,"sw":1,"ind":3,"cap":7,"tuning":false,"tx":false,"freq":7050000,"last_update":1.0}"""
        ) as AtrEvent.State
        assertEquals(12.5, st.s.power, 0.001)
        assertEquals(1, st.s.sw)
        assertEquals(3, st.s.ind)
        val tr = parseAtrEvent(
            """{"type":"atrTuneResult","phase":"auto_success","swr_before":3.2,"swr_after":1.3,"auto":true}"""
        ) as AtrEvent.TuneResult
        assertEquals("auto_success", tr.r.phase)
        assertEquals(3.2, tr.r.swrBefore ?: 0.0, 0.001)
    }

    @Test fun `atr error parses and unknown ATR payload is null`() {
        val err = parseAtrEvent("""{"type":"error","message":"ATR1000 not connected"}""")
        assertEquals("ATR1000 not connected", (err as AtrEvent.Error).message)
        assertNull(parseAtrEvent("""{"type":"pong"}"""))
    }

    @Test fun `atr tune text follows the web copy`() {
        assertEquals("ATR 连续 3 次无改善，已放弃该频点自动调谐",
            AtrText.result(AtrTuneResultDto(phase = "auto_giveup")))
        assertEquals("ATR 调谐完成: SWR 3.2 → 1.3",
            AtrText.result(AtrTuneResultDto(phase = "success", swrBefore = 3.2, swrAfter = 1.3)))
    }
```

- [ ] **步骤 2：运行验证失败**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*NetworkStatsTest*' --tests '*ProtocolTest*'
```

预期：编译失败 `unresolved reference: NetworkStats` / `AtrEvent`。

- [ ] **步骤 3：实现**

`NetworkStats.kt`：

```kotlin
package com.hamradio.ft710android.Network

/**
 * 5 路 WS 的收发字节计数 + RTT。时钟可注入，纯 JVM 可测。
 * 计数器是"自上次 drain 以来"的增量：MainViewModel 每秒取一次算 kbps。
 */
class NetworkStats(private val nowMs: () -> Long = { System.currentTimeMillis() }) {
    private var rx = 0L
    private var tx = 0L
    private var pingAt: Long? = null

    @Volatile var lastRttMs: Long? = null
        private set

    fun onReceived(bytes: Int) { if (bytes > 0) rx += bytes }
    fun onSent(bytes: Int) { if (bytes > 0) tx += bytes }
    fun onPingSent() { pingAt = nowMs() }
    fun onPong() { pingAt?.let { lastRttMs = nowMs() - it }; pingAt = null }

    fun drainRx(): Long = rx.also { rx = 0 }
    fun drainTx(): Long = tx.also { tx = 0 }
}
```

`Protocol.kt` 追加：

```kotlin
@Serializable
data class AtrStateDto(
    val connected: Boolean = false,
    val power: Double = 0.0,
    val swr: Double = 0.0,
    val sw: Int = 0,
    val ind: Int = 0,
    val cap: Int = 0,
    @SerialName("ind_uh") val indUh: Double = 0.0,
    @SerialName("cap_pf") val capPf: Double = 0.0,
    val tuning: Boolean = false,
    val tx: Boolean = false,
    val freq: Long = 0,
    @SerialName("last_update") val lastUpdate: Double = 0.0,
)

@Serializable
data class AtrTuneResultDto(
    val phase: String = "",
    @SerialName("swr_before") val swrBefore: Double? = null,
    @SerialName("swr_after") val swrAfter: Double? = null,
    val message: String? = null,
    val auto: Boolean = false,
)

sealed class AtrEvent {
    data class State(val s: AtrStateDto) : AtrEvent()
    data class TuneResult(val r: AtrTuneResultDto) : AtrEvent()
    data class Error(val message: String) : AtrEvent()
}

/** /WSatr1000 文本 → 事件；非 ATR 消息返回 null。 */
fun parseAtrEvent(text: String): AtrEvent? {
    val root = runCatching { json.parseToJsonElement(text).jsonObject }.getOrNull() ?: return null
    val type = root["type"]?.jsonPrimitive?.contentOrNull ?: return null
    return when (type) {
        "atrState" -> runCatching { AtrEvent.State(json.decodeFromString<AtrStateDto>(text)) }.getOrNull()
        "atrTuneResult" -> runCatching { AtrEvent.TuneResult(json.decodeFromString<AtrTuneResultDto>(text)) }.getOrNull()
        "error" -> AtrEvent.Error(ServerErrorDto.from(text)?.message.orEmpty())
        else -> null
    }
}
```

> `ServerErrorDto` 已有；若没有 `from`，就地 `json.decodeFromString<ServerErrorDto>(text).message`。

同一文件追加调谐结果文案（逐条对齐 `static/modules/atr1000.js:tuneResultText`）：

```kotlin
/** Web `tuneResultText` 的中文文案（跳过/成功/回滚/自动六阶段/错误回退）。 */
object AtrText {
    fun result(r: AtrTuneResultDto): String = when (r.phase) {
        "skipped" -> "ATR: SWR ${r.swrBefore ?: "?"} 已达标，无需调谐"
        "success" -> "ATR 调谐完成: SWR ${r.swrBefore} → ${r.swrAfter}"
        "rollback" -> "ATR 调谐无改善，已回滚 (SWR ${r.swrBefore})"
        "auto_success" -> "ATR 自动调谐完成: SWR ${r.swrBefore} → ${r.swrAfter}"
        "auto_no_improve" -> "ATR 自动调谐无改善 (SWR ${r.swrBefore} → ${r.swrAfter})"
        "auto_timeout" -> "ATR 自动调谐超时 (SWR ${r.swrBefore})"
        "auto_aborted" -> "ATR 自动调谐中断: ${r.message ?: "天调断开"}"
        "auto_giveup" -> "ATR 连续 3 次无改善，已放弃该频点自动调谐"
        else -> "ATR 调谐失败: ${r.message ?: r.phase}"
    }
}
```

`ConnectionManager.kt`：
- 构造参数 `onAtrEvent: (AtrEvent) -> Unit`（替代 `(String) -> Unit`）。
- 新增 `private val stats = NetworkStats(nowMs)`（构造参数 `nowMs: () -> Long = { System.currentTimeMillis() }`）。
- `connect(...)`：包装 `onText = { stats.onReceived(it.length); cb(it) }`、`onBinary = { stats.onReceived(it.size); cb(it) }`。
- ATR 连接：`onText = { parseAtrEvent(it)?.let(onAtrEvent) }`。
- `sendPing()`：`stats.onPingSent(); dispatch(ping())`；在 radio 包装里 `if (parseWsEvent(it) is WsEvent.Pong) stats.onPong()`（或让 VM 在 `onWsEvent` 收到 `WsEvent.Pong` 时调 `cm.onPong()`——采用后者，radio 包装只做字节计数）。
- `sendTxAudioBinary`/`sendTxAudioText`/`dispatch`/`sendAtrTune` 都 `stats.onSent(...)`。
- 新增 `fun sendAtrTune()`、`fun lastRttMs(): Long?`、`fun drainRx(): Long`、`fun drainTx(): Long`、`fun onPong()`、`fun disconnect()`（= `stopAll()`）。

`ServiceLocator.kt` / `MainViewModel.kt` 最小接线：`onAtrEvent = { vm.onAtrEvent(it) }`；`MainViewModel` 增加 `fun onAtrEvent(ev: AtrEvent)`（先存一个 `_atrState` StateFlow；注意旧的 `_atr` 已是 `atr1000Enabled` 布尔标志，不得复用名字，任务 8 再扩展）；`onWsEvent` 里 `is WsEvent.Pong -> connectionManager.onPong()`。

- [ ] **步骤 4：运行验证通过（含全量）**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest
```

预期：`BUILD SUCCESSFUL`。

- [ ] **步骤 5：Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Network/ \
        FT710Android/app/src/main/java/com/hamradio/ft710android/App/ServiceLocator.kt \
        FT710Android/app/src/main/java/com/hamradio/ft710android/ViewModel/MainViewModel.kt \
        FT710Android/app/src/test/java/com/hamradio/ft710android/Network/
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(android): ATR1000 events, byte/RTT stats and a hard disconnect on the network layer"
```

---

### 任务 5：本地偏好与 RX 增益公式（D2/S1/S2/M6/L1 的存储与数学部分）

**文件：**
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/Data/SettingsStore.kt`
- 创建：`FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/RxGain.kt`
- 测试：`FT710Android/app/src/test/java/com/hamradio/ft710android/Audio/RxGainTest.kt`

- [ ] **步骤 1：写失败测试**

```kotlin
package com.hamradio.ft710android.Audio

import org.junit.Assert.assertEquals
import org.junit.Test

class RxGainTest {
    @Test fun `ft710 needs the 10x boost, capped`() {
        assertEquals(5.0196075f, RxGain.target(128, 10f, transmitting = false), 0.0001f)
        assertEquals(10f, RxGain.target(255, 10f, transmitting = false), 0.0001f)
    }

    @Test fun `ic7300 boost is unity`() {
        assertEquals(0.5019608f, RxGain.target(128, 1f, transmitting = false), 0.0001f)
    }

    @Test fun `transmitting mutes playback`() {
        assertEquals(0f, RxGain.target(255, 10f, transmitting = true), 0.0001f)
    }
}
```

- [ ] **步骤 2：运行验证失败**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*RxGainTest*'
```

预期：编译失败 `unresolved reference: RxGain`。

- [ ] **步骤 3：实现**

`Audio/RxGain.kt`：

```kotlin
package com.hamradio.ft710android.Audio

import kotlin.math.min

/** Web `_applyAfGainToAudioNode` 的纯函数版：min(10, vol/255 × boost)，TX/TUNE 时 0。 */
object RxGain {
    const val MAX = 10f

    fun target(volume: Int, boost: Float, transmitting: Boolean): Float {
        if (transmitting) return 0f
        val v = volume.coerceIn(0, 255) / 255f
        return min(MAX, v * boost)
    }
}
```

`SettingsStore.kt`：在现有 host/port 基础上加偏好（全部 `preferencesDataStore(name = "settings")` 同一实例）：

```kotlin
    private object Keys {
        val host = stringPreferencesKey("host")
        val port = stringPreferencesKey("port")
        val afVol = intPreferencesKey("afVol")
        val micVol = intPreferencesKey("micVol")
        val micGain = intPreferencesKey("micGain")
        val scopeTheme = stringPreferencesKey("scopeTheme")
        val scopeFloor = intPreferencesKey("scopeFloor")
        val scopeCeil = intPreferencesKey("scopeCeil")
        val fftHeight = intPreferencesKey("fftHeight")
        val wfHeight = intPreferencesKey("wfHeight")
        val keepScreenOn = booleanPreferencesKey("keepScreenOn")
        val backgroundRx = booleanPreferencesKey("backgroundRx")
    }

    val afVol: Flow<Int> = context.dataStore.data.map { it[Keys.afVol] ?: 128 }
    val micVol: Flow<Int> = context.dataStore.data.map { it[Keys.micVol] ?: 100 }
    val micGain: Flow<Int?> = context.dataStore.data.map { it[Keys.micGain] }
    val scopeTheme: Flow<String> = context.dataStore.data.map { it[Keys.scopeTheme] ?: "jet" }
    val scopeFloor: Flow<Int> = context.dataStore.data.map { it[Keys.scopeFloor] ?: 5 }
    val scopeCeil: Flow<Int> = context.dataStore.data.map { it[Keys.scopeCeil] ?: 220 }
    val fftHeight: Flow<Int> = context.dataStore.data.map { it[Keys.fftHeight] ?: 40 }
    val wfHeight: Flow<Int> = context.dataStore.data.map { it[Keys.wfHeight] ?: 110 }
    val keepScreenOn: Flow<Boolean> = context.dataStore.data.map { it[Keys.keepScreenOn] ?: true }
    val backgroundRx: Flow<Boolean> = context.dataStore.data.map { it[Keys.backgroundRx] ?: true }

    suspend fun putAfVol(v: Int) = edit { it[Keys.afVol] = v.coerceIn(0, 255) }
    suspend fun putMicVol(v: Int) = edit { it[Keys.micVol] = v.coerceIn(0, 200) }
    suspend fun putMicGain(v: Int) = edit { it[Keys.micGain] = v.coerceIn(0, 100) }
    suspend fun putScopeTheme(v: String) = edit { it[Keys.scopeTheme] = v }
    suspend fun putScopeFloor(v: Int) = edit { it[Keys.scopeFloor] = v.coerceIn(0, 200) }
    suspend fun putScopeCeil(v: Int) = edit { it[Keys.scopeCeil] = v.coerceIn(50, 255) }
    suspend fun putFftHeight(v: Int) = edit { it[Keys.fftHeight] = v.coerceIn(20, 120) }
    suspend fun putWfHeight(v: Int) = edit { it[Keys.wfHeight] = v.coerceIn(30, 200) }
    suspend fun putKeepScreenOn(v: Boolean) = edit { it[Keys.keepScreenOn] = v }
    suspend fun putBackgroundRx(v: Boolean) = edit { it[Keys.backgroundRx] = v }

    private suspend fun edit(block: (androidx.datastore.preferences.core.MutablePreferences) -> Unit) {
        context.dataStore.edit(block)
    }
```

（`withContext(Dispatchers.IO)` 已有模式；`edit` 就在 DataStore 内部切 IO。）

- [ ] **步骤 4：运行验证通过**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*RxGainTest*' && ./gradlew :app:compileDebugKotlin
```

预期：PASS + `BUILD SUCCESSFUL`。

- [ ] **步骤 5：Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Data/SettingsStore.kt \
        FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/RxGain.kt \
        FT710Android/app/src/test/java/com/hamradio/ft710android/Audio/RxGainTest.kt
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(android): local preferences (volume/scope/keepscreen/background) and the web RX gain rule"
```

---

### 任务 6：RX 播放 — 增益、TX 静音、抖动统计（D2/M4 音频部分）

**文件：**
- 创建：`FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/Resampler.kt`
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/RxAudioPlayer.kt`
- 测试：`FT710Android/app/src/test/java/com/hamradio/ft710android/Audio/ResamplerTest.kt`

- [ ] **步骤 1：写失败测试**

```kotlin
package com.hamradio.ft710android.Audio

import org.junit.Assert.assertEquals
import org.junit.Test

class ResamplerTest {
    @Test fun `882 to 960 keeps a DC level`() {
        val out = Resampler.resample882To960(ShortArray(882) { 1000 })
        assertEquals(960, out.size)
        assertEquals(1000.toShort(), out[0])
        assertEquals(1000.toShort(), out[959])
    }

    @Test fun `linear interpolation follows a ramp`() {
        val input = ShortArray(882) { it.toShort() }
        val out = Resampler.resample882To960(input)
        assertEquals(0, out.first().toInt())
        // 最后一点的位置 = 959 × 44100/48000 ≈ 881.08 → 值 ≈ 881
        assertEquals(881, out.last().toInt())
    }

    @Test fun `empty input yields empty output`() {
        assertEquals(0, Resampler.resample(ShortArray(0), 44100, 48000).size)
    }

    @Test fun `clamps via rounding`() {
        val out = Resampler.resample(shortArrayOf(-32768, 32767), 44100, 48000)
        assertEquals(-32768, out.first().toInt())
    }
}
```

- [ ] **步骤 2：运行验证失败**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*ResamplerTest*'
```

预期：编译失败 `unresolved reference: Resampler`。

- [ ] **步骤 3：实现**

`Audio/Resampler.kt`：

```kotlin
package com.hamradio.ft710android.Audio

import kotlin.math.roundToInt
import kotlin.math.roundToLong

/** 44.1k↔48k 线性插值（与 `audio_resample.py` 同算法）；882@44.1k = 960@48k = 20ms。 */
object Resampler {
    fun resample(input: ShortArray, inRate: Int, outRate: Int): ShortArray {
        if (input.isEmpty() || inRate <= 0 || outRate <= 0) return ShortArray(0)
        val outLen = (input.size.toLong() * outRate / inRate.toDouble()).roundToLong().toInt().coerceAtLeast(1)
        val out = ShortArray(outLen)
        for (i in 0 until outLen) {
            val t = i.toDouble() * inRate / outRate
            val i0 = t.toInt().coerceIn(0, input.size - 1)
            val i1 = (i0 + 1).coerceAtMost(input.size - 1)
            val v = input[i0] * (1.0 - (t - i0)) + input[i1] * (t - i0)
            out[i] = v.roundToInt().coerceIn(-32768, 32767).toShort()
        }
        return out
    }

    fun resample882To960(input: ShortArray): ShortArray = resample(input, 44100, 48000)
}
```

`RxAudioPlayer.kt`：
- 字段：`private var volume = 128`、`private var boost = 10f`、`private var transmitting = false`、`private var gain = 1f`。
- `fun setVolume(v: Int)`、`fun setBoost(b: Float)`、`fun setTransmitting(t: Boolean)` → 都更新 `gain = RxGain.target(volume, boost, transmitting)`（`@Volatile`）。
- `playLoop()`：写 AudioTrack 前按 `gain` 缩放：`for (i in buf.indices) buf[i] = (buf[i] * gain).roundToInt().coerceIn(-32768,32767).toShort()`。`gain == 1f` 时跳过（快路径）；`gain == 0f` 时写静音帧（自然静音）。
- `val bufferMs: Int get() = synchronized(jitter) { jitter.size } * FRAME_MS`。

- [ ] **步骤 4：运行验证通过**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*ResamplerTest*' && ./gradlew :app:compileDebugKotlin
```

预期：PASS + 编译成功。

- [ ] **步骤 5：Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/
        FT710Android/app/src/test/java/com/hamradio/ft710android/Audio/ResamplerTest.kt
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(android): RX playback gain (boost + TX mute) and one JVM-tested resampler"
```

---

### 任务 7：TX 采集 — 44.1k 重采样与 Mic Vol（L2/S2）

**文件：**
- 创建：`FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/TxFraming.kt`
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/TxAudioCapture.kt`
- 测试：`FT710Android/app/src/test/java/com/hamradio/ft710android/Audio/TxFramingTest.kt`

- [ ] **步骤 1：写失败测试**

```kotlin
package com.hamradio.ft710android.Audio

import org.junit.Assert.assertEquals
import org.junit.Test

class TxFramingTest {
    @Test fun `frame length is 20ms at either rate`() {
        assertEquals(960, TxFraming.frameSamples(48000))
        assertEquals(882, TxFraming.frameSamples(44100))
    }

    @Test fun `mic volume scales and clamps`() {
        val half = TxFraming.applyMicVol(shortArrayOf(1000, -1000), 50)
        assertEquals(500, half[0].toInt())
        assertEquals(-500, half[1].toInt())
        val loud = TxFraming.applyMicVol(shortArrayOf(20000, -20000), 200)
        assertEquals(32767, loud[0].toInt())
        assertEquals(-32768, loud[1].toInt())
        val zero = TxFraming.applyMicVol(shortArrayOf(1000), 0)
        assertEquals(0, zero[0].toInt())
    }

    @Test fun `unity volume leaves samples untouched`() {
        val src = shortArrayOf(123, -456)
        assertEquals(123, TxFraming.applyMicVol(src, 100)[0].toInt())
    }
}
```

- [ ] **步骤 2：运行验证失败**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*TxFramingTest*'
```

预期：编译失败 `unresolved reference: TxFraming`。

- [ ] **步骤 3：实现**

`Audio/TxFraming.kt`：

```kotlin
package com.hamradio.ft710android.Audio

import kotlin.math.roundToInt

/** TX 20ms 帧的纯数学：帧长与 Mic Vol 缩放（vol 100 = 原始）。 */
object TxFraming {
    const val FRAME_48 = 960
    const val FRAME_44 = 882

    fun frameSamples(rate: Int): Int = if (rate == 44100) FRAME_44 else FRAME_48

    fun applyMicVol(samples: ShortArray, vol: Int): ShortArray {
        if (vol == 100) return samples
        val f = vol.coerceIn(0, 200) / 100f
        return ShortArray(samples.size) {
            (samples[it] * f).roundToInt().coerceIn(-32768, 32767).toShort()
        }
    }
}
```

`TxAudioCapture.kt`：
- 探测采样率：`private fun openRecord(rate: Int): AudioRecord?`（复用现有构造；`AudioRecord.getState() != STATE_INITIALIZED` 或 `getMinBufferSize <= 0` → null）。先试 48000，再试 44100；都失败 → `onError("无法打开麦克风（48k/44.1k 均失败）")` 并返回。
- `var micVol = 100; private set` + `fun setMicVol(v: Int)`。
- 采集循环：累积样本到 `ShortArray(frameSamples)`；凑满一帧 → `TxFraming.applyMicVol(frame, micVol)`；若 `rate == 44100` → `Resampler.resample882To960(...)`（输出恰 960）→ Opus 编码。不足一帧留在累积区（下次读续接）。
- 采集起始时间不变（`AudioSource.VOICE_COMMUNICATION`）。

- [ ] **步骤 4：运行验证通过**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*TxFramingTest*' && ./gradlew :app:compileDebugKotlin
```

预期：PASS + 编译成功。

- [ ] **步骤 5：Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/TxFraming.kt \
        FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/TxAudioCapture.kt \
        FT710Android/app/src/test/java/com/hamradio/ft710android/Audio/TxFramingTest.kt
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(android): TX capture falls back to 44.1 kHz (882→960) and honours the local mic volume"
```

---

### 任务 8：ViewModel 接线（M4/M5/M7/S2/S8 数据层）

**文件：**
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/ViewModel/MainViewModel.kt`
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/App/ServiceLocator.kt`
- 测试：`FT710Android/app/src/test/java/com/hamradio/ft710android/ViewModel/MainViewModelTest.kt`（追加）

- [ ] **步骤 1：写失败测试**

```kotlin
    @Test fun `fullState exposes capabilities and display name`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val vm = MainViewModel(null, cm(scope), null, null, null, null, null, scope)
        vm.onWsEvent(parseWsEvent(
            """{"type":"fullState","data":{},"bands":[{"name":"40m","default_freq":7050000}],"modes":["USB"],
                "radioDisplayName":"Yaesu FT-710","capabilities":{"scope_type":"civ27","audio_gain_boost":1.0}}"""
        ))
        assertEquals("Yaesu FT-710", vm.displayName.value)
        assertEquals(1f, vm.caps.value.audioBoost)
        assertEquals("civ27", vm.caps.value.scopeType)
    }

    @Test fun `saved mic gain is pushed back when it differs`() = runTest(UnconfinedTestDispatcher()) {
        val sent = mutableListOf<String>()
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val cm = ConnectionManager(OkHttpClient(), scope, {}, {}, {}, {}, {}, {}, sendOverride = { sent.add(it) })
        val vm = MainViewModel(null, cm, null, null, null, null, null, scope)
        vm.setSavedMicGain(55)
        vm.onWsEvent(parseWsEvent("""{"type":"fullState","data":{"mic_gain":20},"bands":[],"modes":[],"memChannels":[]}"""))
        assertTrue(sent.any { it.contains("\"mic_gain\"") && it.contains("55") })
    }

    @Test fun `qsy targets the active vfo field`() = runTest(UnconfinedTestDispatcher()) {
        val sent = mutableListOf<String>()
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val cm = ConnectionManager(OkHttpClient(), scope, {}, {}, {}, {}, {}, {}, sendOverride = { sent.add(it) })
        val vm = MainViewModel(null, cm, null, null, null, null, null, scope)
        vm.onWsEvent(parseWsEvent(
            """{"type":"fullState","data":{"vfo_a_freq":14270000,"active_vfo":"A","scope_span":6},"bands":[],"modes":[],"memChannels":[]}"""
        ))
        vm.qsy(0.5f)
        assertTrue(sent.last().contains("\"field\":\"freq\"") && sent.last().contains("14270000"))
        vm.onWsEvent(parseWsEvent("""{"type":"stateUpdate","fields":{"active_vfo":"B"},"dirty":["active_vfo"]}"""))
        vm.qsy(0f)
        assertTrue(sent.last().contains("\"field\":\"vfo_b_freq\""))
    }

    @Test fun `disconnect marks the user-off state`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val cm = ConnectionManager(OkHttpClient(), scope, {}, {}, {}, {}, {}, {}, sendOverride = {})
        val vm = MainViewModel(null, cm, null, null, null, null, null, scope)
        vm.disconnect()
        assertTrue(vm.userOff.value)
    }
```

- [ ] **步骤 2：运行验证失败**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*MainViewModelTest*'
```

预期：编译失败 `unresolved reference: displayName/caps/setSavedMicGain/qsy/userOff`。

- [ ] **步骤 3：实现**

`MainViewModel` 新增（保持既有构造与行为不变）：

```kotlin
    private val _caps = MutableStateFlow(RadioCaps())
    val caps: StateFlow<RadioCaps> = _caps
    private val _displayName = MutableStateFlow("")
    val displayName: StateFlow<String> = _displayName
    private val _atrState = MutableStateFlow<AtrStateDto?>(null)
    val atrState: StateFlow<AtrStateDto?> = _atrState
    private val _atrTuning = MutableStateFlow(false)
    val atrTuning: StateFlow<Boolean> = _atrTuning
    private val _notice = MutableStateFlow<String?>(null)
    val notice: StateFlow<String?> = _notice
    private val _userOff = MutableStateFlow(false)
    val userOff: StateFlow<Boolean> = _userOff
    private val _rttMs = MutableStateFlow<Long?>(null)
    val rttMs: StateFlow<Long?> = _rttMs
    private val _rxKbps = MutableStateFlow(0L)
    val rxKbps: StateFlow<Long> = _rxKbps
    private val _txKbps = MutableStateFlow(0L)
    val txKbps: StateFlow<Long> = _txKbps
    private val _recordingsCount = MutableStateFlow(0)
    val recordingsCount: StateFlow<Int> = _recordingsCount
    private val _recordingsBytes = MutableStateFlow(0L)
    val recordingsBytes: StateFlow<Long> = _recordingsBytes
    private var savedMicGain: Int? = null
    private var statsJob: Job? = null
```

- `onWsEvent` FullState 分支追加：`_caps.value = RadioCaps.from(ev.capabilities)`、`_displayName.value = ev.radioDisplayName ?: _caps.value.displayName`、`applySavedMicGain()`。
- `onWsEvent` 追加 `is WsEvent.Pong -> connectionManager.onPong()`（任务 4 已加）。
- `fun onAtrEvent(ev: AtrEvent)`：State → `_atrState.value = ev.s`；TuneResult → `_atrTuning.value = ev.r.phase in listOf("start","auto_start")`，其余阶段 `_notice.value = AtrText.result(ev.r)`；Error → `_notice.value = ev.message`。`AtrText.result(r)` 按 `static/modules/atr1000.js:tuneResultText` 输出中文（skipped/success/rollback/auto_* 六个阶段 + error 回退用 `message`）。
- `fun setSavedMicGain(v: Int?) { savedMicGain = v; applySavedMicGain() }`；`private fun applySavedMicGain() { val v = savedMicGain ?: return; if (v != state.micGain) sendSet("mic_gain", v) }`（收到 fullState 即证明控制通道在，不再额外判连接）。
- `fun qsy(fraction: Float)`：`val hz = FreqInput.qsy(state.activeFrequency, _caps.value.spanHz(state.scopeSpan), fraction)`；`sendSet(if (state.activeVfo == "B") "vfo_b_freq" else "freq", hz)`。
- `fun sendFreqHz(hz: Long)`：同字段选择（频率输入对话框用）。
- `fun disconnect()`：`pttManager?.forceRelease(); buttonAllOff(); connectionManager.disconnect(); _userOff.value = true; _connected.value = false`。
- `fun reconnect()`：`_userOff.value = false; connectionManager.reconnectAll()`（无 token 时回到登录页：由 RootScreen 处理）。
- `fun startStats()` / `stopStats()`：1 秒循环，`_rxKbps = cm.drainRx() * 8 / 1000`、`_txKbps = ...`、`_rttMs = cm.lastRttMs()`。`connect()` 成功时启动、`disconnect()`/`logout()` 时停止。
- `refreshRecordings()` 同时写 `_recordingsCount`/`_recordingsBytes`。
- `fun clearNotice()`。

`ServiceLocator`：
- 接线 `onAtrEvent = { vm.onAtrEvent(it) }`；`rxPlayer` 类型保持接口，另加 `setVolume/setBoost/setTransmitting` 的调用需要具体类型 → 用现有的 `RxAudioPlayer` 实例（ServiceLocator 已持有 `rx` 局部变量），在 VM 更新时由 ServiceLocator 订阅？更简单：`MainViewModel` 通过 `rxAudioControl` 接口调用（新增接口方法 `setVolume/setBoost/setTransmitting`）；ServiceLocator 的 `rx` 实现该接口。VM 新增 `fun setAfVol(v: Int)`/`fun setMicVol(v: Int)`/`fun setTransmitting(t: Boolean)` 供 UI 调用。
- 保存的偏好需要注入 VM：在 `ServiceLocator.assemble()` 里用 app scope 收集 `SettingsStore(FT710App.instance)` 的 `micGain` flow → `vm.setSavedMicGain(it)`；`afVol` → `vm.setAfVol(it)`；`micVol` → `vm.setMicVol(it)`；`backgroundRx`/`keepScreenOn`/scope 偏好由 UI 直接读写（任务 10/11）。

- [ ] **步骤 4：运行验证通过（全量）**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest
```

预期：`BUILD SUCCESSFUL`（新增 4 项全过）。

- [ ] **步骤 5：Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/ViewModel/MainViewModel.kt \
        FT710Android/app/src/main/java/com/hamradio/ft710android/App/ServiceLocator.kt \
        FT710Android/app/src/test/java/com/hamradio/ft710android/ViewModel/MainViewModelTest.kt
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(android): wire capabilities, ATR, stats, QSY, mic-gain re-push and the user-off switch into the view model"
```

---

### 任务 9：调色板与瀑布画布（S1/M1 渲染）

**文件：**
- 创建：`FT710Android/app/src/main/java/com/hamradio/ft710android/Spectrum/Palettes.kt`
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/Spectrum/WaterfallCanvas.kt`
- 测试：`FT710Android/app/src/test/java/com/hamradio/ft710android/Spectrum/PalettesTest.kt`

- [ ] **步骤 1：写失败测试**

```kotlin
package com.hamradio.ft710android.Spectrum

import org.junit.Assert.assertEquals
import org.junit.Test

class PalettesTest {
    @Test fun `jet keeps the legacy anchors`() {
        assertEquals(0xFF000080.toInt(), Palettes.argb("jet", 0f))
        assertEquals(0xFF0000FF.toInt(), Palettes.argb("jet", 0.125f))
        assertEquals(0xFF00FFFF.toInt(), Palettes.argb("jet", 0.375f))
        assertEquals(0xFFFFFF00.toInt(), Palettes.argb("jet", 0.625f))
        assertEquals(0xFFFF0000.toInt(), Palettes.argb("jet", 0.875f))
    }

    @Test fun `hot cold thermal night gray anchors`() {
        assertEquals(0xFF000000.toInt(), Palettes.argb("hot", 0f))
        assertEquals(0xFFFF0000.toInt(), Palettes.argb("hot", 0.33f))
        assertEquals(0xFFFFFFFF.toInt(), Palettes.argb("hot", 1f))
        assertEquals(0xFF000000.toInt(), Palettes.argb("cold", 0f))
        assertEquals(0xFF00C8FF.toInt(), Palettes.argb("cold", 0.5f))
        assertEquals(0xFFC80000.toInt(), Palettes.argb("thermal", 0.25f))
        assertEquals(0xFFFFB400.toInt(), Palettes.argb("thermal", 0.5f))
        assertEquals(0xFF000080.toInt(), Palettes.argb("night", 0.33f))
        assertEquals(0xFFB400FF.toInt(), Palettes.argb("night", 0.66f))
        assertEquals(0xFF808080.toInt(), Palettes.argb("gray", 0.5f))
    }

    @Test fun `unknown theme falls back to jet`() {
        assertEquals(Palettes.argb("jet", 0.25f), Palettes.argb("nope", 0.25f))
    }

    @Test fun `lut applies floor and ceiling`() {
        val lut = Palettes.lut("gray", floor = 5, ceil = 220)
        assertEquals(0xFF000000.toInt(), lut[0])
        assertEquals(0xFF000000.toInt(), lut[5])
        assertEquals(0xFFFFFFFF.toInt(), lut[220])
        assertEquals(0xFFFFFFFF.toInt(), lut[255])
    }
}
```

- [ ] **步骤 2：运行验证失败**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*PalettesTest*'
```

预期：编译失败 `unresolved reference: Palettes`。

- [ ] **步骤 3：实现**

`Palettes.kt`：`argb(theme, v)` 逐段照 `static/ft710_ui.js:WF_PALETTES`（`toInt()` 截断与 JS `Math.floor` 在正值上一致；每段返回值 clamp 0..255）；`lut(theme, floor, ceil)` 生成 `IntArray(256)`：`v = ((raw - floor) / max(1, ceil - floor)).coerceIn(0f, 1f)` → `argb(theme, v)`。`jetArgb` 保留（`WaterfallLine` 测试仍用），`Palettes.argb("jet", v)` 复用它。

`WaterfallCanvas.kt` 签名改为：

```kotlin
@Composable
fun WaterfallCanvas(
    rows: List<IntArray>,
    fft: IntArray,
    theme: String,
    floor: Int,
    ceil: Int,
    fftFraction: Float,               // FFT 区占画布比例 = fftH/(fftH+wfH)
    modifier: Modifier = Modifier,
    onQsyFraction: ((Float) -> Unit)? = null,
)
```

- Bitmap 行只画在瀑布区（dst rect `y = fftPx..height`），LUT 由 `remember(theme, floor, ceil) { Palettes.lut(theme, floor, ceil) }`。
- FFT 线画在 `0..fftPx`（`py = fftPx - (v/255)*fftPx`）。
- 中心红线与三角跨全高（现状）。
- `Modifier.pointerInput(onQsyFraction) { detectTapGestures { offset -> onQsyFraction?.invoke(offset.x / size.width) } }`。

- [ ] **步骤 4：运行验证通过**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*PalettesTest*' && ./gradlew :app:compileDebugKotlin
```

预期：PASS + 编译成功（WaterfallCanvas 调用点此时会编译失败——同步改 `MainScreen.kt` 的调用为默认参数即可：`theme="jet", floor=5, ceil=220, fftFraction=0.3f`；任务 10 再接入真实偏好）。

- [ ] **步骤 5：Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Spectrum/ \
        FT710Android/app/src/main/java/com/hamradio/ft710android/UI/MainScreen.kt \
        FT710Android/app/src/test/java/com/hamradio/ft710android/Spectrum/PalettesTest.kt
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(android): six waterfall palettes with floor/ceiling mapping, split FFT band and tap-to-QSY"
```

---

### 任务 10：主屏（M1–M8）

**文件：**
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/UI/MainScreen.kt`
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/App/RootScreen.kt`、`AppSetup.kt`
- 创建：`FT710Android/app/src/main/java/com/hamradio/ft710android/UI/Dialogs.kt`（频率输入对话框先行，其余任务 11 用）
- 创建：`FT710Android/app/src/main/java/com/hamradio/ft710android/UI/UiPrefs.kt`

- [ ] **步骤 1：接线状态与回调**（先让主屏消费任务 8 的流）

新建 `UI/UiPrefs.kt`（`MainScreen` 与 `SettingsScreen` 共用的偏好快照）：

```kotlin
package com.hamradio.ft710android.UI

import com.hamradio.ft710android.Data.SettingsStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.combine

data class UiPrefs(
    val scopeTheme: String = "jet",
    val scopeFloor: Int = 5,
    val scopeCeil: Int = 220,
    val fftHeight: Int = 40,
    val wfHeight: Int = 110,
    val keepScreenOn: Boolean = true,
    val backgroundRx: Boolean = true,
) {
    companion object {
        /** 把 SettingsStore 的十条偏好流合成一个快照（未就绪时用默认值）。 */
        fun from(settings: SettingsStore): Flow<UiPrefs> = combine(
            settings.scopeTheme, settings.scopeFloor, settings.scopeCeil,
            settings.fftHeight, settings.wfHeight, settings.keepScreenOn, settings.backgroundRx,
        ) { theme, floor, ceil, fftH, wfH, keepOn, bgRx ->
            UiPrefs(theme, floor, ceil, fftH, wfH, keepOn, bgRx)
        }
    }
}
```

`RootScreen`：`val prefs by UiPrefs.from(settings).collectAsState(initial = UiPrefs())`；`MainScreen(vm, prefs, onOpenSettings)`、`SettingsScreen(vm, settings, prefs, …)`。

`MainScreen(vm, prefs, onOpenSettings)` 内追加 collect：`caps/displayName/atrState/atrTuning/notice/userOff/rttMs/rxKbps/txKbps/atr1000Enabled`（全部 `collectAsState()`），瀑布与音量/滑条从 `prefs` 读初值。

- [ ] **步骤 2：顶栏（M2/M6/M7）**

```kotlin
Row(verticalAlignment = Alignment.CenterVertically) {
    Text("☰", …clickable { onOpenSettings() })
    Text(fmtMhz(state.activeFrequency), color = MrrcColors.Accent, fontFamily = MonoFont,
        fontSize = 32.sp, maxLines = 1,
        modifier = Modifier.weight(1f).clickable { showFreqInput = true })
    PadBtn("⛶", active = immersive) { immersive = !immersive; onFullscreenChange(immersive) }
    PadBtn("⏻", active = !userOff, danger = userOff) { if (userOff) vm.reconnect() else vm.disconnect() }
    PadBtn("VFO-${state.activeVfo}", active = true) { vm.sendSet("vfo", if (state.activeVfo == "A") "B" else "A") }
}
```

- [ ] **步骤 3：状态行（D5/M4/M5/S8）**

在现有 波段/模式/TX/Serial 行追加：
- TX 徽标：`when (state.txStatus) { 2 -> "TUNE"; 1 -> "TX"; else -> "RX" }`（D5）
- `↓${rxKbps}K ↑${txKbps}K`、`RTT ${rttMs ?: "--"} J${rxPlayer.bufferMs}`（M4；`bufferMs` 通过 `vm.audioStats()` 暴露，见步骤 7）
- `if (state.rxAudioSilent) Text("无声", color = Danger, clickable { vm.showNotice("RX 音频持续全零——电台 USB 音频可能卡死，请重启电台或重插 USB") })`（M5）
- 机型名：`Text(caps.displayName, fontSize = 9.sp, color = TextMuted)`；`if (!caps.verified) Text("实验性", …)`（S8/D7）

- [ ] **步骤 4：ATR 行（M3）与仪表区**（插在 Vd 行之后、quick-controls 之前）

```kotlin
if (atrEnabled) {
    val a = atrState
    Row(Modifier.fillMaxWidth().padding(top = 4.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        MeterCell("ATR", a?.power ?: 0.0, 120f, MrrcColors.Warning, "%.0f W".format(Locale.US, a?.power ?: 0.0), Modifier.weight(1f))
        MeterCell("SWR", a?.swr ?: 0.0, 5f, MrrcColors.Success, if ((a?.swr ?: 0.0) > 0) "%.1f".format(Locale.US, a!!.swr) else "-", Modifier.weight(1f))
        Box(Modifier.weight(1f).height(26.dp).background(MrrcColors.BgSecondary, RoundedCornerShape(6.dp))
            .clickable(enabled = !atrTuning) { vm.atrTune() }, contentAlignment = Alignment.Center) {
            Text(if (atrTuning) "···" else "TUNE", color = MrrcColors.Accent, fontSize = 11.sp, fontWeight = FontWeight.Bold)
        }
    }
    Text(
        if (a == null || !a.connected) "ATR 离线" else
            "${if (a.sw == 1) "CL" else "LC"} L=${a.ind} C=${a.cap}${if (a.tuning) " ⋯" else ""}",
        color = MrrcColors.TextSecondary, fontSize = 10.sp, fontFamily = MonoFont,
    )
}
```

- [ ] **步骤 5：瀑布接线（M1）**

```kotlin
WaterfallCanvas(
    rows = waterfall, fft = fft,
    theme = prefs.scopeTheme, floor = prefs.scopeFloor, ceil = prefs.scopeCeil,
    fftFraction = prefs.fftHeight.toFloat() / (prefs.fftHeight + prefs.wfHeight),
    modifier = Modifier.fillMaxWidth().height((prefs.fftHeight + prefs.wfHeight).dp),
    onQsyFraction = { vm.qsy(it) },
)
```

- [ ] **步骤 6：频率输入 / 记忆 label / 断开遮罩 / notice**

- `showFreqInput` → `Dialogs.kt` 的 `FrequencyInputDialog(state.activeFrequency) { hz -> vm.sendFreqHz(hz) }`（M2）。
- 记忆格副标题用 `ch.label`（M8），仍显示 `M1` 角标。
- body 顶层 `if (userOff) Box(…半透明遮罩…){ PadBtn("重新连接"){ vm.reconnect() } }`（M7）。
- `notice`/`error` 统一显示在顶部（notice 用 Accent，error 用 Danger），4 秒后 `vm.clearNotice()/clearError()`（M3 的调谐结果走这里）。

- [ ] **步骤 7：`RootScreen`/`MainScreen` 签名与 `MainViewModel` 小补**

- `RootScreen`：把 `SettingsStore` 的偏好流收集成 `UiPrefs`（`collectAsState(initial = …)`），传给 `MainScreen` 与 `SettingsScreen`；`AppSetup(keepScreenOn = prefs.keepScreenOn, …)`。
- `MainViewModel` 追加 `fun atrTune()`（`connectionManager.sendAtrTune()`）、`fun audioStats(): Pair<Long?, Int>`（rtt + `rxPlayer?.bufferMs`）、`fun showNotice(msg)`。

> `atr1000Enabled` 在任务 8 已有（`vm.atr1000Enabled: StateFlow<Boolean>`）；主屏用它在 ATR 行前 `if (atrEnabled)` 判断，不要在 `if` 内调 `collectAsState()`。

- [ ] **步骤 8：编译门槛**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest assembleDebug lintDebug
```

预期：`BUILD SUCCESSFUL`，无 lint error（warning 不阻塞）。

- [ ] **步骤 9：Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/UI/MainScreen.kt \
        FT710Android/app/src/main/java/com/hamradio/ft710android/UI/Dialogs.kt \
        FT710Android/app/src/main/java/com/hamradio/ft710android/App/RootScreen.kt \
        FT710Android/app/src/main/java/com/hamradio/ft710android/App/AppSetup.kt \
        FT710Android/app/src/main/java/com/hamradio/ft710android/ViewModel/MainViewModel.kt
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(android): main screen parity — tap QSY, freq input, ATR row, stats, silent warning, TUNE, power switch"
```

---

### 任务 11：设置页、选择器与 Memory Manager（S1–S5/S8/S9/D3/D4）

**文件：**
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/UI/SettingsScreen.kt`
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/UI/Dialogs.kt`
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/UI/MainScreen.kt`（菜单入口）
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/Data/SettingsStore.kt`（如缺 `put*` 方法）
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/App/RootScreen.kt`

- [ ] **步骤 1：S1 Scope 面板**

设置页新增“频谱”分区：
- SPAN：从 `vm.caps.value.spans` 渲染 `FilterChip` 行；点击 `sendSet("scope_span", span.idx)`；选中态 = `state.scopeSpan`。
- SPD：`caps.speeds` 渲染 chips → `sendSet("scope_speed", idx)`。
- 颜色：`Palettes.NAMES` 下拉/行选 → `settings.putScopeTheme`。
- Floor `Slider(0..200)`、Ceil `Slider(50..255)` → 松手 `put*`。
- Spec H `Slider(20..120)`、WF H `Slider(30..200)` → `putFftHeight/putWfHeight`。

- [ ] **步骤 2：S2/S3 滑条（松手提交）**

统一模式（每个滑条 `var local by remember { mutableStateOf<Float?>(null) }`）：

```kotlin
Slider(value = local ?: state.rfPower.toFloat(),
    onValueChange = { local = it },
    onValueChangeFinished = { local?.let { v -> vm.setRfPower(v.toInt()); local = null } },
    valueRange = 5f..100f)
```

- RF PWR（D3：只在 finished 发）。
- RF Gain：显示 0..100%，提交 `sendSet("rf_gain", pct * 255 / 100)`。
- Mic Gain：0..100，提交 `sendSet("mic_gain", v)` + `settings.putMicGain(v)`。
- 🎙 Vol：0..200，`settings.putMicVol(v)` → ServiceLocator 收集后 `vm.setMicVol(v)`。
- 🔊 Vol（设置页 + 主屏共用）：0..255，`settings.putAfVol(v)` → `vm.setAfVol(v)`。
- NR Level 1..15 → `sendSet("nr_level", v)`；NB Level 0..10 → `sendSet("nb_level", v)`。

- [ ] **步骤 3：S4/S5/D4 选择器与 Memory Manager**

`Dialogs.kt` 新增：
- `BandPickerDialog(bands: List<BandDto>, current: String, onPick: (BandDto) -> Unit)`：网格按钮，`label = "${b.name}\n(${start/1e6}–${end/1e6})"`；选择 → `sendSet("freq", b.defaultFreq)`。
- `ModePickerDialog(modes: List<String>, current: String, onPick: (String) -> Unit)`。
- `MemoryManagerDialog(mem: List<MemoryChannel?>, onClear: (Int) -> Unit)`：6 行（`M1…M6`、频率、label、清除按钮）。

主屏 quick-controls 的「模式」「波段」长按打开选择器（点按保持循环）；设置页/菜单也提供入口。`vm.clearMemory(index)` 接到清除。

- [ ] **步骤 4：S8/S9 机型、连接设置、偏好开关**

- 设置页头部：`Text("${vm.displayName.value} · 客户端 v${BuildConfig.VERSION_NAME}")`；`if (!caps.verified) Text("实验性机型：发射${if (caps.txGated) "已禁用" else "已放行"}", color = Warning)`。
- 「保持屏幕常亮」`Switch` ← `settings.keepScreenOn`；「后台接收（退后台继续听）」`Switch` ← `settings.backgroundRx`。
- 「连接设置（浏览器）」：`context.startActivity(Intent(ACTION_VIEW, Uri.parse(vm.baseUrlForUi())))`（新增 VM 方法返回 `baseUrl ?: "https://${SettingsStore.DEFAULT_HOST}:${SettingsStore.DEFAULT_PORT}"`）。
- 「🐞 遇到问题」：`Uri.parse("${vm.baseUrlForUi()}/support.html")`。

- [ ] **步骤 5：编译门槛**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest assembleDebug lintDebug
```

预期：`BUILD SUCCESSFUL`。

- [ ] **步骤 6：Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/UI/ \
        FT710Android/app/src/main/java/com/hamradio/ft710android/App/RootScreen.kt \
        FT710Android/app/src/main/java/com/hamradio/ft710android/ViewModel/MainViewModel.kt
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(android): settings parity — scope panel, gain/level sliders, pickers, memory manager, support links"
```

---

### 任务 12：Cloud Hub 向导（S6）

**文件：**
- 创建：`FT710Android/app/src/main/java/com/hamradio/ft710android/Network/CloudApi.kt`
- 创建：`FT710Android/app/src/main/java/com/hamradio/ft710android/UI/CloudHubDialog.kt`
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/ViewModel/MainViewModel.kt`（cloud 动作）、`UI/MainScreen.kt`（菜单入口）
- 测试：`FT710Android/app/src/test/java/com/hamradio/ft710android/Network/CloudApiTest.kt`

- [ ] **步骤 1：写失败测试**

```kotlin
package com.hamradio.ft710android.Network

import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class CloudApiTest {
    private lateinit var server: MockWebServer
    private lateinit var api: CloudApi

    @Before fun setUp() { server = MockWebServer(); server.start(); api = CloudApi(OkHttpClient()) }
    @After fun tearDown() { server.shutdown() }

    @Test fun `state parses the full payload`() = runTest {
        server.enqueue(MockResponse().setBody(
            """{"connected":true,"callsign":"BG1SB","has_token":true,"label":"bg1sb","entry":"https://bg1sb.mrrc.vlsc.net",
                "cert":"/data/certs/server.crt","portal":"https://cloud.vlsc.net","tunnel_running":true,"tunnel_error":"",
                "cert_reload_required":false,"autoconnect":{"status":"connected","at":1759600000}}"""
        ))
        val st = (api.state(server.url("/").toString().trimEnd('/'), "t") as CloudResult.Ok).value
        assertTrue(st.connected)
        assertEquals("BG1SB", st.callsign)
        assertEquals("connected", st.autoconnect?.status)
        assertEquals("https://bg1sb.mrrc.vlsc.net", st.entry)
        val req = server.takeRequest()
        assertEquals("/api/cloud/state", req.path)
        assertEquals("ft710_auth=t", req.getHeader("Cookie"))
    }

    @Test fun `apply posts callsign contact secret`() = runTest {
        server.enqueue(MockResponse().setBody("""{"submitted":true,"status":"applied","callsign":"BG1SB"}"""))
        val r = api.apply(server.url("/").toString().trimEnd('/'), "t", "bg1sb", "a@b.c", "SECRET")
        assertTrue(r is CloudResult.Ok)
        val body = server.takeRequest().body.readUtf8()
        assertTrue(body.contains("\"callsign\":\"bg1sb"))
        assertTrue(body.contains("\"secret\":\"SECRET\""))
    }

    @Test fun `error payload becomes Err with the server message`() = runTest {
        server.enqueue(MockResponse().setResponseCode(502).setBody("""{"error":"hub 拒绝: 呼号已被占用"}"""))
        val r = api.apply(server.url("/").toString().trimEnd('/'), "t", "x", "", "")
        assertEquals("hub 拒绝: 呼号已被占用", (r as CloudResult.Err).message)
    }

    @Test fun `non-json response becomes Err naming the status`() = runTest {
        server.enqueue(MockResponse().setResponseCode(404).setBody("<html>nope</html>"))
        val r = api.state(server.url("/").toString().trimEnd('/'), "t")
        assertTrue((r as CloudResult.Err).message.contains("404"))
    }
}
```

- [ ] **步骤 2：运行验证失败**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*CloudApiTest*'
```

预期：编译失败 `unresolved reference: CloudApi`。

- [ ] **步骤 3：实现 CloudApi**

```kotlin
@Serializable
data class CloudAutoDto(val status: String = "idle", val at: Long = 0, val error: String? = null)

@Serializable
data class CloudStateDto(
    val connected: Boolean = false,
    val callsign: String = "",
    @SerialName("has_token") val hasToken: Boolean = false,
    val label: String = "",
    val entry: String = "",
    val cert: String = "",
    val portal: String = "",
    @SerialName("tunnel_running") val tunnelRunning: Boolean = false,
    @SerialName("tunnel_error") val tunnelError: String = "",
    @SerialName("cert_reload_required") val certReloadRequired: Boolean = false,
    val autoconnect: CloudAutoDto? = null,
    val status: String = "",
    val submitted: Boolean = false,
    val error: String? = null,
)

sealed class CloudResult<out T> {
    data class Ok<T>(val value: T) : CloudResult<T>()
    data class Err(val message: String) : CloudResult<Nothing>()
}

class CloudApi(private val client: OkHttpClient) {
    private fun req(url: String, token: String, post: Boolean, body: String? = null): Request =
        Request.Builder().url(url).header("Cookie", "ft710_auth=$token")
            .apply { if (post) post(body?.toRequestBody("application/json".toMediaType()) ?: "{}".toRequestBody("application/json".toMediaType())) }
            .build()

    private suspend fun call(url: String, token: String, post: Boolean): CloudResult<CloudStateDto> =
        withContext(Dispatchers.IO) {
            runCatching {
                client.newCall(req(url, token, post)).execute().use { resp ->
                    val text = resp.body?.string().orEmpty()
                    val dto = runCatching { cloudJson.decodeFromString<CloudStateDto>(text) }.getOrNull()
                    when {
                        dto?.error != null -> CloudResult.Err(dto.error!!)
                        dto == null -> CloudResult.Err("接口返回了非 JSON（HTTP ${resp.code}）")
                        !resp.isSuccessful -> CloudResult.Err("HTTP ${resp.code}")
                        else -> CloudResult.Ok(dto)
                    }
                }
            }.getOrElse { CloudResult.Err(it.message ?: "网络错误") }
        }

    suspend fun state(base: String, token: String) = call("$base/api/cloud/state", token, post = false)
    suspend fun refresh(base: String, token: String) = call("$base/api/cloud/refresh", token, post = true)
    suspend fun restart(base: String, token: String) = call("$base/api/cloud/restart", token, post = true)
    suspend fun apply(base: String, token: String, callsign: String, contact: String, secret: String): CloudResult<CloudStateDto> {
        val body = cloudJson.encodeToString(CloudApplyBody(callsign, contact, secret))
        return postJson("$base/api/cloud/apply", token, body)
    }

    private suspend fun postJson(url: String, token: String, body: String): CloudResult<CloudStateDto> =
        withContext(Dispatchers.IO) { /* 与 call 相同，但 body 原样发送 */ }
}

@Serializable private data class CloudApplyBody(val callsign: String, val contact: String, val secret: String)
```

（`postJson` 与 `call` 共用一个 `private suspend fun execute(request: Request): CloudResult<CloudStateDto>` 实现，DRY。）

- [ ] **步骤 4：实现向导 UI**

`CloudHubDialog(vm, onClose)`：
- 打开时 `state()`；显示：申请表单（呼号/联系方式/登记口令/申请按钮）或 已申请（呼号 + 状态 + 登记口令 claim + 刷新 + 每 20 秒轮询）或 已接入（入口链接/证书路径/隧道状态 + `cert_reload_required` 时「重启以启用新证书」按钮）。
- 状态文案用 `cloud_hub.js` 的 `AUTO_LABELS` 中文（还没申请/已提交，等待运维批准/已核验，等待分配入口/已批准，正在接入…/正在接入…/已接入/已接入，正在重启以启用新证书/联系不上 hub（会一直重试）/接入失败（会一直重试））。
- `restart` 后提示「实例重启中，约 6 秒后重连」，10 秒后 `vm.reconnect()`。

- [ ] **步骤 5：运行验证通过**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*CloudApiTest*' assembleDebug
```

预期：PASS + 编译成功。

- [ ] **步骤 6：Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Network/CloudApi.kt \
        FT710Android/app/src/main/java/com/hamradio/ft710android/UI/CloudHubDialog.kt \
        FT710Android/app/src/main/java/com/hamradio/ft710android/ViewModel/MainViewModel.kt \
        FT710Android/app/src/main/java/com/hamradio/ft710android/UI/MainScreen.kt \
        FT710Android/app/src/test/java/com/hamradio/ft710android/Network/CloudApiTest.kt
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(android): Cloud Hub onboarding wizard with the web's apply/wait/restart flow"
```

---

### 任务 13：后台 RX 前台服务（L1）

**文件：**
- 创建：`FT710Android/app/src/main/java/com/hamradio/ft710android/App/RxForegroundService.kt`
- 修改：`FT710Android/app/src/main/AndroidManifest.xml`、`res/values/strings.xml`
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/App/MainActivity.kt`、`App/ServiceLocator.kt`、`App/RootScreen.kt`

- [ ] **步骤 1：服务实现**

```kotlin
package com.hamradio.ft710android.App

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.hamradio.ft710android.R

class RxForegroundService : Service() {
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_DISCONNECT) {
            ServiceLocator.vmFactory().disconnect()
            stopSelf()
            return START_NOT_STICKY
        }
        startForeground(NOTIF_ID, buildNotification())
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(): Notification {
        val mgr = getSystemService(NotificationManager::class.java)
        if (mgr.getNotificationChannel(CHANNEL_ID) == null) {
            mgr.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "后台接收", NotificationManager.IMPORTANCE_LOW))
        }
        val open = PendingIntent.getActivity(this, 0,
            Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val off = PendingIntent.getService(this, 1,
            Intent(this, RxForegroundService::class.java).setAction(ACTION_DISCONNECT), PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("MRRC Modern")
            .setContentText("正在接收（后台 RX）")
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(0, "断开", off)
            .build()
    }

    companion object {
        const val CHANNEL_ID = "rx"
        const val NOTIF_ID = 1001
        const val ACTION_DISCONNECT = "com.hamradio.ft710android.DISCONNECT"

        fun start(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, RxForegroundService::class.java))
        }

        fun stop(ctx: Context) {
            ctx.stopService(Intent(ctx, RxForegroundService::class.java))
        }
    }
}
```

- [ ] **步骤 2：Manifest 与权限**

```xml
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE_MEDIA_PLAYBACK" />
    <uses-permission android:name="android.permission.POST_NOTIFICATIONS" />
    …
        <service
            android:name=".App.RxForegroundService"
            android:exported="false"
            android:foregroundServiceType="mediaPlayback" />
```

- [ ] **步骤 3：接线（连接状态 + 偏好驱动）**

- `ServiceLocator.assemble()`：新增 `BackgroundRxController` 实现（持 `FT710App.instance`）：`fun setEnabled(on: Boolean) { if (on) RxForegroundService.start(ctx) else RxForegroundService.stop(ctx) }`，注入 VM。
- `MainViewModel`：`var backgroundRx = true`（`fun setBackgroundRxPref(v: Boolean)`）；`onConnectionChange(connected)` 与 `disconnect()`/`logout()` 里调 `background.setEnabled(connected && backgroundRx && !listenOnly)`。
- `MainActivity`：登录成功后（`RootScreen` 的 `LaunchedEffect(loggedIn)`）用 `rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission())` 申请 `POST_NOTIFICATIONS`（API 33+，仅一次）；`ServiceLocator` 收集 `SettingsStore.backgroundRx` → `vm.setBackgroundRxPref(it)`。

- [ ] **步骤 4：编译门槛**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest assembleDebug lintDebug
```

预期：`BUILD SUCCESSFUL`（前台服务无法 JVM 测；真机验收第 7 项）。

- [ ] **步骤 5：Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/App/ \
        FT710Android/app/src/main/AndroidManifest.xml \
        FT710Android/app/src/main/res/values/strings.xml \
        FT710Android/app/src/main/java/com/hamradio/ft710android/ViewModel/MainViewModel.kt
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(android): background RX via a mediaPlayback foreground service (TX still releases on stop)"
```

---

### 任务 14：录音面板 seek 与合计（L3）

**文件：**
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/Network/RecordingsApi.kt`
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/UI/RecordingPanel.kt`
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/ViewModel/MainViewModel.kt`
- 测试：`FT710Android/app/src/test/java/com/hamradio/ft710android/Network/RecordingsApiTest.kt`

- [ ] **步骤 1：写失败测试**

```kotlin
    @Test fun `parses summary counts`() {
        val dto = parseRecordingsSummary(
            """{"recordings":[],"count":7,"total_bytes":123456}"""
        )
        assertEquals(7, dto.count)
        assertEquals(123456L, dto.totalBytes)
    }
```

- [ ] **步骤 2：运行验证失败**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*RecordingsApiTest*'
```

预期：编译失败 `unresolved reference: parseRecordingsSummary`。

- [ ] **步骤 3：实现**

- `RecordingsApi.list()` 返回 `RecordingsListDto`；`VM.refreshRecordings()` 用其 `count/totalBytes` 更新两个 StateFlow。
- `RecordingPanel`：播放行加 `Slider`（`value = playbackPos.toFloat()`, `valueRange = 0f..max(1f, playbackDur)`），`onValueChangeFinished` → `player.seekTo(v.toInt())`；顶部加合计 `Text("${vm.recordingsCount.value} 条 · 共 ${fmtBytes(vm.recordingsBytes.value)}")`。

- [ ] **步骤 4：运行验证通过**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest assembleDebug
```

预期：PASS + 编译成功。

- [ ] **步骤 5：Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Network/RecordingsApi.kt \
        FT710Android/app/src/main/java/com/hamradio/ft710android/UI/RecordingPanel.kt \
        FT710Android/app/src/main/java/com/hamradio/ft710android/ViewModel/MainViewModel.kt \
        FT710Android/app/src/test/java/com/hamradio/ft710android/Network/RecordingsApiTest.kt
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(android): seek inside recordings and show the list summary"
```

---

### 任务 15：文档、CHANGELOG 与 mockup

**文件：**
- 修改：`FT710Android/CHANGELOG.md`（1.1.0 条目）、`FT710Android/CLAUDE.md`（协议事实/偏好表/模块）、`FT710Android/BUILD_GUIDE.md`（如版本引用）、`README.md`（仓库根 Android 下载区）
- 新建：`FT710Android/docs/ui-mockup-v1.1.0.svg`（+ `png`）

- [ ] **步骤 1：CHANGELOG 1.1.0**

```markdown
## [1.1.0] — 2026-10-05

- **修复阻断缺陷**：v1.0.1 无法解析真实 `fullState`（服务端 `bands` 是对象数组、`filterTables` 是 `[idx,hz]` 数对），登录后一直"连上但无状态"。现按真实形状解析，fixture 与服务端逐字对齐
- 量程修复：Scope SPAN 改用服务端 `capabilities.scope_spans`（含 CI-V 半幅 ×2）；旧版「50k/100k/1M」实际发的是 1k/2k/5k
- 主屏对齐手机端 Web：点击瀑布 QSY、点击频率输入 MHz、ATR1000 行（PWR/SWR/L-C + TUNE）、状态行码率/RTT/抖动、无声告警、TUNE 状态、连接开关、全屏、记忆 label
- 设置对齐：Scope 面板（SPAN/SPD/6 配色/Floor/Ceil/高度）、RF Gain/Mic Gain/Mic Vol、NR/NB 电平、波段/模式选择器、Memory Manager、机型与版本
- 新增 Cloud Hub 接入向导与「🐞 遇到问题」入口
- 音频：本机播放音量（含 FT-710 10× 增益）与 TX 静音、44.1k 采集重采样（882↔960）、后台 RX 前台服务
- 录音面板：进度拖动 seek + 列表合计
- JVM 单测 52 → 76（新增协议/O 能力表/频率/重采样/增益/调色板/Cloud REST）
```

- [ ] **步骤 2：CLAUDE.md / BUILD_GUIDE / README**

- `CLAUDE.md`：协议事实补 `capabilities`/真实 `fullState` 形状/ATR/Cloud REST/支持页；偏好表（§5.5）；坑位更新（删除"后台 RX 未实现"）。
- `README.md`（仓库根）：Android 行改为 v1.1.0（`release.sh` 之后由脚本刷新站点；README 手动改一次）。
- `BUILD_GUIDE.md`：版本示例 1.1.0（如引用）。

- [ ] **步骤 3：mockup**

更新 `docs/ui-mockup-v1.1.0.svg`：在 v1.0.0 版基础上加 ATR 行、状态行统计、顶栏 ⛶/⏻，并导出 png（`rsvg-convert` 或 `qlmanage`；若无工具则只留 SVG 并在 CHANGELOG 注明）。

- [ ] **步骤 4：Commit**

```bash
git add FT710Android/CHANGELOG.md FT710Android/CLAUDE.md FT710Android/BUILD_GUIDE.md \
        FT710Android/docs/ README.md
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "docs(android): v1.1.0 changelog, protocol facts, preference table and the updated mockup"
```

---

### 任务 16：发布 v1.1.0 + 线上复核

**文件：**
- 修改：`FT710Android/app/build.gradle.kts`（versionCode 3 / versionName "1.1.0"）
- 执行：`FT710Android/release.sh`

- [ ] **步骤 1：版本号**

```kotlin
        versionCode = 3
        versionName = "1.1.0"
```

- [ ] **步骤 2：全量门槛**

```bash
cd FT710Android
export JAVA_HOME=$(/usr/libexec/java_home -v 17); export ANDROID_HOME="$HOME/Library/Android/sdk"
./gradlew test assembleDebug lintDebug assembleRelease
```

预期：`BUILD SUCCESSFUL`；测试计数 = 76（以实际为准，CHANGELOG 同步）。

- [ ] **步骤 3：一键发布（构建→签名核对→上传→页面更新→线上 SHA-256 复核）**

```bash
cd FT710Android && ./release.sh
```

预期：`dist/MRRC-Modern-v1.1.0-Android.apk` + 稳定别名；站点 en/zh 下载块与 hero 按钮指向 v1.1.0；线上 200 + 大小 + SHA-256 逐字节复核通过。

- [ ] **步骤 4：Commit + push**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern/.worktrees/android-v1.0.0
git add FT710Android/app/build.gradle.kts FT710Android/CHANGELOG.md FT710Android/dist/ （dist 已被 .gitignore? 按 v1.0.1 的约定：镜像 APK 忽略、版本 APK 视仓库现状）
git commit -m "release(android): v1.1.0 — versionCode 3 and the website cards"
```

（站点仓库 `~/HAM/website` 的提交由 `release.sh` 负责或按其输出手动提交。）

- [ ] **步骤 5：交付**

把设计文档 §6.3 的真机验收清单发给用户（14 项），等待回报。

---

## 自检记录

- **规格覆盖度**：规格 §3 的 28 项逐一映射到任务 1–14；§5.5 偏好表在任务 5；§6 测试在任务 1–9/12/14；§7 发布在任务 15/16。无遗漏。
- **占位符扫描**：无 TODO/待定；所有测试与关键实现均给出代码；UI 大块以组件签名 + 关键代码 + 编译门槛描述（UI 不可 JVM 单测，验收 = build + 真机）。
- **类型一致性**：`RadioCaps`/`SpanChoice` + `object Capabilities`（任务 2）、`FreqInput`（任务 3）、`NetworkStats`/`AtrEvent`/`AtrText`（任务 4）、`RxGain`/`Resampler`/`TxFraming`（任务 5–7）、`atrState`（不得与旧 `atr1000Enabled` 撞名）、`UiPrefs`（任务 10）、`Palettes`（任务 9）、`CloudResult`/`CloudStateDto`（任务 12）在后续任务中全部按同名引用。
- **构造参数计数**：`ConnectionManager` 位置参数 8 个（client/scope + 6 个回调），`onListenOnly`/`sendOverride` 具名传；新增测试已按此书写。
