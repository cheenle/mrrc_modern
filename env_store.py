"""The one writer of MRRC's env file (design D-8).

Seven code paths used to write ``mrrc.env`` / ``.env`` on their own:
``linux/first_run.py`` and ``macos/first_run.py`` (the same function twice),
``cloud_hub._write_config``, the two ``firstboot_wrapper.py`` copies,
``install.sh`` and ``box-overlay.sh``. They disagreed in ways that only a field
report reveals — one dropped every comment and re-sorted the keys, one widened
the box's ``0640`` to ``0644`` on the first Cloud Hub connect, and none of them
could notice a key written between its own read and its own write. This module
is the single implementation; all seven now call it.

Stdlib only and no app imports, on purpose: ``install.sh`` runs it with the
virtualenv's interpreter before the server exists, ``box-overlay.sh`` runs it
inside a chroot, and the PyInstaller specs already put the repo root on
``pathex`` (the ``launcher_net.py`` precedent). Restarting the service is
deliberately **not** here — the callers restart differently (``server`` exits 42
and lets the launcher relaunch it, ``mrrc-radio`` calls systemctl, firstboot has
not started the service yet), so the layer reports what it did through
``EnvWriteResult`` and the caller decides.
"""
from __future__ import annotations

import codecs
import locale
import os
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

__all__ = [
    "DEFAULT_LOCK_TIMEOUT", "EnvLockTimeout", "EnvStoreError", "EnvWriteResult",
    "load", "parse_env_text", "read_env_text", "render", "update_env_file",
]


def _key_of(line: str) -> str | None:
    """The key of a ``KEY=VALUE`` line; None for a comment, blank or no ``=``.

    A commented-out key (``#MRRC_WEB_PASSWORD=``) is documentation, not a key:
    both ``install.sh``'s template and ``macos/default.env`` explain options
    that way, and treating such a line as set would turn an operator's note
    into a real configuration value.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        return None
    return line.split("=", 1)[0].strip()


def _bom_encodings() -> tuple[tuple[bytes, str], ...]:
    """BOMs a text editor may have written (UTF-32 first: its BOM prefixes UTF-16's)."""
    return (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    )


def read_env_text(path: Path) -> tuple[str, str]:
    """Read a user-editable text file without ever raising on its encoding.

    Returns ``(text, encoding)`` where ``encoding`` is ``"utf-8"`` for a clean
    file and the fallback that was used otherwise, so callers can say so.

    On the Pi and the box this file arrives as the **preseed**: written on the
    operator's own PC and copied onto the boot partition, so a non-UTF-8 save
    (an ANSI/GBK editor turns the template's em-dash ``e2 80 94`` into
    ``e2 80 3f``) is the likeliest encoding to show up. A strict UTF-8 read made
    ``mrrc-firstboot.service`` fail on the very first boot — the same field bug
    as the Windows/macOS launchers (2026-09-12). This is now the **only** copy:
    ``linux/first_run.py`` and ``macos/first_run.py`` re-export it.
    """
    raw = path.read_bytes()
    for bom, encoding in _bom_encodings():
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace"), encoding
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    # Not UTF-8: prefer the local code page (so Chinese device names survive),
    # then GBK explicitly, then a codec that cannot fail — KEY=VALUE lines are
    # ASCII and still parse.
    for encoding in ("cp936", locale.getpreferredencoding(False), "latin-1"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        print(f"Warning: {path} is not valid UTF-8; reading it as {encoding}. "
              f"Re-save it as UTF-8 (any edit from MRRC does that).",
              file=sys.stderr)
        return text, encoding
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def load(path: Path) -> dict[str, str]:
    """The env as it is on disk right now; empty when the file is absent.

    Replaces ``mrrc_radio.current_env()`` and the ad-hoc ``read_env_text(...) →
    parse`` pairs. Never raises on a missing file: "not configured yet" is the
    normal state on a first boot, not an error.
    """
    path = Path(path)
    if not path.is_file():
        return {}
    return parse_env_text(read_env_text(path)[0])


def parse_env_text(text: str) -> dict[str, str]:
    """``KEY=VALUE`` of an env file body, comments and blanks skipped.

    Values are stripped. On a duplicated key the last one wins, which is what
    systemd's ``EnvironmentFile`` does too — two readers disagreeing about the
    same file is worse than either choice.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        key = _key_of(line)
        if key is None:
            continue
        out[key] = line.split("=", 1)[1].strip()
    return out


def render(text: str, updates: dict[str, str], *, only_if_absent: bool = False) -> str:
    """Merge ``updates`` into an env file body. Pure; ``update_env_file`` writes it.

    An existing key keeps its own line, and therefore the comment above it and
    the section it sits in; a new key is appended. Keys this call does not own
    are copied through untouched — design D-8's second requirement, and the
    reason a whole-file rewrite is never acceptable here.

    ``only_if_absent`` leaves a key that is already present alone. That is what
    the two firstboot wrappers and ``box-overlay.sh`` mean by "headless
    defaults, only keys that are not already set".
    """
    pending = dict(updates)
    out: list[str] = []
    for line in text.splitlines():
        key = _key_of(line)
        if key is not None and key in pending:
            value = pending.pop(key)
            out.append(line if only_if_absent else f"{key}={value}")
            continue
        out.append(line)
    for key, value in pending.items():
        out.append(f"{key}={value}")
    # Byte-compatible with the implementation this replaces ("\n".join + "\n"),
    # so a file that lacked a trailing newline gains one, and an empty body is a
    # single newline rather than an empty file.
    return "\n".join(out) + "\n"


DEFAULT_LOCK_TIMEOUT = 5.0


class EnvStoreError(Exception):
    """Base class: every failure here is reportable, not fatal-on-boot."""


class EnvLockTimeout(EnvStoreError):
    """Another MRRC process held this env file's write lock for too long."""


@dataclass(frozen=True)
class EnvWriteResult:
    """What one write did, so the caller can decide and report (design D-8 §3).

    The layer never restarts anything: the three callers restart differently
    (``server`` exits 42 and lets the launcher relaunch it, ``mrrc-radio`` calls
    systemctl, firstboot has not started the service yet), so "写后按需重启" is
    the caller's decision and this is what it decides from.
    """

    path: Path
    changed: dict[str, str]      #: key -> new value, for keys that were already there
    added: tuple[str, ...]       #: keys the file did not have before
    encoding: str                #: what the file was read as ("utf-8" normally)
    written: bool                #: False when nothing on disk needed to change


def _acquire(fd: int) -> None:
    """One non-blocking attempt at an exclusive lock; raises OSError when busy.

    ``msvcrt`` on Windows and ``fcntl`` on POSIX — both stdlib, because the
    Windows launcher writes this file too and the layer must not grow a
    dependency.
    """
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass                      # closing the fd releases it anyway
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _locked(path: Path, timeout: float) -> Iterator[None]:
    """Hold ``<path>.lock`` exclusively across the read-merge-write.

    A merge is only merge-safe if nobody else's write lands between this
    process's read and its replace — that is exactly what R4 means by "并发改
    不丢字段". A *sibling* lock file rather than the env file itself: the write
    replaces the target by rename, so a lock on its inode would be left holding
    a file nobody ever reads again.

    A stale lock is not a failure mode worth designing around: the OS releases
    an flock/msvcrt lock when the holding process dies, so a crashed writer
    cannot lock the box out. The lock file itself is left on disk (deleting it
    would race the next acquirer) and carries no configuration.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.with_name(path.name + ".lock")),
                 os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                _acquire(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise EnvLockTimeout(
                        f"{path} stayed locked for {timeout:.1f}s; another MRRC "
                        "process is writing it"
                    ) from None
                time.sleep(0.01)
        yield
    finally:
        try:
            _release(fd)
        finally:
            os.close(fd)


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` by rename, keeping the target's mode and owner.

    Preserving them is the point, not a nicety: the box's env file is
    ``0640 mrrc:mrrc`` (``box-overlay.sh`` and both firstboot wrappers set it)
    and holds the web password, while a fresh temp file is created ``0644``
    under the usual umask — so the plain ``tmp.replace(path)`` that
    ``cloud_hub._write_config`` used silently widened it on the first Cloud Hub
    connect.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        original = path.stat()
    except OSError:
        original = None
    if original is not None:
        os.chmod(tmp, stat.S_IMODE(original.st_mode))
        if hasattr(os, "chown"):          # not on Windows
            try:
                os.chown(tmp, original.st_uid, original.st_gid)
            except OSError:
                pass    # not the owner (or not root): mode is the part that matters
    os.replace(tmp, path)


def update_env_file(
    path: Path,
    updates: dict[str, str],
    *,
    only_if_absent: bool = False,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> EnvWriteResult:
    """Merge ``updates`` into the env file at ``path`` — the only writer.

    Read → merge → atomic replace, under a lock, preserving comments, the keys
    this call does not own (the password, certificate paths, whatever Cloud Hub
    wrote), the file's mode and its owner. Creates the file and its directory
    when absent. Always writes UTF-8: a file that arrived from an ANSI/GBK
    editor is normalised by the first write.

    The positional signature is the one ``linux/first_run.py`` and
    ``macos/first_run.py`` already had, so their callers (``server.py``,
    ``linux/mrrc_radio.py``, both firstboot wrappers) and the tests that patch
    the name keep working unchanged.
    """
    path = Path(path)
    with _locked(path, lock_timeout):
        existed = path.exists()
        before_bytes = path.read_bytes() if existed else b""
        text, encoding = read_env_text(path) if existed else ("", "utf-8")
        before = parse_env_text(text)
        body = render(text, updates, only_if_absent=only_if_absent)
        after = parse_env_text(body)
        after_bytes = body.encode("utf-8")
        # Compare bytes, not text: a cp936 file whose parsed content did not
        # change still has to be rewritten, because the promise is "any edit
        # from MRRC normalises it to UTF-8".
        written = after_bytes != before_bytes
        if written:
            _atomic_write(path, body)
    return EnvWriteResult(
        path=path,
        changed={k: after[k] for k in after if k in before and after[k] != before[k]},
        added=tuple(k for k in after if k not in before),
        encoding=encoding,
        written=written,
    )
