# 支持诊断包与上报闭环 实现计划（支持链路 1/4）

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让操作员在浏览器点「🐞 遇到问题」就能产出一个**脱敏、自证**的诊断包（日志 + 配置快照 + 环境/状态快照 + 自动体检摘要），上传到 MRRC Modern 专属接收端；无网络时也能只保存到本地。为此补上服务端持久化日志。

**架构：** 新增纯标准库模块 `support_bundle.py`（脱敏/截尾/体检/打包，**不 import 任何应用代码**，将来可被热修覆盖）→ `server.py` 加 3 个 REST 端点（复用既有 `auth_middleware`）与 `MRRC_LOG_DIR` 轮转日志 → `static/support.html` 独立页 + SPA 菜单纯 `<a>` 入口 → `tools/support_receiver/server.py`（vendor）+ `deploy_support_receiver.sh` 部署独立实例（8098，存储 `/var/www/support-modern`）。

**技术栈：** Python 3 标准库（`zipfile`/`hashlib`/`re`/`threading`）、FastAPI/Starlette（既有）、`urllib.request`（上传，`asyncio.to_thread` 包裹）、`unittest`（既有测试风格：直接 import `server`、`_FakeRequest` 调端点、无 HTTP 客户端）。

**规格：** `docs/superpowers/specs/2026-09-17-support-bundle-design.md`（本计划实现其 §4–§13；§14 的风险与 §15 的非目标不变）。

---

## 工作约定（每个任务都适用）

- 测试运行：`cd /Users/cheenle/HAM/mrrc_modern && .venv/bin/python -m unittest discover -s tests`（全套 1103 项 ≈ 23 s；单模块：`.venv/bin/python -m unittest tests.test_support_bundle -v`）。
- **每次 commit 前**：`python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged` 必须 `clean`（退出码 0）。
- 提交信息用本仓既有风格（`feat(...)` / `test(...)` / `docs(...)`，短祈使句）。
- 测试基线：**改动前 1103 tests / 55 modules 全绿**（已验证）。每个任务结束时必须全绿。
- 两个前端文件 `static/ft710_main.js` / `static/ft710_ui.js` 受工具护栏保护：**本计划的任务都不修改它们**（新菜单项是纯 `<a>`，`ft710_ui.js:1793` 的选择器是 `.menu-item[data-action]`，不会拦截）。

---

## 文件结构（先锁定分解，再写任务）

**新建**

| 路径 | 职责 | 约 |
| --- | --- | --- |
| `support_bundle.py` | 脱敏、日志发现/截尾、体检摘要、版本/环境快照、打包与保留策略。纯标准库、无应用依赖 | 300 行 |
| `launcher_log.py` | 启动器侧 StartupTee：子进程 stdout/stderr → 有界文件 + 控制台回显，服务起来后停止 | 90 行 |
| `static/support.html` | 「🐞 遇到问题」页（问题/联系方式 + 3 个按钮 + 清单预览） | 150 行 |
| `tools/support_receiver/server.py` | vendor 的接收端（标准库 HTTP），`product` 元字段 + 8098 默认端口 | 270 行 |
| `tools/support_receiver/support-receiver-modern.service` | systemd unit | 15 行 |
| `deploy_support_receiver.sh` | 幂等部署（unit + 存储 + 口令 + nginx 路径） | 70 行 |
| `website/answers/index.html` | 答复页占位（无 JS） | 40 行 |
| `tests/test_support_bundle.py` | 纯函数全量 + 隐私负例 | 260 行 |
| `tests/test_support_api.py` | 3 端点 + 401 + 单飞 + 保留 + 降级 | 200 行 |
| `tests/test_support_receiver.py` | vendor 接收端不变量 + 部署脚本不变量 | 140 行 |
| `tests/test_support_frontend.py` | 菜单/页面/契约 | 60 行 |
| `tests/test_launcher_log.py` | StartupTee 行为 | 90 行 |

**修改**

| 路径 | 改动 |
| --- | --- |
| `server.py` | `MRRC_LOG_DIR` 常量 + `_setup_file_logging()` + `api_support_bundle/upload/save` + 快照函数 |
| `windows/launcher.py` | 设 `MRRC_LOG_DIR`；Popen 走 StartupTee；服务起来后 `tee.stop()` |
| `macos/launcher.py` | 同上（无回显需求，`echo=False` 亦可，保持 True 无害） |
| `packaging/pyinstaller/mrrc_modern_server.spec` | `hiddenimports` 加 `support_bundle` |
| `packaging/pyinstaller/mrrc_modern_launcher.spec` | `hiddenimports` 加 `launcher_log` |
| `packaging/macos/mrrc_modern_launcher.spec`（若存在同名 spec 也照做） | 同上 |
| `install.sh` | systemd 重定向改 `logs/server-stdout.log` |
| `packaging/windows/build.ps1`、`packaging/macos/build.sh` | 写 `version.txt`（取 CHANGELOG 顶版本） |
| `static/index.html` | 菜单加一行纯 `<a>` |
| `website/deploy.sh` | REQUIRED_FILES 加 `answers/index.html` |
| `website/guide.html`、`website/zh/guide.html` | 「遇到问题怎么报」小节（中英成对） |
| `SDD/05`、`SDD/08`、`SDD/10`、`SDD/12`、`SDD/13`、`SDD/14` | 见任务 13 |
| `README.md`、`AGENTS.md`、`docs/PROJECT_MAP.md`、`tests/README.md`、`CHANGELOG.md` | 见任务 13 |
| `.agents/skills/sdd-guardian/harness/constraints.json` | 隐私不变量守卫 |
| `.agents/skills/dual-platform-release/SKILL.md` + `mac_pack.md`/`win_pack.md`/`pi_pack.md` | 产物抽查加 `version.txt` |

---

## 任务 1：`support_bundle.py` — 脱敏核心（白名单 + 值清洗 + 禁止路径）

**文件：**

- 创建：`support_bundle.py`
- 创建：`tests/test_support_bundle.py`

- [ ] **步骤 1：写失败的测试**

```python
"""Support bundle core (spec 2026-09-17 §4/§6): redaction is the security boundary."""
import unittest

import support_bundle as sb


class RedactEnvTextTests(unittest.TestCase):
    def test_password_key_is_dropped_by_omission(self):
        text, dropped, hits = sb.redact_env_text(
            "MRRC_WEB_PASSWORD=hunter2\nMRRC_WEB_PORT=8888\n")
        self.assertNotIn("hunter2", text)
        self.assertNotIn("MRRC_WEB_PASSWORD", text)
        self.assertIn("MRRC_WEB_PORT=8888", text)
        self.assertEqual(dropped, 1)
        self.assertEqual(hits, 0)

    def test_ssl_key_path_is_dropped(self):
        text, dropped, _ = sb.redact_env_text("MRRC_SSL_KEY=/x/privkey.pem\n")
        self.assertNotIn("privkey.pem", text)
        self.assertEqual(dropped, 1)

    def test_allowlisted_diagnostic_keys_survive(self):
        text, dropped, _ = sb.redact_env_text(
            "MRRC_RADIO_MODEL=ft710\nMRRC_SERIAL_PORT=/dev/cu.usbserial-0121DB3A0\n"
            "MRRC_BAUD_RATE=38400\nMRRC_AUDIO_RX_DEVICE=USB Audio\n")
        self.assertEqual(dropped, 0)
        self.assertIn("MRRC_SERIAL_PORT=/dev/cu.usbserial-0121DB3A0", text)
        self.assertIn("MRRC_AUDIO_RX_DEVICE=USB Audio", text)

    def test_comments_and_blank_lines_are_kept(self):
        text, dropped, _ = sb.redact_env_text("# radio\n\nMRRC_WEB_PORT=8888\n")
        self.assertIn("# radio", text)
        self.assertEqual(dropped, 0)

    def test_value_pass_cleans_a_secret_pasted_into_a_comment(self):
        text, _, hits = sb.redact_env_text("# old password=hunter2\n")
        self.assertNotIn("hunter2", text)
        self.assertEqual(hits, 1)


class RedactTextTests(unittest.TestCase):
    def test_replaces_values_and_counts(self):
        cleaned, hits = sb.redact_text(
            'GET /login?token=abc123&x=1\nMRRC_WEB_PASSWORD=x\n')
        self.assertNotIn("abc123", cleaned)
        self.assertEqual(hits, 2)

    def test_plain_text_untouched(self):
        cleaned, hits = sb.redact_text("RX open failed (-9996) — no device\n")
        self.assertEqual(hits, 0)
        self.assertIn("-9996", cleaned)


class CollectableTests(unittest.TestCase):
    def test_user_data_and_keys_are_refused(self):
        for path in ("recordings/15515kHz_20260912_222653.mp3",
                     "certs/server.key", "state/config.pem",
                     "mem_channels.json", "atr1000_tuner.json"):
            self.assertFalse(sb.is_collectable(path), path)

    def test_logs_and_diagnostics_are_allowed(self):
        for path in ("logs/server.log", "logs/server-stdout.log",
                     "logs/launcher.log", "diagnostics/env.json"):
            self.assertTrue(sb.is_collectable(path), path)

    def test_windows_separators_are_normalised(self):
        self.assertFalse(sb.is_collectable(r"C:\Users\x\certs\server.key"))
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv/bin/python -m unittest tests.test_support_bundle -v`
预期：`ModuleNotFoundError: No module named 'support_bundle'`

- [ ] **步骤 3：写最小实现**

```python
"""Support diagnostics bundle: collect, redact, summarise, package.

Spec: docs/superpowers/specs/2026-09-17-support-bundle-design.md (§4–§6).
Stdlib only and no application imports on purpose: the module stays unit
testable in isolation and can be hot-fixed later without a release.
"""
from __future__ import annotations

import os
import re

REDACTED = "<redacted>"

# Two independent passes (spec §6).  The key allow-list is the security
# boundary; the value pass only cleans text that is collected anyway (a
# password pasted into a log line, a `?token=` inside a URL).
SECRET_VALUE_RE = re.compile(
    r"(?i)\b(cookie[_-]?secret|pass(?:word|wd)?|secret|token|api[_-]?key|credential)\b"
    r"(\s*[=:]\s*)(\S+)"
)

CONFIG_KEY_ALLOWLIST = frozenset({
    # Radio / serial
    "MRRC_RADIO_MODEL", "MRRC_SERIAL_PORT", "MRRC_BAUD_RATE",
    # Web
    "MRRC_WEB_HOST", "MRRC_WEB_PORT",
    # Spectrum
    "MRRC_SCOPE_PORT", "MRRC_SCOPE_BAUD", "MRRC_FTDI_LIB_DIR",
    # Audio / recording
    "MRRC_AUDIO_RX_DEVICE", "MRRC_AUDIO_TX_DEVICE",
    "MRRC_RECORDINGS_BITRATE", "MRRC_RECORDINGS_MAX_SESSION_MIN", "MRRC_CQ_FILE",
    # Tuner / TLS mode / model safety
    "MRRC_ATR1000_HOST", "MRRC_ATR1000_PORT",
    "MRRC_SSL_CERT", "MRRC_SSL", "MRRC_ALLOW_UNVERIFIED_TX",
})

# Path fragments that never enter a bundle (spec §6).
FORBIDDEN_SUBSTRINGS = (
    "certs", "cert", ".pem", ".key", ".crt", ".p12",
    "recordings", "mem_channels.json", "atr1000_tuner.json",
)


def redact_text(text: str) -> tuple[str, int]:
    """Replace secret-looking values; return (new_text, hits)."""
    return SECRET_VALUE_RE.subn(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text or "")


def redact_env_text(text: str) -> tuple[str, int, int]:
    """Allow-list an env file; return (new_text, dropped_keys, value_hits)."""
    out: list[str] = []
    dropped = hits = 0
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            cleaned, n = redact_text(line)
            hits += n
            out.append(cleaned)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key not in CONFIG_KEY_ALLOWLIST:
            dropped += 1
            continue
        cleaned, n = redact_text(line)
        hits += n
        out.append(cleaned)
    return "\n".join(out) + "\n", dropped, hits


def is_collectable(relative_path: str) -> bool:
    """Whether a path may enter the bundle (spec §6)."""
    lowered = str(relative_path).lower().replace("\\", "/")
    return not any(token in lowered for token in FORBIDDEN_SUBSTRINGS)
```

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv/bin/python -m unittest tests.test_support_bundle -v`
预期：`Ran 12 tests ... OK`

- [ ] **步骤 5：Commit**

```bash
git add support_bundle.py tests/test_support_bundle.py
git commit -m "feat(support): bundle redaction core (key allow-list + secret value pass)"
```

---

## 任务 2：日志发现与截尾（多路径 + 行对齐 + 新鲜度）

**文件：**

- 修改：`support_bundle.py`（追加常量与两个函数）
- 修改：`tests/test_support_bundle.py`（追加测试类）

- [ ] **步骤 1：写失败的测试**

```python
class TailLinesTests(unittest.TestCase):
    def test_small_file_is_read_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            path.write_text("line1\nline2\n", encoding="utf-8")
            self.assertEqual(sb.tail_lines(str(path)), "line1\nline2\n")

    def test_tail_is_byte_bounded_and_line_aligned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            path.write_text("".join(f"line{i:05d}\n" for i in range(1000)), encoding="utf-8")
            text = sb.tail_lines(str(path), max_bytes=100)
            self.assertLessEqual(len(text.encode()), 100)
            self.assertTrue(text.startswith("line"), text[:20])
            self.assertTrue(text.endswith("\n"))

    def test_missing_file_is_empty_not_an_exception(self):
        self.assertEqual(sb.tail_lines("/nonexistent/server.log"), "")

    def test_invalid_utf8_is_replaced_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            path.write_bytes(b"ok\n\xff\xfe bad\n")
            self.assertIn("ok", sb.tail_lines(str(path)))


class ResolveLogFilesTests(unittest.TestCase):
    def test_finds_server_stdout_and_launcher_across_two_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "logs").mkdir()
            (data / "logs" / "server.log").write_text("s", encoding="utf-8")
            (data / "logs" / "server-stdout.log").write_text("o", encoding="utf-8")
            (data / "logs" / "server.log.1").write_text("p", encoding="utf-8")
            (data / "launcher.log").write_text("l", encoding="utf-8")
            found = sb.resolve_log_files(data / "logs", data)
        self.assertEqual(Path(found["server"]).name, "server.log")
        self.assertEqual(Path(found["server-prev"]).name, "server.log.1")
        self.assertEqual(Path(found["stdout"]).name, "server-stdout.log")
        self.assertEqual(Path(found["launcher"]).name, "launcher.log")

    def test_only_existing_files_are_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sb.resolve_log_files(Path(tmp) / "logs", Path(tmp)), {})

    def test_source_mode_legacy_name_is_picked_up_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp)
            (install / "logs").mkdir()
            (install / "logs" / "ft710-server.log").write_text("x", encoding="utf-8")
            found = sb.resolve_log_files(install / "logs", "", install)
        self.assertEqual(Path(found["legacy"]).name, "ft710-server.log")
        self.assertEqual(len(set(found.values())), len(found))
```

补充 `tests/test_support_bundle.py` 顶部导入（任务 1 只 import 了 `unittest`）：

```python
import tempfile
import unittest
from pathlib import Path

import support_bundle as sb
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv/bin/python -m unittest tests.test_support_bundle -v`
预期：`AttributeError: module 'support_bundle' has no attribute 'tail_lines'`

- [ ] **步骤 3：写最小实现（在 `redact_env_text` 之后、`is_collectable` 之前插入）**

```python
# ── log discovery and bounded tails (spec §5/§6) ────────────────────────────
DEFAULT_TAIL_BYTES = 2 * 1024 * 1024


def tail_lines(path: str, max_bytes: int = DEFAULT_TAIL_BYTES) -> str:
    """Read at most `max_bytes` from the end of a file, on a line boundary."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()                      # drop the half line
            data = fh.read()
    except OSError:
        return ""
    return data.decode("utf-8", "replace")


def resolve_log_files(log_dir, data_dir="", install_dir="") -> dict:
    """Map bundle role -> existing log file (spec §5).

    Roles: server / server-prev / stdout / stdout-prev / launcher / legacy.
    The desktop launcher puts logs under the user data dir; systemd and
    `start.sh` write into the install dir, which is also where a hand-started
    server writes when MRRC_LOG_DIR is unset.
    """
    from pathlib import Path

    log_dir = Path(log_dir)
    candidates = [
        ("server", log_dir / "server.log"),
        ("server-prev", log_dir / "server.log.1"),
        ("stdout", log_dir / "server-stdout.log"),
        ("stdout-prev", log_dir / "server-stdout.log.1"),
    ]
    if data_dir:
        candidates.append(("launcher", Path(data_dir) / "launcher.log"))
    if install_dir:
        candidates.append(("legacy", Path(install_dir) / "logs" / "ft710-server.log"))
        candidates.append(("legacy-server", Path(install_dir) / "logs" / "server.log"))

    found: dict = {}
    seen: set = set()
    for role, path in candidates:
        try:
            key = os.path.realpath(path)
        except OSError:
            continue
        if key in seen or not os.path.isfile(path):
            continue
        seen.add(key)
        found[role] = str(path)
    return found
```

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv/bin/python -m unittest tests.test_support_bundle -v`
预期：`Ran 20 tests ... OK`

- [ ] **步骤 5：Commit**

```bash
git add support_bundle.py tests/test_support_bundle.py
git commit -m "feat(support): bounded log tails and multi-path log discovery"
```

---

## 任务 3：自动体检摘要 `summarize_log`（匹配本仓真实日志词汇）

**证据来源（本仓实际打印的行，不是照抄兄弟仓）：** `server.py:2215` `Server ready!`、
`server.py:2121` `Could not connect to radio`、`audio_handler.py:341` `Configured audio device '%s' not found`、
`audio_handler.py:431` `No audio input device found`、`audio_handler.py:44` `PyAudio not available`、
`audio_handler.py:462/533/510` `RX open failed` / `RX stream lost` / `RX audio restart failed`、
`server.py:361/365/390/265` `Recording writer is falling behind` / `Recording dropped` /
`Recording queue full` / `Recording disabled`、`backends/ft710/scope_producer.py:170/194` `S-meter fallback`、
`backends/*/backend.py` `enable TX after`（TX 门禁）。本仓**没有** `🎧 音频健康` / `IOLoop stall` 行。

**文件：**

- 修改：`support_bundle.py`
- 修改：`tests/test_support_bundle.py`

- [ ] **步骤 1：写失败的测试**

```python
class SummarizeLogTests(unittest.TestCase):
    def test_startups_without_tracebacks_are_not_a_crash(self):
        log = ("2026-09-17 07:00:00 [INFO] mrrc: Server ready!\n"
               "2026-09-17 07:05:00 [INFO] mrrc: Server ready!\n")
        out = sb.summarize_log(log, freshness_hours=0.2)
        self.assertIn("启动次数：2 次", out)
        self.assertIn("无崩溃痕迹", out)
        self.assertIn("数据新鲜度：日志写于 12 分钟前", out)

    def test_traceback_changes_the_verdict(self):
        log = ("2026-09-17 07:00:00 [INFO] mrrc: Server ready!\n"
               "Traceback (most recent call last):\n")
        out = sb.summarize_log(log)
        self.assertIn("Traceback", out)
        self.assertIn("按崩溃排查", out)

    def test_stale_bundle_is_flagged_as_not_the_scene(self):
        out = sb.summarize_log("2026-09-14 07:00:00 [INFO] mrrc: Server ready!\n",
                              freshness_hours=72.0)
        self.assertIn("可能不是本次故障现场", out)

    def test_no_logs_at_all_is_stated_not_omitted(self):
        out = sb.summarize_log("")
        self.assertIn("数据新鲜度：无日志文件", out)
        self.assertIn("启动次数：日志中未见", out)

    def test_audio_and_serial_and_scope_facts_are_reported(self):
        log = ("Configured audio device 'X' not found\n"
               "RX open failed (-9996) — re-initializing PortAudio\n"
               "[Errno 6] Device not configured\n"
               "scope_pipe worker not found — spectrum will use S-meter fallback only\n"
               "Recording writer is falling behind — dropping audio (the encoder is slower)\n")
        out = sb.summarize_log(log)
        self.assertIn("音频设备", out)
        self.assertIn("-9996", out)
        self.assertIn("串口掉线", out)
        self.assertIn("S 表合成", out)
        self.assertIn("录音写入", out)

    def test_unverified_tx_gate_is_reported(self):
        out = sb.summarize_log("Transmit disabled: set MRRC_ALLOW_UNVERIFIED_TX=1 and restart "
                               "to enable TX after checking\n")
        self.assertIn("TX 门禁", out)

    def test_each_class_shows_at_most_three_recent_lines(self):
        log = "".join(f"Configured audio device 'D{i}' not found\n" for i in range(9))
        out = sb.summarize_log(log)
        self.assertIn("音频设备：9 条", out)
        self.assertEqual(out.count("      - Configured audio device"), 3)
        self.assertIn("D8", out)                   # newest kept
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv/bin/python -m unittest tests.test_support_bundle -v`
预期：`AttributeError: module 'support_bundle' has no attribute 'summarize_log'`

- [ ] **步骤 3：写最小实现（追加到文件末尾）**

```python
# ── automatic triage summary (spec §6; patterns verified against this repo) ──
STALE_LOG_HOURS = 24.0

SUMMARY_PATTERNS = (
    ("Traceback", re.compile(r"Traceback \(most recent call last\)")),
    ("ERROR", re.compile(r"\bERROR\b")),
    ("音频设备", re.compile(r"Configured audio device|No audio input device found|"
                          r"PyAudio not available")),
    ("音频恢复", re.compile(r"RX open failed|RX stream lost|RX audio restart failed")),
    ("串口掉线", re.compile(r"Device not configured|Errno 6")),
    ("频谱", re.compile(r"S-meter fallback|scope_pipe: ")),
    ("录音写入", re.compile(r"Recording writer is falling behind|Recording dropped|"
                          r"Recording queue full|Recording disabled")),
    ("TX 门禁", re.compile(r"enable TX after")),
)

PORT_AUDIO_CODE_RE = re.compile(r"-[0-9]{4}\b")


def summarize_log(text: str, freshness_hours=None) -> str:
    """Triage conclusions + per-class hit lines (the maintainer reads this first)."""
    lines = (text or "").splitlines()
    conclusions: list[str] = []

    if not lines:
        conclusions.append("数据新鲜度：无日志文件（未找到 server.log / server-stdout.log / launcher.log）")
    elif freshness_hours is None:
        conclusions.append("数据新鲜度：未记录")
    elif freshness_hours > STALE_LOG_HOURS:
        conclusions.append(f"数据新鲜度：最新日志约 {freshness_hours / 24:.1f} 天前"
                           "（>24h，可能不是本次故障现场，下列结论仅供参考）")
    elif freshness_hours >= 1:
        conclusions.append(f"数据新鲜度：日志写于 {freshness_hours:.1f} 小时前")
    else:
        conclusions.append(f"数据新鲜度：日志写于 {max(1, int(freshness_hours * 60))} 分钟前")

    for label, pattern in (("音频设备", SUMMARY_PATTERNS[2][1]), ("录音写入", SUMMARY_PATTERNS[6][1])):
        hits = [ln for ln in lines if pattern.search(ln)]
        if hits:
            conclusions.append(f"{label}：{len(hits)} 条命中（见下方明细）")

    codes = sorted({c for c in PORT_AUDIO_CODE_RE.findall(text or "")})
    if "-9996" in (text or "") or "no default output device" in (text or ""):
        conclusions.append("音频设备：PortAudio 报 -9996（找不到可用设备）"
                           f"{'（日志内音频错误码：' + ', '.join(codes[:5]) + '）' if codes else ''}"
                           " —— 若 env.json 的 audio.devices 为空，说明本机没有音频设备"
                           "（虚拟机常见），纯 Web 模式属预期")

    if re.search(r"Device not configured|Errno 6", text or ""):
        conclusions.append("串口恢复：出现 ENXIO/「Device not configured」—— USB 串口桥掉线后重枚举，"
                           "检查电缆/供电/勿用无源 HUB")

    if "S-meter fallback" in (text or ""):
        conclusions.append("频谱：出现 S 表合成回退 —— 真实频谱（FT4222 / CI-V 0x27）未工作")

    if "enable TX after" in (text or ""):
        conclusions.append("TX 门禁：未验证机型拒绝发射（MRRC_ALLOW_UNVERIFIED_TX=1 才可开）—— 属预期行为")

    startups = sum(1 for ln in lines if "Server ready!" in ln)
    crashes = sum(1 for ln in lines if "Traceback (most recent call last)" in ln)
    if startups:
        span = ""
        stamps = [ln[:19] for ln in lines if re.match(r"\d{4}-\d{2}-\d{2}", ln)]
        if len(stamps) >= 2:
            span = f"，时间跨度 {stamps[0]} → {stamps[-1]}"
        verdict = ("⚠️ 同时有 Traceback，按崩溃排查" if crashes
                   else "无崩溃痕迹（升级/重启属正常行为）")
        conclusions.append(f"启动次数：{startups} 次{span} —— {verdict}")
    else:
        conclusions.append("启动次数：日志中未见 —— 日志可能被截断，或服务从未成功启动")

    parts: list[str] = []
    for label, pattern in SUMMARY_PATTERNS:
        hits = [ln.strip()[:300] for ln in lines if pattern.search(ln)]
        if not hits:
            continue
        parts.append(f"== {label}：{len(hits)} 条 ==")
        parts.extend(f"      - {h}" for h in hits[-3:])
        parts.append("")

    head = ["=== 自动体检结论 ==="] + [f"  * {c}" for c in conclusions] + ["", "=== 命中明细 ==="]
    return "\n".join(head + parts + [""])
```

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv/bin/python -m unittest tests.test_support_bundle -v`
预期：`Ran 27 tests ... OK`

- [ ] **步骤 5：Commit**

```bash
git add support_bundle.py tests/test_support_bundle.py
git commit -m "feat(support): triage summary with this repo's real log vocabulary"
```

---

## 任务 4：版本探测与环境快照

**文件：**

- 修改：`support_bundle.py`
- 修改：`tests/test_support_bundle.py`

- [ ] **步骤 1：写失败的测试**

```python
class VersionTests(unittest.TestCase):
    def test_version_txt_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "version.txt").write_text("1.17.0\n", encoding="utf-8")
            self.assertEqual(sb.detect_version(Path(tmp)), "1.17.0")

    def test_falls_back_to_changelog_then_iss(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CHANGELOG.md").write_text("# Changelog\n\n## [v1.17.0] — 2026-09-13\n",
                                              encoding="utf-8")
            self.assertEqual(sb.detect_version(root), "1.17.0")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "packaging" / "windows").mkdir(parents=True)
            (root / "packaging" / "windows" / "MRRC-Modern.iss").write_text(
                '#define MyAppVersion "1.16.0"\n', encoding="utf-8")
            self.assertEqual(sb.detect_version(root), "1.16.0")

    def test_unknown_when_nothing_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sb.detect_version(Path(tmp)), "unknown")


class EnvSnapshotTests(unittest.TestCase):
    def test_carries_version_platform_and_caller_extras(self):
        snap = sb.collect_env_snapshot("1.17.0", extra={"backend": "ft710"})
        self.assertEqual(snap["version"], "1.17.0")
        self.assertEqual(snap["backend"], "ft710")
        self.assertIn("platform", snap)
        self.assertIn("python", snap)
        self.assertIn("frozen", snap)
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv/bin/python -m unittest tests.test_support_bundle -v`
预期：`AttributeError: module 'support_bundle' has no attribute 'detect_version'`

- [ ] **步骤 3：写最小实现（追加到文件末尾）**

```python
# ── version + environment snapshot (spec §6; version authority for sub-project 3) ──
_ISS_VERSION_RE = re.compile(r'MyAppVersion\s+"([^"]+)"')
_CHANGELOG_VERSION_RE = re.compile(r"^##\s*\[?v?([0-9]+\.[0-9]+\.[0-9]+)", re.M)
VERSION_UNKNOWN = "unknown"


def detect_version(runtime_dir, resource_dir="") -> str:
    """version.txt (build product) -> MRRC-Modern.iss -> CHANGELOG.md -> unknown."""
    for directory in (runtime_dir, resource_dir):
        if not directory:
            continue
        try:
            text = (Path(directory) / "version.txt").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    if runtime_dir:
        try:
            text = (Path(runtime_dir) / "packaging" / "windows" / "MRRC-Modern.iss").read_text(
                encoding="utf-8", errors="replace")
            match = _ISS_VERSION_RE.search(text)
            if match:
                return match.group(1)
        except OSError:
            pass
        try:
            text = (Path(runtime_dir) / "CHANGELOG.md").read_text(
                encoding="utf-8", errors="replace")
            match = _CHANGELOG_VERSION_RE.search(text)
            if match:
                return match.group(1)
        except OSError:
            pass
    return VERSION_UNKNOWN


def collect_env_snapshot(version: str = "", extra=None) -> dict:
    """Version / platform / interpreter facts plus whatever the caller adds."""
    import platform
    import sys

    snapshot = {
        "version": version or VERSION_UNKNOWN,
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpuCount": os.cpu_count(),
    }
    snapshot.update(extra or {})
    return snapshot
```

把 `from pathlib import Path` 从函数内移到模块顶部（`resolve_log_files` 内已有局部 import，一并清理）：

```python
from pathlib import Path
```

并删除 `resolve_log_files` 里的 `from pathlib import Path` 局部导入行。

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv/bin/python -m unittest tests.test_support_bundle -v`
预期：`Ran 32 tests ... OK`

- [ ] **步骤 5：Commit**

```bash
git add support_bundle.py tests/test_support_bundle.py
git commit -m "feat(support): version detection (version.txt/iss/CHANGELOG) and env snapshot"
```

---

## 任务 5：打包 `build_bundle`、保留策略与 id 校验

**文件：**

- 修改：`support_bundle.py`
- 修改：`tests/test_support_bundle.py`

- [ ] **步骤 1：写失败的测试**

```python
class BuildBundleTests(unittest.TestCase):
    def _build(self, tmp, **kwargs):
        logs = Path(tmp) / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "server.log").write_text(
            "2026-09-17 07:00:00 [INFO] mrrc: Server ready!\nMRRC_WEB_PASSWORD=hunter2\n",
            encoding="utf-8")
        return sb.build_bundle(
            Path(tmp) / "out",
            problem="接收有杂音",
            contact="BH1XXX",
            env={"version": "1.17.0"},
            log_files={"server": str(logs / "server.log")},
            config_text="MRRC_WEB_PASSWORD=hunter2\nMRRC_WEB_PORT=8888\n",
        )

    def test_bundle_contains_the_expected_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._build(tmp)
            with zipfile.ZipFile(result["path"]) as archive:
                names = set(archive.namelist())
        for expected in ("problem.txt", "README.txt", "manifest.json", "logs/server.log",
                         "state/config-redacted.env", "diagnostics/summary.txt",
                         "diagnostics/env.json"):
            self.assertIn(expected, names)

    def test_no_secret_anywhere_in_the_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._build(tmp)
            with zipfile.ZipFile(result["path"]) as archive:
                blob = b"".join(archive.read(n) for n in archive.namelist())
        self.assertNotIn(b"hunter2", blob)
        self.assertGreater(result["redactions"], 0)

    def test_manifest_hashes_every_collected_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._build(tmp)
            with zipfile.ZipFile(result["path"]) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                for name, digest in manifest["sha256"].items():
                    self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), digest)

    def test_never_collects_forbidden_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = sb.build_bundle(
                Path(tmp) / "out", log_files={"recordings": str(Path(tmp) / "qso.mp3")},
                extra_files={"certs/server.key": b"PRIVATE"})
            with zipfile.ZipFile(result["path"]) as archive:
                names = " ".join(archive.namelist())
        self.assertNotIn("qso.mp3", names)
        self.assertNotIn("server.key", names)
        self.assertTrue(any("受限" in w for w in result["warnings"]), result["warnings"])

    def test_degradation_shrinks_the_tail_before_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "logs" / "server.log"
            big.parent.mkdir(parents=True)
            big.write_text("x" * 400 + "\n" * 1, encoding="utf-8")
            result = sb.build_bundle(Path(tmp) / "out",
                                    log_files={"server": str(big)},
                                    max_total_bytes=200)
            with zipfile.ZipFile(result["path"]) as archive:
                collected = archive.read("logs/server.log")
        self.assertLessEqual(len(collected), 400)
        self.assertIn("已降级", " ".join(result["warnings"]))

    def test_id_is_validated_before_any_path_is_built(self):
        self.assertRegex(sb.new_bundle_id(), sb.BUNDLE_ID_RE)
        self.assertTrue(sb.is_valid_bundle_id("20260917-072530-ab12"))
        self.assertFalse(sb.is_valid_bundle_id("../../etc/passwd"))
        self.assertFalse(sb.is_valid_bundle_id("20260917-072530-ZZZZ"))

    def test_prune_keeps_the_newest_and_ignores_foreign_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            names = [f"support-20260917-0725{ i:02d}-ab12.zip" for i in range(7)]
            for name in names:
                (out / name).write_bytes(b"z")
            (out / "keep-me.txt").write_text("x", encoding="utf-8")
            sb.prune_bundles(out, keep=5)
            remaining = sorted(p.name for p in out.glob("support-*.zip"))
        self.assertEqual(len(remaining), 5)
        self.assertEqual(remaining[0], names[2])
        self.assertTrue((out / "keep-me.txt").exists())
```

顶部导入补 `hashlib`、`json`、`zipfile`：

```python
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv/bin/python -m unittest tests.test_support_bundle -v`
预期：`AttributeError: module 'support_bundle' has no attribute 'build_bundle'`

- [ ] **步骤 3：写最小实现（追加到文件末尾）**

```python
# ── packaging + retention (spec §6/§7) ──────────────────────────────────────
BUNDLE_ID_RE = r"^\d{8}-\d{6}-[0-9a-f]{4}$"
DEFAULT_MAX_TOTAL_BYTES = 20 * 1024 * 1024
_TAIL_LADDER = (DEFAULT_TAIL_BYTES, 512 * 1024, 128 * 1024)

README_TEXT = (
    "本包由 MRRC Modern「🐞 遇到问题」生成。\n\n"
    "包含：日志尾部、脱敏配置快照、环境/电台/音频状态快照、自动体检摘要\n"
    "      （先看 diagnostics/summary.txt）。\n"
    "不含：登录密码、证书私钥、任何令牌，也不含录音、记忆频道与天调学习值\n"
    "      （生成时按白名单裁剪 + 值替换，替换次数写在 manifest.json）。\n"
)


def new_bundle_id() -> str:
    """`YYYYmmdd-HHMMSS-xxxx` — same shape as the receiver's own id validation."""
    import secrets
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def is_valid_bundle_id(bundle_id: str) -> bool:
    return bool(re.fullmatch(BUNDLE_ID_RE, str(bundle_id or "")))


def bundle_path(out_dir, bundle_id: str) -> "Path":
    """Absolute path of a built bundle, or an empty Path when the id is invalid."""
    if not is_valid_bundle_id(bundle_id):
        return Path("")
    return Path(out_dir) / f"support-{bundle_id}.zip"


def prune_bundles(out_dir, keep: int = 5) -> list:
    """Delete older support-*.zip files; never touches anything else."""
    out = Path(out_dir)
    if not out.is_dir():
        return []
    zips = sorted((p for p in out.glob("support-*.zip")), key=lambda p: p.name)
    removed: list = []
    for path in zips[:-keep] if keep > 0 else zips:
        try:
            path.unlink()
            removed.append(str(path))
        except OSError:
            pass
    return removed


def build_bundle(out_dir, *, problem: str = "", contact: str = "", env=None,
                 log_files=None, config_text: str = "", extra_files=None,
                 manifest_extra=None, initial_warnings=None,
                 max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES) -> dict:
    """Write one support zip and return {id, path, size, files, redactions, warnings}.

    Per-file problems become warnings, never failures: a bundle with only a
    manifest still beats "collect failed" (spec §11 minimal-bundle degradation).
    """
    import zipfile

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    bundle_id = new_bundle_id()
    zip_path = bundle_path(out, bundle_id)
    warnings: list = list(initial_warnings or [])
    redactions = 0
    collected: list = []
    hashes: dict = {}
    newest_mtime = None

    config_clean, dropped, cfg_hits = redact_env_text(config_text or "")
    redactions += dropped + cfg_hits

    # Pick the largest tail rung that keeps the archive under the cap.  The cap
    # exists because the receiver refuses >20 MB (spec §7) — we degrade here
    # instead of letting an upload fail on a huge log.
    payloads: dict = {}
    for rung in _TAIL_LADDER:
        payloads.clear()
        total = 0
        for role, path in (log_files or {}).items():
            if not is_collectable(role):
                continue
            text = tail_lines(path, max_bytes=rung)
            if not text:
                continue
            cleaned, hits = redact_text(text)
            payloads[role] = cleaned
            total += len(cleaned.encode("utf-8"))
            try:
                mtime = os.path.getmtime(path)
                newest_mtime = mtime if newest_mtime is None else max(newest_mtime, mtime)
            except OSError:
                pass
        if total + 4096 <= max_total_bytes or rung == _TAIL_LADDER[-1]:
            if rung != _TAIL_LADDER[0]:
                warnings.append(f"日志过大，已降级到每文件 {rung // 1024} KB 尾部")
            break

    log_hits = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        def add(name: str, data) -> None:
            blob = data.encode("utf-8") if isinstance(data, str) else data
            archive.writestr(name, blob)
            collected.append(name)
            hashes[name] = hashlib.sha256(blob).hexdigest()

        add("problem.txt", f"# 问题描述\n{problem or '(未填写)'}\n\n"
                           f"# 联系方式\n{contact or '(未填写)'}\n")
        add("README.txt", README_TEXT)

        summary_parts: list = []
        for role, text in payloads.items():
            add(f"logs/{role}.log", text)
            summary_parts.append(text)
        for role, path in (log_files or {}).items():
            if role not in payloads:
                warnings.append(f"日志不存在、为空或无权限：{role} -> {path}")

        freshness = None
        if newest_mtime:
            freshness = max(0.0, (time.time() - newest_mtime) / 3600.0)
            if freshness > STALE_LOG_HOURS:
                warnings.append(f"日志可能过旧：最新日志写于 {freshness / 24:.1f} 天前，"
                                "可能不是本次问题的现场")

        add("state/config-redacted.env", config_clean)
        add("diagnostics/summary.txt", summarize_log("\n".join(summary_parts),
                                                    freshness_hours=freshness))
        add("diagnostics/env.json", json.dumps(env or {}, ensure_ascii=False, indent=2))

        for name, data in (extra_files or {}).items():
            if not is_collectable(name):
                warnings.append(f"跳过受限文件：{name}")
                continue
            add(name, data)

        manifest = {
            "id": bundle_id,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "problem": problem or "",
            "contact": contact or "",
            "files": collected,
            "sha256": hashes,
            "redactions": redactions + log_hits,
            "warnings": warnings,
        }
        manifest.update(manifest_extra or {})
        add("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    prune_bundles(out, keep=5)
    return {"id": bundle_id, "path": str(zip_path), "size": os.path.getsize(zip_path),
            "files": collected, "redactions": redactions, "warnings": warnings}
```

同时把 `import time` 加到模块顶部导入区（与 `os`/`re` 并列）。测试里 `redactions > 0` 由
`redact_env_text` 的 `dropped`（`MRRC_WEB_PASSWORD` 一行）保证。

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv/bin/python -m unittest tests.test_support_bundle -v`
预期：`Ran 39 tests ... OK`

- [ ] **步骤 5：Commit**

```bash
git add support_bundle.py tests/test_support_bundle.py
git commit -m "feat(support): zip assembly with manifest hashes, size ladder and retention"
```

---

## 任务 6：服务端持久化日志（`MRRC_LOG_DIR` + 轮转 + 降级）

**文件：**

- 修改：`server.py:73-77`（logging 区）+ 新增 `_setup_file_logging()` 与两个常量（放在 `RECORDINGS_INDEX` 之后，约 227 行处）
- 修改：`tests/test_quiet_logging.py`（追加测试类；该模块已存在，负责日志纪律）

- [ ] **步骤 1：写失败的测试**

把测试类追加到 `tests/test_quiet_logging.py`（该模块目前只 import 了 `asyncio`/`Path`/`unittest`，
需补 `logging`、`logging.handlers`、`mock`、`tempfile`）：

```python
class SupportLogFileTests(unittest.TestCase):
    def _fresh(self, tmp):
        """Re-run the file-logging setup against a patched LOG_DIR.

        LOG_DIR is a module constant (import time), so the env var cannot be
        patched here — the env-driven path is verified by the smoke run below.
        """
        root = logging.getLogger()
        saved = list(root.handlers)
        try:
            for handler in saved:
                root.removeHandler(handler)
            with mock.patch.object(server, "LOG_DIR", Path(tmp)):
                path = server._setup_file_logging()
            logging.getLogger("mrrc").info("hello from the test")
        finally:
            for handler in list(root.handlers):
                if isinstance(handler, logging.handlers.RotatingFileHandler):
                    handler.close()
                    root.removeHandler(handler)
            for handler in saved:
                root.addHandler(handler)
        return path

    def test_creates_a_rotating_log_under_log_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fresh(tmp)
            self.assertTrue(str(path).startswith(tmp))
            self.assertTrue(Path(path).exists())
            self.assertIn("hello from the test", Path(path).read_text(encoding="utf-8"))

    def test_unwritable_dir_degrades_to_console_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ro"
            target.mkdir()
            target.chmod(0o500)
            try:
                with mock.patch.object(server, "LOG_DIR", target / "logs"):
                    self.assertIsNone(server._setup_file_logging())
            finally:
                target.chmod(0o700)

    def test_log_dir_defaults_to_logs_next_to_the_runtime(self):
        self.assertEqual(server.LOG_DIR.name, "logs")
        self.assertEqual(server.SUPPORT_OUT_DIR, server.LOG_DIR.parent / "support-out")

```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv/bin/python -m unittest tests.test_quiet_logging -v`
预期：`AttributeError: module 'server' has no attribute '_setup_file_logging'`

- [ ] **步骤 3：写最小实现**

在 `server.py` 的 `RECORDINGS_INDEX = _runtime_dir() / "recordings.json"` 之后插入：

```python
# ── Support logging (spec 2026-09-17 §5) ────────────────────────────
# The packaged desktop app had NO server log file: logging went to the
# console window only, so the support bundle would have been empty.  The
# launchers set MRRC_LOG_DIR to the user data directory (Program Files and
# /Applications are not writable); the default keeps source/Pi runs working.
LOG_DIR = Path(_env("MRRC_LOG_DIR", str(_runtime_dir() / "logs")))
SUPPORT_OUT_DIR = LOG_DIR.parent / "support-out"
SUPPORT_URL = _env("MRRC_SUPPORT_URL", "https://www.vlsc.net/mrrc_modern/support/")
```

把 `server.py:73-77` 的 logging 区替换为（**只定义函数，不在这里调用** —— `LOG_DIR` 是后面的常量，
在这个位置调用会 `NameError`，服务启动即失败）：

```python
# ── Logging ─────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


def _setup_file_logging() -> Optional[Path]:
    """Attach a rotating file handler next to the console one.

    Returns the log path, or None when the directory cannot be created —
    logging must never be the reason the server refuses to start (spec §11).
    Must be called *after* LOG_DIR is defined (support logging block below).
    """
    import logging.handlers

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / "server.log"
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.getLogger().addHandler(handler)
        return path
    except OSError as e:
        logging.getLogger("mrrc").warning(
            "File logging disabled (%s) — set MRRC_LOG_DIR to a writable directory", e)
        return None


logger = logging.getLogger("mrrc")
```

（唯一的调用点写在步骤 5，位于 `LOG_DIR` 定义之后。）

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv/bin/python -m unittest tests.test_quiet_logging -v`
预期：`Ran N tests ... OK`

- [ ] **步骤 5：冒烟验证真实写入（同时验证 env 驱动与导入顺序）**

在 `server.py` 里 `LOG_DIR`/`SUPPORT_OUT_DIR`/`SUPPORT_URL` 三个常量**之后**加一行（这是唯一的调用点）：

```python
# Attach the file handler now that LOG_DIR exists (it needs the constant).
SUPPORT_LOG_FILE = _setup_file_logging()
```

运行（从仓库根跑，日志写到 /tmp —— 新进程才能真正验证 `MRRC_LOG_DIR` 生效）：

```bash
cd /Users/cheenle/HAM/mrrc_modern && rm -rf /tmp/mrrc-log-smoke && \
MRRC_LOG_DIR=/tmp/mrrc-log-smoke/logs .venv/bin/python -c "
import server, logging
logging.getLogger('mrrc').info('smoke line')
"; cat /tmp/mrrc-log-smoke/logs/server.log
```

预期：文件存在且含 `smoke line`（若 `LOG_DIR` 与调用点顺序写反，这里会直接 `NameError`）

- [ ] **步骤 6：Commit**

```bash
git add server.py tests/test_quiet_logging.py
git commit -m "feat(support): rotating server log under MRRC_LOG_DIR (console-only degradation)"
```

---

## 任务 7：启动器 StartupTee + `MRRC_LOG_DIR`（两个平台）

**文件：**

- 创建：`launcher_log.py`
- 创建：`tests/test_launcher_log.py`
- 修改：`windows/launcher.py`（Popen 处 ~254 行、env 设定处 ~137 行）
- 修改：`macos/launcher.py`（Popen 处 ~343 行、env 设定处 ~189 行）

- [ ] **步骤 1：写失败的测试**

```python
"""StartupTee: the crash net for "the server died before logging existed" (spec §5)."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import launcher_log


class StartupTeeTests(unittest.TestCase):
    def test_captures_child_output_and_can_be_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tee = launcher_log.StartupTee(tmp, echo=False)
            proc = subprocess.Popen(
                [sys.executable, "-c", "print('early crash line')"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            tee.start(proc)
            proc.wait(timeout=10)
            tee.stop()
            self.assertIn("early crash line", tee.path.read_text(encoding="utf-8"))

    def test_stop_is_idempotent_and_file_closes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tee = launcher_log.StartupTee(tmp, echo=False)
            proc = subprocess.Popen([sys.executable, "-c", "print('x')"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            tee.start(proc)
            tee.stop()
            tee.stop()                                  # must not raise
            self.assertTrue(tee.path.exists())

    def test_rotates_at_launch_so_the_file_stays_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "server-stdout.log"
            log.write_text("old" * 100, encoding="utf-8")
            launcher_log.StartupTee(tmp, max_bytes=10, echo=False)
            self.assertTrue((Path(tmp) / "server-stdout.log.1").exists())
            self.assertFalse(log.exists())

    def test_unwritable_dir_returns_a_disabled_tee(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ro"
            target.mkdir()
            target.chmod(0o500)
            try:
                tee = launcher_log.StartupTee(str(target / "logs"), echo=False)
                proc = subprocess.Popen([sys.executable, "-c", "print('y')"],
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                tee.start(proc)
                proc.wait(timeout=10)
                tee.stop()
                self.assertFalse(tee.attached)
            finally:
                target.chmod(0o700)
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv/bin/python -m unittest tests.test_launcher_log -v`
预期：`ModuleNotFoundError: No module named 'launcher_log'`

- [ ] **步骤 3：写最小实现**

```python
"""Launcher-side startup tee (spec 2026-09-17 §5).

The launcher is the only component that sees a server which dies before its
own logging exists (PyInstaller missing module, "Failed to load Python shared
library", port already in use).  The tee captures that window into a bounded
file AND echoes it, so the Windows console keeps showing what it always showed.

It stops as soon as the server answers HTTP: from then on `logs/server.log`
(inside the server) is the canonical record, and a second copy would only
double the support bundle.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

DEFAULT_NAME = "server-stdout.log"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024


class StartupTee:
    def __init__(self, log_dir, name: str = DEFAULT_NAME,
                 max_bytes: int = DEFAULT_MAX_BYTES, echo: bool = True):
        self.dir = Path(log_dir)
        self.name = name
        self.max_bytes = max_bytes
        self.echo = echo
        self.path = self.dir / name
        self.attached = False
        self._fh = None
        self._thread = None
        self._stop = threading.Event()
        self._rotate()

    def _rotate(self) -> None:
        """Replace the previous run's file (and its single backup) at launch."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                os.replace(self.path, self.dir / (self.name + ".1"))
        except OSError:
            pass

    def start(self, proc) -> None:
        """Begin draining `proc.stdout` (the child must be spawned with a pipe)."""
        if proc is None or proc.stdout is None:
            return
        try:
            self._fh = open(self.path, "w", encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"Startup log disabled ({e})", file=sys.stderr)
            return
        self.attached = True
        self._thread = threading.Thread(target=self._pump, args=(proc,),
                                        name="startup-tee", daemon=True)
        self._thread.start()

    def _pump(self, proc) -> None:
        try:
            for line in proc.stdout:
                if self._stop.is_set():
                    break
                if self._fh is not None:
                    self._fh.write(line)
                    self._fh.flush()
                if self.echo:
                    try:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                    except (OSError, ValueError):
                        pass
        except (OSError, ValueError):
            pass

    def stop(self, timeout: float = 2.0) -> None:
        """Stop writing (the reader thread may still drain into the void)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
```

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv/bin/python -m unittest tests.test_launcher_log -v`
预期：`Ran 4 tests ... OK`

- [ ] **步骤 5：接进 Windows 启动器**

`windows/launcher.py`：顶部导入区加 `import launcher_log`（与既有 `import ssl_bootstrap` 并列）。
在 `env.setdefault("MRRC_RECORDINGS_DIR", ...)`（约 139 行）之后加：

```python
    env.setdefault("MRRC_LOG_DIR", str(user_data_dir() / "logs"))
```

把 `proc = subprocess.Popen(command, cwd=str(app_dir()), env=env, creationflags=creationflags)`
（约 254 行）与其后的 `wait_for_server` 块替换为：

```python
        # Capture the startup window: a server that dies before its own logging
        # exists is invisible otherwise (spec §5).  The tee stops the moment the
        # server answers HTTP — the in-server rotating log takes over from there.
        tee = launcher_log.StartupTee(env.get("MRRC_LOG_DIR", str(user_data_dir() / "logs")))
        proc = subprocess.Popen(command, cwd=str(app_dir()), env=env,
                                creationflags=creationflags,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        tee.start(proc)
        if wait_for_server(url, proc, secure=secure):
            tee.stop()
            webbrowser.open(url)
        elif proc.poll() is not None:
            tee.stop()
            print(f"Server exited during startup — see {tee.path}")
            return proc.returncode or 1
        else:
            tee.stop()
            print(f"Server did not answer within 15s; opening {url} anyway.")
            webbrowser.open(url)
```

- [ ] **步骤 6：接进 macOS 启动器**

`macos/launcher.py`：顶部导入区加 `import launcher_log`；在
`env.setdefault("MRRC_RECORDINGS_DIR", ...)`（约 191 行）之后加同一行 `env.setdefault("MRRC_LOG_DIR", ...)`。
`self.proc = subprocess.Popen(command, cwd=str(app_dir()), env=env)`（约 343 行）改为：

```python
        self.tee = launcher_log.StartupTee(
            env.get("MRRC_LOG_DIR", str(user_data_dir() / "logs")))
        self.proc = subprocess.Popen(command, cwd=str(app_dir()), env=env,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
        self.tee.start(self.proc)
```

并在 `launch_and_open()` 的 `wait_for_server` 分支里、`webbrowser.open(url)` 之前加
`self.tee.stop()`（`elif`/`else` 两个分支同样先 `self.tee.stop()`）。
在 `App.__init__`（约 312 行）里把 `self.tee = None` 与 `self.proc: subprocess.Popen | None = None` 并列声明。

- [ ] **步骤 7：跑两个启动器测试模块 + 后端 spec 补 hiddenimport**

`packaging/pyinstaller/mrrc_modern_launcher.spec` 的 `hiddenimports` 加一行 `"launcher_log",`；
`packaging/macos/mrrc_modern_launcher.spec`（若同样列出 `ssl_bootstrap`）照做。
`packaging/pyinstaller/mrrc_modern_server.spec` 的 `hiddenimports` 加一行 `"support_bundle",`。

同一改动里把 `install.sh` 的 systemd 重定向改名 —— 否则 systemd 与会话内的 `RotatingFileHandler`
会同时写同一个 `logs/server.log`（名字撞车，正是规格 §5 要避免的）：

```bash
StandardOutput=append:$SCRIPT_DIR/logs/server-stdout.log
StandardError=append:$SCRIPT_DIR/logs/server-stdout.log
```

运行：`.venv/bin/python -m unittest tests.test_launcher_log tests.test_windows_launcher tests.test_macos_launcher tests.test_windows_packaging_files -v`
预期：全绿（新增 4 项 + 既有全部）

- [ ] **步骤 8：Commit**

```bash
git add launcher_log.py tests/test_launcher_log.py windows/launcher.py macos/launcher.py install.sh \
  packaging/pyinstaller/mrrc_modern_launcher.spec packaging/pyinstaller/mrrc_modern_server.spec \
  packaging/macos/mrrc_modern_launcher.spec
git commit -m "feat(support): launcher startup tee + MRRC_LOG_DIR for the packaged app"
```

---

## 任务 8：`/api/support/*` 三个端点（单飞、上传、本地保存）

**文件：**

- 修改：`server.py`（`api_mem_channels` 附近，约 2732 行之前插入）
- 创建：`tests/test_support_api.py`

- [ ] **步骤 1：写失败的测试**

```python
"""Support REST endpoints (spec 2026-09-17 §7/§11).  No HTTP client: the
handlers are called with a fake request exactly like tests/test_server_setup."""
import asyncio
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import server


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class SupportApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "server.log").write_text(
            "2026-09-17 07:00:00 [INFO] mrrc: Server ready!\nMRRC_WEB_PASSWORD=hunter2\n",
            encoding="utf-8")
        patches = [
            mock.patch.object(server, "LOG_DIR", logs),
            mock.patch.object(server, "SUPPORT_OUT_DIR", self.root / "support-out"),
            mock.patch.object(server, "_verify_auth", return_value=True),
            mock.patch.object(server, "_config_file_path",
                              return_value=self.root / "mrrc_modern.env"),
            mock.patch.object(server, "_support_env_snapshot", return_value={"version": "1.17.0"}),
            # hermetic: the repo's own logs/ must not leak into the test bundle
            mock.patch.object(server, "_runtime_dir", return_value=self.root),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        (self.root / "mrrc_modern.env").write_text("MRRC_WEB_PASSWORD=hunter2\n", encoding="utf-8")

    def _build(self):
        return asyncio.run(server.api_support_bundle(
            _FakeRequest({"problem": "接收有杂音", "contact": "BH1XXX",
                          "client": {"ua": "iPhone"}})))

    def test_build_returns_files_and_redaction_count(self):
        payload = json.loads(self._build().body)
        self.assertTrue(payload["ok"])
        self.assertGreater(payload["redactions"], 0)
        self.assertIn("logs/server.log", payload["files"])
        self.assertTrue(Path(payload["path"]).exists())

    def test_bundle_excludes_secret_and_recordings(self):
        payload = json.loads(self._build().body)
        with zipfile.ZipFile(payload["path"]) as archive:
            blob = b"".join(archive.read(n) for n in archive.namelist())
        self.assertNotIn(b"hunter2", blob)

    def test_build_is_single_flight(self):
        server._support_build_lock.acquire()
        try:
            payload = json.loads(self._build().body)
        finally:
            server._support_build_lock.release()
        self.assertFalse(payload["ok"])
        self.assertIn("in progress", payload["reason"])

    def test_upload_forwards_and_reports_the_remote_id(self):
        built = json.loads(self._build().body)
        with mock.patch.object(server, "_support_upload", return_value="20260917-080000-abcd") as up:
            payload = json.loads(asyncio.run(
                server.api_support_upload(_FakeRequest({"id": built["id"]}))).body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["remoteId"], "20260917-080000-abcd")
        self.assertEqual(up.call_args[0][0].name, f"support-{built['id']}.zip")

    def test_upload_failure_still_hands_back_the_local_path(self):
        built = json.loads(self._build().body)
        with mock.patch.object(server, "_support_upload", side_effect=OSError("no route to host")):
            payload = json.loads(asyncio.run(
                server.api_support_upload(_FakeRequest({"id": built["id"]}))).body)
        self.assertFalse(payload["ok"])
        self.assertIn("no route to host", payload["reason"])
        self.assertTrue(payload["localPath"].endswith(".zip"))

    def test_unknown_id_is_refused(self):
        payload = json.loads(asyncio.run(
            server.api_support_upload(_FakeRequest({"id": "../../etc/passwd"}))).body)
        self.assertFalse(payload["ok"])

    def test_save_copies_into_the_user_data_dir(self):
        built = json.loads(self._build().body)
        with mock.patch.object(server, "_support_export_dir", return_value=self.root / "saved"):
            payload = json.loads(asyncio.run(
                server.api_support_save(_FakeRequest({"id": built["id"]}))).body)
        self.assertTrue(payload["ok"])
        self.assertTrue(Path(payload["path"]).exists())

    def test_endpoints_are_auth_gated_by_the_middleware(self):
        with mock.patch.object(server, "_verify_auth", return_value=False):
            response = asyncio.run(server.api_support_bundle(_FakeRequest({})))
        self.assertEqual(response.status_code, 401)
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv/bin/python -m unittest tests.test_support_api -v`
预期：`AttributeError: module 'server' has no attribute 'api_support_bundle'`

- [ ] **步骤 3：写最小实现（`server.py`，插在 `@app.post("/api/mem_channels")` 之前）**

```python
# ── Support bundle (spec 2026-09-17): server-side diagnostics + report loop ──
_support_build_lock = threading.Lock()
_support_last_id: Optional[str] = None


def _support_env_snapshot() -> dict:
    """Live state a maintainer needs and cannot infer from a log file."""
    snapshot = support_bundle.collect_env_snapshot(support_bundle.detect_version(
        _runtime_dir(), _resource_dir()))
    try:
        audio = audio.tx_stats() if audio is not None else {}
        snapshot["audio"] = {
            "devices": _list_audio_devices(),
            "rx_sample_rate": getattr(audio, "rx_rate", None),
            "tx_sample_rate": getattr(audio, "tx_rate", None),
        }
    except Exception as e:                              # never fail the bundle
        snapshot["audio"] = {"error": str(e)}
    caps = backend.capabilities() if backend is not None else None
    snapshot["radio"] = {
        "model": getattr(caps, "model_name", None),          # machine key, e.g. "ft710"
        "display_name": getattr(caps, "display_name", None),
        "scope_type": getattr(caps, "scope_type", None),      # "ft4222" | "civ27" | "none"
        "tx_gated": getattr(caps, "tx_gated", None),
        "serial_connected": radio.serial_connected,
    }
    snapshot["recording"] = {"dir": str(RECORDINGS_DIR), "enabled": _rec_writer_task is not None}
    snapshot["config_file"] = str(_config_file_path())
    snapshot["log_file"] = str(SUPPORT_LOG_FILE) if SUPPORT_LOG_FILE else ""
    snapshot["log_dir"] = str(LOG_DIR)
    return snapshot


def _support_collect(problem: str, contact: str, client: dict) -> dict:
    """Build one bundle from live paths (single-flight is the caller's job)."""
    config_text = ""
    try:
        config_text = _config_file_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        config_text = "; 配置文件不可读（可能尚未生成）\n"
    log_files = support_bundle.resolve_log_files(LOG_DIR, LOG_DIR.parent, _runtime_dir())
    client_doc = json.dumps(client or {}, ensure_ascii=False, indent=2)
    return support_bundle.build_bundle(
        SUPPORT_OUT_DIR, problem=problem, contact=contact,
        env=_support_env_snapshot(), log_files=log_files, config_text=config_text,
        extra_files={"diagnostics/client.json": client_doc},
        manifest_extra={"version": support_bundle.detect_version(_runtime_dir(), _resource_dir())}
    )


def _support_upload(zip_file) -> str:
    """POST api/create then PUT the zip; return the receiver's id (stdlib only)."""
    import urllib.request

    base = SUPPORT_URL.rstrip("/")
    payload = json.dumps({"problem": "", "contact": "", "version": support_bundle.detect_version(
        _runtime_dir(), _resource_dir())}).encode("utf-8")
    request = urllib.request.Request(f"{base}/api/create", data=payload,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        created = json.loads(response.read().decode("utf-8"))
    remote_id = created.get("id", "")
    if not created.get("ok") or not remote_id:
        raise RuntimeError(created.get("reason") or "创建记录失败")
    with open(zip_file, "rb") as fh:
        put = urllib.request.Request(f"{base}/api/{remote_id}/bundle", data=fh.read(),
                                     method="PUT",
                                     headers={"Content-Type": "application/zip"})
        with urllib.request.urlopen(put, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(body.get("reason") or "上传失败")
    return remote_id


def _support_export_dir() -> Path:
    """Where 「只保存到本地」 puts the zip (the user data dir; next to the logs)."""
    target = LOG_DIR.parent / "support-export"
    target.mkdir(parents=True, exist_ok=True)
    return target


@app.post("/api/support/bundle", include_in_schema=False)
async def api_support_bundle(request: Request):
    """Build a redacted diagnostics bundle (spec §7)."""
    global _support_last_id
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not _support_build_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "reason": "build in progress"})
    try:
        result = await asyncio.to_thread(
            _support_collect, str(body.get("problem", ""))[:4000],
            str(body.get("contact", ""))[:200], body.get("client") or {})
        _support_last_id = result["id"]
        result = dict(result, ok=True)
        logger.info("Support bundle built: %s (%d KB, %d redactions)",
                    result["id"], result["size"] // 1024, result["redactions"])
        return JSONResponse(result)
    except Exception as e:
        logger.warning("Support bundle build failed: %s", e)
        return JSONResponse({"ok": False, "reason": str(e)})
    finally:
        _support_build_lock.release()


@app.post("/api/support/upload", include_in_schema=False)
async def api_support_upload(request: Request):
    """Upload a previously built bundle; never lose it on failure (spec §9/§11)."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    bundle_id = str(body.get("id", ""))
    path = support_bundle.bundle_path(SUPPORT_OUT_DIR, bundle_id)
    if not path or not path.is_file():
        return JSONResponse({"ok": False, "reason": "unknown bundle", "localPath": ""})
    try:
        remote_id = await asyncio.to_thread(_support_upload, path)
    except Exception as e:
        logger.warning("Support bundle upload failed: %s", e)
        return JSONResponse({"ok": False, "reason": str(e), "localPath": str(path)})
    logger.info("Support bundle uploaded: %s -> %s", bundle_id, remote_id)
    return JSONResponse({"ok": True, "remoteId": remote_id, "size": path.stat().st_size})


@app.post("/api/support/save", include_in_schema=False)
async def api_support_save(request: Request):
    """Copy the bundle where the operator can send it by hand (mail/WeChat)."""
    import shutil

    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    path = support_bundle.bundle_path(SUPPORT_OUT_DIR, str(body.get("id", "")))
    if not path or not path.is_file():
        return JSONResponse({"ok": False, "reason": "unknown bundle"})
    try:
        target = _support_export_dir() / path.name
        shutil.copy2(path, target)
    except OSError as e:
        return JSONResponse({"ok": False, "reason": str(e)})
    return JSONResponse({"ok": True, "path": str(target)})
```

同时把 `import support_bundle` 加到 `server.py` 的导入区（与 `import recorder` 等并列）。

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv/bin/python -m unittest tests.test_support_api -v`
预期：`Ran 8 tests ... OK`

- [ ] **步骤 5：跑全套确认没有回归**

运行：`.venv/bin/python -m unittest discover -s tests 2>&1 | tail -3`
预期：`Ran 1119 tests ... OK`（1103 + 新增 16）

- [ ] **步骤 6：Commit**

```bash
git add server.py tests/test_support_api.py
git commit -m "feat(support): /api/support bundle, upload and local-save endpoints"
```

---

## 任务 9：`static/support.html` + SPA 菜单入口 + 前端契约测试

**文件：**

- 创建：`static/support.html`
- 修改：`static/index.html`（`menu-list` 里加一行，约 378 行 `recordings` 之后）
- 创建：`tests/test_support_frontend.py`

- [ ] **步骤 1：写失败的测试**

```python
"""Frontend contract for the report entry (spec §8): a plain anchor, so the two
tooling-guarded files stay untouched (ft710_ui.js only binds [data-action])."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
PAGE = ROOT / "static" / "support.html"


class SupportEntryTests(unittest.TestCase):
    def test_menu_has_a_support_link(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn('href="/support.html"', html)
        entry = re.search(r'<a[^>]*href="/support.html"[^>]*>(.*?)</a>', html, re.S)
        self.assertIsNotNone(entry)
        self.assertIn("遇到问题", entry.group(1))
        self.assertIn('target="_blank"', entry.group(0))
        self.assertNotIn("data-action", entry.group(0))   # must stay a plain anchor

    def test_menu_binding_still_ignores_plain_anchors(self):
        ui = (ROOT / "static" / "ft710_ui.js").read_text(encoding="utf-8")
        self.assertIn("document.querySelectorAll('.menu-item[data-action]')", ui)

    def test_page_exists_and_calls_the_three_endpoints(self):
        html = PAGE.read_text(encoding="utf-8")
        for endpoint in ("/api/support/bundle", "/api/support/upload", "/api/support/save"):
            self.assertIn(endpoint, html)

    def test_page_collects_client_context_without_innerHTML(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertIn("userAgent", html)
        self.assertNotIn("innerHTML", html)               # XSS gate used repo-wide
        self.assertIn("rel=\"noopener\"", INDEX.read_text(encoding="utf-8"))
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv/bin/python -m unittest tests.test_support_frontend -v`
预期：`AssertionError`（`href="/support.html"` 不在 `index.html` 中）

- [ ] **步骤 3：写页面 `static/support.html`**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MRRC Modern · 遇到问题</title>
<style>
  body{background:#12121a;color:#e0e0e0;font-family:-apple-system,tahoma,sans-serif;margin:0;padding:16px;line-height:1.6}
  h2{margin:0 0 4px}
  .muted{color:#8a8a96;font-size:12.5px}
  textarea,input[type=text]{width:100%;box-sizing:border-box;background:#0f0f16;color:#fff;
    border:1px solid #444;border-radius:4px;padding:9px;font-size:13.5px}
  textarea{min-height:112px;resize:vertical}
  button{padding:11px 16px;border:0;border-radius:4px;background:#e8a33d;color:#1a1a1a;
    font-weight:bold;font-size:14px;margin:6px 8px 0 0;cursor:pointer}
  button[disabled]{background:#39414d;color:#8a8a96;cursor:not-allowed}
  pre{background:#0f0f16;border:1px solid #333;border-radius:4px;padding:10px;
    white-space:pre-wrap;word-break:break-all;font-size:12px;max-height:260px;overflow:auto}
  .ok{color:#5ad07a}.warn{color:#e8c25a}.bad{color:#e86464}
  .card{background:#181824;border:1px solid #2c2c38;border-radius:6px;padding:14px;margin-bottom:14px}
  a{color:#e8a33d}
</style>
</head>
<body>
  <h2>🐞 遇到问题</h2>
  <p class="muted">生成一个诊断包（日志 + 脱敏配置 + 环境/电台/音频状态 + 自动体检结论）并上传给维护者。
  <b>不会包含</b>登录密码、证书私钥、任何令牌，也不会包含录音、记忆频道与天调学习值。</p>

  <div class="card">
    <label class="muted">① 问题描述（越具体越好：什么现象、什么时候开始、电台型号/频率）</label>
    <textarea id="problem" placeholder="例如：接收声音每隔约 1 秒卡一下；FT-710，14.265MHz；今天换到 Windows 后开始"></textarea>
    <label class="muted">联系方式（可选）</label>
    <input type="text" id="contact" placeholder="呼号 / 邮箱 / 微信">
    <div>
      <button id="btnBuild">② 生成诊断包</button>
      <button id="btnUp" disabled>③ 上传给维护者</button>
      <button id="btnSave" disabled>只保存到本地</button>
    </div>
  </div>

  <div class="card">
    <div id="status" class="muted">尚未生成。</div>
    <pre id="listing" style="display:none"></pre>
  </div>

<script>
var currentId = null;
function $(id) { return document.getElementById(id); }
function setStatus(text, cls) {
  var node = $('status');
  node.className = cls || 'muted';
  node.textContent = text;                       // never innerHTML (XSS gate)
}
function clientContext() {
  var ctx = {ua: navigator.userAgent, language: navigator.language,
             platform: navigator.platform, screen: screen.width + 'x' + screen.height};
  try {
    ctx.visibility = document.visibilityState;
    if (window.radioState) { ctx.radioState = {freq: window.radioState.frequency,
                                              mode: window.radioState.mode,
                                              serial_connected: window.radioState.serial_connected}; }
    if (window.wsReconnectCount !== undefined) { ctx.wsReconnects = window.wsReconnectCount; }
  } catch (e) { ctx.error = String(e); }
  return ctx;
}
async function api(path, payload) {
  var res = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                              body: JSON.stringify(payload || {})});
  if (res.status === 401 || res.status === 403) throw new Error('请先回到 MRRC Modern 界面登录');
  try { return await res.json(); } catch (e) { return {ok: false, reason: 'HTTP ' + res.status}; }
}
async function buildBundle() {
  $('btnBuild').disabled = true;
  setStatus('正在收集（读取日志、设备与配置快照）…');
  try {
    var data = await api('/api/support/bundle', {problem: $('problem').value,
                                                contact: $('contact').value,
                                                client: clientContext()});
    if (!data.ok) { setStatus('生成失败：' + (data.reason || '未知原因'), 'bad'); return; }
    currentId = data.id;
    $('listing').style.display = 'block';
    $('listing').textContent = data.files.map(function (f) { return '  ' + f; }).join('\n') +
      '\n\n合计 ' + (data.size / 1024).toFixed(0) + ' KB，已脱敏 ' + data.redactions + ' 处' +
      (data.warnings.length ? '\n提示：\n' + data.warnings.map(function (w) { return '  ! ' + w; }).join('\n') : '');
    $('btnUp').disabled = false;
    $('btnSave').disabled = false;
    setStatus('已生成。确认清单没问题就可以上传（或只保存到本地）。', 'ok');
  } catch (e) { setStatus('生成失败：' + e.message, 'bad'); }
  finally { $('btnBuild').disabled = false; }
}
async function uploadBundle() {
  if (!currentId) { return; }
  $('btnUp').disabled = true;
  setStatus('正在上传…');
  try {
    var data = await api('/api/support/upload', {id: currentId});
    if (data.ok) {
      var url = 'https://www.vlsc.net/mrrc_modern/answers/#' + data.remoteId;
      setStatus('已上传（编号 ' + data.remoteId + '，' + (data.size / 1024).toFixed(0) +
                ' KB）。答复会发布在 ' + url + ' —— 多数问题那里直接有解决办法。', 'ok');
    } else {
      setStatus('上传失败：' + (data.reason || '未知原因') + '\n诊断包仍在：' + (data.localPath || '') +
                '\n可点「只保存到本地」再用邮件/微信发给我。', 'bad');
    }
  } catch (e) { setStatus('上传失败：' + e.message, 'bad'); }
  finally { $('btnUp').disabled = false; }
}
async function saveLocal() {
  if (!currentId) { return; }
  try {
    var data = await api('/api/support/save', {id: currentId});
    setStatus(data.ok ? '已保存到：' + data.path : '保存失败：' + (data.reason || ''), data.ok ? 'ok' : 'bad');
  } catch (e) { setStatus('保存失败：' + e.message, 'bad'); }
}
$('btnBuild').addEventListener('click', buildBundle);
$('btnUp').addEventListener('click', uploadBundle);
$('btnSave').addEventListener('click', saveLocal);
</script>
</body>
</html>
```

- [ ] **步骤 4：`static/index.html` 菜单加一项（`recordings` 那行之后）**

```html
          <li>
            <a href="/support.html" target="_blank" rel="noopener" class="menu-item"
              >🐞 遇到问题</a
            >
          </li>
```

- [ ] **步骤 5：运行测试确认通过**

运行：`.venv/bin/python -m unittest tests.test_support_frontend -v`
预期：`Ran 4 tests ... OK`
再跑前端契约回归：`.venv/bin/python -m unittest tests.test_ws_protocol tests.test_audio -v`（缓存版本断言未被触碰）

- [ ] **步骤 6：Commit**

```bash
git add static/support.html static/index.html tests/test_support_frontend.py
git commit -m "feat(support): 🐞 遇到问题 page and SPA menu entry (plain anchor, no guarded-file edit)"
```

---

## 任务 10：接收端 vendor + systemd unit + 幂等部署脚本

**文件：**

- 创建：`tools/support_receiver/server.py`、`tools/support_receiver/support-receiver-modern.service`、`deploy_support_receiver.sh`
- 创建：`tests/test_support_receiver.py`

- [ ] **步骤 1：vendor 接收端（含来源标注与本地改动）**

从兄弟仓复制（`scp`/`cp` 也行）：

```bash
cp /Users/cheenle/HAM/mrrc/tools/support_receiver/server.py tools/support_receiver/server.py
```

在文件头部 docstring 之后插入来源标注与本地改动，并把默认端口改为 8098：

```python
# Vendored from the sibling project `mrrc` (tools/support_receiver/server.py,
# 262 lines) on 2026-09-17.  Local changes, deliberately kept minimal so the two
# copies stay comparable:
#   1. SUPPORT_PORT default 8099 -> 8098 (MRRC Modern owns this port on the host);
#   2. SUPPORT_DIR default /var/www/support -> /var/www/support-modern;
#   3. `product` is written into meta.json so the list page and the (sub-project 2)
#      triage can tell the two products' bundles apart.
# Interface unchanged: POST /api/create, PUT /api/<id>/bundle, GET /api/list (Basic
# auth), GET /api/<id>/bundle (Basic auth).
```

对应三处改动：

```python
DIR = os.environ.get("SUPPORT_DIR", "/var/www/support-modern")
PRODUCT = os.environ.get("SUPPORT_PRODUCT", "mrrc_modern")
PORT = int(os.environ.get("SUPPORT_PORT", "8098"))
```

```python
                  "version": str(meta.get("version", ""))[:40],
                  "product": PRODUCT}
```

列表页版本列改为 `f"{meta.get('product', '?')} {meta.get('version', '?')}"`。

- [ ] **步骤 2：systemd unit 与部署脚本**

`tools/support_receiver/support-receiver-modern.service`：

```ini
[Unit]
Description=MRRC Modern support bundle receiver
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/mrrc-modern-support.env
ExecStart=/usr/bin/python3 /opt/mrrc-modern-support/server.py
Restart=on-failure
RestartSec=5
# Bundles are operator data: a dedicated user, no shell, no write access to the docroot.
User=www-data
Group=www-data

[Install]
WantedBy=multi-user.target
```

`deploy_support_receiver.sh`（幂等；口令文件不入库、不打印）：

```bash
#!/usr/bin/env bash
# Deploy/refresh the MRRC Modern support receiver (idempotent).
#   ./deploy_support_receiver.sh [user@host]
# Password source: $SUPPORT_PASSWORD -> ~/.mrrc-support-credentials.txt -> generated.
set -euo pipefail

REMOTE="${1:-cheenle@www.vlsc.net}"
HERE="$(cd "$(dirname "$0")" && pwd)"

PW="${SUPPORT_PASSWORD:-}"
if [ -z "$PW" ] && [ -f "$HOME/.mrrc-support-credentials.txt" ]; then
    PW="$(tr -d '\n' < "$HOME/.mrrc-support-credentials.txt")"
fi
if [ -z "$PW" ]; then
    PW="$(python3 -c 'import secrets,string;print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(24)))')"
    printf '%s' "$PW" > "$HOME/.mrrc-support-credentials.txt"
    chmod 600 "$HOME/.mrrc-support-credentials.txt"
    echo "generated a new password -> ~/.mrrc-support-credentials.txt"
fi

echo "==> directories (storage outside the docroot, owned by www-data)"
ssh "$REMOTE" 'sudo mkdir -p /opt/mrrc-modern-support && sudo mkdir -p /var/www/support-modern && sudo chown www-data:www-data /var/www/support-modern && sudo chmod 750 /var/www/support-modern'

echo "==> upload server + unit"
rsync -az "$HERE/tools/support_receiver/server.py" "$REMOTE:/tmp/support-modern-server.py"
rsync -az "$HERE/tools/support_receiver/support-receiver-modern.service" "$REMOTE:/tmp/support-receiver-modern.service"
ssh "$REMOTE" 'sudo install -m 644 /tmp/support-modern-server.py /opt/mrrc-modern-support/server.py && sudo install -m 644 /tmp/support-receiver-modern.service /etc/systemd/system/support-receiver-modern.service'

echo "==> password (0600 root only, never printed, never in git)"
ssh "$REMOTE" "sudo bash -c 'umask 077; printf \"SUPPORT_PASSWORD=%s\nSUPPORT_DIR=/var/www/support-modern\nSUPPORT_PORT=8098\nSUPPORT_PRODUCT=mrrc_modern\n\" \"$PW\" > /etc/mrrc-modern-support.env'"

echo "==> start/restart"
ssh "$REMOTE" 'sudo systemctl daemon-reload && sudo systemctl enable --now support-receiver-modern >/dev/null 2>&1; sudo systemctl restart support-receiver-modern; sleep 1; sudo systemctl is-active support-receiver-modern'
ssh "$REMOTE" 'curl -s -o /dev/null -w "  local probe /api/list without password -> HTTP %{http_code} (want 401)\n" http://127.0.0.1:8098/api/list'

echo "==> nginx path /mrrc_modern/support/ (idempotent)"
# Local quoted heredoc piped into ssh: nothing is expanded locally, and the
# remote python gets the real $host for nginx (an unquoted delimiter inside a
# single-quoted ssh command would let the *remote* shell eat it).
ssh "$REMOTE" sudo python3 - <<'NGINX_PY'
path = "/etc/nginx/sites-available/vlsc.net"
text = open(path, encoding="utf-8").read()
if "location /mrrc_modern/support/" in text:
    print("nginx: already present")
else:
    block = """    # ── MRRC Modern support receiver (/mrrc_modern/support/) ──
    location /mrrc_modern/support/ {
        proxy_pass http://127.0.0.1:8098/;
        proxy_set_header Host $host;
        client_max_body_size 25m;
    }
"""
    # Insert before the website block so the file keeps its reading order.
    marker = "    # ── MRRC Modern website (/mrrc_modern/) ──"
    text = (text.replace(marker, block + "\n" + marker) if marker in text
            else text.rstrip() + "\n\n" + block)
    open(path, "w", encoding="utf-8").write(text)
    print("nginx: added /mrrc_modern/support/")
NGINX_PY
ssh "$REMOTE" 'sudo nginx -t && sudo systemctl reload nginx'
echo "==> done: https://www.vlsc.net/mrrc_modern/support/"
```

- [ ] **步骤 3：写测试**

```python
"""Vendored receiver + deployment invariants (spec §9).  Structural checks only —
the deploy itself is verified live by the script's own probes."""
import inspect
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "support_receiver"))

import server as receiver                                       # noqa: E402  (vendored module)


class ReceiverVendoringTests(unittest.TestCase):
    def test_layout_defaults_are_this_product(self):
        self.assertEqual(receiver.PRODUCT, "mrrc_modern")
        self.assertIn("support-modern", receiver.DIR)
        self.assertEqual(receiver.ID_RE.pattern, r"^\d{8}-\d{6}-[0-9a-f]{4}$")

    def test_meta_records_the_product(self):
        source = inspect.getsource(receiver)
        self.assertIn('"product": PRODUCT', source)

    def test_id_regex_refuses_traversal(self):
        self.assertIsNone(receiver.ID_RE.match("../../etc/passwd"))
        self.assertIsNotNone(receiver.ID_RE.match("20260917-072530-ab12"))

    def test_list_requires_a_password(self):
        source = inspect.getsource(receiver)
        self.assertIn("_require_auth", source)
        self.assertIn("Basic", source)


class DeployScriptTests(unittest.TestCase):
    def setUp(self):
        self.script = (ROOT / "deploy_support_receiver.sh").read_text(encoding="utf-8")

    def test_targets_the_dedicated_unit_port_and_storage(self):
        self.assertIn("support-receiver-modern", self.script)
        self.assertIn("SUPPORT_PORT=8098", self.script)
        self.assertIn("/var/www/support-modern", self.script)

    def test_password_file_is_0600_and_never_echoed(self):
        self.assertIn("chmod 600", self.script)
        self.assertIn("umask 077", self.script)
        self.assertNotIn('echo "$PW"', self.script)

    def test_no_password_literal_is_committed(self):
        self.assertIsNone(re.search(r"SUPPORT_PASSWORD=[A-Za-z0-9]{8,}", self.script))
```

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv/bin/python -m unittest tests.test_support_receiver -v`
预期：`Ran 7 tests ... OK`

- [ ] **步骤 5：部署到真机并验证（需要网络 + ssh 权限）**

运行：`./deploy_support_receiver.sh`
预期：`systemctl is-active` 输出 `active`；`/api/list` 无口令探测打印 `HTTP 401`

- [ ] **步骤 6：Commit**

```bash
git add tools/support_receiver deploy_support_receiver.sh tests/test_support_receiver.py
git commit -m "feat(support): vendored receiver instance + idempotent deploy (8098, own storage)"
```

---

## 任务 11：答复页占位 + 站点接线（中英成对）

**文件：**

- 创建：`website/answers/index.html`
- 修改：`website/deploy.sh`（`REQUIRED_FILES` 数组）
- 修改：`website/guide.html`、`website/zh/guide.html`

- [ ] **步骤 1：写占位页**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MRRC Modern · 问题答复</title>
<style>
  body{background:#12121a;color:#e0e0e0;font-family:-apple-system,tahoma,sans-serif;
       margin:0 auto;padding:24px;max-width:760px;line-height:1.7}
  h1{font-size:22px;margin:0 0 6px}
  .muted{color:#8a8a96;font-size:13px}
  code{background:#1b1b26;border-radius:3px;padding:1px 5px}
  a{color:#e8a33d}
</style>
</head>
<body>
  <h1>🐞 问题答复</h1>
  <p class="muted">这一页发布「🐞 遇到问题」上报的分析结论。每条的编号形如
  <code>20260917-072530-ab12</code>，就是你上传成功后界面显示的那个。</p>

  <h2>现在还没有答复</h2>
  <p>答复发布功能正在建设中（支持链路第 2 期：自动分诊 + 答复页）。目前你的诊断包已经能上传，
  维护者会人工查看并在下一期把答复发到这里。</p>

  <h2>着急的话</h2>
  <p>请把界面上的 <b>编号</b> 连同问题描述一起发到
  <a href="https://www.vlsc.net/mrrc_modern/">MRRC Modern 主页</a>上的联系方式。
  也可以先把诊断包【只保存到本地】，用邮件或微信直接发给我。</p>
</body>
</html>
```

- [ ] **步骤 2：接进部署脚本**

`website/deploy.sh` 的 `REQUIRED_FILES` 数组里加一行（跟随既有缩进）：

```bash
 "answers/index.html"
```

- [ ] **步骤 3：指南页两处小节（中英成对，站点审计会检查成对）**

`website/zh/guide.html`（在"遇到问题"相关小节处）插入：

```html
<h3>🐞 遇到问题怎么报</h3>
<p>界面右上角菜单 → <b>🐞 遇到问题</b>：写下现象 → <b>生成诊断包</b> → <b>上传给维护者</b>。
诊断包内含日志、脱敏配置、环境/电台/音频状态与自动体检结论，<b>不含</b>密码、证书私钥、录音与记忆频道。
生成后界面会给你一个编号，答复会发布在 <a href="/mrrc_modern/answers/">问题答复</a> 页。</p>
<p>没有网络时（例如树莓派不在线）点 <b>只保存到本地</b>，再把文件发给我即可。</p>
```

`website/guide.html` 对应英文：

```html
<h3>Reporting a problem</h3>
<p>Menu (top right) → <b>🐞 Report a problem</b>: describe what you see → <b>Build bundle</b> →
<b>Upload</b>. The bundle carries logs, a redacted config snapshot, environment/radio/audio state and an
auto-triage summary — <b>never</b> passwords, private keys, recordings or memory channels. The page shows a
bundle id; answers are published on the <a href="/mrrc_modern/answers/">answers page</a>.</p>
<p>No internet (e.g. an offline Raspberry Pi)? Use <b>Save locally</b> and send me the file.</p>
```

- [ ] **步骤 4：跑发布门禁确认站点一致**

运行：

```bash
python3 .agents/skills/dual-platform-release/harness/release_check.py; echo "exit=$?"
```

预期：`exit=0`（离线规则全绿；中英成对由既有站点守卫测试覆盖）

- [ ] **步骤 5：Commit**

```bash
git add website/answers/index.html website/deploy.sh website/guide.html website/zh/guide.html
git commit -m "docs(site): answers placeholder + bilingual 'report a problem' section"
```

---

## 任务 12：`version.txt`（构建产物）+ 发布流程接线

**文件：**

- 修改：`packaging/macos/build.sh`、`packaging/windows/build.ps1`
- 修改：`.agents/skills/dual-platform-release/SKILL.md`、`mac_pack.md`、`win_pack.md`、`pi_pack.md`
- 修改：`tests/test_release_artifacts.py`（追加源码级断言；该模块已在套件内）

- [ ] **步骤 1：写失败的测试**

```python
class VersionTxtBuildStepTests(unittest.TestCase):
    """version.txt is a build product: the offline release rules cannot see it,
    so the enforceable part is that each build script still derives it from the
    CHANGELOG (spec §16)."""

    def test_macos_build_writes_version_txt_next_to_the_executable(self):
        script = (REPO / "packaging" / "macos" / "build.sh").read_text(encoding="utf-8")
        self.assertIn("version.txt", script)
        self.assertIn("CHANGELOG.md", script)

    def test_windows_build_writes_version_txt(self):
        script = (REPO / "packaging" / "windows" / "build.ps1").read_text(encoding="utf-8")
        self.assertIn("version.txt", script)
        self.assertIn("CHANGELOG.md", script)
```

（该模块已有 `REPO = Path(__file__).resolve().parent.parent` 常量，测试本体不加新常量。）

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv/bin/python -m unittest tests.test_release_artifacts -v`
预期：`AssertionError: 'version.txt' not found in ...`

- [ ] **步骤 3：两个构建脚本写 `version.txt`**

`packaging/macos/build.sh`（在把 server onedir 拷进 `$APP_MACOS` 的那一行
（`cp -R "$PYI_ROOT/MRRC-Modern-Server/." "$APP_MACOS/"`）之后插入）：

```bash
# version.txt next to the frozen server executable: $APP_MACOS is
# Contents/MacOS, which is what _runtime_dir() resolves to at runtime.  Without
# it the app cannot state what it is (support bundle manifest; and it is the
# version authority sub-project 3 needs).  $VERSION comes from the CHANGELOG
# top entry parsed above.
printf '%s\n' "$VERSION" > "$APP_MACOS/version.txt"
```

`packaging/windows/build.ps1`（插在 `Copy-Item` 组装块之后 —— 此时 `$AppRoot` 已是最终安装目录）：

```powershell
# version.txt beside the exe inside the assembled app dir ($AppRoot is what the
# installer packages and what _runtime_dir() resolves to at runtime).
$appVersion = (Select-String -Path "CHANGELOG.md" -Pattern '^## \[v?([0-9]+\.[0-9]+\.[0-9]+)' |
    Select-Object -First 1).Matches[0].Groups[1].Value
Set-Content -Path (Join-Path $AppRoot "version.txt") -Value $appVersion -Encoding ascii
```

- [ ] **步骤 4：发布流程文档加抽查项**

在 `.agents/skills/dual-platform-release/SKILL.md` 的产物抽查小节、以及
`mac_pack.md` / `win_pack.md` / `pi_pack.md` 的对应排错表里各加一条：

```markdown
- `version.txt` 必须存在于产物内且等于 CHANGELOG 顶版本（诊断包 manifest 与后续一键升级都读它）：
  macOS `Contents/MacOS/version.txt`、Windows `<install>\version.txt`、rpi64 `/opt/mrrc_modern/version.txt`
```

- [ ] **步骤 5：运行测试确认通过**

运行：`.venv/bin/python -m unittest tests.test_release_artifacts -v`
预期：全绿

- [ ] **步骤 6：Commit**

```bash
git add packaging/macos/build.sh packaging/windows/build.ps1 tests/test_release_artifacts.py \
  .agents/skills/dual-platform-release/SKILL.md mac_pack.md win_pack.md pi_pack.md
git commit -m "feat(packaging): write version.txt into every artifact + release-time check"
```

---

## 任务 13：文档同步（SDD / README / AGENTS / PROJECT_MAP / tests-README / CHANGELOG / constraints）

**文件：** 见下表（每处都要动，`release_check.py` 与既有测试会检查）

- [ ] **步骤 1：SDD**

| 文件 | 改动 |
| --- | --- |
| `SDD/08-architecture-decisions.md` | 索引表加一行 ` | AD-021 | Server-built, allow-list-redacted support bundle with a dedicated receiver | Implemented | `，正文加`## AD-021: ...`（决策、选型对比：浏览器构建 vs 服务端构建 vs 上传第三方；后果：日志持久化成为前置、接收端独立实例、`version.txt` 成为版本权威） |
| `SDD/10-service-model.md` | §10.1 加 ` | SupportService | Support | Implemented | POST /api/support/{bundle,upload,save}: redacted diagnostics bundle + report loop | `；§10.3 接口表加同一行的接口契约 |
| `SDD/12-operational-model.md` | §12.6 表格加 `logs/server.log`（轮转 2 MB × 2）、`logs/server-stdout.log`（启动器 tee）、`support-out/`（保留 5 个包）；§12.5 加"如何部署接收端（`./deploy_support_receiver.sh`）"与"如何读一个诊断包（先看 `diagnostics/summary.txt`）" |
| `SDD/13-feasibility-assessment.md` | 加风险 R13：脱敏是尽力而为 + 接收端 create/PUT 不鉴权（缓解：白名单 + 值替换 + 计数可见、限速 + 不可猜 ID + 清单鉴权 + 手工删除） |
| `SDD/14-version-history.md` | 首行加新版本行（SDD V2.51） |
| `SDD/05-non-functional-requirements.md` | 加 NFR：用户数据（录音/记忆频道/证书/密码）**永不**因诊断包离开本机；只有操作员主动点击才生成脱敏包 |

- [ ] **步骤 2：README / AGENTS / PROJECT_MAP / tests-README / CHANGELOG**

- `README.md`：环境变量表加 `MRRC_LOG_DIR`、`MRRC_SUPPORT_URL`；功能段加一段"遇到问题 → 诊断包"。
- `AGENTS.md`：模块表加 `support_bundle.py`、`launcher_log.py`、`tools/support_receiver/`；env 清单加两个变量；**测试数从 1055/53 修正为实际值**（本任务结束时 `tests/README.md` 里已更新）。
- `docs/PROJECT_MAP.md`：§2.1 加一行"支持链路"（代码 ↔ 规格 ↔ 测试 ↔ 文档 ↔ 部署），§2.3 加 `MRRC_LOG_DIR`/`MRRC_SUPPORT_URL` 与 `deploy_support_receiver.sh` 的归属。
- `tests/README.md`：表格加 5 个新模块与用例数，总数改为实际值（跑一次全套取数字）。
- `CHANGELOG.md`：顶部加未发布段 `## [Unreleased]`，条目写清"诊断包 + 上报闭环（含日志持久化）"，并按既有体例注明验证边界（Windows 真机与 rpi64 未验证）。

- [ ] **步骤 3：守护约束（每次事故/新能力都要留守卫）**

`.agents/skills/sdd-guardian/harness/constraints.json` 加一条：

```json
{
  "id": "support-bundle-privacy",
  "severity": "block",
  "globs": ["support_bundle.py", "server.py", "static/support.html"],
  "rule": "A support bundle must never carry secrets, certificates, recordings, memory channels or ATR learning data. Collect config keys strictly through CONFIG_KEY_ALLOWLIST and files through is_collectable().",
  "enforced_by": ["tests/test_support_bundle.py", "tests/test_support_api.py"]
}
```

- [ ] **步骤 4：跑文档一致性门禁**

运行：

```bash
python3 .agents/skills/dual-platform-release/harness/release_check.py; echo "release_check=$?"
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged; echo "guardian=$?"
.venv/bin/python -m unittest tests.test_sdd_docs_consistency tests.test_release_artifacts -v 2>&1 | tail -3
```

预期：`release_check=0`、`guardian=0`、测试全绿

- [ ] **步骤 5：Commit**

```bash
git add SDD README.md AGENTS.md docs/PROJECT_MAP.md tests/README.md CHANGELOG.md \
  .agents/skills/sdd-guardian/harness/constraints.json
git commit -m "docs(support): AD-021, SDD sync, project map and guardian constraint for the bundle privacy"
```

---

## 任务 14：总验收（对照规格 §13 的 5 条）

**文件：** 无代码改动；产出验收记录写进 CHANGELOG 的"验证边界"段

- [ ] **步骤 1：全套测试 + 门禁**

```bash
.venv/bin/python -m unittest discover -s tests 2>/tmp/ut.txt >/dev/null; echo "exit=$?"; tail -3 /tmp/ut.txt
python3 .agents/skills/dual-platform-release/harness/release_check.py; echo "exit=$?"
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged; echo "exit=$?"
```

预期：三个 `exit=0`，测试数 = 1103 + 新增（预计 ≈1125）

- [ ] **步骤 2：本机真跑一次「生成 → 只保存到本地」（规格 §13.1/§13.3/§13.4）**

```bash
.venv/bin/python -c "
import os, tempfile, pathlib, json
os.environ['MRRC_LOG_DIR'] = tempfile.mkdtemp() + '/logs'
import server, support_bundle
path = pathlib.Path(server.LOG_DIR); path.mkdir(parents=True, exist_ok=True)
(path / 'server.log').write_text('2026-09-17 09:00:00 [INFO] mrrc: Server ready!\n')
r = support_bundle.build_bundle(server.SUPPORT_OUT_DIR, problem='smoke',
                                log_files=support_bundle.resolve_log_files(server.LOG_DIR, server.LOG_DIR.parent),
                                config_text='MRRC_WEB_PASSWORD=topsecret\nMRRC_WEB_PORT=8888\n',
                                env=support_bundle.collect_env_snapshot('1.17.0'))
print(json.dumps({k: r[k] for k in ('id','size','redactions','warnings')}, ensure_ascii=False))
print('SECRET IN ZIP:', 'topsecret' in open(r['path'],'rb').read().decode('latin-1'))
print(open(r['path'],'rb').read().decode('latin-1').count('Server ready!'))
"
```

预期：打印 `SECRET IN ZIP: False` 与 `1`（日志进入包内），`redactions >= 1`

- [ ] **步骤 3：真机验收（需要操作员在场的部分，逐条记录结果）**

| 项 | 平台 | 通过判据 |
| --- | --- | --- |
| 生成 + 上传 | macOS 安装版 | 界面给出编号；接收端 `https://www.vlsc.net/mrrc_modern/support/api/list`（口令）可见该编号 |
| SHA 一致 | macOS 安装版 | 接收端下载件的 SHA-256 == 本机 `support-out/support-<id>.zip` |
| 生成（无网络） | Windows 安装版 | 断网时上传失败但「只保存到本地」出包且完整 |
| systemd 日志路径 | rpi64 | `/opt/mrrc_modern/logs/server-stdout.log` 非空、`logs/server.log` 亦有 |

未完成项如实写进 CHANGELOG 的"验证边界"（不要写"应该没问题"）。

- [ ] **步骤 4：Commit 验收记录**

```bash
git add CHANGELOG.md
git commit -m "test(support): record the acceptance evidence for the bundle/report loop"
```

---

## 自检（写计划者自查，已在计划内修好）

**1. 规格覆盖度**

| 规格节 | 实现任务 |
| --- | --- |
| §4 架构/3 个新单元 | 任务 1–5、8、10 |
| §5 日志持久化（两个文件 + tee + 名字统一） | 任务 6、7（`install.sh` 改名在任务 7 步骤 7 一并改） |
| §6 内容与隐私契约（白名单/值清洗/禁止项/摘要） | 任务 1、2、3、5 |
| §7 API 契约（单飞/保留 5/上传/本地保存） | 任务 5、8 |
| §8 前端（页面、菜单纯 `<a>`、客户端上下文） | 任务 9 |
| §9 接收端与部署（vendor、8098、独立存储、幂等脚本） | 任务 10 |
| §10 答复页占位 | 任务 11 |
| §11 失败路径（无日志/过旧/不可写/超限/断网/401/中途关闭） | 任务 3、5、6、8（`401` 由既有中间件 + 任务 8 测试覆盖；页面 401 分支在任务 9） |
| §12 测试计划（5 个新模块） | 任务 1–11（`tests/test_launcher_log.py` 属任务 7；`test_support_frontend.py` 属任务 9） |
| §13 验收 5 条 | 任务 14 |
| §16 文档与守卫 | 任务 12、13 |

**遗漏补齐**：`install.sh` 的 systemd 重定向改名原先只写在规格里没有任务 —— 已放进任务 7 步骤 7；
`version.txt` 的构建脚本与发布抽查原先只写了"要做" —— 已在任务 12 给出两个平台的具体代码。

**2. 占位符扫描**：无 TODO/待定。任务 12 已给出两个构建脚本里**真实存在**的变量名
（macOS `$VERSION` / `$APP_MACOS`，Windows `$AppRoot`），不再有"取脚本内实际变量名"这类软占位。

**3. 类型一致性**（跨任务引用同一名字，已核对）

| 名字 | 定义位置 | 使用位置 |
| --- | --- | --- |
| `redact_env_text / redact_text / is_collectable` | 任务 1 | 任务 5（`build_bundle`） |
| `tail_lines / resolve_log_files` | 任务 2 | 任务 5、8 |
| `summarize_log / STALE_LOG_HOURS` | 任务 3 | 任务 5 |
| `detect_version / collect_env_snapshot` | 任务 4 | 任务 8 |
| `BUNDLE_ID_RE / new_bundle_id / is_valid_bundle_id / bundle_path / prune_bundles / build_bundle` | 任务 5 | 任务 8 |
| `LOG_DIR / SUPPORT_OUT_DIR / SUPPORT_URL / SUPPORT_LOG_FILE / _setup_file_logging` | 任务 6（常量在 `RECORDINGS_INDEX` 之后；`SUPPORT_LOG_FILE = _setup_file_logging()` 在常量之后调用） | 任务 8（`_support_collect`、`_support_upload`、`_support_export_dir`） |
| `StartupTee(.start/.stop/.path/.attached)` | 任务 7 | 任务 7（两个启动器） |
| `_support_build_lock / _support_env_snapshot / _support_upload / _support_export_dir / api_support_*` | 任务 8 | 任务 8 测试、任务 9 页面端点名 |
| `SUPPORT_PRODUCT / PRODUCT / ID_RE`（接收端） | 任务 10 | 任务 10 测试 |
