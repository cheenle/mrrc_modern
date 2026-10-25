# W103D 常见问题：一句话「连不上门户」

盒子报「连不上 `portal.mrrc.vlsc.net`」的时候，**断点几乎从来不在 portal**。

2026-10-25 同一天的两台盒子说明了这件事：一台的网络、DNS、时钟、TLS 全部正常，断在**本机配置
文件读不到**；另一台的网络同样正常，断在**本机时钟慢了 24 天**。两台在页面上看到的都是同一句
「连不上」。所以这份文档按**「先看哪一行」**组织，不按原因分类 —— 原因有四类，报错却长得一模一样。

## 一分钟判据

| 你看到的 | 先看这个 | 通常是 |
| --- | --- | --- |
| 界面「提交失败」，日志里 `POST /api/cloud/apply 502` | 本机有没有 `MRRC_CLOUD_*`（见下） | 盒子没有云端身份，重新申请又被门户拒绝 |
| `certificate is not yet valid` / `certificate has expired` | `date`、`timedatectl` | **本机时钟错**，不是证书错 |
| `Name or service not known` | `getent hosts portal.mrrc.vlsc.net` | DNS |
| `Hostname mismatch` | 解析出来的 IP | 域名解析到了错的地方 |
| 入口显示「这台实例当前离线」（503） | 盒子上 `pgrep -a frpc` | 隧道没起（进程挂了或被顶掉） |
| 「已存在申请（状态 granted）」 | 门户里的申请记录 | 这个呼号已经在别处登记过 |
| `proxy [bg7xxx] already exists` | 另一台机器是否还开着 | 一个呼号只能有一条活隧道 |

## 五条命令，两分钟

在盒子上（SSH 或本机终端）依次跑，哪一条先失败，问题就在那一层：

```
# 1) 本机有没有云端身份（没这几行 = 从没接入过，或配置文件丢了）
grep -E '^MRRC_CLOUD' /opt/mrrc_modern/env/mrrc.env

# 2) 配置文件读得到吗 —— 属主必须是应用自己的用户（mrrc），不能是 root
stat -c '%U:%G %a' /opt/mrrc_modern/env/mrrc.env

# 3) 时钟（报证书问题时一律先看这一条）
date; timedatectl | grep -E 'synchronized|NTP service'; chronyc sources

# 4) DNS 与出网（resolv.conf 为空 = 解析器没配，见下面「盒子的 DNS」）
cat /etc/resolv.conf | grep -v '^#'
getent hosts portal.mrrc.vlsc.net
curl -sS -o /dev/null -w '%{http_code} %{remote_ip}\n' --max-time 15 https://portal.mrrc.vlsc.net/

# 5) 应用自己的信任库（和浏览器用的不是同一套）
sudo -u mrrc /opt/mrrc_modern/venv/bin/python -c \
  "import sys; sys.path.insert(0,'/opt/mrrc_modern'); import net_tls; \
   print('net_tls ->', net_tls.urlopen('https://portal.mrrc.vlsc.net/', timeout=15).status)"
```

判读口诀：

- **`synchronized: no` 且 `chronyc sources` 是空表** → 先修时间，别去动门户配置。
- **第 4 条 200、界面却说失败** → 不是网络，去看第 1、2 条（本机状态）。
- **门户回 409「已存在申请」** → 这个呼号已被占用，走门户的登记口令（换机）流程，不要反复提交申请。

## 为什么会这样：TLS 只是这条链的翻译器

上游四件事任何一件出错，最后都会变成一句 `CERTIFICATE_VERIFY_FAILED` 或者一个 502：

| 上游故障 | 链路上的样子 | 应用最终吐出的话 |
| --- | --- | --- |
| DNS 不对 / 不解析 | 名字解不出来 | `Name or service not known` |
| 域名解析到错地址 | TCP 通，证书名不对 | `Hostname mismatch` |
| 外部 NTP 不通 | 时钟停住不动 | `certificate is not yet valid` / `has expired` |
| 本机时钟错（无 RTC 电池） | 同上 | 同上 |
| 随包 CA 与系统 CA 不一致 | 浏览器正常、应用失败 | `unable to get local issuer certificate` |
| 本机配置读不到 | 应用以为自己没登记 | 界面「提交失败」+ 日志 502 |

**一句话：看到 `certificate is not yet valid`，先敲 `date`，不要先怀疑证书。**

## 常见问题

### 盒子能上网，但页面说「连不上门户」，日志是 `POST /api/cloud/apply 502`

**真相**：网络没问题，是这台盒子**没有云端身份**。它只能走「申请」，而门户回的是 409
「已存在申请（状态 granted）」，服务端把门户的任何拒绝一律显示成 502 —— 看起来像网关故障。

**怎么办**：不要反复点申请。让卖家/运维在门户上给这个呼号做「换机」（用登记口令接管，
或重新下发身份），盒子这边再点一次连接即可。判断"是不是这类问题"只看一条：
第 1 条命令有没有输出。

### 报 `certificate is not yet valid`（证书尚未生效）

**真相**：盒子以为现在是过去。门户的证书是近期签发的，在慢了的时钟看来"还没生效"。
2026-10-25 的现场：盒子显示 `2026-10-01 17:38`，慢了 24 天。

**怎么办**：

```
systemctl restart chrony        # 立刻重新解析时间源
sleep 8; chronyc sources        # 表里应该有 3 个以上来源
timedatectl | grep synchronized # 应为 yes
```

如果 `chronyc sources` 仍是空表（开机时 Wi-Fi 比 chrony 起得晚，时间源域名解析不到），
按下面那条加纯 IP 的时间源。

### `chronyc sources` 是空的 —— 时间从来没同步过

**真相**：镜像是从"几号烧的盘"那一刻开始的，而盒子**没有 RTC 电池**，断电就丢时间。
如果 Wi-Fi 在 chrony 启动之后才连上，基于域名的时间源（`pool …`）解析失败，chrony
就一直空着 —— 重启一次 chrony 立刻就好，但下次断电还会重演。

**怎么办**（一次性）：写一份**纯 IP** 的时间源，不依赖 DNS：

```
sudo tee /etc/chrony/sources.d/mrrc.sources >/dev/null <<'EOF'
server 203.107.6.88   iburst
server 119.28.206.193 iburst
server 162.159.200.1  iburst
EOF
sudo systemctl restart chrony
```

这三个是阿里 / 腾讯 / Cloudflare 的公共 NTP。写在 `/etc` 下，固化和升级都不会覆盖。

### 门户里显示「已批准」，公网入口还是 503

**真相**：批准 ≠ 这台盒子在用。入口要成立，需要**四件事同时成立**：盒子有身份 →
隧道连着 → 门户里有这台盒子的证书 → 盒子真的在出示那张证书。

**怎么办**：先把顺序理一遍，缺哪一步补哪一步：盒子 `ps -ef | grep frpc` 有没有进程 →
盒子 `curl -sk https://127.0.0.1:8888/login` 有没有应答 → 门户里这台盒子的证书指纹和
`server.crt` 是否一致。任何一步不一致都请卖家处理，不要自己删证书。

### 「已存在申请（状态 granted）」/ `proxy [bg7xxx] already exists`

**真相**：一个呼号只能有一条活隧道。原来的机器（通常是另一台电脑或旧盒子）还连着，
新机器顶上去会被拒绝。

**怎么办**：先让旧机器退出（关掉它的公网入口/退出应用），再在新盒子上接管呼号。
顺序反了会出现"两台都显示已连接、入口却指向旧机器"。

### 入口的证书和盒子出示的不一致，入口一直 502

**真相**：接入时新证书写进了配置，但**进程还持有启动时那张证书**。盒子上的
`MRRC_SSL_CERT` 已经指向新文件，服务却要重启才会重新加载。

**怎么办**：重启一次应用（页面里提示「还需重启应用」时就点它，或在盒子上
`sudo systemctl restart mrrc-modern`），再刷新入口。

### 页面说「还没提交过申请」，可是明明申请过

**真相**：`/opt/mrrc_modern/env/mrrc.env` **读不到**。最常见是被改成 `root:root`，
而应用以 `mrrc` 运行 —— 应用读配置失败时不报错，只是当成"什么都没有"。

**怎么办**：

```
sudo chown mrrc:mrrc /opt/mrrc_modern/env/mrrc.env
sudo chmod 0640       /opt/mrrc_modern/env/mrrc.env
sudo systemctl restart mrrc-modern
```

改完再看第 1 条命令，能列出呼号与令牌就对了。

### 盒子的 DNS 是什么？路由器不发 DNS 会怎样

**出厂默认**：每条已保存的连接都带两个备用解析器 —— `114.114.114.114` 和 `8.8.8.8`，
追加在已有 DNS 的后面。**你自己配的 DNS 不会被替换**，路由器下发的那个也照旧可用
（`ipv4.ignore-auto-dns` 保持关闭）。

为什么要有这条默认：盒子没有 RTC 电池，开机时间来自镜像；如果 Wi-Fi 比 DHCP 先起来，
解析器就是空的 —— 于是 `chronyc` 连 NTP 的域名都解析不到，时钟停在镜像日期，接着每个
HTTPS 都报 `certificate is not yet valid`。2026-10-25 的两台盒子就是这么坏的。

看一眼现在生效的解析器：

```
cat /etc/resolv.conf | grep nameserver
nmcli -g ipv4.dns connection show "$(nmcli -t -f NAME connection show --active | head -1)"
```

想换掉或再加一条（改完立刻生效，不用重启）：

```
nmcli connection modify "<连接名>" ipv4.dns "114.114.114.114 8.8.8.8 223.5.5.5"
```

只有一条 `dns=` 行、没有重复，就是对的：这个默认值是幂等的，每次启动只会补上缺的那几条。

**更常见的一层**：`/etc/resolv.conf` 本身就指向一个「外来」地址（2026-10-25 之前的镜像把它烤进了成品），这时连 NetworkManager 的 DNS 都没被用上 —— 见 [盒子的 DNS 坏了：在 Windows 上一条命令修好](fix-dns.html)，那张页面给了一个只要网页口令的小工具。

**如果 `nmcli` 里能看到这几条、`/etc/resolv.conf` 里却只有一条**：那是 2026-10-25 之前的镜像（构建时把构建机的 `resolv.conf` 拷进了系统，`resolvectl status` 会显示 `resolv.conf mode: foreign`）。**不用敲任何 Linux 命令** —— 在 Windows 电脑上运行 [`MRRC-FixDNS.cmd`](fix-dns.html)，填一次盒子 IP 与网页口令就能修好；有 root 权限的运维也可以照 [从零指南 §20](W103D_GUIDE.md) 那条手工三行修。

### 时间不对还会影响什么

不止门户。2026-10-25 的现场里，同一台盒子因为时钟错了 24 天，**软件更新也一直失败**
（更新检查走的是同一个 HTTPS），时间修好后当次就升了一个版本。同理受影响的还有
诊断包上传、远程协助、以及所有需要校验证书的功能。

### 自查完还是不通，交什么给支持

三样东西，缺一样都会多一轮往返：

1. 上面「五条命令」的原样输出（`MRRC_CLOUD_TOKEN` 那行涂掉）。
2. 页面上的完整报错文字，以及出现的时间。
3. 菜单 → 「🐞 遇到问题」里的**诊断包**（传不上去就按
   [局域网取包与诊断](diag.html) 那页拿下来手工发）。

## 两句能记住的话

- **报证书问题时先看 `date`；说连不上时先看配置文件属主。**
- **一个呼号一条活隧道：换机先停旧机，再接管。**
