# macOS 安装包打包流程（本机 Mac 直接构建）

> 用途：在开发者本机 Mac 上构建 `MRRC-Modern-<ver>-arm64.dmg`（最新 v1.14.0）。
> 本文按 v1.7.0 首次打包的实际操作整理，照做即可复现。
> 用户向的安装/使用说明见 [docs/MACOS_INSTALLER_GUIDE.md](docs/MACOS_INSTALLER_GUIDE.md)，本文是**打包方**的操作手册。
> 与 Windows 不同，macOS 不需要 KVM 虚拟机——直接在本机用 `.venv` 打包。

## 1. 环境拓扑

```
本机 Mac (Apple Silicon, macOS 11+)
   .venv/                  Python 3.13 + 项目依赖 + PyInstaller + rumps
   dist/macos/             构建产物
   vendor/ftdi/macos/       FT4222 真谱 dylib（universal arm64，随包分发）
```

系统要求：

- Apple Silicon（arm64）。Intel Mac 不在此 build 支持范围内。
- Xcode Command Line Tools：`codesign`、`hdiutil`（`xcode-select --install`）。
- Homebrew：`brew install portaudio`（pyaudio wheel 需要的 PortAudio C 库）。
- Python 3.13（系统自带或 pyenv 均可，本机 `.venv` 即 3.13）。

## 2. 一次性准备

### 2.1 venv 与依赖

```bash
cd ~/HAM/mrrc_modern
python3.13 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r packaging/macos/requirements-build.txt   # 已锁 pyinstaller==6.21.0 + rumps
```

> ⚠️ **不要 `source .venv/bin/activate`**：本仓库的 `.venv` 当初是从隔壁 `mrrc_ft710` 项目复制来的，
> activate 脚本里硬编码了 `VIRTUAL_ENV=/Users/cheenle/HAM/mrrc_ft710/.venv`——激活后 PATH 指向
> **没有 PyInstaller 的错误项目 venv**，`build.sh` 会在 PyInstaller 步骤报 `No module named PyInstaller`
> （2026-09-09 v1.14.0 发布时实测踩坑）。另外裸 `python3`（Homebrew 3.14）没装项目依赖，
> 跑测试会得到 50 个 `No module named 'serial'` import error。
> **一律用显式路径**：`.venv/bin/python` 或 `PYTHON=$(pwd)/.venv/bin/python bash packaging/macos/build.sh`。

`requirements.txt` 里的 pyaudio 依赖 PortAudio，必须先 `brew install portaudio`，否则 pip 装不上。

### 2.2 FT4222 真谱 dylib

`vendor/ftdi/macos/libft4222.dylib` 和 `libftd2xx.dylib` 已就位（自 `lib/` 拷贝，universal arm64），**随包分发**，所以 FT-710 开箱即得真 FFT 频谱。

来源是 FTDI 官网的 LibFT4222 macOS 构建和 D2XX 驱动包。FTDI 官网对脚本下载可能返回 403，需在浏览器手动下载后解压、改名为上面的文件名，放到 `lib/`，再拷贝到 `vendor/ftdi/macos/`。

## 3. 每次打包流程

### Step 0 — 本地检查

```bash
venv/bin/python -m unittest discover -s tests        # 必须全绿
```

确认版本号一致：版本来源是 `CHANGELOG.md` 顶部 `## [vX.Y.Z]`（顶部允许存在 `[Unreleased]`，`grep -m1` 会取到下一个已命名版本）——build.sh 会自动从这里读取版本注入 `Info.plist`，无需手动改 spec。

### Step 1 — 构建（在本机仓库根目录）

```bash
PYTHON=$(pwd)/.venv/bin/python packaging/macos/build.sh   # 不要 source .venv/bin/activate（见 §2.1 警告）
```

`build.sh` 依次：语法检查 → 测试 → 3 个 PyInstaller spec → 组装 `.app` → **`Contents/Frameworks` symlink（关键，见 §5）** → ad-hoc 签名 → `hdiutil` 打 dmg → 打印 MD5/SHA-256。约 3–5 分钟。

任何一步非零退出都会因 `set -euo pipefail` 中止，不会带病出包。

### Step 2 — 验证产物

```bash
ls -lh dist/macos/MRRC-Modern-*.dmg
codesign -dv dist/macos/MRRC-Modern.app                            # 显示 ad-hoc 签名
ls dist/macos/MRRC-Modern.app/Contents/MacOS/MRRC-Modern-Server          # server 在位
ls dist/macos/MRRC-Modern.app/Contents/MacOS/_internal/static/index.html   # static 在 _internal
ls dist/macos/MRRC-Modern.app/Contents/MacOS/macos/default.env     # 配置模板
ls dist/macos/MRRC-Modern.app/Contents/MacOS/mem_channels.json     # 初始频道种子
```

冒烟测试：

```bash
hdiutil attach dist/macos/MRRC-Modern-*.dmg
cp -R "/Volumes/MRRC Modern/MRRC-Modern.app" /Applications/
xattr -dr com.apple.quarantine /Applications/MRRC-Modern.app      # 跳过 Gatekeeper 首次拦截
open /Applications/MRRC-Modern.app                                 # 应在菜单栏出现图标并开浏览器
hdiutil detach "/Volumes/MRRC Modern"
```

### Step 3 — 记录校验和

`build.sh` 末尾会打印 DMG 的 size / MD5 / SHA-256，发布网页要用。也可手算：

```bash
du -h dist/macos/MRRC-Modern-*-arm64.dmg
md5 -q dist/macos/MRRC-Modern-*-arm64.dmg
shasum -a 256 dist/macos/MRRC-Modern-*-arm64.dmg
```

### 3.5 首启自动配置

`macos/first_run.py` 在菜单栏 launcher 里、server 启动**之前**运行，一次性完成零配置：

- 生成随机 web 密码（`secrets.token_urlsafe(16)`），写回 env 的 `MRRC_WEB_PASSWORD` 并置 `MRRC_AUTO_PASSWORD=1`。
- 扫描 `/dev/cu.*` 串口（CP210x/SLAB/usbserial 优先），逐个探测：FT-710 ASCII `ID;` @ 38400、Icom CI-V 0x19 @ 115200，命中即定型号并置 `MRRC_PORT_CONFIRMED=1`。
- 全部探测失败时安全回落：`MRRC_RADIO_MODEL` 取默认 `ft710`，串口取首个候选。
- 结果写回用户 env 文件；`needs_first_run()` 在配置已落定（非默认密码 + 合法型号 + 端口已确认）时跳过重探测。

冒烟验证：首次运行弹密码通知 → 登录页横幅显示「首次运行已自动生成密码」→ 菜单栏 **Show Password…** 可随时查看。

## 4. 发布到网站（可选）

下载镜像在 **<www.vlsc.net**（webroot> `/var/www/vlsc.net/mrrc_modern/`，属 `www-data:www-data`，cheenle 有 sudo 免密）：

```bash
scp dist/macos/MRRC-Modern-<ver>-arm64.dmg www.vlsc.net:/tmp/MRRC-Modern-<ver>-arm64.dmg.new
ssh www.vlsc.net "sudo -n cp /var/www/vlsc.net/mrrc_modern/downloads/MRRC-Modern-<ver>-arm64.dmg /tmp/old.bak; \
  sudo -n mv /tmp/MRRC-Modern-<ver>-arm64.dmg.new /var/www/vlsc.net/mrrc_modern/downloads/MRRC-Modern-<ver>-arm64.dmg; \
  sudo -n chown www-data:www-data /var/www/vlsc.net/mrrc_modern/downloads/MRRC-Modern-<ver>-arm64.dmg; \
  sudo -n chmod 644 /var/www/vlsc.net/mrrc_modern/downloads/MRRC-Modern-<ver>-arm64.dmg; \
  md5sum /var/www/vlsc.net/mrrc_modern/downloads/MRRC-Modern-<ver>-arm64.dmg"
```

### 版本号/大小/校验和同步清单（共 5 处，新版发布都要改）

- `CHANGELOG.md` 最新条目（版本源头）
- `packaging/windows/MRRC-Modern.iss` 的 `MyAppVersion`（Windows）
- `packaging/macos/mrrc_modern_launcher.spec` 的 Info.plist（macOS，build.sh 自动从 CHANGELOG 注入，无需手改）
- `website/index.html`（Windows + macOS 两处 MD5/SHA-256 + 版本/大小文字）
- `website/zh/index.html`（同上）
- `docs/WINDOWS_INSTALLER_GUIDE.md` + `docs/MACOS_INSTALLER_GUIDE.md`（Download 表格 + 构建说明段）

改完上传 `website/index.html` → webroot 根、`website/zh/index.html` → webroot `zh/`（同样 sudo + chown www-data）。

验证：`curl -sI https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-<ver>-arm64.dmg`（200 + content-length 正确）。

## 5. 故障排查

| 现象 | 原因 | 处理 |
| ------ | ------ | ------ |
| `pip install pyaudio` 失败 | 缺 PortAudio | `brew install portaudio` 后重装 |
| rumps/PyObjC 装不上 | 用了非 arm64 或过旧的 Python | 用 Python 3.13（本机 `.venv`），确保 `packaging/macos/requirements-build.txt` 已装 |
| `codesign` 报 nested 内容未签名 | 用了 `--deep`（会误签 `_internal/*.dist-info`） | build.sh 已改用“先签 dylib/.so + 各主 exe + 根 bundle，不用 `--deep`”；若手动签也照此 |
| 打出的 `.app` 首次打开“已损坏” | Gatekeeper 拦截 ad-hoc 签名 | 右键→打开，或 `xattr -dr com.apple.quarantine /Applications/MRRC-Modern.app` |
| 退出菜单栏 app 后 server 仍在跑 | 用了 Force Quit（SIGKILL 不可捕获） | `pkill -f MRRC-Modern-Server`；正常退出请用菜单栏的 Quit 项 |
| `MRRC-Modern.app` 体积异常大 | rumps 拉入了整个 PyObjC | 正常，PyObjC 约 40–50 MB；如需缩小可后续裁剪 frameworks |
| `MRRC_RADIO_MODEL` 为空/非法 | env 里没写型号 | config.py 空值安全回落 `ft710`（`os.environ.get(...) or "ft710"`），服务正常启动，无需手动改 |
| 首启自动探测没识别出电台 | 串口探测超时 / 电台未开 / 多设备 | 探测不打断服务：失败即安全回落（型号默认 ft710），server 照常启动；稍后可菜单栏 Edit Configuration… 手工修正 |
| **server 起不来：`[PYI-XXXX:ERROR] Failed to load Python shared library '.../Contents/Frameworks/Python'`** | PyInstaller 的 onedir exe 放进 `.app` 的 `Contents/MacOS` 后，bootloader 切到 **bundle 模式**：Python 框架和整个 onedir 数据（`sys._MEIPASS`：base_library.zip/numpy/static…）都按 `Contents/Frameworks` 解析，而不是非 bundle 的 `_internal` 同级目录。v1.7.0 及之前的 mac 包都有此缺陷（server 一启动即崩，菜单栏/浏览器是死的） | **v1.13.0 已修**：build.sh 组装时 `ln -sfn MacOS/_internal "$APP_BUNDLE/Contents/Frameworks"`。若手动搭 app 务必照做；冒烟验证：`Contents/MacOS/MRRC-Modern-Server --no-ssl --port 8899 --serial-port ""` 起来后 `curl -s -o /dev/null -w '%{http_code}' 127.0.0.1:8899/api/health` 应返回 401 |

## 6. 常用命令速查

```bash
# 构建
PYTHON=$(pwd)/.venv/bin/python packaging/macos/build.sh    # 勿用 activate（§2.1 警告：指向 mrrc_ft710）

# 只跑测试
.venv/bin/python -m unittest discover -s tests

# 验证签名
codesign -dv dist/macos/MRRC-Modern.app
spctl -a -vv dist/macos/MRRC-Modern.app      # 预期 “not a source of notarization”——ad-hoc 正常

# 挂载查看
hdiutil attach dist/macos/MRRC-Modern-*.dmg
```
