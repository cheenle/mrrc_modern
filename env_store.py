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

__all__ = ["parse_env_text", "render"]


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
