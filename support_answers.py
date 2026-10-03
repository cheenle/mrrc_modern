"""Answer cards for the support chain's second half (spec 2026-09-17-support-autopilot §4–§7).

Pure logic, stdlib only, no application imports: the autopilot CLI does the
network/model/file work, this module decides **what is safe to publish**.  The
answers page is public, so the privacy filter is the security boundary here —
same relationship `support_bundle.py` has to the diagnostics bundle.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from pathlib import Path

STATUSES = ("answered", "needs_fix", "need_more_info")
CATEGORIES = ("环境", "使用问题", "产品缺陷", "网络", "升级", "音频", "电台", "其他")
REQUIRED_KEYS = ("verdict", "status", "category", "diagnosis", "solution")
LIST_KEYS = ("diagnosis", "solution", "evidence", "keys")
# Only these statuses may reach the public page (spec §1 D2).
PUBLISHABLE_STATUSES = ("answered", "needs_fix")

# ── privacy filter (spec §7) ────────────────────────────────────────────────
# The page is public and the input is an operator's own words plus machine
# facts: contact details, device paths, private addresses and local paths must
# never appear.  Ordered: each pattern is replaced with a neutral marker so a
# reader still sees that something was withheld.
PRIVACY_PATTERNS = (
    ("邮箱", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("设备路径", re.compile(r"/dev/(?:cu|tty)[\w.-]*")),
    ("串口", re.compile(r"\bCOM\d+\b")),
    ("内网地址", re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
                        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
                        r"|192\.168\.\d{1,3}\.\d{1,3})\b")),
    ("本机路径", re.compile(r"(?:/Users/[\w.-]+|/home/[\w.-]+|[A-Z]:\\Users\\[\w.-]+)")),
    ("临时路径", re.compile(r"(?:/var/folders/[\w/.-]+|/tmp/[\w/.-]+|/private/var/[\w/.-]+)")),
)
REDACTED_MARK = "[已隐去]"


def redact_public(text: str) -> str:
    """Strip operator-identifying data from anything that may be published."""
    out = str(text or "")
    for _label, pattern in PRIVACY_PATTERNS:
        out = pattern.sub(REDACTED_MARK, out)
    return out


def privacy_hits(text: str) -> list[str]:
    """Which privacy classes the text contained (for the draft's own audit line)."""
    return [label for label, pattern in PRIVACY_PATTERNS if pattern.search(str(text or ""))]


# ── analysis contract (spec §4) ─────────────────────────────────────────────
def parse_analysis(raw: str) -> dict:
    """Extract and validate the model's JSON object.

    Raises ValueError with a readable reason: a malformed answer must become a
    recorded failure, never a published card (spec §4).
    """
    text = str(raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型输出里没有 JSON 对象")
    try:
        data = json.loads(text[start:end + 1])
    except ValueError as e:
        raise ValueError(f"模型输出的 JSON 无法解析：{e}") from e
    if not isinstance(data, dict):
        raise ValueError("模型输出的 JSON 不是对象")
    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:
        raise ValueError(f"缺少字段：{', '.join(missing)}")
    status = str(data.get("status", ""))
    if status not in STATUSES:
        raise ValueError(f"status 不是 {STATUSES} 之一：{status!r}")
    category = str(data.get("category", ""))
    if category not in CATEGORIES:
        raise ValueError(f"category 不在词表内：{category!r}")
    cleaned: dict = {
        "verdict": str(data.get("verdict", "")).strip(),
        "status": status,
        "category": category,
        "needs_code_change": bool(data.get("needs_code_change", False)),
        "code_hint": str(data.get("code_hint", "") or "").strip(),
    }
    # Optional FDE-board fields (support_board): kind/severity/title. Older
    # model outputs stay valid — absent means unclassified on the board.
    for opt in ("kind", "severity", "title"):
        if str(data.get(opt) or "").strip():
            cleaned[opt] = str(data[opt]).strip()[:120]
    for key in LIST_KEYS:
        value = data.get(key) or []
        if isinstance(value, str):
            value = [value]
        cleaned[key] = [str(item).strip() for item in value if str(item).strip()]
    if not cleaned["solution"]:
        raise ValueError("solution 为空：结论必须落到用户能做的动作")
    return cleaned


def should_publish(analysis: dict) -> bool:
    """`need_more_info` is a question for the operator, not an answer (spec §1 D2)."""
    return str(analysis.get("status", "")) in PUBLISHABLE_STATUSES


# ── known-issue cards (symptom-addressed, no bundle id) ────────────────────
# A defect the maintainer already located does not need a bundle number to be useful: the
# operator searches for what they *see* ("接入云端报错", "频谱不对").  These cards are
# versioned here rather than in the autopilot state, so a state reset cannot lose them, and
# they go through the same renderer + privacy filter as an analysed bundle.  Keep the field
# shape identical to a parsed analysis, plus `id`, `problem` (the card title) and `at`.
KNOWN_ISSUES = (
    {
        "id": "known-20261004-tls",
        "at": "2026-10-04",
        "problem": "接入云端报 CERTIFICATE_VERIFY_FAILED（诊断包也上传不了、软件更新检查失败）",
        "status": "needs_fix",
        "category": "网络",
        "verdict": "macOS 打包版（v1.25.0 及更早）没有可用的 CA 信任库：应用里所有外发 HTTPS 都无法校验",
        "diagnosis": [
            "报错形如 certificate verify failed: unable to get local issuer certificate (_ssl.c:1032)，"
            "这不是 portal 或网络的问题——同一台机器上 curl 访问 portal 是 200、证书链完整",
            "根因：安装包把构建机的 OpenSSL 一起带走了，它的默认 CA 路径是构建机的 /opt/local/...（MacPorts），"
            "用户机上没有这个目录，于是信任库为空；空信任库同样导致诊断包上传失败",
            "Windows 与源码运行不受影响（Windows 用系统证书库）",
        ],
        "solution": [
            "临时规避（立刻可用）：在配置文件里加一行 SSL_CERT_FILE=/etc/ssl/cert.pem，然后重启 MRRC Modern",
            "macOS 配置文件位置：~/Library/Application Support/MRRC-Modern/mrrc_modern.env",
            "重启后：接入云端、诊断包上传、软件更新检查应同时恢复",
            "修复已进入源码（随包 CA + 显式信任库），随下一个安装包到达用户，无需你重装以外的任何操作",
        ],
        "evidence": [
            "Support bundle upload failed: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] "
            "certificate verify failed: unable to get local issuer certificate (_ssl.c:1032)>",
            "libcrypto.3.dylib 编译期默认 CA：/opt/local/libexec/openssl3/etc/openssl/cert.pem",
        ],
        "keys": ["接入云端", "portal", "证书", "CERTIFICATE_VERIFY_FAILED", "诊断包上传失败",
                 "软件更新", "SSL_CERT_FILE"],
        "needs_code_change": True,
        "code_hint": "net_tls.py（信任库）+ packaging/*/build.*（随包 vendor/ca/cacert.pem）",
    },
    {
        "id": "known-20261004-ftdi",
        "at": "2026-10-04",
        "problem": "频谱显示不对：一直是一条光滑的假曲线，看不到真实信号",
        "status": "needs_fix",
        "category": "产品缺陷",
        "verdict": "macOS 打包版 v1.25.0（及 v1.18.0 起的同类构建）真频谱从未启动，界面画的是 S 表合成曲线",
        "diagnosis": [
            "日志特征：反复 scope_pipe exited (frames=0, connected=False) 与 "
            "Spectrum broadcast active: S-meter fallback；真频谱成功时应有 "
            "scope_pipe: first frame received — spectrum active",
            "硬件、驱动、库都没问题（FT4222 在 USB 总线上、两个 dylib 在包内，手工用正确目录运行立刻出帧）",
            "根因：应用配置里的 FTDI 库目录是相对路径 vendor/ftdi/macos，启动器把它锚到了 "
            "Contents/MacOS/（那里什么都没有），而数据树在 Contents/Resources/ —— 于是 scope_pipe "
            "报 FTDI libraries not found 并退出",
        ],
        "solution": [
            "临时规避（立刻可用）：把配置里的 MRRC_FTDI_LIB_DIR 改成绝对路径 "
            "/Applications/MRRC-Modern.app/Contents/Resources/vendor/ftdi/macos，然后重启应用",
            "重启后刷新页面：频谱应变成真实底噪 + 信号（可在日志确认 first frame received — spectrum active）",
            "修复已进入源码（启动器改为经 _internal 解析相对路径），随下一个安装包到达用户",
        ],
        "evidence": [
            "2026-10-04 07:07:53 [WARNING] mrrc.backend.ft710.scope: scope_pipe exited (frames=0, connected=False)",
            "2026-10-04 07:01:10 [INFO] mrrc: Spectrum broadcast active: S-meter fallback, 1701 bytes/frame, 1 clients",
        ],
        "keys": ["频谱", "瀑布", "假曲线", "S 表", "FT4222", "scope_pipe", "MRRC_FTDI_LIB_DIR",
                 "真频谱"],
        "needs_code_change": True,
        "code_hint": "macos/launcher.py:load_env（相对 FTDI 目录经 runtime_path 解析）",
    },
)


def known_issue_cards() -> list[str]:
    """Rendered known-issue cards, newest first (they carry no bundle id)."""
    rows = sorted(KNOWN_ISSUES, key=lambda entry: str(entry.get("at", "")), reverse=True)
    return [render_card(entry, str(entry["id"]), problem=str(entry.get("problem", "")),
                        at=str(entry.get("at", ""))) for entry in rows]


# ── rendering ──────────────────────────────────────────────────────────────
def _items(items: list, css: str = "") -> str:
    if not items:
        return '<p class="muted">（无）</p>'
    klass = f' class="{css}"' if css else ""
    return "\n".join(f"        <li{klass}>{html.escape(redact_public(item))}</li>" for item in items)


def render_card(analysis: dict, bundle_id: str, problem: str = "",
                at: str = "") -> str:
    """One answer card (public).  Everything passes the privacy filter."""
    first_line = next((line.strip() for line in str(problem or "").splitlines()
                       if line.strip() and not line.strip().startswith("#")), "")
    title = html.escape(redact_public(first_line[:80]) if first_line
                        else analysis["verdict"][:80])
    status = str(analysis["status"])
    status_class = {"answered": "ok", "needs_fix": "warn", "need_more_info": "warn"}.get(status, "muted")
    status_label = {"answered": "已答复", "needs_fix": "已定位，待修复",
                    "need_more_info": "需要更多信息"}.get(status) or status
    evidence = _items(analysis.get("evidence", []), "ev")
    code_hint = analysis.get("code_hint", "")
    code_line = (f'\n      <p class="muted">相关代码：<code>{html.escape(redact_public(code_hint))}</code></p>'
                 if code_hint else "")
    return f"""    <article class="card" id="{html.escape(bundle_id)}">
      <header>
        <span class="id">{html.escape(bundle_id)}</span>
        <span class="badge {status_class}">{html.escape(status_label)}</span>
        <span class="muted">{html.escape(analysis['category'])} · {html.escape(at)}</span>
      </header>
      <h3>{title}</h3>
      <p class="verdict">{html.escape(redact_public(analysis['verdict']))}</p>
      <h4>诊断</h4>
      <ul>
{_items(analysis.get('diagnosis', []))}
      </ul>
      <h4>你要做的</h4>
      <ul>
{_items(analysis.get('solution', []))}
      </ul>
      <details><summary>原始证据</summary>
      <ul>
{evidence}
      </ul>
      </details>{code_line}
    </article>"""


PAGE_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MRRC Modern · 问题答复</title>
<style>
  body{background:#12121a;color:#e0e0e0;font-family:-apple-system,tahoma,sans-serif;
       margin:0 auto;padding:24px;max-width:820px;line-height:1.7}
  h1{font-size:22px;margin:0 0 6px}
  h2{font-size:17px;margin:26px 0 6px}
  h3{font-size:16px;margin:10px 0 4px}
  h4{font-size:14px;margin:14px 0 4px;color:#e8a33d}
  .muted{color:#8a8a96;font-size:13px}
  code{background:#1b1b26;border-radius:3px;padding:1px 5px;font-size:12.5px}
  a{color:#e8a33d}
  .card{background:#181824;border:1px solid #2c2c38;border-radius:6px;padding:14px;margin:16px 0}
  .card header{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
  .id{font-family:ui-monospace,monospace;font-size:12.5px;color:#e8a33d}
  .badge{font-size:12px;padding:1px 7px;border-radius:10px;border:1px solid #39414d}
  .badge.ok{color:#5ad07a;border-color:#2f6b40}
  .badge.warn{color:#e8c25a;border-color:#6b5f2f}
  .verdict{margin:6px 0 0;color:#e6e6e6}
  ul{margin:4px 0 0;padding-left:1.2rem}
  li{margin:2px 0;color:#cfcfd6}
  li.ev{font-family:ui-monospace,monospace;font-size:12px;color:#8a8a96}
  details{margin-top:10px}
  summary{cursor:pointer;color:#8a8a96;font-size:13px}
  #search{width:100%;box-sizing:border-box;background:#0f0f16;color:#fff;border:1px solid #444;
          border-radius:4px;padding:9px;font-size:14px;margin-top:8px}
  .empty{background:#181824;border:1px solid #2c2c38;border-radius:6px;padding:14px;margin:16px 0}
</style>
</head>
<body>
  <h1>🐞 问题答复</h1>
  <p class="muted">这一页发布「🐞 遇到问题」上报的分析结论。输入编号（形如
  <code>20260917-072530-ab12</code>）或关键词即可筛选。</p>
  <input id="search" type="search" placeholder="编号 / 关键词（如 录音、-9996、CI-V）" autocomplete="off">
  <p class="muted" id="count"></p>
"""

PAGE_TAIL = """
<script>
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  var box = document.getElementById('search');
  var count = document.getElementById('count');
  function applyFilter() {
    var q = (box.value || '').trim().toLowerCase();
    var shown = 0;
    cards.forEach(function (card) {
      var hit = !q || card.textContent.toLowerCase().indexOf(q) !== -1;
      card.style.display = hit ? '' : 'none';
      if (hit) { shown += 1; }
    });
    count.textContent = shown + ' / ' + cards.length + ' 条';
  }
  box.addEventListener('input', applyFilter);
  if (location.hash) {                       // #<编号> 直达并填入搜索框
    box.value = decodeURIComponent(location.hash.slice(1));
    var target = document.getElementById(box.value);
    if (target) { target.scrollIntoView(); }
  }
  applyFilter();
</script>
</body>
</html>
"""

EMPTY_STATE = """  <div class="empty">
    <h2>现在还没有答复</h2>
    <p>诊断包已经可以上传（菜单 → 🐞 遇到问题）。自动分诊每 10 分钟检查一次新包，
    结论会作为卡片出现在这一页。</p>
    <h2>着急的话</h2>
    <p>把界面上的<b>编号</b>连同问题描述发到
    <a href="https://www.vlsc.net/mrrc_modern/">MRRC Modern 主页</a>上的联系方式；
    也可以在「🐞 遇到问题」页点<b>只保存到本地</b>，把 zip 直接发给我。</p>
  </div>"""


def render_page(cards: list, generated_at: str = "") -> str:
    """The whole answers page: known issues first, then newest cards, empty state last."""
    stamp = generated_at or time.strftime("%Y-%m-%d %H:%M")
    known = known_issue_cards()
    sections: list[str] = []
    if known:
        sections.append('  <h2>已知问题（按现象查）</h2>')
        sections.extend(known)
    if cards:
        sections.append('  <h2>按编号的答复</h2>')
        sections.extend(cards)
    else:
        sections.append('  <h2>按编号的答复</h2>')
        sections.append(EMPTY_STATE)
    body = "\n".join(sections)
    nav = ('\n  <p class="muted">工作流看板：'
           '<a href="../board/">FDE 看板</a>'
           '（bug/需求分类 · 排期 · 后台实施结果）</p>\n')
    footer = f'\n  <p class="muted">最后更新：{html.escape(stamp)}（自动生成，勿手改）</p>\n'
    return PAGE_HEAD + body + nav + footer + PAGE_TAIL


# ── state (idempotency) ────────────────────────────────────────────────────
def load_state(path: "str | os.PathLike[str]") -> dict:
    """`{bundle_id: {...}}`; a missing or corrupt file is an empty state."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: "str | os.PathLike[str]", state: dict) -> None:
    """Atomic write: a killed run must not leave a half-written state file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, target)


def record_result(state: dict, bundle_id: str, analysis: dict, published: bool,
                  at: str = "") -> dict:
    """Update (in place) and return the state for one processed bundle."""
    state[bundle_id] = {
        "status": analysis.get("status", ""),
        "category": analysis.get("category", ""),
        "verdict": analysis.get("verdict", ""),
        "published": bool(published),
        "at": at or time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    return state


def answered_cards(state: dict) -> list[tuple]:
    """`[(bundle_id, record)]` sorted newest-first, published entries only."""
    rows = [(bid, rec) for bid, rec in state.items()
            if isinstance(rec, dict) and rec.get("published")]
    return sorted(rows, key=lambda row: str(row[1].get("at", "")), reverse=True)
