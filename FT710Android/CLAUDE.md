# CLAUDE.md — FT710Android

> 面向 AI 编码代理。原生 Android 遥控客户端（Kotlin + Jetpack Compose），通过 4 路 WSS 连仓库根 Python FastAPI 服务端（`server.py`），功能全量对齐 Web 前端。设计 spec：`docs/superpowers/specs/2026-08-16-ft710-android-app-design.md`；实施计划：`docs/superpowers/plans/2026-08-16-ft710-android-app.md`。

## 构建与测试

```bash
cd FT710Android
export JAVA_HOME=$(/usr/libexec/java_home -v 17)   # AGP 8.x 需要 JDK 17
export ANDROID_HOME="$HOME/Library/Android/sdk"
./gradlew test assembleDebug lintDebug   # CI 门槛：JVM 单测 + 编译 + lint
./gradlew installDebug                   # 真机安装（USB 调试）
```
- 工具链安装步骤见 `BUILD_GUIDE.md`（本机已验证：JDK 17 Corretto + SDK 35 + NDK 27.2.12479018 + Gradle 8.9 wrapper）。
- 仪器测试（`app/src/androidTest`，OpusBridge round-trip）需真机/模拟器：`./gradlew connectedDebugAndroidTest`。

## 架构速览

```
FT710App (Application) → ServiceLocator.assemble() 构造依赖闭环
  └─ MainViewModel (普通类, MainViewModelHolder/ViewModel 持有)
      ├─ ConnectionManager → 4+1 路 WebSocketConnection (OkHttp)
      │    /WSradio /WSaudioRX /WSaudioTX /WSspectrum (+ 可选 /WSatr1000)
      ├─ RadioState          (服务端字段镜像, apply 增量)
      ├─ RxAudioPlayer       (Opus/PCM → AudioTrack, 抖动缓冲)
      ├─ TxAudioCapture      (AudioRecord → Opus 编码 → WS)
      ├─ SpectrumProcessor   (1701B 帧 → 瀑布环 + FFT)
      └─ PTTManager          (安全状态机 + 看门狗)
```
纯逻辑类（协议/PTT/频谱/记忆频道/RadioState）不依赖 Android SDK，JVM 可测。
Compose 重组：`RadioState` 是可变普通类，UI 订阅 `MainViewModel.version`（`StateFlow<Long>`，每次 apply 后 +1）触发重组，随后读 `vm.state.*`。

## 协议事实（逐字对齐 server.py）

- 登录：`POST /api/auth/login` → `{"ok":true,"token"}` + `Set-Cookie: ft710_auth`；401 密码错 / 429 限流 → 回登录页并**停止重连**（`AuthApi.login` 返回 `AuthResult`）。WS URL 拼 `wss://<host>/WSxxx?token=<token>`。
- 控制通道 `/WSradio`（JSON）：下行 `fullState`（data+bands+modes+memChannels+filterTables+atr1000Enabled）、`stateUpdate`（fields 增量+dirty）、`memChannels`、`error`、`pong`；上行 `{"type":"set","field","value"}`、`{"type":"ping"}` 每 2s（`ConnectionManager` 心跳）、`{"type":"get","field":"fullState"}`、`{"type":"memSave","channels":[...]}`。
- **可设字段**：`freq` `vfo_a_freq` `vfo_b_freq` `mode` `ptt` `tune` `filter`/`filter_width` `af_gain` `rf_power` `preamp` `att`/`attenuator` `nb`/`noise_blanker` `nr`/`noise_reduction` `an`/`auto_notch` `comp`/`compressor` `tuner` `vfo` `split` `power` `squelch` `mic_gain` `scope_span` `scope_speed` `scope_mode` `nb_level` `nr_level` `comp_level`/`compressor_level` `monitor` `vox` `break_in` `key_speed` `cw_pitch` `rit` `rit_freq` `xit`。字段名以 `server.py:_execute_set_command` 为准，**禁止自创**。
- 音频：帧 = 1B tag（`0x00` PCM Int16 LE / `0x01` Opus）+ payload；48k 单声道 20ms（960 样本）。TX 恒 Opus CBR 64kbps；RX 解码 Opus 或直通 PCM。TX 文本帧 `s:` 停止、`m:` 设置。
- 频谱：`/WSspectrum` 二进制 1701B = 1B version(0x01) + 850B wf1 + 850B wf2；实际 ~5fps（`server.py:285`）。
- 记忆频道：**6 槽** + `null` 补空，键 `label`（`MemoryChannels.parse/toJson`）。
- ATR1000：`/WSatr1000` 可选，服务端禁用时 close 4000；用 `fullState.atr1000Enabled` 决定是否显示天调 UI。

## 安全铁律（PTT，spec §7）

- `PTTManager.release()` 无条件发 `ptt:false` + `s:`（不等回显）；`forceRelease()` 任意状态幂等。
- 手势 `finally` 兜底（`PTTButton.detectTapGestures` onPress→finally release）——修掉 iOS `onEnded` 竞态。
- `onStop`（`AppSetup` + `MainActivity`）→ `forceRelease()`；看门狗 500ms×3，重试耗尽 `onStuckTX`。
- `press()` 仅 Idle/Releasing 受理且要求控制通道已连接——避免"发不出去的乐观 TX"。

## 坑

- `ConnectionManager.onRadioEvent` 已解析为 `WsEvent`，`MainViewModel.onWsEvent(ev: WsEvent)` 直接消费（别传原始文本）。
- 自签 TLS：`AuthApi.selfSignedOkHttpClient()` 接受任意证书；`network_security_config.xml` 默认拒绝明文，`--no-ssl` 仅调试。
- `--no-ssl` 时 baseUrl 用 `http://`，`ConnectionManager.wsUrl` 自动转 `ws://`。
- 后台 RX 播放未实现（v1 决策）；退后台即停 TX。44.1k 设备采集重采样留作后续增强。
- `RadioState` 字段与 `radio_state.py:to_dict` 的 key 一一对应，新增字段两端同步。
