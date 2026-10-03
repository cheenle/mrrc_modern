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
- 证书指纹（SHA-256）：`A0:9E:13:64:A7:DD:C0:08:EC:EE:5D:0C:5A:98:A6:09:19:73:94:25:B8:D3:F0:78:67:68:14:C4:31:44:65:0B`
- 口令与路径：`FT710Android/keystore.properties`（gitignore，仅本机）。**丢失 keystore 将无法覆盖升级**，请单独备份文件与口令。
  换 checkout（如 worktree 合并后）时重建该文件：

```bash
umask 077
printf 'storeFile=%s\nstorePassword=<口令>\nkeyAlias=mrrc-modern\nkeyPassword=<口令>\n' \
  "$HOME/.android-keystores/mrrc-modern-release.jks" > FT710Android/keystore.properties
```

- 一条命令发布（构建 → 签名核对 → 站点上传 → 线上 SHA-256 复核）：

```bash
cd FT710Android && ./release.sh            # --dry-run 只本地构建+改页面；--skip-tests 跳过单测
```
