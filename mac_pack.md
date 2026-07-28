# macOS 安装包打包流程（本机 Mac 直接构建）

> 用途：在开发者本机 Mac 上构建 `MRRC-FT710-<ver>-arm64.dmg`。
> 本文按 v1.7.0 首次打包的实际操作整理，照做即可复现。
> 用户向的安装/使用说明见 [docs/MACOS_INSTALLER_GUIDE.md](docs/MACOS_INSTALLER_GUIDE.md)，本文是**打包方**的操作手册。
> 与 Windows 不同，macOS 不需要 KVM 虚拟机——直接在本机用 `.venv` 打包。

## 1. 环境拓扑

```
本机 Mac (Apple Silicon, macOS 11+)
   .venv/                  Python 3.12 + 项目依赖 + PyInstaller + rumps
   dist/macos/             构建产物
   vendor/ftdi/macos/       可选：FT4222 真谱 dylib（缺则回退 S 表）
```

系统要求：

- Apple Silicon（arm64）。Intel Mac 不在此 build 支持范围内。
- Xcode Command Line Tools：`codesign`、`hdiutil`（`xcode-select --install`）。
- Homebrew：`brew install portaudio`（pyaudio wheel 需要的 PortAudio C 库）。
- Python 3.12（系统自带或 pyenv 均可）。

## 2. 一次性准备

### 2.1 venv 与依赖

```bash
cd ~/HAM/mrrc_ft710
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r packaging/macos/requirements-build.txt   # pyinstaller==6.21.0 锁版 + rumps
```

`requirements.txt` 里的 pyaudio 依赖 PortAudio，必须先 `brew install portaudio`，否则 pip 装不上。

### 2.2 可选：FT4222 真谱 dylib

需要 `vendor/ftdi/macos/libft4222.dylib` 和 `libftd2xx.dylib`，缺了也能打包（S 表回退频谱），build.sh 只警告。

来源是 FTDI 官网的 LibFT4222 macOS 构建和 D2XX 驱动包。FTDI 官网对脚本下载可能返回 403，需在浏览器手动下载后解压、改名为上面的文件名，放到 `vendor/ftdi/macos/`。

> v1.7.0 默认**不**随包附带这两个 dylib——发布 S 表回退版本，用户可自行补齐（见用户指南的 FT4222 一节）。

## 3. 每次打包流程

### Step 0 — 本地检查

```bash
venv/bin/python -m unittest discover -s tests        # 必须全绿
```

确认版本号一致：`CHANGELOG.md` 最新条目（`## [vX.Y.Z]`）——build.sh 会自动从这里读取版本注入 `Info.plist`，无需手动改 spec。

### Step 1 — 构建（在本机仓库根目录）

```bash
source .venv/bin/activate
packaging/macos/build.sh
```

`build.sh` 依次：语法检查 → 测试 → 3 个 PyInstaller spec → 组装 `.app` → ad-hoc 签名 → `hdiutil` 打 dmg → 打印 MD5/SHA-256。约 3–5 分钟。

任何一步非零退出都会因 `set -euo pipefail` 中止，不会带病出包。

### Step 2 — 验证产物

```bash
ls -lh dist/macos/MRRC-FT710-*.dmg
codesign -dv dist/macos/MRRC-FT710.app                            # 显示 ad-hoc 签名
ls dist/macos/MRRC-FT710.app/Contents/MacOS/ft710-server          # server 在位
ls dist/macos/MRRC-FT710.app/Contents/MacOS/_internal/static/index.html   # static 在 _internal
ls dist/macos/MRRC-FT710.app/Contents/MacOS/macos/default.env     # 配置模板
ls dist/macos/MRRC-FT710.app/Contents/MacOS/mem_channels.json     # 初始频道种子
```

冒烟测试：

```bash
hdiutil attach dist/macos/MRRC-FT710-*.dmg
cp -R "/Volumes/MRRC FT-710/MRRC-FT710.app" /Applications/
xattr -dr com.apple.quarantine /Applications/MRRC-FT710.app      # 跳过 Gatekeeper 首次拦截
open /Applications/MRRC-FT710.app                                 # 应在菜单栏出现图标并开浏览器
hdiutil detach "/Volumes/MRRC FT-710"
```

### Step 3 — 记录校验和

`build.sh` 末尾会打印 DMG 的 size / MD5 / SHA-256，发布网页要用。也可手算：

```bash
du -h dist/macos/MRRC-FT710-*-arm64.dmg
md5 -q dist/macos/MRRC-FT710-*-arm64.dmg
shasum -a 256 dist/macos/MRRC-FT710-*-arm64.dmg
```

## 4. 发布到网站（可选）

下载镜像在 **www.vlsc.net**（webroot `/var/www/vlsc.net/mrrc_ft710/`，属 `www-data:www-data`，cheenle 有 sudo 免密）：

```bash
scp dist/macos/MRRC-FT710-<ver>-arm64.dmg www.vlsc.net:/tmp/MRRC-FT710-<ver>-arm64.dmg.new
ssh www.vlsc.net "sudo -n cp /var/www/vlsc.net/mrrc_ft710/downloads/MRRC-FT710-<ver>-arm64.dmg /tmp/old.bak; \
  sudo -n mv /tmp/MRRC-FT710-<ver>-arm64.dmg.new /var/www/vlsc.net/mrrc_ft710/downloads/MRRC-FT710-<ver>-arm64.dmg; \
  sudo -n chown www-data:www-data /var/www/vlsc.net/mrrc_ft710/downloads/MRRC-FT710-<ver>-arm64.dmg; \
  sudo -n chmod 644 /var/www/vlsc.net/mrrc_ft710/downloads/MRRC-FT710-<ver>-arm64.dmg; \
  md5sum /var/www/vlsc.net/mrrc_ft710/downloads/MRRC-FT710-<ver>-arm64.dmg"
```

### 版本号/大小/校验和同步清单（共 5 处，新版发布都要改）

- `CHANGELOG.md` 最新条目（版本源头）
- `packaging/windows/MRRC-FT710.iss` 的 `MyAppVersion`（Windows）
- `packaging/macos/ft710_launcher.spec` 的 Info.plist（macOS，build.sh 自动从 CHANGELOG 注入，无需手改）
- `website/index.html`（Windows + macOS 两处 MD5/SHA-256 + 版本/大小文字）
- `website/zh/index.html`（同上）
- `docs/WINDOWS_INSTALLER_GUIDE.md` + `docs/MACOS_INSTALLER_GUIDE.md`（Download 表格 + 构建说明段）

改完上传 `website/index.html` → webroot 根、`website/zh/index.html` → webroot `zh/`（同样 sudo + chown www-data）。

验证：`curl -sI https://www.vlsc.net/mrrc_ft710/downloads/MRRC-FT710-<ver>-arm64.dmg`（200 + content-length 正确）。

## 5. 故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| `pip install pyaudio` 失败 | 缺 PortAudio | `brew install portaudio` 后重装 |
| rumps/PyObjC 装不上 | 用了非 arm64 或过旧的 Python | 用 Python 3.12，确保 `packaging/macos/requirements-build.txt` 已装 |
| `codesign` 报 nested 内容未签名 | 用了 `--deep`（会误签 `_internal/*.dist-info`） | build.sh 已改用“先签 dylib/.so + 各主 exe + 根 bundle，不用 `--deep`”；若手动签也照此 |
| 打出的 `.app` 首次打开“已损坏” | Gatekeeper 拦截 ad-hoc 签名 | 右键→打开，或 `xattr -dr com.apple.quarantine /Applications/MRRC-FT710.app` |
| 退出菜单栏 app 后 server 仍在跑 | 用了 Force Quit（SIGKILL 不可捕获） | `pkill -f ft710-server`；正常退出请用菜单栏的 Quit 项 |
| `MRRC-FT710.app` 体积异常大 | rumps 拉入了整个 PyObjC | 正常，PyObjC 约 40–50 MB；如需缩小可后续裁剪 frameworks |

## 6. 常用命令速查

```bash
# 构建
source .venv/bin/activate && packaging/macos/build.sh

# 只跑测试
.venv/bin/python -m unittest discover -s tests

# 验证签名
codesign -dv dist/macos/MRRC-FT710.app
spctl -a -vv dist/macos/MRRC-FT710.app      # 预期 “not a source of notarization”——ad-hoc 正常

# 挂载查看
hdiutil attach dist/macos/MRRC-FT710-*.dmg
```
