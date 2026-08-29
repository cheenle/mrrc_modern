"""macOS menu-bar launcher for MRRC Modern Web Control.

Mirror of windows/launcher.py, adapted for macOS:

- User config lives in ~/Library/Application Support/MRRC-Modern/mrrc_modern.env.
- The server binary is `MRRC-Modern-Server` (no .exe) next to this launcher
  inside the .app bundle's Contents/MacOS/.
- The launcher is a background (LSUIElement) menu-bar app built on rumps:
  it spawns the server subprocess, opens the web UI, and offers menu items
  to reopen the UI, edit the config, restart, or quit cleanly.

First launch must be via right-click -> Open (or `xattr -d com.apple.quarantine`)
because the .app is ad-hoc signed and Gatekeeper will otherwise block it.
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import rumps

from macos import first_run
import ssl_bootstrap


APP_NAME = "MRRC Modern"
DEFAULT_PORT = "8888"


def _env(env: dict[str, str], name: str, default: str = "") -> str:
    """Read ``MRRC_*`` from an env dict, falling back to the legacy ``FT710_*`` key."""
    if name in env and env[name] != "":
        return env[name]
    if name.startswith("MRRC_"):
        legacy = "FT710_" + name[len("MRRC_"):]
        if legacy in env and env[legacy] != "":
            return env[legacy]
    return default


def app_dir() -> Path:
    """Directory containing the bundled binaries.

    Frozen (inside the .app): Contents/MacOS/ — the parent of sys.executable.
    Source mode: the repo root (parent of this file's dir).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def user_data_dir() -> Path:
    # macOS convention for per-user app data. FALLBACK for source-mode runs
    # without HOME (CI) keeps the same shape as the Windows launcher.
    root = os.environ.get("HOME") or str(Path.home())
    return Path(root) / "Library" / "Application Support" / "MRRC-Modern"


def config_path() -> Path:
    return user_data_dir() / "mrrc_modern.env"


def default_config_path() -> Path:
    return app_dir() / "macos" / "default.env"


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
                "MRRC_WEB_HOST=127.0.0.1\n"
                "MRRC_WEB_PORT=8888\n"
                "MRRC_SERIAL_PORT=/dev/cu.SLAB_USBtoUART\n",
                encoding="utf-8",
            )
    return path


def seed_mem_channels() -> None:
    """Copy the bundled starter channels to the user data dir on first run."""
    target = user_data_dir() / "mem_channels.json"
    if target.exists():
        return
    # The frozen server keeps its own seed in _internal/, but the launcher also
    # ships a copy at Contents/MacOS/mem_channels.json for first-run seeding.
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
                    timeout_s: float = 15.0, secure: bool = False) -> bool:
    """Poll until the server answers HTTP (any status) or give up.

    Any HTTP response — even 401 from the auth middleware — proves the server is
    listening. Returns False on startup crash or timeout. `secure` skips TLS
    verification (self-signed bootstrap certs are not trusted by any store yet).
    """
    ctx = None
    if secure:
        import ssl as _ssl

        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
    deadline = time.monotonic() + timeout_s
    probe = url + "/api/health"
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False  # server exited during startup
        try:
            with urllib.request.urlopen(probe, timeout=2, context=ctx):
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
    env.setdefault("MRRC_MEM_FILE", str(user_data_dir() / "mem_channels.json"))
    env.setdefault("MRRC_ATR1000_STORE", str(user_data_dir() / "atr1000_tuner.json"))
    env.setdefault("MRRC_FTDI_LIB_DIR", str(app_dir() / "vendor" / "ftdi" / "macos"))
    ftdi_dir = Path(_env(env, "MRRC_FTDI_LIB_DIR"))
    if not ftdi_dir.is_absolute():
        env["MRRC_FTDI_LIB_DIR"] = str(app_dir() / ftdi_dir)
    return env


def ensure_first_run() -> dict[str, str]:
    """Apply first-launch auto-config if needed and surface the password.

    Returns the (possibly updated) env dict ready to pass to the server.
    """
    cfg = config_path()
    env = load_env(cfg)
    if first_run.needs_first_run(env):
        env = first_run.apply_first_run(env, cfg)
        if env.get("MRRC_AUTO_PASSWORD") == "1":
            rumps.notification(
                APP_NAME,
                "首次运行",
                f"登录密码已自动生成：{env.get('MRRC_WEB_PASSWORD', '')}\n"
                "已存入配置，菜单栏「Show Password…」可随时查看。",
            )
    return env


def show_password() -> None:
    env = load_env(config_path())
    pwd = _env(env, "MRRC_WEB_PASSWORD", "")
    rumps.alert(
        title=APP_NAME,
        message=f"Web 登录密码：{pwd}\n（菜单栏「Edit Configuration…」可修改）",
    )


def server_executable() -> Path | None:
    exe = app_dir() / "MRRC-Modern-Server"
    if exe.exists():
        return exe
    # Source-mode fallback only. When frozen, sys.executable IS this launcher,
    # so "fallback" to [sys.executable, server.py] would spawn another launcher
    # (which spawns another...) — an unbounded process chain.
    script = app_dir() / "server.py"
    if not getattr(sys, "frozen", False) and script.exists():
        return script
    return None


def local_url(env: dict[str, str], secure: bool = False) -> str:
    port = _env(env, "MRRC_WEB_PORT", DEFAULT_PORT)
    host = _env(env, "MRRC_WEB_HOST", "127.0.0.1")
    scheme = "https" if secure else "http"
    if host == "::":
        url_host = "localhost"
    elif host in ("0.0.0.0", ""):
        url_host = "127.0.0.1"
    else:
        url_host = host
    return f"{scheme}://{url_host}:{port}"


def ssl_material(env: dict[str, str]):
    """Resolve the TLS cert/key pair for the server.

    Honours explicit MRRC_SSL_CERT/MRRC_SSL_KEY, then falls back to a
    self-signed pair generated into the user data dir on first run.
    Returns None to stay on plain HTTP (MRRC_SSL=off, or the
    cryptography package missing).
    """
    if _env(env, "MRRC_SSL", "").strip().lower() == "off":
        return None
    cert = _env(env, "MRRC_SSL_CERT", "").strip()
    key = _env(env, "MRRC_SSL_KEY", "").strip()
    if cert and key and Path(cert).exists() and Path(key).exists():
        return Path(cert), Path(key)
    return ssl_bootstrap.ensure_self_signed(user_data_dir() / "certs")


def build_command(ssl_pair=None) -> list[str] | None:
    server = server_executable()
    if server is None:
        return None
    # Both the frozen binary and `python server.py` take the same CLI args.
    if getattr(sys, "frozen", False) and server == app_dir() / "MRRC-Modern-Server":
        args = [str(server)]
    else:
        args = [sys.executable, str(server)]
    if ssl_pair is None:
        args.append("--no-ssl")
    else:
        args += ["--ssl-cert", str(ssl_pair[0]), "--ssl-key", str(ssl_pair[1])]
    return args


def stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()  # SIGTERM -> graceful: audio drains, PTT releases
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def open_in_textedit(path: Path) -> None:
    subprocess.Popen(["open", "-a", "TextEdit", str(path)])


class MRRCModernApp(rumps.App):
    def __init__(self, url: str, host: str, port: str) -> None:
        super().__init__(
            f"{APP_NAME} :{port}",
            quit_button=None,  # we provide our own Quit to clean up first
        )
        self.url = url
        self.host = host
        self.port = port
        self.proc: subprocess.Popen | None = None
        self.secure = False
        self._quitting = False
        # Set early so atexit (registered below) can always reach it.
        self.menu = [
            "Open Web UI",
            "Edit Configuration…",
            "Show Password…",
            "Restart Server",
            None,  # separator
            "Quit MRRC Modern",
        ]

    # ---- lifecycle -------------------------------------------------------

    def start_server(self) -> int | None:
        """Spawn the server subprocess. Returns its pid, or None on failure."""
        env = load_env(config_path())
        ssl_pair = ssl_material(env)
        self.secure = ssl_pair is not None
        self.url = local_url(env, secure=self.secure)
        command = build_command(ssl_pair)
        if command is None:
            rumps.alert(
                title="MRRC Modern",
                message="MRRC-Modern-Server was not found next to the launcher. "
                         "Reinstall the app.",
            )
            rumps.quit_application()
            return None
        env["MRRC_CONFIG_FILE"] = str(config_path())
        self.proc = subprocess.Popen(
            command,
            cwd=str(app_dir()),
            env=env,
        )
        return self.proc.pid

    def launch_and_open(self) -> None:
        """Background-thread worker: wait for the server, then open the browser."""
        if self.proc is None:
            return
        url = self.url
        if wait_for_server(url, self.proc, secure=self.secure):
            webbrowser.open(url)
        elif self.proc.poll() is not None:
            rumps.notification(
                APP_NAME, "Server exited during startup",
                "See Console for details.",
            )
        else:
            rumps.notification(
                APP_NAME, "Server slow to start",
                f"No HTTP answer within 15s; opening {url} anyway.",
            )
            webbrowser.open(url)

    def _monitor_loop(self) -> None:
        """Watch the server; auto-restart when it exits with RESTART_EXIT_CODE."""
        while not self._quitting:
            proc = self.proc
            if proc is None:
                time.sleep(0.5)
                continue
            rc = proc.wait()
            if rc == 42:  # config changed via the web UI -> apply by restarting
                self.start_server()
                threading.Thread(
                    target=self.launch_and_open, name="wait-for-server", daemon=True
                ).start()
            else:
                if rc != 0 and not self._quitting:
                    rumps.notification(
                        APP_NAME, "服务器异常退出",
                        f"进程退出码 {rc}。可在菜单栏 Restart Server 重新启动。",
                    )
                # Park the monitor on any non-42 exit (incl. a clean rc==0 external
                # stop). Without this, the loop would busy-spin on the reaped
                # process's wait() returning immediately at 100% CPU.
                self.proc = None

    # ---- menu callbacks --------------------------------------------------

    @rumps.clicked("Open Web UI")
    def on_open(self, _):
        webbrowser.open(self.url)

    @rumps.clicked("Edit Configuration…")
    def on_edit(self, _):
        open_in_textedit(config_path())

    @rumps.clicked("Show Password…")
    def on_show_password(self, _):
        show_password()

    @rumps.clicked("Restart Server")
    def on_restart(self, _):
        stop_process(self.proc)
        self.start_server()
        threading.Thread(
            target=self.launch_and_open, name="wait-for-server", daemon=True
        ).start()

    @rumps.clicked("Quit MRRC Modern")
    def on_quit(self, _):
        self._quitting = True
        stop_process(self.proc)
        rumps.quit_application()


def main() -> int:
    ensure_config()
    seed_mem_channels()

    # Determine the URL before starting the server (also seeds the env on disk).
    env = ensure_first_run()
    port = _env(env, "MRRC_WEB_PORT", DEFAULT_PORT)
    host = _env(env, "MRRC_WEB_HOST", "127.0.0.1")
    cert_new = not (user_data_dir() / "certs" / ssl_bootstrap.CERT_FILENAME).exists()
    ssl_pair = ssl_material(env)
    secure = ssl_pair is not None
    url = local_url(env, secure=secure)
    if secure and cert_new:
        rumps.notification(
            APP_NAME,
            "HTTPS 已启用（自签名证书）",
            "浏览器首次打开会提示“不安全”——点「高级 → 继续访问」即可。",
        )

    app = MRRCModernApp(url=url, host=host, port=port)

    # Reap the server child if the .app is killed by SIGTERM (e.g. logout,
    # `kill` from a terminal, or Activity Monitor's normal Quit). SIGKILL /
    # Force Quit cannot be caught and may orphan the server — documented.
    def _on_sigterm(signum, frame):
        app._quitting = True
        stop_process(app.proc)
        rumps.quit_application()

    signal.signal(signal.SIGTERM, _on_sigterm)
    atexit.register(stop_process, app.proc)

    app.start_server()
    threading.Thread(target=app._monitor_loop, name="server-monitor", daemon=True).start()
    threading.Thread(
        target=app.launch_and_open, name="wait-for-server", daemon=True
    ).start()

    rumps.App.run(app)  # blocks on the NSApplication run loop
    stop_process(app.proc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
