# FlexRadio 接收链与协议底座体系化加固设计

> 日期：2026-11-16
> 状态：已获用户批准，待实施计划
> 适用版本起点：盒子线 1.28.76，FLEX-6600 / SmartSDR 4.3.16.42034 / protocol 1.4.0.0
> 架构约束：AD-003、AD-004、AD-011、AD-019、AD-025、AD-032、AD-036，SDD §15

## 1. 目标与范围

第一轮同时完成四组工作：

1. 修正已复现的 VITA-49、MultiFlex、重连和电台状态权威问题。
2. 用电台原生 `0x8004` waterfall tile 替换由 FFT 复制的假瀑布。
3. 为 FT8 提供按需、独立的 DAX 单声道音频，不再默认解码 speaker mix。
4. 补齐 TX 的离线安全缺口，但继续保持 `VERIFIED=False` 和 TX 门禁。

明确不做：立体声、多 slice/多 pan UI、SmartLink、完整 SmartSDR 功能移植、真机发射验收、热修发布和 git 提交。

实现只借鉴 AetherSDR 的协议事实、约束和测试方法，不复制其 GPL-3.0 C++ 代码。

## 2. 已确认的现场事实

- FLEX-6600 当前可同时报告多块 pan；它们的 `center`、`bandwidth`、`min_dbm`、`max_dbm`、像素尺寸各自独立。
- `display pan rfgain_info` 返回两个 SCU 三元组：`-8,32,8,0,16,32`，不能假定 RF gain 为 0–100。
- `interlock timeout` 单位是毫秒，现场值 `0` 表示硬件超时保护关闭。
- 当前 SSB 滤波器为 `100…2800 Hz`，本地默认索引不能代表电台真实滤波器。
- `remote_audio_rx` 是电台混合后的 speaker stream，不是单 slice 独立音频。
- 当前 FFT 子头被误读为五个 `uint16`；正确布局为四个 `uint16` 加一个 `uint32 frame_index`。
- 重复半个 FFT 分片两次，当前实现会错误输出含零填充的假完整帧。
- 连续两个 TX Opus 包的 VITA packet count 当前都为 0。

## 3. 总体结构

保持现有模块边界，不重写成分线程客户端：

```text
vita.py       纯字节编解码、分片覆盖、频率/强度换算
protocol.py   纯文本协议与状态字段
session.py    TCP/UDP、回执、interlock、连接代际
engine.py     每连接资源、每 pan 状态、每流时序、speaker/DAX 队列
audio.py      speaker 与 decoder 两条独立的 48 kHz 单声道适配器
scope.py      最新 FFT + 最新原生 waterfall 的发布与新鲜度
backend.py    CAT 翻译、真实状态回读、TX 门禁
server.py     通用消费者接线，不出现 Flex 机型分支
```

所有硬件无关规则先在纯逻辑层测试。`server.py` 只通过可选 handler hook 使用 decoder 音频，其他后端行为保持字节不变。

## 4. VITA-49 正确性

### 4.1 FFT

子头固定为 12 字节：

```text
u16 start_bin
u16 num_bins
u16 bin_size       必须为 2
u16 total_bins
u32 frame_index
```

解析要求：

- 不接受声明长度超过实际 payload 的分片；不得把子头当 bin 数据回退成“整包一帧”。
- 用唯一 bin 覆盖表计数；重复或重叠区域不增加完成度。
- 较旧 frame index 的分片只能丢弃，不能重置正在组装的新帧。
- 只有覆盖完整才发布；不完整帧有界保留并在新帧/重连时丢弃计数。
- 每 pan 保存实际的 `min_dbm/max_dbm/y_pixels`，按该 pan 的编码参数换算，不使用全局常量替代电台状态。

### 4.2 原生 waterfall tile

`0x8004` 子头固定 36 字节，解析：

- `FrameLowFreq` 与 `BinBandwidth`：带符号 64 位定点，单位为 `Hz × 2^20`。
- `LineDurationMS`、`Width`、`Height`、`Timecode`、`AutoBlackLevel`。
- `TotalBinsInFrame` 与 `FirstBinIndex`。
- payload 按有符号 int16 大端读取并除以 128，得到 tile intensity，而不是 dBm。

组帧规则与 FFT 相同：唯一覆盖、迟到丢弃、长度边界、不得伪造缺失 bin。第一轮只接受 `height=1` 的 tile；其他 height 明确拒绝并计数，不猜测多行布局。

每行保留：`stream_id`、强度数组、实际低频、实际高频、timecode、auto-black、单调接收时间。

### 4.3 时序、重排与新鲜度

- 每 stream ID 独立跟踪 4 bit packet count。
- 使用最长 30 ms 的有界重排窗；顺序包立即交付，缺口后的后继包短暂等待。
- 超时后按序释放并记缺包；重复包不重复交付。
- FFT/waterfall 缺片段只计数，不插造数据。
- 音频缺包必须保持样本时间轴：PCM 按丢失包对应的精确样本数补零，并在拼接点做不超过 5 ms 的淡出/淡入；Opus 优先使用解码器原生 PLC。不得直接删除缺包对应的时间。
- 每类流保存最后真实接收时间。只有 generation 变化才推进显示；TCP 存活而 UDP 停止时进入明确 stale 状态，不得重复滚动旧瀑布。
- UDP socket 设置有证据目标的接收缓冲并记录系统实际授予值，不把应用队列长度当作内核缓冲。

## 5. MultiFlex 与连接生命周期

### 5.1 每 pan 状态

`engine.pans[pan_id]` 至少保存：owner handle、center、bandwidth、min/max dBm、x/y pixels、waterfall ID、最后状态时间。

全局 `pan_center_hz`/`bandwidth_hz` 不再被任意 pan 更新；读取频谱轴时只取当前 `pan_id` 对应项。其他 pan 可观察，但不能驱动本机显示或控制。

### 5.2 slice 归属

- 当前 slice 必须位于当前拥有的 pan 上。
- 同 handle 但位于其他 pan 的 slice 不得被选中。
- slice 换 pan、被删除或 owner 改变时重新执行归属协调。
- 不发送 `slice set active=1`；active 只作为电台状态观察。
- 无合法 slice 时明确失败或创建自己的 slice，不退回 `min(slices)` 操作别人的对象。
- `_slice_id()` 在失去合法归属时不得返回别人的 ID。

### 5.3 连接代际与重连

每次成功 TCP 握手创建新 generation。重连时：

1. 旧 generation 的回调、pending create 和音频全部失去权限。
2. 清除 pan/slice/stream ready 与旧 ID、旧拼帧、旧队列、DAX 绑定。
3. 重新执行 probe → pan → slice → speaker/TX stream → 可选 DAX。
4. 任一步失败都同步设置 `connected=False` 并清理 session，不能留下半连接。
5. 正常 stop 只删除本 generation 明确拥有的资源。

## 6. 原生 waterfall 的现有帧协议映射

不改变 `/WSspectrum` 的版本字节和长度：

- full/high：`0x01 + wf1[850] + wf2[850]`，1701 B。
- mid/low：仍为 `0x01 + wf1[850]`，851 B，符合 AD-025。

语义调整：

- `wf1` 始终是 FFT，用于频谱线。
- `wf2` 在 Flex 原生 waterfall 可用时为最新真实 tile 行；尚未收到时不得宣称原生模式。
- 旧客户端继续只读 `wf1`，无需升级。
- 新浏览器在完整帧且原生模式有效时用 `wf1` 画 FFT、`wf2` 画 waterfall。
- 短帧到达时只更新 FFT；原生 waterfall 不前进，绝不插入一行 FFT 冒充 tile。
- 非 Flex 后端保持现有 wf1/wf2 行为不变。

auto-black 使用电台每行原始值：默认 offset=50、color gain=50 时，`lowRaw=autoBlack`，`num=0.5×cbrt(65535-lowRaw)`，`highRaw=max(lowRaw+100, lowRaw+num³)`；将 intensity 从 `[lowRaw/128, highRaw/128]` 线性映射到 0–255 并夹紧。现有前端 floor/ceiling 是用户显示后处理，不参与电台 intensity 的单位换算。

FFT 与 waterfall 分别发布实际 start/span。前端点击调谐必须使用所点击平面的轴，不能共享一个猜测的 VFO 居中轴。

## 7. DAX 独立 FT8 音频

### 7.1 两条音频语义

- speaker：`remote_audio_rx`，保留电台混音、AF 和 mute 语义，供浏览器、录音、本地喇叭使用。
- decoder：`dax_rx`，当前拥有 slice 的独立解调输出，仅供 FT8，保持 48 kHz 单声道 codec domain。

DAX 不用于改变现有浏览器音量模型，也不默认长期开启。

### 7.2 可选 handler 接口

Flex handler 提供：

- `start_decoder_rx() -> bool`
- `stop_decoder_rx() -> None`
- `read_decoder_rx_chunk() -> Optional[bytes]`
- `decoder_rx_source() -> str`：`dax`、`dax-pending` 或 `speaker-mix`

server 的 RX loop 在 FT8 tap 从无到有时调用 start，在从有到无、重连和 lifespan 时调用 stop。其他后端没有这些 hook 时完全沿用原 speaker PCM。

FT8 tap 优先读 decoder 队列；`dax-pending` 期间等待真实 DAX 数据，不得临时混入 speaker。启动失败后才明确记录并使用 speaker mix fallback，同时通过诊断状态暴露 `speaker-mix`，不得伪装成 DAX。

### 7.3 DAX 生命周期

启动：

1. 重新确认当前 slice/pan/connection generation。
2. 从所有 slice 的 `dax` 状态计算空闲通道，不硬编码通道 1。
3. 给当前 slice 绑定空闲通道并创建 `dax_rx`。
4. 只接收匹配新 stream ID 的 `0x03E3` 或 `0x0123` 包。
5. 使用独立的重采样状态、队列和 20 ms 帧累加器，不与 speaker 共用历史。

停止：移除本 generation 的流并仅在该 slice 仍持有同一通道时解绑。通道占满、创建失败或状态不一致时启动失败并进入显式 fallback。

## 8. 电台真实状态

- AGC speed 从 `agc_mode` 回读，不再由本地初始值冒充。
- 当前滤波器从 `filter_lo/filter_hi` 和模式推导最近的真实索引；无法精确映射时显示 Hz，不伪造索引。
- AGC-T 未收到时显示未知，不填 65。
- `display pan set` 必须检查回执，随后仍以状态推送为最终权威。
- 前端使用电台实际跨度，而不是请求索引对应的跨度。
- `rfgain_info` 按每 SCU 的 `low,high,step` 三元组保存；只有取得当前 SCU 合法量程后才开放 RF gain 控件，写入值必须落在量程并按 step 对齐。

## 9. TX 安全（不开放发射）

- `interlock timeout` 转为秒存储；`0` 在自检和启动日志中明确为“硬件 TX timeout 已关闭”，建议 30 s。
- TX Opus packet count 每发送一包递增 mod 16，重连按新流重新初始化。
- key-up 前先确保 TX slice 和音频 pacer 就绪，再发 `xmit 1`；任何失败都有界释放。
- interlock 离开 `TRANSMITTING` 时停止 pacer、清队列并广播权威 RX 状态。
- key-up 后设置有界 `SC_MIC` 自证窗口：队列确有音频而电台表无能量，或无法取得必要证据时，立即 unkey 并设置 `tx_audio_path_unconfirmed`。
- 正常 release 仍直接发送 `xmit 0`，不恢复阻塞式多次读回。
- `VERIFIED=False` 不变；只有后续独立假负载验收才能调整。

## 10. 错误与降级原则

- 缺 bin、旧帧、外国流、旧 generation：丢弃并计数，不生成貌似合理的数据。
- 无原生 waterfall：保持 FFT 可用，瀑布明确等待，不复制 FFT 行。
- 无 DAX：FT8 可退回 speaker mix，但状态必须可见。
- 无合法 slice/pan：明确未连接，不操作别人的资源。
- 无真实 AGC/filter/RF 范围：显示未知或隐藏控件，不填默认值。
- 任何 TX 证据不足：优先 unkey，绝不发静音载波。

## 11. 测试策略

### 11.1 先写失败回归

- 真 12 字节 FFT 子头的 `uint32 frame_index`。
- 重复/重叠分片不能完成帧；迟到分片不能重置新帧。
- 外国 pan 状态不能改本机轴；slice 动态换 pan 必须重新协调。
- 两个 TX 包序号连续。
- timeout=0 与 30000 ms 的判断。
- 重连后旧 stream ready/ID 不得复用。

### 11.2 原生 waterfall

- 36 字节子头、负近 DC 频率、唯一覆盖、迟到 timecode、auto-black 黄金值。
- 1701 B full 帧中 wf1=FFT、wf2=tile；851 B 短帧不推进原生瀑布。
- 非 Flex 频谱字节回归。
- 新帧才滚动，stale 后停止。

### 11.3 DAX 与音频

- 空闲通道选择、通道占满、重复 start/stop、重连 cleanup。
- speaker 与 decoder 队列/重采样历史互不影响。
- DAX 包按 stream 过滤；缺包后样本时钟不缩短。
- 其他后端没有 hook 时 PCM 路径不变。

### 11.4 系统验收

- Flex 专项测试和完整 `python -m unittest discover -s tests`。
- 前端语法、静态缓存 pin、WS 协议契约。
- 假电台端到端：连接 → FFT/tile → speaker → DAX → 重连。
- 真机只读：tile 轴/auto-black、DAX 启停、无残留 pan/slice/stream、stale 行为。
- 不做真机 TX；TX 仅离线证据链验收。

## 12. 文档与交付纪律

同步 `AGENTS.md`、README、SDD 相关章节与版本历史、`tests/README.md` 和本设计对应的实施计划。缓存版本与内容哈希 pin 一并更新。

实施按测试驱动小步提交到工作区，但不执行 git commit、不构建发布包、不部署热修；这些动作需要用户另行明确授权。
