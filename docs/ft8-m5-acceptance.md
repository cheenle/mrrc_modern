# FT8 M5 真机验收（W103D 盒子 + FT-710）

- **日期**：2026-10-12
- **机器**：ZTE W103D（aarch64）`192.168.1.217` / `w103d.vlsc.net`，FT-710 接 `/dev/ttyUSB0` + C-Media USB 音频（44.1 kHz）
- **版本**：mrrc_modern `feat/ft8`（本次部署 = `feat/ft8` HEAD；盒子底版 1.25.5）
- **部署通道**：本地 `stage-tree.sh` → rsync → 盒子 `mrrc-update`（见 §4 事故记录）

## 1. 验收结果

| # | 项目（规格 §6） | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | 解码：切 FT8 收到实台解码 | ✅ | `decodes: 4 / slots: 6`（20m，14.074 MHz，2026-10-12 23:15 UTC+8）；引擎日志 `FT8 engine started (profile 3, threads 3)`、`RX tap delivering (dev 44100 Hz -> 12000 Hz)` |
| 2 | TX：CQ 一轮 · 电台键控 · 波形 12.6 s | ⏸ **待操作者**（发空信号需呼号设置与用户在场；平台 TX 门控与 FT8 safety 已在本地测试覆盖） | 见 §3 待办 |
| 3 | QSO 落库 | ⏸ 同上（依赖 2） | — |
| 4 | 切换往返 ×10：无 PTT 残留、台况恢复、音频不断流 | ✅（本轮 3 次往返：ft8→ssb→ft8→ssb） | `safety.ptt_on=false`、`stop_count=1`（切回走安全停机路径）；SSB 页音频/频谱期间未中断（平台 Opus 流独立于 tap） |
| 5 | FT8 TX 中紧急 STOP | ⏸ 待 TX 验收（STOP 端点本身已测：任意已认证会话可用） | — |
| 6 | 性能：单时隙有余量、SSB 音频不断 | ✅ | M1 基准：profile 3 / threads 3 = **2.14 s**（判据 ≤8 s）；真机 FT8 模式期间平台 CPU ~14%、DSP worker ~5% |

**API/页面**：
- `GET /api/ft8/state` → 200 JSON（sequencer/safety/orchestrator/station）
- `GET /api/ft8/health` → 200 JSON
- `GET /static/ft8.html` → 200
- `POST /api/ft8/mode {ft8|ssb}` → 200；`POST /api/ft8/radio/band {freq_hz}` → 200（真机把它拨到 14.074）

## 2. 联调中发现并修复的真缺陷（全部已在 feat/ft8 修复）

| # | 症状 | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | `/api/ft8/*` 返回 SPA 的 index.html（JSON 变 HTML） | SPA 兜底路由 `app.add_api_route("/{path:path}", ...)` 在**模块末尾**注册，Starlette 按注册顺序匹配——lifespan 里追加的路由永远排在它后面 | FT8 REST/WS 改为**模块级**挂载（`_LateBound` 代理，lifespan 只 bind 组合根） |
| 2 | `/api/ft8/mode` / `radio/band` 一律 403 | 平台角色字符串是 `operator`/`listener`，适配层按 `control` 判定 | `require_lease` 与 `AuthShim` 对齐为 `operator` |
| 3 | 引擎起来但**零解码**（`slots_skipped` 增长） | `_audio_rx_loop` 的省电门：**无客户端时不读 PCM**，FT8 tap 因此永远没有音频 | idle 门加 `_ft8_tap_live` 条件（FT8 模式保持读 PCM） |
| 4 | 无法判断 tap 是否在工作 | 缺现场可判读的日志 | `rx_tap` 首次投递打 INFO（设备率/输出率/首块样本/epoch） |
| 5 | `orchestrator` 计数不出现在 state/health | `Ft8AppState.orchestrator` 在构造时是 None（真正实例在 `runtime.start()` 才创建） | state 增加 `runtime` 引用，快照从 `runtime.orchestrator` 读 |

## 3. 待操作者完成的验收（TX 相关）

1. 在 FT8 页面设置 **呼号 / Grid**（设置抽屉；当前为空，仅影响高亮与 AP，不影响解码）
2. 执行 `CQ` 一轮：确认键控、功率输出、SWR；波形 12.6 s
3. 完成一次真 QSO 并检查落库（`/api/ft8/logs/qsos`）
4. FT8 发射中从另一会话发 STOP，确认 PTT 立即释放
5. 切换往返 ×10 的完整台况字段核对（频率/模式/滤波/功率/AGC/ATT/PREAMP）

## 4. 部署事故记录（**重要教训**）

**事实**：首次 `mrrc-update` 后 `mrrc-modern` 反复重启失败（`Failed to load environment files: No such file or directory`）。
**根因**：盒子上的 `/usr/local/bin/mrrc-update` 是 **1.25.5 镜像带的旧版**，它的 rsync **带 `--delete`**；
用它更新时，源树里没有的**运行时目录被删除**：`env/`（口令与证书路径）、`profiles/`（11 机型）、
`recordings/*.mp3`（5 个录音）、`.local/share/MRRC-Modern/fleet/`（Cloud Hub 隧道配置）。

**恢复**：部署前做的 `/root/mrrc-modern-backup-2026-10-12.tgz` 全量恢复上述四类数据；
`env/` 恢复后服务立刻 active。

**另有第二处**：首次手动 rsync **绕过 `stage-tree.sh`** 直接同步原树，把开发机上
`fleet/frpc`（Mach-O）覆盖了盒子的 aarch64 frpc（`Exec format error`，Cloud Hub 隧道起不来）。
从备份恢复。

**已落实的修复**：
1. `/usr/local/bin/mrrc-update` 更新为**现行版本**（无 `--delete`，脚本自身承诺 "env file left untouched"）；
   旧版留在盒子 `/root/mrrc-update.old-2026-10-12` 备查。
2. 部署流程固定为：**本地 `packaging/common/stage-tree.sh` 生成 stage 树 → rsync → `mrrc-update`**，
   排除清单只在 `stage-tree.sh` 一处（本次绕过它正是事故的第二个成因）。
3. 部署前**必须**先备份 `/opt/mrrc_modern`（本次 126 MB tgz 恢复了三类数据）。

> **给镜像线的行动项**：旧镜像（≤1.25.5）的 `mrrc_update.sh` 带 `--delete`，任何用它做升级的现场机
> 都会删运行时数据。1.26.0 起的镜像已是新版；仍跑旧版的机器在升级前需先替换该脚本或先备份。

## 5. 未完成（后续版本）

- FT4；桌面客户端（`/desktop`）；band hunter；JTDX ADIF 同步；auto-call-new-DXCC；RUMLogNG（均首版非目标）
- FT8 页面的瀑布与设置抽屉的完整联调（页面骨架与解码列表已随部署上线）
- DSP 编译烘焙进镜像（当前为设备上原生编译，见 M1 基线 §1）
