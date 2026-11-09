# 侧键 PTT 与海能达安卓机（调研 · 2026-11）

> 目的：搞清楚「海能达对讲机的侧面 PTT 在 Android 里到底是什么」，以及本 App
> （`FT710Android/`）要怎样才能用它发射。本文只做事实、来源与设计，**没有真机抓包**，
> 凡未实测的地方都标了「未验证」。

---

## 0. 结论先行

1. **海能达的 Android 机分两类，PTT 键的去向完全不同**：
   - **PDC/PTC 多模机**（PDC760 / PTC760）：PTT 属于**窄带电台引擎**，Android 只是它上面
     的一层应用系统。第三方 App **拿不到**这个键（GroupTalk 兼容性矩阵实测 `PDC760 PTT=No`，
     而同一台的报警键 `Alarm=Yes`）。
   - **PNC PoC 机**（PNC370/380/460/550/560）：整机就是安卓 PoC，PTT 是**普通输入键**，
     第三方 PTT App 能用（GroupTalk 矩阵 `PNC370/380/550 PTT=Yes`）。
2. **Android 没有标准 PTT keycode**。API 35 的 `android.jar` 里 319 个 `KEYCODE_*` 常量
   **没有 `KEYCODE_PTT`**（AOSP main 分支也没有）。所以每家厂商各自为政：要么用厂商私有
   码（三星 1015、UROVO 520–522），要么把标准键重新映射（摩托罗拉 LEX F10 / Sonim XP10 = 228，
   Ulefone = 381/301/131）。**因此唯一可靠的方案是「学习按键」**，不能写死码表。
3. **本 App 在 v1.1.36 之前完全没有硬件按键通路**：PTT 只有屏上
   `PTTButton → PTTManager.press()/release()`（`PTT/PTTManager.kt:45,57`）。
   现在已补上（见 §4）：**前台 `onKeyDown/onKeyUp` → 无障碍服务（后台/息屏）→ 共用同一个
   `PTTManager`**；媒体键（MediaSession）仍未做。

---

## 1. 海能达产品线：谁把 PTT 交给 Android

| 机型 | OS | PTT 键到得了第三方 App？ | 依据 |
| --- | --- | --- | --- |
| **PDC760 / PTC760** | Android + PDT/DMR 双模 | **否**（报警键可以） | GroupTalk 兼容性表：`PDC760 PTT=No / Alarm=Yes`；PDC760 的 PTT 是大号窄带 PTT，配 PDT 集群/常规业务 |
| **PNC370** | Android（5.1） | **是** | GroupTalk：`PTT=Yes / Alarm=No` |
| **PNC380** | Android | **是** | GroupTalk：`PTT=Yes / Alarm=Yes` |
| **PNC550** | Android（8.1） | **是** | GroupTalk：`PTT=Yes / Alarm=Yes`；侧键：`1x PTT, 2x Vol, 1x Alarm, 1x Power` |
| **PNC460 / PNC560** | Android 12 | 未直接实测，按同族推断为**是** | 官方强调 «open API» 与 «物理 PTT 键»；PNC560 中文页写「提供开放式 API 接口」 |
| **PNC360** | **Linux/RTOS，不是 Android** | 不适用 | 规格页明确 `RTOS (Linux)`；GroupTalk 只写「GUI 不适配」 |

要点：

- **PDC760 是可编程键（programmable key）+ 报警键能出来，主 PTT 不出来**。想让这类机器
  按侧键发射，只能：① 用报警键/可编程键当 PTT（CPS 里把它们设成不冲突的功能）；
  ② 或者干脆用屏上 PTT / 蓝牙 PTT 咪。这不是本 App 能改的，是硬件归属问题。
- 海能达确实宣传「Open API / SDK 供第三方开发」（PNC550 手册、PNC460 新闻稿），但
  **公开渠道拿不到文档**，要走海能达商务/开发者渠道（NDA）。所以不要按「SDK 路线」设计
  产品功能，按「通用安卓按键」设计才对所有 rugged 机通吃（Inrico / RugGear / Ulefone /
  Sonim / Motorola LEX 都是同一套路）。

---

## 2. Android 侧：一个「侧键」要经过什么

### 2.1 物理键 → `.kl` → `KeyEvent`

```
按键矩阵 → Linux input event (EV_KEY, scanCode)
        → /system/usr/keylayout/*.kl 映射
        → KeyEvent(keyCode, scanCode, action, repeatCount)
        → 焦点窗口 / 无障碍服务 / MediaSession
```

厂商在 `.kl` 里既可以映射成标准键（`KEYCODE_F1=131`、`KEYCODE_12=228`、`KEYCODE_DEMO_APP_1=301`），
也可以声明私有码（数值 500+）。**同一个数字在不同机器上含义不同**，这就是为什么必须学习。

### 2.2 已知的厂商 PTT 码（社区/邻域实测，用于「猜默认值」）

| 设备 | PTT 键码 | 备注 |
| --- | --- | --- |
| Motorola LEX F10 / LEX L11 | **228** | HyTalkPTT 默认值；228 = 标准 `KEYCODE_12` |
| Sonim XP10 (XP9900) | **228** | TAK-XVoice 源码注释 `KEYCODE_PTT / 228` |
| UROVO DT30 | **520–522** | 私有码区 |
| Ulefone（26WT/20WT/18T…） | **381 / 301 / 131** | 301=`DEMO_APP_1`，131=`F1` |
| Samsung Active Key（XCover/Tab Active） | **1015** | 三星 Knox 私有码，走广播 `com.samsung.android.knox.intent.action.HARD_KEY_REPORT` |
| **Hytera PNC 系** | **未公开** | 用 §5 的方法现抓；不要猜 |

### 2.3 四条到达路径（按「能否后台」排序）

| 路径 | 前台 | 后台/息屏 | 代价 |
| --- | --- | --- | --- |
| `Activity.dispatchKeyEvent` / `onKeyDown/Up` | ✅ | ❌ | 零权限，但必须在前台且未被锁屏 |
| **AccessibilityService**（`canRequestFilterKeyEvents`） | ✅ | ✅ | 用户手动开无障碍；**全系统同时只允许一个过滤服务** |
| MediaSession / 媒体键（`KEYCODE_HEADSETHOOK=79`、`MEDIA_PLAY_PAUSE=85`） | ✅ | 部分 | 蓝牙耳机/PTT 咪常走这条；需 `FLAG_HANDLES_MEDIA_BUTTONS` + 设置 PlaybackState |
| 系统广播 `android.intent.action.PTT_DOWN` / `PTT_UP` | ✅ | ✅ | **HyTalk 生态内部约定**（Hytera 自家 PoC App `com.hytera.ocean` 收这两个广播）；第三方 App 也能发，但目标 App 必须主动接收 |

**无障碍路径的两个硬事实**（TAK-XVoice 2026-07-14 实测记录，可直接引用）：

- `android:canRequestFilterKeyEvents="true"` 是**承重行**：缺了它，`onKeyEvent` 永远不派发，
  哪怕 `dumpsys accessibility` 里显示 `requestFilterKeyEvents=true`。
- `accessibilityEventTypes` 不能写 `typeNone`/`0`（AAPT 直接拒绝），写一个无害的
  `typeWindowStateChanged` 即可；`accessibilityFeedbackType` 也不能为空，否则服务可能不在
  `getEnabledAccessibilityServiceList()` 里，设置页会显示「未启用」。

### 2.4 必须处理的坑

1. **auto-repeat**：按住不放时 `onKeyDown` 会以 `repeatCount > 0` 反复来，只能认第一次。
2. **锁屏**：Activity 收不到键；要后台就得无障碍。
3. **系统抢占**：Hytera PNC 上预装的 HyTalk 可能自己吃 PTT 键（甚至直接拉起自己）。
   现场要先把 HyTalk 的 PTT 占用让出来（关掉自启/换 PTT key app）。
4. **过滤服务互斥**：已有一个「按键过滤」无障碍服务时，我们的服务收不到键（系统只给一个）。
5. **国产 ROM**：无障碍服务被清理/被「省电策略」杀掉后不会自动回来，界面必须能显示真实状态。

---

## 3. 本 App 的现状（代码事实）

| 位置 | 现在是什么 |
| --- | --- |
| `UI/PTTButton.kt:39` | 屏上按钮，`detectTapGestures` 手势 → `vm.onPttGesture()` / `vm.onPttRelease()` |
| `ViewModel/MainViewModel.kt:335-336` | `onPttGesture → pttManager.press()`；`onPttRelease → pttManager.forceRelease()` |
| `PTT/PTTManager.kt:45,57,65` | `press() / release() / forceRelease()`，看门狗 500 ms × 3，`release()` 无条件发 `ptt:false` |
| `App/MainActivity.kt:44` | `onStop` → `shouldReleaseOnStop(isChangingConfigurations)` → `vm.onPttRelease()`（唯一松手点） |
| `AndroidManifest.xml` | v1.1.36 起：`WAKE_LOCK` + `PttAccessibilityService` 声明 |
| `UI/SettingsScreen.kt` | 分区：频谱 / 音量增益 / 本机 / **硬件 PTT** / 入口 / 设备诊断 |

上表是 v1.1.36 **之前**的基线；硬件 PTT 补齐后的落点见 §4。

服务端侧已经具备的东西（不用改）：按键会话独占仲裁、`txhb` 活性闸门、
`MRRC_PTT_MAX_TX_SECONDS` 时长上限——硬件 PTT 只是多了几个「按下/松开」的来源。

---

## 4. 落地情况（v1.1.36 已实现）

原计划三档代码，实际落地如下——**§4.1 / §4.2 已完成，§4.3 未做**。

| 文件 | 作用 |
| --- | --- |
| `PTT/HardwarePtt.kt` | 纯 Kotlin：`HardwarePttConfig` / `HardwarePttMode` / `HardwarePttRouter`（按键→press/release，学习、去重、去自动重复）+ `object HardwarePtt`（进程级单例，Activity 与无障碍服务共用） |
| `App/MainActivity.kt` | 前台捕获：`onKeyDown/onKeyUp` → `HardwarePtt.router` |
| `App/PttAccessibilityService.kt` + `res/xml/ptt_accessibility_service.xml` | 后台/息屏捕获（`canRequestFilterKeyEvents`），可选唤醒并切前台 |
| `UI/HardwarePttSection.kt` | 设置页「硬件 PTT」卡片：开关 / 学习对话框 / 模式 / 无障碍状态与跳转 / 唤醒开关 |
| `Data/SettingsStore.kt`、`UI/UiPrefs.kt` | 持久化：`hwPttEnabled` / `hwPttKeyCode` / `hwPttScanCode` / `hwPttMode` / `hwPttWake` |
| `App/ServiceLocator.kt` | 把偏好流推进 `HardwarePtt`，把 `press/release/isKeyed` 接到 `MainViewModel` |

三条与原计划的出入，都是实现时被系统/工具链逼出来的：

1. **前台用 `onKeyDown/onKeyUp`，不用 `dispatchKeyEvent`**。后者在 `androidx.activity.ComponentActivity`
   上标了 `@RestrictTo`，lint `RestrictedApi` 直接报错（首次构建实测）。语义等价：界面没消费的键
   会冒泡到 Activity。
2. **无障碍开启时，"两条路径都收到同一个键"其实很少发生**：按键过滤服务先收到键，
   返回 `true` 就等于消费掉，应用根本看不到。真正会两边都收到的是**音量键**——因为我们
   故意不全局吞它（见下）。所以 `down` 去重仍然必须保留。
3. **音量键不做全局劫持**：服务对 `VOLUME_*` 返回 `false`，别的界面照常调音量；前台时由
   Activity 消费掉，所以不会出现"一边发射一边改音量"。

### 4.1 安全铁律（已实现，不可退化）

| 规则 | 理由 / 落点 |
| --- | --- |
| 学习模式**只记录、不发射** | 用户在学习时按的是同一颗物理键；`startLearn()` 同时把可能残留的"按住"放掉 |
| 学习不绑定 Home/Back/AppSwitch | 否则用户退不出对话框（`learnIgnored`） |
| `ACTION_UP` 必发 `release()`，`repeatCount>0` 一律吞 | 松手不发射是最高优先级 |
| 服务 `onInterrupt` / `onDestroy` → `reset()` 并补释放 | 服务被杀时物理键还在"按住"状态，不释放就是长发射 |
| 退后台退出学习（`ON_STOP` → `cancelLearn`） | 否则无障碍服务会在**别的 App 里**吞掉下一个按键 |
| 硬件与屏上共用 `PTTManager` | TX 状态单一真源，看门狗/txhb 只有一套 |
| 未连接控制通道时 `press()` 直接丢 | `PTTManager` 已有，别绕过 |
| `MainActivity.onStop` 仍是唯一"退后台松手"点 | 旋转走 `RotationPolicy`，不松手 |

### 4.2 设置页（已实现）

```
硬件 PTT
[开关] 侧键 / 音量键发射
      按键：KEYCODE_F1·87                [重新学习]
      模式：[按住说话] [点按切换]
      后台与息屏生效                     [去开启] / [系统设置]   ← 显示系统里的真实状态
[开关] 按下时唤醒并切到前台
```

「点按切换」下面常驻一行警示：离开界面/息屏后可能再也解不开，只在对端设了 TX 超时兜底时使用。

### 4.3 未做：蓝牙媒体键（MediaSession）

`KEYCODE_HEADSETHOOK` / `MEDIA_PLAY_PAUSE` 这类走 MediaSession 的按键**没有**单独实现：
前台时它们会和普通按键一样冒泡到 `Activity.onKeyDown`，能学习、能用；**后台**要活得靠
MediaSession（`FLAG_HANDLES_MEDIA_BUTTONS` + PlaybackState），这次没做。HFP 厂商私有事件
（`+XEVENT` / `TALK`）本来就在应用层看不到（§6）。

---

## 5. 现场怎么把键抓出来

设备侧（不需要 root）：

```bash
# 1) 按下侧键，看内核报的扫描码与映射后的键名
adb shell getevent -l
#    /dev/input/event2: EV_KEY  KEY_F1  DOWN

# 2) 看这个键最终映射成哪个 KEYCODE（含 .kl 文件路径）
adb shell dumpsys input | grep -A40 "KeyLayouts\|Key Edges"

# 3) 反过来验证：注入一个键码，看 App 有没有反应
adb shell input keyevent 228
```

App 内（最终验收走这条，用户手上没有 adb）：设置 →「硬件 PTT」→「学习」→ 按住侧键 →
界面显示 `keyCode / scanCode`。

海能达现场顺序：

1. **PNC 机**：先确认 HyTalk 不抢键（关闭它的自启 / 系统里若有「PTT key app」映射就指到本 App），
   再学习。
2. **PDC760**：主 PTT 抓不到是正常的。改抓**报警键**或**可编程键**（CPS 里配置），
   抓到后当 PTT 用；两者都抓不到就只能屏上 PTT。
3. **普通手机（小米/荣耀/…）**：没有侧键可抓，走屏上 PTT 或蓝牙 PTT 咪。

---

## 6. 未验证 / 风险

- **没有海能达真机**：PNC 的键码、PDC760 报警键的键码都未实测；本文的机型结论来自
  GroupTalk 兼容性矩阵与厂商资料，不是抓包。
- **Hytera SDK**：只确认「官方宣称开放 API」；接口形态、是否要签名/白名单、是否暴露 PTT
  按下/松开事件，均未核实。若将来拿到文档，可另开一条「SDK 源」，但仍应保留通用按键源。
- **HFP 厂商私有事件的蓝牙 PTT 咪**（如某些 Inrico 咪）在应用层可能完全不可见，
  只能靠用户换咪或改用带按键的蓝牙耳机。
- **国产 ROM 的后台启动限制**：无障碍服务里 `startActivity` 在部分 ROM 上会被拦，
  需要现场验证；被拦时退化为「不唤醒，只发射」。
- **多用户/工作资料**：无障碍服务是按用户启用的，副用户里要重开。

---

## 7. 参考

- GroupTalk 兼容性矩阵（含 Hytera 各机型 PTT/Alarm 实测）：<https://grouptalk.com/ptt-accessories/tested-phones>
- HyTalkPTT（按键学习 + 广播转发的完整实现，厂商码表来源）：<https://github.com/chepil/HyTalkPTT>
- TAK-XVoice 无障碍服务注释（`canRequestFilterKeyEvents` 承重、Sonim/三星码、去重）：
  <https://github.com/TX-RX/TAK-XVoice>
- Tetra Poland 操作手册（Hytera PNC-class 预设 + 学习按键 + 后台需无障碍）：
  <https://tetra-poland.pl/manual/app/TetraPoland-Operating-Manual-EN.pdf>
- Android `AccessibilityServiceInfo.FLAG_REQUEST_FILTER_KEY_EVENTS` 文档：
  <https://developer.android.com/reference/android/accessibilityservice/AccessibilityServiceInfo>
- Hytera PDC760（Android 多模机，PTT 归窄带引擎）：<https://www.hytera.com/eg/products/lte-broadband/lte-radio/PDC760.html>
- Hytera PNC560（Android 12 + open API）：<https://www.hytera.com/cn/product-new/lte-broadband/poc-radio/pnc560.html>
