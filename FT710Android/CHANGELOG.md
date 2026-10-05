# FT710Android Changelog

App 版本独立于服务端版本；全功能需服务端 ≥ v1.22（txhb 闸门），更低版本自动降级。

## [1.1.0] — 2026-10-05

- **修复阻断缺陷**：v1.0.1 无法解析真实 `fullState`——服务端 `bands` 是对象数组（`name/start/end/bsr/default_freq`）、`filterTables` 是 `[idx,hz]` 数对，而 DTO 写成了 `List<String>/List<Int>`，每次解码抛异常、事件被丢弃，登录后一直“连上但无状态”。现按真实形状解析并用与服务端同形的 fixture 锁死（D0）
- **量程修复**：Scope SPAN 改用服务端 `capabilities.scope_spans`（`civ27` 半幅 ×2），旧版“50k/100k/1M”实际发的是 1k/2k/5k；标尺/点击 QSY 与设置页共用同一张表
- **主屏对齐手机端 Web**：点击瀑布/FFT QSY、点击频率数字输入 MHz、ATR1000 行（PWR/SWR/L-C + TUNE，含自动调谐阶段提示）、状态行码率 ↓↑/RTT/抖动、无声告警、TUNE 状态、连接开关 ⏻、全屏 ⛶、记忆频道 label、未验证机型“实验性”徽章
- **设置对齐**：Scope 面板（全量程/SPD/6 套配色/Floor/Ceil/Spec H/WF H）、RF Gain（0-100%↔0-255）、Mic Gain（本地持久化并在重连后回推）、🎙 Vol、🔊 Vol、NR/NB 电平、波段/模式选择器、Memory Manager（含清除）、机型与版本显示
- **新增**：Cloud Hub 接入向导（`/api/cloud/state|apply|refresh|restart`，含 `cert_reload_required` 的重启流程）、「🐞 遇到问题」与「连接设置」浏览器入口
- **音频**：本机播放音量（web 🔊 Vol 语义）+ 每种机型的 `audio_gain_boost`（FT-710 = 10×）+ TX/TUNE 期间静音防自噪回声；TX 采集 48k 优先、只给 44.1k 时 882↔960 重采样；Mic Vol 本机软件增益
- **后台接收**：`mediaPlayback` 前台服务 + 常驻通知（可断开），退后台继续听；TX 仍在 `onStop` 无条件释放
- **录音面板**：进度拖动 seek + 列表合计（条数/字节）
- **测试**：JVM 单测 52 → 99（协议真实形状、能力表、频率解析/QSY、ATR、重采样、增益、6 套调色板锚点、Cloud REST）；`test + assembleDebug + lintDebug + assembleRelease` 全绿
- 发版：`versionCode 3`；APK 与官网（en/zh）下载卡同步更新

## [1.0.1] — 2026-10-03

- **界面按手机端 Web 全量重做**（用户要求「根据手机端 WEB 更新」）：设计令牌逐字取自 `static/ft710.css :root`（`#1a1a1a` 底 / `#f59e0b` 琥珀 / `#ef4444` 红 / 圆角 6·10·14 / 等宽频率），新增 `UI/Theme.kt` 挂成全局 Material3 深色主题，登录/设置/录音面板一并继承
- 主屏布局对齐 Web：`☰ + 07.013.50` 大号琥珀频率 + VFO 徽标 → 状态行（波段 / 琥珀模式徽章 / RX·TX / Serial 点）→ **蓝色 jet 瀑布 + 青色 FFT + 红色中心标记 + 琥珀频率标尺** → 分段 S 表（S1…+40 刻度）→ 仪表 PWR·ALC / SWR·Id(青)·Vd(紫) → 五键行 → NR·NB·AN·COMP + ATU 芯片 → 音量滑条 → `◀◀ ◀ [1k] ▶ ▶▶` 步进 → VFO-A/B·A=B·SPLIT → 录音入口 → 记忆格；底部固定**红 PTT + CQ + 黄 TUNE**
- 瀑布配色改为实现 Web 的 `WF_PALETTES.jet` 分段函数，逐像素一次成图（Bitmap + 256 色查表），替代原逐行 drawRect
- 波段循环表移植 Web `DEFAULT_BAND_CYCLE`（12 波段含默认频率，未知波段→80m、末位→160m）
- 新增 JVM 测试：波段循环 4 条、jet 调色板锚点 2 条（共 52 条全绿）
- 发版：`versionCode 2`；APK 与官网下载卡同步更新
- `release.sh` 修复：同步刷新官网 hero 安卓按钮上的版本文字（旧版只改下载卡，导致按钮写着 v1.0.0 而链接已指 v1.0.1）；找不到该按钮时直接报错，不再静默放过

## [1.0.0] — 2026-10-03

- 首个公开发布：官网签名 APK（LAN + Cloud Hub 接入），显示名 MRRC Modern，应用图标，minSdk 26
- 新增：txhb 发射心跳（声明服务端活性闸门能力，AD-007 V2.63 / SDD15 §15.6）
- 新增：录音面板——启停、列表、本地播放 + 拖动进度、导出（系统分享）、删除（AD-017）
- 新增：一键 CQ 呼叫，含进度与中止（AD-020）
- 修复：连接指示灯永远灰；设置页不可达；只读登录（4003）静默失败
- 发布：`release.sh` 一键构建/签名/上传/线上 SHA-256 复核；keystore 仓库外保管
