# macOS 安装使用说明（MRRC Modern v1.16.0）

本页面面向第一次使用 MRRC Modern 的用户 —— 不需要会命令行，不需要改文件。

## 一、安装（约 1 分钟）

1. 打开下载的 `MRRC-Modern-v1.16.0-arm64.dmg`。
2. 把 **MRRC Modern** 图标拖进 **应用程序** 文件夹。
3. 弹出磁盘后，到「应用程序」里找到 **MRRC Modern**。

## 二、首次打开

1. 首次打开：在「应用程序」里 **右键点击** MRRC Modern → **打开** → 再点 **打开**。
   （只有第一次需要这样，之后双击即可。这是 macOS 对未认证 App 的正常保护。）
2. 菜单栏（屏幕右上角）出现 **MRRC Modern :8888** 图标，浏览器自动打开登录页。

## 三、登录（零配置）

1. 登录页会显示一行橙色提示：**「首次运行已自动生成密码：XXXX」**。
2. 输入这个密码，点 **登录**，就能用了。
3. 忘了密码？点菜单栏 **MRRC Modern** 图标 → **Show Password…** 即可查看。

## 四、连电台（即插即用）

- **FT-710**：USB 线插上 Mac 即自动识别，真 FFT 频谱开箱即用。
- **IC-7300 / IC-7300MK2**：USB 线插上自动识别；频谱走 CI-V。
- **IC-705 / IC-7610 / IC-7760（预览）**：同样即插即用，但配置未做真机验证 —— 接收可用，**发射默认被拒绝**（核对电台后设 `MRRC_ALLOW_UNVERIFIED_TX=1`）。
- **Yaesu FTDX10 / FTDX101D / FTDX101MP / FTX-1F（实验性）**：串口 ASCII-CAT，驱动与 FT-710 相同；同样默认只收不发，且没有真机频谱源（界面显示 S 表合成频谱）。
- 若同时插了多个串口设备，程序会优先选 CP210x/USB 串口；可到菜单栏
  **Edit Configuration…** 里确认 `MRRC_SERIAL_PORT`。

## 五、常用操作

- 浏览器再次打开控制页：菜单栏图标 → **Open Web UI**。
- 退出：菜单栏图标 → **Quit MRRC Modern**（先松开 PTT）。
- 改配置/密码：菜单栏图标 → **Edit Configuration…**（改后点 **Restart Server**）。

## 常见问题

| 问题 | 解决 |
| ------ | ------ |
| 提示"无法打开，因为无法验证开发者" | 右键 → 打开 → 再点打开（一次性） |
| 登录页没显示密码 | 点菜单栏图标 → Show Password… |
| 电台没反应 | 确认 USB 已插；菜单栏 → Edit Configuration… 看 `MRRC_SERIAL_PORT` |
| 频谱是假的 | 确认 `MRRC_FTDI_LIB_DIR=vendor/ftdi/macos` 且安装的是 v1.16.0 |
