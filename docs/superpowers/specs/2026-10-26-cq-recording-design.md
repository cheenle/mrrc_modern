# CQ 呼叫录音（自定义 CQ.WAV）设计

- **日期**：2026-10-26
- **状态**：待实现
- **范围**：`settings.html` 新增「CQ 呼叫录音」卡；服务端四个接口；不装新依赖
- **相关代码**：`cq_asset.py`（新）、`cq_player.py`、`server.py`、`static/settings.html`、
  `static/modules/cq_recorder.js`（新）
- **一句话**：让操作员在网页上录自己的 CQ（带自己的呼号），存在数据目录里，换掉随包那一段。

---

## 1. 现状

| 事实 | 位置 |
| --- | --- |
| CQ 素材由 `MRRC_CQ_FILE` 指定，默认 `static/audio/cq.wav` | `config.py`、`server.py:721` |
| `CQPlayer.load()` 只认 **16-bit PCM WAV**，任意采样率/声道会统一成 48 kHz 单声道，切 20 ms 帧，>30 s 拒绝 | `cq_player.py:86` |
| CQ 播放**不经过**盒子 TX 麦克风增益，也不经过浏览器🎙 Vol | `server.py:_cq_key/_feed_tx_from_uplink` |
| 服务端只有编码器（lameenc），**没有解码器**——MP3 之类进不来 | `recorder.py` |
| `static/` 在盒子升级时被整包覆盖；`data/` 在 `box_update.KEEP` 里 | `box_update.py:52` |

所以"录一条自己的 CQ"必须是：浏览器采集 → 前端编成 WAV → 服务端校验并写入**数据目录**。

## 2. 决策

| # | 决策 | 理由 |
| --- | --- | --- |
| D-1 | 网页里直接录（`getUserMedia` + 前端 WAV 编码） | 不装依赖、手机上也能录；服务端没有解码器，上传 MP3 这条路走不通 |
| D-2 | 存 `MEM_FILE.parent / "cq.wav"`（盒子即 `data/cq.wav`） | `data/` 在 `KEEP` 里，升级不丢；与 `mem_channels.json`、`audio-levels.json` 同处 |
| D-3 | 随包 `static/audio/cq.wav` 保留为出厂默认；有自定义就用自定义 | 「恢复出厂 CQ」= 删掉自定义文件，不需要第二份副本 |
| D-4 | 保存时**峰值归一到 −3 dBFS**，并把增益量告诉操作员 | 麦克风远近不定；存下来的就是将来发射的那份，播放时不再加工 |
| D-5 | 录音/保存时**发射中拒绝**（含 FT8） | CQ 是发射素材，边发边录只会互相打架 |
| D-6 | 入口放 `settings.html`「音频电平」卡之后 | 同一类：盒子级、跨设备共享、重启保留 |

## 3. 校验（服务端说了算）

`POST /api/cq` 的请求体就是 WAV 字节，上限 **4 MB**。`cq_asset.inspect_wav()`：

| 条件 | 拒绝理由 |
| --- | --- |
| 不是合法 WAV | 「不是有效的 WAV 文件」 |
| 位深 ≠ 16-bit | 「需要 16-bit PCM WAV（当前 X-bit）」 |
| 采样率 < 8 kHz 或 > 192 kHz | 「采样率 X Hz 不支持」 |
| 声道数 ∉ {1,2} | 「只支持单声道或立体声」 |
| 时长 < 0.5 s | 「太短了（X 秒），至少 0.5 秒」 |
| 时长 > 30 s | 「太长了（X 秒），上限 30 秒」 |
| 峰值 = 0 | 「录到的是静音」 |
| 峰值 < −40 dBFS | 「音量太低（X dBFS），把麦克风靠近一点」 |

归一：`gain = 10^((−3 − peak_dbfs)/20)`，`round(s × gain)` 后钳到 int16，
响应里回 `applied_gain_db`。**原子写**（tmp + `os.replace`），失败不动旧文件。

## 4. 接口

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/api/cq` | `{source, ready, duration_s, peak_dbfs, has_custom, calling, reason}` |
| GET | `/api/cq/audio` | 当前生效的 WAV（`FileResponse`，供试听） |
| POST | `/api/cq` | 校验 → 归一 → 原子写 → `load()` → 广播 `cqState`；400 校验失败，409 发射中/CQ 播放中 |
| DELETE | `/api/cq` | 删自定义 → 回落出厂 → `load()` → 广播 |

`listen` / `sdr` 角色：GET 放行，写操作由既有中间件 403。

**立即生效**：不重启服务。改完 `_cq_player.set_asset(_cq_effective_path()); _cq_player.load()`，
再 `await _broadcast_cq_state()`，主界面 CQ 按钮的提示随之更新。

## 5. 前端

`settings.html`「音频电平」之后新增卡片：

```
CQ 呼叫录音
当前：出厂 CQ · 6.1 秒 · 峰值 -3.0 dBFS      [🔊 试听]
[● 开始录制]   0:00 / 0:30
录完 → [🔊 试听这一条]  [保存为 CQ]  [重录]
[恢复出厂 CQ]
提示：戴耳机或把电台音量关掉再录，否则会把收到的声音一起录进去。
```

- `static/modules/cq_recorder.js`（普通脚本，暴露 `window.CQRecorder`）：
  `getUserMedia({audio:{channelCount:1, echoCancellation:true, autoGainControl:false,
  noiseSuppression:false}})` → `AudioContext({sampleRate:48000})` → AudioWorklet（Blob 内联源码，
  不新增文件）→ 收 Float32 → 停止时编 16-bit 单声道 48 kHz WAV（RIFF 头 44 字节）。
- 页面脚本沿用 `box_api.js` 的 `api()`（自动带 Bearer），错误直接显示服务端原文。
- 30 秒到点自动停；未保存的录音只活在页面内存里，刷新即丢。

## 6. 缓存

`settings.html` 每次都重新校验（HTML），`modules/cq_recorder.js?v=1` 走 1 小时缓存，
所以新模块从第一天就带 `?v=1`；`settings.html` 需在测试里钉住这个 pin。`sw.js` 不预缓存
`settings.html` 与这个模块，CACHE 不动。

## 7. 测试

新增 `tests/test_cq_recording.py`：

- 校验矩阵：非 WAV / 8-bit / 太短 / 太长 / 静音 / 太低 → 各自的理由；
- 接受矩阵：44.1 kHz 单声道、48 kHz 立体声；
- 归一：−18 dBFS → −3 dBFS，`applied_gain_db` ≈ 15，且不越界（钳位）；
- `write_atomic`：目标目录不存在时创建；写失败不破坏旧文件；
- `CQPlayer.load()` 接受归一后的文件，`status()["peak_dbfs"]` 在 ±0.2 dB 内；
- 源码级契约：四个路由存在、发射中 409、CQ 播放中 409、`_cq_effective_path()` 优先自定义、
  `settings.html` 含卡片与 `cq_recorder.js?v=1`。

真机验证不在套件里：能不能录到人声、麦克风权限弹窗、手机上录音，需要一台带麦克风的设备；
这一点在 CHANGELOG 里写明，不冒充已验证。

## 8. 明确不做

- 不做 MP3/其他格式上传（没有解码器，也不为它拉 ffmpeg 依赖）。
- 不做降噪/压缩/EQ 处理，只做峰值归一。
- 不改 CQ 播放链路（`_cq_key/_cq_unkey/_feed_tx_from_uplink` 一行不动）。
- 不做多份 CQ 管理（一个自定义槽位 + 一个出厂默认）。
