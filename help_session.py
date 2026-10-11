"""Call help: the customer's own door to this instance, open only when they ask.

Model
-----
The customer presses one button. This module then: makes a fresh credential pair,
binds an SSH server on loopback, asks the portal for a tunnel port, adds a second
proxy to the running frpc through its admin API, and starts a 20 s heartbeat that
carries the portal's "stop" back. Three things can end it — the customer, the
window, the portal — and all three run the same ``close(reason)``.

What is deliberately absent
---------------------------
* **No persistence.** The credentials live in this process only. A restart ends
  help, which is the point: there is no stored password to forget to expire.
* **No privilege.** The session runs as this process's user and gets this
  process's environment, because that is what a supporter needs to look at; it
  grants nothing the app's own user does not already have.
* **No public listener.** The bind is loopback, and the tunnel's far end is
  loopback too (frps binds proxy ports on 127.0.0.1), so nothing here is
  reachable from the internet.
* **No password in any log, config, support bundle or portal request body.** The
  portal's request schema has no field it could travel in.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# A hard import, like cryptography in ssl_bootstrap: the four packaging lines all
# install requirements.txt, so a missing SSH transport is a broken install — and a
# broken install must fail at startup rather than at the moment a customer presses
# "call help".
import paramiko

logger = logging.getLogger(__name__)

#: Hard ceiling, whatever a caller asks for. No "until I say stop" option: the
#: classic failure of this feature is forgetting to turn it off.
MAX_WINDOW_MINUTES = 180
DEFAULT_WINDOW_MINUTES = 60
#: Failed authentications before the whole session shuts down.
MAX_AUTH_FAILURES = 5
#: How often the instance tells the portal it is still here, and asks whether to stop.
HEARTBEAT_SECONDS = 20
#: Loopback, always. A test asserts this is not "0.0.0.0" or "".
BIND_HOST = "127.0.0.1"

#: States. ``closed`` exists so a caller can read the reason after the fact.
OFF, ARMING, OPEN, CLOSED = "off", "arming", "open", "closed"


def make_credentials() -> tuple[str, str]:
    """(username, password), fresh for every enable.

    The username is short on purpose: a human types it at a hub shell from a phone
    call. The password is the only thing that stops somebody else on the hub (or a
    process on this machine) from walking in, so it is long and random — and it
    never leaves this process except through the customer's eyes.
    """
    return "help-" + secrets.token_hex(2), secrets.token_urlsafe(18)


def _usable_username(name: str) -> bool:
    """A username a human can type from a phone call: bounded, no whitespace, no
    shell metacharacters. Not a security boundary (the password is), just hygiene."""
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_.\-]{3,32}", name or ""))


def credentials_match(expected_user: str, expected_pw: str,
                      got_user: str, got_pw: str) -> bool:
    """Constant-time compare of both fields.

    Both are always compared (never short-circuiting on the username) so a probe
    cannot learn that the username was right by timing the answer.
    """
    ok_user = hmac.compare_digest(expected_user, got_user)
    ok_pw = hmac.compare_digest(expected_pw, got_pw)
    return ok_user and ok_pw


@dataclass
class HelpSession:
    """One help window. Never reused: close it, and the next enable is a new object.

    Five collaborators, all injectable, because every one of them is a thing that
    fails in the field and none of them should need a tunnel to test:

    * ``opener``   — ask the portal for a tunnel port: ``(minutes) -> remote_port``
    * ``tunnel``   — add/remove the frpc proxy: ``(local_port, remote_port, add) -> None``
    * ``listener`` — bind the SSH server on loopback: ``() -> local_port``
    * ``closer``   — tell the portal this window is over: ``() -> None``
    * ``clock``    — injectable time, so the window is testable without waiting
    """

    opener: Callable[[int], int]
    tunnel: Callable[[int, int, bool], None]
    listener: Callable[[], int]
    closer: Optional[Callable[[], None]] = None
    clock: Callable[[], float] = time.time
    on_event: Optional[Callable[[dict], None]] = None
    server: Optional[Any] = None
    #: Called every ``beat_interval`` seconds while open: (port, connections) -> reply.
    #: A reply with ``stop`` closes the session ("portal"); **no reply is not a reason
    #: to close** — the window is a local promise, and one flaky hop must not cut a
    #: customer's support call short. Injected because a heartbeat without a portal is
    #: still worth testing.
    beat: Optional[Callable[[int, int], dict]] = None
    beat_interval: float = HEARTBEAT_SECONDS

    state: str = field(default=OFF, init=False)
    close_reason: str = field(default="", init=False)
    connections: int = field(default=0, init=False)

    _user: str = field(default="", init=False, repr=False)
    _pw: str = field(default="", init=False, repr=False)
    _local_port: int = field(default=0, init=False)
    _remote_port: int = field(default=0, init=False)
    _expires_at: float = field(default=0.0, init=False)
    _failures: int = field(default=0, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _beat_stop: threading.Event = field(default_factory=threading.Event, init=False,
                                        repr=False)
    #: 只有真的起了线程才有值。测试与"这一轮到底有没有心跳"都靠它判断，
    #: 而不是去 enumerate() 全局线程名——那是竞态的写法（关闭中的线程还能被看见）。
    _beat_thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)

    # ── lifecycle ──────────────────────────────────────────────────────────
    def enable(self, *, minutes: int = DEFAULT_WINDOW_MINUTES,
               username: str = "", password: str = "") -> dict:
        """Open the window. Raises on any failure, and leaves nothing behind.

        The customer may bring their own credentials (that is what "in the
        customer's hands" means); leaving them empty generates a pair. A supplied
        username is only checked for shape — it is typed by a human at a hub shell.
        """
        with self._lock:
            if self.state in (ARMING, OPEN):
                raise RuntimeError("a help session is already open")
            self.state = ARMING
            self.close_reason = ""
            self._failures = 0
            self.connections = 0
            try:
                # Four combinations, one meaning each: both supplied → use them;
                # either one alone → the other is generated; neither → both generated.
                # The UI always shows the pair it ends up with, so a customer who only
                # cares about the password still gets a coherent window.
                user = username or make_credentials()[0]
                pw = password or make_credentials()[1]
                if username and not _usable_username(username):
                    raise ValueError("username must be 3-32 chars, [A-Za-z0-9_.-]")
                if len(pw) < 8:
                    raise ValueError("password must be at least 8 characters")
                self._user, self._pw = user, pw
                if self.server is not None:
                    self.server.set_credentials(self._user, self._pw)
                self._local_port = int(self.listener())
                window = max(1, min(int(minutes), MAX_WINDOW_MINUTES))
                self._remote_port = int(self.opener(window))
                self.tunnel(self._local_port, self._remote_port, True)
                self._expires_at = self.clock() + window * 60
                self.state = OPEN
                self._emit("opened", user=self._user, port=self._remote_port,
                           minutes=window)
                self._start_heartbeat()
                return self.status()
            except BaseException:
                # Every failure path leaves through the same door, so a bound socket
                # or a half-written proxy cannot survive an exception here.
                self.close("error")
                raise

    def tick(self) -> Optional[str]:
        """Called by the heartbeat. Returns the reason if this tick closed it."""
        with self._lock:
            if self.state != OPEN:
                return None
            if self.clock() >= self._expires_at:
                self.close("expired")
                return "expired"
            return None

    def close(self, reason: str) -> None:
        """The only way out. Idempotent; the first reason wins."""
        with self._lock:
            if self.state == CLOSED:
                return
            was_open = self.state == OPEN
            self.close_reason = self.close_reason or reason
            self.state = CLOSED
            self._beat_stop.set()             # 心跳先停，免得它回头再来碰一个正在关的会话
            local, remote = self._local_port, self._remote_port
            self._local_port = self._remote_port = 0
            if local and remote:
                try:
                    self.tunnel(local, remote, False)
                except Exception as exc:          # 记下来，但不阻断清理
                    logger.warning("help: cannot remove the tunnel proxy: %s", exc)
                    self._emit("tunnel_removal_failed", error=str(exc))
            self._close_listener()
            if self.closer is not None:
                try:
                    self.closer()
                except Exception as exc:          # 门户报错不能把监听留在那里
                    logger.warning("help: cannot tell the portal: %s", exc)
            self._user = self._pw = ""
            if was_open or self.close_reason:
                self._emit("closed", reason=self.close_reason)

    # ── reporting ──────────────────────────────────────────────────────────
    def status(self) -> dict:
        """Everything the UI shows. **No password here**: the UI reads it through
        ``password_for_display()``, which has its own name for a reason."""
        with self._lock:
            # Display data, so a broken clock must not take the status endpoint down
            # with it: the window itself is enforced by tick(), not by this number.
            remaining = 0
            if self.state == OPEN:
                try:
                    remaining = max(0, int(self._expires_at - self.clock()))
                except (TypeError, ValueError):
                    remaining = 0
            return {
                "state": self.state,
                "username": self._user,
                "port": self._remote_port,
                "expires_at": self._expires_at,
                "expires_in_s": remaining,
                "connections": self.connections,
                "failures": self._failures,
                "close_reason": self.close_reason,
            }

    @property
    def local_port(self) -> int:
        """The loopback port the SSH server is on. Not in ``status()``: the UI has no
        use for it (the tunnel is what matters to the supporter), but tests and log
        lines do."""
        with self._lock:
            return self._local_port

    def password_for_display(self) -> str:
        """The one accessor for the password, named so a reader sees why it exists:
        the customer has to read it out loud, so it must be visible while open —
        and empty the moment it is not."""
        with self._lock:
            return self._pw if self.state == OPEN else ""

    # ── auditing ──────────────────────────────────────────────────────────
    def note_auth_failure(self) -> None:
        """One failed authentication. The fifth closes the window.

        A live help session is exactly the moment somebody would probe a port they
        noticed, and loopback-only is not a defence against a local process.
        """
        with self._lock:
            if self.state != OPEN:
                return
            self._failures += 1
            self._emit("auth_fail", failures=self._failures)
            if self._failures >= MAX_AUTH_FAILURES:
                self.close("auth_failures")

    def note_connection(self, *, duration_s: float) -> None:
        with self._lock:
            self.connections += 1
            self._emit("disconnect", duration_s=round(duration_s, 1))

    # ── heartbeat ─────────────────────────────────────────────────────────
    def _start_heartbeat(self) -> None:
        if self.beat is None:
            return
        self._beat_stop.clear()
        self._beat_thread = threading.Thread(target=self._beat_loop,
                                             name="mrrc-help-beat", daemon=True)
        self._beat_thread.start()

    def _beat_loop(self) -> None:
        """Two jobs per beat: enforce the window locally, and read the portal's stop.

        The order matters: ``tick()`` first, so an expired window is closed by its own
        deadline even when the portal is unreachable.
        """
        while not self._beat_stop.wait(self.beat_interval):
            if self.state != OPEN:
                return
            if self.tick() == "expired":
                return
            try:
                reply = self.beat(self._remote_port, self.connections)  # type: ignore[misc]
            except Exception as exc:
                logger.info("help: heartbeat failed (portal unreachable?): %s", exc)
                continue
            if isinstance(reply, dict) and reply.get("stop"):
                self.close("portal")
                return

    # ── internals ─────────────────────────────────────────────────────────
    def _emit(self, event: str, **kv: Any) -> None:
        if self.on_event:
            try:
                self.on_event({"at": self.clock(), "event": event, **kv})
            except Exception as exc:              # 审计不能反过来弄坏会话
                logger.warning("help: audit sink failed: %s", exc)

    def _close_listener(self) -> None:
        if self.server is not None:
            try:
                self.server.stop()
            except Exception as exc:
                logger.warning("help: cannot stop the SSH server: %s", exc)


#: A command that proves a session works without running anything the supporter did
#: not ask for. Chosen to exist on all three shells.
PROBE_MARK = "mrrc-help-ok"


def probe_command() -> str:
    if os.name == "nt":
        return f"Write-Output {PROBE_MARK}"
    return f"echo {PROBE_MARK}"


def platform_shell() -> list[str]:
    """The interactive shell a support session gets on this platform."""
    if os.name == "nt":
        return ["powershell.exe", "-NoLogo"]
    if sys.platform == "darwin":
        return ["zsh", "-l"]
    return ["bash", "-l"]


class _ServerInterface(paramiko.ServerInterface):
    """Password only, session channels only.

    The four result constants below live at paramiko module level (0/2 for auth,
    0/1 for channel open) but paramiko 5.0 does not declare them in its type stubs,
    hence the narrow ignores — they are read at runtime and checked by the tests in
    tests/test_help_session.py, which drive a real client through both answers.

    Deliberately not a general SSH server: one user, one password, one shell per
    channel, and it exists for the length of one help window.
    """

    def __init__(self, owner: "SshHelpServer") -> None:
        self._owner = owner

    def check_auth_password(self, username, password):
        if self._owner.check(username, password):
            return paramiko.AUTH_SUCCESSFUL                 # type: ignore[attr-defined]
        return paramiko.AUTH_FAILED                         # type: ignore[attr-defined]

    def check_auth_publickey(self, username, key):
        # 围栏 6：只允许口令。一把被授权的公钥会变成一个没有口令的后门。
        return paramiko.AUTH_FAILED                         # type: ignore[attr-defined]

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED                  # type: ignore[attr-defined]
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED  # type: ignore[attr-defined]

    def check_channel_pty_request(self, *a, **kw):
        return True

    def check_channel_shell_request(self, channel):
        self._owner.spawn_shell(channel)
        return True

    def check_channel_exec_request(self, channel, command):
        return self._owner.run_exec(channel, command.decode(errors="replace"))


class SshHelpServer:
    """The SSH door itself: bind, authenticate, hand out a shell.

    One instance per help window; ``stop()`` is the only way it ends.
    """

    bind_host = BIND_HOST

    def __init__(self, *, on_event: Optional[Callable[[dict], None]] = None) -> None:
        self.on_event = on_event
        self._user = self._pw = ""
        self._failures = 0
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._children: list = []
        self._key = None

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> int:
        """Bind loopback on a kernel-chosen port and start accepting. Returns the port."""
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.bind_host, 0))     # 0 = 让内核挑一个空闲端口
        self._sock.listen(8)
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop,
                                        name="mrrc-help-ssh", daemon=True)
        self._thread.start()
        return self._sock.getsockname()[1]

    def set_credentials(self, user: str, password: str) -> None:
        """Injected, not generated here: the session owns the pair, and this only
        checks it. Two generators would be two things to keep in step."""
        self._user, self._pw = user, password
        self._failures = 0

    def stop(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        for child in list(self._children):
            try:
                child.close()
            except Exception:
                pass
        self._children.clear()
        self._user = self._pw = ""

    # ── authing ─────────────────────────────────────────────────────────
    def check(self, username: str, password: str) -> bool:
        ok = credentials_match(self._user, self._pw, username, password)
        if not ok:
            self._failures += 1
            if self.on_event:
                self.on_event({"event": "auth_fail", "failures": self._failures})
        return ok

    @property
    def auth_failures(self) -> int:
        return self._failures

    # ── the shell ───────────────────────────────────────────────────────
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                client, _ = sock.accept()
            except OSError:
                return                          # stop() 关了监听
            transport = paramiko.Transport(client)
            try:
                transport.add_server_key(self._host_key())
                transport.start_server(server=_ServerInterface(self))
            except Exception as exc:
                logger.warning("help: SSH handshake failed: %s", exc)
                transport.close()
            self._children.append(transport)

    def spawn_shell(self, channel) -> None:
        """Start the platform shell on this channel and pump both ways."""
        import fcntl
        import pty
        import select
        import struct
        import subprocess
        import termios

        env = dict(os.environ, TERM=channel.get_terminal_type() or "xterm-256color")
        master, slave = pty.openpty()
        try:
            cols, rows = channel.get_terminal_size()
            fcntl.ioctl(slave, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass                                # 尺寸不是要紧的事
        proc = subprocess.Popen(platform_shell(), stdin=slave, stdout=slave,
                                stderr=slave, env=env, close_fds=True)
        os.close(slave)
        started = time.time()
        try:
            while True:
                ready, _, _ = select.select([master, channel], [], [], 1.0)
                if master in ready:
                    data = os.read(master, 4096)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in ready:
                    data = channel.recv(4096)
                    if not data:
                        break
                    os.write(master, data)
        except Exception as exc:
            logger.info("help: shell session ended: %s", exc)
        finally:
            for closer in (proc.terminate, lambda: os.close(master), channel.close):
                try:
                    closer()
                except Exception:
                    pass
            if self.on_event:
                self.on_event({"event": "disconnect",
                               "duration_s": round(time.time() - started, 1)})

    def run_exec(self, channel, command: str) -> bool:
        """Only the probe command is allowed as exec; the supported path is a shell.

        A general exec would be a second, quieter way to run things — and the one
        nobody reviews, because the shell is the obvious path.
        """
        if command.strip() != probe_command().strip():
            # Return False and say nothing else: paramiko answers CHANNEL_FAILURE
            # and the client's exec_command raises. Sending an exit status for a
            # request that was never accepted would be a lie about what happened.
            return False
        channel.sendall((PROBE_MARK + "\n").encode())
        channel.send_exit_status(0)
        # shutdown_write, not close: close() tears the channel down while the
        # client is still waiting for the exec response and it raises "Channel
        # closed" instead of returning. EOF lets it read the output and exit
        # status, and the client closes when it is done (measured, paramiko 5.0).
        channel.shutdown_write()
        return True

    def _host_key(self):
        """A fresh host key per process.

        The supporter will see "host key changed" on every window, and that is
        deliberate: a pinned key is another secret to store, rotate and leak, and
        this door exists for an hour at a time behind a password the customer just
        read out loud.
        """
        if self._key is None:
            self._key = paramiko.RSAKey.generate(2048)
        return self._key


def build_session(*, portal_opener: Callable[[int], int],
                  tunnel: Callable[[int, int, bool], None],
                  portal_closer: Optional[Callable[[], None]] = None,
                  portal_beat: Optional[Callable[[int, int], dict]] = None,
                  on_event: Optional[Callable[[dict], None]] = None) -> HelpSession:
    """Assemble a real session: this module's SSH server is the listener.

    The other three collaborators belong to the caller because they need its
    credentials and its portal address — keeping them out of here is what makes
    this file testable without a tunnel or a portal.
    """
    server = SshHelpServer(on_event=on_event)

    def listener() -> int:
        return server.start()

    return HelpSession(opener=portal_opener, tunnel=tunnel, listener=listener,
                       closer=portal_closer, beat=portal_beat,
                       on_event=on_event, server=server)
