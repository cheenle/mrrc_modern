# W103D 盒子 DDNS（w103d.vlsc.net AAAA）· 设计

- **日期**：2026-10-11
- **状态**：已批准（用户当日口头确认），进入实现计划
- **相关**：`docs/W103D_GUIDE.md`、`packaging/box/`、`~/dns/`（既有 DDNS 脚本族）

---

## 1. 背景与动机

W103D 盒子（192.168.1.217）需要一个固定名字方便寻址。`~/dns/` 下现有一套阿里云 AliDNS 的 DDNS 脚本族
（`dns6.sh`、`aliddns*.py`、`ddns.py` 等），但全部**绑定在 macOS 上跑**，且各管各的子域（`p9`/`p6`/`w4`/`radio`）。

本设计把这套脚本的**可复用部分**（AliDNS REST 签名调用）改造为一个跑在盒子上的自更新器，负责维护
`w103d.vlsc.net` 的 **AAAA** 记录，指向盒子当前的出网 IPv6 地址。

### 现场证据（2026-10-11 实测）

- 盒子 `ip -brief addr`：只有 **wlan0 UP**（持 `192.168.1.217/24`）；eth0 DOWN。
- wlan0 全球 IPv6：`2409:8a00:1894:9940:c4c1:3f5e:173f:96d1/64`（中国移动家宽前缀），
  `dynamic`、非 `temporary`；默认路由 `via fe80::1 dev wlan0 proto ra`。
- `ip -6 route get 2400:3200::1` → `src 2409:8a00:…:96d1`；`curl -6 https://ifconfig.co` 回显同一地址
  → **内核认可的出网源地址 = 该全球地址**。
- 操作者 Mac 在同一 `/64`（`…:1867:f61c:698e:b325`）→ **从内网无法验证公网入站可达性**（本设计范围外）。
- 盒子已备：`curl`、`jq`、`python3`（均有，**无需安装任何东西**）。

> **实现期修正（2026-10-11）**：签名原定用 `jq @uri` 做 RFC3986 编码，预检实测其会把 `! ' ( ) *` 留着不编码，
> 不符合阿里云要求。改为**一次 `python3 -c` 完成编码 + HMAC-SHA1 签名**（皆为 stdlib，无新增依赖）；
> `jq` 仍用于 JSON 解析，`openssl` 在签名路径上不再需要。

### 既有脚本被改造时暴露的缺陷（需在新脚本中修正）

| 缺陷 | 位置 | 新实现的做法 |
| --- | --- | --- |
| 取地址用 `ifconfig en1`（macOS 专属） | `dns6.sh`、`aliddns_optimized.py` | `ip -6 route get`（Linux，内核裁定源地址） |
| "IP 未变则跳过" 分支只 `echo skipping`，不退出 | `dns6.sh` | 先 Describe 再比对，相同即退出（零写请求） |
| Timestamp 预先 URL-encode（`%3A` 手写进参数） | `dns6.sh` | 参数保持原文，统一走 `urlenc()` |
| 走 `http://` 提交 AccessKeyId | `dns6.sh` | `https://alidns.aliyuncs.com/` |
| AccessKey 明文硬编码在 6 个文件里 | 全目录 | 盒子侧收敛到一个 600 权限的 env 文件 |

## 2. 目标与非目标

**目标**

1. 盒子上有一个自更新器：把 `w103d.vlsc.net` 的 AAAA 记录**追着盒子当前出网 IPv6** 走，前缀变化后自动跟上。
2. 断电重启自愈；记录被改坏能自愈；地址取不到时**宁可不动，也不写坏记录**。
3. 零新增依赖（不装 pip 包、不建 venv、不动系统包）。

**非目标（范围 A）**

- **不做公网入站验证**，不改光猫 / 路由器 IPv6 防火墙，不动 sshd。
- 不做 IPv4（A 记录）；不做其他子域；不做证书 / 反向代理。
- 不改 `~/dns/` 里任何既有 macOS 脚本的行为。

## 3. 方案选型

| 方案 | 结论 |
| --- | --- |
| **① 纯 shell + AliDNS REST API + systemd timer** | **采用**。零依赖；`dns6.sh` 的签名套路已被实际使用验证；~120 行可完全审计 |
| ② Python + 阿里云 SDK（改 `aliddns_optimized.py`） | 否决。要 pip 装 SDK：污染 MRRC 服务 venv，或为 100 行的活单开 venv，维护成本 > 收益 |
| ③ 方案 ① + NetworkManager dispatcher 事件触发 | 暂缓。可把"最坏 5 分钟"降到秒级，但多一处系统集成；跑稳后可作为增强叠加 |

## 4. 布局与边界

```
盒子 /opt/w103d-ddns/
├── w103d-ddns.sh      # 唯一脚本（bash，755），带 --dry-run
├── ddns.env           # 密钥与配置（600，root 独占），脚本运行时 source
└── README.md          # 排障手册：手动跑 / 看日志 / 验证 / 改周期 / 卸载

/etc/systemd/system/
├── w103d-ddns.service # Type=oneshot
└── w103d-ddns.timer   # OnBootSec=90s + OnUnitActiveSec=5min
```

- **master 副本**在 `~/dns/w103d/`（该目录不是 git 仓库；`ddns.env` 含真实密钥，**不进任何仓库**）。
- 本仓库（`mrrc_modern`）只收设计/计划两份文档，**不含任何密钥**，也不含脚本副本。

**不改动**：MRRC 服务、apt 源、sshd、光猫、`~/dns/` 既有脚本、镜像构建（`packaging/`）。

## 5. 脚本行为

### 5.1 单次运行数据流

```
读配置 → 取地址 → 查记录 → 比对 → (新建|更新|什么都不做) → 一行日志
```

1. **取地址**
   - 主路径：`ip -6 route get 2400:3200::1` 取 `src`（AliDNS 的 v6 地址，只用于路由查询，不产生流量）。
   - 回退：默认路由接口上第一个 `scope global` 且非 `temporary`/`deprecated` 的地址。
   - **校验闸门**：地址必须落在 `2000::/3`；**不合格或取不到 → 退出 1，本轮不更新**（绝不写坏记录）。
   - 测试钩子：`W103D_FORCE_ADDRESS` 强制指定（同样过校验闸门）。
2. **查记录**：`DescribeSubDomainRecords`（`Type=AAAA`，`SubDomain=w103d.vlsc.net`）
   - `TotalCount=0` → `AddDomainRecord`
   - `TotalCount=1` 且值相同（经 `norm6` 文本归一）→ 打印 `无变化`，**不发写请求**
   - `TotalCount=1` 且值不同 → `UpdateDomainRecord`（带 `RecordId`）
   - `TotalCount>1` → 退出 2（异常状态，不猜，人工核查）
3. **日志**：每次一行进 journald（`无变化` / `已新建` / `已更新` / `ERROR: …`）。

### 5.2 签名与传输

- AliDNS OpenAPI 签名：参数按 key 排序 → RFC3986 编码（`python3` 的 `urllib.parse.quote`，`safe=""`）
  → `StringToSign = GET&%2F&<编码后的canonical query>` → `HMAC-SHA1(secret + "&")` → Base64；
  编码与 HMAC 同在一次 `python3 -c` 调用内完成（见 §1 实现期修正：`jq @uri` 不符合 RFC3986）。
- 端点 `https://alidns.aliyuncs.com/`；`curl --max-time 20 --retry 2`。
- 响应必须是 JSON；含 `Code` 即 API 错误，连同 `Message` 打印后退出 3。

### 5.3 并发与退出码

- `flock -n /run/w103d-ddns.lock`：timer 与手动运行不打架；抢不到锁打印"跳过本轮"退出 0。
- 退出码：`0` 成功/无变化，`1` 配置或取地址失败，`2` 记录数异常，`3` AliDNS API 失败，`64` 参数错误。

## 6. systemd 驱动

```ini
# w103d-ddns.service
[Unit]
Description=更新 w103d.vlsc.net 的 AAAA 记录
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
ExecStart=/opt/w103d-ddns/w103d-ddns.sh

# w103d-ddns.timer
[Unit]
Description=每 5 分钟更新 w103d.vlsc.net 的 AAAA 记录
[Timer]
OnBootSec=90s
OnUnitActiveSec=5min
AccuracySec=30s
Unit=w103d-ddns.service
[Install]
WantedBy=timers.target
```

配置由脚本自行 `source /opt/w103d-ddns/ddns.env`（systemd 与手动运行共用同一份配置，不设 `EnvironmentFile`，
少一个错位点）。TTL=600（AliDNS 免费版下限）。

## 7. 安全边界

- `ddns.env` 为 `600 root:root`；脚本本身不含密钥，可自由复制审计。
- AccessKey **复用 `~/dns/` 既有那把**：权限面不扩大（本来就能改整个 `vlsc.net` 的解析），只是多一处存放。
- 范围 A 不开放任何入站；但**记录一旦公开可解析**，若哪天确认网关放行入站，盒子的 root 口令（当前仍为
  出厂 `1234`）必须先换、并改走密钥登录 —— 写进 README 警示，本设计不执行。

## 8. 验收（全部真机实操）

| # | 步骤 | 通过判据 |
| --- | --- | --- |
| 1 | `w103d-ddns.sh --dry-run` | 打印出网地址与将执行动作；不产生写请求（Describe 成功即证明签名/凭据正确） |
| 2 | 首跑（真实） | 日志 `已新建`；`dig @223.5.5.5 AAAA w103d.vlsc.net` == 盒子当前出网地址 |
| 3 | 幂等：再跑一次 | 日志 `无变化`，记录值不变 |
| 4 | 自愈：`W103D_FORCE_ADDRESS=<Mac 地址>` 跑一次（记录被改坏）→ 去掉钩子再跑 | 记录恢复为盒子地址 |
| 5 | 守卫：`W103D_FORCE_ADDRESS=fd00::1` 跑一次 | 退出码 1、无 API 写请求；记录不变 |
| 6 | `systemctl enable --now w103d-ddns.timer` | `list-timers` 显示下一轮；journald 能看到连续两轮运行；`systemctl is-failed` 为空 |

## 9. 已知取舍

- **5 分钟窗口**：前缀变化到 DNS 跟随之间最坏 5 分钟（TTL 600 已注定传播延迟以分钟计，不值得为秒级加复杂度）。
- **记录公开性**：`w103d.vlsc.net` 出现在公共 DNS 里（这是"分配域名"的题中之义）；安全措施见 §7。
- **移动家宽前缀会变**：地址是 RA 下发的动态前缀 + 稳定接口标识；前缀一变整条地址都变，timer 负责跟随。
- 记录条数受 AliDNS 免费版配额限制（现有子域已若干条），新增 `w103d` 一条。
