# 天线切换控制台（`/Antenna`）设计

- **日期**：2026-11-08
- **状态**：设计批准，待实现（第一期：4 选 1 手动切换 + 状态读回 + 诊断；不含自动/联动）
- **范围**：涂鸦（Tuya）Wi-Fi 4 路继电器模块（`1×4`，如 `TYWB-4CH` 一类），
  装在盒子所在局域网内，由盒子直连 TCP 6668 控制，作为**天线 4 选 1 切换器**
- **相关代码（计划）**：`tuya_proto.py`（纯协议层）、`tuya_switch.py`（连接 + 互锁状态机）、
  `static/antenna.html`、`static/antenna.js`、`server.py`（只做接线与守卫）
- **相关文档**：SDD 新增 **AD-034**；`docs/nano_vna.md`（同为「外设控制台」页面的先例）、
  `packaging/common/mrrc-service-layer.sh`（无需改动：不新增依赖）

---

## 1. 背景与动机

操作员手上有一台涂鸦 4 路 Wi-Fi 继电器模块，用途是**天线 4 选 1 切换**：四路继电器分别
对应四条天线，任意时刻只允许一路吸合。

这台设备没有 HTTP 接口，只有两条控制路径：

| 路径 | 特征 |
| --- | --- |
| 涂鸦云 | App 走这条（MQTT 到涂鸦服务器）。要外网、要 Access ID/Secret、token 会过期 |
| **本地协议** | **TCP 6668**，AES-128-ECB（3.1/3.3）、3.4 加 HMAC-SHA256 会话密钥、3.5 换 AES-GCM |

盒子与模块在同一局域网，本地协议是唯一不需要外网、延迟毫秒级、且不受云端改版影响的路径。
本设计的全部内容就是这个路径的落地。

### 1.1 为什么这不是一个「开关页」

天线切换器有两条物理约束，普通开关页两条都违反：

1. **互锁**：两路同时吸合 = 两个天线口并联/短路。必须 break-before-make。
2. **发射中不可切换**：PA 在切换瞬间失去负载，继电器触点还在拉弧。与
   `nano/vna.py` 的「发射中拒绝扫描」同一条理由（那里是 CH0 坐在天线上）。

所以本设计的核心不是 UI，而是 `AerialSwitcher` 那个状态机。

---

## 2. 硬件与协议事实

> **证据等级**：下列协议事实来自 **tinytuya 1.20.0 源码逐字段核对**
>（`core/message_helper.py` / `core/header.py` / `core/command_types.py` /
> `core/XenonDevice.py` / `core/crypto_helper.py`），**不是**在本设备上抓包得到的。
> 实现阶段必须用 `tests/test_antenna_proto.py` 的构造向量守住格式，并在拿到真机后
> 做一次线上核对（§10.3）。凡标 `[待实测]` 的，都是设计假设。

### 2.1 设备

| 项 | 值 |
| --- | --- |
| 类型 | 涂鸦 Wi-Fi 4 路继电器模块（`interlock / self-locking / inching` 三模式） |
| 继电器 | **非 latching**：断电即全释放 `[待实测]` |
| 局域网端口 | TCP 6668 |
| 发现端口 | UDP 6666（3.1）/ 6667（3.3）/ 7000（App 加密广播、3.5  solicited） |
| 凭证 | `dev_id` + `local_key`（16 字节）+ `ip` + 协议版本 |
| 凭证来源 | **涂鸦云一次性获取**（iot.tuya.com 项目 + 关联 App 账号 + `tinytuya wizard`）。之后本地永久有效，直到设备重新配网/恢复出厂 |

### 2.2 帧格式

**55AA 帧**（3.1 / 3.3 / 3.4）：

```
00 00 55 AA | seq u32be | cmd u32be | len u32be | payload | tag | 00 00 AA 55
len = len(payload) + len(tag) + 4
tag = crc32(prefix..payload)      # 3.1/3.3：4 字节 CRC32
tag = HMAC-SHA256(session_key)    # 3.4：32 字节
```

**6699 帧**（3.5）：

```
00 00 66 99 | 00 00 | seq u32be | cmd u32be | len u32be | iv(12) | ciphertext | tag(16) | 00 00 99 66
len = 明文长度 + 12 + 16
AAD = 帧头第 4..18 字节（14 字节，不含 prefix）
```

### 2.3 加密（`local_key` 16 字节）

| 版本 | 算法 |
| --- | --- |
| 3.1 / 3.3 | 明文 JSON → AES-128-ECB 加密（PKCS#7 pad）→ 若 cmd ∉ `NO_PROTOCOL_HEADER_CMDS`，在密文前拼 **15 字节明文头** `b"3.3" + b"\x00"*12` |
| 3.4 | 明文先拼 15 字节头 → 用**会话密钥**整体 AES-128-ECB 加密；帧尾用 **HMAC-SHA256(session_key)** 代替 CRC32 |
| 3.5 | 明文先拼 15 字节头 → AES-**GCM**（IVAAD = 帧头 14 字节），帧用 6699 前缀 |

`NO_PROTOCOL_HEADER_CMDS = {DP_QUERY(0x0A), DP_QUERY_NEW(0x10), UPDATEDPS(0x12),
HEART_BEAT(0x09), SESS_KEY_NEG_*(3/4/5), LAN_EXT_STREAM(0x40)}`
—— 即 **DP 查询与心跳不加版本头**，SET 类命令要加。这条很容易写错，单测必须覆盖。

**3.4 会话密钥协商**（`SESS_KEY_NEG_*`）：

```
1. C→D  cmd=0x03  payload = client_nonce(16B 随机)
2. D→C  cmd=0x04  payload = device_nonce(16B) + HMAC-SHA256(local_key, client_nonce)(32B)
        3.4 的整段 payload 先用 local_key 做 ECB 解密
3. C 校验 HMAC，然后发 cmd=0x05  payload = HMAC-SHA256(local_key, device_nonce)
4. session_key:
     3.4: AES-ECB(local_key).encrypt(client_nonce XOR device_nonce)   # pad=False
     3.5: iv = client_nonce[:12]
          AES-GCM(local_key, iv).encrypt(xor)[12:28]
```

### 2.4 命令与报文

| 用途 | 3.3 | 3.4 / 3.5 |
| --- | --- | --- |
| 查状态 | cmd `0x0A`，`{"gwId":id,"devId":id,"uid":id,"t":"<秒>"}` | cmd `0x10`，`{}` |
| 设 DP | cmd `0x07`，`{"devId":id,"uid":"","t":"<秒>","dps":{"1":false,…}}` | cmd `0x0D`，`{"protocol":5,"t":<int秒>,"data":{"dps":{…}}}` |
| 心跳 | cmd `0x09`，`{"gwId":id,"devId":id}` | 同左 |

**多 DP 一次下发是原生的**（`dps` 是多键对象），所以「四路一起断」是一个报文，
不是四次 —— 这正是 break-before-make 需要的原子性。

响应状态形如 `{"devId":"…","dps":{"1":true,"2":false,…}}`。

### 2.5 DP 映射

| DP | 含义 | 用途 |
| --- | --- | --- |
| **1 / 2 / 3 / 4** | 四路开关（bool） | 天线 1–4 |
| 9–12 | 四路倒计时（s） | 不使用 |
| 38 | `relay_status` 断电恢复策略：0=全断 1=全通 2=恢复上次 | 只读展示，可选写入 |

四路编号按 tuya-local / Homey 对 4-gang 开关的通行约定取 `1..4`。
`[待实测]` 若实测不同，改配置 `relay_dp` 即可，不改代码。

---

## 3. 决策

**D1 — 自己写协议层，不引入 tinytuya。**
`cryptography` 已在 `requirements.txt`（SSL/远程协助在用），所以**零新依赖**。
协议层是纯函数，可以完全离线单测，与 `net_wifi` / `local_ctl` / `box_ops` 同一套风格。
tinytuya 只作为「标定工具」在 Mac 上跑（生成参考帧），**不进盒子、不进依赖**。
代价：3.4 协商与 3.5 GCM 要自己踩，由 §10.1 的向量测试兜住。

**D2 — 互锁两道防线。**
设备固件设成 `interlock` 模式（App 里设），软件仍自己做 break-before-make。
不依赖固件行为：固件可能是「开新路时自动关旧路」，也可能是「拒绝第二路」，两种都不能
作为唯一保障。软件序列在两种固件上都安全。

**D3 — 启动只读回，不自动改。**
盒子启动后读回真实 DP，与该路上次选择比对，不一致**只在页面提示，不自动纠正**。
自动纠正意味着无人确认的物理动作，而此刻可能正在发射。

**D4 — 协议版本自动探测。**
连接时依次尝试 **3.3 → 3.4 → 3.5**，每次 2 s 超时（最坏 6 s，只在首次连接发生一次），
`version` 可人工钉死。顺序按「最轻的先试」定：3.3 是一条 DP 查询就能判定（设备返回
`data unvalid` 即为不匹配），3.4/3.5 要先跑一轮 nonce 握手才能判定，所以放在后面；
反过来先试 3.5 会让每台 3.3 设备白等一轮握手超时。探测成功的版本写回配置。

**D5 — 发射中拒绝，且拒绝在服务端。**
`POST /api/antenna/select` 在 `state.tx`（含 TUNE / FT8 发射）为真时返回 **409**，
连 OFF 都不下发。UI 灰按钮只是提示，不是守卫。

**D6 — 页面轮询 state，不上 WebSocket。**
设备状态变化极少（人手切 + 偶发 App 操作），2 s 轮询足够，且省掉一条 WS 通道的
生命周期与鉴权负担。若实测发现需要 <500 ms 反馈，再升级。

**D7 — 密钥失效单独报。**
解密失败 / CRC 错 / `retcode=0x01` → 「密钥失效或协议版本不符」，与「设备离线」
分开。两者的处置完全不同：前者要重新取 key，后者要检查电源和 Wi-Fi。

---

## 4. 组件

### 4.1 `tuya_proto.py`（纯协议，无 I/O）

```python
# 帧
def frame(seq: int, cmd: int, payload: bytes, version: float,
          session_key: bytes | None) -> bytes
def parse_frames(buf: bytes) -> tuple[list[TuyaMessage], bytes]   # 粘包/半包安全

# 加密
def aes_ecb_encrypt(key, data, pad: bool) -> bytes
def aes_ecb_decrypt(key, data, unpad: bool) -> bytes
def aes_gcm_encrypt(key, plaintext, aad) -> bytes      # iv|ct|tag
def aes_gcm_decrypt(key, blob, aad) -> bytes

# 3.4 / 3.5 会话密钥
def session_key(version, real_key, client_nonce, device_nonce) -> bytes

# 报文
def payload_query(version, dev_id, now) -> bytes
def payload_set(version, dev_id, dps: dict[int, bool], now) -> bytes
def payload_heartbeat(version, dev_id) -> bytes
def parse_status(payload: bytes) -> dict[int, object]   # "dps" → {int: value}
```

所有函数无 socket、无全局状态，输入输出都是 bytes/dict。

### 4.2 `tuya_switch.py`

```python
class TuyaError(Exception): ...
class TuyaAuthError(TuyaError): ...      # 密钥失效 → 页面单独报
class TuyaOffline(TuyaError): ...

class TuyaLink:
    """一条长连接：心跳、重连、请求-响应匹配。socket 工厂可注入。"""
    async def start(self) -> None
    async def stop(self) -> None
    async def query(self) -> dict[int, object]
    async def set_dps(self, dps: dict[int, bool]) -> dict[int, object]  # 返回读回值

class AerialSwitcher:
    """互锁状态机。不碰 socket，只调 link。"""
    async def select(self, channel: int) -> SwitchResult
    async def read(self) -> SwitchState
```

`AerialSwitcher` 的依赖（时间、link、锁）全部注入，测试用假 link + 假时钟。

### 4.3 `server.py` 接线

新增：

- `GET /Antenna` → `static/antenna.html`（`listen`/`sdr` 会话 403，与 `/Nano` 一致）
- `GET /api/antenna/state` → 页面轮询
- `POST /api/antenna/select` `{"channel": 1}` → 409（发射中）/ 422（未配置）/ 200
- `GET /api/antenna/config` / `POST /api/antenna/config` → 凭证与名称
- `POST /api/antenna/scan` → UDP 广播发现（返回可连的 `{ip, dev_id, product_key}`，
  拿不到 `local_key`，只用于省去手抄 IP/devId）。广播报文用 3.x 的固定 UDP 密钥解密；
  这一步**失败不影响手填**，扫描是便利功能，不是配置的前置条件。

模块级单例 `_antenna_switcher`，lazy 初始化，`lifespan` 结束时 `stop()`。

---

## 5. 互锁状态机（本设计的主体）

```
select(n):
  ①校验 n ∈ relay_dp
  ②async with lock:                      # 并发点两次只切一次
     ③state = await link.query()         # 读真实值，不信缓存
     ④healthy = 恰好一路 ON？
       若 healthy 且 ON 的正是 n  → 返回 unchanged（绝不重复敲继电器）
     ⑤if tx_active: raise TxLocked       # 409，连 OFF 都不发
     ⑥await link.set_dps({d1:False, d2:False, d3:False, d4:False})   # 一个报文
     ⑦await sleep(settle_ms)             # 默认 100，范围 60–150
     ⑧await link.set_dps({dn:True})
     ⑨after = await link.query()         # 必须有这一步：ACK ≠ 实际动作
     ⑩校验 after：恰好一路 ON，且是 n
```

### 5.1 失败语义

| 失败点 | 物理状态 | 返回 | 页面 |
| --- | --- | --- | --- |
| ③ 查询失败 | 未知 | `TuyaOffline` | 「设备离线」+ 重试 |
| ⑥ 全断失败 | 原状态未变 | `TuyaError` | 普通错误，可重试 |
| ⑧ 合目标失败 | **天线全断** | `SwitchResult(open=True)` | **红字「天线已断开」** + 自动重试 1 次 + 重试按钮 |
| ⑩ 读到 ≥2 路 ON | **互锁被破坏** | `SwitchResult(interlock_broken=True)` | **红字「互锁失效，请检查继电器」**，此后**不再自动动作** |
| ⑩ 读到 0 路 ON | 天线全断 | `SwitchResult(open=True)` | 同上红字 |

「不再自动动作」是粘滞的：`interlock_broken` 置位后，`select()` 直接拒绝，
直到人工点页面上的「我已检查，解除锁定」，该动作只清标志、不切继电器。

### 5.2 时间预算

| 阶段 | 典型 | 上限 |
| --- | --- | --- |
| 一个报文往返 | 30–80 ms | 500 ms（超时） |
| 空窗 | 100 ms | 150 ms |
| 一次完整切换 | 250–350 ms | 2.0 s |
| 心跳间隔 | 10 s | — |
| 心跳失败 3 次 → 重连 | — | 30 s，指数退避 1→30 s |

---

## 6. 配置

`antenna-switch.json`，默认与 `MRRC_MEM_FILE` 同目录（与 `audio-levels.json` 一致），
权限 `0600`（含密钥），原子写。

```json
{
  "dev_id": "bf1a2c3d4e5f6a7b8c9d0e",
  "local_key": "0123456789abcdef",
  "ip": "192.168.1.42",
  "version": "auto",
  "relay_dp": [1, 2, 3, 4],
  "names": ["10m", "15m", "20m", "备用"],
  "settle_ms": 100,
  "last_channel": 1
}
```

- `MRRC_ANTENNA_FILE` 覆盖路径；`MRRC_ANTENNA_*` 覆盖字段
- 密钥**永不出现在日志、bundle、API 响应**里（`GET /api/antenna/config` 返回
  `local_key_set: true/false`，不返回值）——与 `net_wifi` 的 PSK 处理同一条纪律
- 手工改坏的文件：启动时 clamp 并警告，不致命（与 `audio_levels` 一致）

---

## 7. 页面（`/Antenna`）

```
┌──────────────────────────────────────────────┐
│ 天线切换          ● 在线 · 3.3 · 192.168.1.42 │
├──────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐          │
│  │ ①  10m       │  │ ②  15m       │          │
│  │   ● 已接通   │  │   ○          │          │
│  └──────────────┘  └──────────────┘          │
│  ┌──────────────┐  ┌──────────────┐          │
│  │ ③  20m       │  │ ④  备用      │          │
│  │   ○          │  │   ○          │          │
│  └──────────────┘  └──────────────┘          │
├──────────────────────────────────────────────┤
│ 发射中，切换已锁定                            │  ← 仅 TX 时出现
└──────────────────────────────────────────────┘
```

- 四路大卡片；当前那一路整块高亮 + 绿色圆点，其余 `opacity: .55`
- 名字就地编辑（`contenteditable` → 失焦保存），默认「通路 1–4」
- 顶部状态条四项：在线/离线、协议版本、IP、**实际读回的状态**
- 切换中：被点的那张卡片显示 spinner，其余禁用（一次只有一个切换在跑）
- 三种异常态（§5.1）用页面顶部的红条呈现，红条不自动消失
- 未配置时整页替换为「连接设置」表单：IP / devId / localKey / 版本（auto 或钉死）
- 权限：`listen` / `sdr` 会话 403；未登录跳登录页

样式复用 `ft710.css` 的暗琥珀主题变量，不新建主题。页面自包含
（`antenna.html` + `antenna.js`），**不加载** `ft710_main.js` / `ft710_ui.js`。

---

## 8. 安全与守卫

| 守卫 | 实现 |
| --- | --- |
| 发射中拒绝 | `state.tx` 为真 → 409；TUNE / FT8 发射同样计入 |
| 并发 | `asyncio.Lock`，一次一个切换 |
| 角色 | `listen` / `sdr` 会话 403（`_antenna_forbidden`，与 `_nano_forbidden` 同形） |
| 密钥泄露 | 只在 0600 文件里；API 只回布尔；日志与 bundle 过滤 |
| 互锁破坏后 | 粘滞停机，只能人工解除 |
| 未配置 | `POST /select` 返 422，页面显示连接表单 |

**不做**：CSRF token（与其余 `/api/*` 一致，主站会话本身即凭证）、二次确认弹窗
（切换动作本身安全，唯一的危险时刻已由 TX 守卫拦下）。

---

## 9. 数据流

```
浏览器 ──2s GET /api/antenna/state──▶ server ──（缓存 1s）──▶ TuyaLink
浏览器 ──POST /api/antenna/select───▶ server ──▶ AerialSwitcher ──▶ TuyaLink ──TCP 6668──▶ 模块
Browser ◀── 读回后的真实 DP ──────────┘
```

`TuyaLink` 常驻一条 TCP 连接，后台 task 每 10 s 心跳；收到设备主动上报的
`cmd=8 (STATUS)` 或 `cmd=0x0A/0x10` 响应时更新本地缓存。`GET /state` 命中 1 s 内的
缓存就不再打设备 —— 这条缓存是为了**多标签页同时轮询**时别把一条 6668 连接压满，
不是为了省延迟；`select()` 永远绕过缓存做实时 `query()`。

---

## 10. 测试

### 10.1 协议层（离线，`tests/test_antenna_proto.py`）

- 构造向量：3.3 / 3.4 / 3.5 各一组 SET / QUERY / HEARTBEAT 的**完整字节**
  （由 Mac 上的 tinytuya 生成，固化成常量）
- 帧解析：粘包（两个帧拼一起）、半包（截断）、CRC 错、suffix 错、超长 `len`
- 加密往返：3.3 pad/unpad；3.4 HMAC；3.5 GCM + AAD
- `NO_PROTOCOL_HEADER_CMDS` 的反向断言：DP_QUERY 不带版本头、CONTROL 带
- 会话密钥派生：3.4 与 3.5 各自的 XOR + 加密 + 切片 `[12:28]`

### 10.2 状态机（假 link + 假时钟，`tests/test_antenna_switch.py`）

- 正常切换：断言调用序列 `off-all → sleep → on-n → query`，且 OFF 是一次调用
- 幂等：已在该路 → 零次 `set_dps`
- 已两路 ON（不健康）→ 先全断再合，且不返回 `unchanged`
- ON 失败 → `open=True`，自动重试恰好 1 次
- 读回 ≥2 路 ON → `interlock_broken=True` 且此后 `select()` 直接拒绝
- TX 中 → `TxLocked`，且**没有**任何 `set_dps` 调用
- 并发两次 `select(1)`/`select(2)` → 串行，第二次基于第一次的结果
- `settle_ms` 边界（60 / 150 / 越界 clamp）

### 10.3 真机核对（拿到 key 后做一次，记录进文档）

- `query()` 返回的 DP 键集合中确实有 1/2/3/4
- 设 `interlock` 模式后再发「两路同开」，观察设备是拒绝还是自动关旧路（决定 D2 的注释）
- 断电再上电，读回的四路值（DP 38 的实际生效行为）
- 一次完整切换的实测耗时分布

### 10.4 路由（`tests/test_antenna_api.py`）

- 未登录 401；`listen` / `sdr` 403
- 未配置 → `/state` 200 + `configured:false`；`/select` 422
- TX 中 `/select` 409
- 配置读写：`local_key` 不回显；坏文件 clamp 不崩

---

## 11. 范围外（本期不做）

- 按频段自动切换（用户明确选纯手动）
- 倒计时 DP 9–12、定时
- 两路以上同开（物理上禁止）
- 涂鸦云 API 兜底路径
- `interlock / self-locking / inching` 模式的页面切换（在 App 里设一次即可）
- 天线切换与 ATR-1000 记忆库的联动

---

## 12. 风险与未验证项

| # | 风险 | 处置 |
| --- | --- | --- |
| R1 | 非 latching 继电器，**盒子断电 = 天线全断** | 文档明写：切换器与电台不同电源，或断电时必须手动断电台。软件无法补偿 |
| R2 | 协议事实来自 tinytuya 源码，未在本机抓包 | §10.3 真机核对；协议层用固化的构造向量，改动用测试守 |
| R3 | 部分固件限制并发本地连接数 | 盒子只占一条长连接；若实测挤掉第二个控制器，退化为「按需连接」（切换时连、之后断开） |
| R4 | DP 编号与假设不符 | `relay_dp` 是配置项，不改代码 |
| R5 | `local_key` 在设备重配网后失效 | D7 单独报错 + 页面提示重新获取；不在代码里藏重试 |
| R6 | 3.5 的 GCM 细节（AAD 长度）实现偏差 | 10.1 的 3.5 向量测试；失败时明确报「协议 3.5 不支持」而不是静默离线 |
