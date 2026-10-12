"""Platform-authenticated session shim for the ported FT8 REST layer.

The platform (``server.py``) owns login, the session token and the control
role.  This module adapts that to the shape the ported FT8 endpoints expect
(``authenticate``/``login``/…), so their bodies stay verbatim.  ``login``
and password re-auth are deliberately inert: those flows live on the
platform's own pages.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Session:
    """One authenticated session view (the platform token's digest)."""

    id: str
    created_epoch: float = 0.0
    touched_epoch: float = 0.0
    reauth_epoch: float | None = None


def host_allowed(host: str | None, allowed_hosts: frozenset[str]) -> bool:
    """NFR-035: the request Host must be one of the configured names."""

    if not host:
        return False
    name = host.split(":", 1)[0].lower()
    return name in {h.lower() for h in allowed_hosts}


def origin_allowed(origin: str | None, host: str | None, allowed_hosts: frozenset[str]) -> bool:
    """NFR-035: Origin (when present) must resolve to an allowed host."""

    if not host or not host_allowed(host, allowed_hosts):
        return False
    if origin is None:
        return True
    marker = "://"
    if marker not in origin:
        return False
    return host_allowed(origin.split(marker, 1)[1], allowed_hosts)


def session_id_for_token(token: str) -> str:
    """Stable, non-secret session handle for one platform token."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


class PlatformAuthAdapter:
    """``AuthService``-shaped adapter over the platform's token check."""

    def __init__(self, verify: Callable[[str], bool],
                 role_of_token: Callable[[str], str] | None = None) -> None:
        self._verify = verify
        self._role_of_token = role_of_token
        self._roles: dict[str, str] = {}

    def authenticate(self, session_id: str | None) -> Session | None:
        token = session_id or ""
        if not token or not self._verify(token):
            return None
        sid = session_id_for_token(token)
        self._roles[sid] = (self._role_of_token(token) if self._role_of_token else "operator")
        return Session(id=sid)

    def role_of(self, session_id: str | None) -> str:
        return self._roles.get(session_id or "", "operator")

    def session_count(self) -> int:
        """Sessions seen since start (the health payload reports this)."""

        return len(self._roles)

    # The FT8 pages never call these — the platform's login page does.
    def login(self, password: str):                     # pragma: no cover
        raise NotImplementedError("login belongs to the platform")

    def logout(self, session_id: str | None) -> None:   # pragma: no cover
        return None

    def reauthenticate(self, session_id: str, password: str) -> bool:  # pragma: no cover
        return self._verify(session_id or "")

    @property
    def reauth_window_s(self) -> float:
        return 0.0

    def has_recent_reauth(self, session_id: str | None) -> bool:
        return bool(session_id and session_id)
