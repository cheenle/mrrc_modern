# Support Bundle and Report Loop (server-side diagnostics, MRRC Modern receiver)

**Date:** 2026-09-17

**Status:** Approved for implementation (design review 2026-09-17, decisions D1–D12)

**Scope:** Give the operator a **🐞 遇到问题** entry in the web UI that builds a **redacted, self-describing
diagnostics bundle** on the **server** (logs + config snapshot + environment/state snapshot + auto-triage
summary), uploads it to an **MRRC Modern-only receiver**, and can also save it locally when the machine has
no internet. Add the persistent logging this depends on.

This is sub-project 1 of 4 in the support-chain migration (`mrrc-product-support` skill:
发布 → 升级 → 诊断 → 分析 → 答复). Sub-project 2 (AI triage + public answers page), 3 (one-click upgrade)
and 4 (hotfix channel) are explicitly out of scope here.

Reference implementation: the sibling project `mrrc` (`support_bundle.py`, `www/support.html`,
`tools/support_receiver/server.py`, `deploy_support_receiver.sh`). This design reproduces its shape with
this project's conventions — **but not its config parser**: that project reads an INI
(`CONFIG_WHITELIST` sections), MRRC Modern reads a `MRRC_*` env file, so the whitelist is key-based here.

Authoritative in-repo references:

- `server.py:73` — `logging.basicConfig(...)`: **console only, nothing on disk**. `server.py:2304`
  `auth_middleware` (all `/api/*` → 401 without a session token; other paths → `/login`),
  `server.py:3105` catch-all static route (`_verify_auth` → `RedirectResponse("/login")`),
  `server.py:220/224/227` — the `_env(...)`-with-default pattern for runtime paths
  (`MRRC_MEM_FILE`, `MRRC_RECORDINGS_DIR`, `RECORDINGS_INDEX`).
- `windows/launcher.py:254` — `subprocess.Popen(command, cwd=..., env=..., creationflags=...)`:
  **stdout/stderr are not redirected**, so the server's output dies with the console.
  `windows/launcher.py:137-139` / `macos/launcher.py:189-191` — the launchers already set
  `MRRC_MEM_FILE` / `MRRC_ATR1000_STORE` / `MRRC_RECORDINGS_DIR` into the user data directory;
  `windows/launcher.py:294` / `macos/launcher.py:472` — `launcher.log` is written **only** on a startup
  failure. `windows/launcher.py:82` / `macos/launcher.py:146` — `wait_for_server(url, proc)` is where the
  launcher learns that the server is listening (the tee's stop condition).
- `install.sh:939-940` — the Raspberry Pi systemd unit redirects stdout/stderr to
  `$SCRIPT_DIR/logs/server.log`; `start.sh:13` — source-mode background start writes
  `logs/ft710-server.log`. Two more names to reconcile (see §5).
- `static/index.html` — the off-canvas `menu-list`; `static/ft710_ui.js:1793` binds
  `document.querySelectorAll('.menu-item[data-action]')` only, so a plain `<a href>` menu entry needs
  **no JS change** in the two files protected by the tooling guards (AGENTS.md).
- `.agents/skills/dual-platform-release/release-artifacts.json` — every file carrying a version, size or
  SHA is registered there and checked by `harness/release_check.py` + `tests/test_release_artifacts.py`.
- `docs/PROJECT_MAP.md` — the "what must I update?" entry point; a new support layer needs a row there.

## 1. Objective

An operator who hits a problem opens the UI menu, taps **🐞 遇到问题**, writes one paragraph, and gets a
bundle that answers the maintainer's first five questions without a round trip: *which version, which
platform, which radio backend, what did the log say, and is the machine even in a supported state.*
The bundle never contains credentials, certificates or the operator's QSO recordings.

**Success is a fact, not a hope**: the upload returns an id, that id is listed on the receiver, and the
downloaded copy hashes equal to the local one (§13).

## 2. Decisions taken during design review (2026-09-17)

| # | Question | Decision |
| --- | --- | --- |
| D1 | Scope | **Full support-chain migration, sub-project 1 first.** Diagnostics + report loop now; triage/answers (2), one-click upgrade (3), hotfix channel (4) later, in that order. 1/2 touch only the website and new modules; 3/4 change installed layout and need real-machine acceptance |
| D2 | Entry point | **Browser UI** (`static/support.html` opened from the SPA menu) with a **server-built** bundle. Works on desktop, phone and Raspberry Pi; the launcher entry is a later increment (the `launcher.log` "server will not start" case is the reason it may come back) |
| D3 | Receiver namespace | **Separate instance** on the same host: own systemd unit, port 8098, storage `/var/www/support-modern`, own nginx path. Sharing HAM/mrrc's receiver would mix two products' bundles in one list and make sub-project 2's triage unable to filter by product without changing the other repo |
| D4 | Bundle extras | **No 5 s RX audio sample in this change** (default-off checkbox in the sibling project). Ship the loop, measure the real problem distribution on the receiver, then decide |
| D5 | Log persistence | **Both**: a server-side rotating log (canonical, every launch mode) **and** a launcher tee of the child's stderr (startup-window crash net). Neither covers the other's failure class — see §5 |
| D6 | Tee lifetime | The tee **stops writing once `wait_for_server()` succeeds**. Bounded file, no duplication of the runtime log, and it keeps capturing exactly the case it exists for (a server that dies before logging is up) |
| D7 | Redaction model | **Allow-list by env key**, not deny-list, plus a secret-value regex pass over every collected text. Counted in `manifest.json` so the operator can see it happened |
| D8 | Bundle build | Server-side, single-flight, into `<log dir>/../support-out/`, newest 5 kept. Rebuild replaces; no unbounded temp growth |
| D9 | Upload failure | Never lose the bundle: the local path is returned and **只保存到本地** always works (Raspberry Pi behind a missing uplink is the motivating case) |
| D10 | Answers page | Create the URL now as a minimal stub (`website/answers/index.html` → `/mrrc_modern/answers/`) so the success message never links to a 404, and so the deploy/prune path is proven before sub-project 2 fills it |
| D11 | Version authority | The bundle reports the app version; the frozen app carries none today, so the **build scripts write `version.txt`** next to the executable, and detection falls back to `CHANGELOG.md` → `MRRC-Modern.iss` → `unknown`. This is also the version authority sub-project 3 needs |
| D12 | Config | The bundle ships `state/config-redacted.env` (MRRC Modern's actual format), never an INI |

## 3. Current state and the gap

| Today | Consequence |
| --- | --- |
| `logging.basicConfig` only | Packaged installs have **no server log file at all** |
| `Popen` without redirection | The server's stdout/stderr is lost when the console closes; `launcher.log` exists only for launcher-level failures |
| `start.sh` / `install.sh` write `logs/ft710-server.log` / `logs/server.log` | Three different names for the same idea across launch modes |
| No version file in the bundle | The app cannot tell a bundle reader what it is |
| No in-app report path | Problems arrive as prose ("it crackles"), and the first exchange is spent asking for evidence |

Copying the sibling project's `support_bundle.py` as-is would produce a **bundle with no log content and a
config file it cannot parse** — that is the gap this spec closes.

## 4. Architecture and data flow

```
static/support.html                server.py                          support_bundle.py
  problem + contact + client ctx ──▶ POST /api/support/bundle ──▶ collect_logs()      (tail ≤2 MB/line-aligned)
                                    (auth_middleware)              redact_env_text()   (allow-list + secrets)
  ┌── generated listing ◀─────────── {id,size,files,redactions,warnings}  summarize_log()  (triage verdicts)
  │                                single-flight, newest 5        build_bundle()      (zip + manifest sha256)
  ├─ POST /api/support/upload ─────▶ PUT <MRRC_SUPPORT_URL>/api/create + /api/<id>/bundle
  │                                   │
  │                                   └─▶ support-receiver-modern :8098 (own systemd unit)
  │                                        /var/www/support-modern/<id>/{bundle.zip,meta.json}
  └─ POST /api/support/save ───────▶ copy to <data dir>/support-out/<id>.zip (manual send path)

  <data dir>/logs/{server.log, server-stdout.log} + <data dir>/launcher.log  ◀── new logging (§5)
```

Three new units with narrow responsibilities:

| Unit | Responsibility | Depends on |
| --- | --- | --- |
| `support_bundle.py` (repo root, stdlib only, no app imports) | Redaction, tail reading, triage summary, env snapshot, zip assembly | nothing (unit-testable, and hot-fixable later — sub-project 4) |
| `server.py` (`/api/support/*` + logging setup) | Wire real paths and live state into the builder; serve the page | `support_bundle`, existing `_env`/auth helpers |
| `static/support.html` | Ask the operator two questions and drive three buttons | the three endpoints only |

## 5. Log persistence (prerequisite, behaviour change)

| File | Written by | Lifetime | Failure class it covers |
| --- | --- | --- | --- |
| `<data dir>/logs/server.log` | `RotatingFileHandler` in `server.py` (3 × 2 MB, UTF-8, same format as the console) | permanent, rotates | everything from logging-setup onward (the overwhelming majority) |
| `<data dir>/logs/server-stdout.log` | launcher tee of the child's stderr/stdout | **recreated at each launch, closed once `wait_for_server()` returns** | "died before logging existed": PyInstaller missing module, `Failed to load Python shared library`, port already in use |
| `<data dir>/launcher.log` | existing `report_fatal` | overwritten per failure | launcher-level startup failures |
| `logs/server.log` (source mode) | the server handler itself (no launcher sets `MRRC_LOG_DIR`) | permanent, rotates | source/Linux runs started by hand |
| `logs/server-stdout.log` (systemd) | `install.sh`'s unit, renamed to this in the same change | per run | Raspberry Pi without a launcher, same class as the tee |

- New env `MRRC_LOG_DIR` (default `_runtime_dir()/logs`), set by both launchers to the **user data
  directory** — same convention as `MRRC_MEM_FILE` / `MRRC_RECORDINGS_DIR` (a user-writable path is
  mandatory: `Program Files` and `/Applications` are not writable).
- The tee uses a reader thread that **also echoes to the launcher's stdout**, so the Windows console keeps
  showing what it shows today (no regression for operators who watch it).
- Name reconciliation: `install.sh`'s systemd redirect moves to `logs/server-stdout.log` (the tee's name) so
the same file never gets two writers; `start.sh` keeps `logs/ft710-server.log` for the process stdout while
the handler adds `logs/server.log` next to it — distinct names, no double writers, and the bundle collects
whatever exists.
- Rotation happens at launch time for the tee (`os.replace` to `.1`); the server's handler rotates itself.

## 6. Bundle content and privacy contract

```
support-<YYYYmmdd-HHMMSS-xxxx>.zip
├── problem.txt                  # operator's words + contact (may be empty)
├── README.txt                   # what is inside, what is never inside
├── manifest.json                # id, createdAt, version, files[], per-file sha256, redactions, warnings
├── logs/                        # ≤2 MB per file, truncated on a line boundary; flat names, the
│   ├── server.log, server.log.1            #  origin path is recorded per file in manifest.json
│   ├── server-stdout.log                   # from <data dir>/logs
│   ├── launcher.log                        # from the data dir root
│   └── ft710-server.log                    # from the install dir (source/systemd mode)
├── state/config-redacted.env    # allow-listed keys, secret values replaced
└── diagnostics/
    ├── summary.txt              # auto-triage conclusions (read this first)
    ├── env.json                 # version/OS/Python/CPU/frozen + backend, serial port+baud, scope mode
    │                            #   (real FT4222 / CI-V 0x27 / S-meter synthetic), audio device table with
    │                            #   host API + actual rate + channels, recording and ATR switches
    └── client.json              # browser side: UA/device, WS reconnect count, worklet state, recent JS errors
```

**Allow-list (env keys that may appear)** — the diagnostic surface only:

`MRRC_RADIO_MODEL`, `MRRC_SERIAL_PORT`, `MRRC_BAUD_RATE`, `MRRC_WEB_HOST`, `MRRC_WEB_PORT`,
`MRRC_SCOPE_PORT`, `MRRC_SCOPE_BAUD`, `MRRC_AUDIO_RX_DEVICE`, `MRRC_AUDIO_TX_DEVICE`,
`MRRC_FTDI_LIB_DIR`, `MRRC_ATR1000_HOST`, `MRRC_ATR1000_PORT`, `MRRC_SSL_CERT`, `MRRC_SSL`,
`MRRC_RECORDINGS_BITRATE`, `MRRC_RECORDINGS_MAX_SESSION_MIN`, `MRRC_ALLOW_UNVERIFIED_TX`, `MRRC_CQ_FILE`.

Two independent passes, in this order:

1. **Key allow-list** — a line whose key is not listed above is dropped and counted. `MRRC_WEB_PASSWORD` and
   `MRRC_SSL_KEY` are therefore excluded by omission, not by pattern.
2. **Value pass over every collected text** (the redacted config *and* each log file) — any
   `password|passwd|secret|token|api_key|credential` occurrence followed by `=` or `:` has its value replaced
   with `<redacted>` and is counted.

The manifest's `redactions` field is the sum of both counts, so the page can state what happened.

**Never in the bundle** (enforced by a test, not by convention):

| Excluded | Why |
| --- | --- |
| `certs/` (private key, self-signed cert) | A leaked private key is worse than a lost bug report |
| `recordings/` audio content | The operator's QSOs are other people's voices; never shipped by a button press |
| `mem_channels.json`, `atr1000_tuner.json` | Operator data with no diagnostic value |
| `?token=` values appearing in logs | Replaced by the secret pass and counted |

`summary.txt` carries the conclusions the maintainer reads first, ported from the reference implementation
and adapted to this project's log vocabulary: startup count vs `Traceback` count (upgrade/restart is not a
crash), data freshness (>24 h ⇒ "this may not be the failure's scene"), audio device facts (`-9996` with an
empty device list = no audio device on this machine, expected in a VM), serial reconnect/`ENXIO` storms
(`[Errno 6]` / `Device not configured`), scope mode (real vs synthetic S-meter fallback), TX gate state for
unverified models, and recording-writer warnings (`recording queue full` / `dropping audio`).

## 7. REST API contract

All three endpoints sit behind the existing `auth_middleware` — no exemption, no new auth mechanism.

| Endpoint | Body | Response |
| --- | --- | --- |
| `POST /api/support/bundle` | `{problem, contact, client}` | `{ok:true, id, size, files[], redactions, warnings[]}` or `{ok:false, reason}` |
| `POST /api/support/upload` | `{id}` | `{ok:true, remoteId, size}` or `{ok:false, reason, localPath}` |
| `POST /api/support/save` | `{id}` | `{ok:true, path}` or `{ok:false, reason}` |

- **Single-flight**: one build at a time; a second request while a build is running returns
  `{ok:false, reason:"build in progress"}` instead of queueing (a stuck build must not pile up).
- **Retention**: `<log dir>/../support-out/` keeps the newest 5 `support-*.zip`; older ones are deleted after
  a successful build. The temp files of a failed build are removed.
- **Upload**: `MRRC_SUPPORT_URL` (default `https://www.vlsc.net/mrrc_modern/support/`, tests override it),
  `POST api/create` then `PUT api/<id>/bundle` with the zip as the request body, 20 s timeout, **no retry
  loop** (the receiver rate-limits 5 creates/minute/IP; a storm helps nobody).
- **Unknown/expired id** → `{ok:false, reason:"unknown bundle"}`; the client keeps offering 「只保存到本地」.

## 8. Frontend

- `static/support.html` — standalone, mobile-first, dark amber theme consistent with `ft710.css`.
  Two inputs (problem text, contact) and three buttons (生成诊断包 / 上传给维护者 / 只保存到本地), a
  monospace listing of what was collected with the redaction count, and warnings rendered as ⚠ lines.
  Inline script like the sibling project's page; no framework, no build step.
- Data collection for `diagnostics/client.json` happens **in the page** (UA, `navigator` platform, WS
  reconnect counter and worklet state read from the SPA's globals when present, a bounded ring of recent
  window `error`/`unhandledrejection` messages).
- Menu entry in `static/index.html`:
  `<li><a href="/support.html" target="_blank" rel="noopener" class="menu-item">🐞 遇到问题</a></li>` —
  a plain anchor deliberately: `ft710_ui.js:1793` only intercepts `.menu-item[data-action]`, so the two
  tooling-guarded files stay untouched, and `target="_blank"` keeps the SPA's WebSocket (and any PTT
  ownership) alive while the operator reports.
- Upload success shows the returned id, the answers-page URL (`/mrrc_modern/answers/#<id>`) and the
  reminder that the local copy is still on disk.
- Cache-bust bumps (`ft710_main.js?v=`, `ft710_ui.js?v=`, `ft710.css?v=`, service worker `mrrc-vNN`) only if
  a guarded file changes; `support.html` is a new URL and needs none.

## 9. Receiver and deployment

- **Vendored** `tools/support_receiver/server.py` — the sibling project's 262-line stdlib server, copied with
  a provenance header naming the source and the local changes: `product` recorded in `meta.json`, default
  port 8098, and this project's paths. Vendoring (not a cross-repo dependency) keeps both products
  independently deployable; the header makes the origin auditable.
- `deploy_support_receiver.sh` — idempotent: `systemd` unit `support-receiver-modern`, `SUPPORT_DIR=/var/www/support-modern`
  (**outside the docroot**), `SUPPORT_PORT=8098`, password from `~/.mrrc-support-credentials.txt` (the
  existing mechanism) written to `/etc/mrrc-modern-support.env` (0600 root, not printed, not in git),
  nginx `location /mrrc_modern/support/` → `127.0.0.1:8098`, plus a `401`-without-password probe.
- The maintainer list/download UI stays behind Basic auth (`/api/list`), and uploads are rate-limited.
- Accepted (reference design, restated as a risk in §14): `api/create` + `api/<id>/bundle` are
  unauthenticated by design, because the uploading machine cannot hold a server secret. Protection is
  rate limiting, unguessable ids, an auth-gated list, and manual deletion.

## 10. Answers page placeholder

`website/answers/index.html` — a static, JS-free page stating what the numbers mean and that answers are
published there; added to `website/deploy.sh`'s required-file list. Sub-project 2 replaces it with the
searchable answers index. Ship it now so the success message links somewhere real (D10).

## 11. Failure paths and degradation

| Scenario | Behaviour |
| --- | --- |
| No log file at all | Bundle still builds; `warnings` says so; `summary.txt` reports "数据新鲜度：无日志文件" |
| Logs older than 24 h | `summary.txt` + `warnings` say the bundle is probably not the failure scene |
| Config file unreadable | `state/config-redacted.env` contains a comment instead; warning added |
| Bundle would exceed 20 MB | Per-file tails are reduced (2 MB → 512 KB → 128 KB) and the effective size is recorded; the receiver cap is never hit |
| No network / receiver unreachable | `{ok:false, reason, localPath}` → page offers 「只保存到本地」; the zip is still on disk |
| Receiver returns 429/rate-limited | Reason passed through verbatim, no retry |
| Client not logged in | 401 → the page tells the operator to log in first (mirrors `support.html`'s 401 branch) |
| Log directory not writable/creatable | Logging degrades to console-only with a single WARNING (never a startup failure); the bundle then reports "no log file" like the first row |
| Operator closes the page mid-build | The build finishes server-side; the id and file remain valid for a later upload |

## 12. Test plan (no hardware; boundaries mocked)

| Module | Coverage |
| --- | --- |
| `tests/test_support_bundle.py` | allow-list keeps the diagnostic keys and drops the rest; secret regex counts hits; `MRRC_WEB_PASSWORD`/`MRRC_SSL_KEY` never appear; tail is line-aligned and byte-bounded; summary verdicts (startups vs tracebacks, staleness >24 h, `-9996`+empty devices, ENXIO, TX gate, recording drops); bundle contents + manifest sha256 consistency; **negative privacy tests** (no `certs/`, no `recordings/`, no memory/ATR files even when present) |
| `tests/test_support_api.py` | the three endpoints happy path; 401 without a session; single-flight rejection; retention keeps 5; unknown id; upload failure returns `localPath` and leaves the zip; oversized-log degradation path |
| `tests/test_support_receiver.py` | vendored receiver: id regex/traversal refusal, `meta.json` written with `product`, rate limit, 401 on list without a password, deploy script's invariants (port 8098, storage outside docroot, 0600 env file, no password literal) |
| `tests/test_support_frontend.py` | the menu entry exists and is a plain anchor (**no** `data-action`, so `ft710_ui.js`'s `[data-action]`-scoped binding keeps working — asserted, rather than freezing the guarded files, which legitimately change when cache-bust versions bump); `/support.html` exists and references the three endpoints; the page's inputs/buttons contract |
| logging tests (extend `tests/test_quiet_logging.py`) | `MRRC_LOG_DIR` honoured; rotating handler attached once; tee stops after the server is up; log file is created on a fresh data dir |

Documentation of the counts: `tests/README.md` and AGENTS.md test totals are updated in the same change
(the suite is currently 1103 tests / 55 modules).

## 13. Verification checklist (acceptance)

1. **Desktop (macOS + Windows install) and Raspberry Pi**: one press produces a bundle containing a
   **non-empty** log and a `summary.txt` with a startup-count line.
2. **Upload**: the id appears on the receiver's list, downloads successfully, and the downloaded
   **SHA-256 equals the local one**.
3. **Privacy**: `grep` for the real web password and for the cert private key inside the bundle returns
   **zero hits**; the file list contains no recording, memory-channel or ATR file.
4. **Offline**: with the network down, upload fails but 「只保存到本地」 produces a complete bundle.
5. **Gates**: `python -m unittest discover -s tests` green; `.agents/skills/dual-platform-release/harness/release_check.py`
   offline rules green (version consistency across CHANGELOG/`.iss`/website/SDD, so the website and guide
   changes are covered); the frontend guards still hold per §12.

## 14. Accepted risks and explicit boundaries

| Risk | Why accepted / mitigation |
| --- | --- |
| Redaction is best-effort | A secret can only leak through a log line that contains it; the value pass is applied to every collected text and the count is visible in the manifest. Reviewed in the field, not claimed as a guarantee |
| Unauthenticated upload endpoints | Inherited from the reference design (see §9); rate limit + auth-gated list + manual deletion |
| Receiver outage blocks the upload path | The local-save path is not optional — it is a first-class button, and the spec requires it to work with the receiver down |
| Disk growth from bundles | Newest 5 retained, one active build, temp cleaned |
| Placing logs in the user data directory | The only writable, per-user location on all three platforms; it is also where `MRRC_MEM_FILE`/`MRRC_RECORDINGS_DIR` already live |
| Two writers, one name (systemd vs the handler) | Prevented by giving the systemd redirect the tee's name (`server-stdout.log`) — the collision is checked by a test on `install.sh` |

Not verified by this change (documented, not implied): the Windows install path end-to-end
(KVM has no audio device beyond the log evidence), the Raspberry Pi image build, and the maintainer's
nginx/Basic-auth deployment (verified by the deploy script's probes when it is run).

## 15. Non-goals

- 5 s RX audio sample, screenshots, or any other capture of the operator's traffic (D4).
- AI triage, model calls, the answers index, and the public reply workflow (sub-project 2).
- One-click upgrade, `latest.json`, `state.json` success semantics (sub-project 3).
- Hotfix packaging and the overlay mechanism (sub-project 4). `support_bundle.py` stays stdlib-only so that
  sub-project can override it later without a release.
- Changing the WebSocket protocol, the CAT/audio paths, or anything the radio depends on. **No operator-facing
  behaviour changes outside the new logging files and the menu entry.**

## 16. SDD traceability

| Item | Action |
| --- | --- |
| `SDD/08-architecture-decisions.md` | New **AD-021** (server-built, allow-list-redacted diagnostics bundle + dedicated receiver) + index row |
| `SDD/10-service-model.md` | `SupportService` row in §10.1 and §10.3 interface table |
| `SDD/12-operational-model.md` | §12.6 gains the log-file inventory and the bundle; §12.5 gains "how to deploy the receiver" and "how to read a bundle" |
| `SDD/13-feasibility-assessment.md` | New risk: redaction completeness + an unauthenticated receiver endpoint |
| `SDD/14-version-history.md` | New row (SDD version bump) |
| `SDD/05-non-functional-requirements.md` | NFR for "user data never leaves the machine except in a redacted, operator-initiated bundle" |
| `README.md` / `AGENTS.md` | Env table (`MRRC_LOG_DIR`, `MRRC_SUPPORT_URL`), module table (support_bundle + tools/support_receiver), test counts — **AGENTS.md is already stale** (says 1055 tests / 53 modules while `tests/README.md` and the suite say 1103 / 55): correct it in this change |
| `docs/PROJECT_MAP.md` | New "support chain" row: code ↔ design ↔ tests ↔ docs ↔ deploy |
| `tests/README.md` | New modules and counts |
| `CHANGELOG.md` | Feature entry (unreleased) |
| `website/guide.html` + `website/zh/guide.html` | A short "遇到问题怎么报" section (bilingual pair, site audit rule) |
| `.agents/skills/dual-platform-release/SKILL.md` + `mac_pack.md` / `win_pack.md` / `pi_pack.md` | The bundled-file inspection step must now also assert that `version.txt` inside the built artifact equals the CHANGELOG's top version. It is a **build product**, so `release-artifacts.json` cannot carry an offline rule for it — the artifact-time check is where it is enforceable (same place the existing bundle inspections live). A source-level test asserts each build script still derives it from the CHANGELOG |
| `.agents/skills/sdd-guardian/harness/constraints.json` | Guard: "a diagnostics bundle never contains secrets, certificates or recording content" |
| `website/deploy.sh` | `answers/index.html` added to the required-file list |

Per the skill's rule ("every incident leaves a guard"), the guards registered by this change are: the
privacy negative tests (§12), the `version.txt` release rule, and the systemd/tee name-collision test.
