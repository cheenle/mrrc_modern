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
