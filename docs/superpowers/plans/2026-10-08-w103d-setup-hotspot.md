# W103D 热点 + 最小向导 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 刷完盘开机，盒子在没有可用上行网络时自己发一个**开放热点**；手机连上后打开 `https://<网关>:8888`
**免口令**即可设 Web 口令、选 Wi-Fi；成功后切到 STA 并关掉热点。

**架构：** 新增一个**常驻**小服务 `linux/mrrc_hotspot.py`（判定 → 起/停热点 → 计时器 → 对外报状态），
用 `nmcli` 起开放热点；服务器新增 `/setup` 路由与引导页，**免口令闸门是"请求来自热点网段"**而不是
"是否已配置"标记（热点没了，那个网段自然不存在 —— 见规格 D-6）。引导页写口令**复用既有的**
`cloud_hub._write_config` 通道，不新建写入者（规格 D-7.5）。

**技术栈：** Python 3.11（镜像内）/ 3.13（本仓 `.venv`，见全局约束 1）、纯标准库（`subprocess`、`ipaddress`、
`argparse`、`time`）、FastAPI（既有）、`unittest`、bash（`box-overlay.sh` 在 `set -euo pipefail` 下）。

**设计文档：** `docs/superpowers/specs/2026-10-08-w103d-setup-ap-design.md`（下称"规格"；§N / D-N / R-N 指其章节、决策号与需求号）。

---

## 全局约束（每个任务都适用，违反即返工）

1. **测试环境与基线（2026-10-08 实测）**：本 worktree **没有自己的 venv**，用主仓那个：
   ```bash
   cd /Users/cheenle/HAM/hub/mrrc_modern/.worktrees/w103d-box
   PY=/Users/cheenle/HAM/hub/mrrc_modern/.venv/bin/python
   $PY -m unittest discover -s tests
   ```
   当前基线：**1766 例 / 1 个既存失败**（`test_tls_trust_store.CallSiteGuardTests.test_every_call_site_sets_a_context`，
   **main 上同样红，不许修**）。每完成一个任务，失败数**不得超过 2**。

2. **绝不 `git add -A`**：主检出有 3 个不属于本任务的脏文件（`atr1000_tuner.json`、`mem_channels.json`、`start.sh`）。
   只 `git add` 本任务明确列出的文件。

3. **每次 commit 前**必须 `python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged` 为 clean。

4. **commit 风格**：conventional commits，英文小写祈使句，正文说**为什么**不说**做了什么**。

5. **测试与硬件无关**：`nmcli` 一律经**注入的 runner** 调用，测试里用假 runner。禁止在测试里真跑 `nmcli`。

6. **块级约束（sdd-guardian 报过，会拦 commit）**：`secrets-hardcoded`（口令/密钥不得写字面量）、
   `cat-direct-serial-io`（串口 I/O 只能经 CatController/CivController）。本计划的代码不得触碰这两条。

7. **计数器同步**：新增测试模块要同时更新 `tests/README.md` 的计数与清单，以及
   `tests/test_spectrum_profile_docs.py::VersionConsistencyTests::test_tests_readme_matches_the_actual_count`
   里那两个字面量（它同时断言旧值**不存在**，所以要改三处：总数、模块数、旧值）。

---

## 文件结构（先锁分解，再写任务）

| 动作 | 文件 | 职责 | 谁在测 |
| --- | --- | --- | --- |
| **新建** | `linux/mrrc_hotspot.py` | 热点服务：判定 / 起停 / 计时 / 状态。**唯一**碰 `nmcli` 的地方 | `tests/test_mrrc_hotspot.py` |
| **新建** | `packaging/box/mrrc-hotspot.service` | systemd 单元（常驻） | `tests/test_box_profiles.py` |
| **修改** | `server.py` | 新增 `/setup`（GET 页面 + POST 设口令/连 Wi-Fi），含免口令闸门 | `tests/test_setup_wizard.py` |
| **新建** | `static/setup.html` | 引导页（设口令 + 选 Wi-Fi） | `tests/test_setup_wizard.py` |
| **修改** | `packaging/box/box-overlay.sh` | 装热点服务 + 单元 + `dnsmasq` 进 apt 清单 | `tests/test_box_profiles.py` |
| **新建** | `tests/test_mrrc_hotspot.py` | 判定 / nmcli 参数 / 计时器 / 状态 | 自身 |
| **新建** | `tests/test_setup_wizard.py` | 闸门 / 设口令 / 连 Wi-Fi 的负例 | 自身 |

**边界铁律**：
- `mrrc_hotspot.py` **只碰 NetworkManager 的连接**，绝不读写 `env/mrrc.env` ✅
- `server.py` 的 `/setup` **只碰 env**（经既有写入通道），绝不碰 NM ✅
- 两个域各有唯一写入者 —— 这是 R4 与可测性的前提 ✅

---

## 任务 1：热点判定是纯函数

**文件：**
- 创建：`linux/mrrc_hotspot.py`
- 测试：`tests/test_mrrc_hotspot.py`

- [ ] **步骤 1：编写失败的测试**

```python
"""The rule that decides whether the box should be broadcasting.

Pure function first, because the rule is the part worth arguing about and the
part the spec states exactly: a hotspot appears when there is no usable uplink.
"""
from __future__ import annotations

import unittest

from linux.mrrc_hotspot import Uplink, needs_hotspot


class NeedsHotspotTests(unittest.TestCase):
    def test_no_cable_and_no_wifi_means_hotspot(self):
        self.assertTrue(needs_hotspot(Uplink(ethernet_link=False, wifi_connected=False)))

    def test_a_cable_switches_it_off(self):
        self.assertFalse(needs_hotspot(Uplink(ethernet_link=True, wifi_connected=False)))

    def test_joined_wifi_switches_it_off(self):
        self.assertFalse(needs_hotspot(Uplink(ethernet_link=False, wifi_connected=True)))

    def test_having_both_is_still_off(self):
        self.assertFalse(needs_hotspot(Uplink(ethernet_link=True, wifi_connected=True)))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_mrrc_hotspot -v`
预期：FAIL — `ModuleNotFoundError: No module named 'linux.mrrc_hotspot'`

- [ ] **步骤 3：编写最少实现代码**

```python
#!/usr/bin/env python3
"""Keep an open setup hotspot up while the box has no way onto a network.

Why this exists: configuring the box needs a way *into* the box, and every way
in needs a network or a keyboard. Someone holding only a phone has neither, so
the box broadcasts one and the wizard at /setup does the rest (design D-6).

This module owns the network side and nothing else. It never reads or writes
`env/mrrc.env`: the wizard owns that, through the server's existing writer.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Uplink:
    """What the box currently has, as far as getting onto a network goes."""

    ethernet_link: bool
    wifi_connected: bool


def needs_hotspot(uplink: Uplink) -> bool:
    """Broadcast only when there is nothing else.

    Both conditions matter: a cable with no link is not an uplink, and an
    authenticated WiFi association is what makes the radio useless for an AP.
    """
    return not uplink.ethernet_link and not uplink.wifi_connected
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_mrrc_hotspot -v`
预期：PASS（4 例）

- [ ] **步骤 5：Commit**

```bash
$PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add linux/mrrc_hotspot.py tests/test_mrrc_hotspot.py
git commit -m "box: the hotspot rule is a pure function, so it can be argued about"
```

---

## 任务 2：nmcli 适配层（唯一的 network 写入者）

**文件：**
- 修改：`linux/mrrc_hotspot.py`
- 测试：`tests/test_mrrc_hotspot.py`

- [ ] **步骤 1：编写失败的测试**

```python
class NmcliTests(unittest.TestCase):
    """Every nmcli call goes through an injected runner, so tests never touch
    the real NetworkManager. The assertions are on the argv, because the argv
    is what actually decides whether an AP is encrypted."""

    def _recording(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return NmcliRunnerResult(stdout="", returncode=0)
        return calls, runner

    def test_uplink_reads_ethernet_carrier_and_wifi_state(self):
        calls, runner = self._recording()
        net = Network(runner)
        net.uplink()
        joined = " ".join(" ".join(c) for c in calls)
        self.assertIn("nmcli", joined)
        self.assertIn("device", joined)

    def test_hotspot_is_started_open(self):
        """No password argument: the operator is not asked to guess one before
        they can get in (design D-6)."""
        calls, runner = self._recording()
        Network(runner).start_hotspot()
        argv = " ".join(calls[0])
        self.assertIn("hotspot", argv)
        self.assertNotIn("password", argv)

    def test_hotspot_can_be_stopped(self):
        calls, runner = self._recording()
        Network(runner).stop_hotspot()
        self.assertIn("down", " ".join(calls[0]))
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_mrrc_hotspot -v`
预期：FAIL — `NameError: name 'Network' is not defined`

- [ ] **步骤 3：编写最少实现代码**

```python
import ipaddress
import subprocess

HOTSPOT_CONNECTION = "mrrc-setup"
#: The subnet NetworkManager's shared mode hands out. The wizard's gate is
#: "is this request from inside the hotspot", so both sides need the same value.
HOTSPOT_CIDR = ipaddress.ip_network("10.42.0.0/24")


@dataclass(frozen=True)
class NmcliRunnerResult:
    stdout: str
    returncode: int


def _run(argv, **kwargs):  # pragma: no cover - replaced in every test
    """The only place a real process is spawned."""
    done = subprocess.run(argv, capture_output=True, text=True, **kwargs)
    return NmcliRunnerResult(stdout=done.stdout, returncode=done.returncode)


class Network:
    """NetworkManager, narrowed to the four things this service does."""

    def __init__(self, runner=_run):
        self._run = runner

    def uplink(self) -> Uplink:
        out = self._run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"]).stdout
        link = wifi = False
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) < 3:
                continue
            _dev, kind, state = parts[0], parts[1], parts[2]
            if kind == "ethernet" and state == "connected":
                link = True
            if kind == "wifi" and state == "connected":
                wifi = True
        return Uplink(ethernet_link=link, wifi_connected=wifi)

    def start_hotspot(self) -> None:
        """Open on purpose: the wizard, not a WiFi password, is the gate."""
        self._run([
            "nmcli", "device", "wifi", "hotspot",
            "con-name", HOTSPOT_CONNECTION,
            "ssid", "MRRC-Setup",
        ])

    def stop_hotspot(self) -> None:
        self._run(["nmcli", "connection", "down", HOTSPOT_CONNECTION])
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_mrrc_hotspot -v`
预期：PASS（7 例）

- [ ] **步骤 5：Commit**

```bash
git add linux/mrrc_hotspot.py tests/test_mrrc_hotspot.py
git commit -m "box: one place talks to NetworkManager, and it starts the AP open"
```

---

## 任务 3：循环与计时器

**文件：**
- 修改：`linux/mrrc_hotspot.py`
- 测试：`tests/test_mrrc_hotspot.py`

- [ ] **步骤 1：编写失败的测试**

```python
import unittest.mock


class TickTests(unittest.TestCase):
    """The whole decision, once per tick, driven by injected facts."""

    def _net(self, uplink, started=None):
        started = started if started is not None else []
        net = unittest.mock.MagicMock()
        net.uplink.return_value = uplink
        net.start_hotspot.side_effect = lambda: started.append("start")
        net.stop_hotspot.side_effect = lambda: started.append("stop")
        return net

    def test_starts_once_and_does_not_restart_every_tick(self):
        events = []
        svc = HotspotService(self._net(Uplink(False, False), events),
                             now=lambda: 0.0)
        svc.tick(); svc.tick(); svc.tick()
        self.assertEqual(events, ["start"], "a running AP must not be restarted each tick")

    def test_stops_when_an_uplink_appears(self):
        events = []
        states = [Uplink(False, False), Uplink(True, False)]
        net = self._net(None, events)
        net.uplink.side_effect = states
        svc = HotspotService(net, now=lambda: 0.0)
        svc.tick(); svc.tick()
        self.assertEqual(events, ["start", "stop"])

    def test_a_failed_start_is_not_reported_as_broadcasting(self):
        """AP mode is the one premise that could not be checked before the
        hardware arrived (R-1). If it fails, the box has to say so instead of
        claiming a hotspot nobody can see — that is the difference between a
        diagnosis and a mystery."""
        net = self._net(Uplink(False, False), [])
        net.start_hotspot.side_effect = RuntimeError("AP mode not supported")
        svc = HotspotService(net, now=lambda: 0.0)
        svc.tick()
        self.assertFalse(svc.broadcasting)
        self.assertIn("AP mode not supported", svc.status()["error"])

    def test_gives_up_after_the_window(self):
        events = []
        clock = [0.0]
        svc = HotspotService(self._net(Uplink(False, False), events),
                             now=lambda: clock[0], window_seconds=60)
        svc.tick()
        clock[0] = 61.0
        svc.tick()
        self.assertEqual(events, ["start", "stop"], "the window must close on its own")
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_mrrc_hotspot -v`
预期：FAIL — `NameError: name 'HotspotService' is not defined`

- [ ] **步骤 3：编写最少实现代码**

```python
DEFAULT_WINDOW_SECONDS = 30 * 60


class HotspotService:
    """One tick does one decision. Time is injected so the window is testable."""

    def __init__(self, net: Network, now=time.monotonic,
                 window_seconds: int = DEFAULT_WINDOW_SECONDS):
        self._net = net
        self._now = now
        self._window = window_seconds
        self._up = False
        self._deadline = 0.0
        self._error: str | None = None

    @property
    def broadcasting(self) -> bool:
        return self._up

    def tick(self) -> None:
        uplink = self._net.uplink()
        if self._up:
            if not needs_hotspot(uplink) or self._now() >= self._deadline:
                self._net.stop_hotspot()
                self._up = False
            return
        if not needs_hotspot(uplink):
            return
        try:
            self._net.start_hotspot()
        except Exception as exc:
            # Report rather than retry invisibly: the operator's next move is
            # the HDMI console, and they can only make it if the box says the
            # radio refused.
            self._error = str(exc)
            return
        self._error = None
        self._up = True
        self._deadline = self._now() + self._window

    def status(self) -> dict:
        """What /manage will show. Seconds left, not a timestamp."""
        return {
            "broadcasting": self._up,
            "error": self._error,
            "seconds_left": max(0, int(self._deadline - self._now())) if self._up else 0,
            "ssid": "MRRC-Setup" if self._up else None,
        }
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_mrrc_hotspot -v`
预期：PASS（12 例）

- [ ] **步骤 5：Commit**

```bash
git add linux/mrrc_hotspot.py tests/test_mrrc_hotspot.py
git commit -m "box: the window closes by itself, because the one that stays open is the risk"
```

---

## 任务 4：装成 systemd 服务

**文件：**
- 创建：`packaging/box/mrrc-hotspot.service`
- 修改：`packaging/box/box-overlay.sh`
- 修改：`tests/test_box_profiles.py`

- [ ] **步骤 1：编写失败的测试**

```python
    def test_hotspot_service_is_installed_and_long_running(self):
        """It has to outlive its own decision: /manage asks it for status, and a
        oneshot unit is gone by then (design D-7)."""
        unit = (REPO / "packaging" / "box" / "mrrc-hotspot.service").read_text(encoding="utf-8")
        self.assertIn("Type=simple", unit)
        self.assertIn("ExecStart=/usr/local/bin/mrrc-hotspot", unit)
        self.assertIn("Restart=on-failure", unit)

    def test_dnsmasq_is_installed_because_nm_needs_it_for_the_ap(self):
        overlay = (REPO / "packaging" / "box" / "box-overlay.sh").read_text(encoding="utf-8")
        self.assertIn("dnsmasq", overlay,
                      "NM's shared hotspot serves DHCP and DNS through dnsmasq")
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_box_profiles -v`
预期：FAIL — `FileNotFoundError: .../mrrc-hotspot.service`

- [ ] **步骤 3：编写实现**

`packaging/box/mrrc-hotspot.service`：

```ini
[Unit]
Description=MRRC Modern setup hotspot (open AP while there is no uplink)
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
ExecStart=/usr/local/bin/mrrc-hotspot serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`linux/mrrc_hotspot.py` 追加 CLI：

```python
def serve(interval: float = 5.0) -> None:
    service = HotspotService(Network())
    while True:
        try:
            service.tick()
        except Exception as exc:  # keep the loop alive: a dead service cannot recover
            print(f"[mrrc-hotspot] tick failed: {exc}", flush=True)
        time.sleep(interval)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mrrc-hotspot")
    parser.add_argument("command", choices=["serve", "status", "tick"])
    args = parser.parse_args(argv)
    if args.command == "status":
        print(json.dumps(HotspotService(Network()).status()))
    elif args.command == "tick":
        service = HotspotService(Network())
        service.tick()
        print(json.dumps(service.status()))
    else:
        serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`packaging/box/box-overlay.sh` 在两处改：

```bash
# ① apt 装机清单里加 dnsmasq（NM 的 shared 模式靠它发 DHCP/DNS）
apt-get install -y -qq --no-install-recommends \
  python3.11-venv python3-dev \
  portaudio19-dev libportaudio2 libasound2-dev \
  libopus0 libopus-dev dnsmasq
```

```bash
# ② 装服务与单元（放在 mrrc-radio 那一组旁边）
install -m 0755 "$MRRC_HOME/linux/mrrc_hotspot.py" /usr/local/bin/mrrc-hotspot
install -m 0644 "$REPO_SRC/packaging/box/mrrc-hotspot.service" \
  /etc/systemd/system/mrrc-hotspot.service
```

```bash
# ③ enable 那一行加上它
systemctl enable mrrc-firstboot.service mrrc-modern.service mrrc-hotspot.service
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_box_profiles tests.test_mrrc_hotspot -v`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add packaging/box/mrrc-hotspot.service packaging/box/box-overlay.sh \
        linux/mrrc_hotspot.py tests/test_box_profiles.py tests/test_mrrc_hotspot.py
git commit -m "box: the hotspot runs as a service, and dnsmasq comes with it"
```

---

## 任务 5：`/setup` 的免口令闸门

**文件：**
- 修改：`server.py`
- 测试：`tests/test_setup_wizard.py`

- [ ] **步骤 1：编写失败的测试**

```python
"""The gate is the network path, not a flag.

A flag can fail open: forget to write it, or write it late, and the box accepts
anonymous password changes forever. A subnet cannot fail open, because once the
hotspot is down nothing is on that subnet at all (design D-6).
"""
import unittest
from unittest import mock

from server import is_setup_request


class SetupGateTests(unittest.TestCase):
    def test_request_from_the_hotspot_subnet_is_allowed(self):
        self.assertTrue(is_setup_request("10.42.0.1"))

    def test_another_address_on_the_hotspot_subnet_is_allowed(self):
        self.assertTrue(is_setup_request("10.42.0.87"))

    def test_lan_address_is_not_allowed(self):
        self.assertFalse(is_setup_request("192.168.1.50"))

    def test_public_address_is_not_allowed(self):
        self.assertFalse(is_setup_request("203.0.113.7"))

    def test_unparseable_address_is_not_allowed(self):
        """Fail closed on anything unexpected."""
        self.assertFalse(is_setup_request(""))
        self.assertFalse(is_setup_request("not-an-address"))
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_setup_wizard -v`
预期：FAIL — `ImportError: cannot import name 'is_setup_request' from 'server'`

- [ ] **步骤 3：编写最少实现代码**

在 `server.py` 中，与其余小助手并列处：

```python
#: Imported, not redeclared: the hotspot decides what subnet it hands out and
#: the gate has to agree with it. Two literals here would be two things to keep
#: in step, and the failure mode is the gate quietly widening.
from linux.mrrc_hotspot import HOTSPOT_CIDR as SETUP_SUBNET


def is_setup_request(client_host: str) -> bool:
    """True when this request came over the setup hotspot."""
    try:
        return ipaddress.ip_address(client_host) in SETUP_SUBNET
    except ValueError:
        return False
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_setup_wizard -v`
预期：PASS（5 例）

- [ ] **步骤 5：Commit**

```bash
git add server.py tests/test_setup_wizard.py
git commit -m "server: the setup gate is a subnet, because a subnet cannot fail open"
```

---

## 任务 6：设口令（复用既有写入通道）

**文件：**
- 修改：`server.py`
- 测试：`tests/test_setup_wizard.py`

- [ ] **步骤 1：编写失败的测试**

```python
class SetupPasswordTests(unittest.TestCase):
    def test_password_is_written_through_the_existing_config_writer(self):
        """No new writer. The rule that seven writers became one is worth
        keeping; the wizard borrows the one server.py already has."""
        written = {}
        with mock.patch("server.cloud_hub._write_config",
                        side_effect=lambda path, updates: written.update(updates)):
            apply_setup({"password": "s3cret-and-long"})
        self.assertEqual(written["MRRC_WEB_PASSWORD"], "s3cret-and-long")

    def test_short_password_is_refused(self):
        with self.assertRaises(ValueError):
            apply_setup({"password": "short"})

    def test_empty_password_is_refused(self):
        """Empty is not 'no password': the server generates a random one when
        the value is empty, and the operator would never learn it."""
        with self.assertRaises(ValueError):
            apply_setup({"password": ""})
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_setup_wizard -v`
预期：FAIL — `NameError: name 'apply_setup' is not defined`

- [ ] **步骤 3：编写最少实现代码**

```python
MIN_SETUP_PASSWORD = 8


def apply_setup(payload: dict) -> None:
    """Take the two things the operator supplies and apply them.

    Deliberately narrow: this exists to end a deadlock, not to be a second
    settings UI. Everything else is reachable from the normal UI afterwards.
    """
    password = (payload.get("password") or "")
    if len(password) < MIN_SETUP_PASSWORD:
        raise ValueError("password must be at least 8 characters")
    cloud_hub._write_config(_config_file_path(), {"MRRC_WEB_PASSWORD": password})
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_setup_wizard -v`
预期：PASS（8 例）

- [ ] **步骤 5：Commit**

```bash
git add server.py tests/test_setup_wizard.py
git commit -m "server: the wizard writes the password the way everything else already does"
```

---

## 任务 7：连 Wi-Fi（含失败必须回得来）

**文件：**
- 修改：`server.py`
- 测试：`tests/test_setup_wizard.py`

- [ ] **步骤 1：编写失败的测试**

```python
class SetupWifiTests(unittest.TestCase):
    def test_successful_join_is_reported(self):
        runner = mock.MagicMock(
            return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        ok, detail = join_wifi("MyNet", "pw", runner=runner)
        self.assertTrue(ok)
        self.assertIn("MyNet", " ".join(runner.call_args[0][0]))

    def test_failed_join_returns_the_reason_instead_of_swallowing_it(self):
        """The negative case is the one that matters: a silent failure leaves
        the operator staring at a page that did nothing."""
        runner = mock.MagicMock(
            return_value=mock.Mock(returncode=1, stdout="", stderr="Secrets were required"))
        ok, detail = join_wifi("MyNet", "wrong", runner=runner)
        self.assertFalse(ok)
        self.assertIn("Secrets were required", detail)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_setup_wizard -v`
预期：FAIL — `NameError: name 'join_wifi' is not defined`

- [ ] **步骤 3：编写最少实现代码**

```python
def join_wifi(ssid: str, password: str, runner=subprocess.run):
    """Ask NetworkManager to join, and report what it said.

    Returns the reason rather than raising: a failure here is the operator's
    next message on the page, not a stack trace in a log they cannot read.
    """
    done = runner(
        ["nmcli", "device", "wifi", "connect", ssid, "password", password],
        capture_output=True, text=True,
    )
    if done.returncode == 0:
        return True, "connected"
    return False, (done.stderr or done.stdout or "nmcli failed").strip()
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_setup_wizard -v`
预期：PASS（10 例）

- [ ] **步骤 5：Commit**

```bash
git add server.py tests/test_setup_wizard.py
git commit -m "server: a failed join says why, because the page has nowhere else to put it"
```

---

## 任务 8：路由与页面

**文件：**
- 修改：`server.py`
- 创建：`static/setup.html`
- 测试：`tests/test_setup_wizard.py`

- [ ] **步骤 1：编写失败的测试**

```python
class SetupRouteTests(unittest.TestCase):
    def test_setup_page_is_served_to_the_hotspot(self):
        client = TestClient(app)
        resp = client.get("/setup", client=("10.42.0.5", 1234))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Wi-Fi", resp.text)

    def test_setup_page_is_refused_from_the_lan(self):
        client = TestClient(app)
        resp = client.get("/setup", client=("192.168.1.5", 1234))
        self.assertEqual(resp.status_code, 404)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_setup_wizard -v`
预期：FAIL — 404 / 200 不符

- [ ] **步骤 3：编写实现**

在 `server.py` 的 API 路由之后、SPA 兜底**之前**注册：

```python
def _hotspot_client(request: Request) -> str:
    return request.client.host if request.client else ""


def _require_hotspot(request: Request) -> None:
    if not is_setup_request(_hotspot_client(request)):
        raise HTTPException(status_code=404)


@app.get("/setup")
async def setup_page(request: Request):
    _require_hotspot(request)
    return FileResponse(os.path.join(STATIC_DIR, "setup.html"))


@app.get("/api/setup/wifi")
async def setup_wifi_list(request: Request):
    _require_hotspot(request)
    names = re.findall(r"^SSID:(.*)$", subprocess.run(
        ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list"],
        capture_output=True, text=True).stdout, re.M)
    return {"wifi": [{"ssid": n} for n in dict.fromkeys(names) if n]}


@app.post("/api/setup")
async def setup_apply(request: Request):
    _require_hotspot(request)
    payload = await request.json()
    try:
        apply_setup(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    ssid = payload.get("ssid")
    if ssid:
        ok, detail = join_wifi(ssid, payload.get("wifi_password", ""))
        if not ok:
            # 口令已经写进去了，但网络没配上：如实报错，让操作者改了重试，
            # 不要假装成功再把他踢到一个连不上的地址上。
            return {"connected": False, "detail": detail}
    return {"connected": True, "detail": "connected"}
```

`static/setup.html`（单页，两个字段即可 —— 其余设置配好网之后回主界面改）：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MRRC 盒子初始化</title>
  <style>
    body { font-family: -apple-system, "PingFang SC", sans-serif; background: #0d0d0d;
           color: #eee; margin: 0; padding: 2rem 1.25rem; line-height: 1.7; }
    .card { max-width: 460px; margin: 0 auto; background: #161616; border: 1px solid #2b2b2b;
            border-radius: 14px; padding: 1.75rem; }
    h1 { font-size: 1.35rem; margin: 0 0 .35rem; }
    p { color: #9a9a9a; font-size: .92rem; }
    label { display: block; margin: 1.1rem 0 .35rem; font-size: .9rem; color: #c8c8c8; }
    input, select { width: 100%; box-sizing: border-box; padding: .6rem .7rem; border-radius: 8px;
                    border: 1px solid #333; background: #0f0f0f; color: #eee; font-size: 1rem; }
    button { width: 100%; margin-top: 1.4rem; padding: .8rem; border: 0; border-radius: 9px;
             background: #f0a020; color: #1a1200; font-size: 1rem; font-weight: 600; }
    .msg { margin-top: 1rem; font-size: .9rem; }
    .err { color: #e8734a; }
  </style>
</head>
<body>
  <div class="card">
    <h1>MRRC 盒子初始化</h1>
    <p>你正连在盒子自己开的热点上。设一个登录口令、选你家 Wi-Fi，盒子就会连上去并关掉热点。</p>
    <form id="f">
      <label for="pw">设置登录口令（至少 8 位）</label>
      <input id="pw" type="password" autocomplete="new-password" required minlength="8">
      <label for="ssid">你家 Wi-Fi</label>
      <select id="ssid"></select>
      <label for="wpw">Wi-Fi 密码</label>
      <input id="wpw" type="password" autocomplete="current-password">
      <button type="submit">保存并连接</button>
    </form>
    <div class="msg" id="m"></div>
  </div>
  <script src="setup.js"></script>
</body>
</html>
```

`static/setup.js` 的同目录投放与提交逻辑（POST `/api/setup`，失败时**把 nmcli 的原话显示出来**，
不重定向，让操作者能改密码重试）：

```javascript
const $ = (id) => document.getElementById(id);
fetch("/api/setup/wifi").then(r => r.json()).then(list => {
  const sel = $("ssid");
  list.forEach(n => sel.add(new Option(n.ssid, n.ssid)));
});
$("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const resp = await fetch("/api/setup", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: $("pw").value, ssid: $("ssid").value, wifi_password: $("wpw").value }),
  });
  const body = await resp.json();
  $("m").className = "msg" + (resp.ok ? "" : " err");
  $("m").textContent = resp.ok ? "已保存。盒子正在连接你家 Wi-Fi，热点会关闭。" : (body.detail || "失败");
});
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_setup_wizard -v`
预期：PASS（12 例）

- [ ] **步骤 5：Commit**

```bash
git add server.py static/setup.html static/setup.js tests/test_setup_wizard.py
git commit -m "server: a setup page that only exists where the hotspot is"
```

---

## 任务 9：全量回归 + 计数 + 镜像

- [ ] **步骤 1：全套件**

运行：`$PY -m unittest discover -s tests`
预期：失败 **1 个**（既存的那个，见全局约束 1）。**总数以实测为准**（不要照抄本计划的数字）

- [ ] **步骤 2：同步三处计数**

`tests/README.md` 的总数与模块数、以及 `tests/test_spectrum_profile_docs.py` 里那两个断言
—— **用实测数字**填 `assertIn`，并把**当前**值加进 `assertNotIn`。

- [ ] **步骤 3：release_check**

运行：`$PY .agents/skills/dual-platform-release/harness/release_check.py`
预期：`30 ok, 0 failing`

- [ ] **步骤 4：Commit 并推送**

```bash
git add -u tests/ docs/
git commit -m "test: the suite count moves with the wizard's tests"
git push origin HEAD:main
```

- [ ] **步骤 5：重建镜像（**只有这一步需要 Docker**）**

```bash
packaging/box/build-image.sh          # 约 5–15 分钟，视网络
ls -l dist/w103d/MRRC-Modern-*-w103d.img.gz
```

---

## 验收（操作者视角，规格 §8.5）

- [ ] 刷完盘、**不插网线、不接键盘**
- [ ] 手机 Wi-Fi 列表里出现 **`MRRC-Setup`**（**无密码**）
- [ ] 连上后打开 `https://10.42.0.1:8888/setup`（自签证书警告 → 继续）
- [ ] **免口令**即可设口令 + 选自家 Wi-Fi
- [ ] 保存后盒子切到 STA、**热点消失**、`https://<盒子IP>:8888` 可用新口令登录
- [ ] 从局域网访问 `/setup` → **404**

## 边界（未验证，须真机）

- ⚠️ **MT7663S 的 AP 模式是否可用**（SDIO 变体）—— 这是本计划唯一无法在本机验证的前提（R-1）。
  若真机上 `nmcli device wifi hotspot` 失败，HDMI 控制台 + `nmtui` 仍是可用退路（指南 §9）
- 切 STA 时手机端的重连体验
- 热点网段与盒子其他接口的冲突
