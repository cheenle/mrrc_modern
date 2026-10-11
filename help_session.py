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
