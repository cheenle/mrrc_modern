# Yaesu FT-991A 机型接入 — 设计文档 (Design Spec)

**日期**: 2026-10-31
**状态**: 已批准（范围=方案 A：控制 profile，频谱保持 `scope_type="none"`）
**范围**: 在 profile 驱动的 Yaesu ASCII-CAT 共享核心（`backends/yaesu/`）上接入 **FT-991A 作为第 6 个 profile**。FT-710 已验证路径（`backends/ft710/`）不动；`cat_core.py` 不改一行（FT-991 的滤波 SET 形式由既有 `filter_width_prefix` 字段覆盖）。
**前置**: `docs/superpowers/specs/2026-09-12-yaesu-sdr-models-design.md`（四款 Yaesu 原始设计）、`docs/superpowers/specs/2026-10-05-ft891-model-design.md`（FT-891 第 5 profile，本文件沿用其 D1/D2/D5/§7 模板与未验证纪律）
**SDD 追溯**: AD-018（profile 驱动核心）/ AD-019（未验证机型只收不发 + 逐表溯源）/ NFR-032（注册键）/ NFR-067（验证诚实性）/ §10.4（SH 前缀按机型）

**权威参考（全部离线，本机）**: Hamlib 4.7.2 `~/hamlib/Hamlib-4.7.2/rigs/yaesu/`：

- `ft991.c`（`taken from ft950.c`，头注释 "The FT991 is very much like the FT950"）、`ft991.h`（能力表）
- `newcat.c`（共享核心；FT-991 落在 `else` 分支的 SH 写格式，`newcat.c:9665-9668`）
- `newcat.h:59-60`：`NC_RIGID_FT991 = 570`、`NC_RIGID_FT991A = 670`

---

## 1. 目标

操作者在连接对话框选 "Yaesu FT-991A"（或 `MRRC_RADIO_MODEL=ft991a`）后，得到与其他 Yaesu 机型同等的 CAT 控制体验，并**修正当前"配成 FT-891"丢失的一切**：内置天调、Vd/Id 电表、2m/70cm 波段、C4FM/AM-N 模式、USB 声卡自动识别、`ID 0670` 身份核对。

**与本设计的核心边界**（用户已确认）：真实频谱/瀑布**不做**。FT-991A 的 3D 频谱只在电台自身屏幕上，CAT 没有公开的频谱波形数据流（Hamlib `ft991.c` 亦无 scope 输出），所以 `scope_type="none"`、走 S 表合成图——与本仓现有 5 款 Yaesu 机型一致。用户最初报的「频谱只能在 100 宽度」不会因本机型接入而改变。

## 2. 关键决策

| # | 问题 | 决策 |
| --- | --- | --- |
| D1 | 落点 | **`backends/yaesu/` 的第 6 个 profile**。与 FT-891 D1 同理由：不开新目录、不在 `cat_core.py` 写 `if model == "ft991a"`。 |
| D2 | 硬件可用性 | **无真机**。`verified=False`、`tx_gated=True`、逐表 `provenance`、前端标"实验性，仅接收"。 |
| D3 | 范围 | **仅控制 profile**（方案 A）。频谱 `scope_type="none"`。 |
| D4 | 机型键 | **只接 `ft991a`**（用户手上的机型；`id_answer="0670"`）。纯 FT-991（`NC_RIGID=570`）日后要加只需改 id/名字，本次 YAGNI。 |
| D5 | SH 写格式 | **`filter_width_prefix="SH0"`**（P1 only，无 P2）。FT-991 在 Hamlib `newcat.c:9665-9668` 的 SH set 落到 `else` 分支 `"SH%c%02d"`（`main_sub_vfo='0'`）→ `SH0NN`。这**不同于**家族 `SH00NN` 与 FT-891 `SH01NN`，也**不同于** FT-710/ftdx/ftx1 的"SH0NN 是缺位被忽略"结论——对 FT-991 这恰是正确的形式。约束 `cat-sh-format` message 同步修订。 |
| D6 | 滤波档位 | 抄 `ft991.c:283 .filters`（`.remember = order matters!`：列表位置即 SH 索引）。CW 17 档、RTTY/DATA 17 档、SSB **20** 档。AM/FM/C4FM/DATA-FM **不进表**（宽度由 `NA` 窄带标志固定，无 SH 槽位，同 FT-891 D5）。 |
| D7 | 模式 | `FT991_ALL_RX_MODES` = 家族表 + C4FM/AM-N/FM-N/DATA-FM，**无 DATA-FM-N(0xF)**（仅 FTX-1 有）。即 `_FAMILY_MODE_NUMBERS` 去掉 `DATA-FM-N`。 |
| D8 | 天调 | `has_atu=True`（`FT991_FUNCS` 含 `RIG_FUNC_TUNER`）、`tune_via="tx2"`（家族有内置天调的机型一致）。 |
| D9 | 音频 | **有内置 USB 声卡**（FT-991 系首款）→ `audio_name_hints=_YAESU_AUDIO_HINTS`。采样率默认 44100（Yaesu USB 惯例，FT-710 已验证 44.1k），标 `TODO(hw-verify)`。 |
| D10 | 电表 | `has_vd_id_meters=True`（`FT991_LEVELS` 含 VD+ID）。具体 VD/Id/COMP/功率曲线走 `config.RAW_TO_METER_TABLES` 通用表（与 FTDX101 相同，profile 不承载），列 `unverified_meters`。 |
| D11 | 串口默认 | `default_baud=38400`（FT-991 菜单 031 CAT RATE 出厂 38400）。Hamlib `serial_rate_min=4800 "Default rate per manual"` 有歧义；`_adopt_cat_rate` 阶梯兜底。 |

## 3. FT-991A profile 逐字段（草案即实现）

见 `backends/yaesu/yaesu_profiles.py::FT991A`。数据源逐表写入 `provenance`。

## 4. 接线 / 测试 / 文档

同 FT-891 spec §7 模板（9 处代码 + 既有 6 处测试必须改 + 新增 FT-991A 专属断言 + SDD/AGENTS/README/website 同步）。`cat_core.py` **零改动**。

## 5. 硬件验收边界（留给 `_diag_yaesu.py --model ft991a`）

USB 音频采样率（44.1k vs 48k，错了有音高偏移）、`ID;` 应答是否 `0670`、`SH0NN` 是否真被接受、SSB 20 档 Hz、2m/70cm 波段、天调/前置 10/20 dB。

## 6. 非目标

真实频谱/瀑布、纯 FT-991（非 A）、C4FM 数字语音控制、发版（版本号/CHANGELOG/安装包）。
