# FlexRadio 的对象模型：RX、SCU、panadapter、slice、天线、stream

> 适用固件：FLEX-6600 v4.3.16.42034，SmartSDR 协议 1.4.0.0。
> 本文的每一条结论都来自**真机实测**（2026-11-16，盒子 `ba1rb`），并注明观测方式。
> 与串口电台（FT-710 / IC-7300）的根本差别是：**Flex 的状态在电台那边，是一个有
> 对象的数据库；客户端只是订阅者之一。** 其余一切缺陷都从这里长出来。

---

## 1. 为什么这台机器"和别的电台都不一样"

| | 串口电台（FT-710 / IC-7300） | FlexRadio（SmartSDR 协议） |
| --- | --- | --- |
| 谁拥有状态 | 客户端（我们）记一份，电台是寄存器 | **电台是唯一权威**，客户端靠 `status` 推送同步 |
| 频谱 | 电台固定 CENTER 模式，VFO 恒在中心 | **panadapter 是独立对象**，有自己的中心、带宽、孔径 |
| 解调器 | 一个电台一个 | **slice**：一条接收链的调谐/模式/滤波/天线/音量 |
| 天线 | 全局一个 `AN` | **接收天线在 slice 上（`rxant`），同一块 pan 上的 slice 共用**；发射天线 `txant` 才是每 slice |
| 多客户端 | 不支持 | **支持**（本机 `licensed_clients=2`）：另一台 GUI 的 pan/slice 的**状态也会推给你** |
| 音频 | 声卡 | `stream`（网络音频流，Opus 或未压缩 PCM） |

因此凡是"我以为电台是我一个人的"地方，都会出错。

---

## 2. 对象层次

```
电台 (radio)
 ├── 能力：model / serial / version / num_scu / num_slice / licensed_clients
 │        available_clients / available_panadapters / max_panadapters / max_slices
 ├── SCU（频谱捕获单元，本机 num_scu=2）
 │     └── 天线插座：ANT1 / ANT2 / RX_A / RX_B / XVTA / XVTB（电台自报 ant_list）
 ├── panadapter（本机 0x40000000 …，max_panadapters 有限）
 │     ├── 状态：center / bandwidth / min_dbm / max_dbm / x_pixels / y_pixels / fps
 │     ├── 输出：**FFT 数据流，stream id == pan id**
 │     └── 挂载：slice 0, slice 1, …
 ├── slice（解调器，本机 num_slice=4）
 │     ├── 状态：RF_frequency / mode / filter_lo,hi / rxant / txant / audio_level
 │     │        rfgain / agc_mode / agc_threshold / nb / nr / anf / pan / active
 │     └── 音频源：我们的 RX `stream`
 └── stream（音频/DAX）
        ├── type=remote_audio_rx / remote_audio_tx
        └── id：0x04000008 这类（**不是** pan id）
```

### 2.1 命令语法：`<对象> <set|create|…> <id> k=v …`

```
display panafall create x=100 y=100      → R<hex handle>|0x40000001     创建 pan
display pan set 0x40000001 center=14.1 bandwidth=0.1 min_dbm=-135 …
slice set 0 rxant=ANT2 mode=USB filter_lo=100 filter_hi=2900
stream create type=remote_audio_rx compression=none
```

> **`slice s 0 …` 是错的**（少 `set`）。SmartSDR 对**对象名不认识**的命令表现为
> "接受、不报错、不执行"。2026-11-16 之前本仓库一直发这个形式，于是**模式、滤波、
> 音频电平、RF Gain、AGC-T、NB/NR/ANF、rxant/txant 全都没有到达电台**。
> 这是整条控制面的单点失效，也是一个教训：**"没报错"不等于"生效了"，必须看状态回读。**

### 2.2 状态推送是**增量**的，而且是**所有客户端**的

`sub pan all` / `sub slice all` 订阅之后，电台把**每一个** pan 与 slice 的状态变化都推给
你 —— 包括另一台 SmartSDR-Win 的。实测（2026-11-16）：同一个 pan id 在 3 ms 内被写成
`min_dbm=-140 max_dbm=-50`（我们）与 `-135/-40`（另一台 GUI）。

**规则：任何 per-object 状态都必须按对象 id 分槽。** 本仓库踩过的两种错法：

- `_update_pan()` 把 `bandwidth_hz` / `pan_center_hz` / 孔径写成**全局**（别人的 pan 一更新
  就覆盖我们）→ 频谱横轴、带宽、像素标定跟着别人走；
- 显示挂在"我 `create` 出来的那块 pan"上，而我们的 slice 还挂在**上一轮**的 pan 上。

---

## 3. 今天的真机测量（每条都可复现）

### 3.1 一块**没有 slice** 的 panadapter 仍然送 FFT，但那是没有射频的底噪

| 观测 | 数值 |
| --- | --- |
| 显示 pan `0x40000001`（无我们的 slice）的原始像素 | 216–228（≈6 dB 动态范围） |
| 换 ANT1→ANT2→RX_A→RX_B 对该 pan 的影响 | **Δ ≤ 0.05 dB** |
| 切到 slice 所在的 pan 后（ANT2 有天线） | 噪底 181、**信号峰 134**（≈22 dB 动态范围） |

**结论：FFT 的 stream id 等于 pan id，而 pan 有没有射频取决于它上面有没有 slice。**

### 3.2 天线：`rxant` 在 slice 上，作用域是 panadapter

官方手册原文（FLEX-6000 系列）：*"All Slices placed on a Panadapter must use the same
receive antenna as the Panadapter."* 即：

- **接收天线**：按 pan 生效，同一块瀑布上的 slice 共用（写哪一条 slice 都一样）；
- **发射天线**（`txant`）：每 slice 独立；
- 名字必须来自电台自报的 `ant_list`（本机 `ANT1,ANT2,RX_A,RX_B,XVTA,XVTB`），
  `txant` 的可选集更窄（`ANT1,ANT2,XVTA,XVTB`）；
- **`slice set <n> ant=` 不存在的键**（静默忽略），正确的是 `rxant=`。

现场后果（2026-11-16）：操作员天线接在 **ANT2**，而 slice 停在 **ANT1**（空口）。

| | 天线 | S 表（2.4 kHz） | 瀑布噪底 | 瀑布最亮桶 |
| --- | --- | --- | --- | --- |
| 坏 | ANT1（空口） | −109.9 dBm | 36.6 | **45** |
| 好 | ANT2（天线） | **−91.1 dBm** | 80.1 | **235** |

S 表 **+19 dB**、亮桶 **45 → 235** —— 这就是"声音有（其实是底噪）、频谱没信号"。

### 3.3 电台**不保留** slice 的 `rxant`

换一次客户端会话（我们每重启一次算一次），电台就把 slice 的 `rxant` 复原成默认口
（现场就是空着的 ANT1）。实测：设成 ANT2 → 重启服务 → 又是 ANT1。

**规则：操作员的选择必须我们自己记住并重放**（`MRRC_FLEX_RX_ANT` +
`engine._restore_antenna()`）。

### 3.4 孔径（`min_dbm` / `max_dbm`）是**电台**的属性

- 我们请求 `min_dbm=-140 max_dbm=-50`，电台实际在用的可以是 `-135/-40`（它会夹）；
- 像素→dBm 只有用**它回报的**孔径才对。用请求值算，整个瀑布就带一个未知 dB 偏移 ——
  这正是"反复调窗口还是黑"的那类 bug（我们连调了三个版本）。
- **`y_pixels` 不是像素量程**：本机回报 `ypixels=100`，而 FFT 字节跑到 228。字节域恒为
  **0..255**。把它当分母会整帧压成 0（我们踩过一次）。
- 个别桶会报 255 之外的值（实测 543 / 888 / 905）：钳到 255 并计数，否则 `255 - pixel`
  会把它折成一个**假中灰**，看起来像弱信号。

### 3.5 声学交叉校验：频谱标定对不对，可以用 S 表验证

同一段噪声，两种带宽下的读数应当相差 `10·log10(BW_demod / BW_bin)`：

```
S 表 −109.4 dBm（2.4 kHz） − 10·log10(2400/118) ≈ −122.5 dBm
实测每桶噪底（100 kHz / 850 桶 = 118 Hz）      = −122.7 dBm   ✓
```

**这是判断"频谱数据是不是真的"最快的办法**，不需要信号源。

---

## 4. 由此定下的实现规则

| 规则 | 代码位置 | 守卫用例 |
| --- | --- | --- |
| **pan 跟着 slice 走**：连上先找我们的 slice 在哪块 pan，在那儿就地显示 | `engine._slice_pan()` / `ensure_pan()` / `ensure_slice()` | 真机日志 `our slice is already on panadapter 0x40000000` |
| **只有自己的 pan 能改自己的状态** | `engine._update_pan()`（按对象 id 分槽） | `test_another_clients_pan_never_touches_our_axis_or_aperture` |
| **slice 命令必须是 `slice set <n>`** | `backend._slice_set()` | `SliceCommandFormTests` |
| **像素→dBm 用回读孔径；字节域 0..255** | `engine._ingest_fft()` | `test_the_radios_edge_artifacts_are_flattened` 等 |
| **横轴以 pan 的实际起点/带宽为准**（`scope_axis_from_pan`） | `static/ft710_ui.js:_scopeAxis()` + `scope.py` | `ScopeAxisContractTests` |
| **天线选择要记住并重放** | `config.FLEX_RX_ANT` + `engine._restore_antenna()` | 真机重启验证 |
| **保存的设置必须写进 systemd 真正读的那个 env 文件** | `server._config_file_path()` | `tests/test_server_setup.py` 等 |

---

## 5. 排查这一类问题的顺序（下次别再猜）

1. **看 `/api/status` 的 `antennas.pans`** —— 你的 slice 在哪块 pan 上？那块 pan 是不是
   你正在显示的那块？`ours` 对不对？
2. **看天线**：`rxant` 是不是接天线的那个口？（本机天线在 ANT2）
3. **换天线做对照**：S 表动、频瀑不动 ⇒ 你显示的不是接收到射频的那块 pan。
4. **算声学交叉校验**（§3.5）：每桶 dBm 与 S 表差 13 dB 左右才对。
5. **看原始像素**：median 与 span（动态范围 < 8 dB ⇒ 没有射频；20 dB 以上 ⇒ 有信号）。
6. 才去怀疑**显示侧**（窗口、亮度、颜色表）。

---

## 6. 已知未做

- `rfgain`（SCU 的射频增益）量程未证：`has_rf_gain=False`，不拿 0–255 的串口语义撒谎。
- `agc_threshold`（AGC-T 0–100）现在**真的能调**了（此前被 `slice s` 吃掉），待现场确认效果。
- AM 滤波对称化、FFT 首尾伪影的宽度（现为抹平首 26 / 尾 12 桶）仍以实测为准。
- 发射链（TX stream / interlock）尚未在真机上键控验证（TX 门禁 `MRRC_ALLOW_UNVERIFIED_TX` 始终关）。
