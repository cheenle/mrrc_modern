# Cloud Hub 实例生命周期：应用侧发现（2026-10-11）

> **背景**：`bg7ifp.mrrc.vlsc.net` 长期 502 的完整取证（frps 侧、门户侧、时间线、诊断链）在 hub 仓：
> `mrrc_hub/docs/2026-10-11-instance-lifecycle-and-port-squatting.md`。
> 本文件只记 **mrrc_modern 侧**的复现、证据与修法建议。**本文档不伴随任何代码变更。**

## 结论速览

| # | 现象 | 严重度 | 建议修法 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | **退出应用不回收子进程**（macOS 打包版）：quit 后 `MRRC-Modern-Server` + `frpc` 成孤儿，继续占着隧道端口 | 高 | 启动器退出时先停隧道、再退服务；启动时把"孤儿清扫"从 frpc 扩展到 Server | 未修 |
| 2 | **证书重签后必须重启进程**，否则继续出示旧证书（页面无提示） | 高 | 监听证书文件变化重建 TLS 上下文，或至少在 Cloud 面板提示"需重启" | 未修 |
| 3 | **源码运行**：frpc 只在 `<repo>/fleet/frpc` 找；缺失时 connect 返回 `tunnel_started: false` 而**界面不报错** | 中 | 查找路径回退到数据目录（与打包版一致）；connect 失败必须回错误 | 未修 |
| 4 | **已撤销的实例仍会连回，并可抢占门户已分配给他人的端口** | 高 | 应用侧：连之前查一次门户状态，`revoked` 就不起隧道；根治在 hub 侧（每实例独立 token，见 hub 文档 §5.1） | 未修 |

## 1. 退出应用不回收子进程（macOS，实测一次）

**证据**（MacBook Pro，2026-10-11 20:4x；进程树取自 quit 之前）：

```text
19185  /Applications/MRRC-Modern.app/Contents/MacOS/MRRC-Modern-Launcher
  └ 19194  MRRC-Modern-Server --ssl-cert …/certs/fullchain.pem --ssl-key …/certs/bg8aaa.key
      └ 19195  /Applications/MRRC-Modern.app/Contents/Frameworks/payload/frpc \
                 -c …/Application Support/MRRC-Modern/fleet/frpc-bg8aaa.toml
（应用连续运行 7 天 11 小时；launchd 里只有 application.net.vlsc.mrrc-modern.*，即应用本身）
```

**复现**：`osascript -e 'quit app "MRRC Modern"'` → **Launcher 退出，19194/19195 的 PPID 变为 1 并继续运行**，
远端端口仍被占；手动 `kill 19194 19195` 才释放。

**待确认**：菜单里的"退出"是否同样如此（本次只在 AppleScript quit 上复现过）。

**修法建议**：

- 启动器在退出流程里**显式**停隧道（`TunnelProcess.stop()`）等停服务端，再退出自身；
- 启动时的孤儿清扫（v1.25.4 已在 macOS 生效的那套）**覆盖 `MRRC-Modern-Server`**，而不只是 `frpc`；
- 服务端加父进程死亡检测（或让启动器用 `PDEATHSIG` 等价机制），避免"父死子活"。

## 2. 证书重签后必须重启进程

**机理**：TLS 上下文（`ssl.SSLContext` / 证书链）只在进程启动时构建一次；Cloud Hub 的 enroll/重签
发生在进程运行期间，因此进程继续出示**旧**证书，直到重启。

**实例证据**（ba4eg / W103D 盒子）：证书文件 20:05 已安装，而 `mrrc-modern` 服务 19:31 就起来了
⇒ 对外仍是旧证书；`systemctl restart mrrc-modern` 后立即正确（入口 302）。

**修法建议**：

- 监听 `MRRC_SSL_CERT/KEY` 的 mtime，变化后**重建 TLS 上下文**（无需重启进程）；
- 退一步：Cloud Hub 面板在"证书与门户不一致"时给出**显式提示**（"需重启才能生效"），
  现在的表现是静默仍用旧证书。

## 3. 源码运行：frpc 查找路径与静默失败

**证据**（本机 Mac mini，bg1sb 实例）：

- 应用在源码运行下只查 **`<repo>/fleet/frpc`**；`~/Library/Application Support/MRRC-Modern/fleet/frpc`
  在这条路径上**不被读取**（打包版才用数据目录）—— 这正是"frpc 装好了却 `tunnel_started: false`"的原因；
- `POST /api/cloud/connect` 在 frpc 缺失时返回 `{"tunnel_started": false}`，**HTTP 仍是 200、界面照常显示已连接**。
  设 `MRRC_FLEET_DIR` 指向 repo 的 `fleet/` 后同一调用立即 `true`。

**修法建议**：查找顺序改为"数据目录 → repo（或反向）"并提示命中路径；`connect` 失败必须回**非 2xx + 明确原因**。

## 4. 已撤销的实例仍会连回并抢占端口（应用侧事实）

- 应用只依据本地 env（`MRRC_CLOUD_TOKEN/LABEL/PORT`）与 `fleet/frpc-<label>.toml` 连隧道，
  **从不查询门户的授权状态**；
- 因此 2026-10-06 被撤销的 bg8aaa 一直连到今天，并在 10-11 抢占了门户重新分配给 BG7IFP 的 18803；
- 应用侧可做的缓解：启动/重连前拉一次门户状态，`revoked` 就**不起隧道**并把本地 Cloud Hub 置为断开；
  根治需要 hub 侧"每实例独立 frpc user/token"（见 hub 文档 §5.1）。

## 附：同批次发现、尚未处理（提案，未获批）

- **连接设置页的串口下拉**会把板载 `ttyS0`–`ttyS4` 也列出来（W103D 上尤其显眼）⇒ 建议按 `driver=` 过滤（`ftdi_sio` 等）。
- **电台后插**：启动时没有设备则当前不会自动重试 ⇒ 建议无设备时进入低频重试，插上即用。

（这两条与本次 502 无关，仅登记，均未开工。）

## 复现与验收

```bash
# ① 本机 frpc 与父进程（打包版应挂在 MRRC-Modern-Server 下，源码运行挂在 server.py 下）
ps -Ao pid,ppid,etime,command | grep -E '[f]rpc'
# ② 退出应用后检查孤儿（PPID=1 即缺陷 1）
ps -Ao pid,ppid,command | grep -E '[M]RRC-Modern|[f]rpc'
# ③ 实例对外出示的证书 vs 本机证书（不等即缺陷 2 的场景）
echo | openssl s_client -connect 127.0.0.1:8888 2>/dev/null | openssl x509 -noout -subject -fingerprint -sha256
# ④ 源码运行的隧道启动结果（false 即缺陷 3）
curl -sk -X POST https://127.0.0.1:8888/api/cloud/connect -H 'Content-Type: application/json' -d '{}'
```
