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
