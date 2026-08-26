# Constraint Catalog — mrrc_modern

Human-readable companion to `harness/constraints.json` (SDD V2.23, 21 rules).
Each rule lists its severity, SDD source, and the incident or rationale that
produced it. The JSON file is the machine-readable source of truth; keep the
two in sync.

## Block-level rules (edits rejected by the hook)

| ID | Rule | SDD ref | Why |
|----|------|---------|-----|
| `cat-no-dn` | Never send/poll `DN;` | AD-014, V1.2 | On the FT-710 `DN;` steps the active VFO DOWN ~20 Hz. Polling it every 2 s caused a live frequency-drift incident. DNR level is intentionally unread. |
| `cat-pr-errata` | Compressor = `PR00`/`PR01` | AD-014 | The Yaesu PDF documents 1=OFF/2=ON; empirically the radio uses 0/1 like every other binary command, and `PR02` kills TX audio. |
| `cat-ac-mapping` | Tuner = `AC000/AC001/AC003` | AD-014, gap-analysis §1.1 | Deliberate divergence from Hamlib's `AC010/AC011` claim — do not "fix" towards Hamlib without radio re-verification. |
| `cat-sh-format` | Filter SET = `SH00NN` | §10.4, a07bb49, V1.7 | `SH0NN` is silently ignored. After setting, read `SH0;` back (~150 ms) and broadcast the actual index — the radio silently rejects invalid indexes. |
| `cat-direct-serial-io` | Serial only via `CatController` | AD-002 | Lock serialization + thread offload + 20 ms pacing live there; bypassing them races the asyncio loop and the poll scheduler. |
| `secrets-hardcoded` | No literal secrets/paths | AGENTS.md, §5.3 | Env vars (`MRRC_*`, legacy `FT710_*` still honored) carry deployment values. |
| `index-html-no-logic` | No inline JS in `index.html` | AGENTS.md | SPA shell only; logic in `ft710_ui.js` / `static/modules/`. |
| `audio-16k-rate` | Never 16 kHz audio | AD-011, V1.1 | 16 kHz capture vs 48 kHz playback caused the TX "crackling" incident. Domains: 48 kHz codec, per-backend device rate (FT-710 44.1 kHz). |

## Warn-level rules (allowed but surfaced)

| ID | Rule | SDD ref | Why |
|----|------|---------|-----|
| `audio-pyaudio-rate` | Device rate comes from backend capabilities | AD-011 (amended V2.9/V2.14) | The original "always 44100" rule was falsified twice by the field: Windows WASAPI and IC-7300 both run 48 kHz natively. AD-011's amended form: codec domain is always 48 kHz; the device-domain rate is whatever `RadioCapabilities.audio_rx_rate/audio_tx_rate` declares (FT-710 44100, IC-7300 48000). Hardcoding either value bypasses the backend contract and the SRC bridge. |
| `state-direct-assign` | Use `radio.update(...)` | AD-003 | Direct assignment skips dirty tracking → clients never see the change. Exception: watchdog bulk re-sync via `setattr`. |
| `ws-endpoint-auth` | New WS endpoints need `?token=` auth | §5.3 | All five existing endpoints gate on `_auth_tokens`. |
| `env-hardcoded-device` | Deployment values via env | §12.2 | `/dev/cu.*`, `COMn` literals don't belong in shipped source (probe scripts exempt). |
| `static-path-traversal` | Static paths stay inside `STATIC_DIR` | §13.4 I8 | `serve_static` builds `STATIC_DIR / path` with no resolve + containment check — an authenticated raw-HTTP client can `GET /../server.py` and read arbitrary files (browsers normalize `..`; curl-class clients do not). Warn-level until the fix lands so core-file checks stay green; becomes a hard reject candidate after I8 closes. |
| `password-constant-time` | Password checks use `hmac.compare_digest` | §13.4 I9 | Plain `!=` leaks a timing side channel; combined with the warning-only weak-default-password posture a fresh install is effectively open. Warn-level until fixed (same ratchet logic as I8). |

## Info-level rules (guidance in context output)

| ID | Rule | SDD ref | Why |
|----|------|---------|-----|
| `ptt-release-no-verify` | TX0 fire-and-forget | AD-007, Ch15 | The V1.2 triple-verify cost 600 ms per release; stuck-keyup detection is the 500 ms TX poll + browser watchdog. |
| `poll-stale-guard` | Re-check skip after every poll await | AD-009, V1.7 | A query in flight when the user's set arrives returns the pre-command value; applying it snaps the UI back (the "SSB filter sometimes ignored" bug). |
| `poll-priority-preemption` | PTT/TUNE use `send_priority_set_command` | AD-015 | `_cancel_polls` cooperative abort keeps key-up latency bounded. |
| `web-audio-subchannel-reconnect` | Audio/spectrum subchannels reconnect independently | §13.4 I10 | Only `/WSradio` auto-reconnects today; when its siblings drop, controls stay alive but audio dies until the control channel also cycles. atr1000.js already implements the backoff pattern to copy. |
| `ios-ptt-unconditional-release` | iOS PTT releases unconditionally + watchdog | §13.4 I11, Ch15 | iOS gates `ptt:false` on a server echo of `tx_status>0` with no watchdog/scenePhase guard — the documented P0 race that can key the transmitter over WAN. FT710Android's `PTTManager` is the reference implementation. |
| `docs-sync` | Doc updates ride with the change | §14 | SDD chapters, version history, AGENTS.md, README, tests/README. |
| `testing-conventions` | Hardware-free unittest + counts synced | tests/README | Suite must pass with no radio attached; every regression gets a test. |

## Open design issues (do not assume solved)

- **I6** — No multi-client control arbitration; concurrent browsers are last-writer-wins (SDD §13.4).
- **I7** — `mem_channels.json` POST lacks schema validation and backup (SDD §13.4).
- **I8** — `serve_static` path traversal: resolve + `is_relative_to(STATIC_DIR)` containment missing (SDD §13.4).
- **I9** — Non-constant-time password compare; weak default password only warns (SDD §13.4).
- **I10** — Web audio/spectrum subchannels have no independent reconnect (SDD §13.4).
- **I11** — iOS PTT release race — approved PTTManager-style fix not yet implemented (SDD §13.4).
