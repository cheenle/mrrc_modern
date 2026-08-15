# FT710 Android App 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `FT710Android/` 新建一个原生 Android（Kotlin + Jetpack Compose）遥控客户端，通过 4 路 WSS 连到仓库根的 Python FastAPI 服务端，功能全量对齐 Web 前端（控制 / RX+TX 音频 / 频谱瀑布 / 记忆频道 / ATR1000 联动），PTT 安全从首版做对。

**Architecture:** 单 Gradle 模块 `app/` + 分层包。`MainViewModel`（普通类，非 AndroidX ViewModel，Compose 内 `remember` 创建，避免 DI 样板且可 JVM 单测）为总协调器，持有 `ConnectionManager`（4+1 路 WS）、`RadioState`、`RxAudioPlayer`、`TxAudioCapture`、`SpectrumProcessor`、`PTTManager`。纯逻辑类不依赖 Android SDK，全部 JVM 单测。协议字段**逐字对齐 `server.py`**。

**Tech Stack:** Kotlin 2.0.21 · Compose BOM 2024.12.01 (Material3) · AGP 8.7.3 · Gradle 8.9 · JDK 17 · OkHttp 4.12.0 · kotlinx-serialization-json 1.7.3 · kotlinx-coroutines · DataStore · libopus via NDK/CMake · minSdk 26 · target/compile SDK 35。

## Global Constraints

- 包名 `com.hamradio.ft710android`；App 显示名 `FT-710 Control`；minSdk 26；target/compile SDK 35。
- 版本钉死：AGP 8.7.3 + Gradle 8.9 + Kotlin 2.0.21 + Compose BOM 2024.12.01 + JDK 17。**本机 JDK 11 不够，AGP 8.x 必须 JDK 17**。
- 协议字段名必须**逐字**等于 `server.py` 的字段（见 Task 3 字段表）；**禁止自创字段名**（iOS 发 `"ipo"` 被静默吞掉是前车之鉴）。
- 可设字段（`server.py:_execute_set_command` 已核对）：`freq` `vfo_a_freq` `vfo_b_freq` `mode` `ptt` `tune` `filter`/`filter_width` `af_gain` `rf_power` `preamp` `att`/`attenuator` `nb`/`noise_blanker` `nr`/`noise_reduction` `an`/`auto_notch` `comp`/`compressor` `tuner` `vfo` `split` `power` `squelch` `mic_gain` `scope_span` `scope_speed` `scope_mode` `nb_level` `nr_level` `comp_level`/`compressor_level` `monitor` `vox` `break_in` `key_speed` `cw_pitch` `rit` `rit_freq` `xit`。区分 `tuner`（天调开关 0/1/2）与 `tune`（调谐载波 bool）。
- PTT 安全铁律（spec §7）：`release()` 无条件发 `ptt:false` + `s:`；手势用 `finally` 兜底；`onStop` → `forceRelease()`；看门狗 500ms×3。
- 音频：48kHz 单声道，20ms 帧（960 样本）；帧首 1B tag `0x00`=PCM Int16 LE、`0x01`=Opus；TX 恒 Opus CBR 64kbps。
- 记忆频道：6 槽、`null` 补空、键名 `label`（服务端格式）。
- 频谱帧：1701B = `0x01` + 850B wf1 + 850B wf2；实际 ~5fps。
- 认证：`POST /api/auth/login` → `{"ok":true,"token"}` + `Set-Cookie: ft710_auth`；WS 拼 `?token=<token>`；401/429 → 回登录页并**停止重连**。
- TLS：默认接受自签证书（OkHttp 自定义 TrustManager）；明文 HTTP 仅调试（`network_security_config.xml`）。
- 测试门槛：`./gradlew test assembleDebug lintDebug` 必须通过（无需真机）。所有纯逻辑必有 JVM 单测。
- 仓库约定：编辑仓库根文件前 `python3 .agents/skills/sdd-guardian/harness/sdd_context.py brief <files>`；每个任务结束单独 commit，消息带 `Co-Authored-By: Claude <noreply@anthropic.com>`。
- 不引入 DI 框架（Hilt/Koin）；不引入额外多平台框架。

---

### Task 1: 工具链 + 工程骨架（可编译的 hello-world）

**Files:**
- Create: `FT710Android/BUILD_GUIDE.md`
- Create: `FT710Android/settings.gradle.kts`
- Create: `FT710Android/build.gradle.kts`
- Create: `FT710Android/gradle.properties`
- Create: `FT710Android/gradle/libs.versions.toml`
- Create: `FT710Android/app/build.gradle.kts`
- Create: `FT710Android/app/src/main/AndroidManifest.xml`
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/App/FT710App.kt`
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/App/MainActivity.kt`
- Create: `FT710Android/app/src/main/res/values/themes.xml`
- Create: `FT710Android/app/src/main/res/values/strings.xml`
- Create: `FT710Android/app/src/main/res/mipmap-*/ic_launcher.xml`（用任意 1 个占位 mipmap）

**Interfaces:**
- Consumes: 无（首个任务）。
- Produces: 一个可用 `./gradlew assembleDebug` 构建、安装到真机后显示 "FT-710 Control" 的 Compose 空壳。后续任务都在这套 Gradle 配置上叠加。

- [ ] **Step 1: 安装 JDK 17 与 Android SDK，写入 BUILD_GUIDE.md**

在 `FT710Android/BUILD_GUIDE.md` 记录以下命令（本机是 macOS，`~/Library/Android/sdk` 为空、JDK 是 11）：

```bash
# JDK 17（Corretto，与现有 java 同族）
brew install --cask corretto@17
# 验证
/usr/libexec/java_home -V | grep 17
export JAVA_HOME=$(/usr/libexec/java_home -v 17)

# Android cmdline-tools
mkdir -p "$HOME/Library/Android/sdk/cmdline-tools"
cd "$HOME/Library/Android/sdk/cmdline-tools"
curl -o tools.zip https://dl.google.com/android/repository/commandlinetools-mac-11076708_latest.zip
unzip tools.zip && mv cmdline-tools latest && rm tools.zip
export ANDROID_HOME="$HOME/Library/Android/sdk"

# 安装平台/构建工具，接受许可
yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --licenses
"$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" \
  "platforms;android-35" "build-tools;35.0.0" "platform-tools"

# Gradle（生成 wrapper 用）
brew install gradle
```

确认后 `sdkmanager --list_installed` 能看到 platform-tools 与 platforms;android-35。

- [ ] **Step 2: 创建 Gradle 配置**

`FT710Android/settings.gradle.kts`:
```kotlin
pluginManagement {
    repositories { google(); mavenCentral(); gradlePluginPortal() }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories { google(); mavenCentral() }
}
rootProject.name = "FT710Android"
include(":app")
```

`FT710Android/build.gradle.kts`:
```kotlin
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.kotlin.compose) apply false
    alias(libs.plugins.kotlin.serialization) apply false
}
```

`FT710Android/gradle.properties`:
```properties
org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8
android.useAndroidX=true
kotlin.code.style=official
android.nonTransitiveRClass=true
```

`FT710Android/gradle/libs.versions.toml`:
```toml
[versions]
agp = "8.7.3"
kotlin = "2.0.21"
composeBom = "2024.12.01"
okhttp = "4.12.0"
serialization = "1.7.3"
coroutines = "1.9.0"
datastore = "1.1.1"
junit = "4.13.2"

[libraries]
compose-bom = { group = "androidx.compose", name = "compose-bom", version.ref = "composeBom" }
compose-ui = { group = "androidx.compose.ui", name = "ui" }
compose-material3 = { group = "androidx.compose.material3", name = "material3" }
compose-ui-tooling-preview = { group = "androidx.compose.ui", name = "ui-tooling-preview" }
compose-ui-tooling = { group = "androidx.compose.ui", name = "ui-tooling" }
activity-compose = { group = "androidx.activity", name = "activity-compose", version = "1.9.3" }
lifecycle-runtime = { group = "androidx.lifecycle", name = "lifecycle-runtime-ktx", version = "2.8.7" }
okhttp = { group = "com.squareup.okhttp3", name = "okhttp", version.ref = "okhttp" }
okhttp-mockwebserver = { group = "com.squareup.okhttp3", name = "mockwebserver", version.ref = "okhttp" }
serialization-json = { group = "org.jetbrains.kotlinx", name = "kotlinx-serialization-json", version.ref = "serialization" }
coroutines-core = { group = "org.jetbrains.kotlinx", name = "kotlinx-coroutines-core", version.ref = "coroutines" }
coroutines-android = { group = "org.jetbrains.kotlinx", name = "kotlinx-coroutines-android", version.ref = "coroutines" }
coroutines-test = { group = "org.jetbrains.kotlinx", name = "kotlinx-coroutines-test", version.ref = "coroutines" }
datastore = { group = "androidx.datastore", name = "datastore-preferences", version.ref = "datastore" }
junit = { group = "junit", name = "junit", version.ref = "junit" }

[plugins]
android-application = { id = "com.android.application", version.ref = "agp" }
kotlin-android = { id = "org.jetbrains.kotlin.android", version.ref = "kotlin" }
kotlin-compose = { id = "org.jetbrains.kotlin.plugin.compose", version.ref = "kotlin" }
kotlin-serialization = { id = "org.jetbrains.kotlin.plugin.serialization", version.ref = "kotlin" }
```

`FT710Android/app/build.gradle.kts`:
```kotlin
plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

android {
    namespace = "com.hamradio.ft710android"
    compileSdk = 35
    defaultConfig {
        applicationId = "com.hamradio.ft710android"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { compose = true }
    testOptions { unitTests.isReturnDefaultValues = true }
}

dependencies {
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.material3)
    implementation(libs.compose.ui.tooling.preview)
    debugImplementation(libs.compose.ui.tooling)
    implementation(libs.activity.compose)
    implementation(libs.lifecycle.runtime)
    implementation(libs.okhttp)
    implementation(libs.serialization.json)
    implementation(libs.coroutines.core)
    implementation(libs.coroutines.android)
    implementation(libs.datastore)
    testImplementation(libs.junit)
    testImplementation(libs.okhttp.mockwebserver)
    testImplementation(libs.coroutines.test)
}
```

- [ ] **Step 3: 生成 Gradle wrapper**

```bash
cd FT710Android
gradle wrapper --gradle-version 8.9
```
预期：生成 `gradlew`、`gradle/wrapper/*`。

- [ ] **Step 4: 创建最小 Compose 壳**

`FT710Android/app/src/main/AndroidManifest.xml`:
```xml
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.RECORD_AUDIO" />
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
    <application
        android:label="@string/app_name"
        android:theme="@style/Theme.FT710"
        android:supportsRtl="true">
        <activity
            android:name=".App.MainActivity"
            android:exported="true"
            android:screenOrientation="portrait">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>
</manifest>
```

`FT710Android/app/src/main/res/values/strings.xml`:
```xml
<resources>
    <string name="app_name">FT-710 Control</string>
</resources>
```

`FT710Android/app/src/main/res/values/themes.xml`:
```xml
<resources>
    <style name="Theme.FT710" parent="android:Theme.Material.NoActionBar">
        <item name="android:windowBackground">#0B0B0C</item>
    </style>
</resources>
```

`FT710Android/app/src/main/java/com/hamradio/ft710android/App/FT710App.kt`:
```kotlin
package com.hamradio.ft710android.App

import android.app.Application

class FT710App : Application()
```

`FT710Android/app/src/main/java/com/hamradio/ft710android/App/MainActivity.kt`:
```kotlin
package com.hamradio.ft710android.App

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { AppTheme { Text("FT-710 Control") } }
    }
}

@Composable
fun AppTheme(content: @Composable () -> Unit) {
    MaterialTheme(content = content)
}
```

（占位 mipmap：在 `app/src/main/res/mipmap-anydpi-v26/ic_launcher.xml` 写一个 adaptive icon 引用纯色 drawable；或直接删掉 manifest 的 `android:icon` 属性以避免缺资源。**选删除 `android:icon` 属性，最简单**，后续再补图标。）

- [ ] **Step 5: 构建验证**

```bash
cd FT710Android && ./gradlew assembleDebug
```
预期：BUILD SUCCESSFUL，产出 `app/build/outputs/apk/debug/app-debug.apk`。

- [ ] **Step 6: Commit**

```bash
git add FT710Android/
git commit -m "feat(android): scaffold FT710Android Gradle/Compose project

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: RadioState（服务端字段镜像 + 增量应用）

**Files:**
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/Data/RadioState.kt`
- Test: `FT710Android/app/src/test/java/com/hamradio/ft710android/Data/RadioStateTest.kt`

**Interfaces:**
- Consumes: kotlinx.serialization `JsonElement`/`JsonPrimitive`（Task 3 的 DTO 会把 `fields`/`data` 作为 `JsonObject` 传给这里）。
- Produces: `class RadioState`，方法 `apply(data: Map<String, JsonElement>): Set<String>`（返回实际应用的 key 集合，供 dirty 追踪/日志）。`fullState.data` 与 `stateUpdate.fields` 是同一套 key（`radio_state.py:to_dict/to_dirty_dict`），故同一方法。

- [ ] **Step 1: 写失败测试**

`FT710Android/app/src/test/java/com/hamradio/ft710android/Data/RadioStateTest.kt`:
```kotlin
package com.hamradio.ft710android.Data

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RadioStateTest {
    private val json = Json { ignoreUnknownKeys = true }

    private fun obj(vararg pairs: Pair<String, String>): Map<String, kotlinx.serialization.json.JsonElement> =
        json.parseToJsonElement(
            pairs.joinToString(prefix = "{", postfix = "}") { (k, v) -> "\"$k\":$v" }
        ).jsonObject

    @Test fun `applyFullState populates all field types`() {
        val state = RadioState()
        state.apply(obj(
            "vfo_a_freq" to "7050000",
            "vfo_b_freq" to "14270000",
            "active_vfo" to "\"A\"",
            "mode" to "1",
            "tx_status" to "0",
            "s_meter" to "4",
            "power_watts" to "12.5",
            "swr_ratio" to "1.4",
            "noise_reduction" to "true",
            "filter_width" to "5",
            "serial_connected" to "true",
            "mode_name" to "\"USB\"",
            "band_name" to "\"20m\""
        ))
        assertEquals(7050000L, state.vfoAFreq)
        assertEquals(14270000L, state.vfoBFreq)
        assertEquals("A", state.activeVfo)
        assertEquals(1, state.mode)
        assertEquals(0, state.txStatus)
        assertEquals(4, state.sMeter)
        assertEquals(12.5, state.powerWatts, 1e-9)
        assertEquals(1.4, state.swrRatio, 1e-9)
        assertTrue(state.noiseReduction)
        assertEquals(5, state.filterWidth)
        assertTrue(state.serialConnected)
        assertEquals("USB", state.modeName)
        assertEquals("20m", state.bandName)
    }

    @Test fun `applyUpdate partial overwrite only touches given keys and returns them`() {
        val state = RadioState().apply(obj("freq" to "7000000", "mode" to "2"))
        val dirty = state.apply(obj("vfo_a_freq" to "7100000"))
        assertEquals(setOf("vfo_a_freq"), dirty)
        assertEquals(7100000L, state.vfoAFreq)
        assertEquals(2, state.mode) // 未触碰
    }

    @Test fun `unknown keys are ignored and reported as not dirty`() {
        val state = RadioState()
        val dirty = state.apply(obj("bogus_field" to "1"))
        assertFalse(dirty.contains("bogus_field"))
    }

    @Test fun `booleans accept true as json bool`() {
        val state = RadioState().apply(obj("vox" to "true", "break_in" to "false"))
        assertTrue(state.vox)
        assertFalse(state.breakIn)
    }
}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*RadioStateTest*'
```
预期：编译失败 / `RadioState` 未定义。

- [ ] **Step 3: 实现 RadioState**

`FT710Android/app/src/main/java/com/hamradio/ft710android/Data/RadioState.kt`:
```kotlin
package com.hamradio.ft710android.Data

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.longOrNull

/**
 * 服务端字段镜像（radio_state.py:to_dict 的 key 逐字对应）。
 * fullState.data 与 stateUpdate.fields 共用同一套 key，apply() 统一处理。
 */
class RadioState {
    // Core
    var vfoAFreq: Long = 0
    var vfoBFreq: Long = 0
    var activeVfo: String = "A"
    var activeFreq: Long = 0
    var mode: Int = 0
    var txStatus: Int = 0
    // Meters（原始 0-255 int + 派生 float）
    var sMeter: Int = 0
    var compMeter: Int = 0
    var alcMeter: Int = 0
    var powerMeter: Int = 0
    var swrMeter: Int = 0
    var idMeter: Int = 0
    var vdMeter: Int = 0
    // Settings
    var afGain: Int = 0
    var rfGain: Int = 0
    var rfPower: Int = 0
    var filterWidth: Int = 0
    var preamp: Int = 0
    var attenuator: Int = 0
    var noiseBlanker: Boolean = false
    var noiseReduction: Boolean = false
    var autoNotch: Boolean = false
    var compressor: Boolean = false
    var compressorLevel: Int = 0
    var nrLevel: Int = 0
    var nbLevel: Int = 0
    var tunerStatus: Int = 0
    var powerOn: Boolean = false
    var squelch: Int = 0
    var micGain: Int = 0
    var split: Boolean = false
    var vox: Boolean = false
    var breakIn: Boolean = false
    // Scope
    var scopeOn: Boolean = false
    var scopeSpan: Int = 0
    var scopeSpeed: Int = 0
    var scopeMode: Int = 0
    var scopeStartFreq: Long = 0
    // Extended DSP
    var antenna: Int = 0
    var agc: Int = 0
    var dnrLevel: Int = 0
    var contourLevel: Int = 0
    // Connection
    var serialConnected: Boolean = false
    var rxAudioSilent: Boolean = false
    var lastUpdate: Double = 0.0
    // Derived（fullState 附带，UI 渲染用）
    var modeName: String = ""
    var modeDisplay: String = ""
    var bandName: String = ""
    var sMeterDbm: Double = 0.0
    var sUnit: Int = 0
    var filterHz: Int = 0
    var preampLabel: String = ""
    var attenuatorLabel: String = ""
    var isTransmitting: Boolean = false
    var powerWatts: Double = 0.0
    var swrRatio: Double = 0.0
    var vdVolts: Double = 0.0
    var idAmps: Double = 0.0
    var alcPct: Double = 0.0

    /** 应用一批字段；返回实际被应用的 key 集合。 */
    fun apply(data: Map<String, JsonElement>): Set<String> {
        val applied = mutableSetOf<String>()
        for ((key, el) in data) if (applyField(key, el)) applied.add(key)
        return applied
    }

    private fun applyField(key: String, el: JsonElement): Boolean {
        fun b(): Boolean = (el as? JsonPrimitive)?.booleanOrNull ?: false
        fun i(): Int = (el as? JsonPrimitive)?.intOrNull ?: 0
        fun l(): Long = (el as? JsonPrimitive)?.longOrNull ?: 0L
        fun d(): Double = (el as? JsonPrimitive)?.doubleOrNull ?: 0.0
        fun s(): String = (el as? JsonPrimitive)?.contentOrNull ?: ""
        when (key) {
            "vfo_a_freq" -> vfoAFreq = l()
            "vfo_b_freq" -> vfoBFreq = l()
            "active_vfo" -> activeVfo = s()
            "active_freq" -> activeFreq = l()
            "mode" -> mode = i()
            "tx_status" -> txStatus = i()
            "s_meter" -> sMeter = i()
            "comp_meter" -> compMeter = i()
            "alc_meter" -> alcMeter = i()
            "power_meter" -> powerMeter = i()
            "swr_meter" -> swrMeter = i()
            "id_meter" -> idMeter = i()
            "vd_meter" -> vdMeter = i()
            "af_gain" -> afGain = i()
            "rf_gain" -> rfGain = i()
            "rf_power" -> rfPower = i()
            "filter_width" -> filterWidth = i()
            "preamp" -> preamp = i()
            "attenuator" -> attenuator = i()
            "noise_blanker" -> noiseBlanker = b()
            "noise_reduction" -> noiseReduction = b()
            "auto_notch" -> autoNotch = b()
            "compressor" -> compressor = b()
            "compressor_level" -> compressorLevel = i()
            "nr_level" -> nrLevel = i()
            "nb_level" -> nbLevel = i()
            "tuner_status" -> tunerStatus = i()
            "power_on" -> powerOn = b()
            "squelch" -> squelch = i()
            "mic_gain" -> micGain = i()
            "split" -> split = b()
            "vox" -> vox = b()
            "break_in" -> breakIn = b()
            "scope_on" -> scopeOn = b()
            "scope_span" -> scopeSpan = i()
            "scope_speed" -> scopeSpeed = i()
            "scope_mode" -> scopeMode = i()
            "scope_start_freq" -> scopeStartFreq = l()
            "antenna" -> antenna = i()
            "agc" -> agc = i()
            "dnr_level" -> dnrLevel = i()
            "contour_level" -> contourLevel = i()
            "serial_connected" -> serialConnected = b()
            "rx_audio_silent" -> rxAudioSilent = b()
            "last_update" -> lastUpdate = d()
            "mode_name" -> modeName = s()
            "mode_display" -> modeDisplay = s()
            "band_name" -> bandName = s()
            "s_meter_dbm" -> sMeterDbm = d()
            "s_unit" -> sUnit = i()
            "filter_hz" -> filterHz = i()
            "preamp_label" -> preampLabel = s()
            "attenuator_label" -> attenuatorLabel = s()
            "is_transmitting" -> isTransmitting = b()
            "power_watts" -> powerWatts = d()
            "swr_ratio" -> swrRatio = d()
            "vd_volts" -> vdVolts = d()
            "id_amps" -> idAmps = d()
            "alc_pct" -> alcPct = d()
            else -> return false
        }
        return true
    }

    /** 活跃 VFO 频率。 */
    val activeFrequency: Long get() = if (activeVfo == "B") vfoBFreq else vfoAFreq
}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*RadioStateTest*'
```
预期：4 个测试全过。

- [ ] **Step 5: Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Data/RadioState.kt FT710Android/app/src/test/java/com/hamradio/ft710android/Data/RadioStateTest.kt
git commit -m "feat(android): add RadioState server field mirror

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 协议 DTO 与命令序列化

**Files:**
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/Network/Protocol.kt`
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/Network/WsCommands.kt`
- Test: `FT710Android/app/src/test/java/com/hamradio/ft710android/Network/ProtocolTest.kt`

**Interfaces:**
- Consumes: `RadioState.apply(Map<String, JsonElement>)`（Task 2）；`JsonElement` 由 DTO 提供。
- Produces:
  - `sealed class WsEvent`：`FullState(data: JsonObject, bands: List<String>, modes: List<String>, memChannels: List<JsonElement?>, filterTables: FilterTables?, atr1000Enabled: Boolean)` · `StateUpdate(fields: JsonObject, dirty: List<String>)` · `MemChannels(channels: List<JsonElement?>)` · `ErrorEvent(message: String)` · `Pong` · `Unknown`
  - `fun parseWsEvent(text: String): WsEvent`
  - `object WsCommands`：`setNumber(field, value)`、`setString(field, value)`、`setBool(field, value)`、`ping()`、`getFullState()`、`memSaveJson(channelsJson: String)`

- [ ] **Step 1: 写失败测试**

`FT710Android/app/src/test/java/com/hamradio/ft710android/Network/ProtocolTest.kt`:
```kotlin
package com.hamradio.ft710android.Network

import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ProtocolTest {
    @Test fun `fullState parsed with bands modes memChannels filterTables`() {
        val text = """{"type":"fullState","data":{"vfo_a_freq":7050000,"mode":1},
            "bands":["160m","80m","40m"],"modes":["LSB","USB","CW"],
            "memChannels":[{"freq":7050000,"mode":"LSB","label":"40m 7.050"},null],
            "filterTables":{"voice":[300,500],"narrow":[3000,5000],"narrowModes":["CW","CW-L"]},
            "atr1000Enabled":false}"""
        val ev = parseWsEvent(text)
        assertTrue(ev is WsEvent.FullState)
        val f = ev as WsEvent.FullState
        assertEquals(3, f.bands.size)
        assertEquals(3, f.modes.size)
        assertEquals(2, f.memChannels.size)
        assertNull(f.memChannels[1])
        assertEquals(listOf(300, 500), f.filterTables!!.voice)
        assertEquals(false, f.atr1000Enabled)
        // data 原样保留，供 RadioState.apply
        assertEquals(7050000, f.data["vfo_a_freq"]!!.jsonPrimitive.int)
    }

    @Test fun `stateUpdate parsed with fields and dirty`() {
        val text = """{"type":"stateUpdate","fields":{"tx_status":1,"s_meter":9},"dirty":["tx_status","s_meter"]}"""
        val ev = parseWsEvent(text) as WsEvent.StateUpdate
        assertEquals(setOf("tx_status", "s_meter"), ev.dirty.toSet())
        assertEquals(1, ev.fields["tx_status"]!!.jsonPrimitive.int)
    }

    @Test fun `memChannels and error and pong parsed`() {
        assertTrue(parseWsEvent("""{"type":"memChannels","channels":[null,null]}""") is WsEvent.MemChannels)
        val err = parseWsEvent("""{"type":"error","message":"Radio not connected"}""")
        assertTrue(err is WsEvent.ErrorEvent)
        assertEquals("Radio not connected", (err as WsEvent.ErrorEvent).message)
        assertTrue(parseWsEvent("""{"type":"pong"}""") is WsEvent.Pong)
    }

    @Test fun `commands serialize exactly`() {
        assertEquals("""{"type":"set","field":"freq","value":7050000}""", WsCommands.setNumber("freq", 7050000))
        assertEquals("""{"type":"set","field":"mode","value":"USB"}""", WsCommands.setString("mode", "USB"))
        assertEquals("""{"type":"set","field":"ptt","value":true}""", WsCommands.setBool("ptt", true))
        assertEquals("""{"type":"ping"}""", WsCommands.ping())
        assertEquals("""{"type":"get","field":"fullState"}""", WsCommands.getFullState())
    }

    @Test fun `fullState with missing optional keys still parses`() {
        val ev = parseWsEvent("""{"type":"fullState","data":{"mode":1}}""")
        assertTrue(ev is WsEvent.FullState)
        assertTrue((ev as WsEvent.FullState).bands.isEmpty())
        assertNull(ev.filterTables)
    }
}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*ProtocolTest*'
```
预期：编译失败 / 符号未定义。

- [ ] **Step 3: 实现 Protocol.kt 与 WsCommands.kt**

`FT710Android/app/src/main/java/com/hamradio/ft710android/Network/Protocol.kt`:
```kotlin
package com.hamradio.ft710android.Network

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

private val json = Json { ignoreUnknownKeys = true }

@Serializable
data class FilterTables(
    val voice: List<Int> = emptyList(),
    val narrow: List<Int> = emptyList(),
    @SerialName("narrowModes") val narrowModes: List<String> = emptyList(),
)

@Serializable
data class FullStateDto(
    val type: String = "fullState",
    val data: JsonObject = JsonObject(emptyMap()),
    val bands: List<String> = emptyList(),
    val modes: List<String> = emptyList(),
    val memChannels: List<JsonElement?> = emptyList(),
    @SerialName("filterTables") val filterTables: FilterTables? = null,
    @SerialName("atr1000Enabled") val atr1000Enabled: Boolean = false,
)

@Serializable
data class StateUpdateDto(
    val type: String = "stateUpdate",
    val fields: JsonObject = JsonObject(emptyMap()),
    val dirty: List<String> = emptyList(),
)

@Serializable
data class MemChannelsDto(val type: String = "memChannels", val channels: List<JsonElement?> = emptyList())

@Serializable
data class ServerErrorDto(val type: String = "error", val message: String = "")

@Serializable
data class PongDto(val type: String = "pong")

sealed class WsEvent {
    data class FullState(
        val data: JsonObject,
        val bands: List<String>,
        val modes: List<String>,
        val memChannels: List<JsonElement?>,
        val filterTables: FilterTables?,
        val atr1000Enabled: Boolean,
    ) : WsEvent()

    data class StateUpdate(val fields: JsonObject, val dirty: List<String>) : WsEvent()
    data class MemChannels(val channels: List<JsonElement?>) : WsEvent()
    data class ErrorEvent(val message: String) : WsEvent()
    object Pong : WsEvent()
    object Unknown : WsEvent()
}

fun parseWsEvent(text: String): WsEvent {
    val root = runCatching { json.parseToJsonElement(text).jsonObject }.getOrNull() ?: return WsEvent.Unknown
    val type = (root["type"] as? JsonElement)?.jsonPrimitive?.contentOrNull ?: return WsEvent.Unknown
    return when (type) {
        "fullState" -> runCatching {
            val d = json.decodeFromString<FullStateDto>(text)
            WsEvent.FullState(d.data, d.bands, d.modes, d.memChannels, d.filterTables, d.atr1000Enabled)
        }.getOrElse { WsEvent.Unknown }
        "stateUpdate" -> runCatching {
            val d = json.decodeFromString<StateUpdateDto>(text)
            WsEvent.StateUpdate(d.fields, d.dirty)
        }.getOrElse { WsEvent.Unknown }
        "memChannels" -> runCatching {
            val d = json.decodeFromString<MemChannelsDto>(text)
            WsEvent.MemChannels(d.channels)
        }.getOrElse { WsEvent.Unknown }
        "error" -> runCatching {
            val d = json.decodeFromString<ServerErrorDto>(text)
            WsEvent.ErrorEvent(d.message)
        }.getOrElse { WsEvent.Unknown }
        "pong" -> WsEvent.Pong
        else -> WsEvent.Unknown
    }
}
```

`FT710Android/app/src/main/java/com/hamradio/ft710android/Network/WsCommands.kt`:
```kotlin
package com.hamradio.ft710android.Network

/**
 * /WSradio 上行命令。field 名与 server.py:_execute_set_command 逐字对齐。
 * 用字符串拼接而非序列化，保证值类型（数字/字符串/布尔）与服务端 switch 的判定完全一致。
 */
object WsCommands {
    fun setNumber(field: String, value: Number): String =
        """{"type":"set","field":"$field","value":$value}"""

    fun setString(field: String, value: String): String =
        """{"type":"set","field":"$field","value":"$value"}"""

    fun setBool(field: String, value: Boolean): String =
        """{"type":"set","field":"$field","value":$value}"""

    fun ping(): String = """{"type":"ping"}"""

    fun getFullState(): String = """{"type":"get","field":"fullState"}"""

    fun memSaveJson(channelsJson: String): String =
        """{"type":"memSave","channels":$channelsJson}"""
}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*ProtocolTest*'
```
预期：5 个测试全过。

- [ ] **Step 5: Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Network/Protocol.kt FT710Android/app/src/main/java/com/hamradio/ft710android/Network/WsCommands.kt FT710Android/app/src/test/java/com/hamradio/ft710android/Network/ProtocolTest.kt
git commit -m "feat(android): add WS protocol DTOs and command serialization

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: 记忆频道模型（6 槽 + null 补空 + label 键）

**Files:**
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/Data/MemoryChannels.kt`
- Test: `FT710Android/app/src/test/java/com/hamradio/ft710android/Data/MemoryChannelsTest.kt`

**Interfaces:**
- Consumes: `WsEvent.MemChannels` / `FullState.memChannels`（`List<JsonElement?>`）。
- Produces: `data class MemoryChannel(freq: Long, mode: String, label: String)`、`object MemoryChannels { fun parse(list: List<JsonElement?>): List<MemoryChannel?>; fun toJson(channels: List<MemoryChannel?>): String }`（后者生成 `memSave` 的 channels 数组 JSON）。

- [ ] **Step 1: 写失败测试**

`FT710Android/app/src/test/java/com/hamradio/ft710android/Data/MemoryChannelsTest.kt`:
```kotlin
package com.hamradio.ft710android.Data

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class MemoryChannelsTest {
    @Test fun `parse six slots with null padding`() {
        val raw = Json.parseToJsonElement(
            """[{"freq":7050000,"mode":"LSB","label":"40m 7.050"},null,{"freq":14270000,"mode":"USB","label":"20m 14.270"},null,null,null]"""
        ).jsonArray
        val list = MemoryChannels.parse(raw)
        assertEquals(6, list.size)
        assertEquals(MemoryChannel(7050000, "LSB", "40m 7.050"), list[0])
        assertNull(list[1])
        assertEquals(MemoryChannel(14270000, "USB", "20m 14.270"), list[2])
    }

    @Test fun `toJson round-trips six slots and drops null padding for save`() {
        val channels = listOf<MemoryChannel?>(
            MemoryChannel(7050000, "LSB", "40m 7.050"), null,
            MemoryChannel(14270000, "USB", "20m 14.270"), null, null, null
        )
        val json = MemoryChannels.toJson(channels)
        val expected = """[{"freq":7050000,"mode":"LSB","label":"40m 7.050"},{"freq":14270000,"mode":"USB","label":"20m 14.270"}]"""
        assertEquals(expected, json)
    }
}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*MemoryChannelsTest*'
```

- [ ] **Step 3: 实现 MemoryChannels**

`FT710Android/app/src/main/java/com/hamradio/ft710android/Data/MemoryChannels.kt`:
```kotlin
package com.hamradio.ft710android.Data

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull

data class MemoryChannel(val freq: Long, val mode: String, val label: String = "")

object MemoryChannels {
    /** 解析服务端 channels 数组（6 槽、null 补空）。返回长度与入参一致，null 表示空槽。 */
    fun parse(list: List<JsonElement?>): List<MemoryChannel?> = list.map { el ->
        if (el is JsonNull || el == null) null
        else runCatching {
            val o = el.jsonObject
            MemoryChannel(
                freq = (o["freq"] as? JsonElement)?.jsonPrimitive?.longOrNull ?: 0L,
                mode = (o["mode"] as? JsonElement)?.jsonPrimitive?.contentOrNull ?: "",
                label = (o["label"] as? JsonElement)?.jsonPrimitive?.contentOrNull ?: "",
            )
        }.getOrNull()
    }

    /** 序列化用于 memSave（丢弃 null 空槽，键名 freq/mode/label 与服务端一致）。 */
    fun toJson(channels: List<MemoryChannel?>): String {
        val arr = buildJsonArray {
            for (c in channels) if (c != null) add(
                buildJsonObject {
                    put("freq", c.freq)
                    put("mode", c.mode)
                    put("label", c.label)
                }
            )
        }
        return arr.toString()
    }
}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*MemoryChannelsTest*'
```

- [ ] **Step 5: Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Data/MemoryChannels.kt FT710Android/app/src/test/java/com/hamradio/ft710android/Data/MemoryChannelsTest.kt
git commit -m "feat(android): add 6-slot memory channel model

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: 频谱帧解析（1701B）

**Files:**
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/Spectrum/SpectrumFrame.kt`
- Test: `FT710Android/app/src/test/java/com/hamradio/ft710android/Spectrum/SpectrumFrameTest.kt`

**Interfaces:**
- Consumes: `/WSspectrum` 原始二进制帧 `ByteArray`。
- Produces: `data class SpectrumFrame(version: Int, wf1: IntArray, wf2: IntArray)`；`fun parseSpectrumFrame(frame: ByteArray): SpectrumFrame?`（长度≠1701 或 version≠0x01 返回 null）。

- [ ] **Step 1: 写失败测试**

`FT710Android/app/src/test/java/com/hamradio/ft710android/Spectrum/SpectrumFrameTest.kt`:
```kotlin
package com.hamradio.ft710android.Spectrum

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class SpectrumFrameTest {
    private fun makeFrame(wf1Value: Byte = 0x55): ByteArray {
        val f = ByteArray(1701)
        f[0] = 0x01
        for (i in 1..850) f[i] = wf1Value
        for (i in 851..1700) f[i] = (i - 850).toByte()
        return f
    }

    @Test fun `parses valid 1701-byte frame`() {
        val sf = parseSpectrumFrame(makeFrame())!!
        assertEquals(1, sf.version)
        assertEquals(850, sf.wf1.size)
        assertEquals(850, sf.wf2.size)
        assertEquals(0x55, sf.wf1[0] and 0xFF)
        assertEquals(1, sf.wf2[0] and 0xFF)
    }

    @Test fun `rejects wrong length`() { assertNull(parseSpectrumFrame(ByteArray(100))) }

    @Test fun `rejects wrong version byte`() {
        val bad = makeFrame().also { it[0] = 0x02 }
        assertNull(parseSpectrumFrame(bad))
    }

    @Test fun `handles empty input`() { assertNull(parseSpectrumFrame(ByteArray(0))) }
}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*SpectrumFrameTest*'
```

- [ ] **Step 3: 实现 SpectrumFrame**

`FT710Android/app/src/main/java/com/hamradio/ft710android/Spectrum/SpectrumFrame.kt`:
```kotlin
package com.hamradio.ft710android.Spectrum

data class SpectrumFrame(val version: Int, val wf1: IntArray, val wf2: IntArray) {
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is SpectrumFrame) return false
        return version == other.version && wf1.contentEquals(other.wf1) && wf2.contentEquals(other.wf2)
    }
    override fun hashCode(): Int = version * 31 + wf1.contentHashCode() + wf2.contentHashCode()
}

/** 1701B = 1B version(0x01) + 850B wf1 + 850B wf2；非法帧返回 null。 */
fun parseSpectrumFrame(frame: ByteArray): SpectrumFrame? {
    if (frame.size != 1701) return null
    if (frame[0] != 0x01.toByte()) return null
    val wf1 = IntArray(850)
    val wf2 = IntArray(850)
    for (i in 0 until 850) wf1[i] = frame[i + 1].toInt() and 0xFF
    for (i in 0 until 850) wf2[i] = frame[i + 851].toInt() and 0xFF
    return SpectrumFrame(version = 1, wf1 = wf1, wf2 = wf2)
}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*SpectrumFrameTest*'
```

- [ ] **Step 5: Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Spectrum/SpectrumFrame.kt FT710Android/app/src/test/java/com/hamradio/ft710android/Spectrum/SpectrumFrameTest.kt
git commit -m "feat(android): parse 1701-byte spectrum frames

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: PTTManager 状态机（安全关键）

**Files:**
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/PTT/PTTManager.kt`
- Test: `FT710Android/app/src/test/java/com/hamradio/ft710android/PTT/PTTManagerTest.kt`

**Interfaces:**
- Consumes: 全部依赖经构造注入（不碰 Android/网络），后续由 `MainViewModel` 接线。
- Produces:
```kotlin
class PTTManager(
    val sendPTT: (Boolean) -> Unit,
    val sendTXAudioStop: () -> Unit,
    val startTxAudio: () -> Unit,
    val stopTxAudio: () -> Unit,
    val serverTXStatus: () -> Int,
    val isCtrlConnected: () -> Boolean,
    val onStuckTX: () -> Unit,
) {
    enum class Phase { Idle, Keying, Keyed, Releasing }
    var phase: Phase = Phase.Idle; private set
    val isTX: Boolean get() = phase == Phase.Keying || phase == Phase.Keyed
    var watchdogIntervalMs: Long = 500
    var maxRetries: Int = 3
    fun press()
    fun release()
    fun forceRelease()
    fun onStatusReceived(txStatus: Int)   // 状态机轮询回显的入口
}
```
  `press()` 在 Idle 且 ctrl 已连接时受理；`release()` 无条件发 `ptt:false` + `s:` + 停 TX 音频 + 起看门狗；`forceRelease()` 任意状态幂等。看门狗用注入的调度器（测试用 `runTest` 虚拟时间）。

- [ ] **Step 1: 写失败测试**

`FT710Android/app/src/test/java/com/hamradio/ft710android/PTT/PTTManagerTest.kt`:
```kotlin
package com.hamradio.ft710android.PTT

import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PTTManagerTest {
    private class Harness {
        val sentPTT = mutableListOf<Boolean>()
        var txAudioStopCalls = 0
        var txAudioStartCalls = 0
        var txAudioStopCalls = 0
        var connected = true
        var txStatus = 0
        var stuck = 0
        val dispatcher = StandardTestDispatcher()

        val manager = PTTManager(
            sendPTT = { sentPTT.add(it) },
            sendTXAudioStop = { txAudioStopCalls++ },
            startTxAudio = { txAudioStartCalls++ },
            stopTxAudio = { txAudioStopCalls++ },
            serverTXStatus = { txStatus },
            isCtrlConnected = { connected },
            onStuckTX = { stuck++ },
            dispatcher = dispatcher,
        ).also { it.watchdogIntervalMs = 500; it.maxRetries = 3 }
    }

    @Test fun `press while disconnected is rejected without commands`() = runTest(harness.dispatcher) {
        val h = Harness(); h.connected = false
        h.manager.press()
        assertEquals(PTTManager.Phase.Idle, h.manager.phase)
        assertTrue(h.sentPTT.isEmpty())
        assertEquals(0, h.txAudioStartCalls)
    }

    @Test fun `press then release always sends ptt false`() = runTest(harness.dispatcher) {
        val h = Harness()
        h.manager.press()
        assertTrue(h.manager.isTX)
        assertEquals(listOf(true), h.sentPTT)
        h.manager.release()
        assertEquals(listOf(true, false), h.sentPTT)
        assertEquals(1, h.txAudioStopCalls)
        assertEquals(1, h.txAudioStopCalls) // 's:' 帧
    }

    @Test fun `watchdog resends and gives up after maxRetries`() = runTest(harness.dispatcher) {
        val h = Harness()
        h.manager.press(); h.txStatus = 1
        h.manager.release()
        // 回显仍是 TX，看门狗重发 3 次后触发 stuck
        advanceTimeBy(1500) // 500ms x 3
        assertEquals(3, h.sentPTT.count { !it })
        assertEquals(1, h.stuck)
        assertEquals(PTTManager.Phase.Idle, h.manager.phase)
    }

    @Test fun `watchdog exits when echo goes RX`() = runTest(harness.dispatcher) {
        val h = Harness()
        h.manager.press(); h.txStatus = 1
        h.manager.release()
        advanceTimeBy(499)
        h.txStatus = 0
        advanceTimeBy(1)
        assertEquals(1, h.sentPTT.count { !it }) // 只有首次 release 的那条
        assertEquals(0, h.stuck)
        assertEquals(PTTManager.Phase.Idle, h.manager.phase)
    }

    @Test fun `press during watchdog cancels retries`() = runTest(harness.dispatcher) {
        val h = Harness()
        h.manager.press(); h.txStatus = 1
        h.manager.release()
        advanceTimeBy(500)
        h.manager.press()
        advanceTimeBy(2000)
        assertEquals(PTTManager.Phase.Keyed, h.manager.phase)
        assertTrue(h.stuck == 0)
    }

    @Test fun `forceRelease is idempotent from any state`() = runTest(harness.dispatcher) {
        val h = Harness()
        h.manager.press(); h.txStatus = 1
        h.manager.forceRelease()
        h.manager.forceRelease()
        assertEquals(2, h.sentPTT.count { !it })
        assertEquals(2, h.txAudioStopCalls)
        assertEquals(PTTManager.Phase.Idle, h.manager.phase)
    }
}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*PTTManagerTest*'
```

- [ ] **Step 3: 实现 PTTManager**

`FT710Android/app/src/main/java/com/hamradio/ft710android/PTT/PTTManager.kt`:
```kotlin
package com.hamradio.ft710android.PTT

import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * PTT 安全状态机。纯 Kotlin，全部依赖注入，JVM 可单测。
 *
 * 安全铁律（spec §7，直接修掉 iOS P0 竞态）：
 *  - release() 无条件发 ptt:false（不等服务端回显）。
 *  - release 后看门狗轮询回显，仍 TX 则重发 TX0，最多 maxRetries 次。
 *  - forceRelease() 任意状态幂等，供断连/退后台调用。
 *  - press() 仅在 Idle 且控制通道已连接时受理——避免"发不出去的乐观 TX"。
 */
class PTTManager(
    val sendPTT: (Boolean) -> Unit,
    val sendTXAudioStop: () -> Unit,
    val startTxAudio: () -> Unit,
    val stopTxAudio: () -> Unit,
    val serverTXStatus: () -> Int,
    val isCtrlConnected: () -> Boolean,
    val onStuckTX: () -> Unit,
    private val dispatcher: CoroutineDispatcher? = null,
) {
    enum class Phase { Idle, Keying, Keyed, Releasing }

    @Volatile var phase: Phase = Phase.Idle; private set
    val isTX: Boolean get() = phase == Phase.Keying || phase == Phase.Keyed

    var watchdogIntervalMs: Long = 500
    var maxRetries: Int = 3

    private val scope = CoroutineScope(SupervisorJob() + (dispatcher ?: kotlinx.coroutines.Dispatchers.Default))
    private var watchdogJob: Job? = null
    private var retryCount = 0

    fun press() {
        if (phase != Phase.Idle) return
        if (!isCtrlConnected()) return // 不产生任何命令
        sendPTT(true)
        startTxAudio()
        watchdogJob?.cancel()
        retryCount = 0
        phase = Phase.Keyed // 乐观置位，不等回显
    }

    fun release() {
        if (phase == Phase.Idle) return
        sendPTT(false)
        stopTxAudio()
        sendTXAudioStop()
        phase = Phase.Releasing
        startWatchdog()
    }

    fun forceRelease() {
        sendPTT(false)
        stopTxAudio()
        sendTXAudioStop()
        watchdogJob?.cancel()
        retryCount = 0
        phase = Phase.Idle
        startWatchdog() // 幂等：重复调用无害，重试会在回显 RX 后停
    }

    /** 外部收到 tx_status 回显时的入口（看门狗与 UI 共用）。open 供测试 spy 覆写。 */
    open fun onStatusReceived(txStatus: Int) {
        if (phase == Phase.Releasing && txStatus == 0) {
            watchdogJob?.cancel()
            retryCount = 0
            phase = Phase.Idle
        }
    }

    private fun startWatchdog() {
        watchdogJob?.cancel()
        retryCount = 0
        watchdogJob = scope.launch {
            while (true) {
                delay(watchdogIntervalMs)
                if (serverTXStatus() == 0) {
                    phase = Phase.Idle; retryCount = 0; return@launch
                }
                if (retryCount >= maxRetries) {
                    onStuckTX(); phase = Phase.Idle; retryCount = 0; return@launch
                }
                sendPTT(false)
                retryCount++
            }
        }
    }
}

- [ ] **Step 4: 运行测试确认通过**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*PTTManagerTest*'
```
预期：6 个测试全过。若 `StandardTestDispatcher` 需要显式 `advanceTimeBy`，按 kotlinx-coroutines-test 语义微调（测试用 `runTest` + `advanceUntilIdle()` 替代 `advanceTimeBy` 亦可）。

- [ ] **Step 5: Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/PTT/PTTManager.kt FT710Android/app/src/test/java/com/hamradio/ft710android/PTT/PTTManagerTest.kt
git commit -m "feat(android): add PTT safety state machine with watchdog

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: AuthApi（登录/登出/状态 + 自签 TLS）

**Files:**
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/Network/AuthApi.kt`
- Test: `FT710Android/app/src/test/java/com/hamradio/ft710android/Network/AuthApiTest.kt`

**Interfaces:**
- Consumes: OkHttp `OkHttpClient`（含接受自签证书的 TrustManager，本任务内组装）。
- Produces: `class AuthApi(private val client: OkHttpClient)`：
  - `suspend fun login(baseUrl: String, password: String): AuthResult`；`sealed class AuthResult { data class Success(val token: String) : AuthResult(); data class Failure(val status: Int, val message: String) : AuthResult() }`
  - `suspend fun logout(baseUrl: String)`：`POST /api/auth/logout`，带 cookie。
  - 组装 `fun selfSignedOkHttpClient(): OkHttpClient`（Task 17 再统一挂网络配置，这里先供测试）。

- [ ] **Step 1: 写失败测试**

`FT710Android/app/src/test/java/com/hamradio/ft710android/Network/AuthApiTest.kt`:
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

class AuthApiTest {
    private lateinit var server: MockWebServer
    private lateinit var api: AuthApi

    @Before fun setUp() {
        server = MockWebServer()
        server.start()
        api = AuthApi(OkHttpClient())
    }

    @After fun tearDown() { server.shutdown() }

    @Test fun `login success returns token`() = runTest {
        server.enqueue(MockResponse()
            .setResponseCode(200)
            .setHeader("Set-Cookie", "ft710_auth=abc123; Path=/; HttpOnly")
            .setBody("""{"ok":true,"token":"abc123"}"""))
        val res = api.login(server.url("/").toString().trimEnd('/'), "secret")
        assertTrue(res is AuthResult.Success)
        assertEquals("abc123", (res as AuthResult.Success).token)
        val recorded = server.takeRequest()
        assertEquals("/api/auth/login", recorded.path)
        assertTrue(recorded.body.readUtf8().contains("\"password\":\"secret\""))
    }

    @Test fun `login wrong password returns 401 failure`() = runTest {
        server.enqueue(MockResponse().setResponseCode(401).setBody("""{"error":"Invalid password"}"""))
        val res = api.login(server.url("/").toString().trimEnd('/'), "bad")
        assertTrue(res is AuthResult.Failure)
        assertEquals(401, (res as AuthResult.Failure).status)
    }

    @Test fun `login rate limited returns 429 failure`() = runTest {
        server.enqueue(MockResponse().setResponseCode(429).setBody("""{"error":"Too many login attempts. Please try again later."}"""))
        val res = api.login(server.url("/").toString().trimEnd('/'), "secret")
        assertTrue(res is AuthResult.Failure)
        assertEquals(429, (res as AuthResult.Failure).status)
    }

    @Test fun `logout posts and clears cookie`() = runTest {
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"ok":true}"""))
        api.logout(server.url("/").toString().trimEnd('/'), "abc123")
        val recorded = server.takeRequest()
        assertEquals("/api/auth/logout", recorded.path)
        assertEquals("ft710_auth=abc123", recorded.getHeader("Cookie"))
    }
}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*AuthApiTest*'
```

- [ ] **Step 3: 实现 AuthApi**

`FT710Android/app/src/main/java/com/hamradio/ft710android/Network/AuthApi.kt`:
```kotlin
package com.hamradio.ft710android.Network

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.security.SecureRandom
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager

sealed class AuthResult {
    data class Success(val token: String) : AuthResult()
    data class Failure(val status: Int, val message: String) : AuthResult()
}

class AuthApi(private val client: OkHttpClient) {

    suspend fun login(baseUrl: String, password: String): AuthResult = withContext(Dispatchers.IO) {
        val body = """{"password":"$password"}""".toRequestBody("application/json".toMediaType())
        val req = Request.Builder()
            .url("$baseUrl/api/auth/login")
            .post(body)
            .build()
        runCatching { client.newCall(req).execute().use { resp ->
            val text = resp.body?.string().orEmpty()
            if (resp.code == 200 && text.contains("\"ok\":true")) {
                val token = regexToken.find(text)?.groupValues?.get(1) ?: ""
                AuthResult.Success(token)
            } else {
                AuthResult.Failure(resp.code, text)
            }
        } }.getOrElse { AuthResult.Failure(0, it.message ?: "network error") }
    }

    suspend fun logout(baseUrl: String, token: String) = withContext(Dispatchers.IO) {
        val req = Request.Builder()
            .url("$baseUrl/api/auth/logout")
            .header("Cookie", "ft710_auth=$token")
            .post("{}".toRequestBody("application/json".toMediaType()))
            .build()
        runCatching { client.newCall(req).execute().close() }
    }

    companion object {
        private val regexToken = Regex(""""token":"([^"]+)"""")

        /** 接受自签证书的 OkHttpClient（spec §8；本机/局域网自签服务端）。 */
        fun selfSignedOkHttpClient(): OkHttpClient {
            val trustAll = object : X509TrustManager {
                override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
                override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
                override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
            }
            val sslContext = SSLContext.getInstance("TLS")
            sslContext.init(null, arrayOf<TrustManager>(trustAll), SecureRandom())
            return OkHttpClient.Builder()
                .sslSocketFactory(sslContext.socketFactory, trustAll)
                .hostnameVerifier { _, _ -> true }
                .build()
        }
    }
}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*AuthApiTest*'
```

- [ ] **Step 5: Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Network/AuthApi.kt FT710Android/app/src/test/java/com/hamradio/ft710android/Network/AuthApiTest.kt
git commit -m "feat(android): add login/logout API with self-signed TLS

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: WebSocketConnection（OkHttp WS 封装）

**Files:**
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/Network/WebSocketConnection.kt`
- Test: `FT710Android/app/src/test/java/com/hamradio/ft710android/Network/WebSocketConnectionTest.kt`

**Interfaces:**
- Consumes: OkHttp `WebSocket`/`WebSocketListener`。
- Produces:
```kotlin
class WebSocketConnection(
    private val client: OkHttpClient,
    val url: String,                 // 含 ?token=
    val onText: (String) -> Unit,
    val onBinary: (ByteArray) -> Unit,
    val onStateChange: (State) -> Unit,
) {
    enum class State { Idle, Connecting, Connected, Failed }
    fun connect()                    // 打开连接；失败转 Failed，由 ConnectionManager 决定重连
    fun sendText(text: String): Boolean
    fun sendBinary(data: ByteArray): Boolean
    fun close(code: Int = 1000, reason: String = "")
}
```

- [ ] **Step 1: 写失败测试**

`FT710Android/app/src/test/java/com/hamradio/ft710android/Network/WebSocketConnectionTest.kt`:
```kotlin
package com.hamradio.ft710android.Network

import okhttp3.OkHttpClient
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test

class WebSocketConnectionTest {
    private lateinit var server: MockWebServer

    @Before fun setUp() { server = MockWebServer() }

    @After fun tearDown() { server.shutdown() }

    private fun serverWebSocket(messages: List<String>): WebSocketListener? {
        // MockWebServer 4.x：Dispatcher 返回带 websocket(listener) 的 MockResponse 完成升级
        server.dispatcher = object : okhttp3.mockwebserver.Dispatcher() {
            override fun dispatch(request: okhttp3.mockwebserver.RecordedRequest): MockResponse {
                if (request.path?.startsWith("/WSradio") == true) {
                    return MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                        override fun onOpen(webSocket: okhttp3.WebSocket, response: okhttp3.Response) {
                            messages.forEach { webSocket.send(it) }
                        }
                    })
                }
                return MockResponse().setResponseCode(404)
            }
        }
        return null
    }

    @Test fun `connects receives text and reports Connected`() {
        val texts = mutableListOf<String>()
        var states = mutableListOf<WebSocketConnection.State>()
        serverWebSocket(listOf("""{"type":"pong"}"""))
        val conn = WebSocketConnection(
            client = OkHttpClient(),
            url = server.url("/WSradio?token=t").toString(),
            onText = { texts.add(it) },
            onBinary = {},
            onStateChange = { states.add(it) },
        )
        conn.connect()
        Thread.sleep(300)
        assertEquals(listOf(WebSocketConnection.State.Connecting, WebSocketConnection.State.Connected), states)
        assertEquals(listOf("""{"type":"pong"}"""), texts)
        conn.close()
    }
}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*WebSocketConnectionTest*'
```
（若 MockWebServer 的 `withWebSocketUpgrade` API 名不同，查 okhttp 4.12 mockwebserver 实际 API 适配；测试目标是验证 connect→Connected→收到文本。）

- [ ] **Step 3: 实现 WebSocketConnection**

`FT710Android/app/src/main/java/com/hamradio/ft710android/Network/WebSocketConnection.kt`:
```kotlin
package com.hamradio.ft710android.Network

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString

/** 单路 WebSocket 封装：文本/二进制回调和状态回调；重连策略由 ConnectionManager 负责。 */
class WebSocketConnection(
    private val client: OkHttpClient,
    val url: String,
    private val onText: (String) -> Unit,
    private val onBinary: (ByteArray) -> Unit,
    private val onStateChange: (State) -> Unit,
) {
    enum class State { Idle, Connecting, Connected, Failed }

    @Volatile private var socket: WebSocket? = null

    fun connect() {
        onStateChange(State.Connecting)
        val request = Request.Builder().url(url).build()
        socket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                onStateChange(State.Connected)
            }
            override fun onMessage(webSocket: WebSocket, text: String) { onText(text) }
            override fun onMessage(webSocket: WebSocket, bytes: ByteString) { onBinary(bytes.toByteArray()) }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                onStateChange(State.Failed)
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                onStateChange(State.Failed)
            }
        })
    }

    fun sendText(text: String): Boolean = socket?.send(text) ?: false
    fun sendBinary(data: ByteArray): Boolean = socket?.send(ByteString.of(*data)) ?: false
    fun close(code: Int = 1000, reason: String = "") {
        socket?.close(code, reason)
        socket = null
    }
}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*WebSocketConnectionTest*'
```

- [ ] **Step 5: Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Network/WebSocketConnection.kt FT710Android/app/src/test/java/com/hamradio/ft710android/Network/WebSocketConnectionTest.kt
git commit -m "feat(android): add OkHttp WebSocket wrapper

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 9: ConnectionManager（4 路 socket 编排）

**Files:**
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/Network/ConnectionManager.kt`
- Test: `FT710Android/app/src/test/java/com/hamradio/ft710android/Network/ConnectionManagerTest.kt`

**Interfaces:**
- Consumes: `AuthApi`、`WebSocketConnection`、`parseWsEvent`。
- Produces:
```kotlin
class ConnectionManager(
    private val client: OkHttpClient,
    private val scope: CoroutineScope,
    private val onRadioEvent: (WsEvent) -> Unit,
    private val onAudioRx: (ByteArray) -> Unit,
    private val onSpectrum: (ByteArray) -> Unit,
    private val onAudioTxText: (String) -> Unit,   // 's:'/'m:' 文本
    private val onAtrEvent: (String) -> Unit,       // 可选
    private val onConnectionChange: (Boolean) -> Unit, // 全部 4 路是否均连接
) {
    fun start(baseUrl: String, token: String)     // 建 4 路 socket，拼 ?token=
    fun sendSet(field: String, value: Any)        // 数字/字符串/布尔 自动选 setNumber/setString/setBool
    fun sendPing()
    fun sendMemSave(channelsJson: String)
    fun sendTxAudioBinary(data: ByteArray)
    fun sendTxAudioText(text: String)             // "s:" / "m:..."
    fun stopAll()
    val isConnected: Boolean
    fun reconnectAll()
}
```
  baseUrl 形如 `https://radio.vlsc.net:8888`，WS URL = `${baseUrl.replaceFirst("http","ws")}/WSradio?token=...`（`https`→`wss`、`http`→`ws`）。

- [ ] **Step 1: 写失败测试**

`FT710Android/app/src/test/java/com/hamradio/ft710android/Network/ConnectionManagerTest.kt`:
```kotlin
package com.hamradio.ft710android.Network

import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class ConnectionManagerTest {
    @Test fun `sendSet routes by value type`() {
        val sent = mutableListOf<String>()
        val cm = ConnectionManager(
            client = OkHttpClient(),
            scope = kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.Unconfined),
            onRadioEvent = {}, onAudioRx = {}, onSpectrum = {}, onAudioTxText = {},
            onAtrEvent = {}, onConnectionChange = {},
            sendOverride = { sent.add(it) },
        )
        cm.sendSet("freq", 7050000)
        cm.sendSet("mode", "USB")
        cm.sendSet("ptt", true)
        assertEquals(listOf(
            """{"type":"set","field":"freq","value":7050000}""",
            """{"type":"set","field":"mode","value":"USB"}""",
            """{"type":"set","field":"ptt","value":true}""",
        ), sent)
    }

    @Test fun `wsUrl converts scheme`() {
        assertEquals("wss://radio.vlsc.net:8888/WSradio?token=abc",
            ConnectionManager.wsUrl("https://radio.vlsc.net:8888", "/WSradio", "abc"))
        assertEquals("ws://192.168.1.10:8888/WSspectrum?token=t",
            ConnectionManager.wsUrl("http://192.168.1.10:8888", "/WSspectrum", "t"))
    }

    @Test fun `isConnected false before start`() {
        val cm = ConnectionManager(OkHttpClient(), kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.Unconfined),
            {}, {}, {}, {}, {}, {}, sendOverride = {})
        assertFalse(cm.isConnected)
    }
}
```
> 注：`sendOverride` 是测试注入口，生产环境为 null（走真实 4 路 socket）。`ConnectionManager` 的构造加可选参数 `private val sendOverride: ((String) -> Unit)? = null`；`sendSet` 组装命令文本后：`(sendOverride ?: { realSockets.forEach { it.sendText(it) } })(cmd)`。

- [ ] **Step 2: 运行测试确认失败**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*ConnectionManagerTest*'
```

- [ ] **Step 3: 实现 ConnectionManager**

`FT710Android/app/src/main/java/com/hamradio/ft710android/Network/ConnectionManager.kt`:
```kotlin
package com.hamradio.ft710android.Network

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient

/** 4 路 (+可选 ATR1000) WS 编排：认证 token 注入、心跳、命令路由、连接状态聚合。 */
class ConnectionManager(
    private val client: OkHttpClient,
    private val scope: CoroutineScope,
    private val onRadioEvent: (WsEvent) -> Unit,
    private val onAudioRx: (ByteArray) -> Unit,
    private val onSpectrum: (ByteArray) -> Unit,
    private val onAudioTxText: (String) -> Unit,
    private val onAtrEvent: (String) -> Unit,
    private val onConnectionChange: (Boolean) -> Unit,
    private val sendOverride: ((String) -> Unit)? = null,
) {
    private var radio: WebSocketConnection? = null
    private var audioRx: WebSocketConnection? = null
    private var audioTx: WebSocketConnection? = null
    private var spectrum: WebSocketConnection? = null
    private var atr: WebSocketConnection? = null
    private var heartbeat: Job? = null
    private val connectedFlags = mutableSetOf<String>()

    @Volatile var isConnected: Boolean = false; private set

    companion object {
        fun wsUrl(baseUrl: String, path: String, token: String): String {
            val scheme = if (baseUrl.startsWith("https")) "wss" else "ws"
            val host = baseUrl.removePrefix("https://").removePrefix("http://")
            return "$scheme://$host$path?token=$token"
        }
    }

    fun start(baseUrl: String, token: String) {
        _baseUrl = baseUrl; _token = token
        stopAll()
        radio = connect(baseUrl, "/WSradio", token,
            onText = { parseWsEvent(it).let(::onRadioEvent) }, onBinary = {})
        audioRx = connect(baseUrl, "/WSaudioRX", token, onText = {}, onBinary = ::onAudioRx)
        audioTx = connect(baseUrl, "/WSaudioTX", token,
            onText = ::onAudioTxText, onBinary = {})  // 上行二进制由 sendTxAudioBinary 发送
        spectrum = connect(baseUrl, "/WSspectrum", token, onText = {}, onBinary = ::onSpectrum)
        atr = connect(baseUrl, "/WSatr1000", token, onText = ::onAtrEvent, onBinary = {})
        heartbeat?.cancel()
        heartbeat = scope.launch { while (isActive) { sendPing(); delay(2000) } }
    }

    fun stopAll() {
        heartbeat?.cancel()
        listOfNotNull(radio, audioRx, audioTx, spectrum, atr).forEach { it.close() }
        radio = null; audioRx = null; audioTx = null; spectrum = null; atr = null
        connectedFlags.clear(); updateConnected()
    }

    fun sendSet(field: String, value: Any) {
        val cmd = when (value) {
            is Boolean -> WsCommands.setBool(field, value)
            is String -> WsCommands.setString(field, value)
            is Number -> WsCommands.setNumber(field, value)
            else -> WsCommands.setNumber(field, value.toString().toLongOrNull() ?: 0L)
        }
        dispatch(cmd)
    }

    fun sendPing() = dispatch(WsCommands.ping())
    fun sendMemSave(channelsJson: String) = dispatch(WsCommands.memSaveJson(channelsJson))
    fun sendTxAudioBinary(data: ByteArray) { audioTx?.sendBinary(data) }
    fun sendTxAudioText(text: String) { audioTx?.sendText(text) }

    fun reconnectAll() {
        val token = _token ?: return
        val base = _baseUrl ?: return
        stopAll(); start(base, token)
    }

    private var _baseUrl: String? = null
    private var _token: String? = null

    private fun connect(baseUrl: String, path: String, token: String, onText: (String) -> Unit, onBinary: (ByteArray) -> Unit): WebSocketConnection {
        val url = wsUrl(baseUrl, path, token)
        val conn = WebSocketConnection(client, url, onText, onBinary) { state ->
            if (state == WebSocketConnection.State.Connected) connectedFlags.add(path)
            else connectedFlags.remove(path)
            updateConnected()
        }
        conn.connect()
        return conn
    }

    private fun updateConnected() {
        val all = setOf("/WSradio", "/WSaudioRX", "/WSaudioTX", "/WSspectrum").all { it in connectedFlags }
        if (all != isConnected) { isConnected = all; onConnectionChange(all) }
    }

    private fun dispatch(cmd: String) {
        if (sendOverride != null) { sendOverride(cmd); return }
        radio?.sendText(cmd)
    }
}
```
> 说明：`onAtrEvent` 收到的是 JSON 文本，Task 16 用 `atr1000Enabled` 决定是否展示天调 UI；`/WSatr1000` 在服务端禁用时 close 4000 → 该路进入 Failed，不影响 4 路主状态（`updateConnected` 只聚合主 4 路）。

- [ ] **Step 4: 运行测试确认通过**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*ConnectionManagerTest*'
```

- [ ] **Step 5: Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Network/ConnectionManager.kt FT710Android/app/src/test/java/com/hamradio/ft710android/Network/ConnectionManagerTest.kt
git commit -m "feat(android): add 4-socket connection manager

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 10: libopus NDK 构建 + OpusBridge（JNI）

**Files:**
- Create: `FT710Android/app/src/main/cpp/CMakeLists.txt`
- Create: `FT710Android/app/src/main/cpp/opus_jni.c`
- Create: `FT710Android/app/src/main/cpp/opus/`（vendored libopus 源码，见 Step 2）
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/OpusBridge.kt`
- Create: `FT710Android/app/src/androidTest/java/com/hamradio/ft710android/Audio/OpusBridgeTest.kt`（仪器测试，真机/模拟器上跑）
- Modify: `FT710Android/app/build.gradle.kts`（加 externalNativeBuild + androidTest deps）

**Interfaces:**
- Consumes: libopus（vendored 源码，CMake 构建）。
- Produces:
```kotlin
object OpusBridge {
    external fun encoderCreate(sampleRate: Int, channels: Int, bitrate: Int): Long
    external fun encoderEncode(handle: Long, pcm: ShortArray, out: ByteArray): Int  // 返回字节数
    external fun decoderCreate(sampleRate: Int, channels: Int): Long
    external fun decoderDecode(handle: Long, opus: ByteArray, len: Int, pcm: ShortArray): Int // 返回样本数
    external fun destroy(handle: Long)
}
```
  TX 编码帧 = 960 样本（48k 20ms）→ Opus 64kbps CBR；RX 解码 48k → 960 样本/帧。

- [ ] **Step 1: 在 build.gradle.kts 启用 native 构建 + androidTest 依赖**

`FT710Android/app/build.gradle.kts` 加：
```kotlin
android {
    defaultConfig { externalNativeBuild { cmake { cppFlags += ""; arguments += listOf("-DOPUS_BUILD_SHARED_LIBRARY=0") } } }
    externalNativeBuild { cmake { path = file("src/main/cpp/CMakeLists.txt") } }
    ndkVersion = "27.2.12479018"   // 若 sdkmanager 未装此版本，装之；或去掉此行用默认
}
```
```bash
# 安装 NDK
"$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" "ndk;27.2.12479018"
```
deps 加 `androidTestImplementation(libs.junit)`（libs 里 junit 已存在，直接引用）。

- [ ] **Step 2: Vendor libopus 源码**

```bash
mkdir -p app/src/main/cpp/opus
cd app/src/main/cpp/opus
curl -L -o opus.tar.gz https://downloads.xiph.org/releases/opus/opus-1.5.2.tar.gz
tar xzf opus.tar.gz --strip-components=1
rm opus.tar.gz
```
预期：`app/src/main/cpp/opus/CMakeLists.txt`、`src/`、`include/` 等就位。

- [ ] **Step 3: CMakeLists 与 JNI 包装**

`FT710Android/app/src/main/cpp/CMakeLists.txt`:
```cmake
cmake_minimum_required(VERSION 3.22.1)
project(opus_jni)

add_subdirectory(opus)
target_include_directories(opus PRIVATE src celt silk)

add_library(opus_jni SHARED opus_jni.c)
target_link_libraries(opus_jni opus)
```

`FT710Android/app/src/main/cpp/opus_jni.c`:
```c
#include <jni.h>
#include <opus.h>
#include <stdlib.h>
#include <string.h>

static jlong create_enc(JNIEnv* env, jint rate, jint ch, jint bitrate) {
    int err;
    OpusEncoder* enc = opus_encoder_create(rate, ch, OPUS_APPLICATION_VOIP, &err);
    if (!enc) return 0;
    opus_encoder_ctl(enc, OPUS_SET_BITRATE(bitrate));
    opus_encoder_ctl(enc, OPUS_SET_VBR(0));            /* CBR */
    opus_encoder_ctl(enc, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));
    return (jlong)(intptr_t)enc;
}

static jlong create_dec(JNIEnv* env, jint rate, jint ch) {
    int err;
    OpusDecoder* dec = opus_decoder_create(rate, ch, &err);
    return dec ? (jlong)(intptr_t)dec : 0;
}

JNIEXPORT jlong JNICALL
Java_com_hamradio_ft710android_Audio_OpusBridge_encoderCreate(
    JNIEnv* env, jobject thiz, jint rate, jint ch, jint bitrate) {
    return create_enc(env, rate, ch, bitrate);
}

JNIEXPORT jint JNICALL
Java_com_hamradio_ft710android_Audio_OpusBridge_encoderEncode(
    JNIEnv* env, jobject thiz, jlong handle, jshortArray pcm, jbyteArray out) {
    OpusEncoder* enc = (OpusEncoder*)(intptr_t)handle;
    jsize inLen = (*env)->GetArrayLength(env, pcm);
    jsize outLen = (*env)->GetArrayLength(env, out);
    jshort* in = (*env)->GetShortArrayElements(env, pcm, NULL);
    jbyte* ob = (*env)->GetByteArrayElements(env, out, NULL);
    int n = opus_encode(enc, (const opus_int16*)in, (int)(inLen / 2), (unsigned char*)ob, (opus_int32)outLen);
    (*env)->ReleaseShortArrayElements(env, pcm, in, JNI_ABORT);
    (*env)->ReleaseByteArrayElements(env, out, ob, 0);
    return n;
}

JNIEXPORT jlong JNICALL
Java_com_hamradio_ft710android_Audio_OpusBridge_decoderCreate(
    JNIEnv* env, jobject thiz, jint rate, jint ch) {
    return create_dec(env, rate, ch);
}

JNIEXPORT jint JNICALL
Java_com_hamradio_ft710android_Audio_OpusBridge_decoderDecode(
    JNIEnv* env, jobject thiz, jlong handle, jbyteArray opus, jint len, jshortArray pcm) {
    OpusDecoder* dec = (OpusDecoder*)(intptr_t)handle;
    jsize outCap = (*env)->GetArrayLength(env, pcm);
    jbyte* in = (*env)->GetByteArrayElements(env, opus, NULL);
    jshort* ob = (*env)->GetShortArrayElements(env, pcm, NULL);
    int n = opus_decode(dec, (const unsigned char*)in, (opus_int32)len,
                        (opus_int16*)ob, (int)(outCap / 2), 0);
    (*env)->ReleaseByteArrayElements(env, opus, in, JNI_ABORT);
    (*env)->ReleaseShortArrayElements(env, pcm, ob, 0);
    return n; /* 解码样本数，负值=错误 */
}

JNIEXPORT void JNICALL
Java_com_hamradio_ft710android_Audio_OpusBridge_destroy(
    JNIEnv* env, jobject thiz, jlong handle) {
    if (handle) opus_decoder_destroy((OpusDecoder*)(intptr_t)handle);
}
```

`FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/OpusBridge.kt`:
```kotlin
package com.hamradio.ft710android.Audio

object OpusBridge {
    const val SAMPLE_RATE = 48000
    const val CHANNELS = 1
    const val FRAME_SAMPLES = 960      // 48k * 20ms
    const val BITRATE = 64000          // CBR

    init { System.loadLibrary("opus_jni") }

    external fun encoderCreate(sampleRate: Int, channels: Int, bitrate: Int): Long
    external fun encoderEncode(handle: Long, pcm: ShortArray, out: ByteArray): Int
    external fun decoderCreate(sampleRate: Int, channels: Int): Long
    external fun decoderDecode(handle: Long, opus: ByteArray, len: Int, pcm: ShortArray): Int
    external fun destroy(handle: Long)
}
```

- [ ] **Step 4: 写仪器测试（真机/模拟器验证 round-trip）**

`FT710Android/app/src/androidTest/java/com/hamradio/ft710android/Audio/OpusBridgeTest.kt`:
```kotlin
package com.hamradio.ft710android.Audio

import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class OpusBridgeTest {
    @Test fun `encode decode round trip on silence`() {
        val enc = OpusBridge.encoderCreate(OpusBridge.SAMPLE_RATE, OpusBridge.CHANNELS, OpusBridge.BITRATE)
        val dec = OpusBridge.decoderCreate(OpusBridge.SAMPLE_RATE, OpusBridge.CHANNELS)
        val pcm = ShortArray(OpusBridge.FRAME_SAMPLES) // 静音
        val out = ByteArray(4096)
        val n = OpusBridge.encoderEncode(enc, pcm, out)
        assertTrue(n > 0)
        val back = ShortArray(OpusBridge.FRAME_SAMPLES)
        val s = OpusBridge.decoderDecode(dec, out.copyOf(n), n, back)
        assertEquals(OpusBridge.FRAME_SAMPLES, s)
        OpusBridge.destroy(enc); OpusBridge.destroy(dec)
    }
}
```
（`androidTestImplementation(libs.junit)` + `androidx.test:runner`；instrumented test 仅在有设备/模拟器时运行，作为 CI 可选步骤，`assembleDebug` 不依赖它。）

- [ ] **Step 5: 构建验证**

```bash
cd FT710Android && ./gradlew assembleDebug
```
预期：BUILD SUCCESSFUL，`.so` 打入 APK（`app/build/intermediates/merged_native_libs/debug/.../libopus.so`）。

- [ ] **Step 6: Commit**

```bash
git add FT710Android/app/src/main/cpp/ FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/OpusBridge.kt FT710Android/app/src/androidTest/ FT710Android/app/build.gradle.kts
git commit -m "feat(android): build libopus via NDK with JNI bridge

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 11: RxAudioPlayer（AudioTrack + 抖动缓冲）

**Files:**
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/RxAudioPlayer.kt`

**Interfaces:**
- Consumes: `OpusBridge`（解码）、解码后 `ShortArray`（960 样本）。
- Produces:
```kotlin
class RxAudioPlayer {
    val rms: Float                     // 0..1，UI 仪表
    fun setFrameSource(source: suspend (FrameHandler) -> Unit)  // 由 MainViewModel 把 WS 帧流接进来
    fun start()
    fun stop()
}
interface FrameHandler { fun onPcm(pcm: ShortArray) }
```
  内部：`AudioTrack`（48000Hz、CHANNEL_OUT_MONO、ENCODING_PCM_16BIT、STREAM_MUSIC、低延迟缓冲）；`ArrayDeque<ShortArray>` 抖动缓冲，目标 ~180ms 预缓冲；专用 `HandlerThread` 消费；缓冲不足静音填充。tag `0x01` Opus 帧先经 `OpusBridge.decoderDecode`，tag `0x00` 直通。

- [ ] **Step 1: 实现 RxAudioPlayer**

`FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/RxAudioPlayer.kt`:
```kotlin
package com.hamradio.ft710android.Audio

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.launch
import java.util.ArrayDeque
import kotlin.math.max
import kotlin.math.min

/** RX 播放：Opus/PCM 帧 → AudioTrack，带抖动缓冲与静音填充。 */
class RxAudioPlayer {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val decoder = OpusBridge.decoderCreate(OpusBridge.SAMPLE_RATE, OpusBridge.CHANNELS)
    private val jitter = ArrayDeque<ShortArray>()
    private val targetBufferedMs = 180
    private val framesPerTarget = max(1, targetBufferedMs / 20)

    @Volatile private var running = false
    @Volatile var rms = 0f; private set

    private var track: AudioTrack? = null

    fun start() {
        running = true
        track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
            .setAudioFormat(AudioFormat.Builder()
                .setSampleRate(OpusBridge.SAMPLE_RATE)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
            .setTransferMode(AudioTrack.MODE_STREAM)
            .setBufferSizeInBytes(OpusBridge.FRAME_SAMPLES * 2 * framesPerTarget)
            .build()
        track?.play()
    }

    /** WS 帧入口：1B tag + payload。tag 0x01 Opus 解码，0x00 PCM 直通。 */
    fun onFrame(frame: ByteArray) {
        if (!running) return
        val tag = frame[0].toInt() and 0xFF
        val payload = frame.copyOfRange(1, frame.size)
        val pcm = ShortArray(OpusBridge.FRAME_SAMPLES)
        val samples = when (tag) {
            0x01 -> OpusBridge.decoderDecode(decoder, payload, payload.size, pcm)
            0x00 -> { for (i in 0 until min(pcm.size, payload.size / 2)) pcm[i] = ((payload[i*2+1].toInt() shl 8) or (payload[i*2].toInt() and 0xFF)).toShort(); min(pcm.size, payload.size / 2) }
            else -> 0
        }
        if (samples > 0) synchronized(jitter) { jitter.addLast(pcm.copyOf(samples)) }
    }

    /** 播放循环：满预缓冲后连续写 AudioTrack；不足时静音填充。 */
    private fun playLoop() {
        val silence = ShortArray(OpusBridge.FRAME_SAMPLES)
        while (running) {
            val buf: ShortArray = synchronized(jitter) {
                if (jitter.size >= framesPerTarget) jitter.removeFirst()
                else if (jitter.isNotEmpty()) jitter.removeFirst()
                else silence
            }
            track?.write(buf, 0, buf.size)
            var sum = 0L
            for (s in buf) sum += s.toLong() * s
            rms = kotlin.math.sqrt((sum / buf.size).toDouble() / 32768.0 / 32768.0).toFloat()
        }
    }

    fun stop() {
        running = false
        synchronized(jitter) { jitter.clear() }
        track?.pause(); track?.flush(); track?.release()
        track = null
    }

    fun release() { stop(); OpusBridge.destroy(decoder); scope.cancel() }
}
```
> 注：`playLoop()` 需在专用 `HandlerThread` 上启动（`start()` 里 `scope.launch(Dispatchers.IO) { playLoop() }`——`Dispatchers.IO` 每帧 `write` 为阻塞 I/O，可行；或自建 HandlerThread。实现时二选一，`stop()` 置 `running=false` 使循环退出）。

- [ ] **Step 2: 编译验证**

```bash
cd FT710Android && ./gradlew assembleDebug
```
预期：BUILD SUCCESSFUL。

- [ ] **Step 3: Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/RxAudioPlayer.kt
git commit -m "feat(android): add RX AudioTrack player with jitter buffer

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 12: TxAudioCapture（AudioRecord → 帧 → Opus 编码）

**Files:**
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/TxAudioCapture.kt`

**Interfaces:**
- Consumes: `OpusBridge`（编码）、`ConnectionManager.sendTxAudioBinary`。
- Produces:
```kotlin
class TxAudioCapture(
    private val sendFrame: (ByteArray) -> Unit,   // 组装好的 [tag 0x01 + opus]
) {
    fun start()   // 打开 AudioRecord 48k 单声道；960 样本/块
    fun stop()
    var onError: ((String) -> Unit)? = null
}
```

- [ ] **Step 1: 实现 TxAudioCapture**

`FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/TxAudioCapture.kt`:
```kotlin
package com.hamradio.ft710android.Audio

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/** TX 采集：AudioRecord 48k 单声道 → 960 样本块 → Opus 编码 → [0x01+payload] 回调。 */
class TxAudioCapture(
    private val context: Context,
    private val sendFrame: (ByteArray) -> Unit,
) {
    private val encoder = OpusBridge.encoderCreate(OpusBridge.SAMPLE_RATE, OpusBridge.CHANNELS, OpusBridge.BITRATE)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var job: Job? = null
    private var record: AudioRecord? = null
    var onError: ((String) -> Unit)? = null

    fun start() {
        if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            onError?.invoke("Missing RECORD_AUDIO permission"); return
        }
        val buf = ShortArray(OpusBridge.FRAME_SAMPLES)
        val minBuf = AudioRecord.getMinBufferSize(
            OpusBridge.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = AudioRecord(
            MediaRecorder.AudioSource.VOICE_COMMUNICATION,
            OpusBridge.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
            maxOf(minBuf, OpusBridge.FRAME_SAMPLES * 2 * 4))
        record = rec
        rec.startRecording()
        job = scope.launch {
            val out = ByteArray(4096)
            while (isActive) {
                val n = rec.read(buf, 0, buf.size)
                if (n <= 0) continue
                val written = OpusBridge.encoderEncode(encoder, buf.copyOf(n), out)
                if (written > 0) sendFrame(byteArrayOf(0x01) + out.copyOf(written))
            }
        }
    }

    fun stop() {
        job?.cancel()
        runCatching { record?.stop() }
        record?.release()
        record = null
    }

    fun release() { stop(); OpusBridge.destroy(encoder); scope.cancel() }
}
```
> 说明：`VOICE_COMMUNICATION` 保证 48k 采集概率最高；若某设备只给 44.1k，`AudioRecord` 会用最近支持速率——本任务 v1 采 48k 直通（服务端 TX 是 48k 域），设备异常时的重采样（44.1→48）标为后续增强，代码留 `TODO(remap)` 注释即可。

- [ ] **Step 2: 编译验证**

```bash
cd FT710Android && ./gradlew assembleDebug
```

- [ ] **Step 3: Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Audio/TxAudioCapture.kt
git commit -m "feat(android): add TX audio capture and Opus encode

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 13: SpectrumProcessor + 瀑布/FFT 渲染

**Files:**
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/Spectrum/SpectrumProcessor.kt`
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/Spectrum/WaterfallCanvas.kt`
- Test: `FT710Android/app/src/test/java/com/hamradio/ft710android/Spectrum/SpectrumProcessorTest.kt`

**Interfaces:**
- Consumes: `parseSpectrumFrame`（Task 5）。
- Produces:
```kotlin
class SpectrumProcessor {
    val waterfall: List<IntArray> get()   // 最新 ≤120 行，每行 850 点
    val fft: IntArray get()               // 最新一行（FFT 折线）
    fun onFrame(frame: ByteArray)         // 解析 + 入环
}
```
`WaterfallCanvas`：Compose `Canvas`，把 `waterfall` 画成瀑布（颜色映射：深蓝→青→黄→红，对齐 Web 的 Jet colormap），`fft` 画顶部折线；点击位置 → 频率换算回调（点击才 QSY）。

- [ ] **Step 1: 写失败测试**

`FT710Android/app/src/test/java/com/hamradio/ft710android/Spectrum/SpectrumProcessorTest.kt`:
```kotlin
package com.hamradio.ft710android.Spectrum

import org.junit.Assert.assertEquals
import org.junit.Test

class SpectrumProcessorTest {
    @Test fun `feeds rows and keeps ring buffer at 120`() {
        val sp = SpectrumProcessor()
        for (k in 0 until 200) {
            val f = ByteArray(1701).also { it[0] = 0x01; for (i in 1..850) it[i] = k.toByte() }
            sp.onFrame(f)
        }
        assertEquals(120, sp.waterfall.size)
        assertEquals(850, sp.fft.size)
    }

    @Test fun `invalid frame is ignored`() {
        val sp = SpectrumProcessor()
        sp.onFrame(ByteArray(100))
        assertEquals(0, sp.waterfall.size)
    }
}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*SpectrumProcessorTest*'
```

- [ ] **Step 3: 实现 SpectrumProcessor 与 WaterfallCanvas**

`FT710Android/app/src/main/java/com/hamradio/ft710android/Spectrum/SpectrumProcessor.kt`:
```kotlin
package com.hamradio.ft710android.Spectrum

import java.util.ArrayDeque

/** 频谱行缓冲：解析 1701B 帧 → 850 点瀑布环（120 行）。 */
class SpectrumProcessor {
    private val rows = ArrayDeque<IntArray>()
    @Volatile private var latest: IntArray = IntArray(850)

    val waterfall: List<IntArray> get() = synchronized(rows) { rows.toList() }
    val fft: IntArray get() = latest

    fun onFrame(frame: ByteArray) {
        val sf = parseSpectrumFrame(frame) ?: return
        synchronized(rows) {
            rows.addLast(sf.wf1)
            if (rows.size > MAX_ROWS) rows.removeFirst()
        }
        latest = sf.wf1
    }

    companion object { const val MAX_ROWS = 120 }
}
```

`FT710Android/app/src/main/java/com/hamradio/ft710android/Spectrum/WaterfallCanvas.kt`:
```kotlin
package com.hamradio.ft710android.Spectrum

import androidx.compose.foundation.Canvas
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke

/** 瀑布（每行 850 点，颜色映射对齐 Web Jet colormap）+ 顶部 FFT 折线。 */
@Composable
fun WaterfallCanvas(
    rows: List<IntArray>,
    fft: IntArray,
    spanHz: Long,
    startFreqHz: Long,
    onTapFreq: (Long) -> Unit,
    modifier: Modifier = Modifier,
) {
    Canvas(modifier) {
        if (rows.isEmpty()) return@Canvas
        val cellH = size.height / rows.size
        for (r in rows.indices) {
            val row = rows[r]
            for (x in row.indices) {
                val v = row[x]
                val color = jetColor(v / 255f)
                drawRect(color, topLeft = Offset(x.toFloat() / 850 * size.width, r * cellH),
                    size = androidx.compose.ui.geometry.Size(size.width / 850, cellH))
            }
        }
        // FFT 折线
        val path = androidx.compose.ui.graphics.Path()
        for (x in fft.indices) {
            val y = size.height - (fft[x] / 255f) * size.height
            if (x == 0) path.moveTo(x / 850f * size.width, y) else path.lineTo(x / 850f * size.width, y)
        }
        drawPath(path, Color(0xFF06B6D4), style = Stroke(width = 2f))
    }
}

/** 对齐 Web Jet colormap：深蓝→青→黄→红。 */
private fun jetColor(t: Float): Color {
    val tt = t.coerceIn(0f, 1f)
    val r = (255f * kotlin.math.max(0f, kotlin.math.min(1f, 1.5f - kotlin.math.abs(4f * tt - 3f)))).toInt()
    val g = (255f * kotlin.math.max(0f, kotlin.math.min(1f, 1.5f - kotlin.math.abs(4f * tt - 2f)))).toInt()
    val b = (255f * kotlin.math.max(0f, kotlin.math.min(1f, 1.5f - kotlin.math.abs(4f * tt - 1f)))).toInt()
    return Color(r, g, b)
}
```
（`onTapFreq` 由外层 `pointerInput` 计算：`x/width * spanHz + startFreqHz` 后回调——Task 16 接入。）

- [ ] **Step 4: 运行测试确认通过**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*SpectrumProcessorTest*'
```

- [ ] **Step 5: Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/Spectrum/ FT710Android/app/src/test/java/com/hamradio/ft710android/Spectrum/SpectrumProcessorTest.kt
git commit -m "feat(android): add spectrum ring buffer and waterfall canvas

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 14: MainViewModel（总协调器）

**Files:**
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/ViewModel/MainViewModel.kt`
- Test: `FT710Android/app/src/test/java/com/hamradio/ft710android/ViewModel/MainViewModelTest.kt`

**Interfaces:**
- Consumes: `ConnectionManager`、`RadioState`、`RxAudioPlayer`、`TxAudioCapture`、`SpectrumProcessor`、`PTTManager`、`AuthApi`、`MemoryChannels`。
- Produces:
```kotlin
class MainViewModel(
    private val authApi: AuthApi,
    private val connectionManager: ConnectionManager,
    private val rxPlayer: RxAudioPlayer,
    private val txCapture: TxAudioCapture,
    private val spectrumProcessor: SpectrumProcessor,
    private val memoryChannels: MemoryChannelsStore,   // Task 4 + 本地快照
    val pttManager: PTTManager,
    private val scope: CoroutineScope,
) {
    val state = RadioState()
    val waterfall: StateFlow<List<IntArray>>
    val fft: StateFlow<IntArray>
    val connected: StateFlow<Boolean>
    val bands: StateFlow<List<String>>
    val modes: StateFlow<List<String>>
    val memChannels: StateFlow<List<MemoryChannel?>>
    val atr1000Enabled: StateFlow<Boolean>
    val error: MutableStateFlow<String?>

    suspend fun connect(host: String, port: String, password: String): AuthResult  // 登录→token→start()
    fun sendSet(field: String, value: Any)
    fun setFrequencyStep(deltaHz: Long)
    fun setMode(mode: String); fun setBand(freqHz: Long); fun cycleFilter()
    fun onPttGesture(); fun onPttRelease()
    fun recallMemory(index: Int) / saveMemory(index: Int) / clearMemory(index: Int)
    fun disconnect() / logout()
}
```
  接线规则：`ConnectionManager.onRadioEvent` → 按 `WsEvent` 分发（FullState→`state.apply`+存 bands/modes/memChannels/filterTables/atr1000Enabled；StateUpdate→`state.apply`+PTT 状态喂 `pttManager.onStatusReceived(txStatus)`；MemChannels→刷新；ErrorEvent→error 流）；`onAudioRx`→`rxPlayer.onFrame`；`onSpectrum`→`spectrumProcessor.onFrame`；`onConnectionChange`→`connected`。`connect()` 失败（401/429/网络）→ 返回 Failure 并保持未连接。

- [ ] **Step 1: 写失败测试**

`FT710Android/app/src/test/java/com/hamradio/ft710android/ViewModel/MainViewModelTest.kt`:
```kotlin
package com.hamradio.ft710android.ViewModel

import com.hamradio.ft710android.Network.ConnectionManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class MainViewModelTest {
    @Test fun `fullState applies to state and exposes bands`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val cm = ConnectionManager(OkHttpClient(), scope, {}, {}, {}, {}, {}, {}, sendOverride = {})
        val vm = MainViewModel(
            authApi = null, connectionManager = cm, rxPlayer = null,
            txCapture = null, spectrumProcessor = null, memoryChannelsStore = null,
            pttManager = null, scope = scope,
        )
        vm.onWsEvent(
            """{"type":"fullState","data":{"vfo_a_freq":7050000,"mode":1},"bands":["20m"],"modes":["USB"],"memChannels":[null,null,null,null,null,null]}"""
        )
        assertEquals(7050000L, vm.state.vfoAFreq)
        assertEquals(listOf("20m"), vm.bands.value)
        assertEquals(1L, vm.version.value) // apply 后版本递增，驱动 Compose 重组
    }

    @Test fun `stateUpdate with tx_status feeds ptt manager`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val cm = ConnectionManager(OkHttpClient(), scope, {}, {}, {}, {}, {}, {}, sendOverride = {})
        var fed = -1
        val ptt = com.hamradio.ft710android.PTT.PTTManager(
            sendPTT = {}, sendTXAudioStop = {}, startTxAudio = {}, stopTxAudio = {},
            serverTXStatus = { 0 }, isCtrlConnected = { true }, onStuckTX = {},
            dispatcher = UnconfinedTestDispatcher(),
        )
        // 用子类覆盖 onStatusReceived 记录喂入值（测试友好）
        val spy = object : com.hamradio.ft710android.PTT.PTTManager(
            sendPTT = {}, sendTXAudioStop = {}, startTxAudio = {}, stopTxAudio = {},
            serverTXStatus = { 0 }, isCtrlConnected = { true }, onStuckTX = {},
            dispatcher = UnconfinedTestDispatcher(),
        ) {
            override fun onStatusReceived(txStatus: Int) { fed = txStatus }
        }
        val vm = MainViewModel(null, cm, null, null, null, null, spy, scope)
        vm.onWsEvent("""{"type":"stateUpdate","fields":{"tx_status":1},"dirty":["tx_status"]}""")
        assertEquals(1, fed)
    }
}
```
> 说明：`onWsEvent(text)` 为公开方法便于测试（内部走 `parseWsEvent` + 分发）。`MainViewModel` 的依赖全部可空/可注入；测试要点是 WsEvent → `RadioState` + StateFlow 分发正确，PTT 回显喂 `onStatusReceived`。

- [ ] **Step 2: 运行测试确认失败**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*MainViewModelTest*'
```

- [ ] **Step 3: 实现 MainViewModel**

`FT710Android/app/src/main/java/com/hamradio/ft710android/ViewModel/MainViewModel.kt`:
```kotlin
package com.hamradio.ft710android.ViewModel

import com.hamradio.ft710android.Data.MemoryChannel
import com.hamradio.ft710android.Data.MemoryChannels
import com.hamradio.ft710android.Data.RadioState
import com.hamradio.ft710android.Network.AuthApi
import com.hamradio.ft710android.Network.AuthResult
import com.hamradio.ft710android.Network.ConnectionManager
import com.hamradio.ft710android.Network.WsEvent
import com.hamradio.ft710android.Network.parseWsEvent
import com.hamradio.ft710android.PTT.PTTManager
import com.hamradio.ft710android.Spectrum.SpectrumProcessor
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/** 总协调器：登录→4 路连接→事件分发→状态/音频/频谱/PTT。普通类，Compose 内 remember 创建。 */
class MainViewModel(
    private val authApi: AuthApi?,
    val connectionManager: ConnectionManager,
    private val rxPlayer: RxPlayerLike?,
    private val txCapture: TxCaptureLike?,
    private val spectrumProcessor: SpectrumProcessor?,
    private val memoryChannelsStore: MemoryStore?,
    val pttManager: PTTManager?,
    private val scope: CoroutineScope,
) {
    val state = RadioState()

    /** 状态版本号：每次 state.apply 后 +1，Compose 读 vm.state.* 并以 version 订阅重组（RadioState 是可变普通类）。 */
    private val _version = MutableStateFlow(0L)
    val version: StateFlow<Long> = _version

    private val _waterfall = MutableStateFlow<List<IntArray>>(emptyList())
    val waterfall: StateFlow<List<IntArray>> = _waterfall
    private val _fft = MutableStateFlow(IntArray(850))
    val fft: StateFlow<IntArray> = _fft
    private val _connected = MutableStateFlow(false)
    val connected: StateFlow<Boolean> = _connected
    private val _bands = MutableStateFlow<List<String>>(emptyList())
    val bands: StateFlow<List<String>> = _bands
    private val _modes = MutableStateFlow<List<String>>(emptyList())
    val modes: StateFlow<List<String>> = _modes
    private val _mem = MutableStateFlow<List<MemoryChannel?>>(emptyList())
    val memChannels: StateFlow<List<MemoryChannel?>> = _mem
    private val _atr = MutableStateFlow(false)
    val atr1000Enabled: StateFlow<Boolean> = _atr
    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error

    fun onWsEvent(text: String) {
        when (val ev = parseWsEvent(text)) {
            is WsEvent.FullState -> {
                state.apply(ev.data.toMap())
                _version.value++
                _bands.value = ev.bands
                _modes.value = ev.modes
                _atr.value = ev.atr1000Enabled
                onMemChannels(ev.memChannels)
            }
            is WsEvent.StateUpdate -> {
                val dirty = state.apply(ev.fields.toMap())
                _version.value++
                if ("tx_status" in dirty) pttManager?.onStatusReceived(state.txStatus)
            }
            is WsEvent.MemChannels -> onMemChannels(ev.channels)
            is WsEvent.ErrorEvent -> _error.value = ev.message
            else -> Unit
        }
    }

    fun onAudioRxFrame(frame: ByteArray) { rxPlayer?.onFrame(frame) }
    fun onSpectrumFrame(frame: ByteArray) { spectrumProcessor?.onFrame(frame) }

    private fun onMemChannels(list: List<kotlinx.serialization.json.JsonElement?>) {
        _mem.value = MemoryChannels.parse(list)
    }

    suspend fun connect(host: String, port: String, password: String): AuthResult {
        val api = authApi ?: return AuthResult.Failure(0, "auth not configured")
        val base = "https://$host:$port"
        val res = api.login(base, password)
        if (res is AuthResult.Success) {
            connectionManager.start(base, res.token)
        }
        return res
    }

    suspend fun logout() {
        connectionManager.stopAll()
        _connected.value = false
    }

    fun sendSet(field: String, value: Any) = connectionManager.sendSet(field, value)

    fun setFrequencyStep(deltaHz: Long) { sendSet("freq", state.activeFrequency + deltaHz) }

    fun setMode(mode: String) = sendSet("mode", mode)
    fun setBand(freqHz: Long) = sendSet("freq", freqHz)
    fun cycleFilter() = sendSet("filter", (state.filterWidth + 1) % 23)

    fun onPttGesture() { pttManager?.press() }
    fun onPttRelease() { pttManager?.forceRelease() }

    fun recallMemory(index: Int) {
        val c = _mem.value.getOrNull(index) ?: return
        if (c == null) return
        sendSet("freq", c.freq); sendSet("mode", c.mode)
    }

    fun saveMemory(index: Int) {
        val list = _mem.value.toMutableList()
        while (list.size < 6) list.add(null)
        list[index] = MemoryChannel(state.activeFrequency, state.modeName, "M${index + 1}")
        _mem.value = list
        connectionManager.sendMemSave(MemoryChannels.toJson(list))
    }

    fun clearMemory(index: Int) {
        val list = _mem.value.toMutableList()
        if (index in list.indices) list[index] = null
        _mem.value = list
        connectionManager.sendMemSave(MemoryChannels.toJson(list))
    }

    fun disconnect() { connectionManager.stopAll(); _connected.value = false }

    fun setScopeSpan(span: Int) = sendSet("scope_span", span)
    fun setRfPower(w: Int) = sendSet("rf_power", w)

    // 轻量接口，便于测试注入与对音频/频谱的强类型
    interface RxPlayerLike { fun onFrame(frame: ByteArray) }
    interface TxCaptureLike { fun start(); fun stop() }
    interface MemoryStore
}
```
> 说明：`onPttRelease()` 调 `forceRelease()`（安全兜底，幂等），PTT 手势松手仍走 `PTTManager.release()`（Task 16 `PTTButton` 直接持有 manager）。`ConnectionManager` 回调在 Task 17 装配时接到 `vm.onWsEvent` / `vm.onAudioRxFrame` / `vm.onSpectrumFrame` / `_connected`。`RxPlayerLike`/`TxCaptureLike`/`MemoryStore` 允许传 null 或轻量 Fake。

- [ ] **Step 4: 运行测试确认通过**

```bash
cd FT710Android && ./gradlew testDebugUnitTest --tests '*MainViewModelTest*'
```

- [ ] **Step 5: Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/ViewModel/MainViewModel.kt FT710Android/app/src/test/java/com/hamradio/ft710android/ViewModel/MainViewModelTest.kt
git commit -m "feat(android): add MainViewModel coordinator

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 15: LoginScreen（登录 + Keystore + 自动登录）

**Files:**
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/UI/LoginScreen.kt`
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/Data/SettingsStore.kt`

**Interfaces:**
- Consumes: `AuthApi`、`MainViewModel.connect`。
- Produces: `@Composable fun LoginScreen(vm: MainViewModel, onLoggedIn: () -> Unit)`；`class SettingsStore(context)` 用 DataStore 存 `host`/`port`，凭据用 Keystore 加密后落 DataStore（简单方案：`EncryptedSharedPreferences` 若可用，否则 Keystore 包装）。

- [ ] **Step 1: 实现 SettingsStore（DataStore + 加密凭据）**

`FT710Android/app/src/main/java/com/hamradio/ft710android/Data/SettingsStore.kt`:
```kotlin
package com.hamradio.ft710android.Data

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "settings")

/** host/port 存 DataStore；password/token 用 Keystore 加密（AndroidKeyStore AES-GCM），密文存 DataStore。 */
class SettingsStore(private val context: Context) {
    private object Keys { val host = stringPreferencesKey("host"); val port = stringPreferencesKey("port") }
    private val ks = KeystoreCipher(context)

    val host: Flow<String> = context.dataStore.data.map { it[Keys.host] ?: "radio.vlsc.net" }
    val port: Flow<String> = context.dataStore.data.map { it[Keys.port] ?: "8888" }

    suspend fun save(host: String, port: String, password: String) {
        context.dataStore.edit { it[Keys.host] = host; it[Keys.port] = port }
        ks.putSecret("password", password)
    }

    suspend fun savedPassword(): String? = ks.getSecret("password")

    /** 退出登录时清除加密凭据（不删 host/port）。 */
    suspend fun clearCredentials() { ks.deleteSecret("password") }

    companion object { const val DEFAULT_HOST = "radio.vlsc.net"; const val DEFAULT_PORT = "8888" }
}
```
> 注：`KeystoreCipher`（AES-GCM，AndroidKeyStore）本任务内实现（`Encrypt/Decrypt`，key alias `ft710_creds`）；也可用 androidx `security-crypto` 的 `EncryptedSharedPreferences`。实现二选一，保证凭据不落明文。

- [ ] **Step 2: 实现 LoginScreen**

`FT710Android/app/src/main/java/com/hamradio/ft710android/UI/LoginScreen.kt`:
```kotlin
package com.hamradio.ft710android.UI

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.hamradio.ft710android.Data.SettingsStore
import com.hamradio.ft710android.Network.AuthResult
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.launch

@Composable
fun LoginScreen(vm: MainViewModel, settings: SettingsStore, onLoggedIn: () -> Unit) {
    val scope = rememberCoroutineScope()
    var host by remember { mutableStateOf(SettingsStore.DEFAULT_HOST) }
    var port by remember { mutableStateOf(SettingsStore.DEFAULT_PORT) }
    var password by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var initialized by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        settings.host.collect { host = it }
        settings.port.collect { port = it }
        val saved = settings.savedPassword()
        if (!saved.isNullOrEmpty()) password = saved
        initialized = true
    }

    Column(Modifier.fillMaxSize().padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Spacer(Modifier.height(80.dp))
        Text("FT-710 Control", style = MaterialTheme.typography.headlineMedium)
        Text("服务器证书未验证（自签）", style = MaterialTheme.typography.bodySmall)
        Spacer(Modifier.height(24.dp))
        OutlinedTextField(host, { host = it }, label = { Text("服务器") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(port, { port = it }, label = { Text("端口") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(password, { password = it }, label = { Text("密码") }, singleLine = true,
            visualTransformation = androidx.compose.ui.text.input.PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth())
        Spacer(Modifier.height(24.dp))
        Button(onClick = {
            if (busy || !initialized) return@Button
            busy = true; error = null
            scope.launch {
                val res = vm.connect(host.trim(), port.trim(), password)
                when (res) {
                    is AuthResult.Success -> { settings.save(host.trim(), port.trim(), password); onLoggedIn() }
                    is AuthResult.Failure -> {
                        error = when (res.status) {
                            401 -> "密码错误"
                            429 -> "尝试过于频繁，请稍后再试"
                            else -> "连接失败：${res.message.take(80)}"
                        }
                        busy = false
                    }
                }
            }
        }, enabled = !busy, modifier = Modifier.fillMaxWidth()) {
            Text(if (busy) "连接中…" else "连接")
        }
        error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
    }
}
```

- [ ] **Step 3: 编译验证**

```bash
cd FT710Android && ./gradlew assembleDebug
```

- [ ] **Step 4: Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/UI/LoginScreen.kt FT710Android/app/src/main/java/com/hamradio/ft710android/Data/SettingsStore.kt
git commit -m "feat(android): add login screen with keystore auto-login

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 16: MainScreen UI（主控界面）

**Files:**
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/UI/MainScreen.kt`
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/UI/PTTButton.kt`

**Interfaces:**
- Consumes: `MainViewModel`（state/bands/modes/waterfall/fft/connected/memChannels）、`WaterfallCanvas`（Task 13）。
- Produces: `@Composable fun MainScreen(vm: MainViewModel)`。

- [ ] **Step 1: 实现 PTTButton（手势 finally 兜底）**

`FT710Android/app/src/main/java/com/hamradio/ft710android/UI/PTTButton.kt`:
```kotlin
package com.hamradio.ft710android.UI

import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.unit.dp
import com.hamradio.ft710android.PTT.PTTManager

/** 触按保持 PTT。finally 保证手势被系统取消时也一定 release（修掉 iOS onEnded 竞态）。 */
@Composable
fun PTTButton(manager: PTTManager, modifier: Modifier = Modifier) {
    val isTX = manager.isTX
    Box(
        modifier = modifier
            .background(if (isTX) Color(0xFFDC2626) else Color(0xFF16A34A), RoundedCornerShape(16.dp))
            .pointerInput(Unit) {
                detectTapGestures(onPress = {
                    manager.press()
                    try { tryAwaitRelease() } finally { manager.release() }
                })
            },
        contentAlignment = Alignment.Center,
    ) {
        Text(if (isTX) "发射" else "PTT", color = Color.White, style = MaterialTheme.typography.titleLarge)
    }
}
```

- [ ] **Step 2: 实现 MainScreen 布局（对齐 spec §4）**

`FT710Android/app/src/main/java/com/hamradio/ft710android/UI/MainScreen.kt`:
```kotlin
package com.hamradio.ft710android.UI

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.itemsIndexed
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.collectAsState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.hamradio.ft710android.Spectrum.WaterfallCanvas
import com.hamradio.ft710android.ViewModel.MainViewModel
import java.util.Locale

@Composable
fun MainScreen(vm: MainViewModel) {
    // RadioState 是可变的普通类：订阅 version 版本号触发重组，随后读 vm.state.* 即拿到最新值
    vm.version.collectAsState()
    val state = vm.state
    val bands by vm.bands.collectAsState()
    val modes by vm.modes.collectAsState()
    val waterfall by vm.waterfall.collectAsState()
    val fft by vm.fft.collectAsState()
    val connected by vm.connected.collectAsState()
    val mem by vm.memChannels.collectAsState()
    val atr by vm.atr1000Enabled.collectAsState()
    val scopeSpanHz = when (state.scopeSpan) { 0 -> 100000L; 1 -> 1000000L; 2 -> 50000L; else -> 100000L }

    Column(Modifier.fillMaxSize().padding(8.dp)) {
        // 顶栏：连接点 + 频率
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            ConnDot(connected, "CTRL")
            Spacer(Modifier.weight(1f))
            Text(formatFreq(state.activeFrequency), fontFamily = FontFamily.Monospace, fontSize = 40.sp)
            Spacer(Modifier.weight(1f))
            Text("${state.modeName} ${state.bandName}", fontSize = 12.sp)
        }
        // VFO A/B
        Row {
            listOf("A", "B").forEach { v ->
                FilterChip(selected = state.activeVfo == v, onClick = { vm.sendSet("vfo", v) },
                    label = { Text(v) })
                Spacer(Modifier.width(4.dp))
            }
            Spacer(Modifier.weight(1f))
            TextButton(onClick = { vm.sendSet("freq", state.activeFrequency + 1000) }) { Text("+1k") }
            TextButton(onClick = { vm.sendSet("freq", state.activeFrequency - 1000) }) { Text("-1k") }
        }
        // 瀑布 + FFT（点击才 QSY）
        WaterfallCanvas(rows = waterfall, fft = fft, spanHz = scopeSpanHz,
            startFreqHz = state.scopeStartFreq,
            onTapFreq = { vm.sendSet("freq", it) },
            modifier = Modifier.fillMaxWidth().height(180.dp))
        // 模式/波段/滤波行
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.fillMaxWidth()) {
            TextButton(onClick = { vm.sendSet("mode", modes.getOrElse(1) { "USB" }) }) { Text("模式") }
            TextButton(onClick = { vm.cycleFilter() }) { Text("滤波 ${state.filterHz}") }
            TextButton(onClick = { vm.sendSet("att", (state.attenuator + 1) % 4) }) { Text("ATT ${state.attenuatorLabel}") }
            TextButton(onClick = { vm.sendSet("preamp", (state.preamp + 1) % 3) }) { Text("PRE ${state.preampLabel}") }
        }
        // DSP 开关
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            DspChip(state.noiseReduction, { vm.sendSet("nr", it) }, "NR")
            DspChip(state.noiseBlanker, { vm.sendSet("nb", it) }, "NB")
            DspChip(state.autoNotch, { vm.sendSet("an", it) }, "AN")
            DspChip(state.compressor, { vm.sendSet("comp", it) }, "COMP")
        }
        // 仪表：S + PWR/SWR/ALC
        SMeterBar(state.sMeter, state.sMeterDbm)
        MeterRow(label = "PWR", value = state.powerWatts, max = 100f)
        MeterRow(label = "SWR", value = state.swrRatio, max = 3f)
        MeterRow(label = "ALC", value = state.alcPct, max = 100f)
        // 记忆频道 6 槽
        LazyVerticalGrid(GridCells.Fixed(3), modifier = Modifier.weight(1f)) {
            itemsIndexed(mem) { i, c ->
                MemCell(i, c, onClick = { vm.recallMemory(i) },
                    onLong = { vm.saveMemory(i) }, onClear = { vm.clearMemory(i) })
            }
        }
        // 底部：TUNE + PTT
        Row(Modifier.fillMaxWidth().height(72.dp), verticalAlignment = Alignment.CenterVertically) {
            Button(onClick = {
                val on = state.tunerStatus == 0
                vm.sendSet("tune", on)
            }, modifier = Modifier.weight(0.4f).fillMaxHeight()) { Text("TUNE") }
            Spacer(Modifier.width(8.dp))
            PTTButton(vm.pttManager!!, Modifier.weight(0.6f).fillMaxHeight())
        }
    }
}

@Composable private fun ConnDot(on: Boolean, label: String) {
    Box(Modifier.size(8.dp).background(if (on) androidx.compose.ui.graphics.Color(0xFF22C55E) else androidx.compose.ui.graphics.Color(0xFF6B7280)))
    Text(" $label", fontSize = 10.sp)
}

@Composable private fun DspChip(on: Boolean, onToggle: (Boolean) -> Unit, label: String) {
    FilterChip(selected = on, onClick = { onToggle(!on) }, label = { Text(label) })
}

@Composable private fun SMeterBar(sMeter: Int, dbm: Double) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text("S", fontSize = 12.sp)
        LinearProgressIndicator(progress = (sMeter / 32f).coerceIn(0f, 1f), modifier = Modifier.weight(1f).height(10.dp))
        Text("%.1f dBm".format(Locale.US, dbm), fontSize = 11.sp)
    }
}

@Composable private fun MeterRow(label: String, value: Double, max: Float) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(label, fontSize = 11.sp, modifier = Modifier.width(40.dp))
        LinearProgressIndicator(progress = (value.toFloat() / max).coerceIn(0f, 1f), modifier = Modifier.weight(1f).height(8.dp))
        Text("%.1f".format(Locale.US, value), fontSize = 11.sp, modifier = Modifier.width(48.dp), textAlign = androidx.compose.ui.text.style.TextAlign.End)
    }
}

@Composable private fun MemCell(index: Int, c: com.hamradio.ft710android.Data.MemoryChannel?, onClick: () -> Unit, onLong: () -> Unit, onClear: () -> Unit) {
    // 点选调出；长按保存当前；滑动/点 X 清除。v1 简化为点选 + 长按保存 + 双击清除。
    val text = c?.let { it.label } ?: "空"
    Box(Modifier.padding(2.dp).fillMaxWidth().clickable { onClick() }
        .combinedClickable(onClick = onClick, onLongClick = onLong),
        contentAlignment = Alignment.Center) {
        Text(if (c == null) "空" else c.label, fontSize = 11.sp)
    }
}

private fun formatFreq(hz: Long): String = "%,d".format(Locale.US, hz).replace(',', ' ') + " Hz"
```

> 说明：`combinedClickable` 需 `ExperimentalFoundationApi` 或直接 `Modifier.clickable` + `pointerInput` 长按；实现时按编译提示调整。`MemCell` 的"双击清除"在 v1 可简化为长按保存、点选调出即可（清除可放到 Task 18 设置页）。

- [ ] **Step 3: 编译验证**

```bash
cd FT710Android && ./gradlew assembleDebug
```
预期：BUILD SUCCESSFUL（若有 `ExperimentalFoundationApi` 需要，在 `MainScreen.kt` 顶部加 `@OptIn(androidx.compose.foundation.ExperimentalFoundationApi::class)`）。

- [ ] **Step 4: Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/UI/MainScreen.kt FT710Android/app/src/main/java/com/hamradio/ft710android/UI/PTTButton.kt
git commit -m "feat(android): add main control screen

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 17: TLS / 网络配置 / 音频焦点 / 亮屏 / 生命周期兜底

**Files:**
- Create: `FT710Android/app/src/main/res/xml/network_security_config.xml`
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/App/AppSetup.kt`
- Modify: `FT710Android/app/src/main/AndroidManifest.xml`（网络配置引用 + `FLAG_KEEP_SCREEN_ON`）
- Modify: `FT710Android/app/src/main/java/com/hamradio/ft710android/App/MainActivity.kt`（onStop → forceRelease）

**Interfaces:**
- Consumes: `PTTManager.forceRelease`、`MainViewModel`、`RxAudioPlayer`。
- Produces: `@Composable fun AppSetup(vm: MainViewModel)` 内部用 `DisposableEffect` 处理音频焦点/亮屏/生命周期；`MainActivity.onStop` 调 `vm.onPttRelease()`（兜底 forceRelease）。

- [ ] **Step 1: 网络配置 + Manifest**

`FT710Android/app/src/main/res/xml/network_security_config.xml`:
```xml
<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
    <base-config cleartextTrafficPermitted="false" />
    <!-- 调试明文 HTTP/WS（--no-ssl 服务端）仅限用户手动添加域；生产默认拒绝明文 -->
</network-security-config>
```
`AndroidManifest.xml` 的 `<application>` 加：
```xml
android:networkSecurityConfig="@xml/network_security_config"
```
（真机调试明文时在 `<debug-overrides>` 或用户设置里加域；自签 https 走 Task 7 的 `selfSignedOkHttpClient`，与网络配置无关。）

- [ ] **Step 2: AppSetup（音频焦点 + 亮屏）**

`FT710Android/app/src/main/java/com/hamradio/ft710android/App/AppSetup.kt`:
```kotlin
package com.hamradio.ft710android.App

import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.view.WindowManager
import androidx.compose.runtime.*
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.activity.compose.LocalActivity

/** 主界面装配：RX 音频焦点（切走暂停）、保持亮屏、退出时释放 TX。 */
@Composable
fun AppSetup(rxRunning: Boolean, onTxRelease: () -> Unit) {
    val context = LocalContext.current
    val activity = LocalActivity.current
    val view = LocalView.current

    // 亮屏
    DisposableEffect(Unit) {
        val w = view.keepScreenOn
        view.keepScreenOn = true
        onDispose { view.keepScreenOn = w }
    }
    // 音频焦点
    val am = remember { context.getSystemService(AudioManager::class.java) }
    val focusReq = remember {
        AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
            .build()
    }
    DisposableEffect(rxRunning) {
        if (rxRunning) am.requestAudioFocus(focusReq) else am.abandonAudioFocusRequest(focusReq)
        onDispose { am.abandonAudioFocusRequest(focusReq) }
    }
    // 生命周期：退后台 → 强制释放 TX
    DisposableEffect(Unit) {
        val obs = object : androidx.lifecycle.DefaultLifecycleObserver {
            override fun onStop(owner: androidx.lifecycle.LifecycleOwner) { onTxRelease() }
        }
        androidx.lifecycle.LifecycleOwner::class
        androidx.compose.ui.platform.LocalLifecycleOwner.current.lifecycle.addObserver(obs)
        onDispose { androidx.compose.ui.platform.LocalLifecycleOwner.current.lifecycle.removeObserver(obs) }
    }
}
```

- [ ] **Step 3: MainActivity 装配 + onStop 兜底**

新建 `FT710Android/app/src/main/java/com/hamradio/ft710android/App/ServiceLocator.kt`（v1 无 DI 框架的最小装配点）：
```kotlin
package com.hamradio.ft710android.App

import com.hamradio.ft710android.Audio.RxAudioPlayer
import com.hamradio.ft710android.Audio.TxAudioCapture
import com.hamradio.ft710android.Network.AuthApi
import com.hamradio.ft710android.Network.ConnectionManager
import com.hamradio.ft710android.PTT.PTTManager
import com.hamradio.ft710android.Spectrum.SpectrumProcessor
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

/** 单例装配：构造依赖闭环（ConnectionManager 回调 → MainViewModel，MainViewModel 又持有 ConnectionManager）。 */
object ServiceLocator {
    lateinit var authApi: AuthApi
    lateinit var vmFactory: () -> MainViewModel

    fun assemble() {
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
        val client = AuthApi.selfSignedOkHttpClient()
        authApi = AuthApi(client)

        lateinit var vm: MainViewModel
        val rx = RxAudioPlayer()
        val tx = TxAudioCapture(FT710App.instance, { /* sendTxAudioBinary */ })
        val spectrum = SpectrumProcessor()
        var connected = false

        val cm = ConnectionManager(
            client, scope,
            onRadioEvent = { vm.onWsEvent(it) },
            onAudioRx = { vm.onAudioRxFrame(it) },
            onSpectrum = { vm.onSpectrumFrame(it) },
            onAudioTxText = {},
            onAtrEvent = {},
            onConnectionChange = { connected = it },
        )
        vm = MainViewModel(
            authApi = authApi,
            connectionManager = cm,
            rxPlayer = rx,
            txCapture = tx,
            spectrumProcessor = spectrum,
            memoryChannelsStore = null,
            pttManager = PTTManager(
                sendPTT = { on -> cm.sendSet("ptt", on) },
                sendTXAudioStop = { cm.sendTxAudioText("s:") },
                startTxAudio = { tx.start() },
                stopTxAudio = { tx.stop() },
                serverTXStatus = { vm.state.txStatus },
                isCtrlConnected = { cm.isConnected },
                onStuckTX = { /* error 提示：连到 vm.error 由实现接上 */ },
            ),
            scope = scope,
        )
        vmFactory = { vm }
    }
}
```
（`FT710App` 加 `companion object { lateinit var instance: FT710App }`，`onCreate` 里 `instance = this`；`ServiceLocator.assemble()` 在 `FT710App.onCreate` 调用。`tx` 的 sendFrame 闭包要在 Task 12 的构造里接 `cm.sendTxAudioBinary`——Task 12 构造为 `(ByteArray) -> Unit`，这里直接 `{ bytes -> cm.sendTxAudioBinary(bytes) }`。）

新建 `FT710Android/app/src/main/java/com/hamradio/ft710android/App/MainViewModelHolder.kt`：
```kotlin
package com.hamradio.ft710android.App

import androidx.lifecycle.ViewModel
import com.hamradio.ft710android.ViewModel.MainViewModel

class MainViewModelHolder : ViewModel() {
    val vm: MainViewModel by lazy { ServiceLocator.vmFactory() }
}
```

新建 `FT710Android/app/src/main/java/com/hamradio/ft710android/App/RootScreen.kt`：
```kotlin
package com.hamradio.ft710android.App

import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import com.hamradio.ft710android.Data.SettingsStore
import com.hamradio.ft710android.UI.LoginScreen
import com.hamradio.ft710android.UI.MainScreen
import com.hamradio.ft710android.UI.SettingsScreen
import com.hamradio.ft710android.ViewModel.MainViewModel

@Composable
fun RootScreen(vm: MainViewModel, settings: SettingsStore) {
    var loggedIn by rememberSaveable { mutableStateOf(false) }
    var showSettings by rememberSaveable { mutableStateOf(false) }
    AppSetup(rxRunning = true, onTxRelease = { vm.onPttRelease() })
    if (!loggedIn) {
        LoginScreen(vm, settings) { loggedIn = true }
    } else if (showSettings) {
        SettingsScreen(vm, settings) { loggedIn = false; showSettings = false }
    } else {
        MainScreen(vm)
    }
}
```

`MainActivity.kt` 改为：
```kotlin
package com.hamradio.ft710android.App

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import com.hamradio.ft710android.Data.SettingsStore

class MainActivity : ComponentActivity() {
    private val holder: MainViewModelHolder by viewModels()
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val settings = SettingsStore(applicationContext)
        setContent { AppTheme { RootScreen(vm = holder.vm, settings = settings) } }
    }
    override fun onStop() { holder.vm.onPttRelease(); super.onStop() }
}
```
> 说明：`MainViewModelHolder` 持有 VM；ServiceLocator 在 `FT710App.onCreate` 初始化，把 `ConnectionManager` 回调接到 `vm.onWsEvent/onAudioRxFrame/onSpectrumFrame`。`AppSetup` 负责亮屏/音频焦点/退后台 forceRelease（Task 17 Step 2）；`onStop` 再兜底一次。TX 采集的 sendFrame 接 `cm.sendTxAudioBinary`。

- [ ] **Step 4: 编译验证**

```bash
cd FT710Android && ./gradlew assembleDebug
```

- [ ] **Step 5: Commit**

```bash
git add FT710Android/app/src/main/res/xml/network_security_config.xml FT710Android/app/src/main/java/com/hamradio/ft710android/App/AppSetup.kt FT710Android/app/src/main/AndroidManifest.xml FT710Android/app/src/main/java/com/hamradio/ft710android/App/MainActivity.kt
git commit -m "feat(android): wire TLS, audio focus, keep-screen-on, lifecycle TX release

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 18: SettingsScreen

**Files:**
- Create: `FT710Android/app/src/main/java/com/hamradio/ft710android/UI/SettingsScreen.kt`

**Interfaces:**
- Consumes: `MainViewModel`、`SettingsStore`。
- Produces: `@Composable fun SettingsScreen(vm: MainViewModel, settings: SettingsStore, onLoggedOut: () -> Unit)`：RF 功率滑杆、scope span 选择、主机/重连、退出登录（`logout` + 清凭据 + 回登录页）。

- [ ] **Step 1: 实现 SettingsScreen**

`FT710Android/app/src/main/java/com/hamradio/ft710android/UI/SettingsScreen.kt`:
```kotlin
package com.hamradio.ft710android.UI

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.collectAsState
import androidx.compose.ui.Alignment
import androidx.compose.ui.unit.dp
import com.hamradio.ft710android.Data.SettingsStore
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.launch

@Composable
fun SettingsScreen(vm: MainViewModel, settings: SettingsStore, onLoggedOut: () -> Unit) {
    val scope = rememberCoroutineScope()
    // 订阅 version 版本号触发重组，然后读 vm.state.* 拿到最新值
    vm.version.collectAsState()
    val state = vm.state

    Column(Modifier.fillMaxSize().padding(16.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Text("设置", style = MaterialTheme.typography.headlineSmall)
        Spacer(Modifier.height(16.dp))
        Text("RF 功率: ${state.rfPower} W")
        Slider(value = state.rfPower.toFloat(), onValueChange = { vm.setRfPower(it.toInt()) }, valueRange = 5f..100f)
        Spacer(Modifier.height(8.dp))
        Text("Scope Span")
        Row {
            listOf("50k", "100k", "1M").forEachIndexed { i, label ->
                FilterChip(selected = state.scopeSpan == i, onClick = { vm.setScopeSpan(i) }, label = { Text(label) })
                Spacer(Modifier.width(6.dp))
            }
        }
        Spacer(Modifier.weight(1f))
        TextButton(onClick = { vm.connectionManager.reconnectAll() }) { Text("重连") }
        Button(onClick = {
            scope.launch { vm.logout(); settings.clearCredentials(); onLoggedOut() }
        }) { Text("退出登录") }
    }
}
```
> 说明：`vm.connectionManager` 为公开属性（Task 14），`vm.logout()` 为 suspend 清理连接，`settings.clearCredentials()` 在 Task 15 的 `SettingsStore` 里补一个 Keystore 删除密钥的方法。

- [ ] **Step 2: 编译验证**

```bash
cd FT710Android && ./gradlew assembleDebug
```

- [ ] **Step 3: Commit**

```bash
git add FT710Android/app/src/main/java/com/hamradio/ft710android/UI/SettingsScreen.kt
git commit -m "feat(android): add settings screen

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 19: 文档 + lint + 最终验证

**Files:**
- Create: `FT710Android/CLAUDE.md`
- Modify: `FT710Android/BUILD_GUIDE.md`（补充完整流程：SDK/JDK/NDK/Gradle/构建/安装）
- Modify: 仓库根 `README.md`（项目结构 + 文档表各加 `FT710Android/`）
- Modify: 仓库根 `AGENTS.md`（加 Android 子项目说明一节）

- [ ] **Step 1: 写 CLAUDE.md**

`FT710Android/CLAUDE.md`（面向 AI 代理，对标 `FT710Mobile/CLAUDE.md` 的写法）：
```markdown
# CLAUDE.md — FT710Android

> 面向 AI 编码代理。原生 Android 遥控客户端（Kotlin + Jetpack Compose），连仓库根 Python FastAPI 服务端。

## 构建与测试
```bash
cd FT710Android
./gradlew test assembleDebug lintDebug   # CI 门槛：JVM 单测 + 编译 + lint
./gradlew installDebug                   # 真机安装（USB 调试）
```
- 需要 JDK 17 + Android SDK（见 `BUILD_GUIDE.md`）；`ANDROID_HOME` 指到 SDK。
- 仪器测试（OpusBridge round-trip）需真机/模拟器：`./gradlew connectedDebugAndroidTest`。

## 架构速览
MainViewModel(普通类, Compose remember) → ConnectionManager(4+1 路 WS) / RadioState / RxAudioPlayer / TxAudioCapture / SpectrumProcessor / PTTManager。纯逻辑 JVM 可测。

## 协议事实（逐字对齐 server.py）
- 登录: POST /api/auth/login → {"ok":true,"token"} + Set-Cookie ft710_auth；401/429 → 回登录页停止重连。
- 4 路 WS: /WSradio(JSON) /WSaudioRX(binary) /WSaudioTX(binary+text 's:') /WSspectrum(1701B) + 可选 /WSatr1000(JSON, close 4000=禁用)。
- 控制上行: {"type":"set","field":...,"value":...}; ping 每 2s。
- 音频帧: 1B tag(0x00 PCM / 0x01 Opus) + payload; 48k 单声道 20ms(960 样本); TX 恒 Opus CBR 64k。
- 记忆频道: 6 槽 null 补空, 键 label。
- fullState 附带 bands/modes/filterTables/atr1000Enabled —— 必须消费下发值, 不建平行常量表。

## 坑
- PTT: release() 无条件发 ptt:false + 's:'; 手势 finally 兜底; onStop forceRelease; 看门狗 500ms×3。
- 字段名以 server.py set 链为准, 禁止自创(如 "ipo" 被静默吞)。
- 后台 RX 播放未实现(v1 决策); 退后台即停 TX。
- 自签 TLS 用 AuthApi.selfSignedOkHttpClient(); 明文仅调试。
```

- [ ] **Step 2: 补 BUILD_GUIDE.md**

在 `FT710Android/BUILD_GUIDE.md` 末尾补完整流程（JDK17、cmdline-tools、SDK、NDK、Gradle wrapper、`./gradlew assembleDebug`、`adb install`、自签连接提示）。

- [ ] **Step 3: 更新仓库根 README.md / AGENTS.md**

`README.md` 项目结构树加：
```text
├── FT710Android/         # 原生 Android 遥控客户端（Kotlin + Compose）
```
文档表加一行 `| [docs/superpowers/specs/2026-08-16-ft710-android-app-design.md](docs/superpowers/specs/2026-08-16-ft710-android-app-design.md) | FT710 Android App 设计 |`。
`AGENTS.md` 的模块表加一行：`| FT710Android/ | 原生 Android 客户端(Kotlin+Compose)；协议见 FT710Android/CLAUDE.md |`。

- [ ] **Step 4: 最终验证**

```bash
cd FT710Android && ./gradlew test assembleDebug lintDebug
```
预期：`BUILD SUCCESSFUL`、`BUILD SUCCESSFUL`（lint 无 error）、`0 tests failed`。

- [ ] **Step 5: 仓库级检查 + Commit**

```bash
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged  # 若该 harness 覆盖新增文件
git add FT710Android/ README.md AGENTS.md
git commit -m "docs(android): add CLAUDE.md, BUILD_GUIDE, repo references

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 自审备注（供执行者）

- **PTTManager 调度器**：`dispatcher` 为构造可选参数（`private val dispatcher: CoroutineDispatcher? = null`），测试构造传 `StandardTestDispatcher` 并让 `runTest` 用同一调度器。若 kotlinx-coroutines-test 虚拟时间 API 有出入，以 `advanceUntilIdle()` 等价替代。
- **Compose 重组**：`RadioState` 是可变的普通类，UI 靠订阅 `MainViewModel.version`（`StateFlow<Long>`，每次 `apply` 后 +1）触发重组，随后读 `vm.state.*`。Task 16/18 已按此写法。
- **Task 14/17** 的 ServiceLocator/holder 是"最小装配点"约定，执行时可落地为 `FT710App` 里的简单单例持有；不要在 UI 里 new 全局依赖。
- 音频/UI 任务（Task 11/12/16/17/18）无法 JVM 单测，交付标准 = `assembleDebug` 通过 + 手动清单（spec §11）。Opus round-trip 仪器测试（Task 10）在真机上跑。
- 真机验收清单最终以 spec §11 为准。
