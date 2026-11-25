# 盒子代刷 MRRC-Passport 固件（AD-043）

- 状态：实现中（2026-11-25）
- 涉及：`passport_flash.py`、`server.py`（`/passport` + 4 个 `/api/passport/*`）、
  `static/passport.html`、`static/passport.js`、`dev_tools/make_passport_firmware.py`、
  `requirements.txt`（`esptool`）、`downloads/passport/firmware.json`（新目录）

## 1. 要解决的问题

MRRC-Passport 是一支 ESP32-C3 手持控制器。出厂时它**没有任何网络配置**，用户必须：

1. 手机连上设备自己开的热点（`MRRC-xxxx`），
2. 在它的配网页里输入家里的 Wi-Fi 名称与口令，
3. 再输入盒子的地址与网页口令。

三步都要在手机的小键盘上敲，而且第一步在 iOS 上会弹出「本网络无法上网」的警告。
客户拿到手的第一件事就是一次失败风险不低的配置 —— 这是产品体验的缺口，不是设备缺陷。

**盒子已经知道这一切**：它自己在哪个 Wi-Fi 上、它的局域网地址、它自己的网页口令。
它还是唯一一个既有 USB 口、又常驻在现场的计算机。所以：把设备插到盒子上，
在盒子的页面里点一次，把**固件 + 配网**一起写进去。

## 2. 决策

### D1 · 盒子刷，不用浏览器的 Web Serial

| 方案 | 为什么不选 |
| --- | --- |
| 浏览器 Web Serial（esptool-js） | 只有 Chrome/Edge 支持；必须 HTTPS + 安全上下文；设备要插在**操作员**的电脑上，而操作员的电脑往往不在这台盒子的网段里；1.7 MB 固件要先从官网下到浏览器再写出去 |
| 盒子刷（选定） | 设备插在盒子 USB 口；盒子有网、有 Python、有串口；页面只发四个字段和一个「开始」 |

代价：盒子要装 `esptool`（进 `requirements.txt`），**这条功能没法用热修补丁投递**
（补丁只搬文件，不装包）。所以老盒子会明确说「本机没有 esptool」，而不是静默失败。

### D2 · 固件目录独立于升级通道

`downloads/passport/firmware.json`，**不**塞进 `latest.json`：

```json
{"product": "MRRC-Passport",
 "builds": [{"version": "1.2.1", "chip": "esp32c3",
             "url": "https://www.vlsc.net/mrrc_modern/downloads/passport/mrrc-terminal-1.2.1-merged.bin",
             "size": 1720320, "sha256": "…", "notes": "AP 配网 + 频道回放"}]}
```

`latest.json` 是**升级通道**：`upgrade_core.parse_manifest` 读它，一条坏记录能让一整版
盒子升不了级。烧录固件是另一条链路、另一群用户、另一套失败模式，混在一个文件里等于
让一次固件发布有了让盒子变砖的权限。两个文件，两处爆炸半径。

### D3 · 自己写 NVS，而不是让用户手输

配网落在 NVS 分区（`0x9000`，`0x6000` 字节），命名空间 `cfg`，键 `ssid`/`pass`/`host`/`srv_pass`
—— 与设备固件 `main/mrrc_cfg.c` 的 `CFG_NS` 完全一致。

`passport_flash.build_nvs_image()` 是 ~80 行标准库代码，**与 ESP-IDF 官方生成器
`esp-idf-nvs-partition-gen` 逐字节比对过**（同一组输入，SHA-256 相同；
`tests/test_passport_flash.py::NvsImageTests` 把 SHA 钉死）。理由：格式写错不报错，
NVS 只会说「没有空闲页」，设备静默退回编译期默认值 —— 也就是**刚刚写进去的 Wi-Fi 悄悄丢了**。
逐字节相等是这个格式唯一可信的证据。

### D4 · 失败要说实话

| 情况 | 行为 |
| --- | --- |
| 端口不对 / 没插 | 只跑 `chip_id`，报「把板子按住 BOOT 再插」 |
| 芯片不是 `esp32c3` | **在第一次写之前**中止 |
| 固件 SHA 不符 / 大小不符 | 不落地（写 `.part` 再 rename），报实测值与期望值 |
| NVS 回读与意图不符 | `ok=false`，报 `read-back mismatch` 并把差异值列出来 —— 绝不显示「完成」 |
| 回读长度不足 | 同上（不是「截断成功」） |
| 盒子没有 esptool | `501` + 「需要 1.28.99 及以后的镜像」 |

NVS 写失败时设备**仍可用**：用户还能用设备自己热点上的配网页手输那四个值
（页面把刚输入的值显示出来，方便照抄）。所以这不是死路，只是退化成原来的流程。

### D5 · 口令不经过 HTTP

- Wi-Fi 口令：`nmcli` 不会把 PSK 交给非 root 用户（也不该交），所以**必须由操作员输入一次**。
  SSID 预填（盒子当前所在的网络）。
- 盒子网页口令：留空即用**服务端自己配置的** `WEB_PASSWORD`。它从不出现在
  `GET /api/passport/state` 的响应里，也不会被页面回显。
- `suggest.host` 预填当前请求的 host（局域网点进来就是局域网 IP），可改成公网域名。
- 所有写进 `job.lines` 的文本都过 `redact(line, psk)` —— 那个日志面板会被截图、
  会进支持包。二进制回读内容**永不**进 `lines`（24 KB 乱码），只记「N 字节已回读」。

### D6 · 轮询，不是 WebSocket

一次刷写 30–60 秒、进度是一行行文本。`GET /api/passport/state` 带 `job` 一起回，
页面在 `state == "running"` 时 1 s 轮一次。不新增 WS 通道：新增通道就要新增鉴权、
新增 `client_count` 记账、新增 keyed-socket 分支，而这里连音频都没有。

同一时刻只允许一次刷写（模块锁 + `409`）：两条 esptool 抢一根 USB 总线 = 一块半写的板子。

## 3. 端口与硬件

- 候选端口：`/dev/ttyACM*`、`/dev/cu.usbmodem*`（原生 USB-Serial-JTAG，C3 默认）、
  `/dev/ttyUSB*`、`/dev/cu.usbserial*`（CH340/CP210x 等桥）。
- **电台自己的端口永远不在候选里**：`candidate_ports(..., radio_port=...)` 排除
  `cat.port` 与 `MRRC_SERIAL_PORT`。把 1.7 MB 写进 FT-710 的 CAT 口不是「失败」，是事故。
- W103D 只有两个 USB 口，都被 FT-710 占着（CAT + scope）。所以页面顶部写明
  「刷之前拔掉电台线」；拔掉后电台掉线、插回去自动恢复（`restart_rx()` 已有此路径）。
- 设备侧 MT7663S 的 Wi-Fi 与 USB 共用一个 SDIO/PHY 子系统，长时间满速写 flash 时
  Wi-Fi 可能短暂抖动 —— 页面在刷写期间不依赖推送，1 s 轮询丢了也能续上。

## 4. 发布链路

```
~/HAM/mrrc-passport$ idf.py build
~/HAM/hub/mrrc_modern$ dev_tools/make_passport_firmware.py \
    --build ~/HAM/mrrc-passport/build --version 1.2.1 \
    --notes "…" --out downloads/passport
```

`make_passport_firmware.py`：

- 以**数据**方式解析 `build/flash_args`（不 `eval`），自己把 bootloader(0x0) /
  分区表(0x8000) / app(0x10000) 铺进一个 4 KB 对齐、其余 `0xFF` 的缓冲区
  —— 也就是 `esptool merge_bin` 的结果，但不依赖本机装 esptool。
- 重叠放置、缺分区表、缺文件一律报错（重叠意味着这份 `flash_args` 不是我们以为的那个）。
- **拒绝 `-dirty` 构建**：ESP-IDF 把 `git describe` 烧进镜像，`-dirty` 意味着
  目录里的版本号和实际字节对不上；后续排查会拿到一份无法复现的固件。
- 更新目录时按版本号倒序、同版本替换、内部字段 `_filename` 不落地。

## 5. 验收

| 项 | 方式 |
| --- | --- |
| NVS 镜像字节正确 | 与 ESP-IDF 生成器 SHA-256 相等（离线） |
| NVS 回读解析正确 | 往返解析 + 篡改一个字节必须报错 |
| 合并镜像布局 | 三个 magic 各在其偏移、间隙为 `0xFF`、`0x9000` 擦除态 |
| 错误芯片不写 flash | 假 runner：只跑 `chip_id` |
| 回读不符 → 失败 | 假 runner 返回别的 SSID |
| 日志里没有口令 | 断言 `job.lines` 不含 PSK / 网页口令 |
| 真机 | 一块 Passport + 一台盒子，刷完拔线插到 USB 电源，看它自己连上 Wi-Fi 并在盒子里显示为一个终端会话 |

真机项未做前，`/passport` 页面顶部标注「未在真机上验收」——
与 AD-019 的未验证机型发射门禁同一个原则：不要让一行绿字代替一次实测。
