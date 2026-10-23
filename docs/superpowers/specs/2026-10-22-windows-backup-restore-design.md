# Windows 换电脑：配置与证书一键备份/恢复脚本设计

- **日期**：2026-10-22
- **状态**：设计定稿，待实现
- **范围**：**只要一个独立发布的 Windows 脚本**（`tools/windows/` → `website/downloads/`）。
  不改安装包、不改应用本体、不改 Python。
- **相关代码**：`windows/launcher.py`（数据目录、证书位置、`version.txt` 来源）、
  `ssl_bootstrap.py`（`server.crt`/`server.key`、云入口的 `fullchain.pem`/`<entry>.key`）、
  `server.py`（`_cloud_user_dir()/fleet`、`auth-tokens.json`）、
  `tools/windows/` 下既有的 Cleanup / Connect / Patch / CertDoctor 四件套（同一发布形态）
- **相关记录**：`docs/OPERATION_GUIDE.md` §0.9–§0.10；`docs/MACOS_INSTALLER_GUIDE.md:133`（FT-891 音频例外）；
  `ssl_bootstrap.sign_for()` 的幂等性注释（云入口钉证书）

---

## 1. 背景

用户换电脑时，MRRC Modern 的所有**用户侧状态**都不在安装目录里，而在用户数据目录：

```
%LOCALAPPDATA%\MRRC-Modern\
```

覆盖面安装（升级）不受影响，但**换机**就没有任何一条路径能把它带走。现场最贵的一步不是重装，
而是这三件事必须一模一样地搬过去：

1. `mrrc_modern.env`：电台机型、串口、波特率、声卡名、网页口令、云端接入参数；
2. `certs\`：HTTPS 证书与私钥 —— **云入口钉住的是这张证书**，换机重新签一张，那条入口就 502
   （`ssl_bootstrap.sign_for()` 的幂等性注释记的就是这个 2026-10-15 现场）；
3. `fleet\frpc-<label>.toml`：云入口隧道配置。

重新配一遍不是"重新填几个框"：声卡名在不同机器上的枚举名不同（现场见过
`麦克风 (USB Audio Device)` 与 `麦克风 (USB PnP Sound Device)` 两台机器各一个），串口要重探，
网页口令要重设，云入口要重新接入并重新钉证书。

## 2. 目标 / 非目标

**目标**

1. 双击一次就能把上表内容打成一个文件带走；在新机器上双击一次恢复，**不需要编辑任何文件**。
2. 恢复是**可退的**：覆盖之前先把目标机现有配置备份出来。
3. 不要求管理员权限（只读写当前用户的 `%LOCALAPPDATA%`）。
4. 密钥类内容的存在与数量**可见**（清单里逐项标出），值**不可见**（任何输出都不打印）。

**非目标**

- 不负责安装应用（新机器先装好 MRRC Modern，再跑恢复）。
- 不带 `logs\` 与 `recordings\`（体积与隐私都不是"配置"）。
- 不做增量、不做云同步、不做跨平台（macOS 侧将来另写一个读同一格式的脚本）。
- 不做"备份即登录"：`auth-tokens.json` 是服务端那一半，见 §7 第 7 步。

## 3. 备份集

固定六项（另加一个条件项）。**顺序与集合写死在脚本里，并由测试钉住**。

| # | 相对数据目录 | 内容 | 不带走的后果 | 敏感 |
| --- | --- | --- | --- | --- |
| 1 | `mrrc_modern.env` | 电台/串口/声卡/网页口令/云端参数 | 全部重配 | **是**（网页口令） |
| 2 | `certs\` | `server.crt`/`server.key`，云入口的 `fullchain.pem`/`<entry>.key`（整目录） | 云入口 502；本地证书换新 | **是**（私钥） |
| 3 | `mem_channels.json` | 记忆频道 | 归零 | 否 |
| 4 | `atr1000_tuner.json` | ATR-1000 天调学习值 | 归零，首次发射要重新学习 | 否 |
| 5 | `fleet\` | `frpc-<label>.toml`、`pending.toml` | 云入口隧道起不来 | **是**（隧道地址/凭据） |
| 6 | `auth-tokens.json` | 服务端 30 天会话库 | 见 §7 第 7 步（仅服务端一半） | **是**（会话凭证） |
| 7 | `external-certs\`（条件项） | `MRRC_SSL_CERT`/`MRRC_SSL_KEY` 指向数据目录之外时，那一对文件 | 新机器上证书路径不存在 → 回落 HTTP | **是**（私钥） |

显式排除并打印出来：`logs\`、`recordings\`、`patch-pending.txt`、以及安装目录下的一切。

**`auth-tokens.json` 的位置不是写死的数据目录**：`server.py:654` 把它放在
`MEM_FILE.parent`，而 `MEM_FILE` 可由 `MRRC_MEM_FILE` 重定向。所以导出时按
「旧 `MRRC_MEM_FILE` 所在目录」取，恢复时写到「改写后的 `MRRC_MEM_FILE` 所在目录」
（默认两者都是数据目录）。打包路径统一记成 `auth-tokens.json`（payload 根），与它在
源机器上的绝对位置无关。

## 4. 包格式

```
MRRC-Modern-backup-<hostname>-<yyyyMMdd-HHmmss>.zip
├─ manifest.json
├─ payload/
│    mrrc_modern.env
│    certs/…
│    mem_channels.json
│    atr1000_tuner.json
│    fleet/…
│    auth-tokens.json
│    external-certs/…           （只有条件项 7 出现时）
└─ RESTORE-README.txt
```

### 4.1 `manifest.json`

```json
{
  "schema": "mrrc-modern-backup/1",
  "createdAt": "2026-10-22T16:40:12+08:00",
  "hostname": "DESKTOP-XXXX",
  "userName": "Administrator",
  "appVersion": "1.28.2",
  "appDir": "C:\\Program Files\\MRRC Modern",
  "dataDir": "C:\\Users\\Administrator\\AppData\\Local\\MRRC-Modern",
  "containsSecrets": true,
  "items": [
    {"path": "mrrc_modern.env", "kind": "config", "size": 3120, "sha256": "…"},
    {"path": "certs/server.key", "kind": "key",    "size": 241,  "sha256": "…"}
  ],
  "excluded": ["logs", "recordings"],
  "notes": ["MRRC_SSL_CERT 指向数据目录之外：已收入 external-certs/"]
}
```

- `kind` ∈ `config | key | data | session`；`key`/`session` 在报告里标红。
- `dataDir` 是**恢复时改写路径的唯一依据**（旧前缀 → 新前缀）。
- `appVersion` 读安装目录的 `version.txt`（注册表 Uninstall 键的 `InstallLocation`，回落
  `C:\Program Files\MRRC Modern`）；读不到写 `"unknown"`，不阻断。

### 4.2 加密容器（`-Password` 时）

包裹整个 zip，产物 `<zip>.enc`，恢复时按键名/schema 识别：

| 偏移 | 长度 | 内容 |
| --- | --- | --- |
| 0 | 8 | ASCII `MRRCBAK1` |
| 8 | 2 | u16 LE，格式版本 = 1 |
| 10 | 4 | u32 LE，PBKDF2 迭代次数（200000） |
| 14 | 16 | salt |
| 30 | 16 | IV |
| 46 | 8 | u64 LE，明文（zip）长度 |
| 54 | 32 | HMAC-SHA256（覆盖 0..53 与全部密文），mac 密钥 |
| 86 | N | AES-256-CBC 密文 |

- 密钥派生：PBKDF2-SHA256(口令, salt, iter) → 64 字节 = 32 字节 enc + 32 字节 mac。
- **先验 HMAC，再解密**；任何一步失败都算"包坏了或口令不对"，退出 20 且不写任何文件。
- 纯 .NET（`System.Security.Cryptography`），不依赖 7-Zip / openssl。PowerShell 5.1 的
  `Rfc2898DeriveBytes(string, byte[], int, HashAlgorithmName)` 需要 .NET 4.7.2+；
  运行时探测失败就给一句明确的话（"这台机器缺 .NET 4.7.2+，改用不带口令的备份"），不静默降级。

## 5. 命令行契约

```
MRRC-Modern-Backup.cmd                          → 菜单：1 备份 / 2 恢复 / Q 退出
MRRC-Modern-Backup.ps1                          → 同上（双击 .cmd 时传进来）
MRRC-Modern-Backup.ps1 -Export [-Out <path>] [-Encrypt] [-Password <pwd>]
MRRC-Modern-Backup.ps1 -Import <file> [-Password <pwd>] [-Yes] [-StopApp]
MRRC-Modern-Backup.ps1 -Restore [-Password <pwd>] [-Yes] [-StopApp]
MRRC-Modern-Backup.ps1 -List <file> [-Password <pwd>]
MRRC-Modern-Backup.ps1 -SelfTest
```

| 参数 | 说明 |
| --- | --- |
| `-Export` | 输出默认 `桌面\MRRC-Modern-backup-<host>-<stamp>.zip` |
| `-Out` | 指定输出路径（目录则自动拼文件名） |
| `-Password` | 非交互入口（给支持人员用）：**会留在 PowerShell 历史与进程命令行里**；本地一次性迁移优先用 `-Encrypt` |
| `-Encrypt` | 导出后加密；口令用 `Read-Host -AsSecureString` 提示两遍，**不经过命令行** |
| `-Import` | 接受 `.zip` 与 `.enc`；`-Import` 是 `[string]`，必须带路径 |
| `-Restore` | 弹出文件选择框，选中后按 `-Import` 处理（双击菜单里的 `2` 走这条路） |
| `-Yes` | 不逐项询问（恢复确认、关应用确认都用"是"） |
| `-StopApp` | 允许脚本关闭 Launcher/Server/scope_pipe/frpc（默认只提示） |
| `-List` | 只打印清单（含每项大小与 `kind`），不写任何文件 |
| `-SelfTest` | 造两个假数据目录，导出→恢复→逐字节比对，见 §9 |
| `-DataDir <path>` | **仅测试用**，覆盖"用户数据目录"的解析结果。正式使用不接受它（防止在真机上写错目录）；`-SelfTest` 内部通过它指到临时目录 |

退出码：

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 10 | 无事可做（没装过 / 空的备份集 / 用户取消） |
| 20 | 校验失败或用法错误（摘要不符、口令错、条目越界、缺 .NET 能力） |
| 30 | 目标机在跑且用户拒绝关闭 |

## 6. 导出流程

1. 定位 `%LOCALAPPDATA%\MRRC-Modern`。不存在 → 打印「这台机器上还没有 MRRC Modern 的用户数据（应用可能没启动过）」，退出 10。
2. 读 `version.txt`（见 §4.1）。
3. 按 §3 收集到临时 staging 目录；`certs\`、`fleet\` 整目录复制（保留相对路径）。
   单个文件被占用时**重试 3 次**（间隔 200 ms）；仍失败则整体失败，退出 20 ——
   宁可不给包，也不给一个缺文件的包。
4. 从 `mrrc_modern.env` 解析 `MRRC_SSL_CERT`/`MRRC_SSL_KEY`（含 `FT710_*` 回退）。
   两者都存在且**不在数据目录内** → 复制进 `payload\external-certs\` 并在 `notes` 里记一条。
5. 逐文件 SHA-256 写 manifest；生成 `RESTORE-README.txt`。
6. 打包；`-Password` 时再加密、删明文。
7. 结束语：清单、体积、"这个包里有网页口令 / TLS 私钥 / 隧道凭据（kind=key/session 的项）"、
   以及一行可直接复制的恢复命令。

**导出不要求关闭应用**：写文件的路径都是原子写（`auth-tokens.json`、记忆频道），
读到的要么是旧版本要么是新版本；重试覆盖短暂占用。

## 7. 恢复流程

1. 识别格式：读前 8 字节，`MRRCBAK1` → 要口令并解容器；否则按 zip 处理。
2. 解到临时目录前**先校验条目名**：拒绝以 `/`、`\`、盘符开头，或任一段为 `..` 的条目（zip-slip）；
   再逐项对 `manifest.items` 核 size 与 SHA-256。任一不符 → 退出 20，**一个字节都不写**。
3. 目标机兜底备份：把将被覆盖的每一项（存在的那部分）复制到
   `桌面\MRRC-restore-pre-<yyyyMMdd-HHmmss>\`，并在报告里打印该路径。
4. 检测 `MRRC-Modern-Launcher` / `MRRC-Modern-Server` / `scope_pipe` / `frpc`。
   在跑 → 列出进程与 pid，问「要先关掉吗」；`-Yes` 视为同意，`-StopApp` 直接关；
   拒绝 → 退出 30。**绝不静默杀进程。**
5. 逐项替换（只覆盖，不删除清单外的东西；`logs\`、`recordings\` 不碰）。
   目标机多出来的文件（例如新机器首启生成的证书）留在原地 —— 覆盖同名项即可，
   备份目录里有它们。
6. 改写 `mrrc_modern.env` 里的路径：对 `MRRC_SSL_CERT`、`MRRC_SSL_KEY`、
   `MRRC_RECORDINGS_DIR`、`MRRC_LOG_DIR`、`MRRC_MEM_FILE`、`MRRC_ATR1000_STORE`、
   `MRRC_CQ_FILE`、`MRRC_CA_BUNDLE`（以及同名 `FT710_*` 别名）执行
   「旧 `dataDir` 前缀 → 新 `dataDir` 前缀」。其余绝对路径若在新机器上不存在，
   **逐条告警但不改写**（用户可能故意指向 D: 盘）。相对路径（如 `vendor\ftdi\…`）不动。
   `MRRC_CA_BUNDLE` 指向的自定义根包**不打包**（那是部署环境的东西，不是用户配置）：
   新机器上没有它时只告警，并说明留空即回落到随包的 `vendor/ca/cacert.pem`。
7. 说明 `auth-tokens.json` 的真实作用：它是**服务端**会话库，浏览器里的 cookie 不会跟着走；
   只有同一套浏览器配置目录才可能免登录。新电脑仍然要输一次口令（口令来自恢复的 env）。
8. 报告：恢复了哪些、目标机原配置备份在哪、下一步「双击桌面上的 MRRC Modern 启动」；
   若 `certs\` 来自另一台机器，加一句「浏览器会照常对自签名证书告警一次；云入口若已接入，
   仍然用这张证书，不要为了消警告重新签」。

## 8. 安全边界（写进脚本头注释，也写进测试）

1. **只写固定清单**里的路径；永不 `Remove-Item` 用户数据（只覆盖，且覆盖前已备份）。
2. 解包先校验后落盘；临时目录用完即删（`try/finally`）。
3. 口令 / 令牌 / 私钥的值**永不打印**：只打印存在性、字节数与 SHA-256 前 8 位。
4. 两次备份（导出包、`MRRC-restore-pre-*`）都不自动删除。
5. 明文 zip 删除是**普通删除**（SSD 上不保证不可恢复），脚本在 `-Password` 导出后如实说这一句。
6. 不读、不写注册表、不改环境变量、不装/卸任何东西（与 Cleanup 的破坏面完全不同）。

## 9. 测试与守卫

**静态守卫** —— 新增 `tests/test_windows_backup_script.py`，照 `tests/test_cert_doctor.py` 的路子：

1. `tools/windows/MRRC-Modern-Backup.ps1` 必须以 UTF-8 **BOM** 开头（PS 5.1 否则按 ANSI 解码，中文成乱码、字符串可能解析失败）。
2. `.cmd` 必须纯 ASCII，且块内 `echo`/`rem` 不得含括号（cmd 会在第一个 `)` 结束块 —— 2026-10-16 实测）。
3. `website/downloads/` 的两份副本必须与 `tools/windows/` **逐字节一致**。
4. 脚本里出现的固定清单（六个相对路径 + 条件项）与测试里的一份常量一致，
   防止将来加一项只改了一边。
5. `-Password` 分支必须同时出现 `Aes`、`Rfc2898DeriveBytes`、`HMACSHA256` 三个词，
   且 `-Export` 分支里加密成功后必须删明文（源码级断言，防"顺手注释掉"）。

**运行时自证** —— `-SelfTest`（在 Windows 上跑）：

```
造 dirA（放 6 项 + 一个假的 logs/recordings）
→ -Export -Out tmp\a.zip
→ 造 dirB（放一个异于 A 的 mrrc_modern.env 当"目标机现有配置"）
→ -Import tmp\a.zip -DataDir dirB -Yes
→ 逐文件比对 A/B；检查 pre-backup 目录存在且含 B 原来那份
→ -Password 往返一次；改一个字节后必须失败且 dirB 不变
输出 PASS/FAIL 摘要，退出码与正式路径一致
```

`-DataDir` 是**只给测试用**的隐藏参数（正式路径不接受它，避免真机上写错目录）。
macOS 上没有 PowerShell，静态守卫在本地跑；`-SelfTest` 进 Windows VM 的构建验收清单。

## 10. 发布面

| 产物 | 位置 |
| --- | --- |
| 源 | `tools/windows/MRRC-Modern-Backup.ps1`、`MRRC-Modern-Backup.cmd` |
| 下载 | `website/downloads/MRRC-Modern-Backup.ps1`、`MRRC-Modern-Backup.cmd`（逐字节副本） |
| 文档 | `website/guide.html` 与 `website/zh/guide.html` 新增 **§6.5**「换电脑：一键备份/恢复」（§6.3/§6.4 已被占用，改号会打断既有锚点）；两处首页页脚在"清理脚本"旁加一行链接 |
| 答复页 | 不需要新卡；`docs/OPERATION_GUIDE.md` 的升级一节补一句交叉引用 |

`.cmd` 行为：双击（无参数）→ 菜单；带参数 → 原样转给 `.ps1`；`.ps1` 不在同目录时按
CertDoctor 的做法自下载一次（URL 指向 `downloads/`），失败则指向整包下载页。

## 11. 验收标准

1. `-Export` 在一个假数据目录上产出的 zip：`manifest.items` 恰为 §3 的六项（条件项只在
   `MRRC_SSL_CERT/KEY` 指向目录外时出现），每项 `sha256` 与磁盘一致，`payload/` 内**没有**
   `logs/`、`recordings/`。
2. `-Import` 到另一个假数据目录后，六项逐字节一致；`logs/`、`recordings/` 未被创建或修改。
3. 目标机原有的 `mrrc_modern.env` 在覆盖前出现在 `桌面\MRRC-restore-pre-*`，报告里打印该路径。
4. 篡改 `payload` 里任意一个字节 → 退出 20，且目标目录**完全未被写入**（含 pre-backup 也不产生）。
5. `-Password` 往返成功；口令错、密文改一位 → 退出 20，目标目录未被写入。
6. 含 `../evil` 或 `C:\evil` 条目的恶意 zip → 退出 20，数据目录外无任何新文件。
7. 旧 `dataDir` 出现在 `MRRC_SSL_CERT` 等七个路径键里时被改写为新前缀；其余绝对路径不存在时
   只告警、不改写；相对路径不变。
8. 目标机有 `MRRC-Modern-Server` 在跑且用户不关 → 退出 30、不写文件；`-StopApp` → 正常恢复。
9. `-List` 不产生任何文件（对临时目录做快照比对）。
10. 静态守卫 5 条全绿；全套件绿；`dev_tools/site_audit.py` 无新增断链（中英两节成对）。
11. Windows VM：`-SelfTest` 输出全 PASS。

## 12. 风险与不做

- **误以为包里有录音**：结束语与 `RESTORE-README.txt` 两处都写"不含 logs/recordings"。
- **口令丢失**：`-Password` 的包无法恢复。脚本在导出结束时重复一遍口令不提现在任何文件里；
  建议与包分开存放（这句话写进 README，而不是只打印一次）。
- **两个"备份"混淆**：本工具是"搬家"，`MRRC-Modern-Cleanup.ps1` 是"清场前存档"。
  文档里各引各的，不合并。
- **不做**：增量备份、云端存储、自动定时备份、macOS/Linux 版本、恢复时自动启动应用
  （只提示，不代替用户点）。
