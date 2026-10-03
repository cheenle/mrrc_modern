# MRRC Modern Android v1.0.0 发布 实施计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把 `FT710Android/` 从"JVM 测试过、从未上真机"的 0.1.0 做成可官网下载的签名 v1.0.0：补齐 txhb 心跳 / 录音面板 / CQ 按键 / 品牌图标 4 项差距，修复 3 处接线缺陷，建立签名与一键发布链路，交给用户真机验收（LAN + Cloud Hub）。

**架构：** 全部改动集中在 `FT710Android/`（Kotlin + Compose，协议逐字对齐 `server.py`），外加 `release.sh` 发布脚本、站点 zh/en 页面标记块、README/CLAUDE/BUILD_GUIDE/CHANGELOG 文档。录音走 REST（列表/下载/删除）+ 本地缓存播放；txhb 在 PTTManager 内 500ms 调度；4003 关闭码上抛标记 listen-only。不做服务端改动。

**技术栈：** Kotlin 2.0.21 · Compose BOM 2024.12.01 · AGP 8.7.3 · Gradle 8.9 · JDK 17 · OkHttp 4.12.0 · kotlinx-serialization · NDK/libopus · minSdk 26 · androidx.core 1.15.0（FileProvider）。

**设计文档：** `docs/superpowers/specs/2026-10-03-android-client-v1.0.0-release-design.md`

---

## 全局约束

- **每个 shell 先 export**：`export JAVA_HOME=$(/usr/libexec/java_home -v 17)`、`export ANDROID_HOME="$HOME/Library/Android/sdk"`。
- **协议字段逐字对齐 `server.py`**（`txhb` 是 `/WSradio` 控制通道文本消息；录音/CQ 是 `set` 字段 + 独立下行消息；`recording`/`cq` 在 fullState 的**顶层**）。
- **PTT 安全铁律不退化**：`release()` 无条件发 `ptt:false`；手势 `finally` 兜底；`onStop` → `forceRelease()`；看门狗 500ms×3。
- **每任务一次 commit**；提交前 `python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged` 必须 clean。
- **不碰**运行期文件：`atr1000_tuner.json`、`mem_channels.json`、`.env`、`certs/`。
- **仓库根路径**：`/Users/cheenle/HAM/hub/mrrc_modern`；站点仓库：`/Users/cheenle/HAM/website`（独立 git 仓库）。
- UI 无法 JVM 单测，交付标准 = `assembleRelease` 通过 + 用户真机验收；纯逻辑必须有 JVM 单测。

## 文件结构（新增 / 修改）

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `FT710Android/app/src/main/java/.../PTT/PTTManager.kt` | 修改 | txhb 心跳调度（注入收尾回调） |
| `.../Network/WsCommands.kt` | 修改 | `txhb()` 命令 |
| `.../Network/ConnectionManager.kt` | 修改 | txhb 路由；4003 → listen-only；连接聚合判据 |
| `.../Network/WebSocketConnection.kt` | 修改 | 关闭码上抛 + onClosing 握手回执 |
| `.../Network/Protocol.kt` | 修改 | recordingState / cqState / fullState 能力字段 |
| `.../Network/RecordingsApi.kt` | 新增 | 录音 REST（列表/下载/删除）+ 纯解析函数 |
| `.../ViewModel/MainViewModel.kt` | 修改 | 连接透传、listen-only、录音/CQ 状态与动作、错误通道 |
| `.../UI/MainScreen.kt` | 修改 | 设置入口、REC 入口、CQ 按钮、listen-only 降级、错误条 |
| `.../UI/RecordingPanel.kt` | 新增 | 录音面板（启停/列表/播放/导出/删除） |
| `.../UI/SettingsScreen.kt` | 修改 | 版本显示 |
| `.../UI/LoginScreen.kt` | 修改 | 品牌名 + 主机/端口提示 |
| `.../UI/Format.kt` | 新增 | `fmtSeconds` / `fmtBytes` / `fmtStartedAt` |
| `.../App/ServiceLocator.kt` | 修改 | 全部接线 |
| `.../App/RootScreen.kt` | 修改 | 设置页可达 |
| `app/build.gradle.kts` | 修改 | versionName 1.0.0、signingConfig、buildConfig |
| `app/src/main/AndroidManifest.xml` | 修改 | 图标、FileProvider |
| `res/mipmap-anydpi-v26/`、`res/drawable/ic_launcher_foreground.xml`、`res/values/colors.xml` | 新增 | 自适应图标 |
| `res/xml/file_paths.xml` | 新增 | FileProvider 路径 |
| `res/values/strings.xml` | 修改 | `MRRC Modern` |
| `gradle/libs.versions.toml` | 修改 | androidx.core |
| `FT710Android/release.sh` | 新增 | 一键发布（构建→签名核对→上传→复核） |
| `FT710Android/CHANGELOG.md` | 新增 | App 版本历史 |
| `FT710Android/keystore.properties`（gitignore）、`FT710Android/.gitignore` | 新增/修改 | 签名密钥引用 |
| `FT710Android/BUILD_GUIDE.md`、`CLAUDE.md` | 修改 | 发布/协议文档 |
| `README.md`（mrrc_modern 根） | 修改 | Android 下载区（稳定别名链接，静态段） |
| `~/HAM/website/mrrc_modern/{index.html,zh/index.html}` | 修改 | 下载卡片 + 标记块 + 去掉"需从源码构建" |

---

### 任务 1：隔离工作区 + 基线证明

**文件：** 无源码改动（可能改 `.gitignore`）

- [ ] **步骤 1：建立隔离工作区**

按 using-git-worktrees 技能：先检测（`git rev-parse --git-dir` 与 `--git-common-dir` 不同则已在 worktree，跳过）；否则征求用户同意后创建：

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
grep -q "^\.worktrees/" .gitignore || { printf '\n# Linked worktrees\n.worktrees/\n' >> .gitignore && git add .gitignore && git commit -m "chore: ignore .worktrees/"; }
git worktree add .worktrees/android-v1.0.0 -b feat/android-1.0.0
cd .worktrees/android-v1.0.0
# 之后所有任务的相对路径均以此为仓库根
```

*用户拒绝 worktree 时：原地工作，跳过本步，任务 13 的"合并"步骤改为无操作。*

- [ ] **步骤 2：证明基线（旧代码在当前工具链全绿）**

```bash
cd FT710Android
export JAVA_HOME=$(/usr/libexec/java_home -v 17); export ANDROID_HOME="$HOME/Library/Android/sdk"
./gradlew test assembleDebug
```

预期：`BUILD SUCCESSFUL`；测试计数 = 33（若基线红，停下修基线并单独 commit，不得在红基线上叠加功能）。

- [ ] **步骤 3：确认运行期文件未被带入暂存**

```bash
git status --short   # 不得出现 atr1000_tuner.json 等运行期文件的改动
```

*无 commit（工作区建立即是本任务成果）。*

---

### 任务 2：连接状态透传（修复 ConnDot 永远灰）

**文件：**

- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/ViewModel/MainViewModel.kt`
- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/App/ServiceLocator.kt`
- 测试：`FT710Android/app/src/test/java/com/hamradio/ft710android/ViewModel/MainViewModelTest.kt`

- [ ] **步骤 1：写失败测试**

在 `MainViewModelTest.kt` 追加（并补 `import org.junit.Assert.assertFalse`、`import org.junit.Assert.assertTrue`）：

```kotlin
    @Test fun `connection change updates connected flow`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val vm = MainViewModel(null, cm(scope), null, null, null, null, null, scope)
        assertFalse(vm.connected.value)
        vm.onConnectionChange(true)
        assertTrue(vm.connected.value)
        vm.onConnectionChange(false)
        assertFalse(vm.connected.value)
    }
```

- [ ] **步骤 2：运行验证失败**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*MainViewModelTest*'
```

预期：编译失败 `unresolved reference: onConnectionChange`（红）。

- [ ] **步骤 3：实现**

`MainViewModel.kt` 在 `onWsEvent` 下方加：

```kotlin
    /** ConnectionManager 四路聚合状态透传（修复：此前无人写入 _connected）。 */
    fun onConnectionChange(connected: Boolean) { _connected.value = connected }
```

`ServiceLocator.kt`：`onConnectionChange = {},` → `onConnectionChange = { vm.onConnectionChange(it) },`

- [ ] **步骤 4：运行验证通过 + 全量测试**

```bash
cd FT710Android && ./gradlew test
```

预期：34 个测试全绿。

- [ ] **步骤 5：Commit**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add FT710Android
git commit -m "fix(android): wire the connection aggregate into the UI (dot was always grey)"
```

---

### 任务 3：设置页可达 + 客户端版本显示（修复设置页死代码）

**文件：**

- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/UI/MainScreen.kt`
- 修改：`.../App/RootScreen.kt`
- 修改：`.../UI/SettingsScreen.kt`
- 修改：`FT710Android/app/build.gradle.kts`

- [ ] **步骤 1：MainScreen 增加设置入口**

签名改为 `fun MainScreen(vm: MainViewModel, onOpenSettings: () -> Unit)`；顶栏在模式文本后加：

```kotlin
            TextButton(onClick = onOpenSettings) { Text("设置", fontSize = 12.sp) }
```

- [ ] **步骤 2：RootScreen 接线**

`MainScreen(vm)` → `MainScreen(vm, onOpenSettings = { showSettings = true })`

- [ ] **步骤 3：SettingsScreen 显示版本**

加 import `com.hamradio.ft710android.BuildConfig`；标题下方加：

```kotlin
        Text("客户端 v${BuildConfig.VERSION_NAME}", style = MaterialTheme.typography.bodySmall)
```

- [ ] **步骤 4：启用 BuildConfig 生成**

`app/build.gradle.kts`：`buildFeatures { compose = true }` → `buildFeatures { compose = true; buildConfig = true }`

- [ ] **步骤 5：构建验证**

```bash
cd FT710Android && ./gradlew assembleDebug
```

预期：`BUILD SUCCESSFUL`（UI 无 JVM 测试，构建即门槛）。

- [ ] **步骤 6：Commit**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add FT710Android
git commit -m "fix(android): make Settings reachable and show the client version"
```

---

### 任务 4：txhb 发射心跳（安全项）

**文件：**

- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/Network/WsCommands.kt`
- 修改：`.../Network/ConnectionManager.kt`
- 修改：`.../PTT/PTTManager.kt`
- 修改：`.../App/ServiceLocator.kt`
- 测试：`.../PTT/PTTManagerTest.kt`、`.../Network/ConnectionManagerTest.kt`、`.../ViewModel/MainViewModelTest.kt`（补构造参数）

- [ ] **步骤 1：写失败测试（PTTManager）**

`PTTManagerTest.kt` 的 Harness 加计数与构造参数：

```kotlin
        var heartbeats = 0
        ...
            sendHeartbeat = { heartbeats++ },
```

追加测试：

```kotlin
    @Test fun `keying sends heartbeat immediately then every 500ms`() = runTest {
        val h = Harness(StandardTestDispatcher(testScheduler))
        try {
            h.manager.press()
            runCurrent()                       // 心跳协程在虚拟时间 0ms 启动即首发
            assertEquals(1, h.heartbeats)
            advanceTimeBy(500); runCurrent()
            assertEquals(2, h.heartbeats)
            advanceTimeBy(1000); runCurrent()
            assertEquals(4, h.heartbeats)
        } finally {
            h.manager.forceRelease()           // 断言失败也不能把心跳留在调度器上（否则 drain 死循环）
        }
    }

    @Test fun `release stops the heartbeat`() = runTest {
        val h = Harness(StandardTestDispatcher(testScheduler))
        try {
            h.manager.press()
            runCurrent()                       // 先让首发真的发生
            assertEquals(1, h.heartbeats)
            h.manager.release()
            advanceTimeBy(2000); runCurrent()
            assertEquals(1, h.heartbeats)      // 释放后不再有新的心跳
        } finally {
            h.manager.forceRelease()
        }
    }

    @Test fun `forceRelease stops the heartbeat`() = runTest {
        val h = Harness(StandardTestDispatcher(testScheduler))
        try {
            h.manager.press()
            runCurrent()
            assertEquals(1, h.heartbeats)
            h.manager.forceRelease()
            advanceTimeBy(2000); runCurrent()
            assertEquals(1, h.heartbeats)
        } finally {
            h.manager.forceRelease()
        }
    }
```

- [ ] **步骤 2：运行验证失败**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*PTTManagerTest*'
```

预期：编译失败（`sendHeartbeat` 参数不存在）。

- [ ] **步骤 3：实现**

`WsCommands.kt` 加：

```kotlin
    fun txhb(): String = """{"type":"txhb"}"""
```

`ConnectionManager.kt` 在 `sendPing` 旁加：

```kotlin
    fun sendHeartbeat() = dispatch(WsCommands.txhb())
```

`PTTManager.kt`：

- 构造参数 `val sendTXAudioStop: () -> Unit,` 之后加 `val sendHeartbeat: () -> Unit,`
- 字段区加：

```kotlin
    var heartbeatIntervalMs: Long = 500
    private var heartbeatJob: Job? = null
```

- `press()` 中 `startTxAudio()` 之后加 `startHeartbeat()`
- `release()` 中 `sendPTT(false)` 之后加 `stopHeartbeat()`
- `forceRelease()` 中 `sendPTT(false)` 之后加 `stopHeartbeat()`
- 文件末尾加：

```kotlin
    private fun startHeartbeat() {
        stopHeartbeat()
        heartbeatJob = scope.launch {
            while (true) {
                sendHeartbeat()               // 首发立即，声明能力（server.py:1768 语义）
                delay(heartbeatIntervalMs)
            }
        }
    }

    private fun stopHeartbeat() {
        heartbeatJob?.cancel()
        heartbeatJob = null
    }
```

`ServiceLocator.kt` PTTManager 构造中加 `sendHeartbeat = { cm.sendHeartbeat() },`。
`MainViewModelTest.kt` 的 PTTManager spy 构造中加 `sendHeartbeat = {},`。

- [ ] **步骤 4：运行验证通过**

```bash
cd FT710Android && ./gradlew test
```

预期：38 个测试全绿。

- [ ] **步骤 5：ConnectionManager 路由测试**

`ConnectionManagerTest.kt` 追加：

```kotlin
    @Test fun `sendHeartbeat routes txhb on the control channel`() {
        val sent = mutableListOf<String>()
        val cm = ConnectionManager(OkHttpClient(), CoroutineScope(Dispatchers.Unconfined),
            {}, {}, {}, {}, {}, {}, sendOverride = { sent.add(it) })
        cm.sendHeartbeat()
        assertEquals(listOf("""{"type":"txhb"}"""), sent)
    }
```

运行 `./gradlew :app:testDebugUnitTest --tests '*ConnectionManagerTest*'`，预期全绿。

- [ ] **步骤 6：Commit**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add FT710Android
git commit -m "feat(android): tx-phase liveness heartbeat (txhb) on the keying session"
```

---

### 任务 5：4003 只读登录处理

**文件：**

- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/Network/WebSocketConnection.kt`
- 修改：`.../Network/ConnectionManager.kt`
- 修改：`.../ViewModel/MainViewModel.kt`
- 修改：`.../App/ServiceLocator.kt`
- 修改：`.../UI/MainScreen.kt`
- 测试：`.../Network/WebSocketConnectionTest.kt`、`.../ViewModel/MainViewModelTest.kt`

- [ ] **步骤 1：写失败测试（关闭码上抛）**

`WebSocketConnectionTest.kt` 追加：

```kotlin
    @Test fun `reports peer close code`() {
        val codes = Collections.synchronizedList(mutableListOf<Int>())
        val latch = CountDownLatch(1)
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse =
                MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        webSocket.close(4003, "listen only")
                    }
                })
        }
        val conn = WebSocketConnection(
            client = OkHttpClient(),
            url = server.url("/WSaudioTX?token=t").toString(),
            onText = {}, onBinary = {}, onStateChange = {},
            onClosedCode = { codes.add(it); latch.countDown() },
        )
        conn.connect()
        assertTrue(latch.await(3, TimeUnit.SECONDS))
        assertEquals(4003, codes.first())
        Thread.sleep(300) // 让 close 握手完成，避免 MockWebServer.shutdown 卡队列
    }
```

- [ ] **步骤 2：运行验证失败**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*WebSocketConnectionTest*'
```

预期：编译失败（`onClosedCode` 参数不存在）。

- [ ] **步骤 3：实现 WebSocketConnection**

构造参数末尾加 `private val onClosedCode: (Int) -> Unit = {},`；listener 里：

```kotlin
            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, reason)   // 完成关闭握手，确保 onClosed 到达
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                onClosedCode(code)
                onStateChange(State.Failed)
            }
```

- [ ] **步骤 4：实现 ConnectionManager**

- 构造参数末尾加 `private val onListenOnly: () -> Unit = {},`
- 状态区加：

```kotlin
    @Volatile var listenOnly: Boolean = false; private set
```

- `start()` 中 TX 与 ATR 两路连接加 `onClosedCode = ::handleCloseCode`（其余三路不带）
- `connect()` 签名加 `onClosedCode: (Int) -> Unit = {}` 并透传给 `WebSocketConnection`
- 加：

```kotlin
    private fun handleCloseCode(code: Int) {
        if (code == 4003 && !listenOnly) {
            listenOnly = true
            connectedFlags.remove("/WSaudioTX")
            updateConnected()
            onListenOnly()
        }
    }
```

- `updateConnected()` 判据改为：

```kotlin
        val required = if (listenOnly) setOf("/WSradio", "/WSaudioRX", "/WSspectrum")
                       else setOf("/WSradio", "/WSaudioRX", "/WSaudioTX", "/WSspectrum")
        val all = required.all { it in connectedFlags }
```

- `stopAll()` 中 `connectedFlags.clear()` 旁加 `listenOnly = false`

- [ ] **步骤 5：MainViewModel 与 ServiceLocator**

`MainViewModel.kt` 加：

```kotlin
    private val _listenOnly = MutableStateFlow(false)
    val listenOnly: StateFlow<Boolean> = _listenOnly

    fun onListenOnly() { _listenOnly.value = true }
```

`connect()` 成功后加 `_listenOnly.value = false`；`logout()`/`disconnect()` 加 `_listenOnly.value = false`。
`ServiceLocator.kt` ConnectionManager 构造加 `onListenOnly = { vm.onListenOnly() },`。

`MainViewModelTest.kt` 补构造参数（`onListenOnly` 有默认值，可不动）与用例：

```kotlin
    @Test fun `listen only event flips the flow`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val vm = MainViewModel(null, cm(scope), null, null, null, null, null, scope)
        assertFalse(vm.listenOnly.value)
        vm.onListenOnly()
        assertTrue(vm.listenOnly.value)
    }
```

- [ ] **步骤 6：UI 降级**

`MainScreen.kt` 读 `val listenOnly by vm.listenOnly.collectAsState()`；在顶栏下加提示条；底部 `TUNE | CQ | PTT` 整行与 REC 入口在 `listenOnly` 时隐藏：

```kotlin
        if (listenOnly) {
            Text("只读登录（listen-only）：发射与设备设置已被服务端禁用", fontSize = 12.sp, color = Color(0xFFE67E22))
        }
```

（底部行的 if 包裹在任务 8 一并完成；本任务先做读状态与提示条。）

- [ ] **步骤 7：全量测试 + 构建**

```bash
cd FT710Android && ./gradlew test assembleDebug
```

预期：40 个测试全绿、`BUILD SUCCESSFUL`。

- [ ] **步骤 8：Commit**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add FT710Android
git commit -m "feat(android): handle listen-only 4003 closes instead of failing silently"
```

---

### 任务 6：录音与 CQ 协议数据层

**文件：**

- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/Network/Protocol.kt`
- 新增：`.../Network/RecordingsApi.kt`
- 修改：`.../ViewModel/MainViewModel.kt`
- 修改：`.../App/ServiceLocator.kt`
- 测试：`.../Network/ProtocolTest.kt`、`.../Network/RecordingsApiTest.kt`（新增）、`.../ViewModel/MainViewModelTest.kt`

- [ ] **步骤 1：写失败测试**

`ProtocolTest.kt` 追加：

```kotlin
    @Test fun `parses recordingState`() {
        val ev = parseWsEvent(
            """{"type":"recordingState","recording":{"recording":true,"freq_hz":7050000,"started_at":"2026-10-03T12:00:00","duration":12.5,"name":"7050000kHz_20261003_120000.mp3","bytes":123456,"dropped":0}}"""
        )
        assertTrue(ev is WsEvent.RecordingState)
        ev as WsEvent.RecordingState
        assertEquals(7050000L, ev.status.freqHz)
        assertEquals(true, ev.status.recording)
    }

    @Test fun `parses cqState`() {
        val ev = parseWsEvent(
            """{"type":"cqState","cq":{"state":"calling","duration_s":6.1,"elapsed_s":2.0,"frames_total":305,"frames_sent":100,"started_by":"abc123","reason":null,"ready":true}}"""
        )
        assertTrue(ev is WsEvent.CqState)
        assertEquals("calling", (ev as WsEvent.CqState).status.state)
    }

    @Test fun `fullState exposes recording and cq capability`() {
        val withFeatures = parseWsEvent(
            """{"type":"fullState","data":{},"bands":[],"modes":[],"memChannels":[],"recording":{"recording":false},"cq":{"state":"idle"},"radioModel":"ft710"}"""
        ) as WsEvent.FullState
        assertEquals(false, withFeatures.recording?.recording)
        assertEquals("idle", withFeatures.cq?.state)
        val without = parseWsEvent(
            """{"type":"fullState","data":{},"bands":[],"modes":[],"memChannels":[]}"""
        ) as WsEvent.FullState
        assertEquals(null, without.recording)
        assertEquals(null, without.cq)
    }
```

新增 `RecordingsApiTest.kt`：

```kotlin
package com.hamradio.ft710android.Network

import org.junit.Assert.assertEquals
import org.junit.Test

class RecordingsApiTest {
    @Test fun `parses list payload`() {
        val rows = parseRecordingsList(
            """{"recordings":[{"name":"7050000kHz_20261003_120000.mp3","freq_hz":7050000,"started_at":"2026-10-03T12:00:00","duration":61.0,"bytes":488123,"recording":false}],"count":1,"total_bytes":488123}"""
        )
        assertEquals(1, rows.size)
        assertEquals("7050000kHz_20261003_120000.mp3", rows[0].name)
        assertEquals(7050000L, rows[0].freqHz)
        assertEquals(61.0, rows[0].duration, 0.001)
    }

    @Test fun `malformed payload yields empty list`() {
        assertEquals(emptyList<RecordingRow>(), parseRecordingsList("<html>not json</html>"))
    }
}
```

- [ ] **步骤 2：运行验证失败**

```bash
cd FT710Android && ./gradlew :app:testDebugUnitTest --tests '*ProtocolTest*' --tests '*RecordingsApiTest*'
```

预期：编译失败（类型不存在）。

- [ ] **步骤 3：实现 Protocol.kt**

加入（顶部 import 已含所需 json 工具）：

```kotlin
@Serializable
data class RecordingStatusDto(
    val recording: Boolean = false,
    @SerialName("freq_hz") val freqHz: Long = 0,
    @SerialName("started_at") val startedAt: String? = null,
    val duration: Double = 0.0,
    val name: String? = null,
    val bytes: Long = 0,
    val dropped: Int = 0,
)

@Serializable
data class CqStatusDto(
    val state: String = "idle",
    @SerialName("duration_s") val durationS: Double = 0.0,
    @SerialName("elapsed_s") val elapsedS: Double = 0.0,
    @SerialName("frames_total") val framesTotal: Int = 0,
    @SerialName("frames_sent") val framesSent: Int = 0,
    @SerialName("started_by") val startedBy: String? = null,
    val reason: String? = null,
    val ready: Boolean = false,
)
```

`FullStateDto` 追加字段：

```kotlin
    val recording: RecordingStatusDto? = null,
    val cq: CqStatusDto? = null,
    val radioModel: String? = null,
```

新增消息 DTO：

```kotlin
@Serializable
data class RecordingStateDto(val type: String = "recordingState", val recording: RecordingStatusDto = RecordingStatusDto())

@Serializable
data class CqStateDto(val type: String = "cqState", val cq: CqStatusDto = CqStatusDto())
```

`WsEvent`：

- `FullState` 加 `val recording: RecordingStatusDto?, val cq: CqStatusDto?, val radioModel: String?`
- 加 `data class RecordingState(val status: RecordingStatusDto) : WsEvent()` 与 `data class CqState(val status: CqStatusDto) : WsEvent()`

`parseWsEvent`：fullState 分支传新字段；新增：

```kotlin
        "recordingState" -> runCatching {
            WsEvent.RecordingState(json.decodeFromString<RecordingStateDto>(text).recording)
        }.getOrElse { WsEvent.Unknown }
        "cqState" -> runCatching {
            WsEvent.CqState(json.decodeFromString<CqStateDto>(text).cq)
        }.getOrElse { WsEvent.Unknown }
```

- [ ] **步骤 4：实现 RecordingsApi.kt**

```kotlin
package com.hamradio.ft710android.Network

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File

@Serializable
data class RecordingRow(
    val name: String = "",
    @SerialName("freq_hz") val freqHz: Long = 0,
    @SerialName("started_at") val startedAt: String = "",
    val duration: Double = 0.0,
    val bytes: Long = 0,
    val recording: Boolean = false,
)

@Serializable
data class RecordingsListDto(
    val recordings: List<RecordingRow> = emptyList(),
    val count: Int = 0,
    @SerialName("total_bytes") val totalBytes: Long = 0,
)

private val recordingsJson = Json { ignoreUnknownKeys = true }

/** 纯解析（JVM 可测）：畸形响应返回空列表而不是抛异常。 */
fun parseRecordingsList(text: String): List<RecordingRow> =
    runCatching { recordingsJson.decodeFromString<RecordingsListDto>(text).recordings }
        .getOrElse { emptyList() }

/** 录音 REST（AD-017）：列表 / 下载 / 删除。认证用登录 Cookie（与 AuthApi.logout 一致）。 */
class RecordingsApi(private val client: OkHttpClient) {
    suspend fun list(baseUrl: String, token: String): List<RecordingRow> = withContext(Dispatchers.IO) {
        val req = Request.Builder().url("$baseUrl/api/recordings")
            .header("Cookie", "ft710_auth=$token").get().build()
        runCatching {
            client.newCall(req).execute().use { resp ->
                if (resp.code == 200) parseRecordingsList(resp.body?.string().orEmpty()) else emptyList()
            }
        }.getOrElse { emptyList() }
    }

    suspend fun delete(baseUrl: String, token: String, name: String): Boolean = withContext(Dispatchers.IO) {
        val req = Request.Builder().url("$baseUrl/api/recordings/$name")
            .header("Cookie", "ft710_auth=$token").delete().build()
        runCatching { client.newCall(req).execute().use { it.code == 200 } }.getOrElse { false }
    }

    suspend fun download(baseUrl: String, token: String, name: String, dest: File): File? =
        withContext(Dispatchers.IO) {
            val req = Request.Builder().url("$baseUrl/api/recordings/$name")
                .header("Cookie", "ft710_auth=$token").get().build()
            runCatching {
                client.newCall(req).execute().use { resp ->
                    if (resp.code != 200) return@use null
                    dest.parentFile?.mkdirs()
                    resp.body?.byteStream()?.use { input -> dest.outputStream().use { input.copyTo(it) } }
                    dest
                }
            }.getOrNull()
        }
}
```

- [ ] **步骤 5：MainViewModel 状态与动作**

构造参数末尾（`scope` 之后）加 `private val recordingsApi: RecordingsApi? = null`。
导入 `kotlinx.coroutines.launch`、`java.io.File`。
字段区加：

```kotlin
    private val _recordings = MutableStateFlow<List<RecordingRow>>(emptyList())
    val recordings: StateFlow<List<RecordingRow>> = _recordings
    private val _recordingState = MutableStateFlow(RecordingStatusDto())
    val recordingState: StateFlow<RecordingStatusDto> = _recordingState
    private val _recordingsAvailable = MutableStateFlow(false)
    val recordingsAvailable: StateFlow<Boolean> = _recordingsAvailable
    private val _cq = MutableStateFlow<CqStatusDto?>(null)
    val cq: StateFlow<CqStatusDto?> = _cq
    private val _cqAvailable = MutableStateFlow(false)
    val cqAvailable: StateFlow<Boolean> = _cqAvailable
    private var baseUrl: String? = null
    private var token: String? = null
```

`onWsEvent` 的 `FullState` 分支追加：

```kotlin
                _recordingsAvailable.value = ev.recording != null
                _cqAvailable.value = ev.cq != null
                ev.recording?.let { _recordingState.value = it }
                ev.cq?.let { _cq.value = it }
```

`when(ev)` 加分支：

```kotlin
            is WsEvent.RecordingState -> _recordingState.value = ev.status
            is WsEvent.CqState -> _cq.value = ev.status
```

`connect()` 成功分支加 `baseUrl = base; token = res.token`；`logout()`/`disconnect()` 清空两者并重置 availability。
动作区加：

```kotlin
    fun startRecording() = sendSet("recording", true)
    fun stopRecording() = sendSet("recording", false)
    fun startCq() = sendSet("cq", true)
    fun abortCq() = sendSet("cq", false)

    fun refreshRecordings() {
        val api = recordingsApi ?: return
        val base = baseUrl ?: return
        val t = token ?: return
        scope.launch { _recordings.value = api.list(base, t) }
    }

    fun deleteRecording(name: String) {
        val api = recordingsApi ?: return
        val base = baseUrl ?: return
        val t = token ?: return
        scope.launch { if (api.delete(base, t, name)) refreshRecordings() }
    }

    suspend fun downloadRecording(name: String, destDir: File): File? {
        val api = recordingsApi ?: return null
        val base = baseUrl ?: return null
        val t = token ?: return null
        return api.download(base, t, name, File(destDir, name))
    }

    fun showError(message: String) { _error.value = message }
    fun clearError() { _error.value = null }
```

- [ ] **步骤 6：ServiceLocator 装配**

`val recordingsApi = RecordingsApi(client)` 并作为 `MainViewModel(..., recordingsApi = recordingsApi)` 传入（用命名参数）。

- [ ] **步骤 7：MainViewModelTest 补能力检测用例**

```kotlin
    @Test fun `fullState with recording and cq marks them available`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val vm = MainViewModel(null, cm(scope), null, null, null, null, null, scope)
        vm.onWsEvent(parseWsEvent(
            """{"type":"fullState","data":{},"bands":[],"modes":[],"memChannels":[],"recording":{"recording":false},"cq":{"state":"idle"}}"""
        ))
        assertTrue(vm.recordingsAvailable.value)
        assertTrue(vm.cqAvailable.value)
    }
```

- [ ] **步骤 8：运行验证通过**

```bash
cd FT710Android && ./gradlew test
```

预期：46 个测试全绿。

- [ ] **步骤 9：Commit**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add FT710Android
git commit -m "feat(android): recording/CQ protocol layer with capability detection"
```

---

### 任务 7：录音面板 UI（列表 / 播放 / 导出 / 删除）

**文件：**

- 新增：`FT710Android/app/src/main/java/com/hamradio/ft710android/UI/RecordingPanel.kt`
- 新增：`.../UI/Format.kt`
- 修改：`.../UI/MainScreen.kt`
- 修改：`.../AndroidManifest.xml`、新增 `res/xml/file_paths.xml`
- 修改：`app/build.gradle.kts`、`gradle/libs.versions.toml`

- [ ] **步骤 1：依赖与 FileProvider 声明**

`libs.versions.toml` `[versions]` 加 `coreKtx = "1.15.0"`；`[libraries]` 加：

```toml
androidx-core-ktx = { group = "androidx.core", name = "core-ktx", version.ref = "coreKtx" }
```

`app/build.gradle.kts` dependencies 加 `implementation(libs.androidx.core.ktx)`。

`AndroidManifest.xml` `<application>` 内加：

```xml
        <provider
            android:name="androidx.core.content.FileProvider"
            android:authorities="${applicationId}.fileprovider"
            android:exported="false"
            android:grantUriPermissions="true">
            <meta-data
                android:name="android.support.FILE_PROVIDER_PATHS"
                android:resource="@xml/file_paths" />
        </provider>
```

`res/xml/file_paths.xml`：

```xml
<?xml version="1.0" encoding="utf-8"?>
<paths>
    <cache-path name="recordings" path="recordings/" />
</paths>
```

- [ ] **步骤 2：Format.kt**

```kotlin
package com.hamradio.ft710android.UI

import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

fun fmtSeconds(s: Double): String {
    val total = s.toInt().coerceAtLeast(0)
    return "%d:%02d".format(Locale.US, total / 60, total % 60)
}

fun fmtBytes(bytes: Long): String =
    if (bytes >= 1_048_576) "%.1f MB".format(Locale.US, bytes / 1_048_576.0)
    else "%.0f KB".format(Locale.US, bytes / 1024.0)

/** 服务端 started_at 是 `2026-10-03T12:00:00`（recorder.py isoformat(timespec="seconds")）。 */
fun fmtStartedAt(iso: String): String = runCatching {
    LocalDateTime.parse(iso).format(DateTimeFormatter.ofPattern("MM-dd HH:mm"))
}.getOrDefault(iso)
```

- [ ] **步骤 3：RecordingPanel.kt**

```kotlin
package com.hamradio.ft710android.UI

import android.content.Intent
import android.media.MediaPlayer
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.core.content.FileProvider
import com.hamradio.ft710android.Network.RecordingRow
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.io.File

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun RecordingPanel(vm: MainViewModel, onClose: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    vm.version.collectAsState()
    val rec by vm.recordingState.collectAsState()
    val rows by vm.recordings.collectAsState()
    val listenOnly by vm.listenOnly.collectAsState()

    var playing by remember { mutableStateOf<String?>(null) }
    var playbackPos by remember { mutableStateOf(0) }
    var playbackDur by remember { mutableStateOf(0) }
    var pendingDelete by remember { mutableStateOf<RecordingRow?>(null) }
    var busy by remember { mutableStateOf(false) }
    val player = remember { MediaPlayer() }

    LaunchedEffect(Unit) { vm.refreshRecordings() }
    DisposableEffect(Unit) { onDispose { player.release() } }
    LaunchedEffect(playing) {
        while (playing != null) {
            playbackPos = runCatching { player.currentPosition }.getOrDefault(0)
            playbackDur = runCatching { player.duration }.getOrDefault(0)
            delay(500)
        }
    }

    fun stopPlayback() {
        runCatching { player.stop() }
        playing = null
    }

    fun play(row: RecordingRow) {
        scope.launch {
            busy = true
            val file = vm.downloadRecording(row.name, File(context.cacheDir, "recordings"))
            busy = false
            if (file == null) { vm.showError("下载失败：${row.name}"); return@launch }
            runCatching {
                player.reset()
                player.setDataSource(file.absolutePath)
                player.prepare()
                player.start()
                playing = row.name
                playbackPos = 0
            }.onFailure { vm.showError("播放失败：${it.message}") }
        }
    }

    fun export(row: RecordingRow) {
        scope.launch {
            busy = true
            val file = vm.downloadRecording(row.name, File(context.cacheDir, "recordings"))
            busy = false
            if (file == null) { vm.showError("下载失败：${row.name}"); return@launch }
            val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
            val intent = Intent(Intent.ACTION_SEND).apply {
                type = "audio/mpeg"
                putExtra(Intent.EXTRA_STREAM, uri)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            context.startActivity(Intent.createChooser(intent, "导出录音"))
        }
    }

    Dialog(onDismissRequest = onClose, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
            Column(Modifier.fillMaxSize().padding(16.dp)) {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text("录音", style = MaterialTheme.typography.titleLarge)
                    Spacer(Modifier.weight(1f))
                    TextButton(onClick = { vm.refreshRecordings() }) { Text("刷新") }
                    TextButton(onClick = onClose) { Text("关闭") }
                }
                HorizontalDivider()
                Row(Modifier.fillMaxWidth().padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    if (rec.recording) {
                        Text("● 录制中 ${fmtSeconds(rec.duration)} · ${fmtBytes(rec.bytes)}", color = Color(0xFFE53935), fontSize = 13.sp)
                    } else {
                        Text("未在录音", fontSize = 13.sp)
                    }
                    Spacer(Modifier.weight(1f))
                    if (!listenOnly) {
                        Button(onClick = { if (rec.recording) vm.stopRecording() else vm.startRecording() }) {
                            Text(if (rec.recording) "停止" else "开始录音")
                        }
                    }
                }
                playing?.let { name ->
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Text("▶ ${fmtSeconds(playbackPos / 1000.0)} / ${fmtSeconds(playbackDur / 1000.0)}", fontSize = 12.sp)
                        Spacer(Modifier.width(8.dp))
                        Text(name, fontSize = 11.sp, modifier = Modifier.weight(1f))
                        TextButton(onClick = { if (player.isPlaying) player.pause() else player.start() }) {
                            Text(if (player.isPlaying) "暂停" else "继续")
                        }
                        TextButton(onClick = { stopPlayback() }) { Text("停止") }
                    }
                }
                if (busy) Text("处理中…", fontSize = 12.sp, color = Color(0xFFE67E22))
                LazyColumn(Modifier.weight(1f)) {
                    items(rows, key = { it.name }) { row ->
                        Column(
                            Modifier.fillMaxWidth()
                                .combinedClickable(
                                    onClick = { if (playing == row.name) stopPlayback() else play(row) },
                                    onLongClick = { if (!row.recording) pendingDelete = row },
                                )
                                .padding(vertical = 8.dp)
                        ) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text("${row.freqHz / 1000} kHz", fontSize = 14.sp, modifier = Modifier.weight(1f))
                                Text(fmtStartedAt(row.startedAt), fontSize = 12.sp)
                                Spacer(Modifier.width(8.dp))
                                Text(fmtSeconds(row.duration), fontSize = 12.sp)
                                Spacer(Modifier.width(8.dp))
                                Text(fmtBytes(row.bytes), fontSize = 12.sp)
                                if (row.recording) {
                                    Text("  ●", color = Color(0xFFE53935), fontSize = 12.sp)
                                } else {
                                    TextButton(onClick = { export(row) }) { Text("导出", fontSize = 12.sp) }
                                }
                            }
                            Text("长按删除", fontSize = 10.sp, color = Color(0xFF6B7280))
                        }
                        HorizontalDivider()
                    }
                }
            }
        }
    }

    pendingDelete?.let { row ->
        AlertDialog(
            onDismissRequest = { pendingDelete = null },
            title = { Text("删除录音") },
            text = { Text("确定删除 ${row.name} ？服务端文件将被永久删除。") },
            confirmButton = {
                TextButton(onClick = { vm.deleteRecording(row.name); pendingDelete = null }) { Text("删除") }
            },
            dismissButton = { TextButton(onClick = { pendingDelete = null }) { Text("取消") } },
        )
    }
}
```

- [ ] **步骤 4：MainScreen 接入**

`MainScreen.kt` 加状态与入口：

```kotlin
    val rec by vm.recordingState.collectAsState()
    val recAvailable by vm.recordingsAvailable.collectAsState()
    var showRecPanel by remember { mutableStateOf(false) }
```

在仪表行（`MeterRow("ALC", ...)`）之后加：

```kotlin
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            if (recAvailable && !listenOnly) {
                TextButton(onClick = { showRecPanel = true }) {
                    Text(
                        if (rec.recording) "● REC ${fmtSeconds(rec.duration)}" else "录音",
                        color = if (rec.recording) Color(0xFFE53935) else Color.Unspecified,
                    )
                }
            }
        }
        if (showRecPanel) RecordingPanel(vm) { showRecPanel = false }
```

补 import：`androidx.compose.runtime.mutableStateOf`、`remember`、`setValue`、`androidx.compose.ui.unit.sp` 已有、`RecordingPanel` 同包免 import。

- [ ] **步骤 5：构建验证**

```bash
cd FT710Android && ./gradlew test assembleDebug lintDebug
```

预期：全部通过（lint 无 error）。

- [ ] **步骤 6：Commit**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add FT710Android
git commit -m "feat(android): recording panel — list, local playback, export, delete"
```

---

### 任务 8：CQ 一键呼叫按钮

**文件：**

- 修改：`FT710Android/app/src/main/java/com/hamradio/ft710android/UI/MainScreen.kt`

- [ ] **步骤 1：接线状态**

`MainScreen.kt` 加：

```kotlin
    val cq by vm.cq.collectAsState()
    val cqAvailable by vm.cqAvailable.collectAsState()
    val error by vm.error.collectAsState()
```

- [ ] **步骤 2：错误条（含 CQ 服务端拒绝原因）**

顶栏下加：

```kotlin
        error?.let { msg ->
            Text(msg, color = Color(0xFFE53935), fontSize = 12.sp)
            LaunchedEffect(msg) { delay(4000); vm.clearError() }
        }
```

（import `kotlinx.coroutines.delay`、`androidx.compose.runtime.LaunchedEffect`。）

- [ ] **步骤 3：底部行加 CQ**

底部 Row 改为（并把整行包进 `if (!listenOnly)`）：

```kotlin
        if (!listenOnly) {
            Row(Modifier.fillMaxWidth().height(72.dp), verticalAlignment = Alignment.CenterVertically) {
                Button(
                    onClick = { vm.sendSet("tune", state.tunerStatus == 0) },
                    modifier = Modifier.weight(0.3f).fillMaxSize(),
                ) { Text("TUNE") }
                Spacer(Modifier.width(8.dp))
                if (cqAvailable) {
                    val calling = cq?.state == "calling"
                    Button(
                        onClick = { if (calling) vm.abortCq() else vm.startCq() },
                        modifier = Modifier.weight(0.3f).fillMaxSize(),
                        colors = if (calling) androidx.compose.material3.ButtonDefaults.buttonColors(containerColor = Color(0xFFE53935))
                                 else androidx.compose.material3.ButtonDefaults.buttonColors(),
                    ) {
                        val s = cq
                        Text(if (calling && s != null) "CQ %.0f/%.0fs".format(s.elapsedS, s.durationS) else "CQ")
                    }
                }
                Spacer(Modifier.width(8.dp))
                vm.pttManager?.let { PTTButton(it, Modifier.weight(0.5f).fillMaxSize()) }
            }
        }
```

（import `androidx.compose.material3.ButtonDefaults`、`java.util.Locale` 已有 `format` 是 `String.format`——`"…".format(...)` 走 Kotlin 扩展，需要 `Locale` 无关；直接用 `"CQ %.0f/%.0fs".format(s.elapsedS, s.durationS)`。）

- [ ] **步骤 4：构建验证**

```bash
cd FT710Android && ./gradlew assembleDebug lintDebug
```

预期：`BUILD SUCCESSFUL`。

- [ ] **步骤 5：Commit**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add FT710Android
git commit -m "feat(android): one-touch CQ key with progress and abort"
```

---

### 任务 9：品牌 / 图标 / 版本号 / 登录提示

**文件：**

- 修改：`FT710Android/app/src/main/res/values/strings.xml`
- 修改：`.../UI/LoginScreen.kt`
- 修改：`app/build.gradle.kts`
- 新增：`res/values/colors.xml`、`res/drawable/ic_launcher_foreground.xml`、`res/mipmap-anydpi-v26/ic_launcher.xml`、`res/mipmap-anydpi-v26/ic_launcher_round.xml`
- 修改：`AndroidManifest.xml`

- [ ] **步骤 1：字符串与版本号**

`strings.xml`：`<string name="app_name">MRRC Modern</string>`
`app/build.gradle.kts`：`versionName = "1.0.0"`（`versionCode` 保持 1）。

- [ ] **步骤 2：登录页**

`LoginScreen.kt`：

- `Text("FT-710 Control", ...)` → `Text("MRRC Modern", ...)`
- 密码框之后加：

```kotlin
        Text("局域网：主机 192.168.x.x / 端口 8888；云端：主机 <呼号>.mrrc.vlsc.net / 端口 443",
            style = MaterialTheme.typography.bodySmall)
```

- [ ] **步骤 3：自适应图标**

`res/values/colors.xml`：

```xml
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <color name="ic_launcher_background">#050A08</color>
</resources>
```

`res/drawable/ic_launcher_foreground.xml`：

```xml
<?xml version="1.0" encoding="utf-8"?>
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="108dp" android:height="108dp"
    android:viewportWidth="108" android:viewportHeight="108">
    <path android:fillColor="#00FF41"
        android:pathData="M54,66 m-4.5,0 a4.5,4.5 0 1,0 9,0 a4.5,4.5 0 1,0 -9,0" />
    <path android:strokeColor="#00FF41" android:strokeWidth="5"
        android:strokeLineCap="round" android:fillColor="#00000000"
        android:pathData="M40,66 A14,14 0 0 1 68,66" />
    <path android:strokeColor="#00FF41" android:strokeWidth="5"
        android:strokeLineCap="round" android:fillColor="#00000000"
        android:pathData="M30,66 A24,24 0 0 1 78,66" />
</vector>
```

`res/mipmap-anydpi-v26/ic_launcher.xml` 与 `ic_launcher_round.xml`（内容相同）：

```xml
<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@color/ic_launcher_background" />
    <foreground android:drawable="@drawable/ic_launcher_foreground" />
</adaptive-icon>
```

`AndroidManifest.xml` `<application>` 加 `android:icon="@mipmap/ic_launcher"` 与 `android:roundIcon="@mipmap/ic_launcher_round"`。

- [ ] **步骤 4：构建验证**

```bash
cd FT710Android && ./gradlew test assembleDebug lintDebug
```

预期：全绿（lint 对图标无 error；minSdk 26 与 anydpi-v26 匹配）。

- [ ] **步骤 5：Commit**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add FT710Android
git commit -m "feat(android): rebrand to MRRC Modern, add adaptive icon, v1.0.0, login hints"
```

---

### 任务 10：签名链路

**文件：**

- 修改：`FT710Android/app/build.gradle.kts`
- 修改：`FT710Android/.gitignore`
- 新增（仓库外）：`~/.android-keystores/mrrc-modern-release.jks`
- 新增（gitignore）：`FT710Android/keystore.properties`

- [ ] **步骤 1：生成 keystore 与 properties（一次性）**

```bash
mkdir -p "$HOME/.android-keystores"
KS="$HOME/.android-keystores/mrrc-modern-release.jks"
if [ ! -f "$KS" ]; then
  STOREPASS=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 24)
  keytool -genkeypair -keystore "$KS" -alias mrrc-modern \
    -keyalg RSA -keysize 4096 -validity 10000 \
    -storepass "$STOREPASS" -keypass "$STOREPASS" \
    -dname "CN=MRRC Modern, OU=Amateur Radio, O=MRRC, C=CN"
  cat > /Users/cheenle/HAM/hub/mrrc_modern/FT710Android/keystore.properties <<EOF
storeFile=$KS
storePassword=$STOREPASS
keyAlias=mrrc-modern
keyPassword=$STOREPASS
EOF
  chmod 600 /Users/cheenle/HAM/hub/mrrc_modern/FT710Android/keystore.properties
  echo "== 把口令抄给用户备份：$STOREPASS =="
fi
```

⚠️ **口令必须让用户当场记下**；`keystore.properties` 与 `.jks` 都不进 git。

- [ ] **步骤 2：.gitignore**

`FT710Android/.gitignore` 追加：

```
/keystore.properties
```

- [ ] **步骤 3：Gradle 签名配置**

`app/build.gradle.kts` 顶部加 `import java.util.Properties`，plugins 块后加：

```kotlin
val keystorePropsFile = rootProject.file("keystore.properties")
val keystoreProps = Properties().apply {
    if (keystorePropsFile.exists()) keystorePropsFile.inputStream().use { load(it) }
}
```

`android {}` 内加：

```kotlin
    signingConfigs {
        create("release") {
            if (keystorePropsFile.exists()) {
                storeFile = file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }
```

`buildTypes.release` 内加 `signingConfig = if (keystorePropsFile.exists()) signingConfigs.getByName("release") else null`。
文件末尾加：

```kotlin
tasks.named("assembleRelease") {
    doFirst {
        if (!keystorePropsFile.exists()) {
            throw GradleException(
                "keystore.properties missing — a signed release requires " +
                    "~/.android-keystores/mrrc-modern-release.jks (see BUILD_GUIDE.md)"
            )
        }
    }
}
```

- [ ] **步骤 4：验证签名与「无密钥即失败」**

```bash
cd FT710Android
./gradlew assembleRelease
"$ANDROID_HOME/build-tools/35.0.0/apksigner" verify --print-certs app/build/outputs/apk/release/app-release.apk | head -6
# 反证：临时改名 properties，构建必须失败
mv keystore.properties keystore.properties.bak
./gradlew assembleRelease; echo "exit=$?"   # 预期非 0，且报 keystore.properties missing
mv keystore.properties.bak keystore.properties
```

- [ ] **步骤 5：Commit**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add FT710Android/app/build.gradle.kts FT710Android/.gitignore
git commit -m "build(android): release signing chain with fail-without-keystore guard"
```

---

### 任务 11：release.sh + 站点标记块 + 文档

**文件：**

- 新增：`FT710Android/release.sh`
- 新增：`FT710Android/CHANGELOG.md`
- 修改：`~/HAM/website/mrrc_modern/zh/index.html`、`~/HAM/website/mrrc_modern/index.html`（站点仓库）
- 修改：`mrrc_modern/README.md`（本仓根）
- 修改：`FT710Android/BUILD_GUIDE.md`、`FT710Android/CLAUDE.md`

- [ ] **步骤 1：站点页面一次性改造（zh）**

替换（唯一匹配）：

```html
<li>PTT 状态机从第一版就是一等公民</li>
<li>纯逻辑（协议 / PTT / 频谱）已过 JVM 测试——真机射频验收待完成</li>
</ul>
```

为：

```html
<li>PTT 状态机从第一版就是一等公民</li>
<li>纯逻辑 JVM 测试 + 真机验收；官网签名 APK 直接安装</li>
</ul>
<!-- android-download:start -->
<p><a class="btn btn-primary btn-large" href="../downloads/MRRC-Modern-Android.apk">下载 APK</a></p>
<!-- android-download:end -->
```

标题 `<strong>早期版本——需从源码构建</strong>` → `<strong>iOS 早期版本——需从源码构建</strong>`；
段落中 `实际按键的客户端。Android：纯逻辑（协议、PTT、频谱、记忆频道）\n已通过 <code>./gradlew test assembleDebug lintDebug</code> 的\nJVM 测试；真机射频验收待完成。` → `实际按键的客户端。`

- [ ] **步骤 2：站点页面一次性改造（en）**

替换：

```html
<li>JVM-tested protocol &amp; PTT logic — device RF acceptance pending</li>
</ul>
```

为：

```html
<li>JVM-tested logic plus an on-device acceptance pass — signed APK available</li>
</ul>
<!-- android-download:start -->
<p><a class="btn btn-primary btn-large" href="../downloads/MRRC-Modern-Android.apk">Download APK</a></p>
<!-- android-download:end -->
```

标题 `<strong>Early access — build from source</strong>` → `<strong>iOS early access — build from source</strong>`；
段落 `the keying client. Android: pure logic (protocol, PTT, spectrum,\nmemory channels) is covered by JVM tests via\n<code>./gradlew test assembleDebug lintDebug</code>; on-device RF\nacceptance is pending.` → `the keying client.`

- [ ] **步骤 3：README 下载区（本仓根，静态段）**

在 Windows 下载段之后加（链接指向**稳定别名**，无需随版本更新）：

```markdown
### Android App (APK, sideload)

Android 8.0+ (minSdk 26)。签名 APK 常驻地址（始终为最新版）：

- <https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-Android.apk>

首次安装需在系统设置里允许「安装未知应用」。客户端与浏览器共用同一套协议，
可连局域网实例，也可走 Cloud Hub 呼号入口。
```

- [ ] **步骤 4：release.sh**

```bash
#!/usr/bin/env bash
# MRRC Modern Android 发布脚本：构建 → 签名核对 → 站点上传 → 线上 SHA-256 复核
# 用法: ./release.sh [--version X.Y.Z] [--dry-run] [--skip-tests]
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
WEBSITE_DIR="${WEBSITE_DIR:-$HOME/HAM/website/mrrc_modern}"
REMOTE_USER="cheenle"
REMOTE_HOST="www.vlsc.net"
REMOTE_DOWNLOADS="/var/www/vlsc.net/mrrc_modern/downloads"

VERSION=""; DRY_RUN=0; SKIP_TESTS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-tests) SKIP_TESTS=1; shift ;;
    *) echo "unknown arg: $1"; exit 2 ;;
  esac
done

: "${JAVA_HOME:=$(/usr/libexec/java_home -v 17)}"; export JAVA_HOME
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
[ -d "$ANDROID_HOME" ] || { echo "ANDROID_HOME not found"; exit 1; }
[ -f "$APP_DIR/keystore.properties" ] || { echo "keystore.properties missing (BUILD_GUIDE.md)"; exit 1; }
[ -d "$WEBSITE_DIR" ] || { echo "website dir not found: $WEBSITE_DIR"; exit 1; }

if [ -z "$VERSION" ]; then
  VERSION=$(grep -oE 'versionName = "[0-9]+\.[0-9]+\.[0-9]+"' "$APP_DIR/app/build.gradle.kts" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
fi
[ -n "$VERSION" ] || { echo "cannot determine version"; exit 1; }
echo "== MRRC Modern Android v$VERSION =="

cd "$APP_DIR"
if [ "$SKIP_TESTS" = 1 ]; then ./gradlew lintDebug assembleRelease; else ./gradlew test lintDebug assembleRelease; fi

APK="$APP_DIR/app/build/outputs/apk/release/app-release.apk"
[ -f "$APK" ] || { echo "APK missing: $APK"; exit 1; }

APKSIGNER="$ANDROID_HOME/build-tools/35.0.0/apksigner"
[ -x "$APKSIGNER" ] || APKSIGNER=$(find "$ANDROID_HOME/build-tools" -name apksigner | sort | tail -1)
echo "-- signature --"; "$APKSIGNER" verify --print-certs "$APK" | head -6

VERSIONED="MRRC-Modern-v${VERSION}-Android.apk"
STABLE="MRRC-Modern-Android.apk"
mkdir -p "$APP_DIR/dist"
cp "$APK" "$APP_DIR/dist/$VERSIONED"
cp "$APK" "$APP_DIR/dist/$STABLE"
SIZE=$(stat -f%z "$APP_DIR/dist/$VERSIONED")
SHA=$(shasum -a 256 "$APP_DIR/dist/$VERSIONED" | awk '{print $1}')
echo "-- artifact: $VERSIONED · $SIZE bytes · $SHA --"

mkdir -p "$WEBSITE_DIR/downloads"
cp "$APP_DIR/dist/$VERSIONED" "$WEBSITE_DIR/downloads/$VERSIONED"
cp "$APP_DIR/dist/$STABLE" "$WEBSITE_DIR/downloads/$STABLE"

update_block() {  # $1=页面文件 $2=下载链接前缀
  python3 - "$1" "$2" "$VERSION" "$SIZE" "$SHA" <<'PY'
import re, sys
page, prefix, version, size, sha = sys.argv[1:6]
html = open(page, encoding="utf-8").read()
mb = f"{int(size) / 1048576:.1f} MB"
block = (
    "<!-- android-download:start -->\n"
    f'<p><a class="btn btn-primary btn-large" href="{prefix}MRRC-Modern-Android.apk">'
    f"下载 APK v{version}（Android 8.0+）</a></p>\n"
    f'<p style="color: var(--scope-text-muted); font-size: 0.85rem; margin-top: .5rem;">'
    f"MRRC-Modern-v{version}-Android.apk · {mb} · SHA-256 <code>{sha}</code><br>"
    "安装：系统设置允许「安装未知应用」后点开 APK。</p>\n"
    "<!-- android-download:end -->"
)
pattern = re.compile(r"<!-- android-download:start -->.*?<!-- android-download:end -->", re.S)
if not pattern.search(html):
    sys.exit(f"marker block not found in {page}")
open(page, "w", encoding="utf-8").write(pattern.sub(block, html))
print(f"updated {page}")
PY
}
update_block "$WEBSITE_DIR/zh/index.html" "../downloads/"
update_block "$WEBSITE_DIR/index.html" "../downloads/"

if git -C "$WEBSITE_DIR" diff --quiet -- zh/index.html index.html; then :; else
  git -C "$WEBSITE_DIR" add zh/index.html index.html
  git -C "$WEBSITE_DIR" commit -m "site(mrrc_modern): Android APK v$VERSION download card"
fi

if [ "$DRY_RUN" = 1 ]; then echo "-- dry run: 跳过上传 / 部署 / 线上复核 --"; exit 0; fi

scp "$APP_DIR/dist/$VERSIONED" "$APP_DIR/dist/$STABLE" "$REMOTE_USER@$REMOTE_HOST:~/"
ssh "$REMOTE_USER@$REMOTE_HOST" "sudo mv ~/$VERSIONED ~/$STABLE $REMOTE_DOWNLOADS/ && sudo chown www-data:www-data $REMOTE_DOWNLOADS/$VERSIONED $REMOTE_DOWNLOADS/$STABLE && sudo chmod 644 $REMOTE_DOWNLOADS/$VERSIONED $REMOTE_DOWNLOADS/$STABLE"
printf 'y\n' | "$WEBSITE_DIR/deploy.sh"

REMOTE_SHA=$(ssh "$REMOTE_USER@$REMOTE_HOST" "sha256sum $REMOTE_DOWNLOADS/$VERSIONED" | awk '{print $1}')
[ "$REMOTE_SHA" = "$SHA" ] || { echo "SHA mismatch: local=$SHA remote=$REMOTE_SHA"; exit 1; }
curl -fsSI "https://$REMOTE_HOST/mrrc_modern/downloads/$VERSIONED" | head -3
TMP=$(mktemp -d)/"$VERSIONED"
curl -fsS -o "$TMP" "https://$REMOTE_HOST/mrrc_modern/downloads/$VERSIONED"
[ "$(shasum -a 256 "$TMP" | awk '{print $1}')" = "$SHA" ] || { echo "downloaded file hash mismatch"; exit 1; }
echo "== v$VERSION published & verified =="
```

`chmod +x release.sh`。

- [ ] **步骤 5：dry-run 验证脚本**

```bash
cd FT710Android && bash -n release.sh && ./release.sh --dry-run
```

预期：构建绿、产物落 `dist/`、zh/en 两处页面标记块被替换为 v1.0.0 内容、以 `-- dry run` 结束、不触碰远端。

- [ ] **步骤 6：CHANGELOG.md**

```markdown
# FT710Android Changelog

App 版本独立于服务端版本；全功能需服务端 ≥ v1.22（txhb 闸门），更低版本自动降级。

## [1.0.0] — 2026-10-03

- 首个公开发布：官网签名 APK（LAN + Cloud Hub 接入），显示名 MRRC Modern，应用图标，minSdk 26
- 新增：txhb 发射心跳（声明服务端活性闸门能力，AD-007 V2.63 / SDD15 §15.6）
- 新增：录音面板——启停、列表、本地播放 + 拖动进度、导出（系统分享）、删除（AD-017）
- 新增：一键 CQ 呼叫，含进度与中止（AD-020）
- 修复：连接指示灯永远灰；设置页不可达；只读登录（4003）静默失败
- 发布：`release.sh` 一键构建/签名/上传/线上 SHA-256 复核；keystore 仓库外保管
```

- [ ] **步骤 7：BUILD_GUIDE.md / CLAUDE.md 文档**

`BUILD_GUIDE.md` 末尾追加：

```markdown
## 签名与发布（v1.0.0 起）

- keystore：`~/.android-keystores/mrrc-modern-release.jks`（别名 `mrrc-modern`，RSA 4096；**仓库外**）
- 口令与路径：`FT710Android/keystore.properties`（gitignore）。**丢失 keystore 将无法覆盖升级**，请单独备份文件与口令
- 指纹（首次生成后填写）：`keytool -list -v -keystore ~/.android-keystores/mrrc-modern-release.jks -alias mrrc-modern | grep SHA256`
- 一条命令发布：`cd FT710Android && ./release.sh`（`--dry-run` 只构建+改本地页面；`--skip-tests` 跳过单测）
```

`CLAUDE.md` 协议事实补：txhb（控制通道，按键首发 + 500ms，停于 release/forceRelease；服务端 `server.py:1768`，闸门仅 PTT 键主）、录音（`set recording` + `recordingState` + REST `/api/recordings`，Cookie 认证）、CQ（`set cq` + `cqState`）、只读（TX 通道 4003 → listen-only）、fullState 顶层 `recording`/`cq`/`radioModel` 能力字段。新模块表加 `RecordingsApi.kt` / `RecordingPanel.kt` / `Format.kt` / `release.sh`。

- [ ] **步骤 8：Commit（本仓 + 站点仓）**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add FT710Android README.md
git commit -m "build(android): one-command release script, docs, changelog, site download blocks"
git -C /Users/cheenle/HAM/website status --short   # 站点仓的页面改动已由脚本提交
```

---

### 任务 12：服务端侧验证 + 全量门槛

**文件：** 无改动

- [ ] **步骤 1：服务端闸门测试**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m pytest tests/test_tx_liveness.py -q
```

预期：全绿（含未声明能力不释放、单拍丢失不释放、旁观者超时不释放三类反例）。

- [ ] **步骤 2：Android 全量门槛**

```bash
cd FT710Android
./gradlew test assembleDebug lintDebug assembleRelease
```

预期：全部 `BUILD SUCCESSFUL`；测试计数与任务 6 收尾时一致。

- [ ] **步骤 3：核对没有越界改动**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern && git status --short   # 只应出现本任务链的文件
git log --oneline -10
```

---

### 任务 13：发布 v1.0.0 + 线上复核 + 交付验收清单

- [ ] **步骤 1：真实发布**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern/FT710Android
./release.sh --version 1.0.0
```

预期：结尾 `== v1.0.0 published & verified ==`；截图/记录 SHA-256、大小、线上 URL。

- [ ] **步骤 2：交付验收清单给用户**（原文抄送设计文档 §5.3 的 12 项，并附上本次发布的版本/大小/SHA 与三个入口：官网下载、LAN 地址、Hub 呼号入口）

- [ ] **步骤 3：验收窗口的服务端准备（需用户同意）**

在本机实例 env（`~/Library/Application Support/MRRC-Modern/mrrc_modern.env`）临时加 `MRRC_REMOTE_SESSION_TX_HEARTBEAT_S=5` 并重启服务端；验收第 11 项完成后恢复（或按 Hub 需要保留）。Hub 侧闸门状态一并核查。

- [ ] **步骤 4：等待用户回报**

问题 → 修复 → `release.sh --version 1.0.1`（versionCode 在 `build.gradle.kts` 递增）→ 用户复验。分支 `feat/android-1.0.0` 待验收通过后合并回 `feat/hub`（finishing-a-development-branch 技能）。

---

## 自审备注

- 任务顺序保证每一步都可独立构建：2/3 修接线 → 4 心跳 → 5 只读 → 6 数据层 → 7/8 UI → 9 品牌 → 10 签名 → 11 发布链路 → 12 门槛 → 13 发布。
- UI 任务（7/8/9）没有 JVM 测试，门槛为 `assembleDebug + lintDebug + assembleRelease`；音频/PTT 的真机行为由用户验收清单覆盖。
- 服务端代码是**只读参照**（不改）；站点与 README 只改任务 11 明确列出的页面/段落，其余不得碰。
- 所有协议字段已与 `server.py`（`txhb` @1768、`recording` @1925、`cq` @1961、fullState @3524）及 `static/modules/ptt_manager.js` 逐字核对。
