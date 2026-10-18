# NanoVNA-H 天线诊断台（`/Nano`）设计

- **日期**：2026-10-18
- **状态**：设计定稿，待实现（第一期：测量 + 设备管理；第二期：固件升级）
- **范围**：接在盒子 USB 上的 NanoVNA-H / H4（`0483:5740`，ChibiOS CDC-ACM），
  实测机为 `NanoVNA-H 4`，firmware `1.2.14`
- **相关代码（计划）**：`nano/`（新包：`vna.py` / `analysis.py` / `flash.py`）、
  `static/nano.html`、`static/nano.js`、`server.py`（只做接线与守卫）
- **相关文档**：SDD 新增 **AD-030**；`docs/current/antenna/`（既有天线画像与
  ATR-1000 学习库）；`packaging/common/`（第二期的 `dfu-util` 与 udev 规则）

---

## 1. 背景与动机

现在测天线是「电台发载波 + ATR-1000 读反向功率」反推 SWR（见
`docs/current/antenna/`，一次全段扫描约 20 分钟，312 点，`step_khz=10`,
`dwell_s=1.2`），代价大、只能得到标量 SWR，还得停掉服务端避开 SWR 守卫。

一台 NanoVNA 直接给出**复数 S11**：SWR、阻抗 R+jX、相位、谐振点、Smith 图，
全是同一次测量的副产品。2026-10-18 把它插到盒子上，实测确认可用后决定接进本仓。

### 1.1 实测事实（下列数字全部来自这台设备，不是文档抄来的）

| 项 | 实测 |
|---|---|
| USB | `0483:5740` STMicroelectronics，`ChibiOS/RT Virtual COM Port`，序列号 `400` |
| 节点 | `/dev/ttyACM0`；`/dev/serial/by-id/usb-STMicroelectronics_ChibiOS_RT_Virtual_COM_Port_400-if00` |
| `info` | `Board: NanoVNA-H 4`，`Version: 1.2.14 [p:401, IF:12k, ADC:384k, Lcd:480x320]`，STM32F303，`Build Time: Aug 31 2022` |
| `vbat` | 3627 mV |
| 标定 | **已加载**（`data 2`…`data 6` 的 cal 数组非零） |
| 出厂扫频 | 50 kHz – 900 MHz / 101 点 |
| 与电台的关系 | CAT 是 CP2105（`ttyUSB0/1`），频谱是 FT4222，声卡是 C-Media —— **ttyACM0 只属于 NanoVNA** |
| 服务用户 | `mrrc` 已在 `dialout`（`ttyACM0` 是 `root:dialout 0660`），无需改权限 |

### 1.2 协议契约（读固件源码 + 线上实测双向核对）

`hugen79/NanoVNA-H` 的 `main.c` 是权威，实测一致：

| 命令 | 语义 |
|---|---|
| `scan <start> <stop> <points> <mask>` | 一次扫频后**吐出数据**，mask 位：`1`=频率 `2`=S11 `4`=S21 `8`=不用标定 `16`=不补偿电延迟 `32`=不补偿 S21 offset `128`=二进制 |
| `scan_bin …` | 同上，二进制帧：`u16 mask, u16 points`，随后每点 `u32 freq` + 每个启用的数组各 `f32×2` |
| `data <0..6>` | `0`=S11、`1`=S21（measured），`2..6`=标定数组。**频率不在 `data` 里** |
| `frequencies` | 当前扫频点频率（每行一个）；这是"进来时设备显示什么范围"的唯一来源 |
| `sweep <start> <stop> <points>` | 只设频率表（并触发一次设备侧扫频），用于还原显示 |
| 其余 | `info` `version` `vbat` `power 0-3\|255` `offset <Hz>` `bandwidth` `edelay <s11\|s21> <ps>` `s21offset <dB>` `smooth 0..8` `cal` `save/recall 0..4` `threshold` `time` `reset dfu` |

**速度**（`scan_bin`，实测三档）：

| 点数 | 耗时 | ASCII 等价体量 | 二进制体量（freq+S11） |
|---|---|---|---|
| 101 | **0.32 s** | 6.0 KB | 1.2 KB |
| 201 | 0.63 s | 12.0 KB | 2.4 KB |
| 401 | **1.26 s** | 23.9 KB | 4.8 KB |

结论：**这是仪器，不是流**。401 点上限约 0.8 Hz，页面按"按一次扫一次 / 1 Hz 定时"设计，
不沿用 SDR 的瀑布流形态。

**副作用**：`scan` / `sweep` 会改掉设备屏幕上的扫频范围。这是本设计里唯一
"用户没要求却被改变了"的设备状态，见 §4.4。

## 2. 目标与非目标

**目标**

1. 一条命令画出天线画像：SWR、阻抗、Smith、谐振点，1–54 MHz 401 点 **1.3 秒**。
2. 存盘 + 叠加对比（干/湿、改前/改后），导出 CSV 与 `s1p`。
3. 设备管理：身份、健康、标定槽、安全设置项，一屏可见可控。
4. 固件升级（U3：默认上锁，`MRRC_NANO_ALLOW_FLASH=1` 才出现），带版本回读与回退。
5. 不破坏设备：扫描后设备状态与进入页面之前一致；发射中永不动设备。

**非目标（YAGNI）**

- **不做校准向导**：OPEN/SHORT/LOAD/THRU 是物理动作，页面只显示标定状态与
  `save`/`recall` 槽位。引导一串可能做错的步骤，不如让人看着设备屏幕做。
- **v1 不显示 S21**：采集路径留 `s21` 参数（协议上是同一个 `scan_bin`），
  但页面只画 S11——双端口要夹具和转接头，没人要求之前不做界面。
- 不做多设备：同一时刻一台 VNA。
- 不替代 ATR-1000 学习库：VNA 给的是"天线本身"，天调给的是"调谐后的结果"，
  两者并存，页面不写学习库。

## 3. 架构总览

```
static/nano.html ── nano.js ──fetch──> server.py（路由 + 守卫，不碰串口细节）
                                          │
                                          ├── nano/vna.py       设备与协议（唯一碰串口）
                                          ├── nano/analysis.py  纯数学：Γ→SWR/Z、谐振、导出
                                          └── nano/flash.py     升级（DFU）
                                                │
                                    /dev/ttyACM0 或 0483:df11
```

单元边界：

| 单元 | 做什么 | 依赖什么 | 怎么测 |
|---|---|---|---|
| `nano/vna.py` | 发现端口、收发帧、扫描、读设置、还原范围 | pyserial（可注入假 I/O） | 抓包 fixture 回放，无硬件 |
| `nano/analysis.py` | 复数→物理量、谐振识别、对比、CSV/s1p | 无（纯函数） | 纯单测 |
| `nano/flash.py` | 取镜像、校验、`dfu-util`、自检、凭证 | `net_tls.urlopen`、`dfu-util` 子进程 | 假子进程 + 假 HTTP |
| `server.py` | 路由、守卫、序列化 | 以上三者 | TestClient + 假设备 |

`vna.py` 不知道 HTTP，`analysis.py` 不知道设备，`flash.py` 不知道页面——
每一层都能单独解释、单独替换。

## 4. 详细设计

### 4.1 端口发现与占用检查

顺序（第一个命中为止）：

1. `MRRC_NANO_PORT`（显式指定，运维逃生口）
2. `/dev/serial/by-id/usb-*ChibiOS_RT*`（Linux；本机就是这条）
3. `/dev/cu.usbmodem*`（macOS 上 CDC-ACM 的命名）
4. `/dev/ttyACM*`

然后**必须验身份**：打开后发 `version`，1.5 s 内收到形如 `1.2.14` / `0.8.0` 的
版本串才算数。只看设备名是错的——CH340 克隆板会伪装成 `ttyUSB`，FT-710 的 CAT
也是 `usb serial`。

**占用检查**：解析后的端口若等于 CAT 串口（`MRRC_SERIAL_PORT`，以及它在
`/dev/serial/by-id` 里的真实路径），直接拒绝打开并报 `port_conflict`——
抢占电台 CAT 口的代价远大于少一个页面。

### 4.2 传输层

- pyserial，`timeout=0.2`，写入后按"读到提示符 `ch> `"或"读满 N 字节"收敛。
- CDC-ACM 忽略波特率，仍写 115200（pyserial 要求一个值）。
- 所有访问串行化：`server.py` 一把 `asyncio.Lock`（一次一件，见 §4.7），
  `vna.py` 内部不假设并发。
- **帧长自校验**：二进制帧长度必须等于 `4 + points × (4 + 8×(1+s21))`；
  不等就丢弃整帧、重扫一次、再不等就报错。**绝不半截解析**——半截数据会画出
  一条看起来正常的假曲线，比报错危险得多。
- 掉线（`SerialException` / 帧超时）标记 `offline`，2 s 退避后按 §4.1 重新发现；
  页面显示"掉线"与手动重连，不猜。

**2026-10-18 真机实测补上的三条（都在传输层，写进代码注释与本表）**：

1. **发命令前必须与 `ch> ` 同步**。直接 `reset_input_buffer()` 再写，会在设备还在
   吐上一条回复时把新行塞进去——实测表现不是"回错"，是 `scan_bin` **一个字节都
   不回**（401 点连续两次空帧）。同步用 `in_waiting` 先看一眼，没人在说话就立刻
   走（第一版无条件读到超时，每条命令白等 0.6 s，一次扫描从 0.32 s 变成 1.05 s）。
2. **按帧长读满，不按"静默多久"判断帧尾**：401 点扫 1.26 s、数据分段到达，空闲
   判据会在段间截断。读够 `4 + points × stride` 再多收 20 ms 收尾。
3. **`data 0` 紧跟 `sweep` 会读到上一轮的数组**：`sweep` 只是启动一次设备侧扫描，
   命令立刻返回，`measured[][]` 还是旧的。实测同一个频点在新旧两套栅格上相差
   1.0（标量），索引越大偏得越多。这正是**只用 `scan_bin` 的自带输出**的理由 ——
   一条命令里拿到的频率与数据天生同一次扫描。

**关于 SDD 约束 `cat-direct-serial-io` 的边界**：那条 AD-002 规则管的是**电台 CAT**
的串口（必须经 `CatController`/`CivController` 串行化并 `to_thread` 卸载）。
NanoVNA 是**另一台仪器、另一条线**，不共享任何电台状态，所以它的串口访问放在
`nano/vna.py` 里 —— 但同样的纪律照搬：`server.py` 一把 `asyncio.Lock` 串行化 +
`asyncio.to_thread` 卸载，从不阻塞事件循环。实施时把 `nano/vna.py` 加进该规则的
`exclude_scope` 并在这里留一句话，而不是让守卫静默放过整个 `nano/`。

### 4.3 扫描

```python
sweep = vna.scan(start_hz, stop_hz, points)   # 默认 s21=False
# 内部: 命令 "scan_bin <start> <stop> <points> 131"
#       mask = 0x80(BINARY) | 0x01(FREQ) | 0x02(S11) = 0x83
```

掩码**不含** `8/16/32`：要标定、要电延迟补偿——那才是"天线在 50Ω 下的真实阻抗"。
每次扫描后立即还原设备显示（§4.4）。

边界约束：`50 ≤ start < stop ≤ 900e6`，`points ∈ {101, 201, 401}`（设备上限实测 401；
三者是固件里 `POINTS_SET` 的档位，别的值不是不行，只是没人验证过，先不开放）。

### 4.4 还原设备显示范围（"统一"决策）

进页面第一次读 `frequencies` 得到 `(start, stop, points)` 原文，存进
`<nano 运行目录>/state.json`（§4.9）。**每次扫描结束后无条件 `sweep <原文>` 还原**。

理由：设备是共享的物理物件，谁最后动过它就决定了下一个人看到什么。让它永远
回到你要它保持的样子，就不存在"现在屏幕上是哪一段"这个问题。代价是每次扫描
多 0.3 s（101 点）～1.3 s（401 点）——用户已明确接受。

若 `state.json` 缺失（新盒子/手工删了），以进页面时读到的值为准并立即落盘。
还原失败只记日志 + 状态里标记 `restore_failed`，不影响本次数据新鲜有效。

### 4.5 分析（`nano/analysis.py`）

输入 S11 复数数组 + 频率数组，纯函数：

| 输出 | 公式 |
|---|---|
| SWR | `(1+\|Γ\|)/(1−\|Γ\|)`；`\|Γ\|→1` 时封顶 99.9（开路不是无穷大，是"超出量程"） |
| 阻抗 | `Z = Z0(1+Γ)/(1−Γ)`，`Z0=50` → `(R, X)` |
| 幅相 | `\|Z\|`、`arg(Z)` |
| 谐振 | SWR 局部极小且 `< 2.0`，把连续低于 1.2 的游程并成一个平台并取中心频率 |
| 可疑点 | **单点** SWR≈1.00 而两侧都 >1.35 时标注 `suspect`（沿用
  `antenna-sweep` 的掉读判据：孤立 1.00 是读数事故，不是谐振） |
| 对比 | 两次扫描按频率对齐（点数不同则线性插值到较细的栅格），输出 ΔSWR |

导出：`CSV`（freq, s11_re, s11_im, swr, r, x）与 `s1p`（Touchstone v1，只 S11）。
`s1p` 是为了让数据能流出去，不被本仓绑死。

### 4.6 HTTP 与页面

页面 `GET /Nano`（大写字面，按用户要求）；资源 `static/nano.html` + `nano.js`，
**前缀安全**（`URL_BASE` 取自 `location.pathname`，同 `listen.js`），
`?v=` 手工 bump，不写进 `sw.js` 预缓存（它是仪器，不是主界面）。

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/nano/status` | GET | 连接状态、身份、版本、vbat、标定、当前设置、当前扫频、`tx`、`flash{enabled,variant}` |
| `/api/nano/scan` | POST | `{start, stop, points}` → `{freqs, s11, swr, z, elapsed_ms}`；**不落盘** |
| `/api/nano/settings` | POST | 白名单设置（§4.8） |
| `/api/nano/cal` | POST | `{slot, action: save\|recall}`，`save` 要 `confirm` |
| `/api/nano/sweeps` | GET / POST | 列表 / 保存最近一次扫描（`{label, note}`） |
| `/api/nano/sweeps/{id}` | GET / DELETE | 读取（`?format=json\|csv\|s1p`）/ 删除 |
| `/api/nano/firmware` | GET | 官方可用版本 + 本地已下载镜像 + 变体记忆 |
| `/api/nano/flash` | POST | 启动一次升级 job（§4.10） |
| `/api/nano/flash/status` | GET | job 状态机快照（页面轮询） |

页面布局（移动优先，沿用琥珀暗色主题）：

- 顶栏：设备型号 / 版本 / 电池 / 掉线提示
- 主图：SWR–频率（对数 X 轴），叠加上一次保存的曲线（虚线）
- 副图切换：Smith 图 / `|Z|` / `R+jX`
- 控制条：起止频率 + 预设（1–54 MHz 全段、3.5–30 HF、单波段）、点数 101/201/401、扫描
- 底部抽屉：设备信息、设置项、标定槽、存盘/导出、升级（受 `MRRC_NANO_ALLOW_FLASH` 控制）
- 顶部横幅：`tx=true` 时常驻"电台在发射，已锁定扫描"

### 4.7 守卫（四条，每条都有现场理由）

1. **发射中拒绝一切设备操作**：PTT / TUNE / FT8 / CQ 任一在发 → `409 tx_active`。
   NanoVNA 的 CH0 就接在天线上，几十瓦射频进去是**烧前端**，不是 UI 问题。
2. **一次一件**：模块级 `asyncio.Lock`；第二个请求立刻 `409 busy`（附当前操作），
   不排队——排队会让人以为页面死了。
3. **端口不许抢**：§4.1 的占用检查；`vna.py` 打开前再核一次。
4. **角色隔离**：`listen`（只听）与 `sdr` 会话一律 `403`；烧写额外要求
   `MRRC_NANO_ALLOW_FLASH=1`（U3），且 job 一次只能有一个。

### 4.8 设置白名单

| 设置 | 命令 | 值域 | 页面 |
|---|---|---|---|
| 电延迟（S11/S21） | `edelay s11\|s21 <ps>` | 设备回读值 | 数字输入 + 回读确认 |
| 平滑 | `smooth <0..8>` | 0–8 | 滑杆 |
| 测量带宽 | `bandwidth` | 设备档位 | 下拉（先读当前值再列档） |
| 输出功率 | `power <0..3\|255>` | 0–3，255=自动 | 下拉 |
| 频率偏移 | `offset <Hz>` | 整数 | 数字输入 |
| S21 offset | `s21offset <dB>` | 浮点 | 数字输入（折叠在高级里） |
| 标定槽 | `save\|recall <0..4>` | 0–4 | `recall` 直接；`save` 需确认 |

**禁止暴露**：`clearconfig`、`reset`（除升级流程里的 `reset dfu`）、`touchcal`、
`sd_delete`、`sd_read`、`usart_cfg`、`tcxo`。

`tcxo` 单独说明：它是整台仪器的频率基准，一个手滑就是全段偏移，且从页面上
看不出错在哪。留在命令行，不给手机误触的机会。

### 4.9 存储

`MRRC_NANO_DIR`（默认 `<可写运行目录>/nano`），原子写。**一个基目录，四个用途**，
不再有第二处路径：

```
state.json            设备进入前的扫频原文（§4.4）
config.json           变体记忆 + 升级偏好（§4.10）
firmware-state.json   每次升级的凭证（§4.10）
sweeps/               sweep_<startMHz>-<stopMHz>_<points>_<YYYYmmdd-HHMMSS>.json
sweeps/latest.json    最近一次
firmware/             下载或上传的镜像
```

JSON 只存**原始复数 S11** 与参数，SWR/R/X **存盘时不落**（能算出来的东西不存第二份）：

```json
{"version":1,"id":"...","created":"2026-10-18T16:40:00Z","label":"40m 雨前",
 "note":"","start_hz":1000000,"stop_hz":54000000,"points":401,
 "device":{"board":"NanoVNA-H 4","version":"1.2.14","serial":"400"},
 "s11":[[0.9995,0.0025], ...]}
```

### 4.10 固件升级（U3）

**为什么能安全地做**：`reset dfu` 走的是 `boardDFUEnter()` →
`__set_MSP(STM32F303xC_SYSTEM_MEMORY)`，即跳到芯片**系统存储区**里的 ROM
bootloader。那段代码不可擦写，所以最坏情况是 App 区烧了一半、设备重启后停在
DFU，再刷一次即恢复——**不存在刷成砖头无法恢复**。代价是刷坏后设备要人工按一下
电源开关，这一点必须写在界面上。

**状态机**（一个 job，一次一个）：

```
idle → checking   取官方 releases（`net_tls.urlopen`），与设备当前版本比较
     → variant    选 H4-MS / H4-SI / H4-ZK（默认取 config.json 记忆，首次必须选）
     → confirming 勾选「设备会重启、屏幕会黑、需要重新插拔或按电源」+ 输入确认字
     → dfu_enter  `reset dfu`，等 ttyACM0 消失（≤5 s）
     → flashing   等 0483:df11 出现 → `dfu-util -a 0 -s 0x08000000:leave -D <镜像>`
     → verifying  等 0483:5740 回来（≤15 s）→ 重开串口 → `info` 读版本
     → self_check 开路端口三点扫频，记 |S11|（**建议值，不作为判据**）
     → done | failed（可重试）| variant_mismatch（换变体再刷）
```

**变体为什么必须人来选**：`-D__MS5351__` 与 ZK 是**编译期**开关，运行时任何命令
都读不出来（`info` 只说 `Board: NanoVNA-H 4`）。刷错变体不会砖，但时钟/RF 会不对。
所以：首次必须显式选，之后记住；`self_check` 的 |S11| 若离 1.0 太远就提示
"变体可能选错了"，而不是假装成功。

**镜像来源**（两条，都核 SHA-256）：

1. 官方 Releases（`api.github.com` 的 `digest: sha256:…` 字段，实测存在）
2. 本地上传 `.bin` / `.dfu`

镜像落 `<nano 运行目录>/firmware/`，变体记忆落 `config.json`，每次升级写完
`firmware-state.json`：镜像名、SHA-256、刷前版本、刷后版本、时间、自检值。**升级是否成功由刷后设备自己报的版本说话**，不由 `dfu-util` 的返回码说话。

**回退是真实存在的**：你现在装的 `1.2.14` 在 releases 里有
`NanoVNA-H4-{MS,SI}_20220831.bin`，页面直接提供"刷回上一版"。

**系统依赖（第二期）**：`dfu-util`（bookworm 源，134 KB，依赖 libusb）+
一条 udev 规则（`0483:df11` → `GROUP="dialout"`，照 `packaging/common/` 里
FT4222 那条的写法）。两条都要进 `packaging/common/`，**重建镜像才生效**——
这也是分两期的原因。

## 5. 测试与验收

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_nano_vna.py` | 抓包 fixture（`info` / `vbat` / `frequencies` / `scan_bin` 帧，已录）、帧长自校验、半帧丢弃、掉线重连、端口发现优先级、CAT 冲突拒绝 |
| `tests/test_nano_analysis.py` | Γ=1+j0 → SWR 封顶、Γ=0 → 1.00、Γ=−1+j0 → 封顶、Z 换算、谐振平台、孤立 1.00 标 `suspect`、不同点数对比插值、CSV/s1p 格式 |
| `tests/test_nano_flash.py` | 变体表、SHA-256 校验、`dfu-util` argv、状态机每条迁移、失败路径（不进 DFU / 不回来 / 版本不符） |
| `tests/test_server_nano_routes.py` | 四守卫（TX 中 409、并发 409、listen/sdr 403、无 flag 403/404）、前缀安全 |

真机验收（盒子，`NanoVNA-H 4` / `1.2.14`）：

1. 401 点 1–54 MHz：页面 < 2 s 出图；设备屏幕恢复原范围。
2. 拔掉 VNA：状态显示掉线；插回：自动恢复，不需要重启服务。
3. 发射中（PTT 按下）扫描：`409 tx_active`，设备端口未被打开。
4. 存盘 → 杀进程重启 → 列表仍在、曲线仍能画。
5. 升级到 `1.2.50` → `info` 报新版本 → 回退到 `1.2.14` → 版本回读正确。

## 6. 风险与未决

| # | 风险 | 处理 |
|---|---|---|
| R1 | 变体选错（MS/SI/ZK 运行时不可读） | 显式选择 + 记忆 + 自检提示 + 可重刷；不会砖 |
| R2 | 刷写中途断电 | ROM bootloader 在系统区，重刷即可；界面写明"按一下电源" |
| R3 | 抢占 CAT 端口 | 端口占用检查，硬拒绝 |
| R4 | 设备被别的程序占用（nanoVNA-QT / Saver） | 打开失败 → 明确报"端口被占用"，不重试到死 |
| R5 | 发射把前端打坏 | TX 守卫（§4.7 第 1 条）+ 页面常驻横幅 |
| R6 | ATR-1000 在线时测的是天调端口，不是天线 | 页面注明；不自动关天调（那是另一条链路的事） |
| R7 | 401 点 1.3 s，有人以为卡住 | 扫描中显示进度与已用时间，按钮置灰 |

未决：无。三处曾待定（代码目录、权限、还原策略）已于 2026-10-18 定：
`nano/` 包、主站口令、每次扫描后自动还原。

## 7. 里程碑

| 期 | 内容 | 验收 |
|---|---|---|
| 第一期 | `nano/vna.py` + `nano/analysis.py` + 页面 + 6 条路径（`status`/`scan`/`settings`/`cal`/`sweeps`/`sweeps/{id}`） | 第 5 节 1–4 条通过 |
| 第二期 | `nano/flash.py` + `dfu-util`/udev 进 `packaging/common/` + 页面升级抽屉 | 第 5 节第 5 条通过 |
| 收尾 | SDD AD-030 + 版本历史 + AGENTS.md 模块表 + README + `tests/README.md` 计数 | sdd-guardian `check --staged` 干净 |
