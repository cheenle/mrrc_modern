"""Remote-session concurrency and uplink metering (pure logic, stdlib only).

WHY THIS EXISTS: the cloud-hub design (`mrrc_hub/SDD`, AD-H12 + open issue I-H1)
must not build RX fan-out until it knows how many listeners one instance really
carries and how much uplink they induce. The raw material already lives in
`server.py` (`spectrum_clients` / `audio_rx_clients` / `_listen_tokens`) but
nothing records it, so the numbers that would decide a fan-out refactor are
currently guesses — and the measured per-session split (spectrum ≈ 408 kbps vs
RX audio 64 kbps, i.e. ~86% spectrum) means the guess directly chooses which
subsystem gets refactored.

Deliberately narrow: **counts and bytes only**. No tokens, no client addresses,
no frequencies, no audio — nothing that widens what a diagnostics bundle may
carry (AD-021 / NFR-068) or reveals what an operator was doing.

Scope note: control-plane text frames are not metered. They are <10 kbps per
session against spectrum's 408 kbps, so the two byte paths that matter are
`spectrum` (uplink fan-out) and `audio_rx` (uplink audio). TX audio travels the
other way (client → server) and is deliberately not counted here.

Stdlib only and no application imports on purpose: the module stays unit
testable in isolation and can be hot-fixed without a release.
"""
from __future__ import annotations

import time
from collections import Counter, deque
from typing import Callable, Optional

#: A socket belongs to exactly one role. `listener` means the session
#: authenticated with MRRC_LISTEN_PASSWORD (see server._listen_tokens).
ROLES = ("operator", "listener")

#: One entry per WebSocket endpoint, since the endpoints differ in cost:
#: `spectrum` and `audio_rx` fan out to every client (that is the uplink),
#: `control` / `audio_tx` / `atr` are per-session.
KINDS = ("control", "spectrum", "audio_rx", "audio_tx", "atr")

#: Kinds whose byte counters are meaningful as an uplink measure.
METERED_KINDS = ("spectrum", "audio_rx")

_PEAK_SUFFIXES = ("sessions", "sockets")


class SessionMetrics:
    """Tracks live sockets per (role, kind), distinct sessions, and uplink bytes.

    A *socket* is one WebSocket connection; a *session* is one auth token, which
    may own several sockets (the browser opens control + spectrum + audio_rx for
    a single listener). Concurrency that matters for capacity planning is the
    session count — that is the number of people — so both are reported.

    `clock` is injectable so windowed peaks can be tested without sleeping.
    """

    def __init__(self, window_seconds: float = 3600.0,
                 clock: Callable[[], float] = time.monotonic):
        self._window = float(window_seconds)
        self._clock = clock
        self._started = self._clock()
        self._sockets: Counter[tuple[str, str]] = Counter()
        # (role, token) -> live socket count. Tokens are used as opaque keys
        # only inside this module and are never emitted by snapshot()/report().
        self._sessions: Counter[tuple[str, str]] = Counter()
        self._bytes: Counter[str] = Counter()
        self._bytes_unreported: Counter[str] = Counter()
        # Spectrum fan-out counters per bandwidth tier, e.g.
        # {"high": {"frames": 12, "bytes": 20412}}.  Names come from the caller
        # (spectrum_profile) so this module keeps zero app dependencies.
        self._spectrum_profiles: dict[str, dict[str, int]] = {}
        self._peak_lifetime: dict[str, int] = {
            f"{role}_{suffix}": 0 for role in ROLES for suffix in _PEAK_SUFFIXES
        }
        # (t, listener_sessions, listener_sockets) appended on every change, so
        # the windowed peak is a real observation rather than a decayed number.
        self._samples: deque[tuple[float, int, int]] = deque()
        self._last_report = self._clock()

    # ── mutation ──────────────────────────────────────────────────────

    def open(self, role: str, kind: str, session: Optional[str] = None) -> None:
        """Record a socket going live for `role`/`kind`."""
        self._check(role, kind)
        self._sockets[(role, kind)] += 1
        if session is not None:
            self._sessions[(role, session)] += 1
        self._record()

    def close(self, role: str, kind: str, session: Optional[str] = None) -> None:
        """Record a socket going away; tolerates a double close.

        WebSocket cleanup runs from both the normal return path and the error
        path, and a broadcast may already have dropped the socket from its
        client set, so underflow must not raise or the counters would drift.
        """
        self._check(role, kind)
        if self._sockets[(role, kind)] > 0:
            self._sockets[(role, kind)] -= 1
        if session is not None:
            key = (role, session)
            if self._sessions[key] > 1:
                self._sessions[key] -= 1
            else:
                del self._sessions[key]
        self._record()

    def add_bytes(self, kind: str, nbytes: int) -> None:
        """Count `nbytes` of payload actually written to one client socket."""
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
        if nbytes <= 0:
            return
        self._bytes[kind] += nbytes
        self._bytes_unreported[kind] += nbytes

    def add_spectrum_profile_frame(self, profile: str, nbytes: int) -> None:
        """Count one spectrum frame delivered under one bandwidth tier.

        This is the only place a tier's effect becomes visible: ``add_bytes``
        reports the total but cannot say whether it came from 12 full frames or
        24 short ones.
        """
        if not profile:
            return
        entry = self._spectrum_profiles.setdefault(profile, {"frames": 0, "bytes": 0})
        entry["frames"] += 1
        if nbytes > 0:
            entry["bytes"] += nbytes

    # ── reads ─────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Current concurrency, lifetimes peaks and windowed peaks.

        Safe to expose over HTTP: integers and seconds only, no identifiers.
        """
        listener_sessions = self._sessions_for_role("listener")
        listener_sockets = self._sockets_for_role("listener")
        return {
            "uptime_seconds": round(self._clock() - self._started, 1),
            "window_seconds": self._window,
            "listeners": {
                "sessions": listener_sessions,
                "sockets": listener_sockets,
                "peak_sessions_lifetime": self._peak_lifetime["listener_sessions"],
                "peak_sessions_window": self._peak_window_sessions(),
                "peak_sockets_lifetime": self._peak_lifetime["listener_sockets"],
            },
            "operators": {
                "sessions": self._sessions_for_role("operator"),
                "sockets": self._sockets_for_role("operator"),
                "peak_sessions_lifetime": self._peak_lifetime["operator_sessions"],
                "peak_sockets_lifetime": self._peak_lifetime["operator_sockets"],
            },
            "sockets_by_kind": {
                kind: self._sockets_for_kind(kind) for kind in KINDS
            },
            "uplink_bytes_total": {
                kind: self._bytes[kind] for kind in METERED_KINDS
            },
            "spectrum_profiles": {
                name: dict(entry)
                for name, entry in sorted(self._spectrum_profiles.items())
            },
        }

    def take_report(self) -> dict:
        """Snapshot plus the bytes accumulated since the previous report.

        Resets the since-report counters, so the caller can log one line per
        interval and read `kbps` as an average over `elapsed_seconds`.
        """
        now = self._clock()
        elapsed = max(now - self._last_report, 1e-6)
        self._last_report = now
        snap = self.snapshot()
        since = {}
        kbps = {}
        for kind in METERED_KINDS:
            nbytes = self._bytes_unreported[kind]
            since[kind] = nbytes
            kbps[kind] = round(nbytes * 8 / elapsed / 1000.0, 1)
            self._bytes_unreported[kind] = 0
        snap["elapsed_seconds"] = round(elapsed, 1)
        snap["bytes_since_report"] = since
        snap["kbps_since_report"] = kbps
        return snap

    # ── internals ─────────────────────────────────────────────────────

    @staticmethod
    def _check(role: str, kind: str) -> None:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")

    def _sessions_for_role(self, role: str) -> int:
        return sum(1 for (r, _token), n in self._sessions.items() if r == role and n > 0)

    def _sockets_for_role(self, role: str) -> int:
        return sum(n for (r, _kind), n in self._sockets.items() if r == role)

    def _sockets_for_kind(self, kind: str) -> int:
        return sum(n for (_role, k), n in self._sockets.items() if k == kind)

    def _record(self) -> None:
        """Update peaks from the state we just changed, then timestamp it."""
        for role in ROLES:
            self._peak_lifetime[f"{role}_sessions"] = max(
                self._peak_lifetime[f"{role}_sessions"], self._sessions_for_role(role))
            self._peak_lifetime[f"{role}_sockets"] = max(
                self._peak_lifetime[f"{role}_sockets"], self._sockets_for_role(role))
        self._samples.append((
            self._clock(), self._sessions_for_role("listener"),
            self._sockets_for_role("listener")))
        self._prune()

    def _prune(self) -> None:
        cutoff = self._clock() - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def _peak_window_sessions(self) -> int:
        """Highest listener-session count observed inside the window.

        Never reports less than the current count: with no samples left (no
        change for a whole window) the current value *is* the observation.
        """
        self._prune()
        current = self._sessions_for_role("listener")
        if not self._samples:
            return current
        return max(current, max(s for (_t, s, _sock) in self._samples))
