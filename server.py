#!/usr/bin/env python3
"""
MRRC Modern Web Control Server
==============================
FastAPI-based web server that bridges a browser to Yaesu FT-710 and Icom
IC-7300/IC-7300MK2 radios via serial CAT/CI-V protocol.  Provides a
WebSocket endpoint for real-time control and state updates, plus REST
APIs for memory channels and authentication.
"""
from __future__ import annotations

import asyncio
from collections import deque
import concurrent.futures
import errno
import hashlib
import hmac
import html
import itertools
import json
import logging
import os
import platform
import re
import secrets as _secrets
import socket
import struct
import sys
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Optional, Protocol

sys.path.insert(0, str(Path(__file__).parent))

# The env-file writer. The desktop trees ship macos/; the Linux images
# deliberately do not — packaging/rpi/build-image.sh's rsync exclusion list drops
# it and packaging/box/box-overlay.sh mirrors that list — so the module the box
# actually carries is the sibling port linux/first_run.py: the same function, and
# the one the first-boot unit already uses. Importing macos unconditionally there
# killed the service with ModuleNotFoundError before its logger existed (the
# published 1.25.4 box image ships that way). Only the absence of macos itself is
# excused: a broken import *inside* macos/ stays a loud failure.
# tests/test_image_imports.py runs this import against the image's tree.
try:
    from macos import first_run
except ModuleNotFoundError as exc:
    if not (exc.name or "").startswith("macos"):
        raise
    from linux import first_run

# The module itself, not only the names: a settings toggle has to *change*
# `ALLOW_UNVERIFIED_TX`, and the backends read it off the module
# (`config.ALLOW_UNVERIFIED_TX` in `backends/*/backend.py`). Rebinding an
# imported name would move a copy and silently change nothing.
import assistant_client
import config

from config import (
    RADIO_MODEL, SERIAL_PORT, BAUD_RATE, WEB_PORT, WEB_HOST, WEB_PASSWORD,
    DEFAULT_WEB_PASSWORD, LISTEN_PASSWORD, PTT_MAX_TX_SECONDS,
    AUDIO_RX_DEVICE, AUDIO_TX_DEVICE,
    SSL_CERTFILE, SSL_KEYFILE,
    AUTH_COOKIE, AUTH_TOKEN_BYTES, MEM_CHANNEL_COUNT,
    UI_MODES, NARROW_MODES,
    ATR1000_HOST, ATR1000_PORT,
    RECORDINGS_BITRATE, RECORDINGS_MAX_SESSION_MIN, RECORDINGS_MIN_FREE_MB,
    CQ_ASSET_PATH,
    SESSION_METRICS_INTERVAL_S, SESSION_METRICS_WINDOW_S,
    REMOTE_SESSION_TX_HEARTBEAT_S,
    _env, default_baud_for, default_user_dir, ENV_REPAIRS,
)
from backends import create_backend, known_models, model_verification
from backends.base import RadioBackend
import box_ops
import box_update
import env_store
import net_bt
import net_wifi
import cloud_hub
import help_session
import net_tls
import spectrum_profile
import sdr_frames
import audio_levels
from sdr_hub import SdrCapacityError, SdrChannelFull, SdrHub
import ssl_bootstrap
import support_bundle
import upgrade_core
from backends.ft710.config_ft710 import (
    MODE_NAME_TO_NUM, BANDS,
    FILTER_WIDTHS_VOICE, FILTER_WIDTHS_NARROW,
)
from backends.ft710.cat_controller import CatController
from radio_state import RadioState
from local_speaker import LocalSpeaker
from recorder import (
    RecorderError, RecordingSession, encoder_available,
    list_recordings, load_index, parse_recording_name, save_index,
)
import cq_asset
from cq_player import CQ_MAX_SECONDS, CQPlayer, CQUnavailable
from session_metrics import SessionMetrics
import host_perf
import nano.flash as nano_flash
from nano import (NanoError, NanoVna, analyse, discover_port, scan_command,
                  to_csv, to_s1p)
from poll_scheduler import PollScheduler
from scope_handler import ScopeHandler  # synthetic scope fallback
from audio_handler import AudioHandler
from opus_rx import (
    RX_RATE,
    RxOpusEncoder, TxOpusDecoder,
    AUDIO_TAG_PCM, AUDIO_TAG_OPUS, DEFAULT_BITRATE, TX_RATE,
)

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, FileResponse, Response,
    PlainTextResponse,
)
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ── Logging ─────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


def _already_logging_to(handlers, path: Path) -> bool:
    """Whether one of ``handlers`` already writes to ``path``.

    File handlers are the only kind that can duplicate a log file; anything without a
    ``baseFilename`` (StreamHandler, the uvicorn handlers) is skipped rather than guessed
    about.
    """
    wanted = os.path.abspath(str(path))
    for handler in handlers:
        base = getattr(handler, "baseFilename", None)
        if base and os.path.abspath(str(base)) == wanted:
            return True
    return False


def _setup_file_logging() -> Optional[Path]:
    """Attach a rotating file handler next to the console one.

    Returns the log path, or None when the directory cannot be created —
    logging must never be the reason the server refuses to start (spec §11).
    Must be called *after* LOG_DIR is defined (support logging block below).

    Idempotent, because the root logger is process-wide: a packaged server can end up with
    this module loaded twice (once as ``__main__``, once as ``server``), and a second
    handler aimed at the same file writes every line twice. The 2026-10-02 field log looked
    exactly like that — each entry paired — which made an already confusing outage read as
    if two things were happening at once.
    """
    import logging.handlers

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / "server.log"
        root = logging.getLogger()
        if _already_logging_to(root.handlers, path):
            return path
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
        return path
    except OSError as e:
        logging.getLogger("mrrc").warning(
            "File logging disabled (%s) — set MRRC_LOG_DIR to a writable directory", e)
        return None


logger = logging.getLogger("mrrc")

# ── Global State ────────────────────────────────────────────────────
radio = RadioState()
backend: RadioBackend | None = None
cat: CatController | None = None
scheduler: PollScheduler | None = None
scope: ScopeHandler | None = None
audio: AudioHandler | None = None

# Connected clients
ctrl_clients: set[WebSocket] = set()
spectrum_clients: set[WebSocket] = set()
# Listen-role spectrum clients get every Nth frame — the "listen" tier below.
# The divider is a ratio, so the delivered rate follows the frame source:
# ~3.7 Hz on the measured 11.1 fps real scope, ~10 Hz in the 30 Hz fallback
# state (V2.59 intent: a phone on a metered link does not need full rate).
_listen_spectrum_clients: set[WebSocket] = set()
# Kept as a module attribute because tests/test_listen_only.py:397 reads it.
LISTEN_SPECTRUM_DIVIDER = spectrum_profile.divider_for(spectrum_profile.LISTEN_PROFILE)

# Tier a socket declared, keyed by the WebSocket object itself.  Absent means
# "this socket never sent spectrumCaps" => the role default (high for an
# operator token, listen for a listener token) => byte-for-byte today's stream.
_spectrum_profiles: dict[WebSocket, str] = {}


def _profile_for(ws: WebSocket) -> str:
    """The spectrum profile in force for one socket.

    Explicit caps win over the role default, and stick for the socket's life.
    """
    declared = _spectrum_profiles.get(ws)
    if declared:
        return declared
    if ws in _listen_spectrum_clients:
        return spectrum_profile.LISTEN_PROFILE
    return spectrum_profile.DEFAULT_PROFILE


def _spectrum_frame_due(ws: WebSocket, tick: int) -> bool:
    """Throttle gate for the spectrum fan-out (see above)."""
    return spectrum_profile.frame_due(tick, spectrum_profile.divider_for(_profile_for(ws)))
audio_rx_clients: set[WebSocket] = set()
audio_tx_clients: set[WebSocket] = set()
# ATR1000 external tuner (optional; None = feature disabled)
atr = None                    # ATR1000Client, lazily imported in lifespan
atr_clients: set[WebSocket] = set()

# Remote-session metering (session_metrics.py): the listener/operator
# concurrency and uplink numbers the hub's RX fan-out decision waits on
# (mrrc_hub AD-H12 / open issue I-H1). Counts and bytes only — no identifiers.
metrics = SessionMetrics(window_seconds=SESSION_METRICS_WINDOW_S)
_session_metrics_task: asyncio.Task | None = None

# Host/process load for the 「性能负载」 page (host_perf.py). One sampler for the
# whole process: rates are deltas, so two callers must not each keep a baseline
# (they would halve each other's intervals and report nonsense).
perf_sampler = host_perf.Sampler()
# Event-loop scheduling delay. This is the number that separates "the box is
# doing work" from "the server is stuck": CPU can be idle while every await is
# late, and then no amount of CPU headroom explains the operator's lag.
perf_lag = {"last_ms": 0.0, "peak_ms": 0.0, "samples": 0}


def _role_for_token(token: str) -> str:
    """Metering role for an authenticated token (see _listen_tokens)."""
    return "listener" if token in _listen_tokens else "operator"


# TX-phase liveness gate (SDD ch15 §15.6, hub AD-H06). Opt-in via
# MRRC_REMOTE_SESSION_TX_HEARTBEAT_S; the cloud hub sets it. A session becomes
# gated by *declaring* capability — sending `txhb` — so a client that predates
# the mechanism is never unkeyed by something it does not implement.
_tx_hb_capable: set[WebSocket] = set()
_tx_hb_last: dict[WebSocket, float] = {}
_tx_liveness_task: asyncio.Task | None = None
TX_LIVENESS_TICK_S = 0.25   # detection latency is threshold + at most one tick


def _tx_liveness_timeout() -> Optional[WebSocket]:
    """The keying session whose heartbeat went stale, or None.

    All three conditions must hold, and each corresponds to a failure the
    design review insisted must not release:
      * the feature is enabled (0 = off is the shipped default);
      * the *keying* session is the one that timed out — a stale listener, or a
        LAN client keying while a remote session dies, must never drop someone
        else's carrier (ch15 Layer 4 key-owner arbitration);
      * that session declared capability and has sent at least one heartbeat, so
        keying immediately after connecting is not a false positive.
    """
    if REMOTE_SESSION_TX_HEARTBEAT_S <= 0:
        return None
    ws = _ptt_key_ws
    if ws is None or ws not in _tx_hb_capable:
        return None
    last = _tx_hb_last.get(ws)
    if last is None:
        return None
    if time.monotonic() - last <= REMOTE_SESSION_TX_HEARTBEAT_S:
        return None
    return ws


async def _tx_liveness_watchdog():
    """Unkey when the transmitting session stops proving it is alive.

    Covers the one failure mode the other layers cannot see: a connection that
    never closes — NAT entry dropped, Wi-Fi switched, packets silently discarded
    — where Layer 4's dead-man switch never fires because there is no TCP
    teardown to observe. Fire-and-forget unkey, no verify loop (V1.2 rule).
    """
    global _ptt_key_ws
    while True:
        await asyncio.sleep(TX_LIVENESS_TICK_S)
        if not (cat is not None and cat.connected and radio.is_transmitting):
            continue
        ws = _tx_liveness_timeout()
        if ws is None:
            continue
        age = time.monotonic() - _tx_hb_last.get(ws, 0.0)
        logger.warning(
            "PTT safety: no session heartbeat for %.1fs while transmitting "
            "(limit %.1fs) — forcing RX", age, REMOTE_SESSION_TX_HEARTBEAT_S)
        try:
            await cat.set_ptt(False)
            _ptt_key_ws = None
            radio.update(tx_status=0, power_meter=0, alc_meter=0,
                         swr_meter=0, comp_meter=0, id_meter=0)
        except Exception as e:
            logger.error("PTT safety force-RX failed: %s", e)
        scheduler and scheduler.skip_next_poll("tx_status", 1.0)
        _tx_hb_capable.discard(ws)
        _tx_hb_last.pop(ws, None)
        msg = json.dumps({"type": "error", "message": (
            "Link lost while transmitting — PTT released locally. "
            "Check your network before keying again.")})
        dead: set[WebSocket] = set()
        for client in ctrl_clients:
            try:
                await client.send_text(msg)
            except Exception:
                dead.add(client)
        ctrl_clients.difference_update(dead)


def _session_metrics_line(r: dict) -> str:
    """Render one Session-metrics log line (pure, so it is testable).

    The kbps figures are *payload* bytes: they are metered inside Starlette, so
    whatever permessage-deflate does downstream is invisible here (browsers
    negotiate it — measured 1701 B -> ~441 B on this payload — while Android's
    OkHttp does not, so a phone really does pay the payload).  Labelling the
    number "payload≈" is the point: it had been read as a wire figure.

    The per-tier frame counts are what makes a bandwidth tier auditable from a
    log alone — bytes tell you the total, frames tell you which tier produced it.
    """
    profiles = r.get("spectrum_profiles") or {}
    by_profile = "/".join(
        f"{name}={entry.get('frames', 0)}" for name, entry in profiles.items()
    ) or "-"
    listeners = r["listeners"]
    kbps = r["kbps_since_report"]
    return (
        f"Session metrics: listeners {listeners['sessions']} "
        f"(sockets {listeners['sockets']}, peak "
        f"{int(listeners['peak_sessions_window'])}/{r['window_seconds']:.0f}s) | "
        f"operators {r['operators']['sessions']} | payload\u2248 "
        f"uplink spectrum {kbps['spectrum']:.1f} kbps, "
        f"audio_rx {kbps['audio_rx']:.1f} kbps | "
        f"spectrum frames {by_profile} | {r['elapsed_seconds']:.0f}s since last report"
    )


async def _session_metrics_loop():
    """One INFO line per interval with measured concurrency and uplink.

    This is the observation the hub's fan-out decision waits on: the design
    refuses to build RX fan-out before the real listener-concurrency
    distribution is known (mrrc_hub AD-H12 / I-H1), and the numbers have to come
    from an instance that is actually serving listeners, not from a load test.
    """
    while True:
        await asyncio.sleep(SESSION_METRICS_INTERVAL_S)
        try:
            r = metrics.take_report()
        except Exception as e:      # a log line must never take the server down
            logger.debug("Session metrics report failed: %s", e)
            continue
        logger.info("%s", _session_metrics_line(r))


_atr_storage = None           # TunerStorage shared with the client
_atr_tune_task = None         # running tune-assist asyncio.Task, if any
_last_meter_broadcast_log = 0.0
# Opt-in stuck-keyup watchdog (MRRC_PTT_MAX_TX_SECONDS, 0 = off). Covers
# clients that hang WITHOUT disconnecting — the dead-man switch and client
# watchdogs never fire for a zombie-but-connected socket.
_max_tx_watchdog_task: asyncio.Task | None = None
_tx_continuous_since: float | None = None
MAX_TX_WATCHDOG_INTERVAL = 1.0  # seconds between watchdog checks (tests shrink this)
# Single-owner guard for the TX uplink: only the first connected TX client's
# audio is fed to the radio. Prevents two open tabs from interleaving mic
# frames into the same playback queue (garbled TX). A later client takes over
# only after the current owner disconnects.
_tx_owner_ws: Optional[WebSocket] = None
# Auth token per websocket — lets the PTT handler find the TX-audio socket
# that belongs to the client keying the radio.
_ws_tokens: dict = {}
# TX-audio session diagnostics (reset on every PTT key-up, logged on release)
_tx_session_frames = 0      # owner frames received over /WSaudioTX
_tx_session_decoded = 0     # frames successfully decoded + fed to the device queue
_tx_session_decode_fail = 0 # Opus frames that failed to decode
_tx_non_owner_drops = 0     # mic frames ignored from non-owner clients
_tx_cq_mic_drops = 0        # mic frames ignored while a CQ call owns TX

# Control-plane mirror of the TX-audio owner: while a client holds the key,
# only that client's release is honored. Field log 2026-09-25: two
# full-control clients produced 75 TX sessions in 5 minutes (16 with zero
# mic frames, three releases 30 ms apart) because every browser tab runs its
# own PTT watchdog (ptt_manager.js) which re-sends ptt:false whenever ITS
# radioState.tx_status != 0 — it cannot distinguish "my release did not
# land" from "another client just keyed", so an idle tab kept unkeying the
# tab that was actually transmitting (SDD I6, PTT half).
_ptt_key_ws: Optional[WebSocket] = None
_ptt_foreign_releases = 0   # releases ignored because another client keyed


def _ptt_release_allowed(ws) -> bool:
    """True when `ws` may drop the carrier (it keyed, or nobody did)."""
    return _ptt_key_ws is None or ws is _ptt_key_ws


def _promote_tx_owner():
    """Promote a remaining TX-audio client to owner after the owner left.

    Without this, all remaining clients stay muted forever once the owner
    disconnects (ownership was only ever assigned at connect time) — the
    radio keys via /WSradio but no mic frames reach the device: the
    "PTT keys but no voice / no power" soft failure.
    """
    global _tx_owner_ws
    _tx_owner_ws = None
    for tx_ws in list(audio_tx_clients):
        _tx_owner_ws = tx_ws
        return tx_ws
    return None


def _claim_tx_owner_for_token(token: Optional[str]):
    """Make the TX-audio client authenticated with `token` the uplink owner.

    Called on PTT key-up: the device that keys the radio is the one whose
    voice must go out.  Fixes the multi-client case (browser tab + iOS app,
    or a stale zombie owner) where an idle first-connected client owned the
    uplink and the PTT-ing client's mic frames were silently dropped.

    ``token`` is Optional in practice: the caller passes ``_ws_tokens.get(ws)``,
    which is None for a socket that never carried an auth token.

    A token is not a socket: one browser session can hold several TX-audio
    sockets (page reload, two tabs — the token is a 30-day cookie), so among
    the matches the LAST connected one wins. Taking the first match scanned out
    of a set resolved the uplink by iteration order and could hand it to a
    half-open predecessor, keying a silent carrier (see `_tx_socket_seq`).
    """
    global _tx_owner_ws
    if not token:
        return None
    newest = None
    newest_seq = -1
    for tx_ws in list(audio_tx_clients):
        if _ws_tokens.get(tx_ws) != token:
            continue
        seq = _tx_socket_seq.get(tx_ws, -1)
        if newest is None or seq > newest_seq:
            newest, newest_seq = tx_ws, seq
    if newest is None:
        return None
    if newest is not _tx_owner_ws:
        _tx_owner_ws = newest
        logger.info("TX audio ownership → PTT client")
    return newest


def _assign_tx_owner_on_connect(ws: WebSocket, token: str) -> bool:
    """Assign the TX uplink when an audio WebSocket connects.

    A replacement socket authenticated by the current owner's token wins.
    This is the page-reload/network-recovery case: the old browser socket can
    remain half-open long enough to mute every frame from its replacement.
    A different authenticated session cannot steal an active uplink merely by
    connecting; its control socket may claim ownership when it actually keys.
    """
    global _tx_owner_ws
    if _tx_owner_ws is None or _ws_tokens.get(_tx_owner_ws) == token:
        replaced = _tx_owner_ws is not None and _tx_owner_ws is not ws
        _tx_owner_ws = ws
        if replaced:
            logger.info("TX audio ownership → replacement connection")
        return True
    return False


# Connect order per TX-audio socket. The session token is a 30-day cookie, so
# ONE browser session can hold several /WSaudioTX sockets at once: a page
# reload leaves its predecessor half-open for tens of seconds, and two tabs of
# one login share the token outright. Resolving a token to a socket by
# scanning `audio_tx_clients` (a set) therefore picked an arbitrary one —
# field log 2026-10-05 19:05:34, a hub instance: the key-up claim landed on
# the dead predecessor, so the radio keyed a silent carrier for 58 s while
# every frame of the reloaded page was dropped as non-owner
# (`frames=0 non_owner_drops=236`, `peak=0%`); TX returned only when the stale
# socket was reaped and `_promote_tx_owner()` handed over. Last connected is
# the live page, which is what SDD §15 Layer 4 means by "a same-session
# replacement audio socket supersedes a half-open page-reload socket".
_tx_socket_seq: dict = {}
_tx_socket_counter = itertools.count()


def _register_tx_socket(ws: WebSocket) -> int:
    """Record a TX-audio socket's connect order (see `_tx_socket_seq`)."""
    seq = next(_tx_socket_counter)
    _tx_socket_seq[ws] = seq
    return seq


def _forget_tx_socket(ws: WebSocket) -> None:
    """Drop a TX-audio socket's per-socket state — idempotent."""
    _tx_socket_seq.pop(ws, None)


def _same_session_uplink_takeover(ws: WebSocket) -> bool:
    """Hand the uplink to a same-session socket that is actually streaming.

    A shared token means one authenticated browser session, so a mic frame
    arriving on a non-owner socket is the operator's own live page — the second
    tab, or the reload whose predecessor still holds the claim — never a
    stranger. Taking over on the first frame self-heals within one 20 ms frame
    instead of muting the whole transmission, and covers the case a connect-
    time rule cannot: two same-session sockets that are BOTH live, where the
    operator happens to work in the one that connected first.

    A *different* session still cannot steal an active uplink by streaming at
    it — that is what the single-owner guard exists for.
    """
    global _tx_owner_ws
    owner = _tx_owner_ws
    if owner is None or owner is ws:
        return False
    token = _ws_tokens.get(ws)
    if not token or _ws_tokens.get(owner) != token:
        return False
    _tx_owner_ws = ws
    logger.info("TX audio ownership → same-session streaming socket")
    return True

_state_broadcast_task: asyncio.Task | None = None
_scope_producer = None          # backends.base.ScopeProducer, created in lifespan
_scope_broadcast_task: asyncio.Task | None = None
_audio_rx_task: asyncio.Task | None = None
_audio_tx_task: asyncio.Task | None = None
_rx_restart_task: asyncio.Task | None = None

# TX Opus decoder (browser mic → server)
_opus_tx_decoder: TxOpusDecoder | None = None

# RX broadcast pacing/guard rails
AUDIO_RX_SEND_TIMEOUT = 0.012          # per-frame per-client timeout (seconds)
RX_SILENCE_ALERT_S = 20                # zero-audio watchdog: alert after this many seconds of bit-exact silence
# Capture-stall watchdog. ``read_rx_chunk()`` returns None both for "no data yet"
# and for "this stream is dead", so a capture that stops delivering is invisible:
# the loop keeps its 50 Hz pace, sends nothing, and the only symptom is at the
# listener's ear. It happens for real — the W103D's ALSA/xhci path stops the
# capture interface on an overflow while PyAudio still reports an open, error-free
# stream (2026-10-11: 4% coverage and then permanent silence; see
# config.RX_HOST_BUFFER_MS for the buffer half of the fix). Alert quickly and
# reopen the stream, but rate-limit the reopen: a dead radio must not turn this
# into a reopen storm.
RX_STALL_ALERT_S = 3.0                 # no PCM for this long, with a client listening, is a stall
RX_STALL_RESTART_MIN_GAP_S = 20.0      # at most one watchdog-driven reopen per this window
METER_BROADCAST_LOG_INTERVAL_SECONDS = 0.5

# Auth: valid session tokens (server-side, cleared on restart)
_auth_tokens: set[str] = set()
# When each live token was issued (epoch).  Only needed to age tokens out of
# the on-disk store, so entries may be missing for tokens restored from a
# file written by an older build.
_token_issued: dict[str, float] = {}
# Session tokens are persisted: the login cookie lives 30 days, and before
# this store existed every restart — including the routine restarts an
# upgrade performs — silently invalidated it, so browsers kept being bounced
# to the login page while holding a perfectly valid cookie.
_AUTH_TOKENS_MAX_AGE_S = 30 * 24 * 3600  # same lifetime as the cookie
# Subset of _auth_tokens authenticated with the listen-only password
# (MRRC_LISTEN_PASSWORD). A listen session may tune frequency and mode and
# receive audio/spectrum, but every transmit/device-setting path is refused
# server-side (the browser UI being hidden is never the enforcement).
_listen_tokens: set[str] = set()
# SDR 接收机（AD-029）的独立口令角色：这些 token 只能进 /sdr 与 /WSsdr，
# 反过来 operator/listen token 也进不来 —— 两条入口互不通用。
_sdr_tokens: set[str] = set()
# /WSradio "set" fields a listen-only session may use. memRecall is allowed
# separately — it only ever writes frequency + mode.
LISTEN_ALLOWED_SET_FIELDS = frozenset({"freq", "vfo_a_freq", "vfo_b_freq", "mode"})
LISTEN_ONLY_MESSAGE = (
    "Listen-only session: only frequency and mode can be changed."
)

# Memory channels file
SCRIPT_DIR = Path(__file__).parent


def _runtime_dir() -> Path:
    """Return the directory containing runtime files for source or frozen mode."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return SCRIPT_DIR


def _writable_runtime_dir() -> Path:
    """Where runtime state goes when nothing pointed us at a writable place.

    ``_runtime_dir()`` is the install directory in a packaged build, and on Windows that is
    ``C:\\Program Files\\...`` — readable, not writable. The launchers set
    ``MRRC_MEM_FILE``/``MRRC_RECORDINGS_DIR`` for the servers they spawn, but the server is a
    standalone entry-point exe too (the installer even ships a shortcut for it), and a
    server started on its own then tries to write next to itself. The field log of
    2026-10-02 shows what that costs:

        could not save the generated password ([Errno 13] Permission denied:
            'C:\\Program Files\\MRRC Modern\\mrrc_modern.env.tmp')

    which means a fresh random admin password on every start, none of them ever readable by
    the user — "installed it and it will not log me in" — plus ``Recording disabled:
    C:\\Program Files\\MRRC Modern\\recordings is not writable``. So a default that lands
    in a directory this process cannot write falls back to the per-user directory, which is
    already what ``LOG_DIR``, the certificate directory and the config file use.

    A packaged build goes straight to the per-user directory without even testing
    writability, because there "writable" is not the same as "correct". Measured on the
    frozen macOS v1.24.6 bundle: a bare start put ``recordings/`` inside
    ``MRRC-Modern.app/Contents/MacOS/``, the owner can write it, and a single file in it
    breaks the code signature —

        dist/macos/MRRC-Modern.app: a sealed resource is missing or invalid
        file added: .../MRRC-Modern.app/Contents/MacOS/recordings/probe.mp3

    — so recording one QSO would leave the app failing signature checks on the next launch,
    the same class of accident as v1.18.1. Windows has the mirror-image problem: an install
    under a writable prefix would keep state that a reinstall or a roaming profile silently
    discards. Both launchers already point ``MRRC_MEM_FILE`` and ``MRRC_RECORDINGS_DIR`` at
    the per-user directory, so this also removes the last way for a bare start and a
    launcher start to disagree about where the same files live.
    """
    if getattr(sys, "frozen", False):
        # Never keep writable state next to packaged code: on macOS that is inside a signed
        # bundle, on Windows inside the install prefix.
        return default_user_dir()
    base = _runtime_dir()
    try:
        if os.access(base, os.W_OK):
            return base
    except OSError:                                        # odd filesystem: use user dir
        pass
    return default_user_dir()


def _resource_dir() -> Path:
    """Return the directory containing bundled read-only resources.

    PyInstaller 6 onedir builds place datas (static/, mem_channels.json)
    under sys._MEIPASS (the "_internal" directory next to the exe), not
    next to the exe itself.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return _runtime_dir()


STATIC_DIR = _resource_dir() / "static"
MEM_FILE = Path(_env("MRRC_MEM_FILE", str(_writable_runtime_dir() / "mem_channels.json")))
# Where live session tokens are persisted (see _save_auth_tokens).
_AUTH_TOKENS_FILE: Path = MEM_FILE.parent / "auth-tokens.json"

# Box-level audio gains (AD-032): the TX microphone uplift and the RX level handed
# to listeners.  One owner each, persisted beside the other runtime state, both
# defaulting to 0 dB — a box that never touched the knob sends exactly the bytes
# it sent before this existed.  The store only holds a path here; the file is
# read in the lifespan, because importing this module must not touch the disk.
AUDIO_LEVELS_FILE = Path(_env("MRRC_AUDIO_LEVELS_FILE",
                              str(MEM_FILE.parent / "audio-levels.json")))
_audio_levels_store = audio_levels.AudioLevelsStore(AUDIO_LEVELS_FILE)
_audio_levels = audio_levels.AudioLevels()
_audio_levels_clip = audio_levels.ClipMonitor()

# Custom CQ (2026-10-26): the operator's own recording, kept beside the other
# runtime state because `static/` is replaced wholesale by every box update — a
# take stored there would disappear on the next fix pack.  The shipped asset
# stays the factory default; this file only wins while it exists and loads.
CQ_CUSTOM_PATH: Path = Path(_env("MRRC_CQ_CUSTOM_FILE", str(MEM_FILE.parent / "cq.wav")))


def _cq_effective_path() -> Path:
    """The asset the CQ key will play: the operator's take, else the shipped one."""
    return CQ_CUSTOM_PATH if CQ_CUSTOM_PATH.is_file() else CQ_ASSET_PATH

# ── 产品 AI 助手（规格 docs/superpowers/specs/2026-10-30-ai-assistant-design.md）──
# 知识库在**中央**，本机只做转发 —— 所以断网就没有助手，面板会直说。关掉就整块消失
# （页面 404）。listen / sdr 会话一律 403：助手会看到本机状态摘要，那不是给陌生人的。
# 默认**关**（偏离规格 §10 的「默认开」，理由写进 CHANGELOG）：中央网关还没部署时，
# 打开只会让每一台盒子在 ☰ 里看到一个点不开的入口。网关就位后打开只需要两行 env
# （MRRC_ASSISTANT=on + URL/TOKEN），不用发版 —— 这正是它做成开关的意义。
ASSISTANT_ENABLED = (_env("MRRC_ASSISTANT", "off") or "off").strip().lower() == "on"
ASSISTANT_URL = (_env("MRRC_ASSISTANT_URL", "") or "").strip()
ASSISTANT_TOKEN = (_env("MRRC_ASSISTANT_TOKEN", "") or "").strip()

#: 最近几条错误的环形缓冲（助手摘要里最多取 3 条）。只存"错误码 + 一句人话"，
#: 归一化由 `_note_error` 负责：路径、呼号、频率都在这里被削掉。
_ERROR_RING: "deque[tuple[str, str]]" = deque(maxlen=8)


def _note_error(component: str, message: str) -> None:
    """记一条归一化错误。**这是助手唯一能看到的本机运行信息**，所以只留骨架。"""
    text = re.sub(r"\s+", " ", str(message or ""))[:120]
    _ERROR_RING.append((str(component or "app"), text))


def _recent_error_codes(limit: int = 3) -> list:
    return [f"{component}:{message}" for component, message in list(_ERROR_RING)[-limit:]]


def _served_fixpack() -> str:
    try:
        return box_update.read_fixpack(_runtime_dir()) or ""
    except Exception:                                  # noqa: BLE001 - 读不到就当没有
        return ""


def _assistant_prompt_state() -> dict:
    """喂给助手的白名单事实源。键集由 assistant_client.ALLOWED_KEYS 决定。"""
    return assistant_client.state_summary({
        "model": RADIO_MODEL,
        "version": support_bundle.detect_version(_runtime_dir(), _resource_dir()),
        "fixpack": _served_fixpack(),
        "serial_connected": bool(getattr(radio, "serial_connected", False)),
        "audio_rx_open": bool(getattr(audio, "_rx_stream", None)) if audio else False,
        "audio_tx_open": bool(getattr(audio, "_tx_stream", None)) if audio else False,
        "last_errors": _recent_error_codes(3),
        "platform": f"{sys.platform}-{platform.machine()}",
        "ui_lang": "zh",
    })


def _assistant_gate(request):
    """三道门：开关 → 鉴权 → 角色。返回 (str, status) 或 None。"""
    if not ASSISTANT_ENABLED:
        return ("assistant disabled", 404)
    if not _verify_auth(request):
        return ("Unauthorized", 401)
    forbidden = _perf_forbidden(request)
    if forbidden is not None:
        return (forbidden[1], forbidden[0])
    return None

# Recordings live in a directory of media files (mrrc layout) plus a small
# JSON index next to the other runtime files; the launchers point
# MRRC_RECORDINGS_DIR at the per-user data directory on packaged installs.
RECORDINGS_DIR = Path(_env("MRRC_RECORDINGS_DIR", str(_writable_runtime_dir() / "recordings")))
RECORDINGS_INDEX = _writable_runtime_dir() / "recordings.json"

# ── Support logging (spec 2026-09-17 §5) ──────────────────────────
# The packaged desktop app had NO server log file (logging went to the console
# window only), so a diagnostics bundle would have been empty.  The launchers
# set MRRC_LOG_DIR to the user data directory — Program Files and /Applications
# are not writable — while the default keeps source and Raspberry Pi runs
# working.  support-out/ holds the built bundles (newest 5, spec §7).
# Default to the user's own data directory: the packaged directory is read-only for a normal user,
# so a bare start (without the launcher, which sets MRRC_LOG_DIR) had no logs at all - reported as
# "File logging disabled ([WinError 5] 拒绝访问 : 'C:\Program Files\MRRC Modern\logs')".
LOG_DIR = Path(_env("MRRC_LOG_DIR", str(default_user_dir() / "logs")))
SUPPORT_OUT_DIR = LOG_DIR.parent / "support-out"
_update_lock = threading.Lock()
SUPPORT_URL = _env("MRRC_SUPPORT_URL", "https://www.vlsc.net/mrrc_modern/support/")
UPDATE_MANIFEST_URL = _env("MRRC_UPDATE_MANIFEST_URL", upgrade_core.DEFAULT_MANIFEST_URL)
# Attach the file handler now that LOG_DIR exists (it needs the constant).
SUPPORT_LOG_FILE = _setup_file_logging()

# Process start (for /api/health uptime; reset by the lifespan below).
STARTUP_TIME = time.time()

# ── Recording: server-side QSO session (AD-017) ─────────────────────
# One writer task owns the session so the timeline cursor and the MP3
# encoder are never touched concurrently, and encoder CPU stays off the
# event loop.  The task is one-shot per session (it returns when the MP3 is
# finalised, which is what shutdown awaits) — `_ensure_rec_writer()` creates
# it for every session, not just at boot.
REC_QUEUE_MAX = 200            # ~16 s of audio at 80 ms blocks: a stalled
                               # writer must not grow memory without limit
_rec_queue: "asyncio.Queue" = asyncio.Queue(maxsize=REC_QUEUE_MAX)
# Dedicated single-thread pool: the recorder must never compete for the
# default executor, which the serial/audio paths can monopolise (a CAT
# reconnect storm starved the writer and every RX block was dropped —
# 2026-09-12 field report).  Encoding an 80 ms block measures ~0.04 ms,
# so one thread is far more than enough.
_rec_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="recorder")
_rec_failures = 0
_rec_writer_task: Optional["asyncio.Task"] = None
_rec_session = RecordingSession(RECORDINGS_DIR,
                                bitrate=RECORDINGS_BITRATE,
                                max_seconds=RECORDINGS_MAX_SESSION_MIN * 60)

# ── CQ call (spec 2026-09-13): server-side one-shot CQ playback ──────
# The player is (re)bound to the live audio handler in the lifespan by
# _bind_cq_player(); before that it only knows the asset path.
_cq_player = CQPlayer(asset_path=_cq_effective_path())

# ── FT8 mode (dormant until the operator switches; see ft8/) ─────────────
_ft8_mode = None                # Ft8ModeController (created in the lifespan)
_ft8_runtime = None             # Ft8Runtime
_ft8_settings_store = None      # Ft8SettingsStore
_ft8_owns_tx = False            # True while FT8 mode owns the transmitter


def _log_recording_readiness() -> None:
    """Report recording readiness at startup (missing deps, read-only dir).

    Recording is optional, so this warns instead of failing the boot — but
    the operator must be able to see *why* REC would refuse before pressing
    it (field report 2026-09-12: the reason only surfaced as a dropped
    WebSocket).
    """
    if not encoder_available():
        logger.warning(
            "Recording disabled: the MP3 encoder is missing — run "
            "`pip install -r requirements.txt` (or `pip install lameenc`) "
            "and restart. CAT, audio and spectrum are unaffected.")
        return
    try:
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        probe = RECORDINGS_DIR / ".write_test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as e:
        logger.warning(
            "Recording disabled: %s is not writable (%s) — set "
            "MRRC_RECORDINGS_DIR to a writable directory and restart.",
            RECORDINGS_DIR, e)
        return
    logger.info("Recording ready: %s (16 kHz mono MP3)", RECORDINGS_DIR)


def _log_cq_readiness() -> None:
    """Warn at startup when the CQ key is unavailable (missing/corrupt asset).

    The ready case is announced by ``CQPlayer.load()`` itself, so this only
    adds the actionable half: *why* it is disabled and the env var that fixes
    it.  Pressing CQ must never fail silently (same reasoning as the
    recording readiness check).
    """
    if not _cq_player.ready:
        logger.warning("CQ key disabled: %s — set MRRC_CQ_FILE to a "
                       "16-bit WAV file and restart.",
                       _cq_player.unavailable_reason)


def _recording_should_capture_rx() -> bool:
    """RX is recorded only while not transmitting.

    During PTT/TUNE the radio returns sidetone/duplex audio; recording it
    would double the timeline (the sibling mrrc project uses the same rule).
    """
    return not (radio is not None and radio.tx_status)


def _recording_space_problem() -> str:
    """Why a new recording must not start right now (`""` = there is room).

    The recorded stream is the one unbounded writer an operator can switch on —
    16 kHz mono MP3 at the default 64 kbps is ~29 MB per hour — and a disk that
    fills does not fail politely: the recording dies mid-QSO and the config
    writes, the DB and everything else that needs a byte go with it. Refusing to
    start is the *non-destructive* half of a retention policy: pruning old QSOs
    would be the destructive half, and this deliberately does not do it.

    Unmeasurable is not a refusal (spec 2026-09-17 §11 spirit): a platform where
    ``disk_usage`` throws must not lose a feature over a diagnostic.
    """
    if RECORDINGS_MIN_FREE_MB <= 0:
        return ""
    import shutil

    try:
        free_mb = shutil.disk_usage(str(RECORDINGS_DIR)).free // (1024 * 1024)
    except OSError:
        return ""
    if free_mb >= RECORDINGS_MIN_FREE_MB:
        return ""
    logger.warning("Recording refused: %d MB free, %d MB required",
                   free_mb, RECORDINGS_MIN_FREE_MB)
    return (f"磁盘剩余空间不足（{free_mb} MB，至少需要 "
            f"{RECORDINGS_MIN_FREE_MB} MB），录音未开始。"
            f"请先删除一些旧录音（☰ → 录音）再试。")


def _ensure_rec_writer() -> None:
    """Guarantee that a writer task is waiting on the queue.

    The writer loop is **one-shot per session**: it returns once the MP3 is
    finalised, which is what lets shutdown await its completion.  Creating it
    only in the lifespan made it one-shot per *process*: after the first
    REC/STOP the task was done, nothing drained the queue, the 200-block
    backlog filled within milliseconds and every block of every later
    recording was dropped — and stop() then padded the timeline with silence,
    leaving a correctly-sized, 100 % silent MP3 (field report 2026-09-12
    22:26, "Recording dropped 950 block(s) so far").

    Idempotent and cheap, so the taps can call it on every block: an active
    session must never be without a consumer.
    """
    global _rec_writer_task
    task = _rec_writer_task
    if task is not None and not task.done():
        return
    if task is not None and not task.cancelled():
        try:
            failure = task.exception()
        except asyncio.CancelledError:      # cancelled between the checks
            failure = None
        if failure is not None:
            # A writer that died mid-session looks exactly like a healthy one
            # from the outside: the queue just stops draining.  Never let that
            # pass unnoticed again.
            logger.warning("Recording writer failed — restarting it: %s",
                           failure)
    try:
        asyncio.get_running_loop()
    except RuntimeError:                       # no loop (sync tests/tools)
        return
    _rec_writer_task = asyncio.create_task(
        _recording_writer_loop(_rec_queue, _rec_session), name="rec_writer")


def _rec_enqueue(source: str, pcm: bytes, rate: int,
                 timestamp_ns: Optional[int] = None) -> None:
    """Queue one PCM block for the writer (never blocks, never raises).

    Slowness here means a recording with holes: the count is tracked per
    session and surfaced in the UI status, and the first drop is logged
    with the reason so a field report can be diagnosed immediately.
    """
    if not _rec_session.active:
        return
    _ensure_rec_writer()
    try:
        _rec_queue.put_nowait((source, pcm, rate, timestamp_ns))
    except asyncio.QueueFull:
        _rec_session.dropped += 1
        if _rec_session.dropped == 1:
            logger.warning(
                "Recording writer is falling behind — dropping audio (the "
                "recording will have holes). Check disk speed / CPU load "
                "for %s.", RECORDINGS_DIR)
        elif _rec_session.dropped % 50 == 0:
            logger.warning("Recording dropped %d block(s) so far",
                           _rec_session.dropped)


def _rec_enqueue_stop():
    """Ask the writer to finish the session; returns a future for the result.

    Returning a future lets the WebSocket handler answer only once the MP3
    has been flushed (so the broadcast carries the final size and the
    button flips back to REC exactly when the file is complete).
    """
    fut = None
    try:
        fut = asyncio.get_running_loop().create_future()
    except RuntimeError:                               # no loop (tests/tools)
        fut = None
    _ensure_rec_writer()
    try:
        _rec_queue.put_nowait(("stop", b"", 0, fut))
    except asyncio.QueueFull:
        # A stalled writer left a full backlog.  The oldest block is the least
        # valuable item here, so make room for the sentinel instead of
        # finishing inline: session.stop() encodes and writes, and the writer
        # thread may be inside add_audio right now — a single mutator is what
        # keeps the timeline cursor and the encoder consistent.
        logger.warning("Recording queue full on stop — dropping the oldest "
                       "block to finish")
        try:
            _rec_queue.get_nowait()
            _rec_session.dropped += 1
        except asyncio.QueueEmpty:                     # pragma: no cover
            pass
        try:
            _rec_queue.put_nowait(("stop", b"", 0, fut))
        except asyncio.QueueFull:                      # pragma: no cover
            logger.warning("Recording stop could not be queued — the "
                           "session stays open")
    return fut


def _rec_tap_rx(pcm: bytes, device_rate: int) -> None:
    """RX tap: device-domain PCM straight from the sound card."""
    if not pcm or not _recording_should_capture_rx():
        return
    _rec_enqueue("rx", pcm, device_rate)


def _rec_tap_tx(pcm: bytes) -> None:
    """TX tap: decoded browser-mic PCM (Opus codec rate)."""
    if not pcm:
        return
    _rec_enqueue("tx", pcm, TX_RATE)


async def _recording_writer_loop(queue: "asyncio.Queue",
                                 session: RecordingSession) -> None:
    """Single writer: serialises every session mutation off the event loop."""
    global _rec_failures
    while True:
        # Audio items: ("rx"|"tx", pcm, rate, timestamp).  The stop item
        # carries its completion future in the fourth slot
        # ("stop", b"", 0, future | None).
        source, pcm, rate, extra = await queue.get()
        if source == "stop":
            info = None
            try:
                loop = asyncio.get_running_loop()
                info = await loop.run_in_executor(_rec_pool, session.stop)
            except Exception as e:
                logger.warning("Recording stop failed: %s", e)
            if info is not None:
                logger.info("Recording finished: %s (%.1fs)",
                            info.name, info.duration)
            if extra is not None and not extra.done():
                extra.set_result(info)
            return
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(_rec_pool, session.add_audio,
                                       source, pcm, rate, extra)
        except Exception as e:
            _rec_failures += 1
            if _rec_failures in (1, 10, 100):
                logger.warning("Recording block failed (%d so far): %s",
                               _rec_failures, e)

# ── Auth Helpers ────────────────────────────────────────────────────

# Rate limiting for login attempts
_login_attempts: dict[str, list[float]] = {}
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300  # 5 minutes

def _check_login_rate_limit(ip: str) -> bool:
    """Check if IP has exceeded login attempt rate limit."""
    import time
    now = time.time()
    
    if ip not in _login_attempts:
        _login_attempts[ip] = []
    
    # Clean old attempts outside the window
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _LOGIN_WINDOW_SECONDS]
    
    if len(_login_attempts[ip]) >= _LOGIN_MAX_ATTEMPTS:
        return False  # Rate limited
    
    _login_attempts[ip].append(now)
    return True

def _make_auth_token() -> str:
    """Generate a new random session token."""
    return _secrets.token_hex(AUTH_TOKEN_BYTES)

def _load_auth_tokens() -> None:
    """Restore persisted session tokens (best effort, never raises).

    Called at import time so a restart keeps every 30-day login alive.  Tokens
    older than the cookie lifetime and malformed entries are dropped; an
    unreadable store simply starts empty rather than taking the server down.
    """

    try:
        payload = json.loads(_AUTH_TOKENS_FILE.read_text(encoding="utf-8"))
        entries = payload.get("tokens", [])
    except FileNotFoundError:
        return
    except Exception as exc:                                          # noqa: BLE001
        logger.warning("session store unreadable (%s); starting empty", exc)
        return
    now = time.time()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        token = entry.get("token")
        issued = entry.get("issued_epoch")
        issued = float(issued) if isinstance(issued, (int, float)) else now
        if not isinstance(token, str) or not token:
            continue
        if now - issued > _AUTH_TOKENS_MAX_AGE_S:
            continue
        if entry.get("role") == "sdr":
            _sdr_tokens.add(token)
            _token_issued[token] = issued
            continue
        _auth_tokens.add(token)
        _token_issued[token] = issued
        if entry.get("role") == "listener":
            _listen_tokens.add(token)


def _save_auth_tokens() -> None:
    """Persist the live tokens atomically, owner-readable only.

    Failure is logged and ignored: losing the store costs a re-login after the
    next restart, which must never break the login that is in progress.
    """

    payload = {
        "tokens": [
            {
                "token": token,
                "role": ("sdr" if token in _sdr_tokens
                         else "listener" if token in _listen_tokens
                         else "operator"),
                "issued_epoch": _token_issued.get(token, time.time()),
            }
            for token in sorted(_auth_tokens | _sdr_tokens)
        ]
    }
    try:
        tmp = _AUTH_TOKENS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(_AUTH_TOKENS_FILE)
    except OSError as exc:
        logger.warning("could not persist session tokens: %s", exc)


def _add_auth_token(token: str, *, listen: bool) -> None:
    """Record a freshly issued session token and persist the store."""

    _auth_tokens.add(token)
    _token_issued[token] = time.time()
    if listen:
        _listen_tokens.add(token)
    _save_auth_tokens()


def _drop_auth_token(token: str) -> None:
    """Forget a session token (logout) and persist the store."""

    _auth_tokens.discard(token)
    _listen_tokens.discard(token)
    _sdr_tokens.discard(token)
    _token_issued.pop(token, None)
    _save_auth_tokens()


# Restore persisted sessions: the process may be a restart (an upgrade, a
# crash) and browsers still hold a valid 30-day cookie.
_load_auth_tokens()


def _password_matches(candidate: str) -> bool:
    """Constant-time password comparison (SDD I9).

    A plain == leaks candidate length/prefix timing byte-by-byte;
    hmac.compare_digest runs in time independent of where (or whether)
    the strings first differ. Encoding both sides also keeps non-ASCII
    input from raising.
    """
    return hmac.compare_digest(
        str(candidate).encode("utf-8"), str(WEB_PASSWORD).encode("utf-8")
    )

def _listen_password_matches(candidate: str) -> bool:
    """Constant-time comparison against the optional listen-only password.

    An empty LISTEN_PASSWORD (the default) never matches, so the listen role
    cannot be entered unless the operator configured it.
    """
    if not LISTEN_PASSWORD:
        return False
    return hmac.compare_digest(
        str(candidate).encode("utf-8"), str(LISTEN_PASSWORD).encode("utf-8")
    )

def _is_listen_request(request: Request) -> bool:
    """True when the request is authenticated with a listen-only token."""
    return _token_from_request(request) in _listen_tokens

def _ensure_strong_password() -> None:
    """Replace the well-known default password with a generated one, once, and remember it.

    The launcher generates a password on first run; starting MRRC-Modern-Server.exe by itself (what
    a user does when the launcher will not start) skipped that and served the login page with the
    password written in the source. Generate one and save it, so the next start uses the same value.
    """
    global WEB_PASSWORD
    if not hmac.compare_digest(str(WEB_PASSWORD).encode(), str(DEFAULT_WEB_PASSWORD).encode()):
        return
    generated = _secrets.token_urlsafe(12)
    WEB_PASSWORD = generated
    os.environ["MRRC_WEB_PASSWORD"] = generated
    try:
        cloud_hub._write_config(_config_file_path(), {"MRRC_WEB_PASSWORD": generated})
        logger.info("generated a login password and saved it in %s", _config_file_path())
    except Exception as exc:                                          # noqa: BLE001
        logger.warning("could not save the generated password (%s)", exc)
    logger.warning("login password for this run: %s   (change it in the settings dialog)", generated)


def _warn_if_default_password() -> bool:
    """Loud startup warning when the well-known default password is in use.

    The default is public (it ships in config.py), so any LAN peer could
    log in with it. Returns True when the warning fired so callers/tests
    can observe the condition without parsing logs.
    """
    if hmac.compare_digest(
        str(WEB_PASSWORD).encode("utf-8"),
        str(DEFAULT_WEB_PASSWORD).encode("utf-8"),
    ):
        logger.warning(
            "SECURITY: web login is using the well-known default password! "
            "Set MRRC_WEB_PASSWORD (or --password) to a strong unique value."
        )
        return True
    return False

def _bearer_token(header_value: Optional[str]) -> str:
    """Token from an `Authorization: Bearer <token>` header, or ""."""
    parts = str(header_value or "").split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


_query_token_warned = False


def _warn_query_token_once() -> None:
    """Deprecation notice for a session token arriving in the URL.

    The query-param path writes a 30-day credential into every access log, the
    browser history and any Referer — which is why the web client stopped using
    it (hub AD-H07 / NFR-H020). It is still honoured so already-installed native
    clients keep working, and logged once per process so an operator learns they
    need updating instead of getting one line per reconnect.
    """
    global _query_token_warned
    if _query_token_warned:
        return
    _query_token_warned = True
    logger.warning(
        "SECURITY: session token arrived in the URL query string. That path is "
        "deprecated — it leaks the credential into access logs, browser history "
        "and Referer. The web UI now authenticates with the %s cookie; native "
        "clients should send 'Authorization: Bearer <token>'.", AUTH_COOKIE)


def _token_from_ws_handshake(ws: WebSocket, cookie_name: str = AUTH_COOKIE) -> str:
    """Session token for a WebSocket upgrade: header, cookie, then query param.

    The browser sends the auth cookie automatically on the same-origin
    handshake, so the URL needs no credential at all; native clients can set a
    header (they already set Origin/User-Agent). The query param stays as the
    last resort for clients that predate this change, and warns once.
    """
    token = _bearer_token(ws.headers.get("authorization"))
    if token:
        return token
    token = ws.cookies.get(cookie_name) or ""
    if token:
        return token
    token = ws.query_params.get("token", "") or ""
    if token:
        _warn_query_token_once()
    return token


def _token_from_request(request: Request, cookie_name: str = AUTH_COOKIE) -> str:
    """Same resolution order as _token_from_ws_handshake, for HTTP requests."""
    token = _bearer_token(request.headers.get("authorization"))
    if token:
        return token
    token = request.cookies.get(cookie_name) or ""
    if token:
        return token
    token = request.query_params.get("token", "") or ""
    if token:
        _warn_query_token_once()
    return token


def _verify_auth(request: Request) -> bool:
    """Whether the request carries a valid session token (see _token_from_request)."""
    return _token_from_request(request) in _auth_tokens

# ── Memory Channel Helpers ──────────────────────────────────────────

def _load_mem_channels() -> list:
    """Load memory channels from disk."""
    if MEM_FILE.exists():
        try:
            data = json.loads(MEM_FILE.read_text())
            channels = data.get("channels", [])
            # Pad to exactly MEM_CHANNEL_COUNT slots
            while len(channels) < MEM_CHANNEL_COUNT:
                channels.append(None)
            return channels[:MEM_CHANNEL_COUNT]
        except Exception:
            pass
    return [None] * MEM_CHANNEL_COUNT

def _save_mem_channels(channels: list):
    """Save memory channels to disk."""
    MEM_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEM_FILE.write_text(json.dumps({"channels": channels[:MEM_CHANNEL_COUNT]}, indent=2))

# ── Broadcast ───────────────────────────────────────────────────────

def _on_cat_broadcast(field: str, value):
    """Radio-originated state change (CI-V transceive broadcast).

    Called by the active backend on the asyncio loop when the radio
    itself reports a change (front-panel knob/mode).  Reuses the normal
    dirty-broadcast path; runs on the event loop so ensure_future is safe.
    """
    if field in ("vfo_a_freq", "vfo_b_freq"):
        # Same plausibility guard as the poll loop.
        if not isinstance(value, int) or not 30000 <= value <= 75000000:
            return
    changed = radio.update(**{field: value})
    if changed:
        asyncio.ensure_future(_broadcast_state())


async def _broadcast_state():
    """Send dirty state fields to all connected WebSocket clients."""
    global ctrl_clients, _last_meter_broadcast_log
    dirty = radio.get_and_clear_dirty()
    if not dirty:
        return
    if "serial_connected" in dirty:
        # Diagnostic (2026-08-15: frontend showed serial=false while the
        # radio was healthy).  Demoted to DEBUG for V2.42: it fired 84 times
        # per USB outage (the reconnect sweep used to mark every field
        # dirty), burying the actual cause.
        logger.debug(
            "state broadcast contains serial_connected=%s (dirty=%s)",
            radio.serial_connected, sorted(dirty),
        )
    if "tx_status" in dirty:
        # Radio entered/left TX — pause/resume the scope pipe's SPI reads.
        if _scope_producer is not None:
            _scope_producer.notify_tx(bool(radio.is_transmitting))
        if not radio.is_transmitting and audio:
            # Full-duplex USB audio stacks can leave the long-lived RX
            # capture stream degraded after TX playback opens.  Reopen it on
            # every TX→RX transition; this runs off-loop below.
            global _rx_restart_task
            if _rx_restart_task is None or _rx_restart_task.done():
                _rx_restart_task = asyncio.create_task(
                    asyncio.to_thread(audio.restart_rx), name="rx_restart")
    fields = radio.to_dirty_dict(dirty)
    meter_dirty = {
        "s_meter", "power_meter", "alc_meter", "swr_meter", "id_meter", "vd_meter",
    } & dirty
    now = time.monotonic()
    if meter_dirty and now - _last_meter_broadcast_log >= METER_BROADCAST_LOG_INTERVAL_SECONDS:
        _last_meter_broadcast_log = now
        logger.debug(
            "Meter broadcast dirty=%s raw={S=%s PWR=%s ALC=%s SWR=%s ID=%s VD=%s} "
            "derived={dBm=%.1f unit=%s W=%.1f ALC%%=%.0f SWR=%.1f Id=%.1f Vd=%.1f}",
            sorted(meter_dirty),
            fields.get("s_meter"),
            fields.get("power_meter"),
            fields.get("alc_meter"),
            fields.get("swr_meter"),
            fields.get("id_meter"),
            fields.get("vd_meter"),
            fields.get("s_meter_dbm", radio.s_meter_dbm),
            fields.get("s_unit", radio.s_unit),
            fields.get("power_watts", radio.power_watts),
            fields.get("alc_pct", radio.alc_pct),
            fields.get("swr_ratio", radio.swr_ratio),
            fields.get("id_amps", radio.id_amps),
            fields.get("vd_volts", radio.vd_volts),
        )
    update = {
        "type": "stateUpdate",
        "fields": fields,
        "dirty": list(dirty),
        "work_mode": _work_mode_value(),
    }
    data = json.dumps(update)
    dead: set[WebSocket] = set()
    for ws in ctrl_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    ctrl_clients -= dead

    # Optional ATR1000 tuner linkage — short-circuits to nothing when the
    # feature is disabled (atr is None).  Covers both poll-driven and
    # command-driven changes since both flow through radio.update().
    # NOTE: `dirty` holds SOURCE fields only (derived fields are expanded
    # later in to_dirty_dict), so check vfo_*/active_vfo and tx_status.
    if atr is not None:
        try:
            if {"vfo_a_freq", "vfo_b_freq", "active_vfo"} & dirty:
                atr.notify_freq(radio.active_freq)
            if "tx_status" in dirty:
                atr.notify_tx(bool(radio.is_transmitting))
        except Exception as e:
            logger.debug("ATR1000 linkage hook failed: %s", e)

async def _max_tx_watchdog():
    """Force RX when continuous transmit exceeds MRRC_PTT_MAX_TX_SECONDS.

    Opt-in (0 = disabled, the default). Runs at 1 s resolution; the first
    tick of a TX keys the timestamp, so effective timeout is limit..limit+1s.
    Mirrors the PTT-release path: fire-and-forget unkey via set_ptt(False)
    plus a UI error toast — no blocking verify loop (SDD ch15).
    """
    global _tx_continuous_since, _ptt_key_ws
    while True:
        await asyncio.sleep(MAX_TX_WATCHDOG_INTERVAL)
        if PTT_MAX_TX_SECONDS <= 0:
            _tx_continuous_since = None
            continue
        if not (cat is not None and cat.connected and radio.is_transmitting):
            _tx_continuous_since = None
            continue
        now = time.monotonic()
        if _tx_continuous_since is None:
            _tx_continuous_since = now
            continue
        if now - _tx_continuous_since < PTT_MAX_TX_SECONDS:
            continue
        elapsed = now - _tx_continuous_since
        _tx_continuous_since = None
        logger.warning(
            "PTT safety: %.0fs continuous TX exceeds MRRC_PTT_MAX_TX_SECONDS"
            " (%.0fs) — forcing RX", elapsed, PTT_MAX_TX_SECONDS)
        try:
            await cat.set_ptt(False)
            _ptt_key_ws = None
            radio.update(tx_status=0, power_meter=0, alc_meter=0,
                         swr_meter=0, comp_meter=0, id_meter=0)
        except Exception as e:
            logger.error("PTT safety force-RX failed: %s", e)
        scheduler and scheduler.skip_next_poll("tx_status", 1.0)
        msg = json.dumps({"type": "error", "message": (
            f"Max TX time {PTT_MAX_TX_SECONDS:.0f}s reached — PTT released.")})
        dead: set[WebSocket] = set()
        for ws in ctrl_clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        ctrl_clients.difference_update(dead)

def _recording_state_payload() -> dict:
    """Recording snapshot for the ``recordingState`` message / fullState."""
    return _rec_session.status()


async def _broadcast_recording_state() -> None:
    """Push the recording session snapshot to every control client."""
    global ctrl_clients
    if not ctrl_clients:
        return
    payload = json.dumps({"type": "recordingState",
                          "recording": _recording_state_payload()})
    dead = set()
    for ws in list(ctrl_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    ctrl_clients -= dead


# ── CQ call (spec 2026-09-13 §3/§6) ───────────────────────────────

def _ws_client_id(ws: WebSocket) -> str:
    """Short opaque id for a control client (used to name the CQ initiator)."""
    return f"{id(ws) & 0xFFFFFF:06x}"


def _cq_state_payload() -> dict:
    """CQ snapshot for the ``cqState`` message, fullState and ``GET /api/cq``.

    One shape everywhere: the recording screen, the main UI's CQ button and the
    push share these fields, so a page never has to guess which one is current.
    """
    payload = dict(_cq_player.status())
    has_custom = CQ_CUSTOM_PATH.is_file()
    payload["source"] = "custom" if has_custom else ("default" if _cq_player.ready else "none")
    payload["has_custom"] = has_custom
    payload["max_seconds"] = CQ_MAX_SECONDS
    return payload


async def _broadcast_cq_state() -> None:
    """Push the CQ snapshot to every control client (recordingState pattern)."""
    global ctrl_clients
    if not ctrl_clients:
        return
    payload = json.dumps({"type": "cqState", "cq": _cq_state_payload()})
    dead = set()
    for ws in list(ctrl_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    ctrl_clients -= dead


def _reload_cq_asset() -> bool:
    """Re-point the player at whatever is effective now and re-read it.

    Upload and reset both call this: a replaced CQ has to be live without a
    restart, and a reload that fails must leave the player refusing calls with
    its own reason rather than holding half an asset.
    """
    _cq_player.set_asset(_cq_effective_path())
    ok = _cq_player.load()
    if not ok:
        logger.warning("CQ asset reload failed: %s", _cq_player.unavailable_reason)
    return ok


def _cq_write_refusal() -> Optional[str]:
    """Why a CQ asset may not be replaced right now, or None.

    Two refusals, both measured consequences rather than caution: replacing the
    asset mid-call would swap the frames under a running transmission, and
    recording while the radio is keyed (including FT8) would put the receiver's
    own audio into the take.
    """
    if _cq_player.is_calling:
        return "CQ 正在播放，等它播完再替换素材"
    if radio is not None and radio.is_transmitting:
        return "正在发射（含 FT8），不能替换 CQ 素材"
    return None


def _work_mode_value() -> str:
    """The global FT8/SSB mode as a string (SSB when the engine is unbound)."""
    return "ft8" if _ft8_mode is not None and str(_ft8_mode.mode) == "ft8" else "ssb"


_ft8_bus = None                 # Ft8EventBus, created in the lifespan


def _platform_dial_hz() -> Optional[int]:
    """The radio's real dial frequency from the platform's polled state."""

    if radio is None:
        return None
    return radio.vfo_b_freq if radio.active_vfo == "B" else radio.vfo_a_freq


_ft8_pub_logged = False


def _ft8_transmitting() -> bool:
    """True only while the FT8 engine has the transmitter keyed.

    With the engine always-on there is no "mode switch" to arbitrate any
    more: the station stays freely controllable, and the FT8 engine is just
    another receiver.  The one real conflict left is electrical — two
    sources keying the same radio — so that is what this guards.
    """

    runtime = _ft8_runtime
    safety = getattr(runtime, "safety", None) if runtime is not None else None
    return bool(safety is not None and safety.ptt_on)


def _ft8_queue_frame(frame) -> None:
    """Hand one frame (JSON dict or WF01 bytes) to every /WSft8 subscriber."""

    global _ft8_pub_logged
    if _ft8_bus is None:
        if not _ft8_pub_logged:
            _ft8_pub_logged = True
            logger.warning("FT8 frame dropped: event bus not bound yet")
        return
    _ft8_bus.publish(frame)
    if not isinstance(frame, (bytes, bytearray, memoryview)):
        # Temporary field trace: every non-waterfall frame and its fan-out
        # width, so a broken decode path is visible in the log.
        logger.info("FT8 bus publish %s -> %d subscriber(s)",
                    frame.get("type", "?"), _ft8_bus.subscriber_count)


async def _broadcast_ft8_json(frame: dict) -> None:
    _ft8_queue_frame(frame)


class TxSession:
    """The platform's one TX keying sequence — CQ player and FT8 share it.

    ``key()`` is the PTT-on sequence including ownership; it returns False
    (with the carrier released again) when the backend gate refuses or the
    TX audio device cannot open, so a caller never reports a silent dead
    carrier as a success.  ``unkey()`` stops the audio stream first, then
    releases PTT.  FT8's ``PlatformRig.set_ptt`` routes through here too, so
    both TX sources cannot key the radio at once by construction.
    """

    def __init__(self) -> None:
        self._keyed = False

    @property
    def keyed(self) -> bool:
        return self._keyed

    async def key(self, owner_token: Optional[str] = None, *, label: str = "TX") -> bool:
        if backend is not None and backend.capabilities.tx_gated:
            return False
        _claim_tx_owner_for_token(owner_token)
        try:
            await cat.set_ptt(True)
            radio.update(tx_status=1)
            if audio and not await asyncio.to_thread(audio.start_tx):
                logger.error("%s: TX audio stream failed — unkeying radio", label)
                await cat.set_ptt(False)
                radio.update(tx_status=0)
                return False
        except Exception as e:
            logger.error("%s: keying failed (%s) — releasing", label, e)
            try:
                await cat.set_ptt(False)
            except Exception:
                pass
            radio.update(tx_status=0)
            return False
        self._keyed = True
        logger.info("%s: keyed the radio", label)
        return True

    async def unkey(self, graceful: bool = False, *, label: str = "TX") -> None:
        self._keyed = False
        try:
            if audio:
                await asyncio.to_thread(audio.stop_tx, graceful)
        finally:
            # The PTT release must survive cancellation: the drain above is an
            # await, and a disconnect (or a second CQ press landing in the final
            # 50 ms grain) cancels us right there.  A keyed radio with nobody
            # left to send TX0 is the one failure this class exists to prevent,
            # so the release runs as its own shielded task and finishes even
            # while the caller is unwinding.
            await asyncio.shield(self._release_ptt(label))

    async def _release_ptt(self, label: str) -> None:
        if cat is not None and cat.connected:
            try:
                await cat.set_ptt(False)
            except Exception as e:
                logger.warning("%s: unkey failed: %s", label, e)
        radio.update(tx_status=0, power_meter=0, alc_meter=0, swr_meter=0,
                     comp_meter=0, id_meter=0)
        scheduler and scheduler.skip_next_poll("tx_status", 1.0)


_tx_session = TxSession()
"""Shared by the CQ player, FT8 and (through them) nothing else."""


async def _cq_key(owner_token: Optional[str]) -> bool:
    """CQ wrapper over the shared TX session (keeps CQ's mic-drop accounting)."""
    global _tx_cq_mic_drops
    _tx_cq_mic_drops = 0
    return await _tx_session.key(owner_token, label="CQ")


async def _cq_unkey(graceful: bool) -> None:
    """CQ wrapper over the shared TX session (keeps CQ's session log)."""
    await _tx_session.unkey(graceful, label="CQ")
    if audio:
        st = audio.tx_stats()
        logger.info("CQ session: written=%d write_err=%d queue_drops=%d "
                    "mic_drops=%d", st["written"], st["write_err"],
                    st["queue_drops"], _tx_cq_mic_drops)


async def _feed_tx_from_uplink(pcm: bytes) -> None:
    """Route decoded browser-mic audio to the DAC, unless a CQ call owns TX.

    During a CQ call the server is the audio source; mixing the operator's
    microphone in would transmit two voices at once (spec §6).

    The box-level mic gain (AD-032) is applied here, and the TX recording tap
    sits right after it: a recording is then what the radio was actually
    given — gain included, clipping included — and all three uplink formats
    (Opus, raw PCM, the legacy untagged frame) share one path instead of the
    Opus branch alone being recorded.
    """
    global _tx_cq_mic_drops
    if _ft8_transmitting():
        return                                   # FT8 engine is keyed right now
    if _cq_player is not None and _cq_player.is_calling:
        _tx_cq_mic_drops += 1
        return
    if not audio:
        return
    pcm, clipped = audio_levels.apply_gain(pcm, _audio_levels.tx_mic_db)
    _audio_levels_clip.note("tx", len(pcm) // 2, clipped,
                            gain_db=_audio_levels.tx_mic_db,
                            now=time.monotonic())
    _rec_tap_tx(pcm)
    audio.feed_tx_audio(pcm)


async def _cq_abort_if_client_gone(ws: WebSocket) -> None:
    """Abort a CQ call whose initiating client just disconnected."""
    if _cq_player is None or not _cq_player.is_calling:
        return
    if _cq_player.status().get("started_by") == _ws_client_id(ws):
        logger.warning("CQ initiator disconnected — aborting the call")
        await _cq_player.abort("client_gone")


def _bind_cq_player() -> None:
    """Attach the live audio handler and callbacks (called from the lifespan)."""
    global _cq_player
    _cq_player = CQPlayer(
        asset_path=_cq_effective_path(),
        audio=audio,
        key=_cq_key,
        unkey=_cq_unkey,
        is_transmitting=lambda: bool(radio.is_transmitting),
        on_change=lambda snap: _broadcast_cq_state(),
    )
    _cq_player.load()


async def _broadcast_mem_channels():
    """Send memory channels to all connected clients."""
    global ctrl_clients
    channels = _load_mem_channels()
    msg = json.dumps({"type": "memChannels", "channels": channels})
    dead: set[WebSocket] = set()
    for ws in ctrl_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    ctrl_clients -= dead


def _audio_levels_state() -> dict:
    """The box-level gains as every surface renders them (AD-032).

    One shape everywhere — REST, fullState and the WebSocket push — so the
    menu slider, the settings card and a second browser all parse the same
    keys and no second parser can drift from the first.
    """
    payload = _audio_levels.to_wire()
    payload["limits"] = audio_levels.AudioLevels.limit_table()
    payload["stats"] = _audio_levels_clip.snapshot()
    payload["source"] = _audio_levels_store.source
    return payload


async def _broadcast_audio_levels():
    """Push the box-level audio gains to every control client (AD-032).

    One box, one value: a second device's slider follows this push, so two
    operators cannot believe they are listening to two different levels.
    """
    global ctrl_clients
    msg = json.dumps({"type": "audioLevels", **_audio_levels_state()})
    dead: set[WebSocket] = set()
    for ws in ctrl_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    ctrl_clients -= dead


async def _broadcast_client_count():
    """Push the online-user count to every control client (listen page
    shows it in the footer; unknown types are ignored by the other UIs)."""
    global ctrl_clients
    msg = json.dumps({"type": "onlineUsers", "count": len(ctrl_clients)})
    dead: set[WebSocket] = set()
    for ws in ctrl_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    ctrl_clients -= dead

# ── ATR1000 External Tuner (optional) ─────────────────────────────
# Enabled only when MRRC_ATR1000_HOST is set.  When disabled, `atr`
# stays None: no client task, no linkage hooks, and /WSatr1000 closes
# immediately — zero impact on installations without the hardware.

# Tune-assist timings (module constants so tests can shrink them).
ATR_TUNE_CARRIER_SETTLE_S = 0.3   # TX2 carrier stabilisation before reading SWR
ATR_TUNE_MIN_S = 5.0              # ignore "not tuning" before this (device lag)
ATR_TUNE_DEADLINE_S = 45.0        # hard timeout for a full tune
ATR_TUNE_COMPARE_SETTLE_S = 0.8   # post-tune settle before comparing SWR
ATR_TUNE_METER_WAIT_S = 2.5       # wait for fresh meter data after carrier on
ATR_TUNE_SWR_SKIP = 1.6           # at or below this, don't tune at all
ATR_TUNE_SWR_IMPROVED = 0.02      # minimum improvement to keep the result

async def _broadcast_atr(msg: dict):
    """Send an ATR1000 message to all tuner-channel clients."""
    if not atr_clients:
        return
    data = json.dumps(msg)
    dead: set[WebSocket] = set()
    for ws in atr_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    atr_clients.difference_update(dead)


def _on_atr_change(state: dict):
    """Sync callback from ATR1000Client (runs in the event loop)."""
    try:
        asyncio.get_running_loop().create_task(
            _broadcast_atr({"type": "atrState", **state}))
    except Exception:
        pass


def _on_atr_tune_event(event: dict):
    """Sync callback from ATR1000Client: forward an auto-tune phase to the
    tuner channel. Same message type as the manual tune assist, plus
    auto=true so the UI can label it."""
    try:
        asyncio.get_running_loop().create_task(_broadcast_atr(
            {"type": "atrTuneResult", **event, "auto": True}))
    except Exception:
        pass


async def _atr_wait_swr(timeout: float) -> Optional[float]:
    """Wait for a fresh SWR reading from the tuner (device pushes
    METER frames at a high rate while a carrier is present)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = atr.read_state()
        last = st.get("last_update") or 0
        if st.get("connected") and (st.get("swr") or 0) > 0 \
                and time.time() - last < timeout:
            return st["swr"]
        await asyncio.sleep(0.2)
    return None


async def _atr_wait_tune_complete(deadline_s: float, minimum_s: float) -> bool:
    """Wait until the tuner clears its tuning flag (the client applies
    the device's stability heuristics).  Returns False on deadline."""
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= minimum_s and not atr.read_state().get("tuning"):
            return True
        if elapsed >= deadline_s:
            return False
        await asyncio.sleep(0.25)


async def _atr_tune_assist():
    """Server-side ATR1000 tune assist (ported from the mrrc project's
    browser-side autoFullTuneIfHighSWR, moved server-side so a busy or
    disconnected browser cannot stall it):
    TX2 carrier → skip if SWR ≤ 1.6 → snapshot relays → full tune →
    keep + learn when SWR improved ≥0.02, otherwise roll the relays
    back.  The carrier is always dropped in `finally`."""
    phase = "error"
    message = ""
    swr_before: Optional[float] = None
    swr_after: Optional[float] = None
    try:
        await _broadcast_atr({"type": "atrTuneResult", "phase": "start"})
        scheduler and scheduler.note_user_command()
        # Key the low-power tune carrier (TX2), same as the radio's own
        # tune flow — but the radio's internal tuner is NOT triggered.
        await cat.set_tune(True)
        radio.update(tx_status=2)
        await asyncio.sleep(ATR_TUNE_CARRIER_SETTLE_S)

        swr_before = await _atr_wait_swr(timeout=2.5)
        if swr_before is None:
            message = "no meter data from tuner"
        elif swr_before <= ATR_TUNE_SWR_SKIP:
            phase = "skipped"
        else:
            snap = atr.read_state()
            orig = (snap.get("sw", 0), snap.get("ind", 0), snap.get("cap", 0))
            await atr.start_tune(mode=2)  # 2 = full tune
            if not await _atr_wait_tune_complete(
                    deadline_s=ATR_TUNE_DEADLINE_S, minimum_s=ATR_TUNE_MIN_S):
                message = "tune timeout (45s)"
            else:
                await asyncio.sleep(ATR_TUNE_COMPARE_SETTLE_S)  # settle
                final = atr.read_state()
                swr_after = final.get("swr") or 0
                if swr_after > 0 and swr_after < swr_before - 0.02:
                    phase = "success"
                    if _atr_storage is not None:
                        try:
                            _atr_storage.learn(
                                radio.active_freq,
                                final.get("sw", 0), final.get("ind", 0),
                                final.get("cap", 0), swr_after,
                                force_update=True,
                            )
                        except Exception as e:
                            logger.warning("ATR1000 learn failed: %s", e)
                else:
                    phase = "rollback"
                    await atr.set_relay(*orig)
        logger.info("ATR1000 tune assist: %s (SWR %s → %s) %s",
                    phase, swr_before, swr_after, message)
    except Exception as e:
        logger.warning("ATR1000 tune assist failed: %s", e)
        message = str(e)
    finally:
        # Always drop the carrier.  The last-client-disconnect dead-man
        # switch also forces RX independently if every client vanishes.
        try:
            await cat.set_tune(False)
            radio.update(tx_status=0)
        except Exception:
            pass
        scheduler and scheduler.skip_next_poll("tx_status", 1.0)
        await _broadcast_atr({
            "type": "atrTuneResult", "phase": phase, "message": message,
            "swr_before": round(swr_before, 2) if swr_before else None,
            "swr_after": round(swr_after, 2) if swr_after else None,
        })


async def _start_atr_tune_assist(ws: WebSocket):
    """Validate preconditions and launch the tune-assist task."""
    global _atr_tune_task
    if atr is None:
        await ws.send_text(json.dumps({
            "type": "error", "message": "ATR1000 not available"}))
        return
    if cat is None or not cat.connected:
        await ws.send_text(json.dumps({
            "type": "error", "message": "Radio not connected"}))
        return
    if radio.is_transmitting:
        await ws.send_text(json.dumps({
            "type": "error", "message": "Radio is transmitting"}))
        return
    if atr.read_state().get("tuning"):
        # Polled tx_status lags up to ~500 ms; this closes the window in which
        # an auto tune started by the high-SWR guard is already running.
        await ws.send_text(json.dumps({
            "type": "error", "message": "Tuner is already tuning"}))
        return
    if _atr_tune_task and not _atr_tune_task.done():
        return  # already running — the button shows progress
    _atr_tune_task = asyncio.create_task(_atr_tune_assist(), name="atr_tune")


# ── Spectrum/Scope ───────────────────────────────────────────────────

SPECTRUM_BROADCAST_FPS = 30


class _SpectrumFrameSource(Protocol):
    _frame_count: int

    def get_spectrum_binary(self) -> bytes: ...


def _new_real_spectrum_frame(
    scope_handler: _SpectrumFrameSource,
    last_frame_count: int,
) -> tuple[bytes | None, int]:
    """Return the latest real scope frame only when its counter advanced."""
    frame_count = scope_handler._frame_count
    if frame_count == last_frame_count:
        return None, last_frame_count
    return scope_handler.get_spectrum_binary(), frame_count


async def _on_scope_frame(_scope: ScopeHandler):
    """Called when a new scope frame is parsed.  Merges key metadata
    into the global radio state and marks dirty for broadcast.
    Also feeds radio state back into scope for S-meter fallback."""
    if _scope.last_update > 0:
        changes = {}
        # S-meter from scope is 30 fps (vs 10 fps CAT) — safe to merge
        # because CAT corrects it every 100 ms. The sentinel -1 (ScopeHandler
        # init) means "this scope source carries no S-meter byte" and is
        # skipped: the IC-7300's CI-V scope segments have no S-meter field,
        # and with the old 0-initialized field every waveform forced
        # radio.s_meter back to 0 between CAT polls — the UI S-meter
        # flickered between 0 and the true reading (SDD V2.39).
        if _scope.s_meter >= 0:
            changes["s_meter"] = _scope.s_meter
        # vfo_a_freq / mode / preamp / attenuator are deliberately NOT
        # taken from scope frames.  The CAT poll path is the single source
        # of truth for these fields — scope data can lag behind user
        # commands and would fight the poll for control, causing the
        # displayed frequency to drift or "adjust itself".
        if _scope.scope_span >= 0:
            changes["scope_span"] = _scope.scope_span
        if _scope.scope_mode >= 0:
            changes["scope_mode"] = _scope.scope_mode
        if _scope.scope_start_freq >= 0:
            changes["scope_start_freq"] = _scope.scope_start_freq
        if changes:
            radio.update(**changes)
            await _broadcast_state()

async def _broadcast_spectrum_loop():
    """Periodically send spectrum data to all spectrum WebSocket clients.

    Ticks at SPECTRUM_BROADCAST_FPS (30 Hz); what each socket actually receives
    is its profile — shape x divider, see spectrum_profile (AD-025).  Frames are
    binary: a 1-byte version (0x01) + 850 bytes wf1, plus 850 bytes wf2 on the
    full shape (1701 B) and absent on the wf1 shape (851 B).

    When scope_pipe is not connected (no FT4222 data), falls back to
    S-meter-based synthetic spectrum from the CAT polling data.  Real-scope
    frames go out only when ScopeHandler._frame_count advances (measured
    ~11.1 fps on the FT-710); the fallback regenerates one on every tick, and
    both paths now pass through the same per-profile gate.

    Idle (0 clients): sleeps 500ms instead of one tick interval, cutting ~60% of
    idle wakeups.  Synthetic Gaussian generation is also skipped.
    """
    global scope, spectrum_clients
    interval = 1.0 / SPECTRUM_BROADCAST_FPS
    idle_interval = 0.500      # 500 ms deep-sleep when no listeners
    last_real_frame_count = -1
    _first = True
    _idle_skipped = 0
    _broadcast_tick = 0
    while True:
        try:
            if not spectrum_clients:
                _idle_skipped += 1
                await asyncio.sleep(idle_interval)
                continue

            if scope:
                if scope.connected:
                    binary, last_real_frame_count = _new_real_spectrum_frame(
                        scope, last_real_frame_count
                    )
                    if binary is None:
                        await asyncio.sleep(interval)
                        continue
                    mode = "real scope"
                else:
                    # No real scope data — use S-meter fallback.
                    scope.update_from_radio_state(radio)
                    binary = scope.get_spectrum_binary()
                    mode = "S-meter fallback"
                if _first:
                    logger.info("Spectrum broadcast active: %s, %d bytes/frame, %d clients",
                                mode, len(binary), len(spectrum_clients))
                    _first = False
                # One shared fan-out: the healthy path and the S-meter fallback
                # path now deliver identical bytes per second for a given
                # profile, and both meter what actually left (851 B on a wf1
                # tier, not the 1701 B built above).
                variants = spectrum_profile.build_variants(binary)
                _broadcast_tick += 1
                spectrum_clients -= await _spectrum_fanout(variants, _broadcast_tick)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("Spectrum broadcast error: %s", e)
        await asyncio.sleep(interval)


async def _spectrum_fanout(variants: dict[str, bytes], tick: int) -> set[WebSocket]:
    """Send this tick's frame to every spectrum client, honouring its profile.

    One shared per-tick fan-out is what keeps the healthy (real-scope) path and
    the S-meter fallback path on identical bytes-per-second: before profiles the
    fallback regenerated a frame on every tick with no gate at all, so the broken
    case cost more than the working one.

    Returns the sockets that failed to take the frame; the caller drops them.
    """
    dead: set[WebSocket] = set()
    for ws in spectrum_clients:
        profile = _profile_for(ws)
        # The same gate the listener throttle has always used — one source of
        # truth for "may this socket have a frame on this tick".
        if not _spectrum_frame_due(ws, tick):
            continue
        payload = spectrum_profile.variant_for(variants, profile)
        try:
            await ws.send_bytes(payload)
        except Exception:
            dead.add(ws)
            continue
        # Metering sits OUTSIDE the send's try on purpose: this except marks the
        # socket dead and the caller drops it, so a bug in the counters would
        # otherwise be laundered into "client went away" and silently stop its
        # spectrum.  Count what actually left the radio, not the frame we built:
        # a wf1-only tier puts 851 B on the wire, and the telemetry has to say so
        # or the kbps numbers lie about the tier's effect.
        metrics.add_bytes("spectrum", len(payload))
        metrics.add_spectrum_profile_frame(profile, len(payload))
    return dead


# ── Audio Broadcast ────────────────────────────────────────────────────

async def _send_audio_frames_to_clients(
    frames: list[bytes],
    clients: set[WebSocket],
    per_frame_timeout: float = AUDIO_RX_SEND_TIMEOUT,
) -> set[WebSocket]:
    """Send encoded RX audio frames to clients with timeout isolation.

    A single slow/backpressured client must not block the whole audio loop.
    Returns the subset of clients that errored/timed out and should be removed.
    """
    if not frames or not clients:
        return set()

    # Snapshot to keep gather/zip pairing stable even if caller mutates set later.
    clients_snapshot = list(clients)

    async def _send_one(ws: WebSocket):
        for frame in frames:
            await asyncio.wait_for(ws.send_bytes(frame), timeout=per_frame_timeout)
            metrics.add_bytes("audio_rx", len(frame))

    results = await asyncio.gather(
        *[_send_one(ws) for ws in clients_snapshot],
        return_exceptions=True,
    )
    dead: set[WebSocket] = set()
    for ws, res in zip(clients_snapshot, results):
        if isinstance(res, Exception):
            dead.add(ws)
    return dead

def _audio_permission_hint() -> str:
    """Trailing clause for the near-silent RX warning.

    A frozen macOS build is a normal TCC subject: without the microphone
    authorization CoreAudio still opens the input stream and fills it with
    zeros, so the server sees a healthy stream carrying nothing.  Say so —
    "check the radio" sent an operator looking at the radio while the fix was in
    System Settings (2026-09-18 field report: no RX audio, peak=0.0%).
    """
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        return ("; in a packaged macOS app also allow MRRC Modern under "
                "System Settings > Privacy & Security > Microphone")
    return ""


_local_speaker: Optional[LocalSpeaker] = None


def _rx_pcm_for_clients(pcm: bytes) -> bytes:
    """The RX block as *listeners* get it (AD-032).

    The box-level RX level lives here and nowhere else: the recording tap, the
    FT8 decoder and a local speaker are all handed the native block earlier in
    the loop, so a knob whose job is to make a phone louder can never clip a
    decode or a recording.  At 0 dB this returns the caller's own bytes object.
    """
    pcm_out, clipped = audio_levels.apply_gain(pcm, _audio_levels.rx_db)
    _audio_levels_clip.note("rx", len(pcm) // 2, clipped,
                            gain_db=_audio_levels.rx_db,
                            now=time.monotonic())
    return pcm_out


async def _audio_rx_loop():
    """Capture RX audio from the sound card and broadcast to clients.

    Reads int16 PCM chunks from the audio handler, encodes via Opus (or
    sends raw PCM), and broadcasts to all connected audio_rx_clients.

    Idle (0 clients): skips PCM reads entirely and sleeps 500ms instead
    of 20ms, cutting ~96% of wakeups.  On client arrival the next 20ms
    tick catches the small accumulated buffer with no perceptible delay.
    """
    global audio, audio_rx_clients, _rx_restart_task
    interval = 0.020       # 20 ms chunk interval (active)
    idle_interval = 0.500  # 500 ms deep-sleep when no listeners
    _first = True
    _loop_count = 0
    _pcm_count = 0
    _send_count = 0
    _idle_skipped = 0
    # Stall watchdog state (see RX_STALL_ALERT_S): a fresh timestamp rather than
    # time.monotonic() itself, so a loop that has never seen audio still gets one
    # alert after the window instead of firing on the first idle tick.
    _rx_last_pcm = time.monotonic()
    _rx_stall_restart_at = 0.0
    while True:
        try:
            _loop_count += 1
            if audio and audio._rx_running:
                # ── Idle: no clients, no recording, no FT8 → skip the read ──
                # PCM read + resample + Opus encode is the #1 CPU
                # consumer when idle.  Skip everything, sleep deep — but a
                # running recording still needs the audio, and so does the
                # FT8 decoder (its RX tap is fed right below), so both keep
                # the loop awake.
                _ft8_tap_live = _ft8_runtime is not None and _ft8_runtime.rx_tap is not None
                # The local speaker is a listener too (AD-028): an operator
                # standing next to the box with a headset opens no browser, and
                # the FT8 tap already taught this lesson once.
                _speaker_live = _local_speaker is not None and _local_speaker.active
                if (not audio_rx_clients and not _rec_session.active
                        and not _ft8_tap_live and not _speaker_live):
                    _idle_skipped += 1
                    if _loop_count % 500 == 0 and _idle_skipped > 50:
                        logger.info(
                            "Audio loop idle: %d skips so far, entering deep-sleep",
                            _idle_skipped,
                        )
                    await asyncio.sleep(idle_interval)
                    continue

                pcm = audio.read_rx_chunk()
                audio.note_rx_chunk(pcm)
                if pcm and _local_speaker is not None:
                    _local_speaker.feed(pcm)
                if pcm:
                    _rx_last_pcm = time.monotonic()
                    _rec_tap_rx(pcm, audio._rx_dev_rate)
                    # FT8 tap: same device-rate block, resampled to the
                    # decoder domain only while FT8 mode is running.
                    if _ft8_runtime is not None and _ft8_runtime.rx_tap is not None:
                        # read_rx_chunk() returns the 48 kHz codec domain (it
                        # resamples the device for Opus), so the tap's rate is
                        # RX_RATE — feeding it the device rate skews the slot
                        # timeline and nothing ever decodes (found in the field).
                        _ft8_runtime.rx_tap.feed(
                            pcm, time.time() - len(pcm) / 2 / RX_RATE
                        )
                    # While recording, keep every client's duration display
                    # live without a dedicated task (20 ms tick / 50 = 1 Hz).
                    if _rec_session.active and _loop_count % 50 == 0:
                        await _broadcast_recording_state()
                    _idle_skipped = 0
                    _pcm_count += 1
                    # While the radio is keyed (half-duplex), its USB RX
                    # output is muted silence: encoding + pushing those
                    # frames only piles silent Opus packets into every
                    # client's jitter buffer, which must then DRAIN after
                    # unkey before real RX audio is heard (~100+ ms of
                    # the post-PTT release gap).  Read / rec tap / silence
                    # watchdog above stay live; only encode + fan-out is
                    # skipped.
                    _radio_txing = (radio is not None
                                    and radio.is_transmitting)
                    # Opus is only for clients: recording, the FT8 tap and the
                    # local speaker all take the PCM as it is.  Encoding for a
                    # speaker-only box burned a few percent of a core every
                    # 20 ms for frames that were then thrown away.
                    _needs_frames = (bool(audio_rx_clients) or _rec_session.active
                                     or _ft8_tap_live)
                    frames = (None if (_radio_txing or not _needs_frames)
                              else audio.encode_rx_audio(_rx_pcm_for_clients(pcm)))

                    if frames:
                        # Send every encoded frame.  Dropping frames here
                        # leaves holes in the client's jitter buffer and in
                        # recordings; read_rx_chunk already caps each read at
                        # 4 chunks (~80 ms), so the burst is bounded and the
                        # client's time-based jitter buffer absorbs it.
                        _send_count += 1
                        if _first:
                            tag_name = "Opus" if audio.opus_enabled else "Int16 PCM"
                            # Check audio level (safely)
                            _slice = pcm[:200]
                            _sample_count = len(_slice) // 2
                            if _sample_count > 0:
                                import struct as _struct
                                _samples = _struct.unpack(f"<{_sample_count}h", _slice[:_sample_count*2])
                                _peak = max(abs(s) for s in _samples) / 32767 * 100
                                logger.info("RX audio broadcast active: %s, %d clients, peak=%.1f%%",
                                           tag_name, len(audio_rx_clients), _peak)
                                if _peak < 0.5:
                                    logger.warning("RX audio is near-silent (peak=%.1f%%) — "
                                                 "check radio AF gain / USB audio connection%s",
                                                 _peak, _audio_permission_hint())
                            _first = False
                        dead = await _send_audio_frames_to_clients(frames, audio_rx_clients)
                        if dead:
                            logger.warning("Removed %d dead/slow audio clients", len(dead))
                            audio_rx_clients -= dead
                elif audio_rx_clients or _ft8_tap_live or _rec_session.active or _speaker_live:
                    # No PCM at all while somebody needs the audio: the capture
                    # stopped delivering. Reopen it (rate-limited) instead of
                    # leaving the consumer with silence until the next TX.
                    # Two consumers are not WebSockets: the FT8 decoder's RX tap
                    # (whose page never opens /WSaudioRX) and a server-side
                    # recording.  Gating this branch on ``audio_rx_clients``
                    # alone starved the decoder through a 113 s USB
                    # re-enumeration on 2026-10-17: the operator was on the FT8
                    # page, so nothing reopened the stream and every slot was
                    # skipped for the rest of the process.
                    _stalled_s = time.monotonic() - _rx_last_pcm
                    if _stalled_s >= RX_STALL_ALERT_S:
                        _rx_last_pcm = time.monotonic()
                        logger.warning(
                            "RX capture stalled: no PCM for %.0f s (audio clients=%d, "
                            "FT8 tap=%s, recording=%s) — reopening the capture stream "
                            "(check the radio's USB audio; the capture interface may "
                            "have stopped while PyAudio still reports it open)",
                            _stalled_s, len(audio_rx_clients), _ft8_tap_live,
                            _rec_session.active)
                        if (time.monotonic() >= _rx_stall_restart_at
                                and (_rx_restart_task is None or _rx_restart_task.done())):
                            _rx_stall_restart_at = time.monotonic() + RX_STALL_RESTART_MIN_GAP_S
                            _rx_restart_task = asyncio.create_task(
                                asyncio.to_thread(audio.restart_rx),
                                name="rx_restart_stall")
            # Periodic health log
            if _loop_count % 250 == 0 and _idle_skipped == 0:  # Every ~5s when active
                logger.debug("Audio loop: loops=%d pcm_chunks=%d sends=%d clients=%d running=%s peak=%d",
                           _loop_count, _pcm_count, _send_count,
                           len(audio_rx_clients), audio._rx_running if audio else False,
                           audio._rx_last_peak if audio else 0)
                # RX silence watchdog: bit-exact zeros while the squelch is
                # open mean the FT-710 USB audio is muted/wedged (a quiet band
                # is never bit-exact silent). Flag it so the UI can warn.
                if audio and radio and cat and cat.connected:
                    _silent_s = audio.rx_silent_seconds()
                    if (_silent_s >= RX_SILENCE_ALERT_S and radio.squelch_open
                            and not radio.rx_audio_silent):
                        radio.update(rx_audio_silent=True)
                        logger.warning(
                            "RX audio bit-exact silent for %.0fs with squelch open — "
                            "FT-710 USB audio may be wedged; power-cycle the radio "
                            "or re-plug its USB cable", _silent_s)
                        await _broadcast_state()
                    elif _silent_s < 1.0 and radio.rx_audio_silent:
                        radio.update(rx_audio_silent=False)
                        logger.info("RX audio recovered (non-zero samples)")
                        await _broadcast_state()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("Audio RX broadcast error: %s", e)
            import traceback; traceback.print_exc()
        await asyncio.sleep(interval)


async def _audio_tx_drain_loop():
    """Periodically drain queued TX audio to the sound card output.

    Offloads the synchronous PyAudio write to a thread so it never
    blocks the asyncio event loop.  Without this, a slow audio write
    can stall all other async work — including CAT commands and state
    broadcasts — causing UI stutter on both audio and CAT.

    Idle (no pending TX): sleeps 200ms instead of 50ms, cutting
    ~75% of idle wakeups.
    """
    global audio
    active_interval = 0.010  # 10 ms drain interval during TX playback
    idle_interval = 0.200    # 200 ms deep-idle when no TX audio pending
    while True:
        try:
            if audio and audio.has_pending_tx_audio():
                await asyncio.to_thread(audio.write_tx_chunk)
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(
            active_interval if audio and audio.has_pending_tx_audio() else idle_interval
        )


# ── Command Handler ─────────────────────────────────────────────────

# ── local control socket (AD-028) ──────────────────────────────────────────
# The physical remote belongs to the box, not to a browser session.  The daemon
# that reads it runs as the same user as this server and talks over a Unix
# socket whose mode is the authentication: no password, no token, no rate
# limiter.  A web login for a hardware button was the wrong shape — it tripped
# the five-logins-per-five-minutes guard within minutes.
LOCAL_CONTROL_FIELDS = frozenset({
    "freq", "vfo_a_freq", "vfo_b_freq", "mode", "ptt", "af_gain",
})


def _local_socket_path() -> str:
    return _env("MRRC_LOCALCTL_SOCKET", "/run/mrrc/localctl.sock")


class _LocalControlClient:
    """The little a ``set`` command needs from a client: a place to reply."""

    client = ("localctl", 0)

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


def _local_af_gain() -> int:
    """The radio's AF gain in its own 0-255 domain (clamped for the wire)."""
    return max(0, min(255, int(radio.af_gain or 0)))


def _local_control_state() -> str:
    """One line the daemon uses to start from where the radio actually is."""
    name = {num: label for label, num in _mode_name_to_num().items()}.get(radio.mode, "")
    # ``af_gain`` rides along in the radio's own 0-255 domain: without it the
    # daemon starts at its 50 % default and the first 音量 press steps *down*
    # from whatever the operator had set (2026-10-18).
    return json.dumps({"type": "state", "freq": radio.vfo_a_freq, "mode": name,
                       "af_gain": _local_af_gain()}) + "\n"


async def _local_control_handle(reader: asyncio.StreamReader,
                                writer: asyncio.StreamWriter) -> None:
    peer = _LocalControlClient()
    try:
        writer.write(_local_control_state().encode())
        await writer.drain()
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=3600)
            if not line:
                break
            try:
                msg = json.loads(line.decode())
            except (ValueError, UnicodeDecodeError):
                writer.write(b'{"ok":false,"error":"bad json"}\n')
                await writer.drain()
                continue
            field = str(msg.get("field") or "")
            if field not in LOCAL_CONTROL_FIELDS:
                writer.write(json.dumps({"ok": False, "error": f"field not allowed: {field}"}).encode() + b"\n")
                await writer.drain()
                continue
            peer.sent.clear()
            try:
                await _execute_set_command(field, msg.get("value"), peer)
            except Exception as exc:                    # noqa: BLE001 - report, never drop the link
                logger.warning("local control %s failed: %s", field, exc)
                writer.write(json.dumps({"ok": False, "error": str(exc)}).encode() + b"\n")
                await writer.drain()
                continue
            errors = [m for m in peer.sent if '"error"' in m]
            reply = {"ok": not errors}
            if errors:
                reply["error"] = errors[-1]
            # Every reply carries the gain: the operator can also turn the knob
            # on the radio itself, and a daemon stepping from a stale percentage
            # would jump instead of nudging.
            reply["af_gain"] = _local_af_gain()
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
        except Exception:                               # noqa: BLE001
            pass


async def _local_control_server() -> None:
    """Serve the Unix socket; a platform without one simply skips it."""
    path = _local_socket_path()
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if os.path.exists(path):
            os.unlink(path)
        server = await asyncio.start_unix_server(_local_control_handle, path=path)
        os.chmod(path, 0o600)
    except (AttributeError, NotImplementedError, OSError) as exc:
        logger.info("Local control socket not started (%s)", exc)
        return
    logger.info("Local control socket listening at %s", path)
    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass


def _mode_name_to_num() -> dict:
    """Mode-name → register-number map of the active backend.

    Mode numbers are protocol-native (Yaesu register values vs CI-V mode
    bytes), so the map must come from the backend.  Falls back to the
    FT-710 table when no backend is active (legacy/tests).
    """
    if backend is not None:
        m = backend.mode_name_to_num
        if m:
            return m
    return MODE_NAME_TO_NUM


# Operator-facing text for a refused key-up on a model whose profile has
# no hardware evidence (spec 2026-09-12 §6.1).  The refusal is deliberate,
# so it must name the switch that turns it off.
_TX_GATE_MESSAGE = (
    "This radio model is not hardware-verified — transmit is disabled. "
    "On the box, turn it on in 设置 → 实验机型的发射; elsewhere set "
    "MRRC_ALLOW_UNVERIFIED_TX=1 and restart."
)


async def _handle_ws_message(ws: WebSocket, msg_str: str):
    """Process incoming WebSocket control messages.

    Message format: JSON with 'type' and 'field'/'value'.
    """
    global _tx_session_frames, _tx_session_decoded
    global _tx_session_decode_fail, _tx_non_owner_drops
    try:
        msg = json.loads(msg_str)
    except json.JSONDecodeError:
        # Fall back to legacy text format: "field:value"
        if ":" in msg_str:
            field, _, val = msg_str.partition(":")
            msg = {"type": "set", "field": field, "value": val}
        else:
            logger.debug("Invalid message: %s", msg_str[:80])
            return

    msg_type = msg.get("type", "")
    field = msg.get("field", "")
    value = msg.get("value")

    # Listen-only role gate (MRRC_LISTEN_PASSWORD sessions): receive anything,
    # tune frequency/mode, recall memory channels (freq+mode only) — every
    # other mutation (PTT/TUNE/CQ/recording/settings/memory writes) is refused
    # here, server-side, regardless of what the client UI shows.
    if _ws_tokens.get(ws) in _listen_tokens:
        allowed = msg_type in ("ping", "get", "memLoadAll", "memRecall") or (
            msg_type == "set" and field in LISTEN_ALLOWED_SET_FIELDS
        )
        if not allowed:
            logger.info(
                "Listen-only client refused %s %s", msg_type or "message", field,
            )
            await ws.send_text(json.dumps({
                "type": "error", "message": LISTEN_ONLY_MESSAGE,
            }))
            return

    if msg_type == "ping":
        await ws.send_text(json.dumps({"type": "pong"}))

    elif msg_type == "txhb":
        # TX-phase liveness (ch15 §15.6): sending this *is* the capability
        # declaration, so clients that predate the mechanism are never gated.
        _tx_hb_capable.add(ws)
        _tx_hb_last[ws] = time.monotonic()

    elif msg_type == "get":
        if field == "fullState":
            await ws.send_text(json.dumps({
                "type": "fullState",
                "data": radio.to_dict(),
            }))
        else:
            full = radio.to_dict()
            if field in full:
                await ws.send_text(json.dumps({
                    "type": "value", "field": field, "value": full[field],
                }))

    elif msg_type == "memRecall":
        # Atomic memory channel recall: send frequency + mode in a single
        # message so the poll cycle cannot capture the intermediate state
        # between two separate CAT commands.  Order matters — see below.
        if cat is None or not cat.connected:
            await ws.send_text(json.dumps({
                "type": "error", "message": "Radio not connected",
            }))
            return

        freq = msg.get("freq")
        mode_name = msg.get("mode")
        vfo = str(msg.get("vfo") or radio.active_vfo or "A").upper()
        if vfo not in ("A", "B"):
            await ws.send_text(json.dumps({
                "type": "error", "message": f"Unknown VFO: {vfo}",
            }))
            return
        logger.info(
            "Memory recall: freq=%s mode=%s vfo=%s", freq, mode_name, vfo,
        )
        if freq is not None:
            scheduler and scheduler.note_user_command()
            # Skip polls for 2 s so they don't capture stale intermediate
            # state while the two CAT commands are in flight.
            scheduler and scheduler.skip_next_poll("if", 2.0)
            scheduler and scheduler.skip_next_poll("vfo", 2.0)
            # CRITICAL ORDER: frequency FIRST, then mode.
            # The FT-710 band stacking register auto-recalls the last-used
            # mode whenever the frequency changes bands.  If we sent mode
            # first, the subsequent frequency change could revert it.
            # Setting mode AFTER frequency overrides band-stack auto-recall.
            await cat.set_frequency(int(freq), vfo)
            if vfo.upper() == "B":
                radio.update(vfo_b_freq=int(freq))
            else:
                radio.update(vfo_a_freq=int(freq), active_vfo="A")
        if mode_name is not None:
            mode_num = _mode_name_to_num().get(str(mode_name).upper())
            if mode_num is not None:
                await cat.set_mode(mode_num)
                radio.update(mode=mode_num)
                scheduler and scheduler.skip_next_poll("if", 2.0)
                if freq is not None:
                    # SSB mode changes can shift the carrier/displayed
                    # frequency by the sideband offset (observed ±1400 Hz
                    # on FT-710).  Set the stored frequency again after MD0
                    # so the final polled frequency matches the memory slot.
                    await cat.set_frequency(int(freq), vfo)
                    if vfo == "B":
                        radio.update(vfo_b_freq=int(freq))
                    else:
                        radio.update(vfo_a_freq=int(freq))
                    scheduler and scheduler.skip_next_poll("if", 2.0)
                    scheduler and scheduler.skip_next_poll("vfo", 2.0)
            else:
                logger.warning("Memory recall: unknown mode %r", mode_name)

    elif msg_type == "set":
        await _execute_set_command(field, value, ws)

    elif msg_type == "memLoadAll":
        await _broadcast_mem_channels()

    elif msg_type == "memSave":
        channels = msg.get("channels", [])
        _save_mem_channels(channels)
        await _broadcast_mem_channels()

    elif msg_type == "memDelete":
        index = msg.get("index", -1)
        if 0 <= index < MEM_CHANNEL_COUNT:
            channels = _load_mem_channels()
            channels[index] = None
            _save_mem_channels(channels)
            await _broadcast_mem_channels()


# ── Radio power control (CAT PS) ───────────────────────────────────
# NOTE: PS0 (off) is reliable, but PS1 (on) is NOT — field testing
# (2026-07-27/28) showed the radio often ignores PS1 in standby, and a
# PS0 landing mid-boot can wedge the CAT MCU until a physical power
# cycle.  The header switch stayed withdrawn (SDD V2.13); what the menu
# offers instead (2026-10-16) keeps the two intents apart:
#   * 重开电台…  → off, then keep trying to wake it (bounded + cancellable)
#   * 关机…      → off, and stay off (never re-applies power)
# The "power" set command and these guards remain the single execution
# point for both the UI and the maintenance scripts (_power_cycle*.py).
POWER_BOOT_WINDOW_S = 15.0   # PS0 rejected for this long after a PS1
POWER_ON_ATTEMPTS = 3        # PS1 retries before declaring failure
POWER_ON_VERIFY_S = 12.0     # per-attempt wait for the radio to answer FA
                             # (a cold radio took ~25 s to answer in field
                             # testing — 3x12 s covers that with margin)
_power_boot_until: float = 0.0

# ── Wake-up rescue loop（「重开电台…」那一半） ──────────────────────
# One round is the already-tested _power_on_radio(): PS1 x<=3, FA read-back
# verification, boot window re-armed on every write.  This layer adds only
# the budget, the cancellation and the reporting — plus the one thing the
# 36-second single shot could not do: keep trying while the radio's USB
# re-enumerates (the maintenance script's own comment: "10 attempts in case
# port is re-enumerating after power-off").
POWER_RESCUE_INTERVAL_S = 30.0   # minimum gap between two rounds
POWER_RESCUE_MAX_S = 900.0       # hard stop after 15 minutes
_power_rescue_task: "asyncio.Task | None" = None
_power_rescue_state: str = ""    # "" | running | woke | timeout | cancelled
_power_rescue_attempts: int = 0
_power_rescue_started: float = 0.0


async def _power_on_radio(cat, backend=None) -> bool:
    """Send PS1 (with retries) and verify the radio actually boots.

    Returns True once the radio answers a boot check, False after all
    attempts.  Verification matters: PS1 is fire-and-forget and can be
    lost — without a check the UI would show "on" while the radio is off.

    The boot check is backend-specific: the FT-710 answers a Yaesu "FA"
    frequency query; an Icom radio answers a CI-V read (the Yaesu FA
    command is invalid there — 0xFA is the CI-V NG reply code).  The
    backend's ``boot_verify()`` supplies the right query.
    """
    global _power_boot_until
    for attempt in range(1, POWER_ON_ATTEMPTS + 1):
        await cat.set_power(True)
        # Refresh the boot window at EVERY PS1: a PS0 landing while the
        # radio might still be booting (even from a late-acting PS1 from
        # a previous attempt) can wedge the CAT MCU (2026-07-27 incident).
        _power_boot_until = time.monotonic() + POWER_BOOT_WINDOW_S
        deadline = time.monotonic() + POWER_ON_VERIFY_S
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            if backend is not None:
                verified = await backend.boot_verify(cat, 0.4)
            else:
                verified = bool(await cat.query("FA", timeout=0.4))
            if verified:
                logger.info("Power-on verified (PS1 attempt %d)", attempt)
                # Re-arm from the actual boot moment — a slow boot must
                # not eat the whole protection window.
                _power_boot_until = time.monotonic() + POWER_BOOT_WINDOW_S
                # And mute the next power_on poll: a radio that answers a
                # frequency read can still report PS0 for a moment (measured
                # 2026-10-16 on the W103D — a fresh PS; read flipped the
                # verified "on" straight back to off, and the UI showed a
                # radio that was demonstrably running as off).
                if scheduler is not None:
                    scheduler.skip_next_poll("power_on", 25.0)
                return True
        logger.warning("PS1 attempt %d: radio not answering", attempt)
    return False


async def _power_off(cat_obj, radio_obj, scheduler_obj=None) -> str:
    """Turn the radio off.  Returns "" on success, else the user-facing refusal.

    Three guards, each bought with an incident (SDD V2.13):
      * never while transmitting — including FT8, whose carrier is its own
        ``_ft8_transmitting()``; the original branch predates FT8 and only
        looked at ``radio.is_transmitting``, which is a real hole now;
      * never inside ``POWER_BOOT_WINDOW_S`` of a PS1 — a PS0 landing
        mid-boot wedged the FT-710's CAT MCU (2026-07-27) and needed a
        physical power cycle to clear;
      * send PS0 twice — the first write is occasionally lost.
    """
    if radio_obj is not None and radio_obj.is_transmitting:
        return "发射中不能关机"
    if _ft8_transmitting():
        return "FT8 正在发射，不能关机"
    if time.monotonic() < _power_boot_until:
        remain = int(_power_boot_until - time.monotonic()) + 1
        return f"电台正在启动，请 {remain} 秒后再关机"
    if scheduler_obj is not None:
        scheduler_obj.skip_next_poll("power_on", 10.0)
    await cat_obj.set_power(False)
    await asyncio.sleep(0.5)
    await cat_obj.set_power(False)
    if radio_obj is not None:
        radio_obj.update(power_on=False)
    return ""


def _power_rescue_running() -> bool:
    return _power_rescue_state == "running"


def _power_rescue_payload(state: str) -> dict:
    elapsed = 0.0 if not _power_rescue_started else time.monotonic() - _power_rescue_started
    return {
        "type": "powerRescue",
        "state": state,                 # running | woke | timeout | cancelled
        "attempt": _power_rescue_attempts,
        "elapsed": round(elapsed, 1),
        "maxSeconds": int(POWER_RESCUE_MAX_S),
    }


async def _broadcast_power_rescue(state: str) -> None:
    """Push the rescue snapshot to every control client (recordingState pattern)."""
    global ctrl_clients
    if not ctrl_clients:
        return
    payload = json.dumps(_power_rescue_payload(state))
    dead = set()
    for ws in list(ctrl_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    ctrl_clients.difference_update(dead)


def _power_rescue_stop_task() -> bool:
    """Cancel the running rescue task.  Returns True if one was running.

    Sends **nothing** to the radio: cancelling a wake-up attempt must never
    turn into a second PS1 (or a PS0) by itself.
    """
    global _power_rescue_state
    was_running = _power_rescue_state == "running"
    task = _power_rescue_task
    if task is not None and not task.done():
        task.cancel()
    if was_running:
        _power_rescue_state = "cancelled"
    return was_running


async def _power_rescue_cancel() -> bool:
    """Stop a rescue and tell the clients.  Used by 「取消」与「关机」 alike."""
    was_running = _power_rescue_stop_task()
    task = _power_rescue_task
    if task is not None and not task.done():
        try:
            await task
        except asyncio.CancelledError:
            pass
    if was_running:
        await _broadcast_power_rescue("cancelled")
    return was_running


def _power_rescue_start() -> "asyncio.Task":
    """Begin the wake-up loop.  The caller has already turned the radio off."""
    global _power_rescue_task
    _power_rescue_stop_task()
    _power_rescue_task = asyncio.create_task(_power_rescue(), name="power_rescue")
    return _power_rescue_task


async def _power_rescue() -> str:
    """Keep trying to wake the radio until it answers, the budget runs out, or it is cancelled.

    Every round re-reads the module-level ``cat`` / ``backend``: a USB
    re-enumeration (which is exactly what a power cycle causes) rebuilds the
    serial handle, and the round must use whatever is current.  While the
    port is absent we wait — the connection watchdog owns reopening it, and
    this loop must never touch the serial object itself.
    """
    global _power_rescue_state, _power_rescue_attempts, _power_rescue_started
    _power_rescue_state = "running"
    _power_rescue_attempts = 0
    _power_rescue_started = time.monotonic()
    await _broadcast_power_rescue("running")
    try:
        while True:
            if time.monotonic() - _power_rescue_started >= POWER_RESCUE_MAX_S:
                _power_rescue_state = "timeout"
                logger.warning(
                    "Power rescue gave up after %d round(s) / %.0f s — the radio is "
                    "still off; someone has to press its front panel",
                    _power_rescue_attempts, time.monotonic() - _power_rescue_started)
                await _broadcast_power_rescue("timeout")
                return "timeout"
            cat_now, backend_now = cat, backend
            if cat_now is None or not getattr(cat_now, "connected", False):
                # Radio's USB is gone (that is what a power-off looks like).
                await asyncio.sleep(2.0)
                continue
            _power_rescue_attempts += 1
            await _broadcast_power_rescue("running")
            if await _power_on_radio(cat_now, backend_now):
                _power_rescue_state = "woke"
                # The single-shot path updates the state itself; the rescue must
                # do it too, or a radio that is demonstrably running keeps being
                # reported as off until the next poll (measured on the W103D
                # 2026-10-16: "woke on round 1" in the log, power_on=False in
                # fullState).
                if radio is not None:
                    radio.update(power_on=True)
                logger.info("Power rescue woke the radio on round %d after %.0f s",
                            _power_rescue_attempts, time.monotonic() - _power_rescue_started)
                await _broadcast_power_rescue("woke")
                return "woke"
            await asyncio.sleep(POWER_RESCUE_INTERVAL_S)
    except asyncio.CancelledError:
        # Cancelled from _power_rescue_cancel / _power_rescue_stop_task; they
        # own the state and the broadcast, so just stop touching the radio.
        logger.info("Power rescue cancelled after %d round(s)", _power_rescue_attempts)
        raise
    finally:
        if _power_rescue_state == "running":
            _power_rescue_state = "cancelled"


async def _execute_set_command(field: str, value, ws: WebSocket):
    """Execute a set command against the radio."""
    global cat, radio, scheduler
    global _tx_session_frames, _tx_session_decoded, _tx_session_decode_fail, _tx_non_owner_drops
    global _ptt_key_ws, _ptt_foreign_releases

    # Recording is a server-side session over USB audio, not a radio
    # command: handled before the "radio not connected" guard so a working
    # sound card with a broken CAT link can still record (freq_hz is 0 then,
    # which the file name shows as 00000kHz exactly like mrrc).
    if field == "recording":
        on = value is True or str(value).lower() == "true"
        if on:
            problem = _recording_space_problem()
            if problem:
                # Refuse *before* a session exists: a started-then-failed
                # recording leaves a corrupt MP3 and a UI that says REC.
                await ws.send_text(json.dumps({"type": "error",
                                               "message": problem}))
                return
            freq_hz = radio.active_freq if radio is not None else 0
            try:
                started = _rec_session.start(freq_hz=freq_hz)
            except RecorderError as e:
                # Actionable reason (missing lameenc, unwritable directory)
                # instead of an exception that would drop the control
                # channel — the 2026-09-12 field report.
                logger.warning("Recording could not start: %s", e)
                await ws.send_text(json.dumps({"type": "error",
                                               "message": str(e)}))
                return
            if not started:
                await ws.send_text(json.dumps({
                    "type": "error",
                    "message": "Recording already in progress",
                }))
            else:
                _ensure_rec_writer()      # every session needs a consumer
                logger.info("Recording started by client (freq=%s)", freq_hz)
        else:
            pending = _rec_enqueue_stop()
            if pending is not None:
                try:
                    await asyncio.wait_for(pending, timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("Recording stop timed out — broadcasting "
                                   "the current state anyway")
        await _broadcast_recording_state()
        return

    # CQ call (spec 2026-09-13 §3): handled before the "radio not connected"
    # guard like recording, because a refused call must still report a reason.
    # true = start one call, false = abort the running one.
    if field == "cq":
        on = value is True or str(value).lower() == "true"
        if not on:
            if _cq_player.is_calling:
                await _cq_player.abort("aborted_by_user")
            else:
                await _broadcast_cq_state()
            return
        if _cq_player.is_calling:
            await _broadcast_cq_state()      # "already calling" state to the UI
            return
        if backend is not None and backend.capabilities.tx_gated:
            # Same gate as PTT/TUNE (SDD ch15): checked here as well so an
            # unverified model gets the gate message, not an asset error.
            await ws.send_text(json.dumps({"type": "error",
                                           "message": _TX_GATE_MESSAGE}))
            return
        if radio.tx_status == 2:
            await ws.send_text(json.dumps({
                "type": "error",
                "message": "TUNE is active — stop it before calling CQ",
            }))
            return
        if radio.is_transmitting:
            await ws.send_text(json.dumps({
                "type": "error",
                "message": "Radio is already transmitting — release PTT first",
            }))
            return
        try:
            await _cq_player.start(started_by=_ws_client_id(ws),
                                   owner_token=_ws_tokens.get(ws))
        except CQUnavailable as e:
            logger.info("CQ refused: %s", e)
            await ws.send_text(json.dumps({"type": "error", "message": str(e)}))
        return

    if cat is None or not cat.connected:
        await ws.send_text(json.dumps({
            "type": "error", "message": "Radio not connected",
        }))
        return

    # Brief pause of background polling so the user's next command
    # doesn't queue behind a poll cycle on the serial lock.
    scheduler and scheduler.note_user_command()

    try:
        if field == "freq":
            # "freq" targets whichever VFO is currently active on the radio,
            # so web tuning actually affects the receiving VFO.  (Explicit
            # vfo_a_freq / vfo_b_freq below force a specific VFO.)
            freq = int(value)
            vfo = radio.active_vfo or "A"
            # Skip poll BEFORE sending CAT command so in-flight poll
            # results don't overwrite our new setting (same race as band).
            scheduler and scheduler.skip_next_poll("if", 1.0)
            await cat.set_frequency(freq, vfo)
            if vfo == "B":
                radio.update(vfo_b_freq=freq)
            else:
                radio.update(vfo_a_freq=freq)

        elif field == "vfo_a_freq":
            freq = int(value)
            scheduler and scheduler.skip_next_poll("if", 1.0)
            await cat.set_frequency(freq, "A")
            radio.update(vfo_a_freq=freq)

        elif field == "vfo_b_freq":
            freq = int(value)
            scheduler and scheduler.skip_next_poll("vfo", 1.0)
            await cat.set_frequency(freq, "B")
            radio.update(vfo_b_freq=freq)

        elif field == "mode":
            mode_name = str(value).upper()
            mode_num = _mode_name_to_num().get(mode_name)
            if mode_num is not None:
                await cat.set_mode(mode_num)
                radio.update(mode=mode_num)
                scheduler and scheduler.skip_next_poll("if", 1.0)
            else:
                await ws.send_text(json.dumps({
                    "type": "error", "message": f"Unknown mode: {value}",
                }))

        elif field == "ptt":
            tx = value is True or str(value).lower() == "true"
            if tx and _ft8_transmitting():
                # The FT8 engine is mid-waveform: a manual key-up would put a
                # second source on the same carrier.  It releases in <=13 s.
                await ws.send_text(json.dumps({
                    "type": "error",
                    "message": "FT8 引擎正在发射（约 13 秒）——稍候再按 PTT",
                }))
                return
            if tx and _cq_player.is_calling:
                # The CQ player owns the carrier; a manual key-up would key a
                # second source into the same transmission (spec §6).
                await ws.send_text(json.dumps({
                    "type": "error",
                    "message": "CQ call in progress — press CQ to stop it first",
                }))
                return
            if tx and backend is not None and backend.capabilities.tx_gated:
                # Refused before any CAT write. The backend guard stays as
                # defense in depth (iOS/Android/ATR paths do not come
                # through here).
                await ws.send_text(json.dumps({
                    "type": "error",
                    "message": _TX_GATE_MESSAGE,
                }))
                return
            if tx:
                # The client keying the radio owns the TX-audio uplink —
                # otherwise an idle first-connected client (second tab,
                # iOS app, zombie socket) keeps ownership and this
                # client's mic frames are silently dropped: radio keys
                # but there is no modulation / no RF power.
                _claim_tx_owner_for_token(_ws_tokens.get(ws))
                _ptt_key_ws = ws  # control-plane owner: only this socket's release is honored
                # The key-up itself is proof this session is alive, so a
                # capability-declared session's clock is refreshed by it. The
                # browser sends `ptt:true` first and its key-time `txhb` second,
                # and this handler (CAT write + start_tx, tens of ms) runs before
                # that beat is read off the socket — so a liveness tick landing in
                # the window would otherwise see a session that is both the owner
                # *and* silent, and unkey the operator's own healthy press whenever
                # the previous transmission was longer ago than the threshold
                # (field log 2026-10-04 on a hub instance: seven such releases,
                # 20-70 ms after a key-up, ages 9.3-654 s — each age exactly the
                # gap since that operator's previous press). Keying is still not a
                # capability declaration: a session that has never sent `txhb` is
                # not added here and stays ungated.
                if ws in _tx_hb_capable:
                    _tx_hb_last[ws] = time.monotonic()
                _tx_session_frames = 0
                _tx_session_decoded = 0
                _tx_session_decode_fail = 0
                _tx_non_owner_drops = 0
                # Key radio first so the TX light comes on immediately,
                # THEN open the audio output stream synchronously —
                # awaiting start_tx here avoids a race where the drain
                # loop queues mic frames before the PortAudio stream is
                # open and start_tx's queue-clearing discards them.
                # start_tx() is idempotent — if a stream is already active
                # it returns immediately without clearing the queue.
                await cat.set_ptt(True)
                radio.update(tx_status=1)
                if audio:
                    try:
                        ok = await asyncio.to_thread(audio.start_tx)
                    except Exception as e:
                        # A RAISED open (not just a False return) must still
                        # release the carrier: the keying above already
                        # happened, so an escaping exception here would leave
                        # the radio transmitting with no unkey.
                        logger.error("PTT: TX audio open raised (%s) — unkeying radio", e)
                        ok = False
                    if not ok:
                        # Audio path failed to open — the radio is keyed but
                        # has no modulation.  Drop PTT immediately so the
                        # user sees the failure and can try again, rather
                        # than silently transmitting a dead carrier.
                        logger.error("PTT: TX audio stream failed — unkeying radio")
                        await cat.set_ptt(False)
                        _ptt_key_ws = None
                        radio.update(tx_status=0, power_meter=0, alc_meter=0,
                                     swr_meter=0, comp_meter=0, id_meter=0)
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "message": "TX audio device unavailable — PTT released. Try again.",
                        }))
                        scheduler and scheduler.skip_next_poll("tx_status", 1.0)
                        return
            else:
                # Control-plane arbitration (SDD I6, PTT half): while another
                # client holds the key, its carrier is not ours to drop —
                # every browser tab runs a PTT watchdog that re-sends
                # ptt:false on stale local state and used to unkey the tab
                # that was actually transmitting. Push the authoritative
                # state back so the sender's optimistic UI resyncs and its
                # watchdog stands down (ptt_keyed_by_other).
                if not _ptt_release_allowed(ws):
                    _ptt_foreign_releases += 1
                    if _ptt_foreign_releases % 50 == 1:
                        logger.warning(
                            "PTT release ignored — another client holds the key "
                            "(ignored=%d)", _ptt_foreign_releases)
                    try:
                        await ws.send_text(json.dumps({
                            "type": "stateUpdate",
                            "fields": {"tx_status": radio.tx_status},
                            "dirty": ["tx_status"],
                            "ptt_keyed_by_other": True,
                        }))
                    except Exception:
                        pass
                    return
                # Skip the next TX-status poll BEFORE the drain + unkey
                # (poll-stale-guard: the skip must precede the CAT write, or
                # an in-flight poll transaction — up to POLL_TIMEOUT on the
                # serial lock — answers after TX0 with a stale PTT=1 that
                # would flip the UI back to TX).  radio.update(tx_status=0)
                # below stays authoritative in the meantime.
                scheduler and scheduler.skip_next_poll("tx_status", 1.0)
                # Graceful TX stop: drain queued audio to the DAC and block
                # until it has played (Pa_StopStream semantics), so word-
                # endings go out over RF before we drop PTT. Must run off the
                # event loop — the blocking drain can take up to TX_DRAIN_MS.
                if audio:
                    await asyncio.to_thread(audio.stop_tx, True)
                    st = audio.tx_stats()
                    logger.info(
                        "TX session: frames=%d fed=%d decode_fail=%d "
                        "written=%d write_err=%d queue_drops=%d peak=%.0f%% "
                        "non_owner_drops=%d",
                        _tx_session_frames, _tx_session_decoded,
                        _tx_session_decode_fail, st["written"], st["write_err"],
                        st["queue_drops"], st["peak"] / 327.68,
                        _tx_non_owner_drops)
                # Drop PTT immediately after the drain.  No verify loop:
                # the radio obeys TX0 on the first write, and the TX-status
                # poll (plus the client PTT watchdog) catches a stuck keyup.
                # The previous 3×200 ms verify loop added ~600 ms to release.
                await cat.set_ptt(False)
                _ptt_key_ws = None
                radio.update(tx_status=0, power_meter=0, alc_meter=0,
                             swr_meter=0, comp_meter=0, id_meter=0)
            scheduler and scheduler.skip_next_poll("tx_status", 1.0)

        elif field == "tune":
            on = value is True or str(value).lower() == "true"
            if on and _cq_player.is_calling:
                await ws.send_text(json.dumps({
                    "type": "error",
                    "message": "CQ call in progress — stop it before tuning",
                }))
                return
            if on and backend is not None and backend.capabilities.tx_gated:
                await ws.send_text(json.dumps({
                    "type": "error",
                    "message": _TX_GATE_MESSAGE,
                }))
                return
            # "tx2": CAT tune carrier + AC003 tuner trigger (FT-710).
            # "atu": set_tune() alone runs the radio's internal-ATU
            # sequence (IC-7300 1C 01 02/00) — no AC command exists.
            tune_via = backend.capabilities.tune_via if backend else "tx2"
            if on:
                # Start low-power carrier for tuning (TX2)
                await cat.set_tune(True)
                radio.update(tx_status=2)
                if tune_via == "tx2":
                    # After carrier stabilises, trigger the antenna tuner.
                    # AC003 = P1=0,P2=0,P3=3 → Tuning Start (standard tuner).
                    await asyncio.sleep(0.3)
                    await cat.send_priority_set_command("AC003")
                    radio.update(tuner_status=2)
            else:
                # Drop carrier — stop tuner if still running, then end TX.
                if tune_via == "tx2":
                    await cat.send_priority_set_command("AC000")
                await cat.set_tune(False)
                radio.update(tx_status=0, tuner_status=0)
            scheduler and scheduler.skip_next_poll("tx_status", 1.0)

        elif field == "filter" or field == "filter_width":
            idx = int(value)
            logger.info("Filter set command: index=%d", idx)
            scheduler and scheduler.skip_next_poll("filter_width", 3.0)
            ok = await cat.set_filter_width(idx)
            logger.info("Filter set result: ok=%s index=%d", ok, idx)
            if ok:
                # Read back the radio's actual width instead of trusting
                # the write: the FT-710 silently ignores a width index
                # that is invalid for the current mode, and an optimistic
                # update would then linger ~4s before the post-skip poll
                # corrects it.  Skip window (3s) covers this query.
                await asyncio.sleep(0.15)
                actual = await cat.get_filter_width()
                logger.info("Filter read-back: index=%s (requested %d)", actual, idx)
                radio.update(filter_width=actual if actual is not None else idx)
            else:
                await ws.send_text(json.dumps({
                    "type": "error",
                    "message": f"Filter width command failed: {idx}",
                }))

        elif field == "af_gain":
            v = max(0, min(255, int(value)))
            await cat.set_af_gain(v)
            radio.update(af_gain=v)
            scheduler and scheduler.skip_next_poll("af_gain", 3.0)

        elif field == "rf_power":
            v = max(5, min(100, int(value)))
            await cat.set_rf_power(v)
            radio.update(rf_power=v)
            scheduler and scheduler.skip_next_poll("rf_power", 3.0)

        elif field == "preamp":
            v = int(value)
            if v in (0, 1, 2):
                await cat.set_preamp(v)
                radio.update(preamp=v)
                scheduler and scheduler.skip_next_poll("preamp", 3.0)

        elif field == "att" or field == "attenuator":
            v = int(value)
            # Step count is per radio: 0/1 on the FT-710 and IC-7300/IC-705,
            # 0..15 on the IC-7610/IC-7760 (0-45 dB in 3 dB steps).
            _att_steps = (backend.capabilities.att_steps
                          if backend is not None else (0, 6, 12, 18))
            if 0 <= v < len(_att_steps):
                await cat.set_attenuator(v)
                radio.update(attenuator=v)
                scheduler and scheduler.skip_next_poll("attenuator", 3.0)

        elif field == "nb" or field == "noise_blanker":
            on = value is True or str(value).lower() in ("true", "1")
            await cat.set_noise_blanker(on)
            radio.update(noise_blanker=on)
            scheduler and scheduler.skip_next_poll("noise_blanker", 3.0)

        elif field == "nr" or field == "noise_reduction":
            on = value is True or str(value).lower() in ("true", "1")
            await cat.set_noise_reduction(on)
            radio.update(noise_reduction=on)
            scheduler and scheduler.skip_next_poll("noise_reduction", 3.0)

        elif field == "an" or field == "auto_notch":
            on = value is True or str(value).lower() in ("true", "1")
            await cat.set_auto_notch(on)
            radio.update(auto_notch=on)
            scheduler and scheduler.skip_next_poll("auto_notch", 3.0)

        elif field == "comp" or field == "compressor":
            on = value is True or str(value).lower() in ("true", "1")
            await cat.set_compressor(on)
            radio.update(compressor=on)
            scheduler and scheduler.skip_next_poll("compressor", 5.0)

        elif field == "tuner":
            v = int(value)
            if v in (0, 1, 2):
                await cat.set_tuner(v)
                radio.update(tuner_status=v)
                scheduler and scheduler.skip_next_poll("tuner_status", 3.0)

        elif field == "vfo":
            vfo = str(value).upper()
            if vfo in ("A", "B"):
                await cat.set_vfo(vfo)
                radio.update(active_vfo=vfo)

        elif field == "split":
            on = value is True or str(value).lower() in ("true", "1")
            await cat.set_split(on)
            radio.update(split=on)

        elif field == "power":
            token = str(value).strip().lower()
            if token == "restart":
                # Restart = off, then keep trying to wake it until it answers.
                # The two intents must never blur: "off" (below) does not
                # re-apply power by itself, this one does.
                refusal = await _power_off(cat, radio, scheduler)
                if refusal:
                    await ws.send_text(json.dumps({"type": "error", "message": refusal}))
                else:
                    _power_rescue_start()
            elif token == "cancelrescue":
                await _power_rescue_cancel()
            elif value is True or token in ("true", "1"):
                scheduler and scheduler.skip_next_poll("power_on", 25.0)
                if await _power_on_radio(cat, backend):
                    radio.update(power_on=True)
                else:
                    radio.update(power_on=False)
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "message": "电台开机无响应——请检查电台并手动开机",
                    }))
            else:
                # "Off" always means stay off: cancel any rescue first, and
                # let _power_off apply the guards (it never sends a PS1).
                await _power_rescue_cancel()
                refusal = await _power_off(cat, radio, scheduler)
                if refusal:
                    await ws.send_text(json.dumps({"type": "error", "message": refusal}))

        elif field == "squelch":
            v = max(0, min(100, int(value)))
            await cat.set_squelch(v)
            radio.update(squelch=v)

        elif field == "mic_gain":
            v = max(0, min(100, int(value)))
            await cat.set_mic_gain(v)
            radio.update(mic_gain=v)

        elif field == "scope_span":
            v = int(value)
            if 0 <= v <= 9:
                await cat.set_scope_span(v)
                radio.update(scope_span=v)
                scheduler and scheduler.skip_next_poll("scope_span", 3.0)

        elif field == "scope_speed":
            v = int(value)
            if 0 <= v <= 5:
                await cat.set_scope_speed(v)
                radio.update(scope_speed=v)

        elif field == "scope_mode":
            v = int(value)
            if 0 <= v <= 9:
                await cat.set_scope_mode(v)
                radio.update(scope_mode=v)

        elif field == "nb_level":
            v = max(0, min(10, int(value)))
            # Read the level back instead of trusting the write: an FT-710
            # silently ignores a malformed `NL` width, and before this fix
            # nothing ever read these two fields — the slider was a local echo
            # the radio never saw (2026-10-28 field report, BG7ZHS).
            if await cat.set_nb_level(v):
                await asyncio.sleep(0.15)
                actual = await cat.get_nb_level()
                if actual is not None and actual != v:
                    logger.warning(
                        "NB level set to %d but the radio reports %d — the radio "
                        "did not take the command (check the NL command width)",
                        v, actual)
                radio.update(nb_level=actual if actual is not None else v)
                scheduler and scheduler.skip_next_poll("nb_level", 3.0)
            else:
                logger.warning("NB level command failed: %d (serial link down?)", v)

        elif field == "nr_level":
            v = max(1, min(15, int(value)))
            if await cat.set_nr_level(v):
                await asyncio.sleep(0.15)
                actual = await cat.get_nr_level()
                if actual is not None and actual != v:
                    logger.warning(
                        "NR level set to %d but the radio reports %d — the radio "
                        "did not take the command (check the RL command width)",
                        v, actual)
                radio.update(nr_level=actual if actual is not None else v)
                scheduler and scheduler.skip_next_poll("nr_level", 3.0)
            else:
                logger.warning("NR level command failed: %d (serial link down?)", v)

        elif field == "comp_level" or field == "compressor_level":
            v = max(1, min(100, int(value)))
            await cat.set_compressor_level(v)
            radio.update(compressor_level=v)

        elif field == "monitor":
            on = value is True or str(value).lower() in ("true", "1")
            await cat.set_monitor(on)

        elif field == "vox":
            on = value is True or str(value).lower() in ("true", "1")
            await cat.set_vox(on)
            radio.update(vox=on)

        elif field == "break_in":
            on = value is True or str(value).lower() in ("true", "1")
            await cat.set_break_in(on)
            radio.update(break_in=on)

        elif field == "key_speed":
            v = max(0, min(60, int(value)))
            await cat.set_key_speed(v)

        elif field == "cw_pitch":
            v = max(0, min(75, int(value)))
            await cat.set_cw_pitch(v)

        elif field == "rit":
            on = value is True or str(value).lower() in ("true", "1")
            await cat.set_rit(on)

        elif field == "rit_freq":
            v = max(-9999, min(9999, int(value)))
            await cat.set_rit_freq(v)

        elif field == "xit":
            on = value is True or str(value).lower() in ("true", "1")
            await cat.set_xit(on)

        elif field == "meter_display":
            v = int(value)
            if 0 <= v <= 5:
                await cat.set_meter_display(v)
                radio.update(meter_display=v)
                scheduler and scheduler.skip_next_poll("meter_display", 3.0)

        elif field == "amc_level":
            v = max(1, min(100, int(value)))
            await cat.set_amc_level(v)
            radio.update(amc_level=v)
            scheduler and scheduler.skip_next_poll("amc_level", 5.0)

        elif field == "rf_gain":
            v = max(0, min(255, int(value)))
            await cat.set_rf_gain(v)
            radio.update(rf_gain=v)
            scheduler and scheduler.skip_next_poll("rf_gain", 3.0)

        elif field == "scope_on":
            on = value is True or str(value).lower() in ("true", "1")
            await cat.set_scope_on(on)
            radio.update(scope_on=on)
            scheduler and scheduler.skip_next_poll("scope_on", 3.0)

        elif field == "antenna":
            v = int(value)
            if 1 <= v <= 3:
                await cat.set_antenna(v)
                radio.update(antenna=v)
                scheduler and scheduler.skip_next_poll("antenna", 3.0)

        elif field == "agc":
            v = int(value)
            if 0 <= v <= 3:
                await cat.set_agc(v)
                radio.update(agc=v)
                scheduler and scheduler.skip_next_poll("agc", 3.0)

        elif field == "dnr" or field == "dnr_level":
            v = max(0, min(15, int(value)))
            await cat.set_dnr(v)
            radio.update(dnr_level=v)
            scheduler and scheduler.skip_next_poll("dnr_level", 3.0)

        elif field == "contour" or field == "contour_level":
            v = max(0, min(255, int(value)))
            await cat.set_contour(v)
            radio.update(contour_level=v)
            scheduler and scheduler.skip_next_poll("contour_level", 5.0)

        elif field == "drive":
            # Drive maps to RF Power (PC) on FT-710 Yaesu
            v = max(5, min(100, int(value)))
            await cat.set_drive(v)
            radio.update(rf_power=v)
            scheduler and scheduler.skip_next_poll("rf_power", 3.0)

        elif field == "band":
            # Set band by name — look up in the active backend's table
            # (FT-710 BANDS carry "bsr"; IC-7300 bands don't — Icom
            # selects bands purely by frequency, no band-stack register).
            band_name = str(value)
            bands = backend.bands if backend else BANDS
            band = next((b for b in bands if b["name"] == band_name), None)
            if band:
                logger.info(
                    "Band change request: name=%s bsr=%s default_freq=%d active_vfo=%s",
                    band["name"], band.get("bsr"), band["default_freq"], radio.active_vfo,
                )
                # Skip the next IF poll BEFORE sending CAT commands so that
                # any in-flight poll result (which may have already read the
                # old frequency) is discarded rather than overwriting our new
                # setting.  See poll_scheduler._poll_if for the counterpart.
                scheduler and scheduler.skip_next_poll("if", 1.0)
                scheduler and scheduler.skip_next_poll("vfo", 1.0)
                bs_ok = True
                if "bsr" in band:
                    bs_ok = await cat.set_band_stack(band["bsr"])
                fa_ok = await cat.set_frequency(band["default_freq"], "A")
                # FT-710 band stacking auto-recalls the last-used mode for
                # the new band.  Explicitly set a sensible default so the
                # web UI and saved memory channels see a predictable mode.
                # Numbers are backend-native (via the backend mode map):
                # LSB below 10 MHz, USB above.
                mode_map = _mode_name_to_num()
                default_mode = mode_map.get(
                    "LSB" if band["default_freq"] < 10_000_000 else "USB")
                md_ok = await cat.set_mode(default_mode)
                if bs_ok and fa_ok:
                    radio.update(
                        vfo_a_freq=band["default_freq"],
                        active_vfo="A",
                        mode=default_mode,
                    )
                    logger.info(
                        "Band change applied: name=%s BS%s OK FA%09d OK "
                        "MD0%X OK local_band=%s mode=%s",
                        band["name"], band.get("bsr"), band["default_freq"],
                        default_mode, radio.band_name, radio.mode_name,
                    )
                else:
                    # CAT command(s) failed — don't update server state,
                    # let the next poll cycle correct things.  Tell the
                    # client so it can revert its optimistic update.
                    failed = []
                    if not bs_ok:
                        failed.append(f"band stack BS{band['bsr']:02d}")
                    if not fa_ok:
                        failed.append(f"frequency FA{band['default_freq']:09d}")
                    if not md_ok:
                        failed.append(f"mode MD0{default_mode:X}")
                    logger.error(
                        "Band change FAILED: name=%s failed_commands=%s",
                        band["name"], failed,
                    )
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "message": f"Band change to {band['name']} failed: {', '.join(failed)}",
                    }))
                    # Skip the broadcast at the end — poll will correct state
                    return
            else:
                logger.warning("Band change rejected: unknown band=%r", band_name)
                await ws.send_text(json.dumps({
                    "type": "error",
                    "message": f"Unknown band: {band_name}",
                }))

        elif field == "vfo_equal":
            # A = B: copy VFO-B to VFO-A
            scheduler and scheduler.skip_next_poll("if", 1.0)
            await cat.set_frequency(radio.vfo_b_freq, "A")
            radio.update(vfo_a_freq=radio.vfo_b_freq)

        elif field == "vfo_swap":
            # Swap VFO-A and VFO-B
            fa = radio.vfo_a_freq
            fb = radio.vfo_b_freq
            scheduler and scheduler.skip_next_poll("if", 1.0)
            scheduler and scheduler.skip_next_poll("vfo", 1.0)
            await cat.set_frequency(fb, "A")
            await cat.set_frequency(fa, "B")
            radio.update(vfo_a_freq=fb, vfo_b_freq=fa)

        # After executing, broadcast immediately
        await _broadcast_state()

    except Exception as e:
        logger.error("Command error (%s=%s): %s", field, value, e)
        await ws.send_text(json.dumps({
            "type": "error", "message": str(e),
        }))


# ── Initial State Sync ──────────────────────────────────────────────

async def _initial_state_sync():
    """Perform full state read from radio on initial connect."""
    global cat, radio
    if cat is None or not cat.connected:
        return
    logger.info("Performing initial state sync...")
    # Backends return already-parsed RadioState field values (Phase 2b).
    sync_data = await (backend.initial_state_sync() if backend is not None
                       else cat.initial_state_sync())
    new_state = RadioState.from_sync_result(sync_data)
    new_state.serial_connected = True
    # Copy all fields to the global state
    for field_name in vars(new_state):
        if not field_name.startswith('_'):
            setattr(radio, field_name, getattr(new_state, field_name))
    radio.mark_dirty("serial_connected")
    logger.info("Initial state: freq=%d mode=%s s_meter=%d",
                radio.active_freq, radio.mode_name, radio.s_meter)


# ── FastAPI App ─────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: connect to the radio, start polling, scope, and audio.
       Shutdown: disconnect, force RX, cancel tasks."""
    global backend, cat, scheduler, scope, audio, _scope_producer, _scope_broadcast_task
    global _audio_rx_task, _audio_tx_task, _opus_tx_decoder, STARTUP_TIME

    # ── Startup ──
    STARTUP_TIME = time.time()
    logger.info("MRRC Modern Web Control starting on port %d", WEB_PORT)
    logger.info("Radio model: %s, serial port: %s @ %d baud",
                RADIO_MODEL, SERIAL_PORT, BAUD_RATE)
    if ENV_REPAIRS:
        # A repair nobody can see is the kind of invisible state this repo keeps paying
        # for (design D-8): name the keys, the shadowed value and the kept one.
        logger.warning("env file had duplicated keys, repaired in place: %s",
                       ", ".join(f"{k}={v!r}" for k, v in ENV_REPAIRS.items()))
    port_problem = _serial_port_problem()
    if port_problem:
        logger.warning("%s", port_problem)
    _warn_if_default_password()

    backend = create_backend(RADIO_MODEL, port=SERIAL_PORT, baud_rate=BAUD_RATE)
    # The backend surface is CatController-compatible; keep the historical
    # global name so handlers/poll scheduler stay unchanged.
    cat = backend.cat
    # Inject per-radio mode/calibration tables into the shared state.
    radio.configure(**backend.state_tables())
    # Radio-originated changes (IC-7300 CI-V transceive broadcasts) flow
    # into the same dirty-broadcast path as poll-driven changes.
    if hasattr(backend, "set_broadcast_callback"):
        backend.set_broadcast_callback(_on_cat_broadcast)
    connected = await cat.connect()
    radio.update(serial_connected=connected)

    if connected:
        await _initial_state_sync()
        await backend.init_scope()
        scheduler = PollScheduler(cat, radio, on_state_changed=_broadcast_state,
                                  on_reconnected=backend.init_scope,
                                  backend=backend)
        await scheduler.start()
    else:
        logger.warning("Could not connect to radio. Server will serve UI only.")
        logger.warning("Check serial port: %s", SERIAL_PORT)

    # ── Audio handler ──────────────────────────────────────────────
    # Device rate + auto-detect name hints come from the active backend's
    # capabilities (FT-710 values reproduce the historical defaults).
    if backend is not None:
        _caps = backend.capabilities
        if not _caps.verified:
            # Name the matching field tool: the CI-V and ASCII-CAT families
            # have different self-checks (Icom `19 00`/scope vs Yaesu `ID;`).
            _diag = ("_diag_civ.py" if _caps.scope_type == "civ27"
                     else "_diag_yaesu.py")
            logger.warning(
                "Radio model %s is NOT hardware-verified — transmit is %s "
                "(profile verified=False; check the radio with %s, "
                "then set MRRC_ALLOW_UNVERIFIED_TX=1 to enable TX)",
                _caps.model_name,
                "DISABLED" if _caps.tx_gated else "ENABLED by env override",
                _diag)
        audio = AudioHandler(rx_rate=_caps.audio_rx_rate,
                             tx_rate=_caps.audio_tx_rate,
                             name_hints=tuple(_caps.audio_name_hints))
    else:
        audio = AudioHandler()
    # Use a thread with timeout — FT-710 USB audio can hang on open
    _audio_ok = False
    _pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    _audio_fut = _pool.submit(audio.start_rx)
    try:
        _audio_ok = _audio_fut.result(timeout=8)
    except concurrent.futures.TimeoutError:
        logger.warning("Audio RX init timed out (8s) — FT-710 USB audio may be unresponsive")
        logger.warning("  Try reconnecting the FT-710 USB cable or power-cycling the radio")
    except Exception as e:
        logger.warning("Audio RX init failed: %s", e)
    finally:
        _pool.shutdown(wait=False)  # Don't block on the hung p.open() thread

    # TX Opus decoder (browser mic → server → radio)
    try:
        _opus_tx_decoder = TxOpusDecoder()
    except Exception as e:
        logger.warning("TX Opus decoder unavailable: %s", e)
        _opus_tx_decoder = None

    _local_ctl_task = asyncio.create_task(_local_control_server(), name="localctl")
    _perf_lag_task = asyncio.create_task(_perf_lag_monitor(), name="perf_lag")

    # Local speaker (AD-028): the radio's audio on a device beside the box.
    global _local_speaker
    _speaker_device = _env("MRRC_LOCAL_SPEAKER_DEVICE", "")
    if _speaker_device:
        try:
            _local_speaker = LocalSpeaker(
                _speaker_device,
                rate=int(_env("MRRC_LOCAL_SPEAKER_RATE", "8000") or 8000),
                channels=int(_env("MRRC_LOCAL_SPEAKER_CHANNELS", "1") or 1),
            )
            _local_speaker.start()
        except Exception as exc:                        # noqa: BLE001 - never fatal
            logger.warning("Local speaker unavailable: %s", exc)
            _local_speaker = None

    # Start audio tasks (skip if audio init failed)
    if _audio_ok:
        _audio_rx_task = asyncio.create_task(_audio_rx_loop(), name="audio_rx")
    _audio_tx_task = asyncio.create_task(_audio_tx_drain_loop(), name="audio_tx")
    global _rec_writer_task
    _rec_writer_task = asyncio.create_task(
        _recording_writer_loop(_rec_queue, _rec_session), name="rec_writer")
    _log_recording_readiness()
    _bind_cq_player()

    # Box-level audio gains (AD-032): read once at startup, never at import —
    # an unreadable or hand-edited file falls back to 0 dB with one warning.
    global _audio_levels
    _audio_levels = _audio_levels_store.load()
    if _audio_levels.tx_mic_db or _audio_levels.rx_db:
        logger.info("Audio levels from %s: TX mic %+.1f dB, RX %+.1f dB",
                    _audio_levels_store.source,
                    _audio_levels.tx_mic_db, _audio_levels.rx_db)

    # ── FT8 mode composition (dormant until the operator switches) ──
    # The engine is assembled here but started/stopped by the mode controller;
    # the RX tap is fed by _audio_rx_loop (see the recording tap there).
    global _ft8_mode, _ft8_runtime, _ft8_settings_store
    try:
        from ft8.engine.sequencer import Sequencer
        from ft8.mode import Ft8ModeController
        from ft8.platform.cat import PlatformRig
        from ft8.platform.settings import Ft8SettingsStore
        from ft8.platform.tx_sink import PlatformTxPlayer
        from ft8.runtime import Ft8Runtime, _policy_from_settings

        _ft8_settings_store = Ft8SettingsStore(
            _writable_runtime_dir() / "ft8" / "settings.json"
        )
        _ft8_data_dir = _writable_runtime_dir() / "ft8"
        # ``backend`` decides what a mode name and a filter width mean: the
        # FT-710 and the Icom CI-V radios disagree on both (Yaesu USB=0x02 vs
        # Icom USB=0x01; 21 filter widths vs FIL1-3).  Without it the rig fell
        # back to the FT-710 tables and every FT8 start on an IC-7300 failed
        # with "set_filter_width(3000 Hz) refused" (bg7kxr, 2026-10-26).
        platform_rig = PlatformRig(cat, _tx_session, rig_mode="DATA-U", backend=backend)

        async def _ft8_snapshot_ssb() -> dict:
            """SSB operating state, restored field by field on the way back."""
            dial = radio.vfo_b_freq if radio.active_vfo == "B" else radio.vfo_a_freq
            return {
                "freq": dial, "mode": radio.mode, "filter": radio.filter_width,
                "rf_power": radio.rf_power, "attenuator": radio.attenuator,
                "preamp": radio.preamp,
            }

        async def _ft8_apply_ft8_radio() -> None:
            settings = _ft8_settings_store.load()
            platform_rig._rig_mode = settings.rig_mode        # noqa: SLF001 (settings live here)
            await platform_rig.set_mode(settings.rig_mode, 3000)
            await platform_rig.set_filter_width(3000)

        async def _ft8_restore_ssb(snap: dict) -> None:
            if snap.get("freq"):
                await cat.set_frequency(int(snap["freq"]))
            if snap.get("mode") is not None:
                await cat.set_mode(int(snap["mode"]))
            if snap.get("filter") is not None:
                await cat.set_filter_width(int(snap["filter"]))
            if snap.get("rf_power") is not None:
                await cat.set_rf_power(int(snap["rf_power"]))
            if snap.get("attenuator") is not None:
                await cat.set_attenuator(int(snap["attenuator"]))
            if snap.get("preamp") is not None:
                await cat.set_preamp(int(snap["preamp"]))

        def _ft8_set_browser_tx(enabled: bool) -> None:
            # No mode-level TX ownership any more: the SSB side stays
            # available and _ft8_transmitting() guards the actual conflict.
            return None

        async def _ft8_broadcast(mode: str) -> None:
            await _broadcast_state()
            # Carry the engine state so the FT8 page can clear/show its
            # "engine not running" hint without a page reload.
            await _broadcast_ft8_json({
                "type": "mode",
                "mode": mode,
                "engine_running": bool(_ft8_runtime is not None and _ft8_runtime.running),
            })

        def _ft8_make_sequencer() -> Sequencer:
            settings = _ft8_settings_store.load()
            return Sequencer(my_call=settings.my_call, my_grid=settings.my_grid)

        def _ft8_on_decode(batch: object) -> None:
            # ``batch`` is already the wire frame (slot_id/late/messages).
            if isinstance(batch, dict) and batch.get("type") == "decodes":
                _ft8_queue_frame(batch)
            # Once per slot, refresh the page's QSO/safety panel: the
            # sequencer moves (calling → replying → waiting) between slots
            # and the panel would otherwise freeze on the load-time snapshot.
            try:
                if _ft8_runtime is not None and _ft8_runtime.sequencer is not None:
                    seq = _ft8_runtime.sequencer
                    _ft8_queue_frame({
                        "type": "ft8_state",
                        "sequencer": {
                            "state": seq.state.value,
                            "tx_enabled": seq.tx_enabled,
                            "dx_call": seq.dx_call,
                        },
                        "safety": _ft8_runtime.safety.health,
                    })
            except Exception:
                logger.debug("ft8 state frame failed", exc_info=True)

        _ft8_runtime = Ft8Runtime(
            rig=platform_rig,
            tx_player=PlatformTxPlayer(audio, queued_frames=lambda: audio.tx_queue_frames()),
            sequencer_factory=_ft8_make_sequencer,
            data_dir=_ft8_data_dir,
            dev_rate=RX_RATE,          # the codec-domain rate of read_rx_chunk()
            on_decode=_ft8_on_decode,
            on_waterfall=_ft8_queue_frame,
            # The persisted escalation policy, read fresh at every engine start
            # (Task 14).  Injecting the store here rather than letting the runtime
            # build its own keeps ``_ft8_settings_store`` the single owner — a
            # second Ft8SettingsStore would be a silent second source of truth.
            cascade_policy_provider=lambda: _policy_from_settings(
                _ft8_settings_store.load()
            ),
        )
        def _ft8_always_on() -> bool:
            """The engine-always-on switch (settings.json; defaults to on)."""

            try:
                return bool(_ft8_settings_store.load().engine_always_on)
            except Exception:
                return True

        _ft8_mode = Ft8ModeController(
            snapshot_ssb=_ft8_snapshot_ssb,
            apply_ft8_radio=_ft8_apply_ft8_radio,
            restore_ssb=_ft8_restore_ssb,
            start_engine=_ft8_runtime.start,
            stop_engine=_ft8_runtime.stop,
            set_browser_tx=_ft8_set_browser_tx,
            broadcast=_ft8_broadcast,
            engine_always_on=_ft8_always_on,
        )
        # REST surface: /api/ft8/* on the platform app, sharing the platform
        # session cookie (AUTH_COOKIE) and the control/listen role split.
        from ft8.web.auth_shim import PlatformAuthAdapter
        from ft8.web.ft8_api import AppState as Ft8AppState, attach_ft8_router

        ft8_state = Ft8AppState(
            auth=PlatformAuthAdapter(lambda tok: tok in _auth_tokens,
                                     role_of_token=_role_for_token),
            lease=None,
            safety=_ft8_runtime.safety,
            sequencer=_ft8_runtime.sequencer,
            repository=_ft8_runtime.repository,
            rig=platform_rig,
            waterfall_fanout=_ft8_runtime.waterfall,
            my_call=_ft8_runtime.sequencer.my_call,
            my_grid=_ft8_runtime.sequencer.my_grid,
            mode_controller=_ft8_mode,
            runtime=_ft8_runtime,
            platform_dial=_platform_dial_hz,
            settings_store=_ft8_settings_store,
        )

        # Always-on (default): the RX monitor runs from startup so the page
        # has decodes/waterfall even before the operator switches modes.
        if _ft8_always_on():
            try:
                await _ft8_runtime.start()
                logger.info("FT8 engine started at boot (RX monitor always-on)")
            except Exception:
                logger.exception("FT8 always-on start failed; the page offers a manual start")
        from ft8.web.ft8_ws import Ft8EventBus

        global _ft8_bus
        _ft8_bus = Ft8EventBus()
        _ft8_late_state.bind(ft8_state)
        _ft8_bus_late.bind(_ft8_bus)
        logger.info("FT8 composition bound: /api/ft8 and /WSft8 are live")
    except Exception:
        logger.exception("FT8 composition failed — running SSB-only")
        _ft8_mode = _ft8_runtime = _ft8_settings_store = None

    _log_cq_readiness()
    global _max_tx_watchdog_task
    _max_tx_watchdog_task = asyncio.create_task(_max_tx_watchdog(), name="max_tx_watchdog")
    global _session_metrics_task
    if SESSION_METRICS_INTERVAL_S > 0:
        _session_metrics_task = asyncio.create_task(
            _session_metrics_loop(), name="session_metrics")
    global _tx_liveness_task
    if REMOTE_SESSION_TX_HEARTBEAT_S > 0:
        _tx_liveness_task = asyncio.create_task(
            _tx_liveness_watchdog(), name="tx_liveness")

    # Start scope handler — broadcasts S-meter fallback until real data arrives
    scope = ScopeHandler()
    scope.set_on_frame(_on_scope_frame)
    # Backend-owned scope producer (FT-710: FT4222 scope_pipe subprocess).
    # Radio-state merging stays here via the _on_scope_frame callback.
    _scope_producer = backend.create_scope_producer(scope, _on_scope_frame)
    _scope_broadcast_task = asyncio.create_task(_broadcast_spectrum_loop(), name="scope_bcast")

    # Try to initialize scope via CAT (enables FT4222 SPI output on radio)
    # Do this even if initial CAT probe failed — the serial port may still work
    await backend.init_scope()

    # ── ATR1000 external tuner (optional) ─────────────────────────
    # Lazily imported so installs without the hardware never even load
    # the module.  Any failure here just leaves the feature off.
    global atr, _atr_storage
    if ATR1000_HOST:
        try:
            from atr1000_client import ATR1000Client
            from atr1000_tuner import get_storage
            _atr_storage = get_storage()
            atr = ATR1000Client(ATR1000_HOST, ATR1000_PORT, storage=_atr_storage)
            atr.on_change = _on_atr_change
            atr.on_tune_event = _on_atr_tune_event
            await atr.start()
            # Prime the linkage with the current radio state — the startup
            # freq sync ran BEFORE the client existed, so without this the
            # first relay auto-apply would wait for the user's first QSY.
            atr.notify_freq(radio.active_freq)
            atr.notify_tx(bool(radio.is_transmitting))
            logger.info("ATR1000 tuner client started (%s:%d)",
                        ATR1000_HOST, ATR1000_PORT)
        except Exception as e:
            logger.warning("ATR1000 init failed (feature disabled): %s", e)
            atr = None

    logger.info("Server ready!")

    yield

    # ── Shutdown ──
    _local_ctl_task.cancel()
    _perf_lag_task.cancel()
    if _local_speaker is not None:
        _local_speaker.stop()
    if _cq_player is not None and _cq_player.is_calling:
        await _cq_player.abort("shutdown")
    logger.info("Shutting down...")

    # Force RX (safety)
    if cat and cat.connected:
        try:
            await cat.set_ptt(False)
            await asyncio.sleep(0.1)
        except Exception:
            pass

    # Stop the cloud-hub tunnel before the server it points at goes away. frpc is this process's
    # child, and a child that outlives its parent keeps the proxy name against the next start -
    # measured on macOS 2026-10-06: six restarts in half an hour, six orphans, each still logged
    # in and retrying for a name the running instance could not register. A crash or a kill skips
    # this and leaves it to the sweep at startup, which is why that sweep has to work too.
    global _cloud_tunnel
    if _cloud_tunnel is not None:
        _cloud_tunnel.stop()
        _cloud_tunnel = None

    # A help window does not survive a restart either — that is part of "not resident".
    # Closing it here also removes the tunnel proxy, so the next start finds no stray one.
    if _help_singleton is not None:
        _help_singleton.close("shutdown")

    # Stop audio
    if _audio_rx_task:
        _audio_rx_task.cancel()
    if _audio_tx_task:
        _audio_tx_task.cancel()
    if _max_tx_watchdog_task:
        _max_tx_watchdog_task.cancel()
        _max_tx_watchdog_task = None
    if _session_metrics_task:
        _session_metrics_task.cancel()
        _session_metrics_task = None
    if _tx_liveness_task:
        _tx_liveness_task.cancel()
        _tx_liveness_task = None
    # Recording: let the writer finish whatever is queued (a stop sentinel
    # flushes the MP3), then make sure no session is left open.  A crash
    # here must never block shutdown, so close_without_finishing() is the
    # backstop rather than the normal path.
    _rec_enqueue_stop()
    if _rec_writer_task:
        try:
            await asyncio.wait_for(_rec_writer_task, timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _rec_writer_task.cancel()
        _rec_writer_task = None
    _rec_session.close_without_finishing()
    _rec_pool.shutdown(wait=False)
    if _opus_tx_decoder:
        try:
            _opus_tx_decoder.close()
        except Exception:
            pass
        _opus_tx_decoder = None
    if audio:
        await asyncio.to_thread(audio.close)
        audio = None

    # Stop scope
    if _scope_producer is not None:
        await _scope_producer.stop()
    if _scope_broadcast_task:
        _scope_broadcast_task.cancel()
    if scope:
        await scope.disconnect()

    if scheduler:
        await scheduler.stop()
    if atr:
        try:
            await atr.stop()
        except Exception:
            pass
        atr = None
    if cat:
        await cat.disconnect()
    logger.info("Server stopped.")


app = FastAPI(title="MRRC Modern Web Control", lifespan=lifespan)


class _LateBound:
    """A stand-in whose real object is bound by the lifespan.

    The SPA fallback route (``/{path:path}``, registered at the end of this
    module) matches *everything*, and Starlette matches in registration
    order — so /api/ft8/* and /WSft8 must be mounted while the module loads,
    not from the lifespan.  The composition root itself only exists after
    startup, so the route handlers receive this proxy.
    """

    def __init__(self, what: str) -> None:
        object.__setattr__(self, "_what", what)
        object.__setattr__(self, "_impl", None)

    def bind(self, impl: object) -> None:
        object.__setattr__(self, "_impl", impl)

    @property
    def bound(self) -> bool:
        return object.__getattribute__(self, "_impl") is not None

    def __getattr__(self, name: str):
        impl = object.__getattribute__(self, "_impl")
        if impl is None:
            raise RuntimeError(f"{object.__getattribute__(self, '_what')} not bound yet")
        return getattr(impl, name)


_ft8_late_state = _LateBound("FT8 API state")
_ft8_bus_late = _LateBound("FT8 event bus")

try:
    from ft8.web.ft8_api import attach_ft8_router as _attach_ft8_router
    from ft8.web.ft8_ws import Ft8WsState as _Ft8WsState, create_ft8_ws_router as _create_ft8_ws_router

    _attach_ft8_router(app, _ft8_late_state)
    app.include_router(_create_ft8_ws_router(_Ft8WsState(
        bus=_ft8_bus_late,
        token_from_handshake=_token_from_ws_handshake,
        auth_ok=lambda tok: tok in _auth_tokens,
        mode=lambda: _work_mode_value(),
        engine_running=lambda: bool(_ft8_runtime is not None and _ft8_runtime.running),
        recent_decodes=lambda: (
            list(_ft8_runtime.recent_decodes) if _ft8_runtime is not None else []
        ),
    )))
    logger.info("FT8 REST/WS routes mounted (composition root binds at startup)")
except Exception:
    logger.exception("FT8 route mounting failed — the page will not be served")

# COOP/COEP middleware for SharedArrayBuffer (future audio worklet support)
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Embedder-Policy"] = "credentialless"
    return response


# ── Auth Middleware ──────────────────────────────────────────────────

# Paths that don't require authentication
#
# box_api.js is the client script the onboarding page loads (<script
# src="box_api.js"> resolves, from /setup, to GET /box_api.js). That page is
# rendered *before* any session exists, so its asset has to be readable with no
# session — the same reason favicon.png and sw.js are here. It is a fetch
# wrapper and a cookie reader that carries no secret, and anyone who can fetch
# it is already being served setup.html.
PUBLIC_PATHS = {"/login", "/api/auth/login", "/favicon.png", "/manifest.json", "/sw.js",
                "/box_api.js"}
# WebSocket paths: must NOT be redirected — WebSocket can't follow 302.
# Let the WS handler check auth and close with proper code 4001.
WS_PATHS = {"/WSradio", "/WSspectrum", "/WSaudioRX", "/WSaudioTX", "/WSsdr"}
# SDR 入口用独立的 mrrc_sdr cookie（与主站口令隔离），handler 自己鉴权。
SDR_PATHS = frozenset({"/sdr", "/api/sdr/login"})
# sdr.html 引用的四个资源（根相对路径）：只有已认证的 SDR 会话能取。
SDR_STATIC_PATHS = frozenset({"sdr.js", "rx_worklet_processor.js",
                              "modules/opus_wasm.js", "modules/opus_codec.js"})

# The passwordless onboarding window is judged per-request by
# _first_window_open(), not by a path set: /setup plus two /api/box endpoints
# pass the middleware unauthenticated and refuse themselves (design §5, §10).
# The write surface is one key — MRRC_WEB_PASSWORD — and nothing else.
FIRST_WINDOW_PATHS = frozenset({
    "/setup",
    "/api/box/onboarding",
    "/api/box/password",
})

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Protect all routes except public paths, static assets, and login."""
    path = request.url.path

    # Allow static assets and public paths
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)

    # Allow login API
    if path == "/api/auth/login":
        return await call_next(request)

    # WebSocket paths: pass through (WS handlers send code 4001 on auth failure
    # so the browser's handleAuthExpired() can act on it properly).
    if path in WS_PATHS:
        return await call_next(request)

    # SDR 页面与它的登录端点：口令是 MRRC_SDR_PASSWORD，不是主站口令，
    # 所以在这里放行、由 handler 自己比对（与 FIRST_WINDOW_PATHS 同一形状）。
    if path in SDR_PATHS:
        return await call_next(request)

    # SDR 页面自己的四个资源：已认证的 SDR 会话放行，别的会话照旧回登录页。
    # 中间件先于 serve_static 运行，两边用同一个判定（现场：会话已登录却
    # 连自己的 sdr.js 都拿不到，中间件给了 302 /login?next=/sdr.js）。
    if path.lstrip("/") in SDR_STATIC_PATHS and _sdr_static_allowed(request, path):
        return await call_next(request)

    # The onboarding window: pass through so a client that has never logged in
    # can set the box's first password. Before the auth check (that is the
    # point), and therefore also before the listen-only 403 below — so each
    # handler calls _first_window_open() itself (design §5.3). Equality, not
    # startswith: a prefix here would widen the hole to every path under it.
    if path in FIRST_WINDOW_PATHS:
        return await call_next(request)

    # Check auth
    if not _verify_auth(request):
        if path.startswith("/api/"):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        # Redirect to login
        next_url = request.url.path + ("?" + request.url.query if request.url.query else "")
        return RedirectResponse(f"/login?next={next_url}", status_code=302)

    # Listen-only role: REST is read-only. Every mutating API (setup, restart,
    # recordings delete, memory-channel POST, support bundle/upload) is a
    # non-GET under /api/ — refuse those; logout stays reachable.
    if (
        _is_listen_request(request)
        and path.startswith("/api/")
        and request.method not in ("GET", "HEAD", "OPTIONS")
        and path != "/api/auth/logout"
    ):
        return JSONResponse(
            {"error": LISTEN_ONLY_MESSAGE}, status_code=403,
        )

    return await call_next(request)


# ── Login Routes ────────────────────────────────────────────────────

def _first_run_password_banner(request: Request) -> str:
    """One-time banner showing the auto-generated login password.

    Rendered only while MRRC_AUTO_PASSWORD=1 AND the connecting client is a
    loopback address — never leak the auto password to LAN clients when the
    server binds :: (dual-stack).
    """
    if os.environ.get("MRRC_AUTO_PASSWORD") != "1":
        return ""
    client = (request.client.host if request.client else "") or ""
    if client not in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"):
        return ""
    safe = html.escape(str(WEB_PASSWORD))
    return (
        '<div style="background:#2d2410;color:#fbbf24;border:1px solid #b45309;'
        'border-radius:8px;padding:12px;margin:0 0 12px;font-size:14px;'
        'text-align:center;">'
        f'🔑 首次运行已自动生成密码：<b>{safe}</b><br>'
        '<span style="color:#9ca3af;font-size:12px;">'
        '如需修改：菜单栏 MRRC Modern → Edit Configuration…</span></div>'
    )


def _setup_status() -> dict:
    """Status payload shown on the first-run welcome banner after login."""
    return {
        "first_run_done": os.environ.get("MRRC_FIRST_RUN_DONE", "") == "1",
        "auto_password": os.environ.get("MRRC_AUTO_PASSWORD", "") == "1",
        "radio_model": RADIO_MODEL,
        "serial_port": SERIAL_PORT,
        "audio_rx_device": AUDIO_RX_DEVICE,
        "audio_tx_device": AUDIO_TX_DEVICE,
    }


@app.get("/api/setup", include_in_schema=False)
async def api_setup(request: Request):
    """First-run status for the welcome banner (requires login)."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(_setup_status())


RESTART_EXIT_CODE = 42  # launcher re-reads config and restarts on this exit


def _config_file_path() -> Path:
    """Where the launcher keeps the user env file (MRRC_CONFIG_FILE)."""
    env_path = os.environ.get("MRRC_CONFIG_FILE")
    if env_path:
        return Path(env_path)
    return MEM_FILE.parent / "mrrc_modern.env"


def _serial_port_problem() -> str:
    """One sentence when no serial port is configured; "" when one is.

    The field symptom had no sentence at all: `Opening serial port  at 38400 baud`
    and `Failed to connect to : [Errno 2] could not open port : ...: ''` — the port
    name is empty, so it reads like a truncated log line rather than "this box has
    no port" (bundle 20261026-194408-16d0). Naming the variable and the file is what
    turns it into something an operator — or a support reply — can act on.
    """
    if str(SERIAL_PORT or "").strip():
        return ""
    return ("MRRC_SERIAL_PORT is empty — no serial port is configured, so the radio "
            f"cannot be controlled. Set it in {_config_file_path()} (pick the port that "
            "appears when the radio is plugged in) and restart.")


def _as_int(value, default: int = 0) -> int:
    """Coerce an untyped PyAudio field to int (never raises)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _host_api_name(pa, index) -> str:
    """Best-effort host API name for a device.

    Field-relevant: the FT-710/IC-7300 codec enumerates several times (MME,
    WASAPI, WDM-KS), only some of which open successfully — a device-name
    collision across host APIs was a real 2026-09-10 field failure.
    """
    try:
        return str(pa.get_host_api_info_by_index(index).get("name", ""))
    except Exception:
        return ""


def _list_audio_devices() -> dict:
    rx, tx = [], []
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        try:
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                name = str(info.get("name", ""))
                if _as_int(info.get("maxInputChannels")) > 0:
                    rx.append({"index": i, "name": name,
                               "channels": _as_int(info.get("maxInputChannels")),
                               "host_api": _host_api_name(pa, info.get("hostApi", -1))})
                if _as_int(info.get("maxOutputChannels")) > 0:
                    tx.append({"index": i, "name": name,
                               "channels": _as_int(info.get("maxOutputChannels")),
                               "host_api": _host_api_name(pa, info.get("hostApi", -1))})
        finally:
            pa.terminate()
    except Exception:
        pass
    return {"rx": rx, "tx": tx}


def _list_devices() -> dict:
    serial_ports = []
    try:
        import serial.tools.list_ports
        import sys
        for p in serial.tools.list_ports.comports():
            dev = getattr(p, "device", "")
            if sys.platform == "darwin":
                _bogus = ("/dev/cu.Bluetooth", "/dev/cu.IRComm", "/dev/cu.debug-console")
                if dev.startswith("/dev/cu.") and not dev.startswith(_bogus):
                    serial_ports.append({
                        "device": dev,
                        "description": getattr(p, "description", "") or dev,
                    })
            else:
                # Windows (COM*) / Linux: include any real serial device
                if dev:
                    serial_ports.append({
                        "device": dev,
                        "description": getattr(p, "description", "") or dev,
                    })
    except Exception:
        pass
    audio = _list_audio_devices()
    return {"serial_ports": serial_ports, "audio_rx": audio["rx"], "audio_tx": audio["tx"]}


def _schedule_restart() -> None:
    """Exit with RESTART_EXIT_CODE shortly so the launcher restarts the server.

    ``os._exit`` skips the lifespan shutdown, and with it the Force-RX safety
    write — a restart requested while the radio was keyed used to leave the
    carrier up with no process left to send TX0.  Release PTT on the running
    loop first (bounded), then exit.
    """
    try:
        loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    def _exit() -> None:
        c = cat
        r = radio
        if (loop is not None and c is not None and r is not None
                and c.connected and r.is_transmitting):
            try:
                fut = asyncio.run_coroutine_threadsafe(c.set_ptt(False), loop)
                fut.result(timeout=1.5)
                logger.warning("Restart: released PTT before exit")
            except Exception as e:
                logger.error("Restart: could not release PTT before exit: %s", e)
        os._exit(RESTART_EXIT_CODE)

    threading.Timer(1.2, _exit).start()


@app.get("/api/devices", include_in_schema=False)
async def api_devices(request: Request):
    """Detected serial + audio devices for the connection-settings dialog."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(_list_devices())


@app.post("/api/setup", include_in_schema=False)
async def api_setup_save(request: Request):
    """Persist radio/serial/audio/password and restart to apply."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    radio_model = str(body.get("radio_model", "")).strip().lower()
    serial_port = str(body.get("serial_port", "")).strip()
    audio_rx = str(body.get("audio_rx_device", "")).strip()
    audio_tx = str(body.get("audio_tx_device", "")).strip()
    password = str(body.get("password", "")).strip()

    if radio_model not in known_models():
        # Registry-derived: a backend added to backends/__init__.py is
        # immediately selectable here instead of being rejected by a
        # hardcoded tuple (which is how ic705/ic7610/ic7760 would have
        # been unreachable from the connection dialog).
        return JSONResponse({"error": "invalid radio_model"}, status_code=400)
    if not serial_port:
        return JSONResponse({"error": "serial_port required"}, status_code=400)
    if password and len(password) < 8:
        return JSONResponse({"error": "password too short (min 8)"}, status_code=400)

    updates = {
        "MRRC_RADIO_MODEL": radio_model,
        "MRRC_SERIAL_PORT": serial_port,
        # Model↔baud linkage (V2.33): the dialog has no baud field, and a
        # stale MRRC_BAUD_RATE from the legacy installer template (38400 —
        # the FT-710 value) silently overrode the model default and broke
        # the IC-7300 CI-V scope stream (requires 115200). Always align the
        # stored baud with the selected model; anyone needing a non-default
        # baud edits the env file directly (the radio's CI-V baud menu must
        # match — Auto works for both).
        "MRRC_BAUD_RATE": str(default_baud_for(radio_model)),
        "MRRC_AUDIO_RX_DEVICE": audio_rx,
        "MRRC_AUDIO_TX_DEVICE": audio_tx,
        "MRRC_FIRST_RUN_DONE": "1",
        "MRRC_PORT_CONFIRMED": "1",
    }
    logger.info(
        "Saving connection settings: model=%s port=%s baud=%s "
        "(radio menu CI-V baud should be Auto or match)",
        radio_model, serial_port, updates["MRRC_BAUD_RATE"],
    )
    if password:
        updates["MRRC_WEB_PASSWORD"] = password
        updates["MRRC_AUTO_PASSWORD"] = ""  # user-managed now; hide the banner
    try:
        first_run.update_env_file(_config_file_path(), updates)
    except OSError as exc:
        logger.error("Failed to write config: %s", exc)
        return JSONResponse({"error": "write failed"}, status_code=500)
    # Read back what the *next* start will actually see. Writing is not saving: the
    # 2026-10-26 field file carried `MRRC_SERIAL_PORT` twice (the second one empty) and
    # the writer only rewrote the first, so six saves of /dev/ttyUSB0 from this dialog
    # each answered `{"ok": true}` while the server kept starting with an empty port
    # (bundle 20261026-194408-16d0, IC-7300 not controllable, 26 days).
    effective = env_store.load(_config_file_path())
    misapplied = {key: effective.get(key, "")
                  for key in ("MRRC_SERIAL_PORT", "MRRC_BAUD_RATE", "MRRC_RADIO_MODEL")
                  if effective.get(key, "") != updates[key]}
    if misapplied:
        logger.error("Connection settings did not persist: wanted %s, file has %s",
                     {k: updates[k] for k in misapplied}, misapplied)
        return JSONResponse(
            {"error": "settings did not persist (the env file has a conflicting entry)",
             "effective": misapplied},
            status_code=500,
        )
    _schedule_restart()
    return JSONResponse({"ok": True})


@app.post("/api/restart", include_in_schema=False)
async def api_restart(request: Request):
    """Restart the server without changing configuration."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    _schedule_restart()
    return JSONResponse({"ok": True})


@app.get("/login", include_in_schema=False)
async def login_page(request: Request):
    """Serve the login page."""
    login_html = STATIC_DIR / "login.html"
    if login_html.exists():
        html_body = login_html.read_text()
    else:
        html_body = """
    <!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport"
    content="width=device-width,initial-scale=1"><title>MRRC Modern Login</title>
    <style>body{font-family:-apple-system,sans-serif;background:#1a1a1a;color:#eee;
    display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;}
    form{background:#2a2a2a;padding:2rem;border-radius:12px;width:300px}
    h1{text-align:center;color:#f59e0b;margin:0 0 1rem}
    input{width:100%;padding:12px;margin:8px 0;border:1px solid #444;border-radius:8px;
    background:#333;color:#fff;font-size:16px;box-sizing:border-box}
    button{width:100%;padding:12px;margin-top:12px;background:#f59e0b;color:#000;
    border:none;border-radius:8px;font-size:16px;font-weight:bold;cursor:pointer}
    .error{color:#ff4444;margin-top:8px;text-align:center}</style></head>
    <body><form id="loginForm"><h1>MRRC Modern</h1>
    <input type="password" id="password" placeholder="Password" autofocus required>
    <button type="submit">Login</button><div class="error" id="error"></div></form>
    <script>
    document.getElementById('loginForm').onsubmit=async function(e){e.preventDefault();
    var p=document.getElementById('password').value;try{var r=await fetch('api/auth/login',
    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:p})});
    if(r.ok){var d=await r.json();var n=(d&&d.role==='listen')?'listen':
    (new URLSearchParams(window.location.search).get('next')||'/');
    window.location.replace(n);}else{document.getElementById('error').textContent='Invalid password';}
    }catch(err){document.getElementById('error').textContent='Connection error';}};
    </script></body></html>
    """
    banner = _first_run_password_banner(request)
    if banner and html_body:
        html_body = html_body.replace(
            "<h1>MRRC Modern</h1>", "<h1>MRRC Modern</h1>" + banner, 1
        )
    return HTMLResponse(html_body, status_code=200)


@app.post("/api/auth/login")
async def api_login(request: Request):
    """Authenticate with password.  Sets auth cookie on success."""
    # Check rate limit
    client_ip = request.client.host if request.client else "unknown"
    if not _check_login_rate_limit(client_ip):
        logger.warning("Rate limit exceeded for login from IP: %s", client_ip)
        return JSONResponse({"error": "Too many login attempts. Please try again later."}, status_code=429)

    try:
        body = await request.json()
        password = body.get("password", "")
    except Exception:
        password = ""

    # The admin password wins when both are configured identically — an
    # operator who set them equal expects full control, not a restricted UI.
    if _password_matches(password):
        role = "admin"
    elif _listen_password_matches(password):
        role = "listen"
    else:
        return JSONResponse({"error": "Invalid password"}, status_code=401)

    # Password strength validation (only on first login)
    if len(password) < 12:
        logger.warning("Weak password detected! Use at least 12 characters with mixed case, numbers, and symbols.")

    token = _make_auth_token()
    _add_auth_token(token, listen=role == "listen")
    if role == "listen":
        logger.info("Listen-only session logged in from %s", client_ip)

    response = JSONResponse({"ok": True, "token": token, "role": role})
    response.set_cookie(
        AUTH_COOKIE, token,
        max_age=30 * 24 * 3600,  # 30 days
        httponly=False,          # JS needs to read it for WebSocket
        samesite="lax",
    )
    return response


@app.post("/api/auth/logout")
async def api_logout(request: Request):
    """Invalidate the session token."""
    token = request.cookies.get(AUTH_COOKIE)
    if token:
        _drop_auth_token(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(AUTH_COOKIE)
    return response


@app.get("/api/auth/check")
async def api_auth_check(request: Request):
    """Check if the current session is valid."""
    if _verify_auth(request):
        return JSONResponse({"authenticated": True})
    return JSONResponse({"authenticated": False}, status_code=401)


@app.get("/listen", include_in_schema=False)
async def listen_page(request: Request):
    """Serve the listen-only interface (auth enforced by the middleware).

    Reachable with any valid session; sessions authenticated with
    MRRC_LISTEN_PASSWORD are redirected here by the login page and get a
    server-enforced frequency/mode-only role.
    """
    page = STATIC_DIR / "listen.html"
    if page.exists():
        return _html_page(page)
    return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)


# ── Status API ──────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    """Return full radio state as JSON."""
    return JSONResponse(radio.to_dict())


# ── Health Check API ──────────────────────────────────────────────

@app.get("/api/health")
async def api_health():
    """Health check endpoint for monitoring/proxy setups."""
    import time as _time
    health_status = {
        "status": "healthy",
        "radio_connected": cat.connected if cat else False,
        "audio_available": audio is not None and audio._rx_running if audio else False,
        "scope_connected": scope.connected if scope else False,
        "uptime_seconds": round(_time.time() - STARTUP_TIME, 1),
    }
    
    # Check if any critical components are down
    if not health_status["radio_connected"]:
        health_status["status"] = "degraded"
        health_status["message"] = "Radio not connected"
    
    return JSONResponse(health_status)


@app.get("/api/session_metrics")
async def api_session_metrics():
    """Measured remote-session concurrency and uplink (session_metrics.py).

    The hub's RX fan-out decision needs the real listener-concurrency
    distribution rather than an assumption (mrrc_hub AD-H12 / open issue I-H1).
    Counts and bytes only — no tokens, addresses, frequencies or audio.
    """
    return JSONResponse(metrics.snapshot())


# ── Box-level audio gains (AD-032) ──────────────────────────────────

@app.get("/api/audio/levels", include_in_schema=False)
async def api_audio_levels_get():
    """The box's TX mic / RX levels, their ranges, and clipping counters.

    Readable by every logged-in role (the listen page may display it); writing
    is refused for listeners by the middleware's non-GET rule.
    """
    return JSONResponse(_audio_levels_state())


@app.post("/api/audio/levels", include_in_schema=False)
async def api_audio_levels_set(request: Request):
    """Set one or both box-level gains; refuse anything out of range.

    A partial update is the normal case: each slider sends only its own key.
    The response is the full new state (same shape as GET) so the UI writes
    back exactly what the box accepted instead of what it hoped for.
    """
    global _audio_levels
    try:
        body = await request.json()
    except Exception:
        body = None
    levels, problem = audio_levels.parse_update(body, _audio_levels)
    if levels is None:
        return JSONResponse({"error": problem}, status_code=400)
    _audio_levels = levels
    saved = _audio_levels_store.save(levels)
    await _broadcast_audio_levels()
    logger.info("Audio levels: TX mic %+.1f dB, RX %+.1f dB (%s; by %s)",
                levels.tx_mic_db, levels.rx_db,
                "saved" if saved else "memory only",
                _role_for_token(_token_from_request(request) or ""))
    return JSONResponse(_audio_levels_state())


# ── CQ asset API (2026-10-26) ───────────────────────────────────────
# The operator's own CQ recording.  The file lives in the runtime data dir so a
# box update cannot replace it; the shipped WAV stays the factory default, and
# deleting the custom take is what "恢复出厂 CQ" means.


@app.get("/api/cq", include_in_schema=False)
async def api_cq_state():
    """The effective CQ: source, length, peak, and whether a custom take exists."""
    return JSONResponse(_cq_state_payload())


@app.get("/api/cq/audio", include_in_schema=False)
async def api_cq_audio():
    """The WAV the CQ key would play — the settings page's 试听 source."""
    path = _cq_effective_path()
    if not path or not Path(path).is_file():
        return JSONResponse({"error": "没有可播放的 CQ 素材"}, status_code=404)
    return FileResponse(str(path), media_type="audio/wav", filename="cq.wav")


@app.post("/api/cq", include_in_schema=False)
async def api_cq_upload(request: Request):
    """Replace the CQ asset with a recording; refused while TX or a call runs.

    The body *is* the WAV (the browser encodes it), so there is no multipart
    parser in the path and no second format to convert — the server has no
    decoder, and adding one would mean shipping ffmpeg for one feature.
    """
    refusal = _cq_write_refusal()
    if refusal:
        return JSONResponse({"error": refusal}, status_code=409)
    blob = await request.body()
    try:
        stored, info = cq_asset.normalize_wav(blob)
    except cq_asset.CQAssetError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        cq_asset.write_atomic(CQ_CUSTOM_PATH, stored)
    except OSError as e:                     # disk full, read-only data dir
        logger.error("CQ asset write failed: %s", e)
        return JSONResponse({"error": f"保存失败：{e}"}, status_code=500)
    loaded = _reload_cq_asset()
    await _broadcast_cq_state()
    logger.info("CQ asset replaced: %.1f s, peak %.1f → %.1f dBFS (%+.1f dB); loaded=%s",
                info["duration_s"], info["recorded_peak_dbfs"],
                info["stored_peak_dbfs"], info["applied_gain_db"], loaded)
    return JSONResponse({"ok": loaded, "cq": _cq_state_payload(), "upload": info})


@app.delete("/api/cq", include_in_schema=False)
async def api_cq_reset():
    """Drop the custom take and fall back to the shipped CQ."""
    refusal = _cq_write_refusal()
    if refusal:
        return JSONResponse({"error": refusal}, status_code=409)
    removed = False
    try:
        if CQ_CUSTOM_PATH.is_file():
            CQ_CUSTOM_PATH.unlink()
            removed = True
    except OSError as e:
        logger.error("CQ asset delete failed: %s", e)
        return JSONResponse({"error": f"删除失败：{e}"}, status_code=500)
    _reload_cq_asset()
    await _broadcast_cq_state()
    logger.info("CQ asset reset to the shipped one (removed=%s)", removed)
    return JSONResponse({"ok": True, "removed": removed, "cq": _cq_state_payload()})


# ── Memory Channels API ─────────────────────────────────────────────

def _recording_path(name: str) -> Optional[Path]:
    """Resolve a recording name inside RECORDINGS_DIR, or None.

    The name must be one this server would generate (recorder.parse_recording_name)
    and the resolved path must stay inside the recordings directory — the
    I8 lesson applied to the recordings API.
    """
    if parse_recording_name(name) is None:
        return None
    try:
        root = RECORDINGS_DIR.resolve()
        resolved = (RECORDINGS_DIR / name).resolve()
        if not resolved.is_relative_to(root):
            return None
    except (OSError, ValueError):
        return None
    return resolved


def _recordings_payload() -> dict:
    """Recording list with the index merged in, plus the usage total."""
    rows = list_recordings(RECORDINGS_DIR, load_index(RECORDINGS_INDEX),
                           RECORDINGS_BITRATE)
    active = _rec_session.status()["name"] if _rec_session.active else None
    for row in rows:
        row["recording"] = row["name"] == active
    return {"recordings": rows, "count": len(rows),
            "total_bytes": sum(row["bytes"] for row in rows)}


async def _delete_recording(name: str) -> bool:
    """Delete one recording; False when unknown or currently recording."""
    path = _recording_path(name)
    if path is None or not path.is_file():
        return False
    if _rec_session.active and _rec_session.status()["name"] == name:
        return False
    try:
        path.unlink()
    except OSError as e:
        logger.warning("Cannot delete recording %s: %s", name, e)
        return False
    index = load_index(RECORDINGS_INDEX)
    if index.pop(name, None) is not None:
        save_index(RECORDINGS_INDEX, index)
    logger.info("Recording deleted: %s", name)
    await _broadcast_recording_state()
    return True


def _recording_response(name: str):
    """Range-capable FileResponse for one recording (None when invalid)."""
    path = _recording_path(name)
    if path is None or not path.is_file():
        return None
    return FileResponse(path, media_type="audio/mpeg", filename=name)


@app.get("/api/recordings", include_in_schema=False)
async def api_recordings():
    """List recordings (authentication is enforced by auth_middleware)."""
    return JSONResponse(_recordings_payload())


@app.get("/api/recordings/{name}", include_in_schema=False)
async def api_recording_file(name: str):
    """Stream one recording; Starlette's FileResponse handles Range/seek."""
    response = _recording_response(name)
    if response is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return response


@app.delete("/api/recordings/{name}", include_in_schema=False)
async def api_recording_delete(name: str):
    """Delete one recording (409 while it is the active recording)."""
    if _rec_session.active and _rec_session.status()["name"] == name:
        return JSONResponse({"error": "recording in progress"}, status_code=409)
    if not await _delete_recording(name):
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


@app.get("/api/mem_channels")
async def api_get_mem_channels():
    """Return memory channels."""
    return JSONResponse({"channels": _load_mem_channels()})


# ── Support bundle (spec 2026-09-17): server-side diagnostics + report loop ──
# The bundle is built here (not in the browser) because only the server can see
# the logs, the real device table and the radio state.  One build at a time: a
# stuck build must not pile up bundles.
_support_build_lock = threading.Lock()


def _support_env_snapshot() -> dict:
    """Live state a maintainer needs and cannot infer from a log file.

    Every block is guarded: a diagnostics helper that throws would turn a
    support request into "bundle build failed" (spec §11).
    """
    snapshot = support_bundle.collect_env_snapshot(
        support_bundle.detect_version(_runtime_dir(), _resource_dir()))
    try:
        caps = backend.capabilities if backend is not None else None
        snapshot["radio"] = {
            "model": getattr(caps, "model_name", None),
            "display_name": getattr(caps, "display_name", None),
            "scope_type": getattr(caps, "scope_type", None),
            "tx_gated": getattr(caps, "tx_gated", None),
            "serial_connected": bool(radio.serial_connected),
        }
    except Exception as e:
        snapshot["radio"] = {"error": str(e)}
    try:
        snapshot["audio"] = {
            "devices": _list_audio_devices(),
            "device_rates": audio.device_rates if audio is not None else None,
            "tx_stats": audio.tx_stats() if audio is not None else None,
            "configured_rx": AUDIO_RX_DEVICE,
            "configured_tx": AUDIO_TX_DEVICE,
        }
    except Exception as e:
        snapshot["audio"] = {"error": str(e)}
    try:
        snapshot["recording"] = {"dir": str(RECORDINGS_DIR),
                                 "active": _rec_writer_task is not None}
    except Exception as e:
        snapshot["recording"] = {"error": str(e)}
    snapshot["log_dir"] = str(LOG_DIR)
    snapshot["log_file"] = str(SUPPORT_LOG_FILE) if SUPPORT_LOG_FILE else ""
    snapshot["config_file"] = str(_config_file_path())
    snapshot["support_url"] = SUPPORT_URL
    return snapshot


def _support_collect(problem: str, contact: str, client) -> dict:
    """Build one bundle from live paths (single-flight is the caller's job)."""
    try:
        config_text = _config_file_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        config_text = "; 配置文件不可读（可能尚未生成）\n"
    log_files = support_bundle.resolve_log_files(LOG_DIR, LOG_DIR.parent, _runtime_dir())
    version = support_bundle.detect_version(_runtime_dir(), _resource_dir())
    return support_bundle.build_bundle(
        SUPPORT_OUT_DIR, problem=problem, contact=contact,
        env=_support_env_snapshot(), log_files=log_files, config_text=config_text,
        extra_files={"diagnostics/client.json": json.dumps(client or {}, ensure_ascii=False,
                                                         indent=2)},
        manifest_extra={"version": version},
    )


def _support_upload(zip_file) -> str:
    """POST api/create then PUT the zip; return the receiver's id (stdlib only)."""
    import urllib.request

    base = SUPPORT_URL.rstrip("/")

    def _exchange(request) -> dict:
        """Send one request and parse its JSON body (never a raw traceback)."""
        with net_tls.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError as e:                     # a proxy/error page, not our API
            raise RuntimeError(f"接收端未返回 JSON（{e}）：{raw[:200]}") from e

    payload = json.dumps({
        "problem": "", "contact": "",
        "version": support_bundle.detect_version(_runtime_dir(), _resource_dir()),
    }).encode("utf-8")
    created = _exchange(urllib.request.Request(
        f"{base}/api/create", data=payload,
        headers={"Content-Type": "application/json"}))
    remote_id = created.get("id", "")
    if not created.get("ok") or not remote_id:
        raise RuntimeError(created.get("reason") or "创建记录失败")
    try:
        with open(zip_file, "rb") as fh:
            body_bytes = fh.read()
    except OSError as e:
        raise RuntimeError(f"诊断包不可读：{e}") from e
    result = _exchange(urllib.request.Request(
        f"{base}/api/{remote_id}/bundle", data=body_bytes, method="PUT",
        headers={"Content-Type": "application/zip"}))
    if not result.get("ok"):
        raise RuntimeError(result.get("reason") or "上传失败")
    return remote_id


def _support_export_dir() -> Path:
    """Where 只保存到本地 puts the zip (the user data dir, next to the logs)."""
    target = LOG_DIR.parent / "support-export"
    target.mkdir(parents=True, exist_ok=True)
    return target


# ── Update check (spec 2026-09-17-upgrade-channel, slice 1) ─────────
# Read-only: reports whether a newer release exists.  Nothing is downloaded and
# nothing is written here — the download/install half is slice 2, and it must not
# exist half-built (a half-built updater modifies the machine it runs on).
@app.get("/api/update/check", include_in_schema=False)
async def api_update_check(request: Request):
    """Compare the running version with the published manifest."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    current = support_bundle.detect_version(_runtime_dir(), _resource_dir())

    def _check() -> dict:
        # 盒子上「升级」指的是这颗源码树（`box` 段），桌面上指的是安装包
        # （`installer` 段）。两者都返回同一套字段，页面才不用分叉。
        try:
            manifest = upgrade_core.fetch_manifest(UPDATE_MANIFEST_URL)
        except Exception:                                          # noqa: BLE001
            # 拿不到清单就交给 check_quietly：它把失败翻译成一句 reason，
            # 而不是让页面吃一个 500（2026-09-17 的教训）。
            reply = upgrade_core.check_quietly(current, UPDATE_MANIFEST_URL)
        else:
            reply = (upgrade_core.check_box(current, manifest)
                     if manifest.get("box") else upgrade_core.check(current, manifest))
        # 修复包编号（设计 2026-10-22）：只显示、不参与「谁比谁新」。清单里没写就是空串，
        # 页面据此不留尾巴；本机那个读 `fixpack.txt` —— 老盒子没有这个文件，也是空串。
        reply = dict(reply)
        reply.setdefault("latest_fixpack", "")
        reply["current_fixpack"] = box_update.read_fixpack(_runtime_dir())
        return reply

    result = await asyncio.to_thread(_check)
    return JSONResponse(result)


@app.post("/api/update/apply", include_in_schema=False)
async def api_update_apply(request: Request):
    """盒子自更新：下载 → 校验 → 覆盖 → 重启（需要时自动回滚）。

    三条闸门在**动手之前**：正在发射（PTT / TUNE / CQ / FT8 发波）、磁盘不够、
    已经有一次在跑。它们都是「不能开始」的理由，不是「开始了再失败」的理由 ——
    这条链路的终点是重启服务，把发射掐断在一个客户看不见的时刻。
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not _update_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "reason": "已经有一次升级在进行中",
                             "state": box_update.state()}, status_code=409)
    try:
        live = box_update.state()
        if live["state"] not in (box_update.STATE_IDLE, box_update.STATE_DONE,
                                 box_update.STATE_FAILED):
            return JSONResponse({"ok": False, "reason": "已经有一次升级在进行中",
                                 "state": live}, status_code=409)
        current = support_bundle.detect_version(_runtime_dir(), _resource_dir())
        manifest = await asyncio.to_thread(upgrade_core.fetch_manifest,
                                           UPDATE_MANIFEST_URL)
        plan = upgrade_core.check_box(current, manifest)
        if not plan.get("available"):
            return JSONResponse({"ok": False, "reason": "已经是最新版本",
                                 "state": box_update.state()}, status_code=400)
        problem = _update_refusal()
        if problem:
            return JSONResponse({"ok": False, "reason": problem}, status_code=409)
        box_update.reset()
        task = asyncio.create_task(asyncio.to_thread(
            box_update.apply, plan, _runtime_dir(),
            runner=_schedule_restart))
        # `create_task` 的异常没人 await 就等于不存在：一次 apply 抛出去，页面会
        # 永远停在"覆盖代码"这一格，而日志里一个字都没有（2026-10-27 现场丢了
        # 全部痕迹，只能靠磁盘上的 package-new.tar.gz 推断）。
        task.add_done_callback(_log_update_task_failure)
        logger.warning("update: applying %s from %s", plan.get("latest"), plan.get("url"))
        return JSONResponse({"ok": True, "applying": plan.get("latest"),
                             "state": box_update.state()})
    except Exception as exc:                                       # noqa: BLE001
        logger.warning("update: cannot start: %s", exc)
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    finally:
        _update_lock.release()


def _log_update_task_failure(task: "asyncio.Task") -> None:
    """An update task that raised must say so — in the log and on the page."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    logger.error("update: the apply task died: %r", exc)
    box_update.failed(f"升级中断：{type(exc).__name__}: {exc}")


@app.get("/api/update/state", include_in_schema=False)
async def api_update_state(request: Request):
    """What the update card polls while it works."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(box_update.state())


def _update_refusal() -> str:
    """Why this box must not start an update right now ("" = it may)."""
    if radio is not None and getattr(radio, "tx_status", 0):
        return "电台正在发射 —— 发射时不能升级（重启会掐断这一波）"
    if _cq_player is not None and _cq_player.is_calling:
        return "CQ 呼叫正在进行 —— 等它结束再升级"
    # FT8 发波不需要单独的判断：它和 PTT / TUNE / CQ 一样会把 `radio.tx_status`
    # 顶起来，上面那条已经拦住了。而「引擎在后台听」不能拦 —— 它是常开的。
    import shutil

    try:
        free_mb = shutil.disk_usage(str(_runtime_dir())).free // (1024 * 1024)
    except OSError:
        free_mb = 0
    if free_mb < box_update.MIN_FREE_MB:
        return (f"磁盘剩余不足（{free_mb} MB，至少要 {box_update.MIN_FREE_MB} MB）"
                "—— 先删一些录音")
    return ""


@app.post("/api/support/bundle", include_in_schema=False)
async def api_support_bundle(request: Request):
    """Build a redacted diagnostics bundle (spec §7)."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not _support_build_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "reason": "build in progress"})
    try:
        result = await asyncio.to_thread(
            _support_collect, str(body.get("problem", ""))[:4000],
            str(body.get("contact", ""))[:200], body.get("client") or {})
        logger.info("Support bundle built: %s (%d KB, %d redactions)",
                    result["id"], result["size"] // 1024, result["redactions"])
        return JSONResponse(dict(result, ok=True))
    except Exception as e:
        logger.warning("Support bundle build failed: %s", e)
        return JSONResponse({"ok": False, "reason": str(e)})
    finally:
        _support_build_lock.release()


@app.post("/api/support/upload", include_in_schema=False)
async def api_support_upload(request: Request):
    """Upload a built bundle; never lose it on failure (spec §9/§11)."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    path = support_bundle.bundle_path(SUPPORT_OUT_DIR, str(body.get("id", "")))
    if not path.is_file():
        return JSONResponse({"ok": False, "reason": "unknown bundle", "localPath": ""})
    try:
        remote_id = await asyncio.to_thread(_support_upload, path)
    except Exception as e:
        logger.warning("Support bundle upload failed: %s", e)
        return JSONResponse({"ok": False, "reason": str(e), "localPath": str(path)})
    logger.info("Support bundle uploaded: %s -> %s", path.name, remote_id)
    return JSONResponse({"ok": True, "remoteId": remote_id, "size": path.stat().st_size})


@app.post("/api/support/save", include_in_schema=False)
async def api_support_save(request: Request):
    """Copy the bundle where the operator can send it by hand (mail/WeChat)."""
    import shutil

    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    path = support_bundle.bundle_path(SUPPORT_OUT_DIR, str(body.get("id", "")))
    if not path.is_file():
        return JSONResponse({"ok": False, "reason": "unknown bundle"})
    try:
        target = _support_export_dir() / path.name
        shutil.copy2(path, target)
    except OSError as e:
        return JSONResponse({"ok": False, "reason": str(e)})
    return JSONResponse({"ok": True, "path": str(target)})


@app.post("/api/support/download", include_in_schema=False)
async def api_support_download(request: Request):
    """Send a built bundle to the caller as a zip.

    Why this exists: upload is the only way a bundle leaves the machine, and it
    needs the box to reach the internet. When the *network* is what broke — the
    reported case is a box that answers on the LAN while its public entry and
    the support upload both fail — the bundle is built, the page says "saved",
    and nobody can ever get the file out (2026-10-24, BG7SB). This is that exit.

    POST rather than GET on purpose: ``auth_middleware`` already refuses every
    non-GET write for a listen-only session, and a diagnostics bundle (logs,
    redacted config) must not be reachable by the listen role. The body carries
    ``{"id": …}`` like the save endpoint, so one request shape serves both.
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    path = support_bundle.bundle_path(SUPPORT_OUT_DIR, str(body.get("id", "")))
    if not path.is_file():
        return JSONResponse({"ok": False, "reason": "unknown bundle"}, status_code=404)
    logger.info("Support bundle downloaded: %s (%d KB)",
                path.name, path.stat().st_size // 1024)
    return FileResponse(str(path), media_type="application/zip", filename=path.name)


@app.post("/api/mem_channels")
async def api_post_mem_channels(request: Request):
    """Save memory channels."""
    try:
        body = await request.json()
        channels = body.get("channels", [])
        _save_mem_channels(channels)
        await _broadcast_mem_channels()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ── WebSocket Endpoint ──────────────────────────────────────────────

def _full_state_message(data: dict, channels: list) -> dict:
    """Build the fullState payload pushed to every new /WSradio client.

    Band/mode/filter tables and capabilities are sourced from the active
    backend so a different radio model swaps them without server changes.
    """
    return {
        "type": "fullState",
        "data": data,
        "bands": backend.bands if backend else BANDS,
        "modes": backend.ui_modes if backend else UI_MODES,
        "work_mode": _work_mode_value(),
        "recording": _recording_state_payload(),
        "cq": _cq_state_payload(),
        # Box-level audio gains (AD-032): a fresh page must render the sliders at
        # the box's values, not at 0 dB, or the first drag would fight it.
        "audioLevels": _audio_levels_state(),
        "radioModel": backend.capabilities.model_name if backend else RADIO_MODEL,
        "radioDisplayName": (backend.capabilities.display_name
                             if backend else "Yaesu FT-710"),
        "capabilities": backend.capabilities.to_dict() if backend else {},
        # The installed version, for the menu footer.  Same detector the
        # support bundle and the update check use (version.txt in a build
        # product, then the .iss, then the CHANGELOG) so the number shown in
        # the UI is the number those two report — a hardcoded literal here
        # would be a fifth copy to keep in sync.
        "version": support_bundle.detect_version(_runtime_dir(), _resource_dir()),
        # 盒子线的修复包编号（FP1、FP2…，2026-10-22 设计 §5）。只用于显示，页脚把两段拼成
        # `v1.28.5 · FP1`；桌面版没有这个文件，读出空串，页脚就不留尾巴。
        "fixpack": box_update.read_fixpack(_runtime_dir()),
        "memChannels": channels,
        # Server-authoritative filter tables so the frontend stops
        # hardcoding its own (drifting) copies.
        "filterTables": (backend.filter_tables() if backend else {
            "voice": FILTER_WIDTHS_VOICE,
            "narrow": FILTER_WIDTHS_NARROW,
            "narrowModes": sorted(NARROW_MODES),
        }),
        # Optional ATR1000 tuner — the frontend module stays fully
        # inert unless this is true.
        "atr1000Enabled": atr is not None,
    }


@app.websocket("/WSradio")
async def ws_radio(ws: WebSocket):
    """Main control WebSocket.  Handles all real-time radio state and commands."""
    global _ptt_key_ws
    # Check auth via query param (browser WebSocket doesn't support custom headers)
    token = _token_from_ws_handshake(ws)
    if not token or token not in _auth_tokens:
        await ws.close(code=4001, reason="Unauthorized")
        return
    role = _role_for_token(token)

    await ws.accept()
    ctrl_clients.add(ws)
    _ws_tokens[ws] = token
    metrics.open(role, "control", token)
    logger.info("WS client connected (%d total)", len(ctrl_clients))
    if scheduler:
        scheduler.set_active(len(ctrl_clients) > 0)
    await _broadcast_client_count()

    # Push full initial state
    try:
        channels = _load_mem_channels()
        _full_state_data = radio.to_dict()
        # Diagnostic (2026-08-15): user reported spectrum "电台未连接" while
        # radio was fine; log the serial flag at every fullState push so a
        # repro produces the exact value the client received.
        logger.info(
            "WS fullState push: serial_connected=%s clients=%d",
            _full_state_data.get("serial_connected"),
            len(ctrl_clients),
        )
        logger.debug(
            "WS fullState payload: %s",
            json.dumps(_full_state_data)[:400],
        )
        await ws.send_text(json.dumps(_full_state_message(_full_state_data, channels)))
    except Exception:
        ctrl_clients.discard(ws)
        metrics.close(role, "control", token)
        return

    try:
        while True:
            msg_str = await ws.receive_text()
            # One bad command must not tear down the control channel: an
            # exception here used to close the socket with 1006, and the
            # client then reconnect-looped (field report 2026-09-12).
            try:
                await _handle_ws_message(ws, msg_str)
            except Exception as e:
                logger.warning("Command failed (%d bytes): %s",
                               len(msg_str), e)
                logger.debug("Command traceback", exc_info=True)
                try:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "message": f"Command failed: {e}",
                    }))
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WS error: %s", e)
    finally:
        ctrl_clients.discard(ws)
        _ws_tokens.pop(ws, None)
        _tx_hb_capable.discard(ws)
        _tx_hb_last.pop(ws, None)
        metrics.close(role, "control", token)
        logger.info("WS client disconnected (%d remain)", len(ctrl_clients))
        if scheduler:
            scheduler.set_active(len(ctrl_clients) > 0)
        try:
            await _broadcast_client_count()
        except Exception:
            pass

        # A CQ call belongs to the client that started it: if that client goes
        # away the call is cut immediately (never a bystander's carrier).
        try:
            await _cq_abort_if_client_gone(ws)
        except Exception as e:
            logger.warning("CQ abort on disconnect failed: %s", e)

        # PTT safety: the client that keyed just vanished. Drop the carrier
        # even when other clients remain — they are not the keyer and their
        # watchdogs are exactly what the arbitration ignores (a zombie keyer
        # would otherwise hold the radio until the last client leaves or
        # MRRC_PTT_MAX_TX_SECONDS fires). Mirrors the /WSaudioTX owner rule.
        if ws is _ptt_key_ws:
            _ptt_key_ws = None
            if radio.is_transmitting and cat and cat.connected:
                logger.warning("PTT keyer disconnected during TX! Forcing RX.")
                try:
                    await cat.set_ptt(False)
                    radio.update(tx_status=0)
                except Exception:
                    pass
                if audio:
                    await asyncio.to_thread(audio.stop_tx)

        # PTT safety: if no clients remain and radio is transmitting, force RX
        if not ctrl_clients and radio.is_transmitting and cat and cat.connected:
            logger.warning("Last client disconnected during TX! Forcing RX.")
            try:
                await cat.set_ptt(False)
                radio.update(tx_status=0)
            except Exception:
                pass
            if audio:
                await asyncio.to_thread(audio.stop_tx)


# ── Spectrum WebSocket ───────────────────────────────────────────────

@app.websocket("/WSspectrum")
async def ws_spectrum(ws: WebSocket):
    """Binary spectrum data endpoint.

    Frame: 1-byte version (0x01) + 850 bytes wf1, plus 850 bytes wf2 on the
    full shape (1701 B) and absent on the wf1 shape (851 B).  Which shape and
    how often is this socket's profile — see spectrum_profile (AD-025).  The
    loop ticks at SPECTRUM_BROADCAST_FPS (30 Hz); real scope frames go out only
    when the hardware counter advances (~11 fps measured on the FT-710).

    A socket that never sends ``spectrumCaps`` gets the full-rate 1701 B
    stream, byte-for-byte what every client before AD-025 received.
    """
    token = _token_from_ws_handshake(ws)
    if not token or token not in _auth_tokens:
        await ws.close(code=4001, reason="Unauthorized")
        return

    await ws.accept()
    spectrum_clients.add(ws)
    if token in _listen_tokens:
        _listen_spectrum_clients.add(ws)
    metrics.open(_role_for_token(token), "spectrum", token)
    logger.info("Spectrum client connected (%d total)", len(spectrum_clients))
    if _scope_producer is not None:
        await _scope_producer.start()
    try:
        while True:
            # Keep connection alive, actual data sent by broadcast loop.
            # Text frames used to be discarded; they now carry this socket's
            # bandwidth tier (design §5.2).  Anything else — a ping, malformed
            # JSON, an unknown tier name — stays as harmless as silence.
            profile = spectrum_profile.parse_caps(await ws.receive_text())
            if profile and _spectrum_profiles.get(ws) != profile:
                _spectrum_profiles[ws] = profile
                tier = spectrum_profile.PROFILES[profile]
                frame_len = (spectrum_profile.SHORT_FRAME_BYTES
                             if tier.shape == spectrum_profile.SHAPE_WF1
                             else spectrum_profile.FULL_FRAME_BYTES)
                logger.info(
                    "Spectrum profile %s for %s socket: %s frame (%d B), divider %d",
                    profile,
                    "listener" if ws in _listen_spectrum_clients else "operator",
                    tier.shape, frame_len, tier.divider,
                )
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        spectrum_clients.discard(ws)
        _listen_spectrum_clients.discard(ws)
        # Per-socket tier state goes with the socket — three cleanups, not two.
        _spectrum_profiles.pop(ws, None)
        metrics.close(_role_for_token(token), "spectrum", token)
        logger.info("Spectrum client disconnected (%d remain)", len(spectrum_clients))
        if not spectrum_clients and _scope_producer is not None:
            await _scope_producer.stop()


# ── Audio RX WebSocket ────────────────────────────────────────────────

@app.websocket("/WSaudioRX")
async def ws_audio_rx(ws: WebSocket):
    """RX audio stream endpoint. Sends tagged binary frames:
    1-byte codec tag (0x00=PCM, 0x01=Opus) + payload.

    Data is produced by the _audio_rx_loop and broadcast to all
    connected audio_rx_clients.
    """
    token = _token_from_ws_handshake(ws)
    if not token or token not in _auth_tokens:
        await ws.close(code=4001, reason="Unauthorized")
        return

    await ws.accept()
    audio_rx_clients.add(ws)
    metrics.open(_role_for_token(token), "audio_rx", token)
    logger.info("Audio RX client connected (%d total)", len(audio_rx_clients))
    try:
        while True:
            await ws.receive()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        audio_rx_clients.discard(ws)
        metrics.close(_role_for_token(token), "audio_rx", token)
        logger.info("Audio RX client disconnected (%d remain)", len(audio_rx_clients))


# ── Audio TX WebSocket ────────────────────────────────────────────────

@app.websocket("/WSaudioTX")
async def ws_audio_tx(ws: WebSocket):
    """TX mic uplink endpoint. Receives binary frames:
    1-byte codec tag (0x00=PCM, 0x01=Opus) + payload.
    Text frames: 'm:rate,...' for settings, 's:' for stop.

    Single-owner: only the owner's audio is fed to the radio. A replacement
    connection from the same authenticated browser session takes over; other
    clients wait until they key the radio or the owner disconnects.
    """
    global audio, _opus_tx_decoder, _tx_owner_ws, _ptt_key_ws
    global _tx_session_frames, _tx_session_decoded, _tx_session_decode_fail
    global _tx_non_owner_drops

    token = _token_from_ws_handshake(ws)
    if not token or token not in _auth_tokens:
        await ws.close(code=4001, reason="Unauthorized")
        return
    if token in _listen_tokens:
        # Listen-only sessions never get a mic uplink.
        await ws.close(code=4003, reason="Listen-only session")
        return

    await ws.accept()
    audio_tx_clients.add(ws)
    _ws_tokens[ws] = token
    _register_tx_socket(ws)
    metrics.open(_role_for_token(token), "audio_tx", token)
    # Same-token replacement fixes the "PTT keys but non_owner_drops"
    # pattern where a half-open socket hoards the uplink after page reload.
    is_owner = _assign_tx_owner_on_connect(ws, token)
    logger.info("Audio TX client connected (%d total, owner=%s)",
                len(audio_tx_clients), is_owner)

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            if "bytes" in msg and msg["bytes"] is not None:
                # Only the owner's audio reaches the radio; ignore others.
                if ws is not _tx_owner_ws:
                    # A same-session socket that is streaming IS the operator
                    # (the token is a 30-day cookie, so one browser session can
                    # hold several of them) — hand the uplink over rather than
                    # mute the transmission this frame belongs to.
                    if not _same_session_uplink_takeover(ws):
                        _tx_non_owner_drops += 1
                        if _tx_non_owner_drops % 250 == 1:
                            logger.warning(
                                "TX audio from non-owner client ignored "
                                "(drops=%d) — another client owns the uplink",
                                _tx_non_owner_drops)
                        continue
                data = msg["bytes"]
                if len(data) < 2:
                    continue

                # Decode codec-tagged mic frame → PCM → audio output
                tag = data[0]
                frame = data[1:]
                _tx_session_frames += 1

                if tag == AUDIO_TAG_OPUS and _opus_tx_decoder:
                    pcm = _opus_tx_decoder.decode(frame)
                    if pcm and audio:
                        await _feed_tx_from_uplink(pcm)
                        _tx_session_decoded += 1
                    elif not pcm:
                        _tx_session_decode_fail += 1
                elif tag == AUDIO_TAG_PCM:
                    if audio:
                        await _feed_tx_from_uplink(frame)
                        _tx_session_decoded += 1
                else:
                    # Legacy: untagged, assume PCM
                    if audio:
                        await _feed_tx_from_uplink(data)
                        _tx_session_decoded += 1

            elif "text" in msg and msg["text"]:
                text = msg["text"]
                if text.startswith("s:") or text == "stop":
                    # Client stopped capturing. Do NOT clear the queue here —
                    # the PTT-release path drains queued audio before dropping
                    # RF, so word-endings survive. Clearing now would chop
                    # the tail if 's:' arrives before ptt:false.
                    pass
                elif text.startswith("m:"):
                    # Settings: rate, encode, etc.
                    try:
                        parts = text[2:].split(",")
                        # tx_rate = int(float(parts[0])) if parts else 16000
                    except Exception:
                        pass

    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        audio_tx_clients.discard(ws)
        _ws_tokens.pop(ws, None)
        _forget_tx_socket(ws)
        metrics.close(_role_for_token(token), "audio_tx", token)
        if ws is _tx_owner_ws:
            _tx_owner_ws = None
            # Force RX if the TX-audio owner vanishes while keyed — otherwise
            # the radio sits on a dead carrier until the control socket also
            # drops (dead-man switch) or the user releases PTT.
            if radio.is_transmitting and cat and cat.connected:
                logger.warning("TX audio owner disconnected during TX! Forcing RX.")
                try:
                    await cat.set_ptt(False)
                    _ptt_key_ws = None
                    radio.update(tx_status=0)
                except Exception:
                    pass
            if audio:
                await asyncio.to_thread(audio.stop_tx)
            # Hand the uplink to a remaining client — otherwise everyone
            # stays muted until they reconnect (the "PTT keys but no
            # voice" soft failure after the owner drops).
            if _promote_tx_owner() is not None:
                logger.info("TX audio ownership promoted to remaining client "
                            "(%d total)", len(audio_tx_clients))
        logger.info("Audio TX client disconnected (%d remain, owner=%s)",
                    len(audio_tx_clients), _tx_owner_ws is not None)


# ── SDR receiver (AD-029) ───────────────────────────────────────────
#
# 一条独立的入口：自己的口令（MRRC_SDR_PASSWORD）、自己的 cookie（mrrc_sdr）、
# 自己的页面（/sdr）。与无线电控制面零耦合 —— 这里没有任何 CAT/PTT 通路，
# 页面与接口也不返回电台状态。
#
# 设备由 sdr_pipe.py 独占（一个进程一个设备），本文件只做三件事：
# 仲裁/池/容量（SdrHub）、把订阅者接到 WebSocket、把 pipe 的帧扇出去。

SDR_PASSWORD = os.environ.get("MRRC_SDR_PASSWORD", "")
SDR_ENABLED = (os.environ.get("MRRC_SDR", "on") or "on").strip().lower() not in (
    "off", "0", "false", "no")
SDR_COOKIE = "mrrc_sdr"
SDR_MAX_CLIENTS = max(1, int(os.environ.get("MRRC_SDR_MAX_CLIENTS", "50") or 50))
SDR_MAX_CHANNELS = max(1, min(16, int(os.environ.get("MRRC_SDR_MAX_CHANNELS", "8") or 8)))
SDR_SAMPLE_RATE = float(os.environ.get("MRRC_SDR_SAMPLE_RATE", "2400000") or 2400000)
SDR_GAIN_DB = int(os.environ.get("MRRC_SDR_GAIN_DB", "40") or 40)
SDR_LNA = int(os.environ.get("MRRC_SDR_LNA", "2") or 2)

_sdr_hub: Optional[SdrHub] = None
_sdr_runner: Optional["SdrPipeRunner"] = None
_sdr_sid = 0


def _sdr_login_enabled() -> bool:
    """有口令且没被 MRRC_SDR=off 关掉，才有这条入口。"""
    return bool(SDR_ENABLED and SDR_PASSWORD)


def _sdr_password_matches(candidate: str) -> bool:
    """常量时间比较（I9 的既有教训，别在新入口重犯）。"""
    return bool(SDR_PASSWORD) and hmac.compare_digest(str(candidate), SDR_PASSWORD)


def _sdr_static_allowed(request: Request, path: str) -> bool:
    """SDR 页面自己的四个资源：只有已认证的 SDR 会话能取。

    中间件与 ``serve_static`` 各判一次 —— 前者先于后者运行，两边必须用同一份
    白名单，否则会话已登录却连自己的 JS 都拿不到（现场：302 /login?next=/sdr.js）。
    """
    return (_sdr_login_enabled()
            and path.lstrip("/") in SDR_STATIC_PATHS
            and _sdr_token_from_request(request) in _sdr_tokens)


def _sdr_token_from_request(request: Request) -> str:
    """SDR 入口的令牌：同一套解析顺序，只换 cookie 名（AD-024 的弃用 query 仍走共享处）。"""
    return _token_from_request(request, SDR_COOKIE)


def _sdr_token_from_ws(ws: WebSocket) -> str:
    return _token_from_ws_handshake(ws, SDR_COOKIE)


def _sdr_repo_file(name: str) -> Optional[str]:
    for cand in (Path(__file__).parent / name, Path("/usr/local/lib") / name,
                 Path("/usr/lib") / name):
        if cand.exists():
            return str(cand)
    return None


class SdrPipeRunner:
    """持有 sdr_pipe 子进程；把 SdrHub 的调用翻译成 C1 命令帧。

    进程退出（设备被拔、崩溃）→ 广播不可用 → 1/2/5/10 s 退避重启。
    """

    BACKOFF = (1.0, 2.0, 5.0, 10.0)

    def __init__(self, hub: "SdrHub", script: Path, fs: float, max_ch: int):
        self.hub = hub
        self.script = script
        self.fs = fs
        self.max_ch = max_ch
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._tasks: list[asyncio.Task] = []
        self._backoff_i = 0
        self._stopping = False
        self.last_error = ""

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def ensure_started(self) -> None:
        if self.running or self._stopping:
            return
        await self._spawn()

    async def _spawn(self) -> None:
        env = dict(os.environ)
        shim = os.environ.get("MRRC_SDR_SHIM") or _sdr_repo_file("libsdr_api_shim.so")
        dsp = os.environ.get("MRRC_SDR_DSP_LIB") or _sdr_repo_file("libsdr_dsp.so")
        if not shim or not dsp:
            self.hub.set_available(False, "native libraries not installed")
            return
        env["MRRC_SDR_SHIM"] = shim
        env["MRRC_SDR_DSP_LIB"] = dsp
        try:
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable, str(self.script), "--device",
                "--fs", str(self.fs), "--max-channels", str(self.max_ch),
                "--gain", str(SDR_GAIN_DB), "--lna", str(SDR_LNA),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env)
        except Exception as exc:                                  # noqa: BLE001
            self.hub.set_available(False, f"pipe spawn failed: {exc}")
            return
        self._tasks = [asyncio.create_task(self._read_loop(self._proc)),
                       asyncio.create_task(self._stderr_loop(self._proc)),
                       asyncio.create_task(self._monitor(self._proc))]

    async def _read_loop(self, proc) -> None:
        parser = sdr_frames.FrameParser()
        while True:
            data = await proc.stdout.read(65536)
            if not data:
                return
            for frame in parser.feed(data):
                self._dispatch(frame)

    async def _stderr_loop(self, proc) -> None:
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode(errors="replace").strip()
            if text:
                self.last_error = text
                logger.info("sdr_pipe: %s", text)

    async def _monitor(self, proc) -> None:
        rc = await proc.wait()
        if self._stopping:
            return
        reason = f"sdr_pipe exited ({rc})"
        if self.last_error:
            reason += f": {self.last_error}"
        self.hub.set_available(False, reason)
        self.hub.broadcast_state()
        logger.warning("SDR pipe stopped: %s", reason)
        delay = self.BACKOFF[min(self._backoff_i, len(self.BACKOFF) - 1)]
        self._backoff_i += 1
        await asyncio.sleep(delay)
        if not self._stopping:
            await self._spawn()

    def _dispatch(self, frame: tuple) -> None:
        kind = frame[0]
        if kind == "status":
            obj = frame[1]
            if obj.get("error"):
                if self.hub.available:
                    self.hub.set_available(False, str(obj["error"]))
                    self.hub.broadcast_state()
                return
            if not self.hub.available:
                self.hub.set_available(True)
                self.hub.broadcast_state()
            self._backoff_i = 0
            if "center_hz" in obj:
                self.hub.center_hz = int(obj["center_hz"])
        elif kind == "spectrum":
            _, seq, bins = frame
            self.hub.on_spectrum(seq, bins)
        elif kind == "audio":
            _, chid, seq, level, payload = frame
            self.hub.on_audio(chid, seq, level, payload)

    def _send(self, obj: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or not self.running:
            return
        try:
            proc.stdin.write(sdr_frames.encode_command(obj))
        except Exception:                                          # noqa: BLE001
            pass

    # -- SdrHub 的 PipeLike 接口 -----------------------------------------
    def tune(self, hz: float) -> None:
        self._send({"cmd": "tune", "hz": int(hz)})

    def set_channel(self, chid: int, offset_hz: float, mode: int, bw_hz: float) -> None:
        self._send({"cmd": "channel", "chid": chid, "offset_hz": offset_hz,
                    "mode": mode, "bw_hz": bw_hz})

    def clear_channel(self, chid: int) -> None:
        self._send({"cmd": "clear", "chid": chid})

    async def stop(self) -> None:
        self._stopping = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            self._send({"cmd": "quit"})
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                proc.kill()
        for task in self._tasks:
            task.cancel()
        self._tasks = []


def _sdr_ensure_hub() -> "SdrHub":
    global _sdr_hub, _sdr_runner
    if _sdr_hub is None:
        _sdr_hub = SdrHub(clock=time.monotonic, max_clients=SDR_MAX_CLIENTS,
                          max_channels=SDR_MAX_CHANNELS, span_hz=SDR_SAMPLE_RATE)
        _sdr_runner = SdrPipeRunner(_sdr_hub, Path(__file__).parent / "sdr_pipe.py",
                                    SDR_SAMPLE_RATE, SDR_MAX_CHANNELS)
        _sdr_hub._pipe = _sdr_runner          # SdrHub 的 PipeLike
    return _sdr_hub


def _sdr_ws_encode(frame: tuple) -> tuple[str, object]:
    """SdrHub 的帧 → (kind, payload)：binary 走字节，其余走 JSON 文本。"""
    kind = frame[0]
    if kind == "spectrum":
        _, seq, bins = frame
        return "bytes", b"\x01" + struct.pack("<HH", seq & 0xFFFF, len(bins)) + bins
    if kind == "audio":
        _, chid, seq, level, payload = frame
        return "bytes", (b"\x02" + struct.pack("<BHBH", chid, seq & 0xFFFF,
                                               max(0, min(255, int(level * 255))),
                                               len(payload)) + payload)
    return "text", json.dumps({"event_type": kind, "value": frame[1]})


@app.post("/api/sdr/login")
async def api_sdr_login(request: Request):
    """SDR 入口的登录：口令是 MRRC_SDR_PASSWORD，cookie 与主站分开。"""
    if not _sdr_login_enabled():
        return JSONResponse({"error": "SDR receiver is not enabled"}, status_code=404)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_login_rate_limit(client_ip):
        return JSONResponse({"error": "Too many login attempts."}, status_code=429)
    try:
        body = await request.json()
        password = body.get("password", "")
    except Exception:                                             # noqa: BLE001
        password = ""
    if not _sdr_password_matches(password):
        logger.warning("SDR login failed from %s", client_ip)
        return JSONResponse({"error": "Invalid password"}, status_code=401)
    token = _make_auth_token()
    _sdr_tokens.add(token)
    _token_issued[token] = time.time()
    _save_auth_tokens()
    logger.info("SDR receiver login from %s", client_ip)
    response = JSONResponse({"ok": True})
    response.set_cookie(SDR_COOKIE, token, max_age=30 * 24 * 3600,
                        httponly=False, samesite="lax")
    return response


@app.get("/sdr", include_in_schema=False)
async def sdr_page(request: Request):
    """SDR 接收机页面。没有 sdr cookie 时给一张自己的登录表单。"""
    if not _sdr_login_enabled():
        raise HTTPException(status_code=404, detail="SDR receiver is not enabled")
    token = _sdr_token_from_request(request)
    if token not in _sdr_tokens:
        return HTMLResponse(_SDR_LOGIN_HTML, status_code=200)
    page = STATIC_DIR / "sdr.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="sdr.html missing")
    response = FileResponse(page, media_type="text/html")
    response.headers["Cache-Control"] = "no-cache"
    return response


_SDR_LOGIN_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SDR 接收机</title>
<style>body{font-family:-apple-system,system-ui,sans-serif;background:#12120f;color:#e8e2d4;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
form{background:#1d1c17;padding:2rem;border-radius:14px;width:min(88vw,320px);
box-shadow:0 8px 30px rgba(0,0,0,.5)}
h1{margin:0 0 1rem;font-size:1.1rem;color:#f5a623;text-align:center;letter-spacing:.08em}
input{width:100%;padding:12px;border:1px solid #3a382f;border-radius:8px;background:#12120f;
color:#fff;font-size:16px;box-sizing:border-box}
button{width:100%;padding:12px;margin-top:12px;background:#f5a623;color:#12120f;border:0;
border-radius:8px;font-size:16px;font-weight:700;cursor:pointer}
.err{color:#ff6b6b;margin-top:10px;font-size:.9rem;text-align:center;min-height:1.2em}</style>
</head><body><form id="f"><h1>SDR 接收机</h1>
<input type="password" id="p" placeholder="口令" autofocus required>
<button type="submit">进入</button><div class="err" id="e"></div></form>
<script>
document.getElementById('f').onsubmit = async function (ev) {
  ev.preventDefault();
  var e = document.getElementById('e');
  try {
    var r = await fetch('api/sdr/login', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({password: document.getElementById('p').value})});
    if (r.ok) { location.reload(); } else { e.textContent = '口令不对'; }
  } catch (err) { e.textContent = '连接失败'; }
};
</script></body></html>"""


def _next_sdr_sid() -> int:
    global _sdr_sid
    _sdr_sid += 1
    return _sdr_sid


@app.websocket("/WSsdr")
async def ws_sdr(ws: WebSocket):
    """SDR 接收机通道：文本帧发命令，二进制帧收频谱与音频。"""
    token = _sdr_token_from_ws(ws)
    if not token or token not in _sdr_tokens:
        await ws.close(code=4001, reason="Unauthorized")
        return
    if not _sdr_login_enabled():
        await ws.close(code=4003, reason="SDR receiver is not enabled")
        return

    await ws.accept()
    hub = _sdr_ensure_hub()
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    sid = _next_sdr_sid()
    try:
        sub = hub.attach(sid)
    except SdrCapacityError as exc:
        await ws.close(code=4003, reason=str(exc))
        return

    def _push(frame: tuple) -> None:
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            hub.dropped_frames += 1

    sub.send = _push
    logger.info("SDR socket connected (%d listener(s), %d demod(s))",
                hub.listener_count + 1, hub.channel_count)
    metrics.open("sdr", "sdr", token)
    await _sdr_runner.ensure_started()
    await ws.send_text(json.dumps({
        "event_type": "hello",
        "value": {
            "modes": ["AM", "USB", "LSB", "CW", "NFM", "WFM"],
            "max_clients": SDR_MAX_CLIENTS,
            "max_channels": SDR_MAX_CHANNELS,
            "sample_rate": SDR_SAMPLE_RATE,
        },
    }))

    async def pump() -> None:
        while True:
            frame = await queue.get()
            kind, payload = _sdr_ws_encode(frame)
            if kind == "bytes":
                await ws.send_bytes(payload)
                metrics.add_bytes("sdr", len(payload))
            else:
                await ws.send_text(payload)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            text = await ws.receive_text()
            try:
                obj = json.loads(text)
            except Exception:                                     # noqa: BLE001
                continue
            event = obj.get("event_type")
            if event == "tune":
                ok, wait = hub.tune(sub, int(obj.get("value", 0)))
                if not ok:
                    await ws.send_text(json.dumps({
                        "event_type": "error", "code": "locked",
                        "value": {"retry_after": round(wait, 1)}}))
                hub.broadcast_state()
            elif event == "channel":
                val = obj.get("value") or {}
                try:
                    chid = hub.subscribe_channel(sub, int(val.get("mode", 0)),
                                                 int(val.get("bw_hz", 3000)))
                except SdrChannelFull as exc:
                    await ws.send_text(json.dumps({
                        "event_type": "error", "code": "channel_full",
                        "value": {"reason": str(exc)}}))
                    continue
                await ws.send_text(json.dumps({
                    "event_type": "channel_ack", "value": {"chid": chid}}))
            elif event == "cursor":
                hub.set_cursor(sub, float(obj.get("value", 0)))
                hub.broadcast_state()
            elif event == "squelch":
                hub.set_squelch(sub, float(obj.get("value", 0)))
            elif event == "volume":
                hub.set_volume(sub, float(obj.get("value", 1)))
            elif event == "state":
                hub.broadcast_state()
    except WebSocketDisconnect:
        pass
    except Exception as exc:                                      # noqa: BLE001
        logger.debug("SDR socket closed: %s", exc)
    finally:
        pump_task.cancel()
        hub.detach(sub)
        metrics.close("sdr", "sdr", token)
        logger.info("SDR socket disconnected (%d remain, %d demod(s), dropped=%d)",
                    hub.listener_count, hub.channel_count, hub.dropped_frames)
        hub.broadcast_state()


# ── ATR1000 Tuner WebSocket (optional) ──────────────────────────────

@app.websocket("/WSatr1000")
async def ws_atr1000(ws: WebSocket):
    """External-tuner channel: state pushes (meter/relay/tuning) and the
    server-side tune-assist command.  Closes immediately when the
    feature is disabled (no MRRC_ATR1000_HOST configured)."""
    token = _token_from_ws_handshake(ws)
    if not token or token not in _auth_tokens:
        await ws.close(code=4001, reason="Unauthorized")
        return
    if token in _listen_tokens:
        # The tuner channel carries the relay-driving atrTune command —
        # off-limits for listen-only sessions.
        await ws.close(code=4003, reason="Listen-only session")
        return

    await ws.accept()
    if atr is None:
        await ws.close(code=4000, reason="ATR1000 disabled")
        return

    atr_clients.add(ws)
    metrics.open(_role_for_token(token), "atr", token)
    atr.client_count = len(atr_clients)  # drives the client's SYNC poll policy
    logger.info("ATR1000 WS client connected (%d total)", len(atr_clients))
    try:
        await ws.send_text(json.dumps({"type": "atrState", **atr.read_state()}))
        while True:
            msg = json.loads(await ws.receive_text())
            msg_type = msg.get("type", "")
            if msg_type == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
            elif msg_type == "atrTune":
                await _start_atr_tune_assist(ws)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WSatr1000 error: %s", e)
    finally:
        atr_clients.discard(ws)
        metrics.close(_role_for_token(token), "atr", token)
        if atr is not None:
            atr.client_count = len(atr_clients)
        logger.info("ATR1000 WS client disconnected (%d remain)", len(atr_clients))


# ── Static File Serving ─────────────────────────────────────────────

def _resolve_static_path(path: str):
    """Resolve a request path to a file inside STATIC_DIR (SDD I8).

    Naively joining the request path into STATIC_DIR lets
    ``GET /../server.py`` (or an absolute request path, which Path()
    replaces wholesale) read arbitrary files. Resolve the joined path and
    require it to stay inside the resolved STATIC_DIR — this also defeats
    symlink escapes. Returns None when the path escapes containment or
    cannot be resolved.
    """
    rel = path if path else "index.html"
    try:
        resolved = STATIC_DIR.joinpath(rel).resolve()
        contained = resolved.is_relative_to(STATIC_DIR.resolve())
    except OSError:
        return None
    return resolved if contained else None


# 静态件的缓存语义：两层同一套值 —— hub 边缘（`../mrrc_hub/deploy/nginx/mrrc-instances.vhost`）
# 发同样的两个常量。实例侧这一份不是重复，而是覆盖公网边缘之外的所有路径：
# 局域网直连、B2/B3 退化入口、别的反代，以及边缘缓存还没建立时的第一个用户。
CACHE_CONTROL_DOC = "no-cache"                # HTML / JSON / sw.js：每次都重新校验
CACHE_CONTROL_ASSET = "public, max-age=3600"  # 其余静态件：1 小时


def _cache_control_for(path: Path) -> str:
    """文档类每次都重新校验，其余静态件 1 小时。

    1 小时而不是 ``immutable``：`index.html` 里的 ``?v=`` 是**手工** bump 的，漏 bump 时
    1 小时是用户最长踩到旧代码的窗口；``immutable`` 会把这个代价放大成一年。
    部署后新版能立刻看到，靠的是文档类 ``no-cache`` 加 `index.html` 换 ``?v=`` 新号。
    """
    if path.suffix.lower() in (".html", ".htm", ".json") or path.name == "sw.js":
        return CACHE_CONTROL_DOC
    return CACHE_CONTROL_ASSET


def _html_page(page: Path) -> FileResponse:
    """一个 HTML 页面响应：文档类语义（每次重新校验）。

    `/listen` 与 `/settings` 走这里（它们不经 `serve_static`）。`/setup` **刻意不走**：
    那个页面会显示自动生成的口令横幅，所以它自己发 `no-store`，比 no-cache 更严。
    """
    response = FileResponse(page, media_type="text/html")
    response.headers["Cache-Control"] = _cache_control_for(page)
    return response


def _not_modified(request: Request, headers) -> bool:
    """条件请求是否命中（命中则回 304，不重传文件）。

    RFC 7232：带了 ``If-None-Match`` 就不再按 ``If-Modified-Since`` 判断。
    日期报文解析不了时当作“没带条件”—— 宁可多发一次文件，也不要错误地发 304。
    """
    etag = headers.get("etag")
    if_none_match = request.headers.get("if-none-match")
    if if_none_match is not None:
        if not etag:
            return False
        candidates = {token.strip() for token in if_none_match.split(",")}
        return etag in candidates or f"W/{etag}" in candidates
    if_modified_since = request.headers.get("if-modified-since")
    last_modified = headers.get("last-modified")
    if if_modified_since and last_modified:
        try:
            return parsedate_to_datetime(if_modified_since) >= parsedate_to_datetime(last_modified)
        except (TypeError, ValueError):
            return False
    return False



# ── NanoVNA-H 天线诊断台（spec 2026-10-18 / AD-030）───────────────────
#
# 一台接在盒子 USB 上的串口仪器：不是电台，不共享任何 CAT 状态，也不产生
# 需要广播的 radio state。串口只在 nano/vna.py 里出现；本文件负责四件事：
#
#   1. 一次一件（`_nano_lock`）—— 串口是有状态的，两次扫描交错就是两批烂数据；
#   2. 发射中闭嘴（`_nano_tx_active`）—— VNA 的 CH0 接在天线上，几十瓦射频进去
#      是**烧前端**，不是 UI 问题；
#   3. 不抢 CAT 口（`discover_port(cat_port=…)`）；
#   4. 角色隔离（listen / sdr 会话一律 403）。
#
# 扫描会改掉设备屏幕上的扫频范围，所以每次扫完都还原（spec §4.4）：设备在
# 用户进页面之前和退出之后必须完全一致。
NANO_ENABLED = (_env("MRRC_NANO", "on") or "on").strip().lower() not in (
    "off", "0", "false", "no")
NANO_ALLOW_FLASH = (_env("MRRC_NANO_ALLOW_FLASH", "0") or "0").strip().lower() in (
    "1", "true", "yes", "on")
# nano-data 而不是 nano：`nano/` 是代码包目录。数据写进去会在更新时被整目录
# 替换、混进源码、还会继承 rsync 的属主（现场 2026-10-18：state.json.tmp
# Permission denied —— 目录被 sudo rsync 归了 root，而服务跑在 mrrc 下）。
NANO_DIR = Path(_env("MRRC_NANO_DIR", str(_writable_runtime_dir() / "nano-data")))
NANO_SWEEPS_DIR = NANO_DIR / "sweeps"
NANO_STATE_FILE = NANO_DIR / "state.json"

_nano_device: Optional[NanoVna] = None
_nano_lock = asyncio.Lock()          # 一次一件
_nano_next_attempt = 0.0             # 打开失败后的退避（秒，monotonic）
_nano_last_sweep: Optional[dict] = None
_SWEEP_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _nano_tx_active() -> bool:
    """电台在发射（PTT / TUNE / FT8 / 一键 CQ）时，绝不能碰接在天线上的 VNA。"""
    if radio is not None and radio.tx_status:
        return True
    try:
        if _ft8_transmitting():
            return True
    except Exception:                                              # noqa: BLE001
        pass
    try:
        if _cq_player.is_calling:
            return True
    except Exception:                                              # noqa: BLE001
        pass
    return False


def _nano_forbidden(request: Request) -> Optional[tuple[int, str]]:
    """页面与接口共用的前置门：主站会话可以，listen / sdr 会话不行。"""
    if not NANO_ENABLED:
        return (404, "nano disabled")
    if _is_listen_request(request):
        return (403, "listen-only session")
    if _sdr_token_from_request(request) in _sdr_tokens:
        return (403, "sdr session")
    return None


def _nano_gate(request: Request) -> Optional[JSONResponse]:
    reason = _nano_forbidden(request)
    if reason is None:
        return None
    return JSONResponse({"error": reason[1]}, status_code=reason[0])


def _nano_connect() -> Optional[NanoVna]:
    """按需连一次；失败后退避 2 s 再试，避免状态轮询把串口敲烂。"""
    global _nano_device, _nano_next_attempt
    if _nano_device is not None:
        return _nano_device
    now = time.monotonic()
    if now < _nano_next_attempt:
        return None
    port = discover_port(os.environ, cat_port=os.environ.get("MRRC_SERIAL_PORT"))
    if not port:
        _nano_next_attempt = now + 2.0
        logger.info("nano: no device found")
        return None
    dev = None
    try:
        dev = NanoVna(port)
        dev.open()
        logger.info("nano: connected on %s (%s)", port,
                    dev.info_cache.version if dev.info_cache else "?")
    except Exception as exc:                                       # noqa: BLE001
        _nano_next_attempt = now + 2.0
        logger.info("nano: open failed on %s (%s)", port, exc)
        # 打开之后才失败的，必须把句柄关掉：泄漏的 tty 会让下一次连接报
        # "device reports readiness to read but returned no data"（现场 2026-10-18，
        # 旧版本在这里抛 NameError，端口被占了一整轮）。
        if dev is not None:
            try:
                dev.close()
            except Exception:                                      # noqa: BLE001
                pass
        return None
    _nano_device = dev
    return _nano_device


def _nano_forget(reason: str) -> None:
    global _nano_device
    if _nano_device is not None:
        logger.info("nano: dropping device (%s)", reason)
        try:
            _nano_device.close()
        except Exception:                                          # noqa: BLE001
            pass
    _nano_device = None


def _nano_read_state() -> dict:
    """进来时设备屏幕上的扫频原文 —— 每次扫描后要还原到它。"""
    try:
        return json.loads(NANO_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:                                              # noqa: BLE001
        return {}


def _nano_write_json(path: Path, payload: dict) -> None:
    """原子写（照 atr1000_tuner.py 的写法：先写 .tmp 再 replace）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _nano_capture_state(dev: NanoVna) -> dict:
    """读设备当前扫频范围并落盘（只在还没有记录或记录失效时调用）。"""
    state = _nano_read_state()
    if state.get("start_hz") and state.get("stop_hz") and state.get("points"):
        return state
    try:
        freqs = dev.frequencies()
    except Exception as exc:                                       # noqa: BLE001
        logger.info("nano: cannot read device sweep (%s)", exc)
        return {}
    if len(freqs) < 2:
        return {}
    state = {"start_hz": freqs[0], "stop_hz": freqs[-1], "points": len(freqs)}
    try:
        _nano_write_json(NANO_STATE_FILE, state)
    except OSError as exc:
        logger.info("nano: cannot persist device sweep (%s)", exc)
    return state


def _nano_status_block(dev: NanoVna, with_settings: bool) -> dict:
    info = dev.info()
    block: dict = {
        "connected": True,
        "port": dev.port,
        "board": info.board,
        "version": info.version,
        "build": info.build,
        "platform": info.platform,
        "points_max": info.points_max,
        "vbat_mv": None,
        "cal": None,
        "device_sweep": _nano_read_state(),
    }
    # 设备不应答 = 句柄已死（USB 重新枚举）。这里**必须**抛出去而不是吞掉：
    # 吞掉会让状态一直说"在线"，下一次扫描才炸 —— 用户看到的就成了"刚才还好好的"。
    try:
        block["vbat_mv"] = dev.vbat_mv()
        block["cal"] = dev.cal_state()
    except Exception as exc:                                       # noqa: BLE001
        raise NanoError("device stopped answering: %s" % exc)
    if with_settings:
        try:
            block["settings"] = dev.read_settings()
        except Exception as exc:                                   # noqa: BLE001
            block["settings_error"] = str(exc)
    return block


def _nano_require_device() -> NanoVna:
    dev = _nano_connect()
    if dev is None:
        raise NanoError("device offline")
    return dev


def _nano_sweep_path(sid: str) -> Optional[Path]:
    if not _SWEEP_ID_RE.match(sid or ""):
        return None
    path = NANO_SWEEPS_DIR / (sid + ".json")
    return path if path.exists() else None


def _nano_list_sweeps() -> list[dict]:
    out: list[dict] = []
    if not NANO_SWEEPS_DIR.is_dir():
        return out
    for path in sorted(NANO_SWEEPS_DIR.glob("sweep_*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:                                          # noqa: BLE001
            continue
        out.append({
            "id": path.stem,
            "created": data.get("created"),
            "label": data.get("label") or "",
            "note": data.get("note") or "",
            "start_hz": data.get("start_hz"),
            "stop_hz": data.get("stop_hz"),
            "points": data.get("points"),
        })
    return out


async def _perf_lag_monitor() -> None:
    """Measure how late the event loop is, once every 250 ms.

    A `sleep(0.25)` that returns after 0.9 s means something held the loop for
    650 ms — a blocking CAT write, an encoder on the loop, a PTY call. That is
    exactly the operator's "卡一下" and it shows up here while CPU stays low, so
    this number has to be measured in-process: no external tool can see it.
    """
    interval = 0.25
    while True:
        try:
            started = time.perf_counter()
            await asyncio.sleep(interval)
            late_ms = max(0.0, (time.perf_counter() - started - interval) * 1000.0)
            perf_lag["last_ms"] = round(late_ms, 1)
            perf_lag["samples"] += 1
            # Peak decays slowly instead of resetting: one 800 ms stall is the
            # symptom worth keeping for a minute, not a blip to average away.
            perf_lag["peak_ms"] = round(max(perf_lag["peak_ms"] * 0.995, late_ms), 1)
        except asyncio.CancelledError:
            raise
        except Exception:                                          # noqa: BLE001
            await asyncio.sleep(interval)


def _perf_forbidden(request: Request) -> Optional[tuple[int, str]]:
    """性能页与接口共用的前置门：主站会话可以，listen / sdr 会话不行。

    这台盒子的内部数字（进程、温度、磁盘）不对外，而 listen 会话就是给陌生人的。
    """
    if _is_listen_request(request):
        return (403, "listen-only session")
    if _sdr_token_from_request(request) in _sdr_tokens:
        return (403, "sdr session")
    return None


def _perf_service_block() -> dict:
    """The in-process half: what this server is doing, not what Linux is."""
    audio_stats = {}
    if audio is not None:
        try:
            audio_stats = dict(audio.tx_stats())
            audio_stats["rx_silent_s"] = round(audio.rx_silent_seconds(), 1)
            audio_stats["device_rates"] = audio.device_rates()
        except Exception:                                          # noqa: BLE001
            audio_stats = {}
    try:
        sessions = metrics.snapshot()
    except Exception:                                              # noqa: BLE001
        sessions = {}
    return {
        "event_loop_lag_ms": perf_lag["last_ms"],
        "event_loop_peak_ms": perf_lag["peak_ms"],
        "lag_samples": perf_lag["samples"],
        "uptime_s": round(time.time() - _PROCESS_STARTED_AT, 1)
                    if "_PROCESS_STARTED_AT" in globals() else None,
        "version": support_bundle.detect_version(_runtime_dir(), _resource_dir()),
        "model": RADIO_MODEL,
        "clients": {
            "radio": len(ctrl_clients), "spectrum": len(spectrum_clients),
            "audio_rx": len(audio_rx_clients), "audio_tx": len(audio_tx_clients),
        },
        "audio": audio_stats,
        "sessions": sessions,
        "radio_connected": bool(cat and cat.connected),
        "tx": bool(radio.tx_status) if "radio" in globals() else None,
    }


@app.get("/Perf", include_in_schema=False)
async def perf_page(request: Request):
    """性能负载页：盒子与服务的 CPU / 内存 / IO / 网络一眼看完。"""
    reason = _perf_forbidden(request)
    if reason is not None:
        raise HTTPException(status_code=reason[0], detail=reason[1])
    page = STATIC_DIR / "perf.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="perf.html missing")
    response = FileResponse(page, media_type="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/assistant/status", include_in_schema=False)
async def api_assistant_status(request: Request):
    """面板开屏先问一句：开着吗、网关配了吗。"""
    blocked = _assistant_gate(request)
    if blocked is not None:
        return JSONResponse({"error": blocked[0]}, status_code=blocked[1])
    return JSONResponse({"enabled": True, "endpoint": bool(ASSISTANT_URL)})


@app.post("/api/assistant/ask", include_in_schema=False)
async def api_assistant_ask(request: Request):
    """一条提问 → 一条答案。**永远 200**（失败也在 payload 里降级），面板只看 degraded。"""
    blocked = _assistant_gate(request)
    if blocked is not None:
        return JSONResponse({"error": blocked[0]}, status_code=blocked[1])
    try:
        body = await request.json()
    except Exception:                                  # noqa: BLE001 - 坏 JSON 当成空提问
        body = {}
    if not isinstance(body, dict):
        body = {}
    if not ASSISTANT_URL:
        return JSONResponse(assistant_client.offline("unconfigured"))
    client = assistant_client.AssistantClient(ASSISTANT_URL, ASSISTANT_TOKEN)
    message = str(body.get("message") or "")[:4000]
    try:
        payload = await asyncio.to_thread(
            client.ask, body.get("conversation_id"), message, _assistant_prompt_state())
    except Exception:                                  # noqa: BLE001 - 助手永不影响主链路
        payload = assistant_client.offline()
    return JSONResponse(payload)


@app.post("/api/assistant/clear", include_in_schema=False)
async def api_assistant_clear(request: Request):
    """清空本机会话 —— 客户的删除权，硬删（规格 §7）。"""
    blocked = _assistant_gate(request)
    if blocked is not None:
        return JSONResponse({"error": blocked[0]}, status_code=blocked[1])
    try:
        body = await request.json()
    except Exception:                                  # noqa: BLE001
        body = {}
    if not ASSISTANT_URL:
        return JSONResponse({"deleted": False, "reason": "unconfigured"})
    client = assistant_client.AssistantClient(ASSISTANT_URL, ASSISTANT_TOKEN)
    try:
        payload = await asyncio.to_thread(
            client.clear, str((body or {}).get("conversation_id") or ""))
    except Exception:                                  # noqa: BLE001
        payload = {"deleted": False, "reason": "offline"}
    return JSONResponse(payload)


@app.get("/api/perf", include_in_schema=False)
async def api_perf(request: Request):
    """One snapshot: host load/IO/network plus this process's own symptoms.

    Read-only and cheap (a handful of /proc reads), so the page can poll it
    once a second without becoming the load it is trying to measure.
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    reason = _perf_forbidden(request)
    if reason is not None:
        return JSONResponse({"error": reason[1]}, status_code=reason[0])
    snapshot = await asyncio.to_thread(perf_sampler.tick)
    service = _perf_service_block()
    return JSONResponse({"host": snapshot, "service": service,
                         "verdict": host_perf.verdict(snapshot, service)})


@app.get("/Nano", include_in_schema=False)
async def nano_page(request: Request):
    """NanoVNA 天线诊断台。主站会话即可（listen / sdr 会话被拒）。"""
    reason = _nano_forbidden(request)
    if reason is not None:
        raise HTTPException(status_code=reason[0], detail=reason[1])
    page = STATIC_DIR / "nano.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="nano.html missing")
    return _html_page(page)


@app.get("/api/nano/status")
async def api_nano_status(request: Request, full: int = 0):
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    payload: dict = {
        "enabled": True,
        "connected": False,
        "port": None,
        "tx": _nano_tx_active(),
        "busy": _nano_lock.locked(),
        "device_sweep": _nano_read_state(),
        "flags": _nano_config().get("flags") or {},
        "last_sweep": (None if _nano_last_sweep is None else {
            "start_hz": _nano_last_sweep["start_hz"],
            "stop_hz": _nano_last_sweep["stop_hz"],
            "points": _nano_last_sweep["points"],
        }),
        "flash": {"enabled": NANO_ALLOW_FLASH, "variant": _nano_read_state().get("variant")},
    }
    if _nano_lock.locked():
        # 忙时**不碰串口**：并发读写同一个 pyserial 句柄会抢走扫描的帧字节。
        # connected 必须如实 —— 忙 ≠ 掉线，这个判断错了页面每扫一次就喊一次"掉线"。
        payload["connected"] = _nano_device is not None
        return JSONResponse(payload)
    async with _nano_lock:
        try:
            dev = await asyncio.to_thread(_nano_require_device)
            payload.update(await asyncio.to_thread(_nano_status_block, dev, bool(full)))
        except NanoError as exc:
            payload["detail"] = str(exc)
            if _nano_device is not None:
                await asyncio.to_thread(_nano_forget, str(exc))   # 旧句柄，下次轮询重连
        except Exception as exc:                                   # noqa: BLE001
            logger.warning("nano: status failed (%s)", exc)
            payload["detail"] = str(exc)
    return JSONResponse(payload)


@app.post("/api/nano/scan")
async def api_nano_scan(request: Request):
    """扫一次。守卫顺序：开关/角色 → 发射中 → 繁忙 → 参数。"""
    global _nano_last_sweep
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    if _nano_tx_active():
        return JSONResponse({"error": "tx_active",
                             "detail": "电台在发射，已锁定扫描"}, status_code=409)
    if _nano_lock.locked():
        return JSONResponse({"error": "busy",
                             "detail": "另一次设备操作正在进行"}, status_code=409)
    try:
        body = await request.json()
        start = int(body["start"])
        stop = int(body["stop"])
        points = int(body.get("points", 401))
        scan_command(start, stop, points)                 # 参数在进锁之前就校验
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)

    async with _nano_lock:
        try:
            dev = await asyncio.to_thread(_nano_require_device)
            await asyncio.to_thread(_nano_capture_state, dev)
            sweep = await asyncio.to_thread(dev.scan, start, stop, points)
            state = _nano_read_state()
            restored = True
            if state.get("start_hz"):
                restored = await asyncio.to_thread(
                    dev.restore_sweep, state["start_hz"], state["stop_hz"], state["points"])
        except NanoError as exc:
            await asyncio.to_thread(_nano_forget, str(exc))
            return JSONResponse({"error": "device_error", "detail": str(exc)},
                                status_code=503)
        except Exception as exc:                                   # noqa: BLE001
            logger.exception("nano: scan failed")
            await asyncio.to_thread(_nano_forget, str(exc))
            return JSONResponse({"error": "device_error", "detail": str(exc)},
                                status_code=503)

    _nano_last_sweep = {"freqs": sweep.freqs, "s11": sweep.s11, "start_hz": start,
                        "stop_hz": stop, "points": points}
    payload = {"freqs": sweep.freqs, "s11": sweep.s11, "elapsed_ms": sweep.elapsed_ms,
               "restored": restored}
    payload.update(analyse(sweep.freqs, sweep.s11))
    return JSONResponse(payload)


NANO_BASELINE_FILE = Path(__file__).parent / "docs" / "nano" / "field-baseline.json"


def _nano_live_snapshot(dev) -> dict:
    """设备当前状态里**可读**的那部分（与基准文件同形状）。"""
    freqs = dev.frequencies()
    return {
        "sweep": {"start_hz": freqs[0], "stop_hz": freqs[-1], "points": len(freqs)},
        "traces": dev.read_traces(),
        "markers": dev.read_markers(),
        "settings": dev.read_settings(),
    }


def _nano_diff(baseline: dict, live: dict) -> list:
    """基准 ↔ 现场：只报"可读且不同"的项，一条一句话。"""
    out = []
    b_sweep = baseline.get("sweep") or {}
    l_sweep = live.get("sweep") or {}
    for key, label in (("start_hz", "扫描起点"), ("stop_hz", "扫描终点"),
                       ("points", "点数")):
        if b_sweep.get(key) != l_sweep.get(key):
            out.append({"field": "sweep." + key, "label": label,
                        "baseline": b_sweep.get(key), "live": l_sweep.get(key)})
    b_traces = {t["id"]: t for t in baseline.get("traces") or []}
    l_traces = {t["id"]: t for t in live.get("traces") or []}
    for tid in sorted(set(b_traces) | set(l_traces)):
        b, l = b_traces.get(tid), l_traces.get(tid)
        if b is None:
            out.append({"field": "traces.%d" % tid, "label": "迹线 %d" % tid,
                        "baseline": None, "live": l.get("type")})
        elif l is None:
            out.append({"field": "traces.%d" % tid, "label": "迹线 %d" % tid,
                        "baseline": b.get("type"), "live": None})
        elif (b.get("type"), b.get("channel")) != (l.get("type"), l.get("channel")):
            out.append({"field": "traces.%d" % tid, "label": "迹线 %d 格式" % tid,
                        "baseline": "%s/%s" % (b.get("type"), b.get("channel")),
                        "live": "%s/%s" % (l.get("type"), l.get("channel"))})
    if (baseline.get("markers") or [{}])[0].get("freq_hz") != \
            (live.get("markers") or [{}])[0].get("freq_hz"):
        out.append({"field": "markers",
                    "label": "标记频率",
                    "baseline": (baseline.get("markers") or [{}])[0].get("freq_hz"),
                    "live": (live.get("markers") or [{}])[0].get("freq_hz")})
    b_set = baseline.get("settings") or {}
    l_set = live.get("settings") or {}
    for key, label in (("smooth", "平滑"), ("power", "功率"),
                       ("bandwidth", "带宽档"), ("edelay_s11_ps", "电延迟")):
        if b_set.get(key) != l_set.get(key):
            out.append({"field": "settings." + key, "label": label,
                        "baseline": b_set.get(key), "live": l_set.get(key)})
    return out


@app.get("/api/nano/baseline")
async def api_nano_baseline(request: Request, compare: int = 0):
    """基准配置（`docs/nano/field-baseline.json`）；`?compare=1` 时与现场对比。"""
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    try:
        baseline = json.loads(NANO_BASELINE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:                                       # noqa: BLE001
        return JSONResponse({"error": "baseline_missing", "detail": str(exc)},
                            status_code=404)
    if not compare:
        return JSONResponse({"baseline": baseline})
    if _nano_lock.locked():
        return JSONResponse({"error": "busy", "baseline": baseline}, status_code=409)
    async with _nano_lock:
        try:
            dev = await asyncio.to_thread(_nano_require_device)
            live = await asyncio.to_thread(_nano_live_snapshot, dev)
        except NanoError as exc:
            await asyncio.to_thread(_nano_forget, str(exc))
            return JSONResponse({"error": "device_error", "detail": str(exc)},
                                status_code=503)
    return JSONResponse({"baseline": baseline, "live": live,
                         "diff": _nano_diff(baseline, live)})


@app.post("/api/nano/marker")
async def api_nano_marker(request: Request):
    """把 1 号标记挪到第 N 个扫描点 —— 图上点一下，设备屏幕上跟着动。"""
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    if _nano_tx_active():
        return JSONResponse({"error": "tx_active"}, status_code=409)
    try:
        body = await request.json()
        index = int(body["index"])
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
    if index < 0 or index > 400:
        return JSONResponse({"error": "bad_request", "detail": "index out of range"},
                            status_code=400)
    if _nano_lock.locked():
        return JSONResponse({"error": "busy"}, status_code=409)
    async with _nano_lock:
        try:
            dev = await asyncio.to_thread(_nano_require_device)
            await asyncio.to_thread(dev.set_marker_index, 0, index)
        except NanoError as exc:
            await asyncio.to_thread(_nano_forget, str(exc))
            return JSONResponse({"error": "device_error", "detail": str(exc)},
                                status_code=503)
    return JSONResponse({"ok": True, "index": index})


@app.get("/api/nano/view")
async def api_nano_view(request: Request):
    """设备屏幕上正在画什么（迹线格式 + 标记）—— 固件里唯一可读回的显示状态。"""
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    if _nano_lock.locked():
        return JSONResponse({"busy": True, "traces": [], "markers": []})
    async with _nano_lock:
        try:
            dev = await asyncio.to_thread(_nano_require_device)
            traces = await asyncio.to_thread(dev.read_traces)
            markers = await asyncio.to_thread(dev.read_markers)
        except NanoError as exc:
            await asyncio.to_thread(_nano_forget, str(exc))
            return JSONResponse({"error": "device_error", "detail": str(exc)},
                                status_code=503)
    return JSONResponse({"traces": traces, "markers": markers,
                         "types": list(nano_vna_types())})


def nano_vna_types() -> tuple:
    from nano.vna import TRACE_TYPES
    return TRACE_TYPES


@app.post("/api/nano/trace")
async def api_nano_trace(request: Request):
    """改设备屏幕上的迹线格式/通道（logmag、swr、smith、phase…）。"""
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    if _nano_tx_active():
        return JSONResponse({"error": "tx_active"}, status_code=409)
    try:
        body = await request.json()
        type_name = str(body.get("type") or "")
        src = int(body.get("src", 0))
        trace_id = int(body.get("id", 0))
        scale = body.get("scale")
        refpos = body.get("refpos")
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
    if _nano_lock.locked():
        return JSONResponse({"error": "busy"}, status_code=409)
    async with _nano_lock:
        try:
            dev = await asyncio.to_thread(_nano_require_device)
            if type_name:
                await asyncio.to_thread(dev.set_trace_type, trace_id, type_name, src)
            if scale is not None:
                await asyncio.to_thread(dev.set_trace_param, trace_id, "scale", float(scale))
            if refpos is not None:
                await asyncio.to_thread(dev.set_trace_param, trace_id, "refpos", float(refpos))
        except ValueError as exc:
            return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
        except NanoError as exc:
            await asyncio.to_thread(_nano_forget, str(exc))
            return JSONResponse({"error": "device_error", "detail": str(exc)},
                                status_code=503)
    return JSONResponse({"ok": True})


@app.post("/api/nano/settings")
async def api_nano_settings(request: Request):
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    if _nano_tx_active():
        return JSONResponse({"error": "tx_active"}, status_code=409)
    if _nano_lock.locked():
        return JSONResponse({"error": "busy"}, status_code=409)
    try:
        body = await request.json()
        name = str(body["name"])
        value = body["value"]
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
    async with _nano_lock:
        try:
            dev = await asyncio.to_thread(_nano_require_device)
            if name.startswith("cfg_"):
                flag = name[4:]
                await asyncio.to_thread(dev.set_config_flag, flag, bool(value))
                flags = _nano_config().get("flags") or {}
                flags[flag] = bool(value)
                _nano_config_update(flags=flags)
                settings = await asyncio.to_thread(dev.read_settings)
            elif name == "saveconfig":
                await asyncio.to_thread(dev.save_config)
                settings = await asyncio.to_thread(dev.read_settings)
            else:
                await asyncio.to_thread(dev.set_setting, name, value)
                settings = await asyncio.to_thread(dev.read_settings)
        except ValueError as exc:
            return JSONResponse({"error": "bad_setting", "detail": str(exc)},
                                status_code=400)
        except NanoError as exc:
            await asyncio.to_thread(_nano_forget, str(exc))
            return JSONResponse({"error": "device_error", "detail": str(exc)},
                                status_code=503)
    return JSONResponse({"ok": True, "settings": settings})


@app.post("/api/nano/device-sweep")
async def api_nano_device_sweep(request: Request):
    """设**设备自己**的扫描范围（独立带出去时它屏幕上扫的就是这一段）。"""
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    if _nano_tx_active():
        return JSONResponse({"error": "tx_active"}, status_code=409)
    try:
        body = await request.json()
        start = int(body["start"]); stop = int(body["stop"]); points = int(body["points"])
        scan_command(start, stop, points)
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
    if _nano_lock.locked():
        return JSONResponse({"error": "busy"}, status_code=409)
    async with _nano_lock:
        try:
            dev = await asyncio.to_thread(_nano_require_device)
            await asyncio.to_thread(dev.set_device_sweep, start, stop, points)
            await asyncio.to_thread(dev.save_config)
        except NanoError as exc:
            await asyncio.to_thread(_nano_forget, str(exc))
            return JSONResponse({"error": "device_error", "detail": str(exc)},
                                status_code=503)
    _nano_write_json(NANO_STATE_FILE,
                     {"start_hz": start, "stop_hz": stop, "points": points})
    return JSONResponse({"ok": True, "start_hz": start, "stop_hz": stop, "points": points})


@app.post("/api/nano/field-preset")
async def api_nano_field_preset(request: Request):
    """一键把设备配成"野外独立测天线"：扫哪段、屏幕上显示 SWR、标记在哪。

    每一项都不是随便定的（页面上的说明文字与这里同源）：
    * 范围 1–54 MHz / 401 点：HF 与 6m 一次覆盖，401 点是这台机器的上限；
    * 迹线 0 = SWR + 刻度 1.0/格 + 参考 0：屏幕上直接读驻波，不用心算 dB；
    * 标记放在扫描中点：拔下来以后转旋钮就能拖到谐振点；
    * 然后 `saveconfig`，否则断电全回默认。
    """
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    if _nano_tx_active():
        return JSONResponse({"error": "tx_active"}, status_code=409)
    try:
        body = await request.json()
    except Exception:                                              # noqa: BLE001
        body = {}
    start = int(body.get("start", 1_000_000))
    stop = int(body.get("stop", 54_000_000))
    points = int(body.get("points", 401))
    try:
        scan_command(start, stop, points)
    except ValueError as exc:
        return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
    if _nano_lock.locked():
        return JSONResponse({"error": "busy"}, status_code=409)
    done: list = []
    async with _nano_lock:
        try:
            dev = await asyncio.to_thread(_nano_require_device)
            await asyncio.to_thread(dev.set_device_sweep, start, stop, points)
            done.append("sweep")
            # 现场只有 CH0 一根天线：S21 / Smith / 相位全是噪声，四条迹线叠在
            # 那块 480×320 屏上反而看不清。先全关，再只开 trace 0 的 S11 SWR。
            await asyncio.to_thread(dev.disable_all_traces)
            done.append("traces: all off")
            await asyncio.to_thread(dev.set_trace_type, 0, "swr", 0)
            done.append("trace0=SWR(S11)")
            try:
                await asyncio.to_thread(dev.set_config_flag, "grid", 1)
                done.append("grid values on")
            except Exception:                                      # noqa: BLE001
                pass
            await asyncio.to_thread(dev.set_trace_param, 0, "scale", 1.0)
            done.append("scale=1.0")
            await asyncio.to_thread(dev.set_trace_param, 0, "refpos", 0.0)
            done.append("refpos=0")
            await asyncio.to_thread(dev.set_marker_index, 0, points // 2)
            done.append("marker=mid")
            await asyncio.to_thread(dev.save_config)
            done.append("saveconfig")
            # 迹线格式不在 config 里（真机实测：saveconfig 救不了它），必须存进
            # SAVE/RECALL 槽；野外开机后 RECALL 这个槽就整套回来。
            await asyncio.to_thread(dev.save_state, FIELD_SLOT)
            done.append("save-%d" % FIELD_SLOT)
        except ValueError as exc:
            return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
        except NanoError as exc:
            await asyncio.to_thread(_nano_forget, str(exc))
            return JSONResponse({"error": "device_error", "detail": str(exc),
                                 "done": done}, status_code=503)
    _nano_write_json(NANO_STATE_FILE,
                     {"start_hz": start, "stop_hz": stop, "points": points,
                      "field_slot": FIELD_SLOT})
    _nano_config_update(field_slot=FIELD_SLOT)
    return JSONResponse({"ok": True, "done": done, "slot": FIELD_SLOT,
                         "start_hz": start, "stop_hz": stop, "points": points})


@app.post("/api/nano/cal")
async def api_nano_cal(request: Request):
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    if _nano_tx_active():
        return JSONResponse({"error": "tx_active"}, status_code=409)
    try:
        body = await request.json()
        action = str(body.get("action", "recall"))
        slot = int(body["slot"])
        confirm = body.get("confirm")
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
    if action == "save" and str(confirm or "") != "SAVE":
        return JSONResponse({"error": "confirm_required",
                             "detail": "写入标定槽会覆盖设备里已有的那一组，需要输入 SAVE"},
                            status_code=400)
    if action not in ("save", "recall"):
        return JSONResponse({"error": "bad_request", "detail": "action must be save|recall"},
                            status_code=400)
    if _nano_lock.locked():
        return JSONResponse({"error": "busy"}, status_code=409)
    async with _nano_lock:
        try:
            dev = await asyncio.to_thread(_nano_require_device)
            if action == "save":
                await asyncio.to_thread(dev.save_cal, slot)
            else:
                await asyncio.to_thread(dev.recall_cal, slot)
            cal = await asyncio.to_thread(dev.cal_state)
        except ValueError as exc:
            return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
        except NanoError as exc:
            await asyncio.to_thread(_nano_forget, str(exc))
            return JSONResponse({"error": "device_error", "detail": str(exc)},
                                status_code=503)
    return JSONResponse({"ok": True, "cal": cal})


@app.get("/api/nano/sweeps")
async def api_nano_sweeps(request: Request):
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    return JSONResponse({"sweeps": await asyncio.to_thread(_nano_list_sweeps)})


@app.post("/api/nano/sweeps")
async def api_nano_save_sweep(request: Request):
    """把最近一次扫描存盘。只存原始 S11 —— 能算出来的东西不存第二份。"""
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    if _nano_last_sweep is None:
        return JSONResponse({"error": "no_sweep", "detail": "还没有扫描过"}, status_code=409)
    try:
        body = await request.json()
    except Exception:                                              # noqa: BLE001
        body = {}
    label = str(body.get("label") or "").strip()[:80]
    note = str(body.get("note") or "").strip()[:400]
    sweep = _nano_last_sweep
    now = datetime.now(timezone.utc)
    sid = "sweep_%d-%d_%d_%s" % (round(sweep["start_hz"] / 1e6),
                                 round(sweep["stop_hz"] / 1e6),
                                 sweep["points"], now.strftime("%Y%m%d-%H%M%S"))
    payload = {
        "version": 1,
        "id": sid,
        "created": now.isoformat(timespec="seconds"),
        "label": label,
        "note": note,
        "start_hz": sweep["start_hz"],
        "stop_hz": sweep["stop_hz"],
        "points": sweep["points"],
        "device": {
            "board": (_nano_device.info().board if _nano_device else ""),
            "version": (_nano_device.info().version if _nano_device else ""),
            "port": (_nano_device.port if _nano_device else ""),
        },
        "s11": [[re, im] for re, im in sweep["s11"]],
    }
    await asyncio.to_thread(_nano_write_json, NANO_SWEEPS_DIR / (sid + ".json"), payload)
    return JSONResponse({"ok": True, "id": sid})


@app.get("/api/nano/sweeps/{sid}")
async def api_nano_get_sweep(sid: str, request: Request, format: str = "json"):
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    path = _nano_sweep_path(sid)
    if path is None:
        raise HTTPException(status_code=404, detail="sweep not found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                              # noqa: BLE001
        raise HTTPException(status_code=500, detail="sweep unreadable")
    freqs = _nano_sweep_freqs(data)
    s11 = [(float(p[0]), float(p[1])) for p in data.get("s11", [])]
    if format == "csv":
        return PlainTextResponse(to_csv(freqs, s11), media_type="text/csv",
                                 headers={"Content-Disposition":
                                          'attachment; filename="%s.csv"' % sid})
    if format == "s1p":
        return PlainTextResponse(to_s1p(freqs, s11), media_type="application/octet-stream",
                                 headers={"Content-Disposition":
                                          'attachment; filename="%s.s1p"' % sid})
    payload = dict(data)
    payload.update(analyse(freqs, s11))
    if data.get("freqs") is None:
        payload["freqs"] = freqs
    return JSONResponse(payload)


@app.delete("/api/nano/sweeps/{sid}")
async def api_nano_delete_sweep(sid: str, request: Request):
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    path = _nano_sweep_path(sid)
    if path is None:
        raise HTTPException(status_code=404, detail="sweep not found")
    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return JSONResponse({"ok": True})


def _nano_sweep_freqs(data: dict) -> list[int]:
    """扫描文件里没有存频率表时就按 (start, stop, points) 重建。

    重建是**精确**的：设备的频率表是线性插值取整（固件 set_frequencies），
    与这里同一个公式，所以不必为省 400 个整数去牺牲文件的独立性。
    """
    if isinstance(data.get("freqs"), list):
        return [int(v) for v in data["freqs"]]
    start = int(data.get("start_hz") or 0)
    stop = int(data.get("stop_hz") or 0)
    points = int(data.get("points") or 0)
    if points < 2:
        return []
    return [start + int(round((stop - start) * i / (points - 1))) for i in range(points)]

# ── NanoVNA 固件升级（spec §4.10 / AD-030，U3：默认上锁）────────────────
#
# `reset dfu` 跳进 STM32 系统存储区里的 ROM bootloader —— 那段代码不可擦写，所以
# 刷坏只会停在 DFU、再刷即恢复，不会变砖。但 H/H4 有 MS/SI/ZK 三个**编译期**变体，
# 运行时读不出来，所以变体必须人来选、记住，成功判据是刷后设备自报的版本。
#
# 默认上锁：`MRRC_NANO_ALLOW_FLASH=1` 才出现升级入口。
NANO_FIRMWARE_DIR = NANO_DIR / "firmware"
NANO_FIRMWARE_STATE = NANO_DIR / "firmware-state.json"
NANO_CONFIG_FILE = NANO_DIR / "config.json"
NANO_RELEASES_CACHE = NANO_DIR / "releases.json"
NANO_RELEASES_TTL_S = 6 * 3600

# 野外配置存进哪个 SAVE/RECALL 槽。选 4 是因为它是最后一个，用户自己的槽不会占到。
FIELD_SLOT = 4

_nano_flash_state: dict = {"state": "idle", "detail": ""}
_nano_flash_task: Optional[asyncio.Task] = None


def _nano_config() -> dict:
    try:
        return json.loads(NANO_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:                                              # noqa: BLE001
        return {}


def _nano_config_update(**kw) -> dict:
    cfg = _nano_config()
    cfg.update(kw)
    try:
        _nano_write_json(NANO_CONFIG_FILE, cfg)
    except OSError as exc:
        logger.info("nano: cannot persist config (%s)", exc)
    return cfg


def _nano_flash_gate(request: Request) -> Optional[JSONResponse]:
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    if not NANO_ALLOW_FLASH:
        return JSONResponse({"error": "flash_disabled",
                             "detail": "设置 MRRC_NANO_ALLOW_FLASH=1 并重启服务后才能升级固件"},
                            status_code=403)
    return None


def _nano_releases(force: bool = False) -> list:
    """官方 releases（6 小时缓存）。任何失败都退化成缓存，不假装网络一定在。"""
    if not force:
        try:
            cached = json.loads(NANO_RELEASES_CACHE.read_text(encoding="utf-8"))
            if time.time() - float(cached.get("fetched", 0)) < NANO_RELEASES_TTL_S:
                return nano_flash.parse_releases(cached.get("releases"))
        except Exception:                                          # noqa: BLE001
            pass
    try:
        import net_tls
        with net_tls.urlopen(nano_flash.RELEASES_URL, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:                                       # noqa: BLE001
        logger.info("nano: release list unavailable (%s)", exc)
        try:
            cached = json.loads(NANO_RELEASES_CACHE.read_text(encoding="utf-8"))
            return nano_flash.parse_releases(cached.get("releases"))
        except Exception:                                          # noqa: BLE001
            return []
    try:
        _nano_write_json(NANO_RELEASES_CACHE,
                         {"fetched": time.time(), "releases": payload})
    except OSError:
        pass
    return nano_flash.parse_releases(payload)


def _nano_self_check(dev, points: int = 101) -> dict:
    """刷后自检：开路端口的三点扫描。**建议值，不是判据**。"""
    sweep = dev.scan(1_000_000, 54_000_000, points)
    import math
    mags = [math.hypot(re, im) for re, im in sweep.s11]
    idx = [min(range(len(sweep.freqs)), key=lambda i: abs(sweep.freqs[i] - f))
           for f in (3_500_000, 14_000_000, 50_000_000)]
    return {"s11_abs": [round(mags[i], 3) for i in idx],
            "freqs": [sweep.freqs[i] for i in idx]}


def _nano_flash_blocking(spec: dict) -> None:
    """跑一次升级（在线程里）。结束时把快照留在 `_nano_flash_state`。"""
    global _nano_flash_state
    variant = spec["variant"]
    release = None
    for rel in _nano_releases():                     # 网络部分放在线程里
        if rel.tag == spec["tag"]:
            release = rel
            break
    if release is None:
        _nano_flash_state = {"state": "failed",
                             "detail": "找不到版本 %s 的发布" % spec["tag"]}
        return
    asset = nano_flash.pick_asset(release, variant)
    if asset is None:
        _nano_flash_state = {"state": "failed",
                             "detail": "该发布里没有变体 %s 的镜像" % variant}
        return
    try:
        image = nano_flash.download_asset(asset, NANO_FIRMWARE_DIR)
    except Exception as exc:                                       # noqa: BLE001
        _nano_flash_state = {"state": "failed", "detail": "取镜像失败：%s" % exc}
        return

    dev = _nano_connect()
    if dev is None:
        _nano_flash_state = {"state": "failed", "detail": "设备不在线"}
        return

    def enter_dfu() -> None:
        dev.enter_dfu()
        _nano_forget("entering DFU")

    def read_version() -> str:
        fresh = _nano_connect()
        if fresh is None:
            raise nano_flash.FlashError("设备没有回到应用模式")
        return fresh.version()

    def self_check() -> dict:
        fresh = _nano_connect()
        if fresh is None:
            raise nano_flash.FlashError("设备不在线")
        result = _nano_self_check(fresh)
        if spec.get("ch0_open"):
            worst = min(result["s11_abs"]) if result["s11_abs"] else 1.0
            result["variant_suspect"] = worst < 0.7
            result["ch0_open"] = True
        return result

    hooks = nano_flash.FlashHooks(
        enter_dfu=enter_dfu,
        wait_dfu=lambda: nano_flash.wait_for_usb(*nano_flash.DFU_VID_PID),
        run_dfu=lambda img: nano_flash.run_dfu(img),
        wait_app=lambda: nano_flash.wait_for_usb(*nano_flash.NANO_VID_PID,
                                                 timeout=nano_flash.APP_RETURN_S),
        read_version=read_version,
        self_check=self_check,
    )
    job = nano_flash.FlashJob(image=image, expected_version=release.tag, hooks=hooks)
    job.state = "checking"
    _nano_flash_state = job.snapshot()
    snap = job.run()
    _nano_flash_state = snap
    try:
        nano_flash.record(NANO_FIRMWARE_STATE, snap, variant=variant,
                          tag=release.tag, sha256=asset.sha256)
    except OSError as exc:
        logger.info("nano: cannot write flash proof (%s)", exc)
    _nano_forget("after flashing")                   # 下次按需重连，拿到新固件


async def _nano_flash_worker(spec: dict) -> None:
    global _nano_flash_state
    try:
        async with _nano_lock:
            await asyncio.to_thread(_nano_flash_blocking, spec)
    except Exception as exc:                                       # noqa: BLE001
        logger.exception("nano flash: worker crashed")
        _nano_flash_state = {"state": "failed", "detail": str(exc)}


@app.get("/api/nano/firmware")
async def api_nano_firmware(request: Request, refresh: int = 0):
    """可用版本 + 本地镜像 + 变体记忆。读操作，不需要 flash 开关。"""
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    releases = await asyncio.to_thread(_nano_releases, bool(refresh))
    cfg = _nano_config()
    installed = None
    if _nano_device is not None:
        try:
            installed = _nano_device.version()
        except Exception:                                          # noqa: BLE001
            installed = None
    local = []
    if NANO_FIRMWARE_DIR.is_dir():
        local = sorted(p.name for p in NANO_FIRMWARE_DIR.glob("*"))
    return JSONResponse({
        "enabled": NANO_ALLOW_FLASH,
        "variant": cfg.get("variant"),
        "variants": {k: v["label"] for k, v in nano_flash.VARIANTS.items()},
        "installed_version": installed,
        "releases": [{"tag": r.tag, "published": r.published,
                      "assets": [a.name for a in r.assets]} for r in releases[:8]],
        "local_images": local,
        "job": _nano_flash_state,
    })


@app.post("/api/nano/firmware/variant")
async def api_nano_variant(request: Request):
    gate = _nano_flash_gate(request)
    if gate is not None:
        return gate
    try:
        body = await request.json()
        variant = str(body["variant"])
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
    if variant not in nano_flash.VARIANTS:
        return JSONResponse({"error": "bad_request", "detail": "unknown variant"},
                            status_code=400)
    _nano_config_update(variant=variant)
    return JSONResponse({"ok": True, "variant": variant})


@app.post("/api/nano/flash")
async def api_nano_flash(request: Request):
    """启动一次升级。确认字必须真的打出来 —— 这不是一个可以误触的按钮。"""
    global _nano_flash_task, _nano_flash_state
    gate = _nano_flash_gate(request)
    if gate is not None:
        return gate
    if _nano_tx_active():
        return JSONResponse({"error": "tx_active",
                             "detail": "电台在发射，先停下来"}, status_code=409)
    if _nano_flash_task is not None and not _nano_flash_task.done():
        return JSONResponse({"error": "busy", "detail": "已经有一次升级在进行"},
                            status_code=409)
    try:
        body = await request.json()
        tag = str(body["tag"]).strip()
        variant = str(body["variant"]).strip()
        confirm = str(body.get("confirm") or "")
        ch0_open = bool(body.get("ch0_open"))
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
    if variant not in nano_flash.VARIANTS:
        return JSONResponse({"error": "bad_request", "detail": "unknown variant"},
                            status_code=400)
    if confirm != "FLASH":
        return JSONResponse({"error": "confirm_required",
                             "detail": "升级会重启设备，请输入 FLASH 确认"}, status_code=400)
    _nano_config_update(variant=variant)
    _nano_flash_state = {"state": "checking", "detail": "准备 %s (%s)" % (tag, variant)}
    _nano_flash_task = asyncio.create_task(
        _nano_flash_worker({"tag": tag, "variant": variant, "ch0_open": ch0_open}))
    return JSONResponse({"ok": True, "state": "checking", "tag": tag, "variant": variant})


@app.get("/api/nano/flash/status")
async def api_nano_flash_status(request: Request):
    gate = _nano_gate(request)
    if gate is not None:
        return gate
    payload = dict(_nano_flash_state)
    payload["enabled"] = NANO_ALLOW_FLASH
    payload["running"] = bool(_nano_flash_task and not _nano_flash_task.done())
    return JSONResponse(payload)

async def serve_static(path: str, request: Request):
    """Serve static files.  Index.html is served for SPA routes.

    Registered with ``app.add_api_route`` at the **end** of this module, not here: this handler
    matches every GET path, so anything registered after it is unreachable. Defining it here and
    registering it below all API routes is what keeps that order obvious.

    Caching (2026-10-17): every response carries a ``Cache-Control`` and honours
    ``If-None-Match``/``If-Modified-Since``. Before this, an installed browser revalidated on every
    reload and got the whole file back — ``If-None-Match`` on ``modules/opus_wasm.js`` (534 KB)
    returned 200 with the full body, so "reload" meant "push 534 KB through the tunnel again".
    """
    if not _verify_auth(request):
        # 已登录的 SDR 会话要能取它自己页面的资源（sdr.html 用根相对路径，
        # 与 index.html 同一套约定）；除此之外仍旧回登录页。
        if not _sdr_static_allowed(request, path):
            if path not in ("", "login", "favicon.png", "manifest.json", "sw.js", "box_api.js"):
                return RedirectResponse("/login")

    file_path = _resolve_static_path(path)
    if file_path is None:
        # Escapes STATIC_DIR (path traversal) or unresolvable — never fall
        # through to the SPA fallback for these.
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)

    if not file_path.is_file():
        # A file name that is not on disk is a 404, not the SPA fallback: `/favicon.png`
        # (referenced by index.html and manifest.json) does not exist, and the fallback
        # answered it with 40 KB of HTML that the browser kept as if it were the icon.
        # Only extension-less paths are SPA routes (/settings, /ft8, a deep link).
        if Path(path).suffix:
            return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)
        file_path = STATIC_DIR / "index.html"
        if not file_path.exists():
            return HTMLResponse("<h1>MRRC Modern</h1><p>Static files not found.</p>", status_code=404)

    ext = file_path.suffix.lower()
    content_types = {
        ".html": "text/html", ".css": "text/css", ".js": "application/javascript",
        ".json": "application/json", ".png": "image/png", ".svg": "image/svg+xml",
        ".ico": "image/x-icon", ".wav": "audio/wav", ".mp3": "audio/mpeg",
    }
    media_type = content_types.get(ext, "application/octet-stream")
    response = FileResponse(
        file_path,
        media_type=media_type,
        # stat passed in on purpose: Starlette 1.3's FileResponse only stats the file
        # when it is sent, and the conditional check below has to run *before* that.
        stat_result=file_path.stat(),
    )
    response.headers["Cache-Control"] = _cache_control_for(file_path)
    if _not_modified(request, response.headers):
        headers = {name: response.headers[name]
                   for name in ("etag", "last-modified", "cache-control")
                   if name in response.headers}
        return Response(status_code=304, headers=headers)
    return response


# ── Entry Point ─────────────────────────────────────────────────────

# EADDRINUSE is 98 on Linux and 48 on macOS; Windows reports 10048 (WSAEADDRINUSE)
# and does not put it in errno's map under some Python builds, so all three are listed.
_ADDR_IN_USE_ERRNOS = frozenset({errno.EADDRINUSE, 98, 48, 10048})


def _is_addr_in_use(exc: OSError) -> bool:
    """Whether this bind failure means " somebody else holds this port"."""
    return getattr(exc, "errno", None) in _ADDR_IN_USE_ERRNOS


def _set_bind_exclusion(sock: "socket.socket", *, windows: bool) -> None:
    """Ask the OS for the bind semantics this app needs, per platform.

    On Windows ``SO_REUSEADDR`` does not mean what it means on POSIX: it lets a second
    process bind a port another process is already *listening* on. Two MRRC Modern
    instances then start side by side, both log "Server ready!", and the OS splits
    incoming connections between them. The 2026-10-02 field log is that exact picture —
    every line written twice, ~1 ms apart, two stacks each enumerating the radio's audio
    devices and opening its CAT port. Whichever instance the browser landed on might be
    the one that never got the radio, and the operator saw a console that would not
    answer. So on Windows the listener asks for ``SO_EXCLUSIVEADDRUSE`` instead and a
    second instance fails immediately and visibly; POSIX keeps ``SO_REUSEADDR``, which is
    what lets a restart rebind while the old socket is in TIME_WAIT.
    """
    if windows:
        # The constant only exists on Windows; the literal is its value there, kept so the
        # intent survives a socket module that does not export it.
        sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", 1024), 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def _already_running_hint(port: int, exc: OSError) -> str:
    """The message a user can act on when the bind fails because of contention."""
    return (
        f"cannot listen on port {port}: it is already taken ({exc}). "
        f"Another MRRC Modern is most likely still running — close its server window "
        f"(or exit it from the tray) and start it again. Starting a second copy is not "
        f"a fix: the two would fight over the port and over the radio's serial port."
    )


def _bind_dual_stack_socket(port: int, *, retries: int = 10,
                            retry_delay_s: float = 0.3) -> "socket.socket":
    """Bind :: as a dual-stack socket (IPv4 + IPv6).

    asyncio's loop.create_server(host="::") creates an IPv6-only socket
    (IPV6_V6ONLY=1), which refuses IPv4 — and the launcher opens the browser
    at http://127.0.0.1:8888. Pre-binding with V6ONLY=0 and handing the
    socket to uvicorn.Server.run(sockets=[...]) keeps both stacks working.

    A port already in use is retried briefly (the launcher restarts this exe in-process on
    a config change, and the previous listener can still be on its way out) and then raises
    rather than reporting success: binding is the one place where "keep going anyway"
    produced a server that answered nobody.
    """
    def make() -> socket.socket:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        _set_bind_exclusion(sock, windows=(os.name == "nt"))
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        return sock

    return _bind_with_retry(make, ("::", port), port,
                            retries=retries, retry_delay_s=retry_delay_s)


def _bind_with_retry(make_socket: Callable[[], socket.socket], addr: tuple, port: int, *,
                     retries: int, retry_delay_s: float) -> socket.socket:
    """Bind and listen, retrying a taken port briefly and then failing loudly.

    One implementation for every host, so no start-up path can skip the guard. The retry
    covers the launcher restarting this exe in-process on a config change while the
    previous listener is still on its way out; the final raise is what turns "two servers
    on one port, and the browser landed on the one without the radio" into a message that
    names the port and the likely cause. Binding is the one place where "keep going
    anyway" produced a server that answered nobody.
    """
    last: OSError | None = None
    for attempt in range(retries):
        sock = make_socket()
        try:
            sock.bind(addr)
            sock.listen(2048)
            return sock
        except OSError as exc:
            sock.close()
            last = exc
            if not _is_addr_in_use(exc):
                raise
            if attempt + 1 < retries:
                time.sleep(retry_delay_s)
    raise RuntimeError(_already_running_hint(port, last)) from last


def _bind_listener_socket(host: str, port: int, *, retries: int = 10,
                          retry_delay_s: float = 0.3) -> socket.socket:
    """The listening socket for ANY configured host, guarded the same way.

    An explicit host used to be handed to ``uvicorn.run(host=...)`` instead, which on
    Windows neither asks for ``SO_EXCLUSIVEADDRUSE`` nor says anything an operator can act
    on. Verified on the packaged v1.24.6 exe with a clean per-user state: a second
    instance first logged ``Server ready!`` (the app's startup event fires before uvicorn
    binds) and only then failed with
    ``ERROR: [Errno 10048] error while attempting to bind on address ('127.0.0.1', 18892)``,
    i.e. the OS refused it, not this code — no retry for the restart race and no hint that
    another instance is running. ``MRRC_WEB_HOST=127.0.0.1`` is exactly what the
    2026-10-02 field machine was configured with, so that was the path in use.

    The wildcard keeps the dual-stack socket (see ``_bind_dual_stack_socket``); a specific
    address binds only itself, and a specific IPv6 address stays V6ONLY so it does not also
    claim the IPv4 wildcard.
    """
    if host in ("::", ""):
        return _bind_dual_stack_socket(port, retries=retries, retry_delay_s=retry_delay_s)

    family = socket.AF_INET6 if ":" in host else socket.AF_INET

    def make() -> socket.socket:
        sock = socket.socket(family, socket.SOCK_STREAM)
        _set_bind_exclusion(sock, windows=(os.name == "nt"))
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        return sock

    return _bind_with_retry(make, (host, port), port,
                            retries=retries, retry_delay_s=retry_delay_s)


def _resolve_ssl_kwargs(args) -> dict:
    """uvicorn's ``ssl_*`` keyword arguments for this start-up.

    ``{}`` means plain HTTP, and plain HTTP is only ever what ``--no-ssl`` explicitly asked for:
    the launcher opens an https:// URL, so a server that quietly degraded to HTTP showed the
    browser a protocol error and the UI never appeared — reported from a real machine as
    "installed, then a black screen". A missing certificate is therefore signed here, the same
    thing the launcher does on a fresh install.

    This lives in a function of its own on purpose. Inlined in ``main()`` it was unreachable from
    the suite, so a missing ``import ssl_bootstrap`` — a bare NameError swallowed by the broad
    ``except`` below — survived 1460 green tests, a successful PyInstaller/iscc build and a
    matching SHA-256 to ship broken in v1.24.5, the very release meant to fix this path. Only
    running the packaged Server exe against a clean per-user state exposed it.
    """
    if args.no_ssl:
        logger.warning("SSL disabled by request (--no-ssl): serving plain HTTP")
        return {}
    if os.path.exists(args.ssl_cert) and os.path.exists(args.ssl_key):
        logger.info("SSL enabled: %s", args.ssl_cert)
        return {"ssl_certfile": args.ssl_cert, "ssl_keyfile": args.ssl_key}
    try:
        cert_dir = Path(args.ssl_cert).parent
        cert_dir.mkdir(parents=True, exist_ok=True)
        host = args.host or "127.0.0.1"
        if host in ("0.0.0.0", "::", "[::]"):
            host = "localhost"
        pair = ssl_bootstrap.sign_for(host, cert_dir)
        cert_path, key_path = (pair if isinstance(pair, tuple) else (pair, None))
        if key_path is None:
            raise RuntimeError("sign_for did not return a key path")
        logger.info("SSL enabled with a self-signed certificate just generated: %s", cert_path)
        return {"ssl_certfile": str(cert_path), "ssl_keyfile": str(key_path)}
    except Exception as exc:                                      # noqa: BLE001
        logger.error("could not create a certificate (%s) - falling back to plain HTTP", exc)
        logger.error("open http://%s:%s/ in the browser (NOT https://)", args.host, args.port)
        return {}


def main():
    """Parse CLI args and start the server.

    Environment variables are set BEFORE importing the app so that
    config.py picks up the correct values at module load time.
    """
    import argparse
    parser = argparse.ArgumentParser(description="MRRC Modern Web Control Server")
    parser.add_argument("--port", type=int, default=WEB_PORT, help=f"Web server port (default: {WEB_PORT})")
    parser.add_argument("--serial-port", default=SERIAL_PORT, help=f"Serial port (default: {SERIAL_PORT})")
    parser.add_argument("--baud", type=int, default=BAUD_RATE, help=f"Baud rate (default: {BAUD_RATE})")
    parser.add_argument("--password", default=WEB_PASSWORD, help="Web login password")
    parser.add_argument("--host", default=WEB_HOST, help=f"Bind address (default: {WEB_HOST})")
    parser.add_argument("--ssl-cert", default=SSL_CERTFILE, help=f"SSL cert file (default: {SSL_CERTFILE})")
    parser.add_argument("--ssl-key", default=SSL_KEYFILE, help=f"SSL key file (default: {SSL_KEYFILE})")
    parser.add_argument("--no-ssl", action="store_true", help="Disable SSL (use plain HTTP)")
    args = parser.parse_args()

    # Set env vars BEFORE uvicorn imports the app module
    os.environ["MRRC_SERIAL_PORT"] = args.serial_port
    os.environ["MRRC_BAUD_RATE"] = str(args.baud)
    os.environ["MRRC_WEB_PORT"] = str(args.port)
    # A port name from another operating system can never work here: the macOS default shipped in
    # config.py used to apply everywhere, so a Windows install pointed at /dev/cu.SLAB_USBtoUART
    # while the radio sat on COM5 (seen in a screenshot of a real console). Clear it so the first-run
    # detection does its job, and say so.
    _port = str(getattr(args, "serial_port", "") or "")
    _wrong_os = (os.name == "nt" and _port.startswith("/dev/")) or (os.name != "nt" and _port.upper().startswith("COM"))
    if _wrong_os:
        logger.warning("serial port %r does not exist on this system - ignoring it and detecting instead", _port)
        args.serial_port = ""

    os.environ["MRRC_WEB_PASSWORD"] = args.password
    _ensure_strong_password()
    os.environ["MRRC_WEB_HOST"] = args.host

    # SSL configuration (see _resolve_ssl_kwargs for why this is not inline)
    ssl_kwargs = _resolve_ssl_kwargs(args)
    # Remember what that answer means, so _cert_reload_required can tell whether the certificate
    # enrolled later is the one being served.
    _record_serving_cert(ssl_kwargs)

    # Pass the app object (not the "server:app" import string) so frozen
    # PyInstaller builds work — a frozen exe cannot re-import the "server"
    # module by name.
    # Bind here for EVERY host instead of letting uvicorn do it: the pre-bind is what asks
    # for SO_EXCLUSIVEADDRUSE on Windows, retries the restart race, and turns a taken port
    # into a message naming the other instance. Config still carries host/port so the
    # "Uvicorn running on ..." line matches the socket that is really listening.
    # An instance that is already connected must serve its entry after a restart, without
    # anyone opening the settings dialog: the tunnel used to start only from /api/cloud/state,
    # so a rebooted machine answered 502 until somebody clicked.
    try:
        _cloud_start_tunnel(_cloud_settings())
    except Exception as exc:                                      # noqa: BLE001
        logger.warning("could not start the tunnel at start-up (%s) - the dialog retries", exc)
    # An application submitted before this restart still has to finish by itself; without this
    # the approval would wait for somebody to open the settings dialog.
    try:
        _cloud_start_autoconnect()
    except Exception as exc:                                      # noqa: BLE001
        logger.warning("could not start the cloud auto-connect poller (%s)", exc)

    # Every box ships with two fallback resolvers on each saved profile. A router that hands
    # out no DNS - or a DHCP answer that arrives after the first lookups - otherwise leaves
    # the box unable to resolve *anything*, and the symptom is never "no DNS": measured
    # 2026-10-25, an empty resolver at boot kept chrony from resolving its NTP pool, the
    # clock stayed 24 days behind the image date, and every HTTPS call failed with
    # "certificate is not yet valid". Runs off the loop because it shells out to nmcli.
    threading.Thread(target=_ensure_fallback_dns, name="dns-fallback", daemon=True).start()

    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port,
                                           log_level="info", reload=False, **ssl_kwargs))
    server.run(sockets=[_bind_listener_socket(args.host, args.port)])



# ---------------------------------------------------------------------------
# Cloud Hub onboarding. The tenant-side flow used to be a shell script; it now lives in
# cloud_hub.py and is driven from the settings dialog. State is kept in the same config file the
# launcher reads on every start, so a reboot does not undo it.
# ---------------------------------------------------------------------------
_cloud_tunnel: "cloud_hub.TunnelProcess | None" = None
_cloud_lock = threading.Lock()

#: 隧道没起来的原因，一句能直接摆在设置页上的话（空 = 没有已知问题：要么在跑，要么还没人试过）。
#: 为什么不是直接用 ``TunnelProcess.last_error``：那一个只有在**进程起过**之后才存在，而这里要
#: 说的是「进程根本没起来」—— ``fleet/frpc`` 或 ``fleet/frpc-<label>.toml`` 缺失。那一种此前
#: 一句话都不留（见 CloudTunnelNotStartedTests 的现场记录）。
_cloud_tunnel_problem = ""


def _note_tunnel_problem(problem: str) -> None:
    """记下隧道为什么没跑，并且**每种原因只在日志里说一次**。

    调用点是 ``_cloud_start_tunnel()``，而 ``/api/cloud/state`` 每次轮询都会走它（设置页每 30 秒
    刷一次状态），所以警告必须按内容去重：一次缺失说一遍，别把日志刷满。
    """
    global _cloud_tunnel_problem
    if problem == _cloud_tunnel_problem:
        return
    _cloud_tunnel_problem = problem
    if problem:
        logger.warning("cloud hub: the tunnel is not running - %s "
                       "(the public entry answers 502 until this is fixed)", problem)

_CLOUD_KEYS = ("MRRC_CLOUD_CALLSIGN", "MRRC_CLOUD_TOKEN", "MRRC_CLOUD_LABEL",
               "MRRC_CLOUD_PORT", "MRRC_CLOUD_ENTRY", "MRRC_CLOUD_PORTAL")


def _cloud_fleet_dir() -> Path:
    """Where the installer put frpc/openssl. Windows: <app>/fleet; macOS: Resources/payload."""
    for cand in (_runtime_dir() / "fleet", _resource_dir() / "payload", _resource_dir() / "fleet"):
        if cand.exists():
            return cand
    return _runtime_dir() / "fleet"


def _cloud_user_dir() -> Path:
    """The per-user data directory the launcher points MRRC_LOG_DIR at."""
    return LOG_DIR.parent


def _cloud_cert_dir() -> Path:
    return _cloud_user_dir() / "certs"


def _cloud_data_dir() -> Path:
    return _cloud_user_dir() / "fleet"


def _cloud_settings() -> dict:
    """Cloud Hub values as stored in the app's config file (fresh read, not import-time)."""
    out = {}
    try:
        for raw in _config_file_path().read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() in _CLOUD_KEYS:
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _cloud_portal(settings: dict) -> str:
    return settings.get("MRRC_CLOUD_PORTAL") or cloud_hub.PORTAL_DEFAULT


def _ensure_tunnel_object():
    """Create the supervisor if it does not exist yet. connect() starts it."""
    global _cloud_tunnel
    if _cloud_tunnel is None:
        fleet = _cloud_fleet_dir()
        frpc = fleet / ("frpc.exe" if os.name == "nt" else "frpc")
        with _cloud_lock:
            if _cloud_tunnel is None:
                _cloud_tunnel = cloud_hub.TunnelProcess(frpc, _cloud_data_dir() / "pending.toml")
    return _cloud_tunnel


# ---------------------------------------------------------------------------
# Remote help (`call help`): the customer's own door, implemented in help_session.py and
# reachable only while they hold it open. The three collaborators this file supplies are the
# portal (request/heartbeat/close), the tunnel (add/remove one proxy) and the clock.
# ---------------------------------------------------------------------------
_help_singleton: "help_session.HelpSession | None" = None


def _help_portal_context() -> tuple:
    """(portal, callsign, token), or a CloudHubError the UI can show verbatim."""
    settings = _cloud_settings()
    callsign = settings.get("MRRC_CLOUD_CALLSIGN", "")
    token = settings.get("MRRC_CLOUD_TOKEN", "")
    if not (callsign and token):
        raise cloud_hub.CloudHubError(
            "这台机器还没接入 Cloud Hub —— 远程协助需要在门户上有身份")
    return _cloud_portal(settings), callsign, token


def _help_conf_path() -> Path:
    """The running tunnel's config, or the one it would use for this label."""
    if _cloud_tunnel is not None:
        return Path(_cloud_tunnel.conf)
    label = _cloud_settings().get("MRRC_CLOUD_LABEL", "")
    return _cloud_data_dir() / f"frpc-{label}.toml"


def _help_opener(minutes: int) -> int:
    portal, callsign, token = _help_portal_context()
    username = (_help_singleton.status().get("username", "") if _help_singleton else "")
    reply = cloud_hub.help_request(portal, callsign, token, minutes, username)
    port = int(reply.get("port") or 0)
    if not port:
        raise cloud_hub.CloudHubError(f"门户没有分配协助端口：{reply}")
    return port


def _help_tunnel(local_port: int, remote_port: int, add: bool) -> None:
    conf = _help_conf_path()
    if add:
        cloud_hub.set_help_proxy(conf, help_local_port=local_port,
                                 help_remote_port=remote_port)
    else:
        cloud_hub.set_help_proxy(conf, remove=True)


def _help_portal_closer() -> None:
    portal, callsign, token = _help_portal_context()
    cloud_hub.help_close(portal, callsign, token)


def _help_portal_beat(port: int, connections: int) -> dict:
    """20 秒一次的心跳，也是"门户强制结束"到达实例的唯一通道。

    失败**不抛给调用方**：help_session 把这一跳记成一次失败就继续，窗口仍然按自己的
    截止时刻关闭（那里才是它的权威）。
    """
    portal, callsign, token = _help_portal_context()
    return cloud_hub.help_heartbeat(portal, callsign, token, port, connections)


def _help() -> help_session.HelpSession:
    """The process's one help window (created on first use)."""
    global _help_singleton
    if _help_singleton is None:
        _help_singleton = help_session.build_session(
            portal_opener=_help_opener, tunnel=_help_tunnel,
            portal_closer=_help_portal_closer, portal_beat=_help_portal_beat,
            on_event=lambda ev: logger.info("help: %s", ev))
    return _help_singleton


@app.post("/api/help/enable", include_in_schema=False)
async def api_help_enable(request: Request):
    """Open a help window. Body: {"minutes": 60, "username": "", "password": ""}.

    A **local** action: whoever can use this UI is the person who decides. The
    password is returned to that same caller and to nobody else.
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        session = _help()
        status = await asyncio.to_thread(
            lambda: session.enable(
                minutes=int(body.get("minutes") or help_session.DEFAULT_WINDOW_MINUTES),
                username=str(body.get("username") or ""),
                password=str(body.get("password") or "")))
    except Exception as exc:
        logger.warning("help: cannot open a session: %s", exc)
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return JSONResponse(dict(status, ok=True, password=session.password_for_display()))


@app.post("/api/help/disable", include_in_schema=False)
async def api_help_disable(request: Request):
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    session = _help()
    await asyncio.to_thread(session.close, "user")
    return JSONResponse(dict(session.status(), ok=True))


@app.get("/api/help/status", include_in_schema=False)
async def api_help_status(request: Request):
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    session = _help()
    status = session.status()
    if status["state"] == help_session.OPEN:
        status = dict(status, password=session.password_for_display())
    return JSONResponse(status)


#: The certificate this process was started with. The TLS context cannot be reloaded, and the path
#: usually does not change when the certificate is re-signed - so remember the file's identity, not
#: its name. Comparing names reported "no reload needed" right after a re-enrolment, which is
#: exactly the case the flag exists for.
def _cert_identity(path: str) -> tuple:
    try:
        st = Path(path).stat()
        return (str(Path(path).resolve()), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), 0, 0)


_SERVED_CERT_AT_STARTUP = (_cert_identity(os.environ["MRRC_SSL_CERT"])
                           if os.environ.get("MRRC_SSL_CERT") else None)


def _cloud_start_tunnel(settings: dict) -> None:
    """Start frpc if this instance has been connected. Called lazily from /api/cloud/state."""
    global _cloud_tunnel
    label = settings.get("MRRC_CLOUD_LABEL")
    if not label or _cloud_tunnel is not None:
        return
    fleet = _cloud_fleet_dir()
    frpc = fleet / ("frpc.exe" if os.name == "nt" else "frpc")
    conf = _cloud_data_dir() / f"frpc-{label}.toml"
    # 缺东西时**说清楚缺哪个**。以前这里是静默 return：盒子自己的设置页写着「已接通」，
    # hub 那边 502，服务端日志什么都没有（2026-10-22 现场，盒子 bh7kyz 断链四天）。
    missing = [path for path in (frpc, conf) if not path.exists()]
    if missing:
        _note_tunnel_problem("隧道无法启动：找不到 " + "、".join(str(path) for path in missing))
        return
    with _cloud_lock:
        if _cloud_tunnel is None:
            # 老实例的配置（管理段进生成器之前写的）没有 webServer：这里补上，
            # 因为 frpc 只在启动时读配置 —— 这是唯一安全的时机（见 cloud_hub.ensure_admin_segment）。
            cloud_hub.ensure_admin_segment(conf)
            _cloud_tunnel = cloud_hub.TunnelProcess(frpc, conf)
            _cloud_tunnel.start()
            _note_tunnel_problem("")


@app.get("/api/cloud/state", include_in_schema=False)
async def api_cloud_state(request: Request):
    """What the settings dialog needs to draw itself."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    settings = _cloud_settings()
    _cloud_start_tunnel(settings)
    return JSONResponse({
        "connected": bool(settings.get("MRRC_CLOUD_LABEL")),
        "callsign": settings.get("MRRC_CLOUD_CALLSIGN", ""),
        "has_token": bool(settings.get("MRRC_CLOUD_TOKEN")),
        "label": settings.get("MRRC_CLOUD_LABEL", ""),
        "entry": settings.get("MRRC_CLOUD_ENTRY", ""),
        "cert": os.environ.get("MRRC_SSL_CERT", ""),
        "portal": _cloud_portal(settings),
        "autoconnect": dict(_cloud_autoconnect_state),
        "tunnel_running": bool(_cloud_tunnel and _cloud_tunnel.running),
        "tunnel_error": ((_cloud_tunnel.last_error if _cloud_tunnel else "")
                         or _cloud_tunnel_problem),
        "cert_reload_required": _cert_reload_required(),
    })


@app.post("/api/cloud/apply", include_in_schema=False)
async def api_cloud_apply(request: Request):
    """Ask the portal for an entry. The request token is stored so status can be polled later."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await request.json()
    callsign = str(body.get("callsign", "")).strip().upper()
    contact = str(body.get("contact", "")).strip()
    secret = str(body.get("secret", "")).strip()
    portal = _cloud_portal(_cloud_settings())
    if not callsign:
        return JSONResponse({"error": "需要呼号"}, status_code=400)
    try:
        if secret:
            # The operator already approved this application (perhaps it was entered on the web).
            # The one-time secret they were given claims it, so the tenant does not have to start
            # over. The same secret is what the hub uses to accept the certificate, so it grants
            # nothing new.
            reply = cloud_hub.claim(portal, callsign, secret)
        else:
            reply = cloud_hub.apply(portal, callsign, contact, "mrrc_modern")
    except cloud_hub.CloudHubError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    cloud_hub._write_config(_config_file_path(), {
        "MRRC_CLOUD_CALLSIGN": callsign,
        "MRRC_CLOUD_TOKEN": reply["request_token"],
        "MRRC_CLOUD_PORTAL": portal,
    })
    # From here on nobody has to keep the dialog open: the poll loop lives on this side, so an
    # approval that arrives tomorrow still completes without the tenant watching anything.
    _cloud_start_autoconnect()
    if reply.get("status") == "granted":
        # A claimed application is already approved: connect now instead of asking for another click.
        return await api_cloud_refresh(request)
    return JSONResponse({"submitted": True, "status": reply.get("status", "applied"),
                         "callsign": callsign})


@app.post("/api/cloud/refresh", include_in_schema=False)
async def api_cloud_refresh(request: Request):
    """Poll for approval; once granted, do the whole connection and report the entry."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    settings = _cloud_settings()
    callsign, token = settings.get("MRRC_CLOUD_CALLSIGN", ""), settings.get("MRRC_CLOUD_TOKEN", "")
    if not (callsign and token):
        return JSONResponse({"error": "还没有申请"}, status_code=400)
    portal = _cloud_portal(settings)
    _ensure_tunnel_object()          # first use has none; without this frpc is never started
    try:
        state = cloud_hub.status(portal, callsign, token)
        if state.get("status") != "granted":
            return JSONResponse({"connected": False, "status": state.get("status", "unknown")})
        with _cloud_connect_lock:        # the poller does the same thing; only one enrols
            result = cloud_hub.connect(portal, callsign, token, config_path=_config_file_path(),
                                       cert_dir=_cloud_cert_dir(), fleet_dir=_cloud_fleet_dir(),
                                       data_dir=_cloud_data_dir(), local_port=WEB_PORT,
                                       tunnel=_cloud_tunnel)
    except cloud_hub.CloudHubError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    cloud_hub._write_config(_config_file_path(), {
        "MRRC_CLOUD_LABEL": result.get("label", ""),
        "MRRC_CLOUD_PORT": str(result.get("port", "")),
        "MRRC_CLOUD_ENTRY": result.get("entry", ""),
    })
    # The TLS context was built when uvicorn started, so the certificate signed just now is not the
    # certificate being served: the hub verifies the upstream against the enrolled one and the entry
    # answers 502 until the process restarts. Measured on a clean machine, where the served
    # fingerprint stayed at the bootstrap certificate until the launcher was told to restart. The
    # launcher already restarts the server when asked, so say so instead of pretending.
    result = dict(result)
    result["cert_reload_required"] = _cert_reload_required()
    return JSONResponse(result)


def _configured_ssl_cert() -> str:
    """The certificate path the config file asks for **now** (a fresh read).

    This cannot go through ``_cloud_settings()``: that helper filters to ``_CLOUD_KEYS``, which
    deliberately holds only what the settings dialog may see, and MRRC_SSL_CERT is not in it — so
    the lookup was always None. Falling back to ``os.environ`` was worse: the environment is the one
    this process started with, and a certificate is only ever enrolled *after* that, so the value
    that mattered could not be seen. Measured 2026-10-03: the config asked for fullchain.pem, the
    process served server.crt, and this said there was nothing to reload.
    """
    try:
        for raw in _config_file_path().read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key.strip() == "MRRC_SSL_CERT":
                    return value.strip()
    except OSError:
        pass
    return os.environ.get("MRRC_SSL_CERT", "")


def _same_certificate(a: str, b: str) -> bool:
    """Whether two paths name the same file (resolving links and case where the OS does)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _cert_reload_required() -> bool:
    """True when what this process serves is no longer what the config asks for.

    The TLS context is built once, at start-up, so a certificate enrolled afterwards is not the one
    being served - the hub verifies the upstream against the enrolled one and the entry answers 502
    (measured repeatedly). Two ways for that to happen, and both are checked:

    * the enrolment wrote a **different path** into the config (the launcher's bootstrap
      ``certs/server.crt`` versus the enrolled ``certs/fullchain.pem``) - a difference in mtime
      cannot see this, because the old file never changed;
    * it rewrote the **same path** this process was started with - then only the clock can tell.
    """
    configured = _configured_ssl_cert()
    if not configured:
        return False
    if _SERVING_SSL_CERT and not _same_certificate(configured, _SERVING_SSL_CERT):
        return True
    try:
        cert_mtime = Path(configured).stat().st_mtime
    except OSError:
        return False
    return cert_mtime > _PROCESS_STARTED_AT + 1.0          # one second of slack for clock granularity


# Which certificate this process is actually serving. Recorded from _resolve_ssl_kwargs' answer at
# start-up, because that is the only place that knows it: the config file may name a different one
# by the time anybody asks.
_SERVING_SSL_CERT = ""


def _record_serving_cert(ssl_kwargs: dict) -> None:
    global _SERVING_SSL_CERT
    _SERVING_SSL_CERT = str(ssl_kwargs.get("ssl_certfile") or "")


# Epoch seconds, NOT time.monotonic(): this is compared against st_mtime, and mixing the two
# clocks made every configured certificate look "written after this process started" - a three
# day old file included (measured 2026-10-03: 1.79e9 vs 3.8e5). Harmless while a human clicked
# the restart button and shrugged at the prompt that never went away; fatal once the enrolment
# restarts on its own, because every restart would ask for another one.
_PROCESS_STARTED_AT = time.time()


# How often an unapproved instance asks the hub whether it has been approved. The tenant is the
# one waiting, and a status poll is a single POST - but this is also the loop that runs for days
# on a machine nobody looks at, so it must never be the reason a network is busy.
_CLOUD_AUTOCONNECT_INTERVAL = 30.0
_cloud_autoconnect_state: dict = {}
_cloud_autoconnect_thread: "threading.Thread | None" = None

# Enrolling is not idempotent when two callers overlap: both sign a certificate, both POST it to
# the hub's /enroll, and both write the frpc config. The dialog and the poller can now be in
# flight at the same time, so one of them has to step aside.
_cloud_connect_lock = threading.Lock()


def _note_autoconnect(status: str, error: str = "") -> dict:
    """Record what the unattended poll is doing, so a stuck instance is visible.

    Without this, "the client never asked" and "the client asked and is still pending" look
    identical from every UI we have. Measured 2026-10-03: BG6LH sat on its "已提交申请" message
    for hours after the hub had already granted it, and nothing in the app could say which side
    was waiting - the hub's nginx log was the only place that showed no /status was ever sent.
    """
    global _cloud_autoconnect_state
    _cloud_autoconnect_state = {"status": status, "error": error, "at": time.time()}
    return dict(_cloud_autoconnect_state)


def _cloud_autoconnect_once() -> dict:
    """Ask the hub about our application, and finish the enrolment when it has been granted.

    The settings dialog used to own the only poll loop (open() starts a 20s interval, close()
    clears it), so an approval only landed while somebody kept that dialog open - and the tenant
    who submitted the application is not the person who approves it, and has no reason to keep a
    dialog open for hours or days. The loop therefore belongs on this side, next to the tunnel
    that already starts at boot for the same reason.

    Never raises: this runs on a daemon thread, where an exception would silently end the polling
    and leave the instance looking exactly like one whose application was never approved.
    """
    settings = _cloud_settings()
    callsign = settings.get("MRRC_CLOUD_CALLSIGN", "")
    token = settings.get("MRRC_CLOUD_TOKEN", "")
    if not (callsign and token):
        return _note_autoconnect("idle")
    if settings.get("MRRC_CLOUD_LABEL"):
        return _note_autoconnect("connected")
    portal = _cloud_portal(settings)
    try:
        state = cloud_hub.status(portal, callsign, token)
    except cloud_hub.CloudHubError as exc:
        return _note_autoconnect("unreachable", str(exc))
    if state.get("status") != "granted":
        return _note_autoconnect(str(state.get("status") or "unknown"))
    if not _cloud_connect_lock.acquire(blocking=False):
        return _note_autoconnect("busy")     # the dialog is enrolling this very moment
    try:
        _ensure_tunnel_object()          # first use has none; without this frpc is never started
        result = cloud_hub.connect(portal, callsign, token, config_path=_config_file_path(),
                                   cert_dir=_cloud_cert_dir(), fleet_dir=_cloud_fleet_dir(),
                                   data_dir=_cloud_data_dir(), local_port=WEB_PORT,
                                   tunnel=_cloud_tunnel)
    except Exception as exc:                                     # noqa: BLE001
        logger.warning("cloud hub: unattended connect failed (%s) - will retry", exc)
        return _note_autoconnect("connect-failed", str(exc))
    finally:
        _cloud_connect_lock.release()
    cloud_hub._write_config(_config_file_path(), {
        "MRRC_CLOUD_LABEL": result.get("label", ""),
        "MRRC_CLOUD_PORT": str(result.get("port", "")),
        "MRRC_CLOUD_ENTRY": result.get("entry", ""),
    })
    logger.info("cloud hub: enrolled without anyone opening the dialog - %s",
                result.get("entry", ""))
    if _cert_reload_required():
        # The certificate signed just now is not the one being served, so the entry answers 502
        # until a restart. The dialog offers a button for this; unattended means pressing it.
        if _cloud_restart_now():
            return _note_autoconnect("restarting")
        return _note_autoconnect("connected", "新证书需手动重启应用后才生效")
    return _note_autoconnect("connected")


def _cloud_autoconnect_loop(interval: float = _CLOUD_AUTOCONNECT_INTERVAL) -> None:
    while True:
        try:
            _cloud_autoconnect_once()
        except Exception:                                        # noqa: BLE001
            logger.exception("cloud hub: unattended poll raised - continuing")   # never dies
        time.sleep(interval)


def _cloud_start_autoconnect(interval: float = _CLOUD_AUTOCONNECT_INTERVAL) -> bool:
    """Start the unattended poller when this instance has an application to follow.

    Called at start-up (for an instance that applied before a reboot) and right after an
    application is submitted, so closing the dialog cannot stop the approval from landing.
    Idempotent: a second call while the thread lives is a no-op.
    """
    global _cloud_autoconnect_thread
    settings = _cloud_settings()
    if not (settings.get("MRRC_CLOUD_CALLSIGN") and settings.get("MRRC_CLOUD_TOKEN")):
        return False
    if settings.get("MRRC_CLOUD_LABEL"):
        return False
    if _cloud_autoconnect_thread is not None and _cloud_autoconnect_thread.is_alive():
        return False
    _cloud_autoconnect_thread = threading.Thread(target=_cloud_autoconnect_loop, args=(interval,),
                                                 name="cloud-autoconnect", daemon=True)
    _cloud_autoconnect_thread.start()
    logger.info("cloud hub: waiting for approval; this instance asks every %.0f s", interval)
    return True


def _ensure_fallback_dns() -> list:
    """Put the shipped fallback resolvers on every saved profile. Never raises.

    Runs on a daemon thread at start-up, which is the upgrade path: a box flashed
    before FALLBACK_DNS existed picks it up on its next restart (new boxes get it from
    the image and from ``net_wifi.write_profile``). Logs one sentence when it changes
    something - a box rewriting its own network profiles silently would be exactly the
    kind of invisible state this repo keeps paying for.

    Off the event loop because it shells out to nmcli once per saved profile.
    """
    try:
        changed = net_wifi.ensure_fallback_dns()
    except Exception as exc:                                      # noqa: BLE001
        logger.warning("could not add the fallback resolvers (%s)", exc)
        return []
    if changed:
        logger.info("added the fallback resolvers %s to: %s",
                    ", ".join(net_wifi.FALLBACK_DNS), ", ".join(changed))
    return changed




def _relaunch_command() -> list:
    """How to start this app again, or [] when there is nothing that would.

    Measured: exiting without a supervisor leaves the instance with no service at all - the tunnel
    keeps running and every request to the entry becomes 502, with nothing on the machine to say
    why. So we only exit if we can start the launcher ourselves, detached.
    """
    exe = Path(sys.executable)
    for candidate in (exe.parent / "MRRC-Modern-Launcher",
                      exe.parent / "MRRC-Modern-Launcher.exe",
                      exe.parent.parent / "MRRC-Modern-Launcher",
                      exe.parent.parent / "MRRC-Modern-Launcher.exe"):
        if candidate.exists():
            return [str(candidate)]
    return []


def _cloud_restart_now() -> bool:
    """Restart so the certificate signed at connect time is the one being served.

    The same path the dialog's restart button takes, minus the button: an unattended enrolment
    would otherwise leave the entry answering 502 until somebody clicked. Returns False when
    there is nothing that could start the app again - exiting without a supervisor leaves the
    instance with a running tunnel and no service behind it, which is worse than a 502.
    """
    command = _relaunch_command()
    if not command:
        logger.warning("cloud hub: enrolled, but no launcher to restart with - "
                       "restart by hand so the new certificate is served")
        return False

    def _go():
        try:
            flags = 0
            if os.name == "nt":
                flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen(command, creationflags=flags, close_fds=True)
        except Exception:                                        # noqa: BLE001
            logger.exception("cloud hub: could not start the launcher; staying up")
            return
        os._exit(0)

    threading.Timer(1.2, _go).start()
    return True


@app.post("/api/cloud/restart", include_in_schema=False)
async def api_cloud_restart(request: Request):
    """Restart so the certificate signed at connect time is the one being served."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not _cloud_restart_now():
        return JSONResponse({"error": "找不到启动器，无法自动重启；请手动退出并重新打开应用"}, status_code=409)
    return JSONResponse({"restarting": True})


def _password_is_untouched() -> bool:
    """Whether no human has ever chosen this box's web password.

    The auto-password flag is the honest signal: first_run sets it when it
    generates a password nobody was told, and every user-driven write clears it.
    The two fallbacks catch a box where first run never happened at all.
    """
    if os.environ.get("MRRC_AUTO_PASSWORD") == "1":
        return True
    if not WEB_PASSWORD:
        return True
    return hmac.compare_digest(str(WEB_PASSWORD).encode("utf-8"),
                               str(DEFAULT_WEB_PASSWORD).encode("utf-8"))


def _first_window_open(request: Request) -> bool:
    """Whether this request may set the box's first password with no session.

    Two facts have to agree, both read live (design §5.1):

    1. Nobody has set a password yet. This is why there is no flag to clear and
       no timer to expire: the predicate *is* the state, so the window cannot be
       left open by forgetting to close it.
    2. The client is on a subnet this box itself holds an address on. An
       unreadable set of local subnets closes the window rather than opening it —
       "I cannot tell" has to read as "not you" (spec §5.4 for the residual
       weakness this still leaves on a shared LAN).
    """
    if not _password_is_untouched():
        return False
    host = request.client.host if request.client else ""
    return box_ops.client_in_local_subnet(host)


def _write_password(password: str) -> bool:
    """Persist the web password, and make it live without a restart.

    The one place in server.py that clears MRRC_AUTO_PASSWORD, which is what
    closes the onboarding window — so this is the function that closes it. The
    restart the retired setup flow deliberately did not do is still deliberately
    not done: the caller is mid-onboarding and the next step takes their network
    away.
    """
    global WEB_PASSWORD
    updates = {"MRRC_WEB_PASSWORD": password, "MRRC_AUTO_PASSWORD": ""}
    try:
        first_run.update_env_file(_config_file_path(), updates)
    except OSError as exc:
        logger.error("could not write the web password: %s", exc)
        return False
    WEB_PASSWORD = password
    os.environ["MRRC_WEB_PASSWORD"] = password
    os.environ["MRRC_AUTO_PASSWORD"] = ""
    return True


@app.get("/setup", include_in_schema=False)
async def setup_page(request: Request):
    """The onboarding page.

    Passwordless only while the box has never had a user-set password; from
    anywhere else it is an ordinary authenticated route. A box that is already
    configured sends the operator to the settings page rather than showing them
    a page they cannot use — and so does a listen-only session, whose token is
    logged in but is not entitled to this page (its surface is /settings).
    `no-store` because the window this page renders closes the moment a
    password lands — a cached copy would tell the operator the box is still
    open when it is not.
    """
    if not await asyncio.to_thread(_first_window_open, request):
        if not _verify_auth(request):
            return RedirectResponse("/login?next=/setup", status_code=302)
        if _is_listen_request(request) or not _password_is_untouched():
            return RedirectResponse("/settings", status_code=302)
    page = STATIC_DIR / "setup.html"
    if not page.exists():
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)
    response = FileResponse(page, media_type="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/settings", include_in_schema=False)
async def settings_page(request: Request):
    """The box settings page. Behind login like everything else — the
    middleware enforces it, and this route only has to serve the file."""
    page = STATIC_DIR / "settings.html"
    if not page.exists():
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)
    return _html_page(page)


@app.get("/api/box/onboarding", include_in_schema=False)
async def api_box_onboarding(request: Request):
    """The four things the onboarding page needs, and nothing else.

    The page renders Wi-Fi and a password box; it does not get the network
    report, the system state or the safety fields — those are a logged-in
    surface. Keeping this payload small is the point (design §5.3).
    """
    if not await asyncio.to_thread(_first_window_open, request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    identity = await asyncio.to_thread(box_ops.identity_report)
    return JSONResponse({
        "needs_password": _password_is_untouched(),
        "hostname": identity["hostname"],
        "mdns": identity["mdns"],
        "networks": await asyncio.to_thread(box_ops.scan_networks),
        "min_password_len": box_ops.MIN_PASSWORD_LEN,
    })


@app.post("/api/box/password", include_in_schema=False)
async def api_box_password(request: Request):
    """Set the web password. The window's only write, and it is one key wide.

    Two ways in: an open first window (the onboarding page), or an ordinary
    admin session (the settings page). A listen-only session is neither: its
    token *is* in `_auth_tokens` — that is how /api/auth/login records it — so
    it passes the auth check, but it may never write (the middleware's
    listen-only 403 covers the rest of /api/; this endpoint sits in
    FIRST_WINDOW_PATHS, which the middleware passes through *before* that check,
    so the refusal has to live here). It gets 403, not 401: the session is
    logged in, and 401 would tell the operator their session expired.

    Success signs a session and hands it back, so the operator is not asked to
    log in with the password they just typed.
    """
    if not await asyncio.to_thread(_first_window_open, request):
        if _verify_auth(request):
            if _is_listen_request(request):
                return JSONResponse({"error": LISTEN_ONLY_MESSAGE},
                                    status_code=403)
        else:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    password = (body or {}).get("password", "") or ""
    confirm = (body or {}).get("confirm", password) or ""
    problem = box_ops.password_problem(password, confirm)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=400)
    if not _write_password(password):
        return JSONResponse({"ok": False, "error": "写入配置失败"},
                            status_code=500)

    identity = await asyncio.to_thread(box_ops.identity_report)
    token = _make_auth_token()
    _add_auth_token(token, listen=False)
    response = JSONResponse({"ok": True, "role": "admin", "token": token,
                             "hostname": identity["hostname"],
                             "mdns": identity["mdns"]})
    response.set_cookie(
        AUTH_COOKIE, token,
        max_age=30 * 24 * 3600,  # 30 days, same as /api/auth/login
        httponly=False,          # JS needs to read it for WebSocket
        samesite="lax",
    )
    logger.warning("box: the web password was set — the onboarding window is "
                   "now closed")
    return response


@app.get("/api/box/status", include_in_schema=False)
async def api_box_status(request: Request):
    """Everything the settings page shows, in one read.

    Four reads behind one request because the page is one screen: six panels
    each fetching separately is six chances to be half-loaded, and the network
    report and the identity report are both `nmcli device show` anyway.

    `asyncio.to_thread` because every one of these shells out, and a blocking
    subprocess call inside an async handler stops the whole server — including
    the WebSocket feeds a user may be mid-QSO on.
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    network, system, identity = await asyncio.gather(
        asyncio.to_thread(box_ops.network_report),
        asyncio.to_thread(box_ops.system_report),
        asyncio.to_thread(box_ops.identity_report),
    )
    # Same reason as the three above: `_first_window_open` reads the local
    # subnets, which shells out to nmcli.
    first_window_open = await asyncio.to_thread(_first_window_open, request)
    return JSONResponse({
        "network": network,
        "system": system,
        "identity": identity,
        "safety": box_ops.safety_report(),
        "helper_available": box_ops.helper_available(),
        "first_window_open": first_window_open,
    })


async def _box_body(request: Request) -> dict:
    """The JSON body, or an empty dict. A malformed body is a 400 from the
    endpoint's own validation, not a 500 from the parser."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _box_busy() -> bool:
    """Whether a network change is already in flight.

    Both the Wi-Fi switch and the wired change move the box's own connectivity,
    so they share one slot: two at once would leave the rollback of one fighting
    the apply of the other.
    """
    return box_ops.switch_state()["phase"] == box_ops.PHASE_SWITCHING


@app.get("/api/box/wifi", include_in_schema=False)
async def api_box_wifi_scan(request: Request):
    """Visible networks. Unprivileged: `mrrc` may read NetworkManager."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(
        {"networks": await asyncio.to_thread(box_ops.scan_networks)})


@app.post("/api/box/wifi", include_in_schema=False)
async def api_box_wifi_connect(request: Request):
    """Order a Wi-Fi switch. Returns before the network moves, on purpose.

    The request may be arriving over the very interface this takes down, so
    waiting for the result would wait on a socket that is already gone. The page
    polls `/api/box/wifi/status` after it reconnects (design §6.2).
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    ssid = (body.get("ssid") or "").strip()
    password = body.get("password") or ""
    if not ssid:
        return JSONResponse({"ok": False, "error": "请选择一个 Wi-Fi"},
                            status_code=400)
    if _box_busy():
        return JSONResponse({"ok": False, "error": "已经有一次切换在进行中"},
                            status_code=409)
    asyncio.create_task(asyncio.to_thread(
        box_ops.perform_wifi_switch, ssid, password))
    logger.warning("box: switching to WiFi %r — if the operator reached the box "
                   "over WiFi, their own session is about to drop", ssid)
    return JSONResponse({"ok": True, "switching": True, "ssid": ssid})


@app.get("/api/box/wifi/status", include_in_schema=False)
async def api_box_wifi_status(request: Request):
    """How the switch went. Read after reconnecting, possibly from the new
    network, possibly after a rollback."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(box_ops.switch_state())


@app.post("/api/box/wifi/save", include_in_schema=False)
async def api_box_wifi_save(request: Request):
    """Save (or replace) a Wi-Fi profile **without** switching to it.

    The switch endpoint has to bring the profile up immediately, which is 25 s of
    waiting and a rollback when the access point is not in range. That is the
    wrong tool for preparing a box for someone else's home — the AP is never here
    — so this one only writes the profile. The active connection is untouched.
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    ssid = (body.get("ssid") or "").strip()
    password = body.get("password") or ""
    autoconnect = bool(body.get("autoconnect", True))
    problem = box_ops.ssid_problem(ssid) or box_ops.psk_problem(password)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=400)
    result = await asyncio.to_thread(
        box_ops.save_wifi, ssid, password, autoconnect)
    # The SSID is fine to log; the password never is (design D-4 / NFR-068).
    logger.warning("box: saved Wi-Fi %r (autoconnect=%s)", ssid, autoconnect)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.get("/api/box/bt/scan", include_in_schema=False)
async def api_box_bt_scan(request: Request):
    """Look for nearby remotes (BR/EDR).  Slow on purpose: an inquiry needs ~10 s."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    devices = await asyncio.to_thread(net_bt.scan)
    # 页面按**名字**自动配对（不认 MAC：来料批次会换地址）。名字表从这里下发，
    # 部署侧可以用 MRRC_BT_REMOTE_NAME 改，不必改页面。
    return JSONResponse({"devices": devices,
                         "remoteNames": list(net_bt.remote_name_patterns())},
                        status_code=200)


@app.get("/api/box/bt/state", include_in_schema=False)
async def api_box_bt_state(request: Request):
    """One address's pairing flags plus the verdict that matters: do keys work?"""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    address = (request.query_params.get("address") or "").strip()
    if not address:
        return JSONResponse({"ok": False, "error": "没有指定设备"}, status_code=400)
    return JSONResponse(await asyncio.to_thread(net_bt.state, address), status_code=200)


@app.post("/api/box/bt/pair", include_in_schema=False)
async def api_box_bt_pair(request: Request):
    """Pair + trust a remote, then report whether its keys actually arrive.

    ``keys_work`` is the field the page acts on: a remote can be ``paired`` and
    ``bonded`` while its link stayed unencrypted, in which case bluez builds no
    input device and every button is dead (docs/W103D_REMOTE_GUIDE.md §2).
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    address = (body.get("address") or "").strip()
    if not address:
        return JSONResponse({"ok": False, "error": "没有指定设备"}, status_code=400)
    result = await asyncio.to_thread(net_bt.pair, address)
    state = await asyncio.to_thread(net_bt.state, address)
    # pair() 里那句 error 是操作员唯一能行动的东西（「没在广播 → 按住 OK+返回」）。
    # 早先这里用 state() 重新取了一遍而把它丢了，页面于是只剩一句「配不上」。
    if result.get("error"):
        state["error"] = result["error"]
    logger.warning("box: pair remote %s → paired=%s keys_work=%s (%s)",
                   address, state.get("paired"), state.get("keys_work"),
                   state.get("error") or "ok")
    state["ok"] = bool(state.get("paired")) and bool(state.get("keys_work"))
    return JSONResponse(state, status_code=200 if state.get("paired") else 400)


@app.post("/api/box/bt/forget", include_in_schema=False)
async def api_box_bt_forget(request: Request):
    """Drop a bonding (and power-cycle the controller: bluez caches link keys)."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    address = (body.get("address") or "").strip()
    if not address:
        return JSONResponse({"ok": False, "error": "没有指定设备"}, status_code=400)
    await asyncio.to_thread(net_bt.remove, address)
    if body.get("power_cycle"):
        await asyncio.to_thread(net_bt.power_cycle)
    logger.warning("box: forgot remote %s", address)
    return JSONResponse({"ok": True}, status_code=200)


@app.post("/api/box/wifi/forget", include_in_schema=False)
async def api_box_wifi_forget(request: Request):
    """Forget one saved Wi-Fi profile."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    ssid = (body.get("ssid") or "").strip()
    if not ssid:
        return JSONResponse({"ok": False, "error": "没有指定要删的 Wi-Fi"},
                            status_code=400)
    result = await asyncio.to_thread(box_ops.forget_wifi, ssid)
    logger.warning("box: forgot Wi-Fi %r", ssid)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/api/box/wifi/autoconnect", include_in_schema=False)
async def api_box_wifi_autoconnect(request: Request):
    """Arm or disarm a saved profile: "connect whenever it is in range, from now
    on" versus "keep it, but do not use it by itself"."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    ssid = (body.get("ssid") or "").strip()
    if not ssid:
        return JSONResponse({"ok": False, "error": "没有指定 Wi-Fi"},
                            status_code=400)
    enabled = bool(body.get("enabled", True))
    result = await asyncio.to_thread(
        box_ops.set_wifi_autoconnect, ssid, enabled)
    logger.warning("box: Wi-Fi %r autoconnect=%s", ssid, enabled)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/api/box/hostname", include_in_schema=False)
async def api_box_hostname(request: Request):
    """Rename the box, then tell avahi before it notices on its own.

    avahi-daemon reads the hostname when it starts and when it is reloaded; it
    does not follow `hostnamectl`. Without the nudge the new name is advertised
    at whatever moment avahi next decides to, which is not a thing to make an
    operator wait for.
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    result = await asyncio.to_thread(box_ops.set_hostname,
                                     (body.get("hostname") or "").strip())
    if result.get("ok"):
        await asyncio.to_thread(box_ops.reload_avahi)
    return JSONResponse(result,
                        status_code=200 if result.get("ok") else 400)


@app.post("/api/box/fix-dns", include_in_schema=False)
async def api_box_fix_dns(request: Request):
    """Repair ``/etc/resolv.conf`` with the web password, not root's.

    Boxes flashed before 2026-10-25 carry the **build machine's** ``/etc/resolv.conf``
    inside the image: the build lends the chroot that file so apt/pip can resolve and
    the base image's ``-> ../run/systemd/resolve/stub-resolv.conf`` symlink is never put
    back. Measured on a delivered box (MRRC-BG1SB, image built 2026-10-11): the file held
    Docker Desktop's ``192.168.65.7``, an address that exists on no customer LAN, so glibc
    waited 5 s per lookup and failed while ``ping <ip>`` kept working.

    The repair is ``linux/fix_resolv_conf.sh`` — the same file ``mrrc-update`` runs — so
    this endpoint cannot drift from the upgrade path. It is idempotent, backs the original
    up, and is reachable only with a logged-in session (the listen role is refused by the
    auth middleware, which allows GET only).
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    result = await asyncio.to_thread(box_ops.fix_dns)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/api/box/timezone", include_in_schema=False)
async def api_box_timezone(request: Request):
    """Set the box's timezone, through the helper: `timedatectl` is root's.

    The zone is validated here before the helper is asked, the same shape as
    `api_box_link`: a zone that cannot be set must be refused as a 400 without a
    round trip to the privileged side (`box_ops.timezone_problem` is the pure
    check, and `box_ops.set_timezone` repeats it before it touches the socket).
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    zone = (body.get("timezone") or "").strip()
    problem = box_ops.timezone_problem(zone)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=400)
    result = await asyncio.to_thread(box_ops.set_timezone, zone)
    return JSONResponse(result,
                        status_code=200 if result.get("ok") else 400)


@app.post("/api/box/link", include_in_schema=False)
async def api_box_link(request: Request):
    """Order a change to a wired profile: DHCP or a static address.

    Ordered, not awaited, for the same reason as the Wi-Fi switch: the operator
    may be reaching the box over the profile being edited (design §6.4).
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    profile = (body.get("profile") or "").strip()
    method = (body.get("method") or "").strip()
    address = (body.get("address") or "").strip()
    gateway = (body.get("gateway") or "").strip()
    if not profile:
        return JSONResponse({"ok": False, "error": "没有指定要改的连接"},
                            status_code=400)
    problem = box_ops.link_problem(method, address)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=400)
    if _box_busy():
        return JSONResponse({"ok": False, "error": "已经有一次网络改动在进行中"},
                            status_code=409)
    asyncio.create_task(asyncio.to_thread(
        box_ops.perform_link_change, profile, method, address,
        gateway=gateway))
    logger.warning("box: changing %r to %s — if the operator reached the box "
                   "over this link, their session is about to drop",
                   profile, method)
    return JSONResponse({"ok": True, "changing": True, "profile": profile})


@app.post("/api/box/system-password", include_in_schema=False)
async def api_box_system_password(request: Request):
    """Change the password of root / mrrc / rescue.

    Separate from `/api/box/password` (the web login) on purpose: these are the
    accounts an operator reaches over SSH or HDMI, and getting them wrong locks
    people out of the console — hence the strict account allowlist and the
    sentence-level validation before anything reaches a root process.
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    user = (body.get("user") or "").strip()
    password = body.get("password") or ""
    problem = box_ops.system_password_problem(user, password)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=400)
    result = await asyncio.to_thread(box_ops.set_system_password, user, password)
    # The account is worth a line; the password never is (design D-4 / NFR-068).
    logger.warning("box: system password changed for %r", user)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/api/box/system/reboot", include_in_schema=False)
async def api_box_system_reboot(request: Request):
    """Restart the box, not the service.

    `POST /api/restart` restarts the server and everything comes back; this one
    takes the whole machine down for about a minute and drops every session on
    it. Two buttons on one page with that much difference between them need
    different friction, so this one takes an explicit confirmation value.
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    if body.get("confirm") != "reboot":
        return JSONResponse({"ok": False, "error": "重启盒子需要显式确认"},
                            status_code=400)
    result = await asyncio.to_thread(box_ops.reboot)
    logger.warning("box: reboot requested")
    return JSONResponse(result,
                        status_code=200 if result.get("ok") else 400)


#: The value the settings page must send to turn the experimental-model transmit
#: gate ON.  Same shape as `/api/box/system/reboot`'s `confirm`, and for the same
#: reason: a checkbox is not authority over a radio's PA.  The page asks twice
#: (checkbox + dialog), and this is the server-side half of that.
UNVERIFIED_TX_CONFIRM = "allow-unverified-tx"


def _unverified_tx_state() -> dict:
    """What the gate says right now, plus the models it governs (AD-019/NFR-067).

    `tx_gated` is read from the *live* backend, not from the env var, because a
    verified model has no gate at all and an unverified one reads the flag on
    every key-up.
    """
    profiles = model_verification()
    model = config.RADIO_MODEL
    here = profiles.get(model, {"name": model, "verified": False})
    caps = backend.capabilities if backend is not None else None
    return {
        "enabled": bool(config.ALLOW_UNVERIFIED_TX),
        "radio_model": model,
        "radio_name": here["name"],
        "radio_verified": bool(here["verified"]),
        "tx_gated": bool(getattr(caps, "tx_gated", not here["verified"])),
        "models": [{"key": key, "name": entry["name"]}
                   for key, entry in sorted(profiles.items())
                   if not entry["verified"]],
    }


@app.get("/api/box/unverified-tx", include_in_schema=False)
async def api_unverified_tx_state(request: Request):
    """The experimental-model transmit gate as the box currently has it."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(_unverified_tx_state())


@app.post("/api/box/unverified-tx", include_in_schema=False)
async def api_unverified_tx_set(request: Request):
    """Open or close transmit for the models whose tables were never measured.

    Policy is that these models ship receive-only (AD-019 / NFR-067); this is the
    one documented way to lift it from a browser, and it takes an explicit
    confirmation value because nothing else in the page can speak for the PA.
    Closing never needs one — taking power away is not the risky direction.

    The value goes to the env file through the single writer (`update_env_file`:
    one key, one line — the 2026-10-26 field file carried `MRRC_SERIAL_PORT`
    twice and every save was silently ignored), and is applied to the running
    process immediately: `backend.capabilities.tx_gated` is computed on each
    access, so the very next key-up is judged by the new value.  A restart is
    *not* required, which is the whole point of putting this on the box page.
    """
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await _box_body(request)
    enable = body.get("enabled")
    if not isinstance(enable, bool):
        return JSONResponse({"ok": False, "error": "enabled 必须是 true 或 false"},
                            status_code=400)
    if enable and body.get("confirm") != UNVERIFIED_TX_CONFIRM:
        return JSONResponse(
            {"ok": False,
             "error": "开启未验证机型的发射需要显式确认：命令表来自手册与 Hamlib、"
                      "没有在真机上验证过，功率/驻波/天调都可能与这台机器不一致。"},
            status_code=400)
    value = "1" if enable else "0"
    try:
        first_run.update_env_file(_config_file_path(),
                                  {"MRRC_ALLOW_UNVERIFIED_TX": value})
    except OSError as exc:
        logger.error("Failed to write the transmit gate: %s", exc)
        return JSONResponse({"ok": False, "error": "写入配置失败"}, status_code=500)
    config.ALLOW_UNVERIFIED_TX = enable
    os.environ["MRRC_ALLOW_UNVERIFIED_TX"] = value
    state = _unverified_tx_state()
    if enable:
        logger.warning(
            "box: transmit ENABLED for hardware-unverified models (%s) — their "
            "command tables come from manuals, not from a radio (NFR-067); "
            "start at minimum power and verify the key-up by hand",
            ", ".join(m["key"] for m in state["models"]))
    else:
        logger.info("box: transmit gate closed for hardware-unverified models")
    # `applied_now` tells the page whether it has to ask for a reload: the open
    # control page learned `tx_gated` at connect time, so its PTT button stays
    # greyed until it reconnects even though the server would already key.
    return JSONResponse({"ok": True, "applied_now": True, **state})


# The SPA fallback is registered last on purpose: it matches every GET path, so any GET route
# defined after it is answered with index.html instead of its own handler. That is how
# GET /api/cloud/state came back as 200 + a web page in v1.24.0 (the settings dialog could not
# read its own state), while the POST endpoints beside it worked. Keep this the last route in the
# file; tests/test_cloud_endpoints.py fails if an /api/ route ends up below it.
app.add_api_route("/{path:path}", serve_static, methods=["GET"])


if __name__ == "__main__":
    # The FT8 DSP worker is a multiprocessing ("spawn") child, and in a frozen
    # bundle the child is this very executable re-launched with
    # ``--multiprocessing-fork``.  Without freeze_support() nothing intercepts
    # those arguments, so the child ran main() instead: argparse printed its
    # usage, exited 2, and the supervisor reported "spawn_failed: worker did not
    # become ready" while the UI offered a mode that could never decode.  Found
    # by the clean-room run of the packaged macOS app, 2026-10-13; the same
    # applies to the Windows onedir build.
    #
    # It must be the first thing the entry point does - the bootstrap has to win
    # before anything else looks at argv.
    import multiprocessing

    multiprocessing.freeze_support()
    main()
