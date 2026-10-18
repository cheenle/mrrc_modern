# SDR 接收机（`/sdr`）操作手册

> 适用版本：V3.00 起（AD-029）。平台：**盒子（W103D / Linux aarch64）**；
> macOS/Windows 桌面包里这个功能整体关闭（`MRRC_SDR=off`），页面显示"仅盒子版支持"。

---

## 1. 它是什么

插在盒子上的 **SDRplay RSP1** 被做成一台**对外发布的接收机**：独立的页面、独立的口令，
与电台控制面完全隔离 —— 页面里没有 PTT、没有 CAT，也看不到 FT-710 的频率与模式。

一个接收机能覆盖 **10 kHz – 2 GHz**，但同一时刻只能落在**一个** 2.4 MHz（或 4.8 MHz）窗口里。
换窗口就是"调谐"：任何人可以改中心频率，但**有 30 秒持有锁**，改完锁 30 秒，
这期间别人只能在自己的窗口里选解调点。

---

## 2. 打开与登录

| 入口 | 说明 |
|---|---|
| `https://<盒子地址>:8888/sdr` | 内网直连 |
| `https://<呼号>.mrrc.vlsc.net/sdr` | 云入口（如果开了隧道） |

第一次打开会看到**它自己的**登录页（不是主站那个）—— 口令是 `MRRC_SDR_PASSWORD`，
和网页控制口令、listen 口令**互不相同**。登录后 cookie `mrrc_sdr` 有效 30 天。

口令为空 ⇒ 功能整体关闭（`/sdr` 返回 404）。改口令：编辑 `mrrc_modern.env` 里的
`MRRC_SDR_PASSWORD` 后 `sudo systemctl restart mrrc-modern`。

---

## 3. 页面怎么用

- **瀑布**：横向是频率（刻度自动选 1/2/5 步进），纵向是时间（越上越新）。
  颜色从深蓝（弱）到琥珀/白（强）。
- **点一下**瀑布 = 把解调点移到那里（黄色竖线）。虚线是中心频率。
- **拖动**瀑布 = 平移中心频率（相当于改窗口）。被锁时会提示还剩多少秒。
- **滚轮 / ← →** = 按当前模式的步进微调（SSB 100 Hz、AM 1 kHz、FM 5 kHz / 50 kHz）。
- **双击中心频率** = 直接键入 MHz（例如 `14.0740`）。
- **预设条**：中波 / 80m / 40m / 20m / 15m / 10m / 6m / FM 广播 / 航空 / 2m，
  每个预设自带模式和带宽。
- **模式与带宽**：AM / USB / LSB / CW / NFM / WFM。带宽选项随模式变化。
- **静噪**：按解调后的电平判断，阈值以下不出声（页面右上角的电平条是同一个值）。
- **音量**：只影响你这一路，不影响别人。

第一次点击/按键才会启动音频（浏览器的自动播放策略），所以刚打开没声音时**先点一下页面**。

---

## 4. 增益（最常见的"收不到信号"）

页面不暴露 RF 增益，是因为它属于**盒子级**设置：默认 `gRdB = 40 / LNA = 2`。
如果某个波段明显过载（音频发糊、瀑布一片饱和），或反过来底噪太高，按下面改：

```bash
# 打开电台级配置
sudo nano /opt/mrrc_modern/mrrc_modern.env
# 追加（或者改这两行）
MRRC_SDR_GAIN_DB=40
MRRC_SDR_LNA=2
sudo systemctl restart mrrc-modern
```

RSP1 的 LNA 只有 **0–3** 档（写 4 会 `Init=1`，服务日志里是
`gain LNAstate out of range (0:3)`）。过载事件（`power overload`）会被自动计数并降 6 dB，
下限 `gRdB = 20`。

---

## 5. 故障对照

| 页面现象 | 原因 | 处置 |
|---|---|---|
| 页头红条 `SDR 未连接` | 设备没插 / 被内核驱动抢走 / 库缺失 | 见下方三条命令 |
| `sdr pipe exited (1)` | 子进程崩了（多半是库或权限） | `journalctl -u mrrc-modern -n 50` 看 `sdr_pipe:` 行 |
| 只有噪底、没有台 | 天线不覆盖这个波段（HF 天线在 VHF/UHF 就只有噪底） | 换波段或换天线 |
| 音频断续 | 网络抖动 | 已在 220 ms 预缓冲；外网差时改 `sdr.js` 的 `prebufferMs` |
| 所有人都在同一个频率 | 有人刚改过中心频率，锁 30 秒 | 等倒计时结束 |

**体检三条命令**（在盒子上）：

```bash
lsusb | grep 1df7                     # 设备在不在
lsmod | grep msi2500                  # 不该有输出（被黑名单挡掉才对）
systemctl is-active sdrplay           # 厂商守护进程
```

如果 `msi2500` 出现了，说明黑名单没生效：

```bash
cat /etc/modprobe.d/mrrc-sdrplay.conf   # 应是 blacklist msi2500 / msi001
sudo modprobe -r msi2500 msi001
sudo systemctl restart sdrplay mrrc-modern
```

**设备被拔插**：pipe 会退出并把状态广播为不可用，服务端按 1/2/5/10 秒退避重启；
恢复后回到上次的中心频率，不需要人干预。

---

## 6. 容量与开销（实测）

| 项 | 数值 |
|---|---|
| 每路解调 CPU | AM 0.086 核 / USB 0.151 核（W103D，2.4 MSPS） |
| 解调通道上限 | 默认 8（`MRRC_SDR_MAX_CHANNELS`，上限 16） |
| 听众上限 | 默认 50（`MRRC_SDR_MAX_CLIENTS`） |
| 频谱 | 1024 bin @10 Hz；超过 12 人自动降到 512 bin |
| 带宽 | 每路音频 4 KB/s（32 kbps Opus）；频谱 10 KB/s（降档后 2.5 KB/s） |
| 同频复用 | 同 (offset, mode, bw) 的听众**共用一路解调**，静噪/音量各管各 |

---

## 7. 打包与许可证

镜像里会多出这些（`MRRC_SDR=off` 时一个都不装）：

```
/usr/local/lib/libsdrplay_api.so.3.15      厂商闭源库（SDRplay）
/opt/sdrplay_api/sdrplay_apiService        厂商守护进程（root，sdrplay.service）
/etc/udev/rules.d/66-sdrplay.rules         USB 节点 0666
/etc/modprobe.d/mrrc-sdrplay.conf          blacklist msi2500 / msi001
/usr/local/lib/libsdr_dsp.so               本项目：信道化 + 解调
/usr/local/lib/libsdr_api_shim.so          本项目：厂商 API 的扁平封装
```

`libsdrplay_api` / `sdrplay_apiService` 的许可证原文随镜像放在
`/opt/sdrplay_api/sdrplay_license.txt`，**只允许随 SDRplay 硬件使用**。

---

## 8. 已知限制（V1）

- **WFM 单声道**（立体声留到下一版）；
- **不做**聊天 / spotter / RDS / 数字模式解调 / 录音 / 回放；
- **不支持**"跟随 FT-710 频率"（留了一个预设位，下一版做）；
- 只有**一台** SDR、**一个**调谐器；
- RSP1 已停产：换 RSP1B / RSPdx 时把 `MRRC_SDR_*` 的档位重测一遍（API 支持，但 LNA 档位不同）。
