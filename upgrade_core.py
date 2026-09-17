"""Update channel core (spec 2026-09-17-upgrade-channel §2–§3, slice 1).

Pure logic, stdlib only, no application imports: fetch and validate the release
manifest, compare versions, and own the `state.json` schema that makes an upgrade
**provable**.  Nothing here downloads or installs — that is slice 2, and it must
not exist half-built.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path

DEFAULT_MANIFEST_URL = "https://www.vlsc.net/mrrc_modern/downloads/latest.json"
STATE_STATUSES = ("checking", "available", "up_to_date", "downloading", "installing",
                  "ok", "failed")

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def parse_version(text: str) -> tuple[int, int, int] | None:
    """`v1.17.0-rc1` -> (1, 17, 0); anything without a triple is None."""
    match = _VERSION_RE.match(str(text or "").strip())
    if not match:
        return None
    try:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:                 # unreachable after the regex; never raise here
        return None


def compare_versions(left: str, right: str) -> int:
    """-1 when left is older, 0 when equal, 1 when newer (malformed sorts oldest)."""
    a, b = parse_version(left), parse_version(right)
    if a is None and b is None:
        return 0
    if a is None:
        return -1
    if b is None:
        return 1
    return (a > b) - (a < b)


# ── manifest ───────────────────────────────────────────────────────────────
class ManifestError(ValueError):
    """The manifest is unusable — never guess around it (spec §6)."""


def _require_version(entry: dict, field: str) -> str:
    version = str(entry.get(field, "") or "")
    if parse_version(version) is None:
        raise ManifestError(f"{field} 不是版本号：{version!r}")
    return version


def parse_manifest(data) -> dict:
    """Validate one `latest.json`; raise ManifestError with a readable reason."""
    if isinstance(data, (bytes, str)):
        try:
            data = json.loads(data)
        except ValueError as e:
            raise ManifestError(f"latest.json 不是 JSON：{e}") from e
    if not isinstance(data, dict):
        raise ManifestError("latest.json 不是对象")
    latest = _require_version(data, "latest")
    installer = data.get("installer")
    if not isinstance(installer, dict):
        raise ManifestError("缺少 installer 段")
    if _require_version(installer, "version") != latest:
        raise ManifestError("installer.version 与 latest 不一致")
    url = str(installer.get("url", "") or "")
    if not url.startswith("https://"):
        raise ManifestError(f"installer.url 必须是 https：{url!r}")
    sha = str(installer.get("sha256", "") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ManifestError("installer.sha256 不是 64 位十六进制")
    size = installer.get("size")
    if not isinstance(size, int) or size <= 0:
        raise ManifestError(f"installer.size 不是正整数：{size!r}")
    previous = data.get("previous")
    if previous is not None:
        if not isinstance(previous, dict):
            raise ManifestError("previous 不是对象")
        if _require_version(previous, "version") != str(previous.get("version")):
            raise ManifestError("previous.version 不是版本号")
        if compare_versions(str(previous.get("version")), latest) == 0:
            raise ManifestError("previous 不能等于 latest（回退目标必须更旧）")
    return {
        "latest": latest,
        "installer": {"version": str(installer["version"]), "url": url, "sha256": sha,
                      "size": size},
        "previous": ({"version": str(previous["version"]),
                      "url": str(previous.get("url", "")),
                      "sha256": str(previous.get("sha256", "")).lower(),
                      "size": previous.get("size")} if isinstance(previous, dict) else None),
        "minSupported": str(data.get("minSupported", "") or ""),
        "mandatory": bool(data.get("mandatory", False)),
        "releasedAt": str(data.get("releasedAt", "") or ""),
        "notes": str(data.get("notes", "") or ""),
    }


def fetch_manifest(url: str = DEFAULT_MANIFEST_URL, timeout: int = 20) -> dict:
    """Download and validate the manifest (raises ManifestError / OSError)."""
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        blob = response.read()
    return parse_manifest(blob)


# ── check ──────────────────────────────────────────────────────────────────
def check(current: str, manifest: dict) -> dict:
    """Compare an installed version against a manifest (spec §2 D2/D5)."""
    latest = manifest["latest"]
    newer = compare_versions(current, latest) < 0
    below_supported = bool(manifest.get("minSupported")) and \
        compare_versions(current, manifest["minSupported"]) < 0
    return {
        "available": newer,
        "current": current,
        "latest": latest,
        "mandatory": bool(manifest.get("mandatory", False)),
        "belowMinSupported": below_supported,
        "releasedAt": manifest.get("releasedAt", ""),
        "notes": manifest.get("notes", ""),
        "url": manifest["installer"]["url"],
        "sha256": manifest["installer"]["sha256"],
        "size": manifest["installer"]["size"],
    }


def check_quietly(current: str, url: str = DEFAULT_MANIFEST_URL, timeout: int = 20) -> dict:
    """A maintenance check must not nag: any failure is reported, not raised."""
    try:
        return check(current, fetch_manifest(url, timeout=timeout))
    except (OSError, ManifestError) as e:
        return {"available": False, "current": current, "reason": f"{type(e).__name__}: {e}"}


# ── state: the upgrade's only proof of success (spec §2 D3) ────────────────
def read_state(path) -> dict:
    """`{lastCheck, lastResult:{status, version, at, detail}}`; corrupt reads as empty."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path, state: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, target)


def record_check(state: dict, result: dict, at: str = "") -> dict:
    state = dict(state)
    state["lastCheck"] = {"at": at or time.strftime("%Y-%m-%dT%H:%M:%S"),
                          "available": bool(result.get("available")),
                          "latest": result.get("latest", ""),
                          "reason": result.get("reason", "")}
    return state


def record_result(state: dict, status: str, version: str, detail: str = "", at: str = "") -> dict:
    """Record an upgrade outcome.  `ok` is only ever written by the NEW version
    at boot (spec §2 D3) — an old version that "launched the installer" records
    `installing`, which is explicitly not success."""
    if status not in STATE_STATUSES:
        raise ValueError(f"未知状态：{status!r}")
    state = dict(state)
    state["lastResult"] = {"status": status, "version": str(version), "detail": str(detail),
                           "at": at or time.strftime("%Y-%m-%dT%H:%M:%S")}
    return state


def confirm_boot(state: dict, running_version: str, at: str = "") -> dict:
    """Called by the app at startup: if a pending install targeted this version and
    this *is* that version, the upgrade succeeded — record `ok` (spec §2 D3)."""
    pending = (state.get("pendingInstall") or {}) if isinstance(state.get("pendingInstall"), dict) else {}
    target = str(pending.get("version", "") or "")
    if target and compare_versions(running_version, target) == 0:
        state = record_result(state, "ok", running_version, "新版本已启动（自证）", at=at)
        state.pop("pendingInstall", None)
    return state


def is_success(state: dict, version: str = "") -> bool:
    """The only acceptance question: did the *new* version report itself up?"""
    result = state.get("lastResult") or {}
    if result.get("status") != "ok":
        return False
    return not version or compare_versions(str(result.get("version", "")), version) == 0


def mark_install_started(state: dict, version: str, at: str = "") -> dict:
    """Before handing off to the installer: record the intent, not the outcome."""
    state = record_result(state, "installing", version, "已拉起安装器（尚未证明成功）", at=at)
    state["pendingInstall"] = {"version": str(version), "at": at or time.strftime(
        "%Y-%m-%dT%H:%M:%S")}
    return state
