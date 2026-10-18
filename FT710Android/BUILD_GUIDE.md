# FT710Android 构建指南

> 已验证：macOS (arm64) · JDK 17 · Gradle 8.9 (wrapper) · AGP 8.7.3 · Kotlin 2.0.21 · Compose BOM 2024.12.01 · minSdk 26。

## 1. 工具链（一次性）

```bash
# JDK 17（AGP 8.x 需要；本机默认 JDK 11 不够）
brew install --cask corretto@17
export JAVA_HOME=$(/usr/libexec/java_home -v 17)   # 每次构建前设这个

# Android cmdline-tools
mkdir -p "$HOME/Library/Android/sdk/cmdline-tools"
cd "$HOME/Library/Android/sdk/cmdline-tools"
curl -o tools.zip https://dl.google.com/android/repository/commandlinetools-mac-11076708_latest.zip
unzip -q -o tools.zip && rm -f tools.zip && mv cmdline-tools latest
export ANDROID_HOME="$HOME/Library/Android/sdk"

# SDK 平台 + 构建工具 + 许可
yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --licenses
"$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" \
  "platforms;android-35" "build-tools;35.0.0" "platform-tools" "ndk;27.2.12479018"

# Gradle（仅用于首次生成 wrapper；之后用 ./gradlew）
brew install gradle
cd FT710Android && gradle wrapper --gradle-version 8.9
```

每次 shell 会话构建前：
```bash
export JAVA_HOME=$(/usr/libexec/java_home -v 17)
export ANDROID_HOME="$HOME/Library/Android/sdk"
```

## 2. 构建

```bash
cd FT710Android
./gradlew assembleDebug                 # 出 app/build/outputs/apk/debug/app-debug.apk
./gradlew test assembleDebug lintDebug  # CI 门槛：JVM 单测 + 编译 + lint
./gradlew installDebug                  # 真机安装（需 USB 调试）
```

## 3. 真机连接

- 手机开 USB 调试 → `adb devices` 应列出设备。
- App 登录页填服务端主机（默认 `radio.vlsc.net:8888`）+ 密码。
- 服务端默认自签 TLS：App 已接受自签证书（`AuthApi.selfSignedOkHttpClient`）。`--no-ssl` 明文仅调试（见 `res/xml/network_security_config.xml`）。
- 麦克风权限：首次进 App 需授权（PTT 发射用）。

## 4. 常见问题

| 问题 | 解决 |
|---|---|
| `Unable to strip the following libraries` | 无害警告，忽略 |
| SDK 找不到 | `ANDROID_HOME` 未设，见上 |
| `jvmTarget 17` 报错 | `JAVA_HOME` 指向 JDK 11，改 JDK 17 |
| NDK/CMake 报错 | `ndk;27.2.12479018` 未装，用 sdkmanager 装 |

## 5. 签名与发布（v1.0.0 起）

- keystore：`~/.android-keystores/mrrc-modern-release.jks`（别名 `mrrc-modern`，RSA 4096，**仓库外**）
- 证书指纹（SHA-256）：`78:35:3A:40:EC:C1:56:55:D0:04:0F:F1:75:6F:1C:13:33:90:12:30:0C:34:E6:51:BC:5F:D8:04:9D:8A:54:99`（2026-10-18 起；旧密钥 `mrrc-modern-release.legacy-20261003.jks` 指纹 `A0:9E:13:64:…:65:0B`，口令已丢失，仅作留档）
- 口令：**三份**——`FT710Android/keystore.properties`（gitignore，0600，构建读的那份）、`~/.android-keystores/mrrc-modern-release.properties`（0600，仓库外备份）、macOS 钥匙串 `security find-generic-password -s mrrc-modern-android-keystore -w`。

  > ⚠ 历史：v1.0.0~v1.1.31 用的旧 keystore 口令是生成时随机打的，只打印过一次，后来**真丢了**（2026-10-18：`keystore.properties` 被换 checkout 清掉，全盘 find / shell 历史 / 钥匙串 / `~/.gradle` 都没有，`app/build.gradle.kts` 也没有 env 回退）。所以现在口令存三份，且 `keystore.properties` 丢了照下面重建。

  > `app/build.gradle.kts` **刻意**没有环境变量回退：文件缺失就让 `assembleRelease` 失败，绝不静默产出 debug 签名的“正式版”。

  换 checkout（如 worktree 合并后）时从备份重建该文件：

```bash
umask 077
cp ~/.android-keystores/mrrc-modern-release.properties FT710Android/keystore.properties
# 或手工：printf 'storeFile=%s\nstorePassword=<口令>\nkeyAlias=mrrc-modern\nkeyPassword=<口令>\n' \
#   "$HOME/.android-keystores/mrrc-modern-release.jks" > FT710Android/keystore.properties
```

- **2026-10-18 换过签名密钥（v1.1.32 起）**：签名变了 ⇒ 已装旧版的手机**不能覆盖升级**（Android 报“应用未安装”），必须先卸载 App 再装新包；卸载会清掉 `SettingsStore` 里的服务器地址/口令/云端凭据（需要重新登录、重新接入云端）。官网 Android 卡片同期打了一行提醒（`CARD_NOTE_ZH`/`CARD_NOTE_EN`，见下），下一版不带该变量即自动消失。
- 今后换密钥、改 `versionCode` 方向**只能往上**；真要换密钥请先按上面三点把旧口令确认在手，否则就是这一次的下场。

- 发布是**两条命令**，边界明确（详见 `CLAUDE.md` 的"发布纪律"）：

```bash
cd FT710Android
./release.sh --apk-only        # 构建 → 签名核对 → 只上传 APK → 线上 SHA-256 复核
./publish-card.sh              # 只改线上页面的 Android 标记块与 hero 安卓按钮，然后 reload nginx
```

- **不要**用不带 `--apk-only` 的 `release.sh`，也不要跑站点全站 `deploy.sh`：站点树与 Windows/macOS 发布波共用，全站部署会把对方的下载卡片回退（2026-10-05 实测）。
- `--dry-run`：只本地构建+改页面（不传服务器）；`--skip-tests` 跳过单测（门槛已单独跑过时用）。
