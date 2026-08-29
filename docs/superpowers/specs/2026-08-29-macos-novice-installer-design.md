# macOS 小白零配置安装包设计 (v1.13.0)

日期：2026-08-29
状态：已获用户确认

## 1. 目标与背景

上次 macOS 包是 v1.7.0（rebrand 前、`MRRC-FT710.app`、Python 3.12）。当前仓库已到
v1.12.x（Windows v1.12.1 已发布），macOS 打包侧滞后：spec/文档仍引用旧名
（`ft710_server.spec`、`MRRC-FT710`），首次运行体验对小白不友好（需手动改密码、串口、
电台型号，FT4222 真频谱不随包）。

本次目标：**macOS 安装包装完即可用，用户视为纯小白** —— 不用终端、不碰配置文件：

1. dmg → 拖入 Applications
2. 首次右键 → 打开（一次；ad-hoc 签名，用户无 Apple Developer ID，不公证）
3. 菜单栏图标出现 → 服务自动启动、浏览器自动打开登录页
4. 登录页横幅显示已自动生成的密码 → 输入即用
5. FT-710 开箱即真 FFT 频谱（FT4222 dylib 随包）

版本定为 **v1.13.0**（新功能，CHANGELOG 顶部 `[Unreleased]` 改名而来）。

## 2. 架构决策

**首启自动配置放在 launcher 侧（macOS 菜单栏 app），不放 server 侧。**

理由：`config.py` 在模块 import 时就绑定 `SERIAL_PORT`/`WEB_PASSWORD`/`RADIO_MODEL`
常量；若由 server 探测后改 `os.environ`，常量不会更新，需重构 config 为动态读取。
launcher 在 spawn server **之前**完成检测并把最终值写回 env 文件 → server 拿到的就是
成品配置，零侵入。

探测协议知识以轻量独立实现放 `macos/first_run.py`（原始 pyserial，几行），不复用
asyncio 后端控制器（避免在 launcher 里拉起整个后端）。

## 3. 组件改动

### 3.1 新模块 `macos/first_run.py`

纯函数、可注入依赖（便于单测）：

- `needs_first_run(env: dict) -> bool`
  任一为真则需首启配置：`MRRC_FIRST_RUN_DONE` 未设置、`MRRC_WEB_PASSWORD` 为空/等于
  `config.DEFAULT_WEB_PASSWORD`、`MRRC_SERIAL_PORT` 为空/等于平台默认。
- `generate_password() -> str` — `secrets.token_urlsafe(16)`。
- `detect_serial_ports(comports: list | None = None) -> list[str]`
  用 `serial.tools.list_ports.comports()` 列 `/dev/cu.*`，**按优先级排序返回**：
  description 含 CP210x / SLAB / USB-Serial 的在前，其余 `cu.*` 在后；无则空列表。
- `probe_radio_model(port, open_func=serial.Serial, timeout=1.0) -> str | None`
  - FT-710：38400，发 `ID;`，读响应；以 `ID` 开头 → `"ft710"`。
  - IC-7300：115200，发 CI-V ID 帧 `FE FE 00 E0 19 <xor> FD`（xor=0x00^0xE0^0x19）；
    收合法 `FE FE ... 19 <model> ... FD` 帧 → model 字节映射 `"ic7300"` / `"ic7300mk2"`
    （0x94 → ic7300；MK2 模型字节按 `docs/IC-7300MK2_CI-V_Knowledge_Base.md` 核实）。
  - 均无响应 → None。
- `apply_first_run(env: dict, config_path: Path) -> dict`
  **逐候选串口探测**：按 `detect_serial_ports` 优先级，对每个端口先试 FT-710 再试
  IC-7300，第一个返回有效型号的 (端口, 型号) 胜出并写回。全部失败 → 保留配置默认
  型号 `"ft710"` + 首个候选端口（若有）。填密码 + `MRRC_FIRST_RUN_DONE=1`。
  写回 env 文件。返回更新后的 env。

探测是只读的（每个端口一次 ID 查询），对电台无害。

### 3.2 `macos/launcher.py`

- `main()` 里 `ensure_config()` 之后、`start_server()` 之前调用 `apply_first_run`。
- 若本次生成了密码（`MRRC_AUTO_PASSWORD=1`），`rumps.notification` 弹一次
  「🔑 你的登录密码已自动生成：XXXX」。
- 菜单增加 **Show Password…** 项（读取 env 中当前密码显示），弥补通知可能被系统
  权限/遗漏吞掉。
- `build_command()` 不变（`--no-ssl`）。

### 3.3 `server.py`（小改）

- 登录页：`MRRC_AUTO_PASSWORD=1` 时服务端在 `GET /login` 返回的 HTML 中注入横幅
  「首次运行已自动生成密码：XXXX」。不新增公开 API 泄露密码。
- 新增 `GET /api/setup`（登录后，auth-gated）：返回
  `{first_run_done, auto_password, radio_model, serial_port, audio_rx_device, audio_tx_device}`，
  供主界面首启欢迎横幅（一次性可关）显示自动检测结果，也便于排障。

### 3.4 打包脚本 / spec

- `packaging/pyinstaller/mrrc_modern_server.spec`
  - datas 平台化：darwin 打包 `macos/default.env → macos/`，非 darwin 才打包
    `windows/default.env → windows/`（当前无条件打包 windows，mac 包内冗余）。
  - 确认 `serial.tools.list_ports` 进 bundle（pyserial 已是依赖）。
- `packaging/macos/mrrc_modern_launcher.spec`
  - hiddenimports 增加 `serial`、`serial.tools.list_ports`、
    `serial.tools.list_ports_osx`、`first_run`。
  - pathex 增加 `ROOT/macos`（或等效），保证 `import first_run` 可解析。
- `macos/default.env`：`MRRC_WEB_PASSWORD=`、`MRRC_SERIAL_PORT=`、
  `MRRC_RADIO_MODEL=` 置空 → 触发自动配置（空值由 `apply_first_run` 填充）。
- FTDI：`cp lib/libft4222.dylib lib/libftd2xx.dylib vendor/ftdi/macos/`
  （universal arm64 已确认；build.sh 现有逻辑会拷入
  `Contents/MacOS/vendor/ftdi/macos/`，launcher 设的 `MRRC_FTDI_LIB_DIR` 命中）。

### 3.5 文档

- `docs/MACOS_INSTALLER_GUIDE.md` 重写为小白向：下载 → 拖入 Applications → 首次右键
  打开 → 菜单栏图标 → 登录页密码 → 用。加常见问题（电台不识别、密码在哪、真频谱）。
- `mac_pack.md` 更新：Python 3.13、dylib 已就位、新首启流程、版本提取容忍 `[Unreleased]`。

### 3.6 版本与 CHANGELOG

- `CHANGELOG.md` 顶部 `## [Unreleased] — 2026-08-26` → `## [v1.13.0] — 2026-08-29`，
  追加本功能条目（macOS 小白零配置首启：自动密码/串口/电台探测 + FTDI 随包）。
- build.sh 从 CHANGELOG 自动取到 v1.13.0。

## 4. 数据流（首次运行）

```
launcher main()
  ensure_config()                  # 从 default.env 生成用户 env（若缺）
  load_env()
  needs_first_run? ──是──> apply_first_run():
                              generate_password / detect_serial_ports / probe_radio_model
                              (逐候选串口探测；胜出端口+型号写回)
                              write env + MRRC_FIRST_RUN_DONE=1
  start_server(env)  ──env──> server: config.py 读到成品配置
  通知密码 + 打开 http://127.0.0.1:8888
  server 登录页注入密码横幅 (MRRC_AUTO_PASSWORD=1)
  登录 → 主界面 /api/setup 欢迎横幅（检测结果）
```

## 5. 测试策略

- `tests/test_first_run.py`（新增）：注入 fake `serial.Serial` 与 `list_ports`
  - 密码生成长度/随机；needs_first_run 判定矩阵
  - 串口候选排序（CP210x 优先）
  - FT-710 探测（`ID017;` 响应 → ft710）；IC-7300 探测（CI-V 0x19 帧 → ic7300）；
    无响应 → None → 回落 ft710
  - apply_first_run 写回 env 文件、置 DONE
- 全量 `python -m unittest discover -s tests` 必须全绿（当前 651）。
- 构建后冒烟：挂载 dmg、`codesign -dv`、结构检查、`hdiutil` 校验和。

## 6. 明确不做（YAGNI）

- 不做 Developer ID 签名/公证（用户无账号；ad-hoc + 右键一次）。
- 不做 x86_64 支持（仅 Apple Silicon arm64）。
- 不做「网页内改密码」向导 —— 改密码走菜单栏 Edit Configuration…（指南说明）。
- 不改 Windows 打包。

## 7. 验收标准

- `dist/macos/MRRC-Modern-v1.13.0-arm64.dmg` 产出，测试全绿。
- 全新用户路径：装后右键打开一次 → 菜单栏 → 自动密码登录 → 射频控制可用。
- FT-710 真频谱：app 内 `Contents/MacOS/vendor/ftdi/macos/` 含两个 dylib，频谱走 FT4222。
