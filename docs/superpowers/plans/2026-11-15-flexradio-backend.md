# FlexRadio 原生后端实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）
> 或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）跟踪进度。

**目标：** 让 `MRRC_RADIO_MODEL=flex` 驱动任意 FLEX-6000/8000 电台（控制 + 频谱 + RX 音频 +
TX 语音 + 表），浏览器/iOS/Android 侧零改动。

**架构：** `backends/flex/` 六文件单向分层 —— `vita.py`/`protocol.py` 纯编解码，
`session.py` 管 TCP 4992 + UDP + interlock，`engine.py` 独占网络资源与队列，
`audio.py`/`scope.py` 适配既有 `AudioHandler`/`ScopeProducer` 契约，`backend.py` 翻译
~50 个抽象 CAT 方法。TX 走 AD-019 门禁；无真机期间 `VERIFIED=False`。

**技术栈：** Python 3 stdlib + numpy + 现有 `opus_rx.py`（ctypes libopus）、asyncio。

**规格：** `docs/superpowers/specs/2026-11-15-flexradio-backend-design.md`
**协议依据：** `docs/flexradio-smartsdr-analysis.md`

---

## 文件结构

| 文件 | 职责 | 阶段 |
| --- | --- | --- |
| `backends/flex/__init__.py` | 包文档；导出 `VERIFIED`/`DISPLAY_NAME`/`MODEL_KEY` | P1 |
| `backends/flex/vita.py` | VITA-49 头/FFT/音频/表/Opus 包/发现信封；频谱映射常量 | P1 |
| `backends/flex/protocol.py` | `C/R/S` 行、多词对象名、meter `#`、`info`、`slice list`、发现载荷、错误码、表换算 | P1 |
| `backends/flex/session.py` | TCP 会话、序号/回执、订阅、UDP 登记、`client_handle` 过滤、interlock、重连 | P2 |
| `backends/flex/engine.py` | 网络资源唯一所有者、RX 环、TX 节拍器、表缓存、能力缓存 | P3 |
| `backends/flex/audio.py` | `FlexAudioHandler`（AudioHandler 同款面）、24k↔48k、Opus 收发 | P3 |
| `backends/flex/scope.py` | `FlexScopeProducer`：FFT → 850 bin、span→bandwidth | P3 |
| `backends/flex/backend.py` | `FlexBackend` + `FlexController` | P4 |
| `opus_rx.py` | 扩参：`RxOpusEncoder(rate, channels)` + 新 `TxOpusEncoder` | P3 |
| `tests/flex_fake_radio.py` | 脚本化假电台（TCP 4992 + UDP） | P2 |
| `tests/test_flex_vita.py` | 字节层 | P1 |
| `tests/test_flex_protocol.py` | 文本层 | P1 |
| `tests/test_flex_session.py` | 会话状态机 | P2 |
| `tests/test_flex_audio.py` | 采样率/节拍器 | P3 |
| `tests/test_flex_scope.py` | 频谱映射 | P3 |
| `tests/test_flex_backend.py` | 契约层 | P4 |
| `_diag_flex.py` | 真机自检（单文件可打包） | P4 |

---

## P1 纯编解码层（离线全绿）

### 任务 1：`vita.py`

**文件：** 创建 `backends/flex/vita.py`、`backends/flex/__init__.py`；测试 `tests/test_flex_vita.py`

- [x] 写测试：头解析（type/C/T/TSI/TSF/count/size 字数/trailer 剥离/短包拒绝）
- [x] 写测试：Opus 包**黄金字节**（word0..6 逐字断言、载荷不补齐）
- [x] 写测试：FFT 子头 + 分片拼帧 + 不一致时回落「整包即一帧」
- [x] 写测试：音频 `len%8`、大端 float32 立体声、meter 对、DAX IQ 速率表
- [x] 写测试：`pixels_to_ui` 在 `min=-120/max=0/ypix=256` 下**恰好等于 `255 - s`**
- [x] 实现至全绿
- [ ] `./.venv/bin/python -m unittest tests.test_flex_vita -v`

### 任务 2：`protocol.py`

**文件：** 创建 `backends/flex/protocol.py`；测试 `tests/test_flex_protocol.py`

- [x] 写测试：`build_command`/`parse_response`（成功、错误码、debug、截断）
- [x] 写测试：`parse_status` 多词对象名（`slice 0`、`display pan 0x40…`、`interlock band 9`）
- [x] 写测试：`parse_meter_definitions` 的 `#` 分隔与 `scale_meter` 逐单位换算
- [x] 写测试：`info`（逗号 kv + 引号剥离）、`slice list`、发现载荷（`_`→空格）
- [x] 实现至全绿
- [ ] `./.venv/bin/python -m unittest tests.test_flex_protocol tests.test_flex_vita -v`

### 任务 3：P1 提交

- [x] `git add backends/flex tests/test_flex_vita.py tests/test_flex_protocol.py`
- [x] `git commit -m "feat(flex): VITA-49 与 SmartSDR 文本层（P1 纯编解码）"`  → `4c1f0e2`

---

## P2 会话层

### 任务 4：`session.py`

- [ ] 写 `tests/flex_fake_radio.py`（可脚本化：发 V/H、按命令回执、可推状态、可扮演异常）
- [ ] 写测试：握手顺序（`sub`→`client gui`→UDP 0 字节→`client udpport`→`stream create`）
- [ ] 写测试：序号与回执匹配、回执超时、乱序状态推送
- [ ] 写测试：interlock 迁移（`READY→PTT_REQUESTED→TRANSMITTING→UNKEY_REQUESTED→RECEIVE`、
      `TX_FAULT` 立即上报）、`xmit` 确认 1.5 s 超时
- [ ] 写测试：`client_handle` 过滤（别人的 slice/pan/流不写进模型）
- [ ] 写测试：断开 → 退避重连 1/2/4/8→30 s 上限
- [ ] 实现至全绿；`./.venv/bin/python -m unittest tests.test_flex_session -v`

### 任务 5：P2 提交

- [ ] `git commit -m "feat(flex): SmartSDR 会话层与假电台（P2）"`

---

## P3 音频与频谱

### 任务 6：`opus_rx.py` 扩参

- [ ] 写测试：默认 `48000/1` 行为不变（现有用例继续绿）；`24 kHz 立体声` 编解码往返
- [ ] 实现 `RxOpusEncoder(rate=…, channels=…)` + `TxOpusEncoder`
- [ ] `./.venv/bin/python -m unittest tests.test_opus_rx -v`（若无此文件则新建）

### 任务 7：`engine.py`

- [ ] 写测试：RX 环上限丢最旧、4 包重排、TX 队列溢出计数、表按名字查
- [ ] 实现

### 任务 8：`audio.py` + `scope.py`

- [ ] 写测试：24k 立体声→48k 单声道 **60 s 不漂**（样本数精确）；48k→24k **抗混叠**
      （10 kHz 通、20 kHz 抑制 >40 dB）；240 样本攒帧；节拍器假时钟 10 ms
- [ ] 写测试：850 直通 / 重采样回退、`scope_span`→`bandwidth`、`scope_start_freq`
- [ ] 实现至全绿；`./.venv/bin/python -m unittest tests.test_flex_audio tests.test_flex_scope -v`

### 任务 9：P3 提交

- [ ] `git commit -m "feat(flex): 引擎、音频与频谱适配（P3）"`

---

## P4 契约与集成

### 任务 10：`backend.py`

- [ ] 写测试：能力来自电台自述；`tx_gated`；不支持的能力返回 False 且不进轮询表；
      `tx_audio_path_unconfirmed` latch；释放路径幂等
- [ ] 实现

### 任务 11：注册与配置

- [ ] `backends/__init__.py`：`_BACKENDS["flex"]`、`model_connection`、`model_verification`
- [ ] `config.py`：`FLEX_HOST`/`FLEX_TX_TIMEOUT_WARN_S`/`FLEX_SPECTRUM_FPS`
- [ ] `profiles/flex.env`、`server.py` 的 `flex_host`（对齐 `sunsdr_host`）
- [ ] 修 `tests/test_backend_factory.py`、`tests/test_box_profiles.py`（`UNVERIFIED` + 推导）
- [ ] `static/modules/connection_settings.js` + `/setup` 的机型/主机字段

### 任务 12：`_diag_flex.py` + 文档同步

- [ ] 自检脚本：发现 → 握手 → `info` → pan 抓帧 → RX 音频 → 表 → interlock；`--tx-check`
- [ ] 同步 `AGENTS.md`、`README.md`、`tests/README.md` 计数、`SDD/` 机型章节
- [ ] `./.venv/bin/python -m unittest discover -s tests`（全绿）
- [ ] `git commit -m "feat(flex): 后端契约、注册与自检脚本（P4）"`

---

## 验收

- [ ] 全套 unittest 绿
- [ ] `python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged` 干净
- [ ] 真机项（U1–U6）留待种子用户，见规格 §11
