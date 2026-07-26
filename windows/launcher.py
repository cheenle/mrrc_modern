from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


APP_NAME = "MRRC FT-710"
DEFAULT_PORT = "8888"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def user_data_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / "MRRC-FT710"
    return Path.home() / ".mrrc-ft710"


def config_path() -> Path:
    return user_data_dir() / "ft710.env"


def default_config_path() -> Path:
    return app_dir() / "windows" / "default.env"


def ensure_config() -> Path:
    data_dir = user_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    path = config_path()
    if not path.exists():
        default = default_config_path()
        if default.exists():
            path.write_text(default.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            path.write_text(
                "FT710_WEB_HOST=127.0.0.1\n"
                "FT710_WEB_PORT=8888\n"
                "FT710_SERIAL_PORT=COM3\n",
                encoding="utf-8",
            )
    return path


def seed_mem_channels() -> None:
    """Copy the bundled starter channels to the user data dir on first run."""
    target = user_data_dir() / "mem_channels.json"
    if target.exists():
        return
    # PyInstaller 6 onedir keeps datas in "_internal" next to the exe.
    candidates = (
        app_dir() / "mem_channels.json",
        app_dir() / "_internal" / "mem_channels.json",
    )
    for bundled in candidates:
        if bundled.exists():
            try:
                target.write_text(bundled.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
            return


def wait_for_server(url: str, proc: subprocess.Popen | None = None,
                    timeout_s: float = 15.0) -> bool:
    """Poll until the server answers HTTP (any status) or give up.

    Any HTTP response — even 401 from the auth middleware — proves the
    server is listening. Returns False on startup crash or timeout.
    """
    deadline = time.monotonic() + timeout_s
    probe = url + "/api/health"
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False  # server exited during startup
        try:
            with urllib.request.urlopen(probe, timeout=2):
                return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


def load_env(path: Path) -> dict[str, str]:
    env = os.environ.copy()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    env.setdefault("FT710_MEM_FILE", str(user_data_dir() / "mem_channels.json"))
    env.setdefault("FT710_FTDI_LIB_DIR", str(app_dir() / "vendor" / "ftdi" / "windows" / "bin" / "x64"))
    ftdi_dir = Path(env["FT710_FTDI_LIB_DIR"].replace("\\", os.sep))
    if not ftdi_dir.is_absolute():
        env["FT710_FTDI_LIB_DIR"] = str(app_dir() / ftdi_dir)
    return env


def server_executable() -> Path | None:
    exe = app_dir() / "ft710-server.exe"
    if exe.exists():
        return exe
    # Source-mode fallback only. When frozen, sys.executable IS this
    # launcher, so "fallback" to [sys.executable, server.py] would spawn
    # another launcher (which spawns another…) — an unbounded process
    # chain triggered e.g. by antivirus quarantining ft710-server.exe.
    script = app_dir() / "server.py"
    if not getattr(sys, "frozen", False) and script.exists():
        return script
    return None


def build_command() -> list[str] | None:
    server = server_executable()
    if server is None:
        return None
    if server.suffix.lower() == ".exe":
        return [str(server), "--no-ssl"]
    return [sys.executable, str(server), "--no-ssl"]


def stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def main() -> int:
    cfg = ensure_config()
    seed_mem_channels()
    env = load_env(cfg)
    port = env.get("FT710_WEB_PORT", DEFAULT_PORT)
    host = env.get("FT710_WEB_HOST", "127.0.0.1")
    url_host = "127.0.0.1" if host in ("::", "0.0.0.0", "") else host
    url = f"http://{url_host}:{port}"

    print(APP_NAME)
    print(f"Config: {cfg}")
    print(f"URL:    {url}")
    print("Close this window or press Ctrl-C to stop the server.")

    command = build_command()
    if command is None:
        print("ERROR: ft710-server.exe not found next to the launcher.")
        print("Reinstall the app, or restore the file if antivirus quarantined it.")
        return 1

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(
        command,
        cwd=str(app_dir()),
        env=env,
        creationflags=creationflags,
    )
    if wait_for_server(url, proc):
        webbrowser.open(url)
    elif proc.poll() is not None:
        print("Server exited during startup — see messages above.")
        return proc.returncode or 1
    else:
        print(f"Server did not answer within 15s; opening {url} anyway.")
        webbrowser.open(url)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        stop_process(proc)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
