"""NanoVNA-H 固件升级（spec 2026-10-18 §4.10，AD-030）。

**为什么这件事是安全的**：`reset dfu` 走的是 `boardDFUEnter()`（固件源码：
`__set_MSP(STM32F303xC_SYSTEM_MEMORY)`），即跳进芯片**系统存储区**里的 ROM
bootloader。那段代码不可擦写，所以最坏情况只是 App 区烧了一半、设备重启后停在
DFU，再刷一次即恢复 —— 不存在刷成砖头无法恢复。代价是刷坏后设备要人工按一下
电源开关。

**为什么这件事需要人**：H / H4 固件有 MS / SI / ZK 三个变体（MS5351 vs Si5351
时钟芯片；ZK 是 2025 年 NE602A 换料后的板子），那是**编译期**开关，运行时任何
命令都读不出来（`info` 只说 `Board: NanoVNA-H 4`）。刷错变体不会砖，但时钟/RF
会不对，所以：首次必须显式选、之后记住、刷完用开路端口 |S11| 做建议值自检。

**成功的判据不是 `dfu-util` 的返回码**，而是**刷后设备自己报出来的版本**
（同 AD-022 的思路：让被升级的东西自己作证）。

本模块不 import 任何应用模块（除了出站 HTTPS 必走的 `net_tls`），所有副作用都
是可注入的回调，所以整套流程能在没有设备、没有 dfu-util 的机器上测。
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/hugen79/NanoVNA-H/releases"
# 六个变体：板型 × 时钟/混频器。名字必须与 release 资产前缀一致。
VARIANTS = {
    "h4-ms": {"board": "H4", "tag": "MS", "label": "NanoVNA-H4 · MS5351"},
    "h4-si": {"board": "H4", "tag": "SI", "label": "NanoVNA-H4 · Si5351"},
    "h4-zk": {"board": "H4", "tag": "ZK", "label": "NanoVNA-H4 · ZEETK NE602A"},
    "h-ms": {"board": "H", "tag": "MS", "label": "NanoVNA-H · MS5351"},
    "h-si": {"board": "H", "tag": "SI", "label": "NanoVNA-H · Si5351"},
    "h-zk": {"board": "H", "tag": "ZK", "label": "NanoVNA-H · ZEETK NE602A"},
}

DFU_ADDRESS = "0x08000000:leave"
FLASH_TIMEOUT_S = 180
DFU_APPEAR_S = 10
APP_RETURN_S = 25


class FlashError(Exception):
    """升级流程的失败（带一句能行动的话）。"""


@dataclass
class Asset:
    name: str
    url: str
    size: int
    sha256: str          # 空字符串 = 上游没给 digest


@dataclass
class Release:
    tag: str
    published: str
    assets: list = field(default_factory=list)      # list[Asset]

    def binary(self, variant: str) -> Optional[Asset]:
        return pick_asset(self, variant)


# ── 版本与资产（纯函数）───────────────────────────────────────────

def parse_releases(payload) -> list[Release]:
    """GitHub releases JSON → 结构化列表（新到旧，与上游顺序一致）。"""
    out: list[Release] = []
    for item in payload if isinstance(payload, list) else []:
        tag = str(item.get("tag_name") or "")
        if not tag:
            continue
        assets: list[Asset] = []
        for a in item.get("assets") or []:
            digest = str(a.get("digest") or "")
            sha = digest.split(":", 1)[1] if digest.lower().startswith("sha256:") else ""
            assets.append(Asset(name=str(a.get("name") or ""),
                                url=str(a.get("browser_download_url") or ""),
                                size=int(a.get("size") or 0),
                                sha256=sha))
        out.append(Release(tag=tag, published=str(item.get("published_at") or ""),
                           assets=assets))
    return out


def asset_prefix(variant: str) -> str:
    spec = VARIANTS.get(variant)
    if spec is None:
        raise ValueError("unknown variant: %r" % (variant,))
    return "NanoVNA-%s-%s_" % (spec["board"], spec["tag"])


def pick_asset(release: Release, variant: str) -> Optional[Asset]:
    """从一次 release 里挑出该变体的 `.bin`。`.dfu`/`.hex` 不用于 dfu-util -D。"""
    prefix = asset_prefix(variant)
    for asset in release.assets:
        if asset.name.startswith(prefix) and asset.name.endswith(".bin"):
            return asset
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(262144), b""):
            h.update(chunk)
    return h.hexdigest()


def download_asset(asset: Asset, dest_dir: Path,
                   opener: Optional[Callable] = None) -> Path:
    """下载并校验 SHA-256。上游没给 digest 时拒绝安装（没有依据就不写设备）。"""
    if not asset.sha256:
        raise FlashError("release asset %s has no sha256 digest — refusing to flash" % asset.name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / asset.name
    if dest.exists() and sha256_file(dest) == asset.sha256:
        return dest                                     # 已经下过且校验过
    if opener is None:
        import net_tls
        opener = net_tls.urlopen
    tmp = dest.with_suffix(dest.suffix + ".part")
    with opener(asset.url, timeout=60) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    got = sha256_file(tmp)
    if got != asset.sha256:
        tmp.unlink(missing_ok=True)
        raise FlashError("sha256 mismatch for %s: got %s, expected %s"
                         % (asset.name, got[:12], asset.sha256[:12]))
    tmp.replace(dest)
    return dest


# ── USB 在场判断（ROM DFU ↔ 应用）─────────────────────────────────

def usb_present(vid: str, pid: str, root: Path = Path("/sys/bus/usb/devices")) -> bool:
    """在 sysfs 里找 `vid:pid`。`lsusb` 需要装 usbutils，sysfs 永远在。"""
    try:
        entries = list(root.iterdir())
    except OSError:
        return False
    for entry in entries:
        try:
            v = (entry / "idVendor").read_text(encoding="utf-8").strip().lower()
            p = (entry / "idProduct").read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if v == vid.lower() and p == pid.lower():
            return True
    return False


def wait_for_usb(vid: str, pid: str, want: bool = True, timeout: float = DFU_APPEAR_S,
                 probe: Callable[[], bool] = None,
                 sleep: Callable[[float], None] = time.sleep) -> bool:
    probe = probe or (lambda: usb_present(vid, pid))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe() is want:
            return True
        sleep(0.25)
    return probe() is want


def dfu_args(image: Path) -> list[str]:
    return ["dfu-util", "-a", "0", "-s", DFU_ADDRESS, "-D", str(image)]


def run_dfu(image: Path, runner: Optional[Callable] = None) -> tuple[int, str]:
    """跑 dfu-util，返回 (returncode, 合并输出)。"""
    if runner is None:
        def runner(args):                                   # noqa: ANN001
            return subprocess.run(args, capture_output=True, text=True,
                                  timeout=FLASH_TIMEOUT_S)
    try:
        proc = runner(dfu_args(image))
    except FileNotFoundError:
        raise FlashError("dfu-util is not installed on this box")
    except subprocess.TimeoutExpired:
        raise FlashError("dfu-util timed out after %d s" % FLASH_TIMEOUT_S)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, out


# ── 状态机 ───────────────────────────────────────────────────────

STATES = ("idle", "checking", "dfu_enter", "flashing", "verifying",
          "self_check", "done", "failed", "variant_mismatch")


@dataclass
class FlashHooks:
    """流程需要的全部副作用。测试里全换成假的，真机上全部是设备操作。"""
    enter_dfu: Callable[[], None]
    wait_dfu: Callable[[], bool]
    run_dfu: Callable[[Path], tuple]
    wait_app: Callable[[], bool]
    read_version: Callable[[], str]
    self_check: Callable[[], dict]


@dataclass
class FlashJob:
    """一次升级。**一次只能有一个** —— 由调用方（server）保证。"""
    image: Path
    expected_version: str
    hooks: FlashHooks
    state: str = "idle"
    detail: str = ""
    output: str = ""
    before_version: str = ""
    after_version: str = ""
    self_check_result: dict = field(default_factory=dict)

    def _set(self, state: str, detail: str = "") -> None:
        self.state = state
        if detail:
            self.detail = detail
        log.info("nano flash: %s%s", state, (" — " + detail) if detail else "")

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "detail": self.detail,
            "image": self.image.name,
            "expected_version": self.expected_version,
            "before_version": self.before_version,
            "after_version": self.after_version,
            "self_check": self.self_check_result,
            "output": self.output[-4000:],
        }

    def run(self) -> dict:
        try:
            self._set("dfu_enter", "让设备跳进 ROM DFU")
            self.before_version = self.hooks.read_version()
            self.hooks.enter_dfu()
            if not self.hooks.wait_dfu():
                raise FlashError("设备没有进入 DFU 模式（没看到 0483:df11）")
            self._set("flashing", "写入 %s" % self.image.name)
            rc, out = self.hooks.run_dfu(self.image)
            self.output = out
            if rc != 0:
                raise FlashError("dfu-util 返回 %d" % rc)
            self._set("verifying", "等设备重新枚举并自报版本")
            if not self.hooks.wait_app():
                raise FlashError("设备没有回到应用模式（没看到 0483:5740）")
            self.after_version = self.hooks.read_version()
            if self.expected_version and self.after_version != self.expected_version:
                # 版本没变 = 这次升级没有发生。不许说成功。
                self._set("failed", "刷后版本是 %s，期望 %s" % (self.after_version,
                                                              self.expected_version))
                return self.snapshot()
            self._set("self_check", "开路端口自检（建议值，不作为判据）")
            try:
                self.self_check_result = self.hooks.self_check()
            except Exception as exc:                       # noqa: BLE001
                self.self_check_result = {"error": str(exc)}
            if self.self_check_result.get("variant_suspect"):
                self._set("variant_mismatch",
                          "自检 |S11| 离 1.0 太远，变体可能选错了（可以换一个再刷）")
                return self.snapshot()
            self._set("done", "%s → %s" % (self.before_version, self.after_version))
        except FlashError as exc:
            self._set("failed", str(exc))
        except Exception as exc:                           # noqa: BLE001
            log.exception("nano flash: unexpected failure")
            self._set("failed", "未预期的失败：%s" % exc)
        return self.snapshot()


def record(path: Path, snapshot: dict, *, variant: str, tag: str, sha256: str) -> None:
    """升级凭证：镜像名、SHA-256、刷前/刷后版本、时间、自检值。"""
    payload = dict(snapshot)
    payload.update({"variant": variant, "tag": tag, "sha256": sha256,
                    "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
