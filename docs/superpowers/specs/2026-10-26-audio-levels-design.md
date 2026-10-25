# 盒子级音频电平（TX 麦克风 / RX 下发）设计

- **日期**：2026-10-26
- **状态**：已实现
- **范围**：服务端两个增益（TX 麦克风上行、RX 下发路径）+ 网页端三个入口（☰ 菜单、`settings.html`、🎙 Vol 上限抬高）
- **相关代码**：`audio_levels.py`（新）、`server.py`、`static/index.html`、`static/ft710_main.js`、
  `static/ft710_ui.js`、`static/settings.html`
- **相关文档**：SDD `09-architecture-overview.md` §9.13（AD-032）、`14-version-history.md`、`CHANGELOG.md`
- **一句话**：电平归盒子（一处真值、跨设备一致），浏览器只保留自己的播放音量与麦克风微调。

---

## 1. 问题

三条**实测**事实（不是推测）：

1. **FT-710 的 USB 音频偏轻**，网页端为此内置一个固定的 `AUDIO_GAIN_BOOST = 10.0`（+20 dB）。
   它拉满也只有 10×，再往上没有旋钮 —— 于是"浏览器音量拉满还是听不清"无解。
2. **麦克风的软件增益归属于浏览器**（`🎙 Vol`，cookie `ft710_micVol`，0–200 线性 = 0…+6 dB）。
   上限 +6 dB 对一支偏轻的耳机麦克风不够，而且**每台设备各一份**：手机上 +6 dB、电脑上 +0 dB，
   同一台电台两个声音。
3. **服务端没有任何电平旋钮**：`feed_tx_audio` 与 `encode_rx_audio` 都是原样转发。

## 2. 决策（四条，均为用户拍板）

| # | 决策 | 理由 |
| --- | --- | --- |
| D-1 | TX 增益**只作用于浏览器麦克风上行**，CQ 播放与 FT8 波形不受影响 | CQ/FT8 是固定素材/波形，电平由它们自己决定；只有人声随耳机变化 |
| D-2 | RX 增益**只作用于下发到客户端的那份 PCM** | 录音、FT8 解码、盒子本地扬声器保持原生电平：削顶会伤解码，而这三个消费者不是"听众" |
| D-3 | 两个盒子电平**默认 0 dB**，网页端 🔊 的 10× **一个字节都不动** | day-one 与今天逐字节相同；安卓/iOS/listen/SDR 四端不需要同版本发布 |
| D-4 | 界面上明确写出"盒子 RX 电平 × 浏览器 🔊 相乘" | D-3 的代价，必须写在操作员会看到的地方 |

## 3. 所有权

| 量 | 所有者 | 范围 | 默认 | 落在哪 |
| --- | --- | --- | --- | --- |
| TX 麦克风增益 | 盒子（服务端，跨设备共享） | −20…+25 dB | 0 dB | `_feed_tx_from_uplink` |
| RX 下发电平 | 盒子（服务端，跨设备共享） | −20…+30 dB | 0 dB | `_audio_rx_loop` 编码前 |
| 🎙 Vol | 浏览器（cookie） | −20…+25 dB | 0 dB | 麦克风采集图上的 GainNode |
| 🔊 Vol | 浏览器（cookie） | 0…255 → 0…10× | 128 | RX 播放 GainNode |

## 4. 模块 `audio_levels.py`（纯逻辑，stdlib + numpy，不 import 应用）

- 常量：`TX_MIC_LIMITS = (-20.0, 25.0)`、`RX_LIMITS = (-20.0, 30.0)`、`DEFAULT_*_DB = 0.0`。
- `db_to_linear(db)`，`clamp_db(value, limits)`。
- `apply_gain(pcm, db) -> (pcm_out, clipped_samples)`：
  - **`db == 0.0` 原样返回同一个 bytes 对象**（不做 numpy 运算、不复制）——这既让"默认值 = 无变化"
    成为可断言的事实，也让没有增益时的 CPU 开销为零；
  - 否则 `<i2` → int32 → `rint(samples × factor)` → 统计越界样本 → 钳到 int16 → 回 `<i2`；
  - 奇数长度输入丢掉最后一个字节（20 ms 帧永远是偶数，这条只为"永不抛异常"）。
- `AudioLevels`（`tx_mic_db` / `rx_db`，`to_wire()`）与 `parse_update(payload, current)`：
  **越界/非数字/未知键一律拒绝并给出中文原因，绝不静默夹取**（静默改值是本项目一贯不吃的那套）。
- `AudioLevelsStore(path)`：`load()` 容忍缺失/损坏（回落默认 + 一条警告）、夹取文件里的越界值、
  `save()` 原子写（tmp + `os.replace`）。**永不致命**：写不进去就留在内存里，日志说一次。

## 5. 服务端两处落点

### 5.1 TX

```
/WSaudioTX 二进制帧 ──> Opus/PCM 解码 ──> _feed_tx_from_uplink()
      ├─ FT8 发射中 / CQ 占用 → 丢弃（不变）
      ├─ apply_gain(pcm, tx_mic_db) ──> _rec_tap_tx()      ← 录音录的是"送进电台的信号"
      └─ audio.feed_tx_audio()
```

`_rec_tap_tx(pcm)` 从 WS 处理函数**移进** `_feed_tx_from_uplink`，放在增益之后。这一个动作收掉
三处旧账：① 录音口径与"实际发射"一致（含增益、含削顶）；② Opus / 裸 PCM / 旧的无标签三种上行
格式走同一条路（此前只有 Opus 那支被录进 TX 录音）；③ CQ 期间被丢弃的麦克风帧不再进入录音
（它们本来就没发射出去）。

### 5.2 RX

```
read_rx_chunk() ──> pcm（原生）
      ├─ _local_speaker.feed(pcm)      ← 不变
      ├─ _rec_tap_rx(pcm, rate)        ← 不变
      ├─ _ft8_runtime.rx_tap.feed(pcm) ← 不变（解码要原生电平）
      └─ apply_gain(pcm, rx_db) ──> encode_rx_audio() ──> /WSaudioRX 客户端
```

`_needs_frames` 为假（没人要帧）时不计算增益。

### 5.3 削顶可观测

按方向累计 `(samples, clipped)`。增益 > 0 dB 且单帧削顶比例 > 1% 时**每 30 s 最多一条**警告
（"是盒子把信号顶掉的"，而不是让操作员去猜对方听着为什么糊）；计数进 `GET /api/audio/levels`
响应，诊断包可见。

## 6. 接口

- `GET /api/audio/levels` → `{tx_mic_db, rx_db, limits:{…}, stats:{tx:{samples,clipped}, rx:{…}}, source:"file"|"default"}`
- `POST /api/audio/levels` `{tx_mic_db?: −20…+25, rx_db?: −20…+30}` —— 部分更新；
  成功返回完整新状态；越界/非数字/未知键 → **400 + 允许区间**。
- 落盘 `MEM_FILE.parent / "audio-levels.json"`（`MRRC_AUDIO_LEVELS_FILE` 可覆盖）；
  启动时在 lifespan 读盘，导入期不碰磁盘。
- 变更后向 `ctrl_clients` 广播 `{"type":"audioLevels","tx_mic_db":…,"rx_db":…,"limits":{…}}`
  —— **与 REST / fullState 同一套键名**，前端只有一个解析器。
- `fullState` 增加 `audioLevels`，新页面一连上滑块位置就是对的。
- 权限沿用现有 REST 门：`listen` / `sdr` 的写请求 403（只读角色），GET 允许。

## 7. 前端

- **☰ 菜单**（`index.html` + `ft710_ui.js`）：`Settings` 之上加一段标题「音频电平（盒子）」，
  两行滑块 `slider-txdb` / `slider-rxdb`（步进 1 dB，右侧显示 `+6 dB` / `0 dB` / `-3 dB`）。
  `input` 只动标签，`change` 才 POST（一次拖动一个请求），用响应回写；`fullState` 与
  `audioLevels` 推送都会刷新它 —— 别的设备改了这里跟着动。
- **`settings.html`**：新卡片「音频电平」，两条滑块 + 「复位 0 dB」，说明写清 D-1 / D-2 / D-4
  三件事（TX 只作用于麦克风；RX 不碰录音、FT8、本地扬声器；盒子 RX 电平与浏览器 🔊 相乘）。
- **🎙 Vol**（`ft710_ui.js` 的电台参数弹窗）：滑块改 dB（−20…+25，默认 0，显示带 `dB`），
  `gain = 10^(db/20)`；新 cookie `ft710_micVolDb`，旧 `ft710_micVol`（0…200 线性）一次性迁移
  （`20·log10(v/100)` 后夹取）。

## 8. 测试

- `tests/test_audio_levels.py`：0 dB 恒等（同一对象）、±6 dB 的幅值、钳顶计数、奇数长度、
  `parse_update` 的拒绝面、store 往返/损坏/越界/原子写。
- `tests/test_audio_levels_server.py`：TX 增益只走麦克风（CQ 与 FT8 通路的字节不变、TX 录音
  拿到的是增益后的字节）、RX 增益只进 `encode_rx_audio`（录音 tap / FT8 tap / 本地扬声器
  拿到原生字节）、REST 的 400 / 只读 403 / 广播 / `fullState` 字段。
- 前端契约：菜单两个 id 存在、`?v=` 已 bump、🎙 Vol 的 `max="25"`。

## 9. 明确不做

ALSA 混音器（`amixer`/`alsactl`）、本地扬声器独立增益、CQ/FT8 电平豁免开关、安卓/iOS 的对应
UI（服务端默认 0 dB，四端行为不变）、把网页端 10× boost 收回服务端（那是一次四端联发）。
