# 打包与发布（mrrc_modern）

本仓与旧仓 `mrrc` 的规矩**相反**：这里 **CHANGELOG 顶条是版本权威**（`packaging/windows/MRRC-Modern.iss`
手抄），`tests/test_release_artifacts.py` 是检查器。旧仓那套 `dev_tools/release_windows.sh` 在**这里没有**。

## 两个平台的构建

```bash
# Windows（在 Win11 KVM VM 上，仓库 C:\mrrc_modern）
#   1) 先备构建输入（构建机上取，带 SHA-256 校验）：
#      mrrc_hub/deploy/fetch_installer_payload.sh --out packaging/payload --platforms windows-amd64
#      ⇒ packaging/payload/windows-amd64/ 里会有 frpc.exe + openssl.exe(+9 DLL) + 安装器脚本 + openssl.cnf
#   2) 构建（门禁：py_compile → 全量 unittest → 3×PyInstaller → iscc）：
powershell -NoProfile -ExecutionPolicy Bypass -File C:\mrrc_modern\packaging\windows\build.ps1

# macOS（Apple Silicon，本机）
PYTHON=.venv/bin/python bash packaging/macos/build.sh     # 细节见 .agents/skills/macos-installer
```

## 六个只在"装完真跑"才现形的坑

见 `.pi` 技能 `windows-installer` 的 modern 小节（逐条含症状/根因/修法）。此处只列要点：
随包带 `openssl.cnf` 并显式 `-config`（msys2 的 openssl 会按编译前缀找默认配置）；
shell 侧不要用 `-addext`（LibreSSL 没有）；PS 5.1 下原生程序的 stderr 会冒充致命错误；
计划任务主体用 `COMPUTERNAME\user`；注释别插进反引号续行；`instance-certs/` 属主要给服务账号。

## 构建输入与产物

- `packaging/payload/**` **不入库**（`.gitignore`）—— 它是按 `mrrc_hub/deploy/payload.lock`
  的哈希复现出来的构建输入。**入库的是那份锁**（哈希 + 来源说明）。
- **往 VM 传源码时 tar 必须排除 `venv`**：modern 仓的 venv 没有点，我按 `.venv` 排除过一次，
  把 Mac 的 venv 盖上去 ⇒ `python.exe` 指向 `/opt/local/...` ⇒ `py_compile` 退出码 103、构建秒死。
- 产物：`dist/windows/MRRC-Modern-Setup.exe`、`dist/macos/MRRC-Modern-v<版本>-arm64.dmg`。

## 站点发布（四处事实 + 清单）

```
站点卡片里 Windows/macOS 的 size+SHA 各出现在**四处**（卡片 meta、完整哈希行、步骤说明、指南），改完必须一致
python3 tests/test_release_artifacts.py                     # 必须绿；它禁止卡片出现旧版本号（<em> 注记里允许）
python3 dev_tools/make_latest_json.py --installer <ver> <url> <带版本名的文件> --previous <上一版> …
部署：页面 rsync --delete --exclude downloads（保住服务器上独有的历史 DMG/exe）
      + 安装包与 latest.json 单独上传（downloads/ 是扁平一层，清单也在里面，别被 --exclude 漏掉）
      + 线上复核：下载回来比对 SHA-256（必须逐字节一致）
```

## 发版验收（含今晚血泪的一条）

1. `tests/test_release_artifacts.py` 绿 2. 全量测试绿 3. 产物是新的（**mtime 带年份** + `version.txt` + SHA）
4. 服务器侧哈希一致 5. 线上=本地 6. `latest.json` 指向带版本名的文件 7. 站点显示新版本
8. **在干净机器上装线上包，按租户的方式跑一遍接入** —— 前七项全绿也**不代表**包能用（实测）。
