"""Support diagnostics bundle: collect, redact, summarise, package.

Spec: docs/superpowers/specs/2026-09-17-support-bundle-design.md (§4–§6).
Stdlib only and no application imports on purpose: the module stays unit
testable in isolation and can be hot-fixed later without a release.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

REDACTED = "<redacted>"

# Two independent passes (spec §6).  The key allow-list is the security
# boundary; the value pass only cleans text that is collected anyway (a
# password pasted into a log line, a `?token=` inside a URL).
#
# The leading `(?<![A-Za-z0-9])` (not `\b`) is deliberate: `_` is a word
# character, so `\b` FAILS to match our own `MRRC_WEB_PASSWORD=` (the case this
# pass exists for) while the lookbehind catches it and still ignores words that
# merely end in one of these tokens (`compass=`).
SECRET_VALUE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(cookie[_-]?secret|pass(?:word|wd)?|secret|token|api[_-]?key|credential)\b"
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


# ── log discovery and bounded tails (spec §5/§6) ────────────────────────────
DEFAULT_TAIL_BYTES = 2 * 1024 * 1024


def tail_lines(path, max_bytes: int = DEFAULT_TAIL_BYTES) -> str:
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


def resolve_log_files(log_dir: str | os.PathLike[str],
                      data_dir: str | os.PathLike[str] = "",
                      install_dir: str | os.PathLike[str] = "") -> dict:
    """Map bundle role -> existing log file (spec §5).

    Roles: server / server-prev / stdout / stdout-prev / launcher / legacy.
    The desktop launcher puts logs under the user data dir; systemd and
    `start.sh` write into the install dir, which is also where a hand-started
    server writes when MRRC_LOG_DIR is unset.
    """
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


# ── automatic triage summary (spec §6; patterns verified against this repo) ──
# Every pattern below matches a line this project actually prints (server.py,
# audio_handler.py, scope_producer.py, recorder.py, backends/*/backend.py).
# The sibling project's markers (🎧 音频健康, IOLoop stall) do NOT exist here.
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


def summarize_log(text: str, freshness_hours: float | None = None) -> str:
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
        minutes = max(1, round(freshness_hours * 60))
        conclusions.append(f"数据新鲜度：日志写于 {minutes} 分钟前")

    for label, pattern in (("音频设备", SUMMARY_PATTERNS[2][1]),
                           ("录音写入", SUMMARY_PATTERNS[6][1])):
        hits = [ln for ln in lines if pattern.search(ln)]
        if hits:
            conclusions.append(f"{label}：{len(hits)} 条命中（见下方明细）")

    codes = sorted({c for c in PORT_AUDIO_CODE_RE.findall(text or "")})
    if "-9996" in (text or "") or "no default output device" in (text or ""):
        extra = f"（日志内音频错误码：{', '.join(codes[:5])}）" if codes else ""
        conclusions.append("音频设备：PortAudio 报 -9996（找不到可用设备）" + extra +
                           " —— 若 env.json 的 audio.devices 为空，说明本机没有音频设备"
                           "（虚拟机常见），纯 Web 模式属预期")

    if re.search(r"Device not configured|Errno 6", text or ""):
        conclusions.append("串口恢复：出现 ENXIO/「Device not configured」—— USB 串口桥掉线后重枚举，"
                           "检查电缆/供电/勿用无源 HUB")

    if "S-meter fallback" in (text or ""):
        conclusions.append("频谱：出现 S 表合成回退 —— 真实频谱（FT4222 / CI-V 0x27）未工作")

    if "enable TX after" in (text or ""):
        conclusions.append("TX 门禁：未验证机型拒绝发射（MRRC_ALLOW_UNVERIFIED_TX=1 才可开）"
                           " —— 属预期行为")

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


# ── version + environment snapshot (spec §6; version authority for sub-project 3) ──
_ISS_VERSION_RE = re.compile(r'MyAppVersion\s+"([^"]+)"')
_CHANGELOG_VERSION_RE = re.compile(r"^##\s*\[?v?([0-9]+\.[0-9]+\.[0-9]+)", re.M)
VERSION_UNKNOWN = "unknown"


def detect_version(runtime_dir: str | os.PathLike[str], resource_dir="") -> str:
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


# ── packaging + retention (spec §6/§7) ──────────────────────────────────────
BUNDLE_ID_RE = r"^\d{8}-\d{6}-[0-9a-f]{4}$"
DEFAULT_MAX_TOTAL_BYTES = 20 * 1024 * 1024
# Largest tail first; the bundle degrades instead of failing when a log is huge,
# because the receiver refuses anything over 20 MB (spec §7).
_TAIL_LADDER = (DEFAULT_TAIL_BYTES, 512 * 1024, 128 * 1024)

README_TEXT = (
    "本包由 MRRC Modern「🐞 遇到问题」生成。\n\n"
    "包含：日志尾部、脱敏配置快照、环境/电台/音频状态快照、自动体检摘要\n"
    "      （先看 diagnostics/summary.txt）。\n"
    "不含：登录密码、证书私钥、任何令牌，也不含录音、记忆频道与天调学习值\n"
    "      （生成时按白名单裁剪 + 值替换，替换次数写在 manifest.json）。\n"
)


def new_bundle_id() -> str:
    """`YYYYmmdd-HHMMSS-xxxx` — same shape as the receiver's id validation."""
    import secrets
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def is_valid_bundle_id(bundle_id: str) -> bool:
    return bool(re.fullmatch(BUNDLE_ID_RE, str(bundle_id or "")))


def bundle_path(out_dir: str | os.PathLike[str], bundle_id: str) -> Path:
    """Absolute path of a built bundle; an empty Path when the id is not valid."""
    if not is_valid_bundle_id(bundle_id):
        return Path("")
    return Path(out_dir) / f"support-{bundle_id}.zip"


def prune_bundles(out_dir: str | os.PathLike[str], keep: int = 5) -> list:
    """Delete older support-*.zip files; never touches anything else."""
    out = Path(out_dir)
    if not out.is_dir():
        return []
    zips = sorted(out.glob("support-*.zip"), key=lambda p: p.name)
    removed: list = []
    for path in (zips[:-keep] if keep > 0 else zips):
        try:
            path.unlink()
            removed.append(str(path))
        except OSError:
            pass
    return removed


def build_bundle(out_dir: str | os.PathLike[str], *, problem: str = "", contact: str = "",
                 env: dict | None = None, log_files: dict | None = None, config_text: str = "",
                 extra_files: dict | None = None, manifest_extra: dict | None = None,
                 initial_warnings: list | None = None,
                 max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES) -> dict:
    """Write one support zip; return {id, path, size, files, redactions, warnings}.

    Per-file problems become warnings, never failures: a bundle with nothing but
    a manifest still beats "collect failed" (spec §11 minimal-bundle degradation).
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

    collectible: dict = {}
    for role, path in (log_files or {}).items():
        if is_collectable(role):
            collectible[role] = path
        else:
            warnings.append(f"跳过受限文件：{role}")

    payloads: dict = {}
    payload_hits: dict = {}
    for rung in _TAIL_LADDER:
        payloads.clear()
        payload_hits.clear()
        total = 0
        for role, path in collectible.items():
            text = tail_lines(path, max_bytes=rung)
            if not text:
                continue
            cleaned, hits = redact_text(text)
            payloads[role] = cleaned
            payload_hits[role] = hits
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

    # Counted after the ladder: it re-reads every file per rung, so counting
    # inside would multiply the number by the number of rungs tried.
    redactions += sum(payload_hits.values())

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
        for role, path in collectible.items():
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
            "redactions": redactions,
            "warnings": warnings,
        }
        manifest.update(manifest_extra or {})
        add("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    prune_bundles(out, keep=5)
    return {"id": bundle_id, "path": str(zip_path), "size": os.path.getsize(zip_path),
            "files": collected, "redactions": redactions, "warnings": warnings}
