# 明文落地端口（`::8899` → TLS `127.0.0.1:8888`）

**这是一台盒子的现场安排，不是产品能力。** 产品只提供一个监听、而且始终是 TLS
（`MRRC_SSL_CERT`/`MRRC_SSL_KEY`，缺失时 `ssl_bootstrap.sign_for()` 现签自签证书）。
本文记录的是在 `MRRC-BG1SB`（`192.168.1.217`）上用 socat 加出来的第二个**明文**门：
给那些处理不了自签证书（或干脆只认 `http://` / `ws://`）的客户端留一条路。

| 项 | 值 |
| --- | --- |
| 盒子 | `MRRC-BG1SB` / `192.168.1.217`（armbian，`mrrc` 用户，Cloud Hub `ba4eg`） |
| 产品版本 | 1.28.77（`/opt/mrrc_modern`） |
| 加密门 | `*:8888` TLS，证书 `CN=ba4eg.mrrc.vlsc.net`（`.local/share/MRRC-Modern/certs/`） |
| 明文门 | `*:8899`（`::` 双栈），socat 1.7.4.4-2 |
| 实施日期 | 2026-11-16 |

## 1. 为什么是"脱壳"而不是改 `server.py`

改产品代码的路线（新增 `MRRC_PLAIN_PORT`，同进程第二个 `uvicorn.Server`，第二个实例
`lifespan="off"` 以免电台连接/FT8 引擎初始化两遍）能拿到真实客户端 IP，但要连带改
`server.py` 的启动路径、补测试、走发版——为了**一台**盒子不值得。socat 是**字节隧道**：
WebSocket 升级、Opus 音频、1701 B 频谱帧原样通过，服务端一行代码都不知道多了一扇门。

代价写在 §5：客户端 IP 全部退化成 `127.0.0.1`。

## 2. 组件

```bash
apt-get install -y socat          # bookworm main，1.7.4.4-2，+OPENSSL
```

`/etc/systemd/system/mrrc-plain-relay.service`（`enabled` + `active`）：

```ini
[Unit]
Description=MRRC Modern plaintext relay (:: 8899 -> TLS 127.0.0.1:8888)
After=network-online.target mrrc-modern.service
Wants=network-online.target

[Service]
User=mrrc
Group=mrrc
ExecStart=/usr/bin/socat -d -d TCP6-LISTEN:8899,fork,reuseaddr,ipv6only=0,keepalive,backlog=128 OPENSSL:127.0.0.1:8888,snihost=ba4eg.mrrc.vlsc.net,verify=0
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

## 3. 四个刻意选择

1. **`TCP6-LISTEN:…,ipv6only=0`** = `::` 双栈（按要求）。这同时意味着：盒子持有全局
   IPv6（`2409:8a00:…:fe63`），而 `iptables -P INPUT` 是 `ACCEPT`，所以 **8899 在公网上
   是可见的**，不只是局域网。想收回成仅 IPv4：把 `TCP6-LISTEN:8899,fork,reuseaddr,ipv6only=0`
   换成 `TCP-LISTEN:8899,fork,reuseaddr`。
2. **`verify=0` + `snihost=ba4eg.mrrc.vlsc.net`**：后端就是本机那张 Cloud-Hub 证书。
   **那张证书一个字节都不要动**——`ssl_bootstrap.sign_for()` 幂等、hub 对它做了 pin，
   重签会让 `ba4eg` 入口永久 502（2026-10-15 现场事故）。
3. **没有 `Requires=mrrc-modern.service`**：只有 `After=`。这样 `systemctl restart mrrc-modern`
   （改配置、云证书重启、`os._exit(42)` 那条路径）**不会把门带走**。socat 是每连接才去拨后端，
   所以服务端短暂不在，只会让那一条连接失败。
4. **`backlog=128`**：socat 的默认 backlog 是 5，而一个浏览器页面一次就开 6 条连接
   （控制/音频RX/音频TX/频谱…），5 会在忙碌时直接拒连。

## 4. 验收（2026-11-16 实测，非"应该能行"）

| 检查 | 结果 |
| --- | --- |
| `curl http://192.168.1.217:8899/api/health` | **401**（改之前是 `000` 连不上） |
| `curl http://[2409:8a00:…:fe63]:8899/api/health` | **401**（双栈成立） |
| `curl http://127.0.0.1:8899/login` | **200** |
| `POST http://127.0.0.1:8899/api/auth/login`（真口令） | **200**，`mrrc_auth` cookie 64 hex |
| `ws://…:8899/WSradio` → `fullState.version` | **1.28.77**，`serial_connected=true`；`127.0.0.1`、`[::1]`、`192.168.1.217` 三条都通 |
| `ws://…:8899/WSspectrum` | 2.0 s **18 帧 × 1701 B** 二进制 |
| `curl -k https://192.168.1.217:8888/` | **302**（原 TLS 门行为未变） |
| `journalctl -u mrrc-plain-relay` | `SSL connection using TLS_AES_256_GCM_SHA384`，正常结束后 `exiting with status 0` |
| `ss -lntp` | `*:8899`（socat，backlog 128）+ `*:8888`（python，TLS） |

`fullState.version` 是菜单页脚那个号，所以上面这条是**行为验证**：明文门后面确实是这台盒子的
同一个服务端进程，不是另一份代码。

## 5. 安全边界（用这条门之前必须知道的）

- **口令与音频明文过网**：局域网如此，公网 IPv6 也如此（无防火墙规则兜底）。谁抓到
  `/api/auth/login` 就拿到 30 天有效的会话令牌，而这个界面**能按下发射键**。
- **服务端看到的客户端全是 `127.0.0.1`**：登录限流是 5 次 / 5 分钟 / IP，`127.0.0.1`
  这个桶与 **Cloud Hub 的 frpc 隧道共用**（frpc 也从 loopback 拨进来）。所以在 8899 上
  连错 5 次口令，公网入口会被一起锁 5 分钟。日志里也分辨不出是哪台设备干的。
- 好消息：`MRRC_AUTO_PASSWORD` 是空的，所以"首启口令横幅只给 loopback 看"那条路径不会
  把口令泄露给局域网客户端。
- **浏览器在 `http://` 下不给麦克风**（`getUserMedia` 要安全上下文，只有 localhost 例外）：
  8899 上能听、能看瀑布、能调谐、能按 PTT，但**没有浏览器发射音频**——按 PTT 就是一段
  静音载波。`sw.js` 也不会注册。

## 6. 回滚

```bash
systemctl disable --now mrrc-plain-relay
rm /etc/systemd/system/mrrc-plain-relay.service
systemctl daemon-reload
apt-get purge -y socat
ss -lntp | grep 8899      # 应该什么都没有
```

## 7. 换一台盒子照做

```bash
# 1) 依赖
DEBIAN_FRONTEND=noninteractive apt-get install -y socat
# 2) unit（把 snihost 换成那台盒子的证书 CN，:8899 换成想要的端口）
# 3) 生效
systemctl daemon-reload && systemctl enable --now mrrc-plain-relay
# 4) 验收：health 401 / login 200 / WS fullState 拿到版本号
curl -s -o /dev/null -w '%{http_code}\n' http://<ip>:8899/api/health
```

`mrrc-update` / 整包升级**不会**覆盖 `/etc/systemd/system/mrrc-plain-relay.service`，
所以这条门在升级后仍在；反过来说，升级完也要记得它还在。

## 8. 不要做的事

- 不要为了"让证书对得上"去重签盒子的证书（hub pin，见 §3）。
- 不要把这条门当产品能力分发到客户盒子：客户盒子的明文口等于把口令写在明信片上。
  真要做成产品能力，走 §1 提到的同进程第二监听，那一条能拿到真实客户端 IP。
