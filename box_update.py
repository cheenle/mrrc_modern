"""盒子自更新：从 `/settings` 点一次，把源码树换到新版本。

为什么是源码而不是安装包：W103D 上的服务端**本来就是一份源码树**
（`/opt/mrrc_modern`，由 `install.sh` 建 venv、由 `mrrc-update` 就地更新），
没有 PyInstaller 冻结层。所以「升级」= 下载一个**已按仓库排除清单 stage 过的
tar.gz** → 校验 → 覆盖 → pip → 重编 FT8 → 重启，与 `linux/mrrc_update.sh`
同一条路径，只是把「谁来跑」从 SSH 换成浏览器。

这个模块**只做纯逻辑 + 可注入的 I/O**：下载/解包/覆盖/写版本号/拉起看门狗，
每一步都能在测试里替换。需要 root 的只有「重启服务」一件事，它走既有的
`box_helper` 套接字（新增一个无参数的 `restart-service` 动词），不给服务端加
任何提权通道。

现场约束（2026-10-18，W103D）：用户没有桌面、没有 Linux 能力、只有 `/settings`
一页；所以：

* 一切判断都在盒子本地做完（版本比较、SHA、磁盘、发射中），页面上只显示结论；
* **失败必须能自己退回去**：覆盖前先备份，重启后由看门狗探活，活不过来就还原；
* 发射中（PTT / TUNE / CQ / FT8）**拒绝升级**——那条链路里重启服务等于掐断正在发的波。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import net_tls

#: 升级过程的最大体积（tar.gz）。一份 stage 过的树约 24 MB，留足余量给增长。
MAX_PACKAGE_BYTES = 200 * 1024 * 1024
#: 覆盖前至少要有这么多空闲；一次升级会同时存在旧树备份 + 新树。
MIN_FREE_MB = 400
#: 重启后等多久去探活；超时即回滚。
WATCHDOG_AFTER_S = 90
#: 探活之后的观测窗（容器/慢卡上首帧可能晚）。
WATCHDOG_POLL_S = 30

#: 覆盖时**不动**的目录：要么是本机状态，要么是运行期数据。
#:
#: `ft8-slots` 是现场诊断用的槽音频（一台跑过几天的盒子能有 750 MB —— 2026-10-18
#: 实测），把它算进备份会让「覆盖前备份」这件事自己先把磁盘吃掉。
KEEP = ("venv", "logs", "recordings", ".local", "env", "fleet", "data",
        "ft8-slots", "__pycache__", "ft8/dsp/build/CMakeFiles")

STATE_IDLE, STATE_DOWNLOAD, STATE_VERIFY, STATE_APPLY, STATE_RESTART, \
    STATE_DONE, STATE_FAILED = ("idle", "downloading", "verifying", "applying",
                                "restarting", "done", "failed")

_state: dict = {"state": STATE_IDLE, "version": "", "detail": "",
                "started": 0.0, "finished": 0.0, "rolledBack": False}


# ── where things live ──────────────────────────────────────────────────────
def update_dir(home: Optional[Path] = None) -> Path:
    """Where packages, backups and the last outcome live.

    Under the service user's own data directory (`~/.local/share/MRRC-Modern/`),
    the same place the certificates and the tunnel config live: `/opt` itself is
    root-owned on a shipped box, and the updater must not need root to download
    a package. `stage_over` skips `.local` for the same reason it skips `venv`.
    """
    if os.environ.get("MRRC_UPDATE_DIR"):
        return Path(os.environ["MRRC_UPDATE_DIR"])
    return Path(home or "/opt/mrrc_modern") / ".local" / "share" / "MRRC-Modern" / "update"


def state_path(home: Optional[Path] = None) -> Path:
    return update_dir(home) / "last.json"


def state() -> dict:
    """The live state, for the page to poll."""
    return dict(_state)


def _set(state_name: str, *, version: str = "", detail: str = "") -> None:
    _state.update({"state": state_name, "version": version or _state["version"],
                   "detail": detail})
    if state_name in (STATE_DONE, STATE_FAILED):
        _state["finished"] = time.time()
    elif state_name != STATE_IDLE:
        _state["started"] = _state["started"] or time.time()


def reset() -> None:
    _state.update({"state": STATE_IDLE, "version": "", "detail": "",
                   "started": 0.0, "finished": 0.0, "rolledBack": False})


# ── package handling ──────────────────────────────────────────────────────
def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, dest: Path, *, opener: Callable = net_tls.urlopen,
             max_bytes: int = MAX_PACKAGE_BYTES) -> int:
    """Fetch `url` to `dest`, refusing anything suspiciously large."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with opener(request, timeout=60) as response, open(dest, "wb") as out:
        while True:
            block = response.read(1 << 20)
            if not block:
                break
            written += len(block)
            if written > max_bytes:
                raise ValueError(f"更新包超过上限 {max_bytes} 字节，已中止")
            out.write(block)
    return written


def verify(tarball: Path, expected_sha: str, expected_size: int) -> str:
    """Why this package is not the one the manifest promised ("" when it is).

    Both checks matter: the size catches a truncated download in one second, the
    hash catches everything else.
    """
    size = tarball.stat().st_size
    if expected_size and size != expected_size:
        return f"大小不对：{size} 字节，清单说 {expected_size}"
    actual = sha256_of(tarball)
    if expected_sha and actual.lower() != expected_sha.lower():
        return f"SHA-256 不对：{actual[:16]}…，清单说 {expected_sha[:16]}…"
    return ""


def extract(tarball: Path, dest: Path) -> int:
    """Unpack a package into `dest`; refuses absolute paths and `..`.

    A tar is an archive **with paths in it**, so "extract here" is a write
    primitive: a member named `../../etc/shadow` would land outside the tree. The
    package is ours, but this runs as the service user on a customer's box, so the
    check costs nothing and removes the argument.
    """
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    count = 0
    with tarfile.open(tarball, "r:gz") as archive:
        for member in archive.getmembers():
            target = (dest / member.name).resolve()
            if member.name.startswith("/") or os.path.isabs(member.name) or \
                    (root != target and root not in target.parents):
                raise ValueError(f"更新包里有越界路径：{member.name!r}")
            if member.issym() or member.islnk():
                link = (dest / member.name).parent / member.linkname
                if root not in link.resolve().parents and link.resolve() != root:
                    raise ValueError(f"更新包里有越界链接：{member.name!r}")
        archive.extractall(dest)
        count = len(archive.getmembers())
    return count


def _skip(rel: str) -> bool:
    rel = rel.replace(os.sep, "/")
    for keep in KEEP:
        if rel == keep or rel.startswith(keep + "/"):
            return True
    return False


def stage_over(src: Path, home: Path) -> int:
    """Copy the staged tree over the installed one. Never deletes anything.

    No delete on purpose: the update must not be able to remove a file the
    running product still imports (and `linux/mrrc_update.sh` made the same
    choice for the same reason).
    """
    copied = 0
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        dirs[:] = [d for d in dirs if not _skip(str(rel_root / d))]
        target_dir = home / rel_root
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            rel = str(rel_root / name)
            if _skip(rel):
                continue
            source = Path(root) / name
            target = home / rel
            shutil.copy2(source, target)
            copied += 1
    return copied


def version_in(tree: Path) -> str:
    """The version this package claims: `version.txt`, else the CHANGELOG top."""
    stamp = tree / "version.txt"
    try:
        text = stamp.read_text(encoding="utf-8").strip()
        if text:
            return text.splitlines()[0].strip()
    except OSError:
        pass
    import re
    try:
        text = (tree / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"##\s*\[v(\d+\.\d+\.\d+)\]", text)
    return match.group(1) if match else ""


def write_version(home: Path, version: str) -> None:
    if not version:
        return
    (home / "version.txt").write_text(version + "\n", encoding="utf-8")
    (home / "VERSION").write_text(version + "\n", encoding="utf-8")


def backup(home: Path, version: str, *, keep: int = 2) -> Path:
    """Tar the current tree (code only) so the watchdog can put it back."""
    out_dir = update_dir(home)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"rollback-{version or 'unknown'}.tar.gz"
    skip = set(KEEP)
    with tarfile.open(target, "w:gz") as archive:
        for entry in sorted(home.iterdir()):
            if entry.name in skip or entry.name.startswith("."):
                continue
            archive.add(entry, arcname=entry.name)
    old = sorted(out_dir.glob("rollback-*.tar.gz"), key=lambda p: p.stat().st_mtime)
    for stale in old[:-keep]:
        try:
            stale.unlink()
        except OSError:
            pass
    return target


def restore(home: Path, tarball: Path) -> None:
    with tarfile.open(tarball, "r:gz") as archive:
        archive.extractall(home)


# ── the watchdog ──────────────────────────────────────────────────────────
def watchdog_command(home: Path, rollback: Path, *, after: int = WATCHDOG_AFTER_S,
                     poll: int = WATCHDOG_POLL_S) -> str:
    """A detached shell that restarts the *old* tree when the new one never wakes.

    There is no "rollback" state machine above this: if the new code cannot serve
    `/api/health` within `after + poll` seconds, the backup is unpacked and the
    service is restarted again. The marker file is what the page reads to say
    "回滚了，原因见日志".
    """
    marker = update_dir(home) / "last.json"
    # The command is a **POSIX shell** script for the box, so every path in it must
    # be POSIX-spelled.  `str(Path("/tmp/x"))` is `\tmp\x` when this code is
    # exercised on Windows (the suite runs there too) — the shell then gets a
    # literal backslash path and the rollback silently does nothing.
    home_posix = Path(home).as_posix()
    rollback_posix = Path(rollback).as_posix()
    marker_posix = Path(marker).as_posix()
    return (
        f"sleep {after}; "
        f"ok=0; for i in 1 2 3; do "
        f"  if curl -skf --max-time 5 https://127.0.0.1:8888/api/health >/dev/null; then ok=1; break; fi; "
        f"  sleep {max(5, poll // 3)}; "
        f"done; "
        f"if [ \"$ok\" = 0 ]; then "
        f"  tar -xzf {rollback_posix} -C {home_posix} && "
        f"  printf '%s' '{{\"rolledBack\": true}}' > {marker_posix}; "
        f"  {_restart_command(home)}; "
        f"fi"
    )


def _restart_command(home: Path) -> str:
    """Restart without root: the service runs as us, and its unit is Restart=on-failure.

    `systemctl restart` would need the root helper; killing our own process does
    not, and systemd brings it straight back (Restart=on-failure, RestartSec=3).
    """
    return "pkill -f 'venv/bin/python server.py' || true"


def spawn_watchdog(home: Path, rollback: Path) -> None:
    """Start the watchdog detached, so it outlives the restart it triggers."""
    subprocess.Popen(["/bin/sh", "-c", watchdog_command(home, rollback)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True,
                     cwd=str(home))


# ── the whole thing ───────────────────────────────────────────────────────
def apply(plan: dict, home: Path, *, runner: Optional[Callable] = None,
          opener: Callable = net_tls.urlopen,
          spawn: Callable = None) -> dict:
    """Download → verify → unpack → back up → stage → stamp → restart.

    `runner` is the "ask the root helper to restart the service" call; it is
    injected so the whole flow is testable without systemd.
    """
    version = str(plan.get("version") or "")
    work = update_dir(home)
    work.mkdir(parents=True, exist_ok=True)
    tarball = work / f"package-{version or 'new'}.tar.gz"
    staging = work / f"staged-{version or 'new'}"

    _set(STATE_DOWNLOAD, version=version)
    size = download(plan["url"], tarball, opener=opener)
    _set(STATE_VERIFY, detail=f"{size} 字节")
    problem = verify(tarball, str(plan.get("sha256") or ""), int(plan.get("size") or 0))
    if problem:
        _set(STATE_FAILED, detail=problem)
        return state()

    _set(STATE_APPLY, detail="解包")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    extract(tarball, staging)

    # 包里的树有两种形状：整棵树（version.txt 在根）或带一层目录。
    tree = staging
    if not (staging / "server.py").is_file():
        children = [p for p in staging.iterdir() if p.is_dir()]
        for child in children:
            if (child / "server.py").is_file():
                tree = child
                break

    _set(STATE_APPLY, detail="备份当前版本")
    rollback = backup(home, version_in(home))
    _set(STATE_APPLY, detail="覆盖代码")
    stage_over(tree, home)
    write_version(home, version_in(tree) or version)

    _set(STATE_RESTART, detail="重启服务（约 10 秒；90 秒内没起来会自动回滚）")
    spawn = spawn or spawn_watchdog
    spawn(home, rollback)
    if runner is not None:
        runner()
    _set(STATE_DONE, detail="已重启，页面会重新连接")
    return state()
