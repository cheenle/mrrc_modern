#!/usr/bin/env python3
"""The five system verbs the web pages may ask for, and nothing else.

Why a separate root process (design §7.3): `mrrc` may *read* NetworkManager, but
`hostnamectl`, `timedatectl` and `systemctl reboot` are root's. Shrinking that
authority to a fixed verb table is cheaper to audit than a sudoers file, and it
keeps the Wi-Fi password out of a second channel — `mrrc` writes NetworkManager
itself (the polkit rule installed beside this file is what lets it), so a PSK
never comes near this socket.

Transport: one line of JSON in, one line out, over /run/mrrc/box-helper.sock
(0660 root:mrrc; the directory comes from the unit's RuntimeDirectory= with
Group=mrrc). The peer's uid comes from SO_PEERCRED and must be `mrrc` — a socket
mode is a check the kernel makes at connect() time, and this is the check it does
not make.

No shell, ever: every verb is an argv array built here from a validated argument,
so there is nothing for a metacharacter to be interpreted by. Nothing a client
sends is ever concatenated into a command string.

Runs as root under mrrc-box-helper.service. stdlib only — it is copied into the
image beside the other linux/ entry points and must import cleanly with no venv
site-packages beyond what the image has.
"""
from __future__ import annotations

import json
import os
import re
import socket
import struct
import subprocess
import sys
from pathlib import Path
from typing import Optional

SOCKET_PATH = "/run/mrrc/box-helper.sock"
SOCKET_MODE = 0o660
#: The one uid that may ask. server.py runs as this user (User=mrrc in
#: mrrc-modern.service); nothing else on the box has any business here.
PEER_USER = "mrrc"
MAX_LINE = 4096
COMMAND_TIMEOUT_S = 30

HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
ZONEINFO = "/usr/share/zoneinfo"


def log(message: str) -> None:
    """journald picks this up (`journalctl -u mrrc-box-helper`). No verb here
    takes a sensitive argument, so there is nothing to redact — and nothing is
    logged beyond the verb and its program's own error text."""
    print(f"box-helper: {message}", flush=True)


# ── the verb table ──────────────────────────────────────────────────
def zone_ok(zone: str) -> bool:
    """A real zone under /usr/share/zoneinfo, with no way out of it.

    `timedatectl` would reject a bad name anyway. The check is here because this
    argument reaches a root process, and "it fails safely downstream" is not the
    same as "it was checked".
    """
    if not zone or zone != zone.strip() or zone.startswith("/"):
        return False
    if ".." in zone or "\n" in zone or "\r" in zone:
        return False
    root = os.path.realpath(ZONEINFO)
    target = os.path.realpath(os.path.join(root, zone))
    return (target == root or target.startswith(root + os.sep)) \
        and os.path.isfile(target)


def argv_for(verb: str, args: list) -> Optional[list]:
    """The command for this verb, or None when the verb or its arguments lie.

    Validation lives here rather than at the call site so there is exactly one
    place to read when asking "what can this socket make the box do".
    """
    if verb == "reboot" and not args:
        return ["systemctl", "reboot"]
    if verb == "avahi-reload" and not args:
        return ["systemctl", "reload", "avahi-daemon"]
    if verb == "restart-bluetooth" and not args:
        # 蓝牙 HID 的补救：配对成功、链路也通，但 bluez 手里的 GATT 数据库是残缺的
        # （2026-10-19 现场：`No attribute found within the given range`，键盘设备
        # 0x0E 一路重试、dmesg 里实例号一路涨），重配 bond 治不了，重启 bluetoothd
        # 治得了。服务用户没有 root，这是它唯一能走的路；参数列表为空即全部约束。
        return ["systemctl", "restart", "bluetooth"]
    if verb == "set-hostname" and len(args) == 1 and HOSTNAME_RE.match(args[0]):
        return ["hostnamectl", "set-hostname", "--static", args[0]]
    if verb == "set-timezone" and len(args) == 1 and zone_ok(args[0]):
        return ["timedatectl", "set-timezone", args[0]]
    return None


#: The only accounts `set-password` will touch: the two a shipping image sets.
#: A free-form user name here would turn this socket into "become anyone", which
#: is not a verb table any more.
PASSWORD_USERS = ("root", "mrrc")
PASSWORD_MIN, PASSWORD_MAX = 8, 128


def password_problem(user: str, password: str) -> str:
    """Why this password change cannot be made, or "" when it can."""
    if user not in PASSWORD_USERS:
        return "只能改这几个账号：" + "、".join(PASSWORD_USERS)
    if len(password) < PASSWORD_MIN:
        return f"口令至少 {PASSWORD_MIN} 位"
    if len(password) > PASSWORD_MAX:
        return f"口令最多 {PASSWORD_MAX} 位"
    # `chpasswd` reads `user:password` per *line*: a newline in the password is a
    # second line, and a NUL truncates. Nothing else is restricted — a passphrase
    # with spaces or punctuation is a good password.
    if any(ch in password for ch in "\r\n\x00"):
        return "口令里不能有换行或 NUL"
    return ""


def do_set_password(args: list) -> dict:
    """Change one account's password. The secret goes in on stdin, never argv.

    Deliberately not an `argv_for` verb: an argument is world-readable in
    `/proc/<pid>/cmdline`, and a password belongs in a pipe rather than in a
    process list. That is also why the password is the one thing this process
    never logs — the log lines below name the account and nothing else.
    """
    if len(args) != 2:
        log("refused set-password with a wrong argument count")
        return {"ok": False, "error": "refused: set-password"}
    user, password = args
    problem = password_problem(user, password)
    if problem:
        log(f"refused set-password for {user!r}: {problem}")
        return {"ok": False, "error": problem}
    try:
        completed = subprocess.run(["chpasswd"],
                                   input=f"{user}:{password}\n",
                                   capture_output=True, text=True,
                                   timeout=COMMAND_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"set-password: {exc}")
        return {"ok": False, "error": "chpasswd could not run"}
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        log(f"set-password failed ({completed.returncode}) for {user!r}: {detail}")
        return {"ok": False, "error": detail or "chpasswd failed"}
    log(f"set-password ok for {user!r}")
    return {"ok": True, "user": user}


def sync_hosts(name: str, path: str = "/etc/hosts") -> bool:
    """Keep the 127.0.1.1 line in step with the hostname.

    `hostnamectl set-hostname` has not touched /etc/hosts since systemd 240, so
    without this the box answers to its new name on mDNS and to nothing at all
    locally — and Debian's `hostname -f`, which sudo and some mailers use, starts
    failing. The hostname is already set by the time this runs, so a failure here
    is reported and not fatal.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        log(f"could not read {path}: {exc}")
        return False
    out, replaced = [], False
    for line in lines:
        fields = line.split()
        if fields and fields[0] == "127.0.1.1":
            out.append(f"127.0.1.1\t{name}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"127.0.1.1\t{name}")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(out) + "\n")
    except OSError as exc:
        log(f"could not write {path}: {exc}")
        return False
    return True


def handle(request) -> dict:
    """Answer one request. Never raises: a crash here would take the socket with
    it, and the page would show a dead panel instead of an error."""
    if not isinstance(request, dict):
        return {"ok": False, "error": "malformed request"}
    verb = request.get("verb")
    args = request.get("args")
    if args is None:
        args = []
    if not isinstance(verb, str) or not isinstance(args, list) \
            or not all(isinstance(arg, str) for arg in args):
        return {"ok": False, "error": "malformed request"}
    if verb == "set-password":
        return do_set_password(args)
    argv = argv_for(verb, args)
    if argv is None:
        log(f"refused verb {verb!r} with {len(args)} argument(s)")
        return {"ok": False, "error": f"refused: {verb}"}
    try:
        completed = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=COMMAND_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"{verb}: {exc}")
        return {"ok": False, "error": f"{argv[0]} could not run"}
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        log(f"{verb} failed ({completed.returncode}): {detail}")
        return {"ok": False, "error": detail or f"{verb} failed"}
    if verb == "set-hostname":
        sync_hosts(args[0])
    log(f"{verb} ok")
    return {"ok": True}


# ── the socket ──────────────────────────────────────────────────────
def peer_is_allowed(peer_uid: int, expected: int) -> bool:
    """SO_PEERCRED comes from the kernel; this is the decision made from it."""
    return peer_uid == expected


def _peer_uid(conn) -> Optional[int]:
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                              struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid
    except OSError:
        return None


def _expecting_uid() -> Optional[int]:
    try:
        import pwd
        return pwd.getpwnam(PEER_USER).pw_uid
    except (ImportError, KeyError):
        return None


def _answer(conn) -> None:
    chunks = []
    total = 0
    complete = False
    while total < MAX_LINE:
        chunk = conn.recv(MAX_LINE - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if b"\n" in chunk:
            complete = True
            break
    if total >= MAX_LINE and not complete:
        # A peer that streams without ever finishing a line is not one this
        # socket answers. Stop reading and fail closed rather than let an
        # unbounded buffer grow a root process out of memory.
        reply = {"ok": False, "error": "malformed request"}
    else:
        line = b"".join(chunks).split(b"\n", 1)[0]
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            reply = {"ok": False, "error": "malformed request"}
        else:
            reply = handle(request)
    try:
        conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
    except OSError as exc:
        log(f"could not answer: {exc}")


def serve(socket_path: str = SOCKET_PATH,
          peer_uid: Optional[int] = None) -> None:
    expected = _expecting_uid() if peer_uid is None else peer_uid
    if expected is None:
        log(f"no `{PEER_USER}` user on this box; refusing to open the socket")
        return
    # systemd makes the directory (RuntimeDirectory= with Group=mrrc, 0770);
    # this is for a manual run, where it may not exist yet.
    os.makedirs(os.path.dirname(socket_path), exist_ok=True)
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    os.chmod(socket_path, SOCKET_MODE)
    server.listen(4)
    log(f"listening on {socket_path} for uid {expected}")
    while True:
        try:
            conn, _ = server.accept()
        except OSError as exc:
            log(f"accept failed: {exc}")
            continue
        with conn:
            uid = _peer_uid(conn)
            if uid is None or not peer_is_allowed(uid, expected):
                log(f"refused a peer with uid {uid}")
                continue
            _answer(conn)


if __name__ == "__main__":
    serve()
