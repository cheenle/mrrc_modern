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

> **前提（镜像已带）**：服务账号 `mrrc` 要在 `input` 组里 —— `/dev/input/eventN` 是
> `root:input 0660`。缺了它，遥控器配得再好，`mrrc-localctl` 每次打开都是
> `Permission denied`，而界面上没有任何地方会说「缺组」。查：`id mrrc` 里有没有 `input`。

1. 让遥控器进入配对模式：**同时长按「OK + 返回」约 5 秒**，指示灯开始快闪。
2. 浏览器打开盒子的 `/settings`，找到「**遥控器**」，点「搜索附近的遥控器」（约 12 秒）。
3. 列表里出现 `CMCC_Voice_Remote` 后点「**配对**」。

> **列表里有一堆东西时挑哪个。** 排在**第一行**的那台就是它，两个标记一起看：
> 行尾写着 **「遥控器/键盘」**（bluez 认得出它是 HID 键盘，这条最硬），
> 后面跟着**信号强度**（贴着盒子时 -40 ~ -60 dBm）。排列顺序是
> 「像遥控器的在前 → 当场扫到的在前（强的在前）→ 只有缓存名字的在后」，所以
> 遥控器在配对模式时必然在最上面。没有信号强度的那几行是 bluez 记得、但此刻没在广播的
> 设备（灯不闪了就是这样）。

**配对为什么要这么久、为什么必须由浏览器点。** 盒子上跑的是交互式 `bluetoothctl`
（pty + `--agent NoInputNoOutput`）。这不是花活：一次性 `bluetoothctl pair` 不注册配对
agent，bluez 发出的 Pairing Request 里**绑定位是 0**，而 BLE HID 遥控器必须存一把密钥才能
重连，它会直接回 SMP `Invalid parameters`（`0x0a`）——链路建起来了，配对被一句话否掉。
进了 pty 之后请求变成 `Bonding (0x29)`，一次就成，bluez 随后建出
`CMCC_Voice_Remote Keyboard`。

**只发现一台正在广播的遥控器时，页面会自己配对**（不用再点「配对」）——扫描结束直接
开始配，配上就写「已配对，按键可用 ✓」。没配上时页面会把 bluez 的原话贴出来
（例如 `Device … not available` = 遥控器已经退出配对模式，`AuthenticationFailed` =
握手时它走了），照着那句话处理即可。

看到页面写 **「已配对，按键可用 ✓」** 就成了，按一下方向键验证。

> 页面判断的是「键盘设备有没有真的建出来」，不是「蓝牙说配上了」。这两件事不一样，
> 原因见下面那节。

### 没有网页的时候（命令行）

SSH 进盒子（`root` 或 `mrrc`）后：

```
bluetoothctl --timeout 20 scan bredr          # 遥控器是经典蓝牙
bluetoothctl --timeout 25 pair  <地址>
bluetoothctl trust <地址>
bluetoothctl info <地址> | grep -E "Paired|Bonded|Connected"
```

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
