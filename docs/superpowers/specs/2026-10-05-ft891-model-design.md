# Yaesu FT-891 机型接入 — 设计文档 (Design Spec)

**日期**: 2026-10-05
**状态**: 设计已批准（4 项澄清 + 3 项签字全部确认），待实施
**范围**: 在既有的 profile 驱动 Yaesu ASCII-CAT 共享核心（`backends/yaesu/`）上接入 **FT-891 作为第 5 个 profile**。FT-710 的已验证独立路径（`backends/ft710/`）不动；共享核心 `cat_core.py` 只改一处字符串拼接；其余全部是数据表、注册表接线、测试与文档同步。
**前置**: `docs/superpowers/specs/2026-09-12-yaesu-sdr-models-design.md`（四款 Yaesu 机型的原始设计，本文件沿用其 §6 未验证纪律与 §5 profile 字段分组）、`docs/superpowers/plans/2026-09-12-yaesu-sdr-models.md`
**SDD 追溯**: AD-018（profile 驱动的 Yaesu 核心，FT-710 路径保持独立）/ AD-019（未验证机型只收不发 + 只读身份核对 + 逐表溯源）/ NFR-032（注册键协议兼容）/ NFR-067（验证诚实性）/ SDD/13 A9（家族共享命令面）/ §10.4 控制服务命令契约

**权威参考（全部离线，均在本机）**:

- Hamlib 4.7.2 源码树 `~/hamlib/Hamlib-4.7.2/rigs/yaesu/`：
  - `ft891.c`（637 行）、`ft891.h`（141 行）—— FT-891 的机型能力表。文件头自承 `taken from ft991c`（"The FT891 is very much like the FT991"）。
  - `newcat.c`（13,048 行共享核心）—— FT-891 的**逐命令**行为分支（`is_ft891`），含滤波宽度的 set/get 索引映射、`SH` 的 "bandwidth on" 位、`ID;` 到机型号的换算。
- **仓库内的 FT-891 真机现场证据**（老产品 `hub/mrrc/`，2026-09-17 用户 bg7zhs 两次上报，产品版本 V6.1.12）：
  - `hub/mrrc/dist/support_answers/20260917-073700-14ef.digest.md`
  - `hub/mrrc/dist/support_answers/20260917-085736-ebd2.digest.md`
  - `hub/mrrc/tests/test_rigctld_supervisor.py:82`（把该现场的 rigctld 命令行钉成断言）
  - `hub/mrrc/CHANGELOG.md:248-259`（V6.1.14 该案的根因与修复）
  这是本项目目前**唯一**的 FT-891 真机数据，用于关闭三个离线推不出的问题：串口参数、有无 USB 声卡、S 表偏差方向。
- 既有实现事实（本仓库，已核对行号）：`backends/yaesu/yaesu_profiles.py`、`backends/yaesu/cat_core.py:416-427`、`backends/yaesu/backend.py:100-108`、`backends/__init__.py`、`config.py:218-232`、`static/index.html:653-663`、`static/ft710_ui.js:309-330,1183`、`audio_handler.py:118-138`、`server.py:2723-2725`。

推不出的字段一律在 `provenance` 里标 `TODO(hw-verify)`，绝不把猜测当数据（AD-019）。

---

## 1. 目标

操作者在连接对话框选 "Yaesu FT-891"（或 `MRRC_RADIO_MODEL=ft891`）后，得到与那四款实验性 Yaesu 机型同等的体验：CAT 控制（频率/模式/滤波/PTT/TUNE/增益/DSP/存储信道）、按机型标定的电表、USB 音频路径、以及**诚实的能力上报**——默认只收不发（`tx_gated=True`，发信需 `MRRC_ALLOW_UNVERIFIED_TX=1`）。

与那四款的两点不同，都由 FT-891 的硬件事实决定，并在 UI 与文档里显式说明：

1. **FT-891 没有 USB 声卡**（USB 口只做 CAT），音频必须外接接口到 DATA/ACC 口 —— 见 D3。
2. **FT-891 没有内置天调** —— 见 D7。

## 2. 设计评审中做出的决策（2026-10-05）

| # | 问题 | 决策 |
| --- | --- | --- |
| D1 | 落点 | **`backends/yaesu/` 的第 5 个 profile**，不开新目录、不在 `cat_core.py` 里写 `if model == "ft891"`。否决理由：单开 `backends/ft891/` 要复制 693 行传输层去承载一个只有滤波前缀不同的机型，而 FT-710 独立成目录的理由是"已真机验证、冻结降风险"（前序 spec D6），FT-891 恰恰相反；写死型号分支则违反 `yaesu_profiles.py` 的模块契约（"机型差异的唯一来源是 profile"）。 |
| D2 | 硬件可用性 | **无真机**。按 AD-019 与前序 spec §6 的未验证纪律落地：`verified=False`、`tx_gated=True`、`unverified_meters` 列明、逐表 `provenance`、推不出的标 `TODO(hw-verify)`、前端标"实验性，仅接收"。 |
| D3 | USB 音频 | **FT-891 无 USB 声卡**（操作者知识 + 现场证据双重确认）。`audio_name_hints=()`、`audio_rx_rate=audio_tx_rate=48000`、前端标签加"需外接声卡"、文档写明音频走 DATA/ACC 外接接口。**这条推翻了评审中途一度采用的"沿用家族 44.1 kHz + Yaesu hints"**（见 §3 的证据更正）。 |
| D4 | 串口参数 | **38400 8N1，`cat_core.py` 不改**。现场证据（§3）显示 FT-891 实配就是 `38400 / data_bits=8 / stop_bits=1 / parity=None`；Hamlib `ft891.c:143` 的 `serial_stop_bits = 2` 自带注释 `Assumed since manual makes no mention`，是假设值，不采信。不新增 `stopbits` profile 字段（那会同时改变四款已发布机型的行为，且两边都无真机证据支持改动）。 |
| D5 | 滤波宽度 | **按 Hamlib get 侧逐档抄全表 + `SH` 前缀 profile 驱动**：narrow 组 17 档、voice 组 21 档；`filter_width_prefix` 新字段（默认 `"SH00"`，FT-891 `"SH01"`）；**AM/FM 不进表**（其宽度由 `NA` 决定、不走 SH 索引，见 §3）；**不建模 `NA`(narrow)**，索引 0 不进 UI 表（本仓库 UI 是"用户点具体档位"，从不发索引 0）。 |
| D6 | `id_answer` | **留空 `""`**（只记日志，不做失配告警）。存在一条可推的证据链（`newcat.c:58 NC_RIGID_FT891=135` + `newcat.c:11212-11227` 对 `ID;` 应答 `atoi` + `newcat.c:51` 注释 `ID 0310 == 310`，并由仓库内官方文档值交叉验证：FTX-1 `"0840"` == `NC_RIGID_FTX1=840`），但操作者选择不把推论值写进 profile，与 `ftdx10`/`ftdx101d`/`ftdx101mp` 三款保持一致。真机 `ID;` 观测后由 `_diag_yaesu.py` 回填。 |
| D7 | 天调 | **`has_atu=False`、`tune_via="tx2"`**。FT-891 无内置天调；`capabilities.has_atu=false` 时前端已会隐藏 ATU 键（`static/ft710_ui.js:1183`），TUNE 仍发 `TX2` 载波——外接天调正是靠载波调谐。不宣称一个验证不了的天调（Hamlib 把 `AC` 标为有效、`FT891_FUNCS` 含 `RIG_FUNC_TUNER`，那是外接天调路径，见 §3）。 |
| D8 | 约束修订 | **只改 block 级约束 `cat-sh-format` 的 message，不动 patterns**（不放松护栏）。原文案断言"Filter width SET is SH00NN"是普适事实，接入 FT-891 后对 `backends/yaesu/` 不再成立，按 sdd-guardian 规矩在同一次改动里改正，不默默偏离。 |
| D9 | S 表曲线 | **沿用 Hamlib `FT891_STR_CAL`（与 FTX-1 逐点相同），不因现场反馈改曲线**。现场那条"比机显高 1–1.5 S"记进 `provenance` 与 §10 的验证清单，作为真机核对的第一项；拿一个产品的观测去改另一个产品的表、且没有原始 raw 值，属于造数据。 |

## 3. 数据溯源

| 数据 | 来源 | 状态 |
| --- | --- | --- |
| ASCII 框架（`CMD;`、`P1P2;`、`;` 结尾） | 前序 spec §3；FT-710 上已现场验证并由 `cat_core.py` 继承 | 家族共享，FT-710 已验证 |
| 模式寄存器值 | Hamlib `newcat.c:12043 newcat_mode_conv[]` | 家族共享，有出处 |
| **FT-891 支持哪些模式** | `ft891.h:35-36 FT891_ALL_RX_MODES` = AM\|CW\|CWR\|SSB\|RTTY\|RTTYR\|PKTLSB\|PKTUSB\|FM\|FMN —— 不含 C4FM / PKTFM / AMN | 有出处；故家族表去掉 `C4FM(0xE)`、`DATA-FM(0xA)`、`AM-N(0xD)`、`DATA-FM-N(0xF)`，余 11 个 |
| **滤波档位（narrow 组）** | `newcat.c:10099-10153`（get 侧逐档）与 `8850-8887`（set 侧）一致：索引 1..17 | 有出处，逐档抄录（§4） |
| **滤波档位（voice 组）** | `newcat.c:10162-10208`（get 侧）：索引 1..21。**Hamlib 自身两处不一致**：set 侧 `newcat.c:8919-8920` 对 >3000 Hz 落到 `w=21` 且注释写 `// 3000 Hz`，get 侧 `case 21: *width = 3200`；另 `ft891.c:245-290 .filters` 列了 SSB 2250 Hz，而 set/get 映射里是 2200/2300 | 有出处，**以 get 侧为准**（"这个索引意味着多少 Hz"），两处不一致写进 provenance |
| **AM/FM 宽度不受 SH 索引控制** | `newcat.c:8924-8936`（set 侧 AM/FM/PKTFM 分支只调 `newcat_set_narrow`，不写 `w`）+ `10217-10228`（get 侧返回固定值：AM/FMN 9000、AMN 6000、FM/PKTFM 16000） | 有出处；故 AM/FM **不进** `filter_widths`——这不是知识缺口，是正确的建模 |
| **`SH` 的 P2 "bandwidth on" 位** | `newcat.c:9658-9663`：`if (is_ftdx101d \|\| is_ftdx101mp \|\| is_ft891) { int on = is_ft891; SNPRINTF(..., "SH%c%d%02d;", main_sub_vfo, on, w); }` → FT-891 得 `SH01NN;`，FTDX101D/MP 得 `SH00NN;` | 有出处；这是 FT-891 与家族唯一的**写格式**差异 |
| 频段 | `ft891.c:202-204` rx 30 kHz–470 MHz 全覆盖；`207-213` tx = `FRQ_RNG_HF` + `FRQ_RNG_6m`（SSB/CW/FM 5–100 W，AM 2–25 W），**无 2m/70cm** | 有出处；复用 `_HF_BANDS`（160m–6m，11 段），与 `ftdx10` 相同 |
| 衰减步进 | `ft891.c:181 .attenuator = { 12, RIG_DBLST_END }` | 有出处 → `(0, 12)` |
| 前置放大 | `ft891.c:180 .preamp = { 10, RIG_DBLST_END }`，Hamlib 自标 `/* TBC */` | 有出处但 Hamlib 存疑 → `{0:"OFF", 1:"AMP1"}` + `TODO(hw-verify)` |
| 功率等级 | `ft891.c:207-213`：100 W 级（AM 25 W） | 有出处 → `power_format="PC1"`、`power_max_w=100`；AM 的 25 W 上限家族同样未建模（单值 `power_max_w`），记 provenance |
| S 表曲线 | `ft891.h:88 FT891_STR_CAL`（16 点，S9 = raw 130），Hamlib 自标 `/* TBC */`；与 `ftx1/ftx1.h:105 FTX1_STR_CAL` **逐点完全相同** | 有出处但 Hamlib 存疑 → 复用同一常量并加注释，`s_meter` 留在 `unverified_meters` |
| Vd/Id 电表 | `ft891.h:51-61 FT891_LEVELS` 含 `ID_METER`、`RFPOWER_METER`、`COMP_METER`，**不含 VD** | 有出处 → 合并标志 `has_vd_id_meters=False`，Id 表因此不显示（已知取舍，记 `TODO(hw-verify)`） |
| VFO-B 直接寻址 | `ft891.h:31 FT891_VFO_ALL` 含 `RIG_VFO_B`；`ft891.c:189 targetable_vfo = RIG_TARGETABLE_FREQ` | 有出处 → `vfo_b_direct=True` + `TODO(hw-verify)` |
| **串口参数（38400 8N1）** | **真机现场**：`20260917-073700-14ef.digest.md` 的 `[HAMLIB]` 段 = `rig_model=1036`（FT-891）、`rig_rate=38400`、`data_bits=8`、`stop_bits=1`、`serial_parity=None`；`hub/mrrc/tests/test_rigctld_supervisor.py:82` 断言同一现场的命令行 `-m 1036 -r COM8 -s 38400 -C stop_bits=1` | **真机证据**。Hamlib `ft891.c:140-145` 的 `serial_rate_min=4800 /* Default rate per manual */` 与 `serial_stop_bits=2 /* Assumed... */` 均不采信；该案"连不上"的根因是 `rigctld daemon not running: timed out`（`hub/mrrc/CHANGELOG.md:252-259`，V6.1.14 修的是自启动），**不是串口帧格式**，故 8N1 无反证 |
| **无 USB 声卡** | **真机现场**：同一份上报的音频设备是 `outputdevice = 扬声器 (2- Realtek High Definition`、`inputdevice = 麦克风 (2- Realtek High Definition` —— 主板声卡，不是 Yaesu USB codec；且老产品配置模板默认按 `USB Audio` 片段自动选设备（`hub/mrrc/CHANGELOG.md:662`），该用户仍落在 Realtek 上，说明系统内没有可选的 Yaesu USB 音频设备。Hamlib 全树无任何 FT-891 音频线索（`ft891.c` 里 "audio" 只出现在 APF 菜单项），属"无正面证据" | **现场证据 + 操作者知识**。⚠️ 评审中途我曾把用户描述里的"音频同步正常"误读为"FT-891 USB 音频可用"，方向是反的：那句话说明**外接/主板声卡路径正常**，坏的是 CAT 那条。本决策以更正后的读法为准 |
| **S 表偏差方向** | **真机现场**：`20260917-073700-14ef.digest.md` 用户描述"S表偏大过电台机显，大概差 1 到 1.5 个 S"（≈ 6–9 dB，按家族 6 dB/S 约定） | 唯一的 FT-891 真机电表观测。**不改曲线**（D9），记入 provenance 与 §10 |
| `ID;` 应答 | 可推但**不采用**（D6）：`newcat.c:58`、`11212-11227`、`51` + FTX-1 交叉验证 | `id_answer=""`，`TODO(hw-verify)` |
| USB 音频采样率 | 无 USB codec，故不存在"电台侧采样率"；取外接接口常态 48 kHz（仓库内 IC-7300 家族已是 48k 端到端免重采样的既证路径，`audio_handler.py:126-131`）；旁证：该现场 `[WDSP] sample_rate = 48000` | **假定** + `TODO(hw-verify)` |

## 4. `FT891` profile（逐字段定值）

```python
FT891 = YaesuModelProfile(
    model_key="ft891",
    display_name="Yaesu FT-891",
    default_baud=38400,
    id_answer="",                        # D6：留空，只记日志（TODO(hw-verify)）
    mode_numbers={                       # 家族表去掉 FT-891 没有的四个
        "LSB": 0x1, "USB": 0x2, "CW-U": 0x3, "FM": 0x4, "AM": 0x5,
        "RTTY-L": 0x6, "CW-L": 0x7, "DATA-L": 0x8, "RTTY-U": 0x9,
        "FM-N": 0xB, "DATA-U": 0xC,
    },
    mode_codes=dict(_FAMILY_MODE_CODES),  # 覆盖 1..F，多余项无害
    ui_modes=_FAMILY_UI_MODES,            # 8 个全在 mode_numbers 里
    filter_widths={...},                  # 见下方两张表；AM/FM 不在其中
    bands=_HF_BANDS,                      # 160m–6m，11 段
    att_steps=(0, 12),
    preamp_labels={0: "OFF", 1: "AMP1"},
    power_format="PC1",
    power_max_w=100,
    has_atu=False,                        # D7
    has_vd_id_meters=False,
    vfo_b_direct=True,
    tune_via="tx2",
    filter_width_prefix="SH01",           # 新字段；家族默认 "SH00"
    s_meter_cal=_FT891_S_CAL,             # == _FTX1_S_CAL（ft891.h:88，Hamlib 自标 TBC）
    audio_rx_rate=48000,                  # D3：无 USB codec，外接接口常态
    audio_tx_rate=48000,
    audio_name_hints=(),                  # D3：不自动匹配任何设备
    verified=False,
    tx_gated=True,
    dual_rx=False,                        # FT-891 单接收
    unverified_meters=("s_meter", "power", "swr", "alc", "comp"),
    provenance={...},                     # §3 每一行的出处，逐表落字
)
```

**narrow 组**（`CW-U`/`CW-L`/`RTTY-U`/`RTTY-L`/`DATA-U`/`DATA-L`，与 `backend.py:103` 的 `narrow_modes` 元组逐字对应），17 档，`newcat.c:10099-10153`：

```
(1,50) (2,100) (3,150) (4,200) (5,250) (6,300) (7,350) (8,400) (9,450)
(10,500) (11,800) (12,1200) (13,1400) (14,1700) (15,2000) (16,2400) (17,3000)
```

**voice 组**（`SSB`/`USB`/`LSB`），21 档，`newcat.c:10162-10208`：

```
(1,200) (2,400) (3,600) (4,850) (5,1100) (6,1350) (7,1500) (8,1650) (9,1800)
(10,1950) (11,2100) (12,2200) (13,2300) (14,2400) (15,2500) (16,2600)
(17,2700) (18,2800) (19,2900) (20,3000) (21,3200)
```

两组索引均升序唯一、Hz 均为正 —— 满足 `tests/test_yaesu_profiles.py:72-82` 的不变量。**索引 0 不写**（其 Hz 取决于 `NA` 开关：CW `narrow?500:2400`、RTTY/PKT `narrow?300:500`、SSB `narrow?1500:2400`，`newcat.c:10110-10116,10166`），本仓库 UI 从不发索引 0，故不建模（D5）。

`filter_tables()`（`backend.py:100-108`）把各模式表拍平成 `voice`/`narrow` 两个去重列表：三张 voice 表逐点相同 → 去重后 21 条；六张 narrow 表逐点相同 → 去重后 17 条。**AM/FM 不进表因此不会污染 voice 列表的索引→Hz 映射**（家族表里 AM `(1,9000)`/FM `(1,16000)` 会在前端 `widths[pair[0]]=pair[1]`（`ft710_ui.js:322`）处覆盖 SSB 的 `(1,3000)`，造成标签失真；FT-891 不复制这个形状）。

`provenance` 至少覆盖测试要求的 6 个键（`mode_numbers`、`filter_widths`、`s_meter_cal`、`bands`、`power_format`、`model_overall`），并额外记 `att_steps`、`preamp_labels`、`audio_rates`、`serial`、`id_answer`、`filter_width_prefix`、`has_atu`、`s_meter_field_feedback`。每条要么点名离线源文件（满足 `PROVENANCE_PATTERN`），要么是显式 `TODO(hw-verify)`。

## 5. 共享核心改动（一处）+ 约束修订

### 5.1 `cat_core.py:set_filter_width`

现状（`backends/yaesu/cat_core.py:416-418`）：

```python
async def set_filter_width(self, index: int) -> bool:
    """`SH00NN;` with the radio's own filter slot number (2 digits)."""
    return await self.set(f"SH00{index:02d}")
```

改为 profile 驱动：

```python
async def set_filter_width(self, index: int) -> bool:
    """`<profile prefix>NN;` — SH + P1(main/sub) + P2(bandwidth on) + 2-digit slot.

    The family prefix is "SH00"; the FT-891 needs P2=1 (Hamlib
    newcat.c:9658-9663, `int on = is_ft891`).
    """
    return await self.set(f"{self._profile.filter_width_prefix}{index:02d}")
```

对四款已发布机型是**字节等价**（前缀默认仍为 `"SH00"`）；FT-891 得到 `"SH01NN"`。**读回侧不改**：`get_filter_width()` 取响应末两位（`cat_core.py:420-427` 的 `resp[-2:]`），`SH00NN` 与 `SH01NN` 都正确解析。

选"字符串前缀字段"而不是"语义 int 字段（`filter_width_on_bit`）"的一个实际原因：`f"SH0{on}{index:02d}"` 会命中 block 约束 `cat-sh-format` 的 pattern `["']SH0\{`，而 `f"{self._profile.filter_width_prefix}{index:02d}"` 不命中任何 pattern。

### 5.2 约束 `cat-sh-format` 修订（`.agents/skills/sdd-guardian/harness/constraints.json`）

**只改 `message`，`patterns` 与 `scope`/`exclude_scope` 一律不动**（不放松护栏）。新文案要点：

- `SH` 的写前缀由 `YaesuModelProfile.filter_width_prefix` 决定：FT-710（已真机验证）与 FTDX10/FTDX101D/FTDX101MP/FTX-1 是 `SH00NN`，**FT-891 是 `SH01NN`**（Hamlib `newcat.c:9658-9663`，`int on = is_ft891`）。
- 规则真正要挡的是**缺位的 3 字符 `SH0NN` 形式**（电台静默忽略），以及"设完必须回读 `SH0`（~150 ms）并广播真实档位"。
- 保留 FT-710 事故溯源（V1.2 频率漂移 / V1.7 SSB 滤波竞态）与 `sdd_ref`。

## 6. 安全与未验证边界

沿用前序 spec §6，无新增机制：

- **TX 闸门**：`tx_gated=True` → `set_ptt(True)`/`set_tune(True)` 返回 False 并每进程记一次日志；**释放方向永不设闸**（`ptt-release-no-verify`、SC8）。`MRRC_ALLOW_UNVERIFIED_TX=1` 才解锁。
- **身份核对**：`id_answer=""` → 连接时只读 `ID;`、把观测字节记 INFO，不做失配告警（前序 spec §6.2 的既有行为）。
- **降级**：`scope_type="none"`、`create_scope_producer()` 返回 `None` → 服务端既有 S-meter 合成器继续供水谱区（FT-891 无 scope 数据流，Hamlib 整个 Yaesu 家族也没有）。
- **电表**：`unverified_meters` 列出的表项照旧上报但标注未验证；不编造数值。
- **串口 I/O**：全部经 `YaesuCatController`（`cat-direct-serial-io` block 约束），不新增裸 serial 访问。
- **启动告警**：`server.py:2715-2722` 的未验证机型告警会自动点名 `_diag_yaesu.py`（按能力族选工具），FT-891 无需改动即被覆盖。

## 7. 接线清单

**代码（9 处）**

1. `backends/yaesu/yaesu_profiles.py` — `YaesuModelProfile` 加 `filter_width_prefix: str = "SH00"`；`_FT891_S_CAL`（复用 `_FTX1_S_CAL` 常量 + 注释说明逐点相同）；`FT891` profile；`PROFILES` 末位追加；模块 docstring 的"None of the four models"→ five
2. `backends/yaesu/cat_core.py` — `set_filter_width` 前缀 profile 驱动（§5.1）
3. `backends/yaesu/backend.py` — `class FT891Backend(YaesuBackend)`（只覆写 `_profile`/`_display_name`）+ `_CLASSES["ft891"]`
4. `backends/yaesu/__init__.py` — 导入 `FT891Backend` + `__all__` + docstring 机型清单
5. `backends/__init__.py` — `_BACKENDS["ft891"] = ("backends.yaesu.backend", "FT891Backend")`（末位）+ 模块 docstring（"four profiles" → five、注册键清单）
6. `config.py` — `_DEFAULT_BAUD_BY_MODEL["ft891"] = 38400`
7. `static/index.html` — 下拉项 `<option value="ft891">Yaesu FT-891（实验性，仅接收，需外接声卡）</option>`（插在 `ftx1` 之后）
8. `_diag_yaesu.py` — `--model` 的 `choices` 追加 `"ft891"`
9. `.agents/skills/sdd-guardian/harness/constraints.json` — `cat-sh-format` 的 message（§5.2）

**明确不改（附理由）**

- `backends/ft710/**` —— 已真机验证路径，冻结（前序 spec D6）
- `cat_core.py:283-285` 的串口 8N1 —— D4：现场证据支持，且改它会波及四款已发布机型
- `macos/first_run.py` —— 只探测 FT-710 与 IC-7300（`:147`），四款 Yaesu 同样不自动探测，FT-891 保持一致
- `static/ft710_ui.js` 的 `getNextFilter` 硬编码轮换表（`:331-345`，voice `[9,13,17,20,23]` / narrow `[3,6,10,13,17,21]`）—— FT-710 遗留；FT-891 的 narrow 1..17 覆盖前 5 个、voice 1..21 覆盖全部 6 个，比家族的 3–4 档更接近 FT-710。改它属于前端重构，超范围（§12 记为已知项）
- `RadioCapabilities` 不新增 `has_usb_audio` 字段 —— 前端无消费它的 UI；空 hints + 下拉标签 + 文档已能诚实表达（YAGNI）。若日后要前端显式提示再加，那是加法、默认 True、对现有 10 个机型零影响
- `.env.example` —— 该文件不含 `MRRC_RADIO_MODEL` 段（已核对），无需改

**测试（既有 6 处必须改，否则套件红）**

- `tests/test_yaesu_profiles.py:19` `EXPECTED_KEYS` += `"ft891"`（末位，与 `PROFILES` 顺序一致）
- `tests/test_yaesu_profiles.py:120-125` `test_audio_hints_are_present` —— **必须改**：断言改为"hints 非空 **或** 该机型声明无 USB 声卡（`audio_name_hints == ()` 且 provenance 记明）"，否则 D3 直接让套件红
- `tests/test_yaesu_profiles.py:172-184` `test_ftx1_is_the_only_multi_band_and_auto_power_model`（PC1 循环加 ft891）、`test_only_explicitly_configured_models_declare_an_id`（ft891 归入 `id_answer == ""` 一侧）
- `tests/test_yaesu_wiring.py:7` `YAESU_KEYS` += `"ft891"`
- `tests/test_backend_factory.py:237-240` 精确元组 += `"ft891"`
- `tests/test_server_ws_protocol.py:1135-1138` 精确元组 += `"ft891"`
- `tests/test_yaesu_backend.py:12` 机型循环 += `"ft891"`

## 8. 测试计划（全部无硬件）

**新增**

- `set_filter_width` 的前缀回归护栏：`ft891` 发 `SH01NN`、`ftdx10` 仍发 `SH00NN`（钉死 §5.1 对老机型字节等价）；读回 `SH01NN` 与 `SH00NN` 都解析出同一索引
- FT-891 档位表与 Hamlib 转写一致（仿 `test_ftdx10_filter_table_matches_hamlib`）：narrow 17 档、voice 21 档逐点断言，含 `(21, 3200)` 这个 set/get 不一致点
- AM/FM **不在** `filter_widths` 里（把 D5 的取舍钉成断言，防止后人"顺手补上"造出无出处的索引）
- 索引 0 不在任何表里（D5）
- FT-891 专属事实：`att_steps == (0, 12)`、`preamp_labels == {0:"OFF",1:"AMP1"}`、`has_atu is False`、`filter_width_prefix == "SH01"`、`audio_name_hints == ()`、`audio_rx_rate == audio_tx_rate == 48000`、`id_answer == ""`、`mode_numbers` 不含 `C4FM`/`DATA-FM`/`AM-N`/`DATA-FM-N`
- 拍平后的能力表：`filter_tables()` 的 `voice` 恰为 21 条且索引唯一、`narrow` 恰为 17 条且索引唯一（防 AM/FM 覆盖失真回归）
- 工厂/能力面：`create_backend("ft891")` → `FT891Backend`，`capabilities.verified is False`、`tx_gated is True`、`scope_type == "none"`、`has_atu is False`；`ServerVisibleSurfaceTests` 自动覆盖（它遍历 `known_models()`）
- TX 闸门：FT-891 上 `set_ptt(True)`/`set_tune(True)` 被拒、`set_ptt(False)` 永不被拒

**既有**：`tests/test_yaesu_fake_radio.py`（pty 假电台）与 Hamlib 模拟器测试保持可选跳过；`test_backend_factory.py:74-84,138-148` 那两处 `assertTrue(has_atu)` 是 FT-710/IC-7300 专属断言（已核对，非全机型循环），不受 `has_atu=False` 影响。

**门槛**：全套件绿（实施前先跑一遍记下基线数，改完同步 `tests/README.md` 计数）→ `python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged` 必须 `clean` → `MRRC_RADIO_MODEL=ft891` 无硬件启动冒烟（预期：ENXIO 分类 + 未验证机型 TX 闸门告警，无 traceback）→ pyright / ruff 干净。

## 9. 文档同步

**必须（`docs-sync` 约束点名）**

- `SDD/03-project-definition.md:11` —— `MRRC_RADIO_MODEL` 枚举 += `ft891`
- `SDD/05-non-functional-requirements.md:39` NFR-032 —— "the ten registry keys" → **eleven**，枚举 += `ft891`；`:72` NFR-067 —— "the four Yaesu models" → **five**
- `SDD/07-subject-area-model.md:14` —— `YaesuModelProfile` 属性列 += `filter_width_prefix`
- `SDD/08-architecture-decisions.md` —— 核对 AD-018/AD-019 措辞是否枚举了"四款"，若有则改为五款并记 FT-891 的两个例外（无 USB 声卡、无内置天调）；`SH` 前缀 profile 化若被视为架构决定，在 AD-018 下补一句而不是新开 AD
- `SDD/11-component-model.md:8` 注册键 += `ft891`；`:13` YaesuDiagnostic "the four Yaesu models" → five
- `SDD/12-operational-model.md:48,60,64` —— "all four Yaesu models" → five；注册键枚举；波特率行
- `SDD/13-feasibility-assessment.md:45` A9 —— "The four Yaesu models" → five，并把 `ft891.c` 加进证据文件列；新增一条风险/假设记录 FT-891 的三处现场事实（8N1、无 USB 声卡、S 表偏高 1–1.5 S）
- `SDD/14-version-history.md` —— 新条目（含"Hamlib 的 `serial_stop_bits=2` 是假设值、现场证据为 8N1"这条更正，以及评审中途音频判断被更正的记录）
- `SDD/README.md:50,55` —— Quick Facts 版本 `V2.71` → `V2.72`、baseline date；"the seven unverified models" → **eight**；`MRRC_RADIO_MODEL` 枚举
- `AGENTS.md:47` —— `backends/yaesu/` 行的机型清单 += FT-891，并写明 `filter_width_prefix`（FT-891 = `SH01`）与"无 USB 声卡"
- `README.md:5,86` —— 型号枚举 += `ft891`；实验性机型段落补 FT-891 的外接声卡要求
- `tests/README.md` —— 模块与计数

**站点（线上 SDD 页面把型号清单写成了枚举，只改代码会与 `known_models()` 不一致）**

- `website/sdd/index.html:297`、`03-project-definition.html:140`、`05-non-functional-requirements.html:313`、`11-component-model.html:136`、`12-operational-model.html:165`、`13-feasibility-assessment.html:340` —— 与对应 SDD 章节同步；`08-architecture-decisions.html:776` 需核对（该处讲的是 Hamlib 自身的文件布局，可能无需改）

**其它**

- `DEPENDENCIES.md:150` —— 小节标题 "Yaesu FTDX10 / FTDX101D / FTDX101MP / FTX-1F (experimental)" += FT-891，并在该节补"FT-891 无 USB 声卡，音频需外接接口"；`:489` 附近的音频设备名启发式段落补 FT-891 的空 hints 例外
- `docs/` 操作指南（`OPERATION_GUIDE.md` 等出现型号清单处）—— 补 FT-891 与外接声卡说明
- **`CHANGELOG.md` 本次不写**：仓库无 Unreleased 段，惯例是条目随发版号落地（四款 Yaesu 机型记在 `## [v1.16.0] — 2026-09-13`）；当前 HEAD 已是发布态 v1.25.3，版本号权威在 `packaging/windows/MRRC.iss`。故 CHANGELOG 条目与版本号留给发版那一步（按 `mrrc-release` 技能）。**这是对评审中"选项 A 含 CHANGELOG"说法的更正。**

## 10. 明确的硬件验收边界

本次全部为文档/现场报告推导。以下留给持有 FT-891 的现场人员用 `_diag_yaesu.py --model ft891` 关闭：

| 项 | 如何关闭 |
| --- | --- |
| `ID;` 实际应答（是否 `0135`） | 诊断报告的观测字节；确认后回填 `id_answer` 并把 `test_only_explicitly_configured_models_declare_an_id` 移到"声明 id"一侧 |
| `SH01NN` 是否真被接受、档位 Hz 是否与 §4 两表一致 | 逐档设 + 回读 `SH0`，与报告里的观测比对 |
| AM/FM 下 UI 显示 SSB 档位标签（§12） | 确认 `NA` 行为后决定：建模 narrow（前序 spec 的 A 方案）或前端按模式隐藏滤波控件 |
| 外接音频接口的实际采样率与设备名 | 设备枚举；48 kHz 假定错了会听出音高偏移 |
| S 表是否确实偏高 1–1.5 S | 已知信号源下对比机显；确认后决定是否需要 FT-891 专属曲线（**不要**用老产品的观测直接改本仓库的表） |
| ATT 12 dB / 前置 10 dB 单档、`AMP1` 命名 | 逐项设 + 回读 |
| PTT/TUNE 时序与 TX 音频路径 | opt-in TX 检查后做一次真实 QSO |
| 38400 8N1 是否稳定（若电台 CAT RATE 菜单被设成 4800） | 连不上时先查电台 CAT 菜单；诊断报告应打印当前假定值 |

## 11. 非目标

- 双接收（FT-891 本就单接收）、真实频谱/瀑布（无 scope 数据流）、RF 与音质验证
- `NA`(narrow) 建模、索引 0 的双语义、AM/FM 的 SH 索引（无此物）
- FT-710 迁移到共享核心（前序 spec 的 phase 3）
- 前端 `getNextFilter` 轮换表重构、`RadioCapabilities.has_usb_audio` 新字段
- 发版（版本号、CHANGELOG、安装包、站点部署）—— 走 `mrrc-release` / `dual-platform-release`
- C4FM/WIRES-X 等数字语音控制（FT-891 无 C4FM）

## 12. 本设计接受的已知缺口

1. **AM/FM 下滤波控件标称失真**：AM/FM 不在 `filter_widths` 里，前端 `_filterTablesFor` 会落到 voice 列表（= SSB 的 21 档），而 FT-891 的 AM/FM 宽度实为固定值（AM 9000 / FM 16000，`newcat.c:10217-10228`）且不受 SH 控制。→ 记 `TODO(hw-verify)`、文档写明、测试钉住"AM/FM 不在表里"。彻底修好需 `NA` 建模或前端按模式隐藏。
2. **Id 电表不显示**：FT-891 有 `ID_METER` 无 `VD`，合并标志 `has_vd_id_meters=False` 把 Id 一起藏了。
3. **AM 的 25 W 上限未建模**：`power_max_w` 是单值 100（家族同样如此）。
4. **`getNextFilter` 轮换表是 FT-710 的**：FT-891 下会多出一个 `--` 档（voice 的 23 不存在）。四款家族机型现状更糟（只有 3–4 档），非本次引入。
5. **无自动探测**：`macos/first_run.py` 不会自动认出 FT-891，操作者必须显式选型号（与四款家族机型一致）。

## 13. SDD 可追溯性

- **AD-018**（profile 驱动的 Yaesu 核心）：FT-891 是该决定的第 5 个实例，并首次证明"写格式差异也能 profile 化"（`filter_width_prefix`）——核心未出现任何型号分支。
- **AD-019**（未验证机型只收不发 + 只读身份核对 + 逐表溯源）：完整适用；FT-891 额外贡献一条经验——**现场支持报告是可以引用的真机证据**（串口参数、有无 USB 声卡、S 表偏差方向三项都由它关闭或定向），这比"无硬件 ⇒ 全部假定"更强。
- **NFR-032 / NFR-067**：注册键从 10 → 11；未验证机型从 7 → 8。
- **§10.4 命令契约**：`filter`/`filter_width` 行的 CAT 命令需注明前缀按机型（`SH00NN` / FT-891 `SH01NN`）。
- **约束**：`cat-sh-format`（block）message 修订（§5.2）；`cat-direct-serial-io`、`ptt-release-no-verify`、`poll-stale-guard`、`docs-sync` 继承且不变。
- **风险**：无真机（缓解：溯源 + 闸门 + 诊断脚本 + 现场报告证据）；Hamlib 自身在 FT-891 上有三处存疑（`serial_stop_bits=2` 假设、`FT891_STR_CAL` 标 TBC、SSB 索引 21 的 set/get 与 `.filters` 三处不一致），均已按"以 get 侧/现场证据为准 + 记 provenance"处理。
