"""Switch the radio model this box serves, then restart onto it.

One profile per registry key lives in ``packaging/box/profiles/`` — copied to
``<MRRC_HOME>/profiles/`` inside the box image — and this command is the only
thing that writes the env file systemd hands the server.
Subcommands: ``list``, ``show``, ``use <model> [--port DEV]``.

Serial I/O is deliberately absent. ``--port`` takes the operator's answer, and
with no ``--port`` we call the probe that already exists in
``linux/first_run.py`` rather than opening a port here — SDD's
``cat-direct-serial-io`` keeps radio serial I/O inside the backends'
controllers, and a second implementation of "which port is the radio" is
exactly the kind of second source of truth this repo avoids.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# In a checkout this file sits in linux/ and the tree is its parent. On the box it
# is a copy at /usr/local/bin/mrrc-radio (box-overlay.sh's install), where that
# parent is /usr/local — not a tree — so the tree comes from MRRC_HOME, the same
# variable linux/mrrc_update.sh reads. The sibling backends/ is what tells the two
# apart: without this, the installed copy died on `import backends`.
_SOURCE = Path(__file__).resolve().parents[1]
REPO = _SOURCE if (_SOURCE / "backends").is_dir() else Path(
    os.environ.get("MRRC_HOME", "/opt/mrrc_modern"))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "linux") not in sys.path:
    sys.path.insert(0, str(REPO / "linux"))

from backends import known_models  # noqa: E402
from first_run import read_env_text, update_env_file  # noqa: E402

# The checkout keeps them under packaging/ (which is not part of the image);
# box-overlay.sh copies them to <MRRC_HOME>/profiles instead.
PROFILE_DIR = REPO / "packaging" / "box" / "profiles"
if not PROFILE_DIR.is_dir():
    PROFILE_DIR = REPO / "profiles"
DEFAULT_ENV = Path("/opt/mrrc_modern/env/mrrc.env")
DEFAULT_UNIT = "mrrc-modern"


def unverified_models() -> frozenset[str]:
    """Registry keys whose tables have no hardware evidence (AD-019)."""
    from backends.ic7300.civ_profiles import get_profile as icom_profile
    from backends.ic7300.civ_profiles import known_models as icom_models
    from backends.yaesu.yaesu_profiles import PROFILES as yaesu_profiles

    out = {m for m in icom_models() if not icom_profile(m).verified}
    out |= {m for m, p in yaesu_profiles.items() if not p.verified}
    return frozenset(out)


def _parse_env_text(text: str) -> dict[str, str]:
    """KEY=VALUE of an env file body, comments and blanks skipped."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def current_env(env_path: Path) -> dict[str, str]:
    """The env as it is on disk right now; empty when the file is absent."""
    if not env_path.is_file():
        return {}
    text, _ = read_env_text(env_path)
    return _parse_env_text(text)


def read_profile(model: str, profile_dir: Path = PROFILE_DIR) -> dict[str, str]:
    path = profile_dir / f"{model}.env"
    if not path.is_file():
        raise FileNotFoundError(f"no profile for {model!r}: {path} is missing")
    text, _ = read_env_text(path)
    return _parse_env_text(text)


def plan(
    model: str,
    env: dict[str, str],
    port: str | None,
    profile_dir: Path = PROFILE_DIR,
) -> dict[str, str]:
    """The keys `use` will write. Pure: no disk writes, no restarts.

    Two rules that are easy to get wrong and are therefore tested:

    * a model change with no explicit port clears ``MRRC_SERIAL_PORT`` rather
      than leaving the previous radio's node behind — the IC-7300 answers on
      ttyACM0 while the FT-710 needs one of two ttyUSB nodes, so a stale value
      is a dead CAT link with no error attached;
    * the transmit gate is re-asserted from the profile instead of inherited,
      so switching away from a model the operator opened cannot leave the next
      one open (design D-3).
    """
    if model not in known_models():
        raise ValueError(f"unknown model {model!r}; known: {', '.join(known_models())}")
    updates = read_profile(model, profile_dir)
    if port:
        updates["MRRC_SERIAL_PORT"] = port
    elif env.get("MRRC_RADIO_MODEL") != model:
        updates["MRRC_SERIAL_PORT"] = ""
    updates["MRRC_ALLOW_UNVERIFIED_TX"] = updates.get("MRRC_ALLOW_UNVERIFIED_TX", "0")
    return updates


def apply(env_path: Path, updates: dict[str, str], dry_run: bool = False) -> None:
    """Back the file up, then merge the updates into it."""
    if dry_run:
        return
    if env_path.is_file():
        shutil.copy2(env_path, env_path.with_suffix(env_path.suffix + ".bak"))
    env_path.parent.mkdir(parents=True, exist_ok=True)
    update_env_file(env_path, updates)


def _systemctl(args: list[str]) -> int:
    if shutil.which("systemctl") is None:
        print("systemctl not found — restart the service yourself", file=sys.stderr)
        return 127
    return subprocess.call(["systemctl", *args])


def detect_port(model: str) -> str | None:
    """Ask the existing probe which node answers. Serial I/O stays in first_run."""
    import first_run

    for candidate in first_run.detect_serial_ports():
        try:
            if first_run.probe_radio_model(candidate) == model:
                return candidate
        except Exception:  # a busy or absent port is not an error here
            continue
    return None


def _cmd_list(args) -> int:
    bad = unverified_models()
    current = current_env(args.env).get("MRRC_RADIO_MODEL", "")
    for model in known_models():
        mark = "←" if model == current else " "
        state = "⚠ 实验性，仅接收" if model in bad else "✅ 已验证"
        print(f"{mark} {model:<10} {state}")
    return 0


def _cmd_show(args) -> int:
    for key, value in sorted(current_env(args.env).items()):
        print(f"{key}={value}")
    return 0


def _cmd_use(args) -> int:
    env = current_env(args.env)
    updates = plan(args.model, env, args.port)
    if args.dry_run:
        for key, value in sorted(updates.items()):
            print(f"{key}={value}")
        return 0

    changing = env.get("MRRC_RADIO_MODEL") != args.model
    if not args.port and changing:
        # Stop first: the server holds the port, and a probe against a held
        # port answers nothing.
        _systemctl(["stop", args.unit])
        port = detect_port(args.model)
        if port:
            updates["MRRC_SERIAL_PORT"] = port
            print(f"detected {args.model} on {port}")
        else:
            print(
                f"no {args.model} answered — serial port left empty; "
                f"re-run with --port /dev/ttyXXX once the radio is connected",
                file=sys.stderr,
            )

    apply(args.env, updates)
    rc = _systemctl(["start", args.unit]) if not args.no_restart else 0
    if args.model in unverified_models():
        print(
            f"{args.model}: 实验性机型，默认只收不发。要发射需设 "
            f"MRRC_ALLOW_UNVERIFIED_TX=1（AD-019）。",
            file=sys.stderr,
        )
    print(f"now serving {args.model}; env={args.env}")
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mrrc-radio", description=__doc__)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--unit", default=DEFAULT_UNIT)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="registry models and their verification state")
    sub.add_parser("show", help="the env keys as they are on disk")

    use = sub.add_parser("use", help="switch the model and restart onto it")
    use.add_argument("model")
    use.add_argument("--port", help="serial node; omit to probe for it")
    use.add_argument("--dry-run", action="store_true", help="print, change nothing")
    use.add_argument("--no-restart", action="store_true", help="for tests")
    use.set_defaults(func=_cmd_use)

    args = parser.parse_args(argv)
    if args.cmd == "list":
        return _cmd_list(args)
    if args.cmd == "show":
        return _cmd_show(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
