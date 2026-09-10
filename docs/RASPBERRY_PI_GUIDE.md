# 树莓派安装使用说明（MRRC Modern rpi64 镜像）

本页面面向第一次使用树莓派跑 MRRC Modern 的用户——不需要会 Linux 命令行，全程只要"烧卡、上电、开浏览器"。

## 一、你需要准备

- 树莓派 **3B+ / 4 / 5**（64 位系统；Zero 2 W 理论可用但内存偏小，未验证）
- ≥8GB 的 microSD 卡 + 读卡器
- 5V 电源（Pi 4 建议官方 USB-C）
- 网线（或烧卡时用 Imager 预配 Wi-Fi）
- USB 线（接电台）

## 二、烧卡（约 3 分钟）

1. 下载 `MRRC-Modern-v1.14.2-rpi64.img.xz`（本页下载卡片）。
2. 安装官方 **Raspberry Pi Imager** → `选择操作系统` → 拉到最底 **使用自定义镜像** → 选中下载的 img.xz。
3. `选择存储卡` → 选中 SD 卡 → **下一步**。
   - 想预配 Wi-Fi / 改主机名：点右下角 **编辑设置**（可选）。
4. 烧录完成弹出 SD 卡。

### （可选但推荐）预置网页密码

不预置也行——系统会自动生成密码（见第三节）。想自己定：

1. 烧完后重新插一次 SD 卡（会看到一个叫 `bootfs` 的分区）。
2. 在该分区根目录新建文本文件 `mrrc.env`，内容一行：

   ```ini
   MRRC_WEB_PASSWORD=你的密码
   ```

   （也可同时写 `MRRC_RADIO_MODEL=ft710`、`MRRC_SERIAL_PORT=/dev/ttyUSB0` 等，格式与桌面版配置相同。）
3. 安全弹出。

## 三、上电与首次启动（约 90 秒）

1. SD 卡插回树莓派，插网线/上电。
2. 首次启动会自动：生成/采纳配置 → 探测电台（现在插不插 USB 线都行，之后随时插）→ 生成 HTTPS 证书 → 启动服务。

### 网页密码怎么拿？

| 烧卡前预置了 `mrrc.env` | 不用拿——你写的就是密码 |
|---|---|
| **没预置** | 三处任选其一：<br>① 树莓派接显示器：HDMI 控制台启动日志末尾有大字横幅<br>② SSH：`ssh mrrc@raspberrypi.local`（密码 `mrrc`），登录提示即含查看命令<br>③ SSH 后执行 `sudo mrrc-show-password` |

## 四、登录

浏览器打开 **`https://raspberrypi.local:8888`**（与树莓派同一局域网；自签证书会警告一次，"高级 → 继续访问"）。

## 五、接电台

- **FT-710**：USB 线直插树莓派——CP210x 串口驱动内核自带，两个串口自动探测；真 FFT 频谱需要手动放 FTDI 库（见第七节），没放也能用 S 表频谱。电台菜单 `MOD SOURCE = USB` 仍需设置（与桌面版相同）。
- **IC-7300 / MK2**：USB 直插即可，CI-V 走同一条线，频谱开箱即用。
- **音频**：电台的 USB 声卡自动识别；若有多个声卡，在 `/opt/mrrc_modern/env/mrrc.env` 里写 `MRRC_AUDIO_RX_DEVICE=USB Audio` / `MRRC_AUDIO_TX_DEVICE=USB Audio` 锁定后 `sudo systemctl restart mrrc-modern`。

## 六、常用命令

```bash
sudo systemctl restart mrrc-modern     # 重启服务（改配置后）
sudo systemctl stop mrrc-modern        # 停（先松开 PTT！）
journalctl -u mrrc-modern -f           # 看实时日志
sudo mrrc-show-password                # 查看网页密码
sudo nano /opt/mrrc_modern/env/mrrc.env # 编辑配置
```

## 七、FT-710 真 FFT 频谱（可选进阶）

镜像不含 FTDI Linux 库（官方按架构单独发布）：

1. 到 FTDI 官网下载 **LibFT4222 Linux ARM64 (aarch64)** 包。
2. 解出 `libft4222.so` 与 `libftd2xx.so`，拷入树莓派：

   ```bash
   sudo cp libft4222.so libftd2xx.so /opt/mrrc_modern/vendor/ftdi/
   sudo systemctl restart mrrc-modern
   ```

3. 重启后瀑布图变为真 FFT；失败自动回落 S 表频谱，不影响其他功能。

## 八、更新

更新 = 烧录新版本镜像（配置和密码不会保留——如需保留请先备份 `/opt/mrrc_modern/env/mrrc.env` 的内容，烧完再预置回去）。

## 常见问题

| 问题 | 解决 |
| ------ | ------ |
| 浏览器打不开 raspberrypi.local | 确认与树莓派同局域网；试 `ping raspberrypi.local`；或登录路由器查 IP 后用 `https://<IP>:8888` |
| 提示证书不受信任 | 正常（自签名），高级 → 继续访问；HTTPS 是手机/平板能用麦克风的前提，不要关 |
| 手机上没有音频/麦克风 | 必须 HTTPS（本镜像默认已开）；确认地址栏是 https:// |
| 找不到电台 | `ls /dev/ttyUSB* /dev/ttyACM*` 看设备；`journalctl -u mrrc-firstboot` 看探测日志；FT-710 注意插的是上方 USB 口（Enhanced） |
| 服务没起来 | `sudo systemctl status mrrc-modern`；`journalctl -u mrrc-modern -n 50` |
| 忘记密码 | `ssh mrrc@raspberrypi.local` → `sudo mrrc-show-password` |
| Zero 2 W 上很卡 | 内存不足属预期；建议 Pi 4 2GB 起步 |
