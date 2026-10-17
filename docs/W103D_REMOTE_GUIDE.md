# W103D 盒子遥控器指南

一台盒子、一支蓝牙遥控器，人站着就能把电台调好、按下去就能说话。这份指南只说遥控器：
怎么配对、每个键干什么、不好用时先查哪一条。

## 1. 能用的遥控器

盒子认的是**标准的蓝牙 HID 遥控器**（就是能和手机、电视配对的这一类）。配好之后，
内核会多出一个叫 `<遥控器名字> Keyboard` 的输入设备，按键从这里进来。

已经实测通过的一支：中国移动的语音遥控器，蓝牙名 `CMCC_Voice_Remote`。

两件目前**不行**的事，先说明白，省得白折腾：

- **红外遥控器不行。** 盒子在设备树里注册了红外接收器（`meson-ir`），但这一批
  板子上没有焊接收头，对着盒子按键收不到任何波形。我们试过两支遥控器，`/dev/lirc0`
  一直是空的。
- **遥控器自带的麦克风用不了。** 它的语音走的是厂商自己的一条蓝牙通道，不是标准
  蓝牙耳机那种音频协议，盒子读不出来。

## 2. 配对（只做一次）

1. 让遥控器进入配对模式。中国移动这支是**同时长按「OK」和「返回」约 5 秒**，
   指示灯开始快闪。
2. 在盒子上配对。有两条路，效果一样：
   - 用面板：浏览器打开 `/settings`，蓝牙一节里搜索并配对；
   - 用命令行：SSH 进盒子（`root` / 出厂口令），`bluetoothctl` 里 `scan on`，
     看到 `CMCC_Voice_Remote` 后 `pair <地址>`、`trust <地址>`。
3. 按一下遥控器的任意键，确认它连上了：

```
bluetoothctl info 0C:F3:CE:B0:88:75
```

看到 `Paired: yes`、`Bonded: yes` 就配好了。**配对状态会保存，重启不掉。**

### 配好了但按键没反应？看这一条

蓝牙有个容易踩的坑：**报告「配对成功」不等于链路加密成功**。

第一版就是这么翻车的：`bluetoothctl` 说 `Paired: yes`，但按键一个都不来。抓空口报文
才看见真相：

```
LE Start Encryption
Encryption Change ... Encryption: Disabled      ← 加密没建立起来
```

链路是明文，遥控器就把所有属性读取都拒掉（日志里 `unlikely error` 一串），
系统因此拼不出键盘设备。**修法是重配一次：**

```
bluetoothctl remove 0C:F3:CE:B0:88:75
bluetoothctl power off ; sleep 2 ; bluetoothctl power on
# 遥控器重新进配对模式，再 pair 一次
```

这次要看到这一行才算成：

```
Encryption: Enabled with AES-CCM
```

加密一通，`/proc/bus/input/devices` 里立刻出现 `CMCC_Voice_Remote Keyboard`，
按键开始上报。

## 3. 按键表

| 按键 | 作用 |
| --- | --- |
| **语音**（按住）| **发射**，松开就停 |
| **▲ / ▼** | 频率加上/减去当前步进 |
| **◀ / ▶** | 换步进档位（100 Hz / 1 k / 5 k / 10 k / 100 k）|
| **频道 + / −** | 换波段（160 m → 80 m → … → 6 m）|
| **数字 0–9** | 直接输入频率，见下一节 |
| **确定**（方向键中间）| 提交输入的数字 |
| **设置** | 模式循环（LSB / USB / CW / AM / FM / FT8）|
| **返回** | 取消还没提交的数字输入 |
| **音量 + / −** | 音量 |
| **电源** | 交给遥控服务处理，**不会**再把盒子关掉 |
| 首页 / 菜单 / M / `*` / `#` | 预留，暂时没有动作 |
| **电视** | 不发蓝牙。这类键只发红外去控电视，盒子收不到 |

发射有三重保护，和网页上一样：松手就停、遥控器掉线也停、单次发射有上限。

## 4. 直接输入频率

数字按 **kHz** 算，按「确定」生效。举例：

| 想去的频率 | 按 |
| --- | --- |
| 7.050 MHz | `7` `0` `5` `0` → 确定 |
| 14.070 MHz | `1` `4` `0` `7` `0` → 确定 |
| 3.573 MHz | `3` `5` `7` `3` → 确定 |

按错了不要紧：按「返回」清掉，或者等 3 秒不管它，输入框自己作废。
输入途中按 ▲▼ 也会把没提交的数字丢掉，直接调频。

## 5. 出问题先查这几条

| 现象 | 先看什么 |
| --- | --- |
| 按键完全没反应 | `bluetoothctl info <地址>` 是否 `Paired: yes`；`journalctl -u bluetooth -n 40` 里有没有 `unlikely error`（有就按 §2 重配）|
| 只有部分键有用 | 按键表按**扫描码**认键。这类遥控器上「设置」「M」「#」三个键的键码都是 240，只看键码会混成一个。换遥控器要改键位表，见 §6 |
| 按电源把盒子关了 | `grep HandlePowerKey /etc/systemd/logind.conf` 应该是 `ignore`；不是就改掉再 `systemctl restart systemd-logind` |
| 遥控器连上又断，反复 | BLE 遥控器空闲时会自己断开、按键时再连，这是正常的；只要按键有效就不必管 |
| Wi-Fi 偶尔卡一下 | 这颗芯片的蓝牙和 Wi-Fi 共用一路 SDIO，遥控器连续操作时 Wi-Fi 可能短暂抖动，几秒内自愈 |
| 服务没在跑 | `systemctl status mrrc-localctl`，日志 `journalctl -u mrrc-localctl` |

## 6. 换一支遥控器

按键和动作的对应关系在 `local_ctl.py` 的 `DEFAULT_KEYMAP` 里，一张
「扫描码 → 动作」的表。换遥控器的步骤：

1. 先认出它发什么：`evtest /dev/input/eventN` 里每个键有两行，`MSC_SCAN` 的值就是
   扫描码（比如 `value 70052`），下一行是键码；
2. 在表里加一条，扫描码写成 `"u:0x70052": "freq_up"`；
3. 不想改代码就写一份 JSON 覆盖文件，用环境变量指过去：

```json
{ "u:0x70052": "freq_up", "u:0x70051": "freq_down" }
```

```
# /etc/systemd/system/mrrc-localctl.service 里加上
Environment=MRRC_LOCALCTL_KEYMAP=/opt/mrrc_modern/env/keymap.json
```

可用的动作名：`ptt`、`freq_up`、`freq_down`、`step_up`、`step_down`、`band_up`、
`band_down`、`enter`、`cancel`、`menu`、`mode_next`、`vol_up`、`vol_down`、
`digit:0` … `digit:9`。

## 7. 它到底怎么连起来的

```
遥控器  ──蓝牙──>  盒子内核（HID 键盘）
                      │
                      ▼
              mrrc-localctl（读按键、查表）
                      │  本机 wss://127.0.0.1:8888/WSradio
                      ▼
                 电台服务（和网页发的是同一套命令）
```

这条链路只走本机回环，不经过局域网，也不经过云端。遥控器配在盒子上，就只对这台盒子有效。
