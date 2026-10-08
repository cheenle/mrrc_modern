# 频谱带宽档位 P2（Android 客户端）实现计划

> **面向 AI 代理的工作者：** 必需子技能：superpowers:executing-plans 逐任务实现；改 `FT710Android/` 下任何代码前必须先读 `android-app` 技能（构建门槛、发布铁律、客户端不变量、固定陷阱都在里面）。

**目标：** 让安卓客户端能吃 851 B 短帧、会声明档位、能在设置里选档——从而真的把手机侧频谱流量降下来。**服务端已上线（P1），安卓不发 caps 就永远拿 `high`，所以这一步不做，手机流量一分不省。**

**架构：** 三处正交改动 —— ① 解析器放宽长度（`Spectrum/SpectrumFrame.kt`）；② 连接层在频谱 socket 上声明档位（`Network/ConnectionManager.kt` + `Spectrum/SpectrumTiers.kt` 纯逻辑表）；③ 设置项持久化 + UI（`Data/SettingsStore.kt`、`UI/UiPrefs.kt`、`UI/SettingsScreen.kt`、`App/RootScreen.kt`、`ViewModel/MainViewModel.kt`）。**唯一真相源是持久化偏好**：`RootScreen` 用 `LaunchedEffect(prefs.spectrumProfile)` 把它推给连接层，所以启动、重连、用户改档三条路走同一条代码，不会出现"UI 显示 A、线上发 B"。

**技术栈：** Kotlin + Jetpack Compose、OkHttp 4.12、DataStore Preferences、JUnit4（JVM 单测）、Gradle/AGP 8.x + JDK 17。

**设计文档：** `docs/superpowers/specs/2026-10-07-spectrum-bandwidth-profiles-design.md`（§4 兼容矩阵、§5 客户端形态、D-2/D-3）
**上一阶段：** `docs/superpowers/plans/2026-10-07-spectrum-bandwidth-profiles-p1.md`（服务端 + Web，已完成并实测）

**代码位置：** worktree `.worktrees/android-v1.0.0`，分支 `feat/android-1.0.0`（**主仓 `main` 上的 `FT710Android/` 是 v0.1.0 远古骨架，改它不会进包**）。所有路径相对该 worktree 的 `FT710Android/`。

---

## 全局约束（每个任务都适用）

1. **基线**：`export JAVA_HOME=$(/usr/libexec/java_home -v 17); export ANDROID_HOME="$HOME/Library/Android/sdk"` 后 `./gradlew test` = `BUILD SUCCESSFUL`（已实测）。日常门槛 `./gradlew test assembleDebug lintDebug`；发版前四连加 `assembleRelease`。
2. **绝不跑不带 `--apk-only` 的 `release.sh`，绝不跑全站 `deploy.sh`**（会把 Windows/macOS 的下载卡回退，2026-10-05 实测过）。卡片只允许动 `<!-- android-download:start/end -->` 标记块。发布**严格串行**：`release.sh --apk-only` → `publish-card.sh` → 等 ~65 s（nginx `open_file_cache` valid 60s）再复核；**别信 curl 的 200**（别名被删后 nginx 会用旧 inode 继续回 200），判真伪用 `ssh sha256sum`。
3. **纯逻辑必须 JVM 单测**（解析器、档位表、caps JSON、`requiredChannels` 交互）；UI 靠 build + 真机验收。
4. **向前兼容**：caps 是一条普通文本帧。老服务端的 `/WSspectrum` 收到文本帧是直接丢弃的（P1 之前就是 `await ws.receive_text()` 不解析），所以**新 App 连老服务端不会坏**，只是拿不到短帧。这条要写进 CHANGELOG。
5. **不碰这些不变量**（技能里点过名的）：`ChannelFlags` 线程安全；播放器/PTT 各自只看自己的通道，**禁止**把新功能挂到"四路全齐"的聚合上；后台暂停期间 `/WSspectrum` **不计入连接判据**（`requiredChannels(listenOnly, spectrumPaused)`），且 `start()` 尊重暂停状态——**发 caps 绝不能变成一次重连**，否则后台会把频谱拉回来；瀑布增量绘制（`WaterfallRingBuffer`）；频谱流只在 `SpectrumPanel` 内订阅；尺寸只从 `Data/ScreenFit.kt` → `LocalScreenMetrics` 出，Composable 里不写死 dp；`IntrinsicSize` 绝不包 `BoxWithConstraints`/`Lazy*`/`TabRow`。
6. **偏好键名对齐 Web cookie 键**（`SettingsStore.kt:19` 的既有约定）：Web 侧是 `getStored('scopeProfile')` → cookie `ft710_scopeProfile`，所以安卓用 `scopeProfile`。**档位值域也对齐服务端白名单** `high`/`mid`/`low`（`listen` 是服务端内部档，客户端不可选）。
7. **默认档位 = `high`**：不静默改变任何现有用户的观感（与 Web 一致，符合规格的兼容不变量）。想让所有安卓用户自动省流量就是把这一个默认值改成 `mid`——但那会把瀑布刷新率静默砍半，属于产品决定，不在本计划内。
8. **脚本改代码必须验证替换生效**：`assert old in s` 再 replace，改完 `grep` 复核关键符号（本仓踩过三次静默失效）。
9. **发版前必做结构 diff**：`git show <上一个好版本>:…/MainScreen.kt` 比对 `PadBtn`/`SmallChip`/`MeterCell` 标签、`vm.*(` 调用、`state.*`/`prefs.*` 字段、`*Dialog`/`*Panel` 集合，少任何一项就不发版。本次只改设置页与网络层，但 `MainScreen.kt` 若被动到就必须做。
10. **版本号**：`app/build.gradle.kts` 当前 `versionCode = 31` / `versionName = "1.1.28"` → 本次 `32` / `"1.1.29"`；`CHANGELOG.md` 顶部加中文条目（写"症状→根因→修法"）。
11. **提交前** `python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged` 必须 clean（在 worktree 里跑，harness 路径相对主仓根：`/Users/cheenle/HAM/hub/mrrc_modern/.agents/...`）。每任务一次 commit。
12. **发布需要用户明确点头**：`release.sh --apk-only` 会把 APK 传上线并改官网卡片，是用户可见动作。代码 + 门槛跑完后**停下来问**，不要自动发。

---

## 文件结构

| 路径（相对 `FT710Android/`） | 动作 | 职责 |
|---|---|---|
| `app/src/main/java/.../Spectrum/SpectrumTiers.kt` | 新建 | 档位表（纯逻辑、可 JVM 测）：值域、默认值、显示标签、caps JSON 构造。**对齐服务端 `spectrum_profile.py` 的 `CLIENT_PROFILES`** |
| `app/src/main/java/.../Spectrum/SpectrumFrame.kt` | 改 | 解析器接受 851 B；`wf2` 改可空（短帧时为 `null`，不再分配 850 个没人读的元素） |
| `app/src/main/java/.../Spectrum/SpectrumProcessor.kt` | 改注释 | 顶部注释"解析 1701B 帧"已不成立 |
| `app/src/main/java/.../Network/ConnectionManager.kt` | 改 | `setSpectrumProfile()`：存档位 + 在已连的频谱 socket 上补发 caps；`connectSpectrum()` 连接后立即声明；修 `:227` 陈旧注释 |
| `app/src/main/java/.../Data/SettingsStore.kt` | 改 | `Keys.scopeProfile` + `val scopeProfile: Flow<String>` + `putScopeProfile()` |
| `app/src/main/java/.../UI/UiPrefs.kt` | 改 | `UiPrefs.spectrumProfile` + 并入 `combine`（注意 5 流上限） |
| `app/src/main/java/.../UI/SettingsScreen.kt` | 改 | 频谱区加一行 `FilterChip` 选档（照抄"配色"那行的写法） |
| `app/src/main/java/.../App/RootScreen.kt` | 改 | `LaunchedEffect(prefs.spectrumProfile) { vm.setSpectrumProfile(...) }` |
| `app/src/main/java/.../ViewModel/MainViewModel.kt` | 改 | `setSpectrumProfile()` 转发给 connectionManager；修 `:209` 陈旧注释 |
| `app/src/test/java/.../Spectrum/SpectrumFrameTest.kt` | 改 | 加 851 B 用例；既有 3 条断言按可空 wf2 调整 |
| `app/src/test/java/.../Spectrum/SpectrumTiersTest.kt` | 新建 | 档位表 + caps JSON + 白名单 |
| `app/src/test/java/.../Network/ConnectionManagerTest.kt` | 改/加 | 发 caps 的时机、后台暂停时不重连、`openedPaths` 不多连 |
| `app/build.gradle.kts`、`CHANGELOG.md` | 改 | 版本 32 / 1.1.29 + 条目 |
| `CLAUDE.md`（worktree 内） | 改 | 协议事实里补短帧与档位；**并修掉"`_broadcast_spectrum_loop` docstring 写 5 fps 是陈旧的"这条警告——P1 已经把那个 docstring 改对了**，留着会让人以为还得绕开它 |

---

## 任务 1：档位表（纯逻辑）+ 解析器放宽到 851 B

**文件：** 新建 `Spectrum/SpectrumTiers.kt`、改 `Spectrum/SpectrumFrame.kt`、改/新建对应测试

- [ ] **步骤 1：写失败测试**

新建 `app/src/test/java/com/hamradio/ft710android/Spectrum/SpectrumTiersTest.kt`：

```kotlin
package com.hamradio.ft710android.Spectrum

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SpectrumTiersTest {
    @Test fun `values match the server whitelist exactly`() {
        // 服务端 spectrum_profile.CLIENT_PROFILES == ("high","mid","low")；
        // "listen" 是服务端内部档（listener 角色默认），客户端不得声明。
        assertEquals(listOf("high", "mid", "low"), SpectrumTiers.NAMES)
        assertEquals("high", SpectrumTiers.DEFAULT)
        assertFalse(SpectrumTiers.isValid("listen"))
    }

    @Test fun `every tier has a label and none is empty`() {
        SpectrumTiers.NAMES.forEach { assertTrue(SpectrumTiers.label(it).isNotBlank()) }
        // 未知名字回退到默认，不抛
        assertEquals(SpectrumTiers.label(SpectrumTiers.DEFAULT), SpectrumTiers.label("nope"))
    }

    @Test fun `caps json is exactly what the server parses`() {
        assertEquals("""{"type":"spectrumCaps","profile":"mid"}""", SpectrumTiers.capsJson("mid"))
    }

    @Test fun `caps json rejects unknown names by falling back to default`() {
        // 宁可声明 high（= 服务端默认，逐字节兼容）也不发一个服务端会忽略的名字
        assertEquals(SpectrumTiers.capsJson(SpectrumTiers.DEFAULT), SpectrumTiers.capsJson("turbo"))
    }

    @Test fun `normalize coerces anything unknown to the default`() {
        assertEquals("high", SpectrumTiers.normalize("high"))
        assertEquals("low", SpectrumTiers.normalize("low"))
        assertEquals("high", SpectrumTiers.normalize(""))
        assertEquals("high", SpectrumTiers.normalize("listen"))
    }
}
```

在既有 `SpectrumFrameTest.kt` 里追加（保留原有 4 条用例）：

```kotlin
    private fun makeShortFrame(wf1Value: Byte = 0x55): ByteArray {
        val f = ByteArray(851)
        f[0] = 0x01
        for (i in 1..850) f[i] = wf1Value
        return f
    }

    @Test fun `parses the 851-byte wf1-only frame (server wf1 tier, AD-025)`() {
        val sf = parseSpectrumFrame(makeShortFrame())!!
        assertEquals(1, sf.version)
        assertEquals(850, sf.wf1.size)
        assertEquals(0x55, sf.wf1[0] and 0xFF)
        assertNull("短帧没有 wf2，不该假装有一个全零的第二瀑布", sf.wf2)
    }

    @Test fun `short frame wf1 equals the full frame's wf1`() {
        val full = parseSpectrumFrame(makeFrame())!!
        val short = parseSpectrumFrame(makeShortFrame())!!
        assertArrayEquals(full.wf1, short.wf1)
    }

    @Test fun `still rejects every other length`() {
        // 只放行 851 与 1701；1700/1702/852 一律 null，
        // 否则半截帧会被当成有效数据画进瀑布。
        listOf(0, 1, 100, 850, 852, 1700, 1702, 3402).forEach { n ->
            assertNull("length $n", parseSpectrumFrame(ByteArray(n).also { if (n > 0) it[0] = 0x01 }))
        }
    }
```

（`assertNull` / `assertArrayEquals` 需要相应 import：`org.junit.Assert.assertArrayEquals`。）

- [ ] **步骤 2：运行确认失败**

```bash
./gradlew test --tests '*SpectrumTiersTest*' --tests '*SpectrumFrameTest*'
```

预期：`SpectrumTiersTest` 编译失败（`unresolved reference: SpectrumTiers`），`SpectrumFrameTest` 新用例失败（短帧返回 null；`sf.wf2` 非空类型上 `assertNull` 编译不过）。

- [ ] **步骤 3：实现**

新建 `Spectrum/SpectrumTiers.kt`：

```kotlin
package com.hamradio.ft710android.Spectrum

/**
 * 频谱带宽档位（服务端 `spectrum_profile.py` / SDD AD-025）。
 *
 * 一个档位 = 帧形状 × 帧率分频，服务端两者一起决定：
 * - `high` 1701B（wf1+wf2）每个广播 tick —— 与 AD-025 之前逐字节相同
 * - `mid`  851B（仅 wf1）每 2 个 tick
 * - `low`  851B（仅 wf1）每 4 个 tick
 *
 * 值域必须与服务端白名单一致；`listen` 是服务端给只读会话的内部默认档，
 * 客户端声明它会被忽略，所以不在 [NAMES] 里。
 *
 * 对手机来说省下来的是**真的线上字节**：OkHttp 4.12 不提供
 * `permessage-deflate`（其 dex 里有 "Request header not permitted:
 * 'Sec-WebSocket-Extensions'" 守卫），所以 1701B 逐个走出电台 —— 这跟浏览器
 * 不一样（浏览器 deflate 后满帧≈441B，砍 wf2 几乎不省）。
 */
object SpectrumTiers {
    /** 与服务端 `CLIENT_PROFILES` 逐字一致。 */
    val NAMES = listOf("high", "mid", "low")

    /** 默认不改变任何人的观感：服务端在未收到 caps 时本来也发 high。 */
    const val DEFAULT = "high"

    /** 设置页芯片上显示的字；对齐手机端 Web 的 Full/Half/Quarter。 */
    private val LABELS = mapOf("high" to "Full", "mid" to "Half", "low" to "Quarter")

    fun isValid(name: String): Boolean = name in NAMES

    /** 未知/内部名字一律收敛到默认档，绝不把非法值发上线。 */
    fun normalize(name: String?): String = if (name != null && isValid(name)) name else DEFAULT

    fun label(name: String): String = LABELS[normalize(name)]!!

    /** `/WSspectrum` 上的能力声明帧。老服务端会把它当保活文本丢弃，因此向前兼容。 */
    fun capsJson(name: String): String =
        """{"type":"spectrumCaps","profile":"${normalize(name)}"}"""
}
```

改 `Spectrum/SpectrumFrame.kt`（`wf2` 变可空；解析器接受两种长度）：

```kotlin
data class SpectrumFrame(val version: Int, val wf1: IntArray, val wf2: IntArray?) {
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is SpectrumFrame) return false
        return version == other.version && wf1.contentEquals(other.wf1) && wf2.contentEquals(other.wf2)
    }
    override fun hashCode(): Int = version * 31 + wf1.contentHashCode() + (wf2?.contentHashCode() ?: 0)
}

/**
 * 1701B = 1B version(0x01) + 850B wf1 + 850B wf2；851B = 1B version(0x01) + 850B wf1。
 * 851B 是服务端的 `wf1` 档位（AD-025），此时 [SpectrumFrame.wf2] 为 null。
 * 其它长度一律返回 null。
 */
fun parseSpectrumFrame(frame: ByteArray): SpectrumFrame? {
    if (frame.size != 1701 && frame.size != 851) return null
    if (frame[0] != 0x01.toByte()) return null
    val wf1 = IntArray(850)
    for (i in 0 until 850) wf1[i] = frame[i + 1].toInt() and 0xFF
    val wf2 = if (frame.size == 1701) {
        IntArray(850).also { for (i in 0 until 850) it[i] = frame[i + 851].toInt() and 0xFF }
    } else {
        null
    }
    return SpectrumFrame(version = 1, wf1 = wf1, wf2 = wf2)
}
```

既有测试里两条断言要跟着可空类型改（不是放宽，是改成表达"满帧时 wf2 确实在"）：
`assertEquals(850, sf.wf2.size)` → `assertEquals(850, sf.wf2!!.size)`；`assertEquals(1, sf.wf2[0] and 0xFF)` → `assertEquals(1, sf.wf2!![0] and 0xFF)`。

`Spectrum/SpectrumProcessor.kt` **不需要改代码**（`onFrame` 只用 `sf.wf1`，已实测确认），只改顶部注释：`解析 1701B 帧` → `解析 1701B 或 851B 帧（wf1 档，AD-025）`。

- [ ] **步骤 4：运行确认通过**

```bash
./gradlew test --tests '*Spectrum*'
```

预期全绿。然后跑全量 `./gradlew test`（确认没有别处依赖 `wf2` 非空）。

- [ ] **步骤 5：提交**

```bash
git add app/src/main/java/com/hamradio/ft710android/Spectrum/ app/src/test/java/com/hamradio/ft710android/Spectrum/
python3 /Users/cheenle/HAM/hub/mrrc_modern/.agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(android): 解析器接受 851B 短帧，wf2 改可空——不再假装有一个没人读的第二瀑布"
```

---

## 任务 2：连接层声明档位（发 caps，且绝不因此重连）

**文件：** 改 `Network/ConnectionManager.kt`、改/加 `app/src/test/java/.../Network/ConnectionManagerTest.kt`

- [ ] **步骤 1：写失败测试**（照既有测试文件的风格加；先读它现有的 fake/夹具再写）

要钉住的四件事：
1. `setSpectrumProfile("mid")` 后，**新建立**的频谱连接会立刻发出一条 `{"type":"spectrumCaps","profile":"mid"}` 文本帧；
2. 已连接时改档位 → **在同一条 socket 上补发**一条 caps，且 `connectSpectrum()` 没有被再调一次（不重连、不惊动 `requiredChannels`、后台暂停时也不会把频谱拉回来）；
3. 档位值非法（`"listen"`/`""`/`"turbo"`）→ 发出去的是 `high`，不是原样透传；
4. `openedPaths` 里 `/WSspectrum` 仍只出现应有的次数（技能里点名的判据：加可选通道后用 `ConnectionManager.openedPaths` 断言"只连了该连的"）。

- [ ] **步骤 2：运行确认失败** → `./gradlew test --tests '*ConnectionManager*'`

- [ ] **步骤 3：实现**

在 `ConnectionManager` 里加字段与方法（紧挨既有的 `setSpectrumPaused` 一带，保持"频谱通道"相关代码聚在一起）：

```kotlin
    /** 当前声明的频谱档位（服务端 spectrum_profile / AD-025）。默认 high = 逐字节兼容。 */
    private var spectrumProfile: String = SpectrumTiers.DEFAULT

    /**
     * 设置频谱档位。已在传就**在同一条 socket 上补发一条 caps**，服务端下一个
     * tick 就换闸门——不重连：重连会惊动 [requiredChannels]，而后台暂停期间把
     * `/WSspectrum` 拉回来正是 [setSpectrumPaused] 明确禁止的事。
     */
    @Synchronized
    fun setSpectrumProfile(name: String) {
        val normalized = SpectrumTiers.normalize(name)
        if (spectrumProfile == normalized) return
        spectrumProfile = normalized
        if (!spectrumPaused) spectrum?.sendText(SpectrumTiers.capsJson(normalized))
    }

    /** 诊断行/测试可读。 */
    val currentSpectrumProfile: String get() = synchronized(this) { spectrumProfile }
```

`connectSpectrum()` 改成连接后立即声明：

```kotlin
    private fun connectSpectrum() {
        val b = _baseUrl ?: return
        val t = _token ?: return
        if (b.isEmpty() || t.isEmpty()) return
        spectrum = connect(b, "/WSspectrum", t, onText = { stats.onReceived(it.length) },
            onBinary = { stats.onReceived(it.size); onSpectrum(it) })
        // 连接后立即声明档位。OkHttp 会把 onOpen 之前 send() 的消息排队，
        // 所以这里不需要等 Connected 状态；服务端在收到 caps 之前一直发 high
        // （1701B 满帧），因此**晚到不会导致丢帧**，只是前几帧贵一点。
        spectrum?.sendText(SpectrumTiers.capsJson(spectrumProfile))
    }
```

并把 `:227` 那段陈旧注释改对（P1 已实测）：

```kotlin
     * 后台时停掉 `/WSspectrum`：服务端满档（high）按 1701B/帧推，广播 tick 是 30 Hz，
     * 但真频谱只在硬件帧计数前进时出帧——**实测 11.1 fps ≈ 151 kbps ≈ 68 MB/小时**；
     * S-meter 回退态才是每 tick 重造一帧（≈408 kbps ≈ 180 MB/小时）。
     * 档位（AD-025）能把 high 降到 mid/low（≈38 / ≈19 kbps），但后台一律直接停：
     * 省流量最狠的一档是"根本不连"。
```

（`import com.hamradio.ft710android.Spectrum.SpectrumTiers` 记得加。）

- [ ] **步骤 4：运行确认通过** → `./gradlew test`（全量）
- [ ] **步骤 5：提交**（`Network/ConnectionManager.kt` + 测试）

---

## 任务 3：偏好持久化 + UI + 单一真相源接线

**文件：** `Data/SettingsStore.kt`、`UI/UiPrefs.kt`、`UI/SettingsScreen.kt`、`App/RootScreen.kt`、`ViewModel/MainViewModel.kt`

- [ ] **步骤 1（实现）** `SettingsStore.kt`：
  - `Keys` 里加 `val scopeProfile = stringPreferencesKey("scopeProfile")`（键名对齐 Web cookie `ft710_scopeProfile`）
  - `val scopeProfile: Flow<String> = context.dataStore.data.map { it[Keys.scopeProfile] ?: SpectrumTiers.DEFAULT }`
  - `suspend fun putScopeProfile(v: String) = edit { it[Keys.scopeProfile] = SpectrumTiers.normalize(v) }`（**写入前就归一化**，非法值不落盘）

- [ ] **步骤 2** `UiPrefs.kt`：data class 加 `val spectrumProfile: String = SpectrumTiers.DEFAULT`；并进 `combine`——注意 `base` 已经是 5 流（`scopeTheme/scopeFloor/scopeCeil/fftHeight/wfHeight`），**不能再往 base 里加**，挂到 `withVol` 那层（`base + afVol + micVol + scopeProfile` = 4 流，仍在 5 流上限内）。

- [ ] **步骤 3** `SettingsScreen.kt`：在"配色"那一行 `FilterChip` 组之后，照抄同一写法加一行（`Text("流量档", …)` + `Row { SpectrumTiers.NAMES.forEach { FilterChip(selected = prefs.spectrumProfile == it, onClick = { scope.launch { settings.putScopeProfile(it) } }, label = { Text(SpectrumTiers.label(it), fontSize = 10.sp) }) } }`）。**只写偏好，不直接调 VM**——推送由步骤 4 的 `LaunchedEffect` 统一做，避免两条路各自为政。

- [ ] **步骤 4** `RootScreen.kt`：在 `val prefs by UiPrefsFlow(settings).collectAsState(initial = UiPrefs())` 之后加

```kotlin
    // 单一真相源：启动、重连、用户在设置里改档，三条路都只经过这里。
    LaunchedEffect(prefs.spectrumProfile) { vm.setSpectrumProfile(prefs.spectrumProfile) }
```

- [ ] **步骤 5** `MainViewModel.kt`：加 `fun setSpectrumProfile(name: String) { connectionManager.setSpectrumProfile(name) }`；顺手把 `:209` 那条"~30fps × 1701B ≈ 51KB/s ≈ 180MB/小时"的注释按实测改对（同任务 2 步骤 3 的口径）。

- [ ] **步骤 6：门槛** `./gradlew test assembleDebug lintDebug`；再跑 `OneScreenFitTest` / `ScreenFitTest` / `MainScreenComposeTest` / `UiLayoutSafetyTest`（技能点名的 UI 守卫）确认设置页加一行没把哪一档挤爆。

> 设置页是滚动容器，加一行不像主屏那样吃"一屏放得下"的预算；但仍要跑 `MainScreenComposeTest`，因为它会用真实 `MainViewModel` + 真实 fullState 把整屏 measure/layout 一遍，能抓到 `UiPrefs` 新增字段导致的构造/默认值漂移。

- [ ] **步骤 7：提交**（Data + UI + App + ViewModel 一起，一个功能一个 commit）

---

## 任务 4：版本、CHANGELOG、文档纠错

- [ ] `app/build.gradle.kts`：`versionCode = 31 → 32`，`versionName = "1.1.28" → "1.1.29"`
- [ ] `CHANGELOG.md` 顶部加中文条目，按仓内"症状→根因→修法"写法，必须包含：为什么安卓侧砍 wf2 是真的省（OkHttp 无 deflate，1701B 逐个走）、三档的实测数字（服务端 P1 实测：payload 轴 high 345.9 / mid 88.0 / low 45.1 kbps，比值 0.254 / 0.126；真频谱态预期 151/38/19）、**向前兼容**（老服务端把 caps 当保活文本丢弃，App 照常工作只是拿不到短帧）、默认仍是 `high`（不静默改观感）
- [ ] `CLAUDE.md`：协议事实一节补 851B 短帧与档位声明；**删掉/改写"`_broadcast_spectrum_loop` docstring 写 5 fps 是陈旧的、引用帧率先读常量别信 docstring"这条警告**——P1 已把那个 docstring 改对（现在写的就是 30 Hz tick / 实测 11.1 fps / 851 与 1701 两种帧长），警告留着会让人以为还得绕开它。改完按技能要求 `grep` 一遍本次删掉的符号，确认 CLAUDE.md 与技能文件里没有残留指向。
- [ ] 同步用户级技能文件 `~/.pi/agent/skills/android-app/SKILL.md` 里同样那条 docstring 警告与 `LISTEN_SPECTRUM_DIVIDER = 3 降到 ~10fps`（真频谱态是 ~3.7 Hz，~10 Hz 只在回退态成立）
- [ ] 门槛四连：`./gradlew test assembleDebug lintDebug assembleRelease`
- [ ] 提交

---

## 任务 5：发布与真机验收

> **执行记录（2026-10-08）**：用户点头后已发布 **v1.1.29 / versionCode 32**。
> `./release.sh --apk-only` → `./publish-card.sh` 串行跑完，两步退出码 0；产物
> `MRRC-Modern-v1.1.29-Android.apk` **9,406,382 bytes**
> `a00d7cdf2abe808aa59b433d02373f43fa27564c1f1bd4d1c9639b9e35a6f1ac`，签名 `CN=MRRC Modern`。
> 四步复核全过（详见 `FT710Android/CHANGELOG.md` 的 v1.1.29 条目）。
> **发布中发现并修掉一个结构性地雷**：`publish-card.sh` 原先只补线上页面，而站点树是指向仓库
> `website/` 的符号链接 ⇒ 仓库里的 Android 卡片停在 v1.1.17，任何一次全站 `deploy.sh`
> （tar `--overwrite`，HTML 在包里）都会把线上卡片降级回 v1.1.17——这就是 2026-10-07 23:39
> 那次事故的机制。已给脚本加第 3 步：线上复核通过后回写仓库两页（验过幂等），
> 并把仓库卡片追平到 v1.1.29、与线上逐字节一致（主仓 commit `79fe518`）。
> 剩下的只有下面的真机验收清单。

代码与门槛过了之后**停下来问用户是否发布**（全局约束 12）。发布与验收按技能的铁律走：

1. `./release.sh --apk-only`（自带 `test lintDebug assembleRelease`，**绝不加 `--skip-tests`**，**绝不用管道接 gradle 输出**——管道退出码取自 `head`，红灯会被吞掉）
2. `./publish-card.sh` → 等 ~65 s → `ssh sha256sum` 复核别名与卡片（别信 curl 200）
3. 真机验收（技能清单 + 本次专项）：
   - 设置页出现"流量档"三个芯片，默认 Full；切到 Quarter 后频谱**仍在滚动**（只是更慢），`ch:` 里 `S` 仍为 `+`
   - 状态行诊断：`S:120` 一类计数继续增长（帧在到），`D:`/`J:` 不异常
   - 服务端日志出现 `Spectrum profile low for operator socket: wf1 frame (851 B), divider 4`，且 `Session metrics` 行的 `spectrum frames` 里 `low=` 在涨
   - **退后台再回前台**：频谱恢复、档位仍是 Quarter（`LaunchedEffect` 重放偏好），且后台期间 `S(p)` 表示暂停而非断线
   - **连老服务端**（如果手边有 1.25.4 的实例）：App 不崩、频谱正常、只是拿 1701B
   - 连续 10 分钟：`J:` 稳定 100~300ms，无秒级累积
4. 把实测结果写回规格 §6（新增"P2 实测"小节）与 `CHANGELOG.md`

---

## P2 之后

- **P3 iOS**：`SpectrumProcessor.swift:52` 本来就接受 `count >= binCount + 1`（能吃 851B），但 `:49,51` 的注释写着 "Expect 1701 bytes"；要省流量同样得发 caps + 加设置项。
- **跨仓 `mrrc_hub`**：`NFR-H007`（0.48 Mbps）与 `AD-H14`（频谱占 86%）建在 30 fps 假设上，输入数据需按实测 151 kbps / ~72% 且"可按档配"修正。
- **音频码率档**：先得真的实现 `setOpusBitrate`（目前只在注释与文档里，P1 已标注）。
