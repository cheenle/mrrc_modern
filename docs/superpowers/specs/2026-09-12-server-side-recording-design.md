# Server-Side QSO Recording (incremental MP3, mrrc-compatible layout)

**Date:** 2026-09-12

**Status:** Approved for implementation

**Scope:** Replace the browser-side QSO recorder with a server-side recorder that taps the
device-domain RX PCM and the decoded TX mic PCM, writes an incrementally-encoded MP3 to disk
while recording, and exposes a recordings panel (list / inline player / download / delete) in
the web UI. Modeled on the sibling `mrrc` project's `RecordingSession` + `recordings/` layout.

## 1. Objective

An operator presses REC and gets a single MP3 of the whole QSO — the other station plus their
own voice, on one time-aligned mono timeline — that plays back smoothly and can be listed,
streamed, downloaded and deleted from the web UI. No browser-side encoding, no blob download,
no dependency on the tab staying in the foreground.

## 2. Decisions taken during design review (2026-09-12)

| # | Question | Decision |
| --- | --- | --- |
| D1 | Scope | **B** — server-side recording plus a recordings panel (list with frequency/date/duration/size, inline player, download, delete, sorted newest-first like mrrc's `get_recordings_list()`); REC state visible to every client |
| D2 | File format / dependency | **MP3 via `lameenc`** (option C). No ffmpeg (it is not shipped and would add ~40–80 MB per installer); `lameenc` is a small prebuilt wheel bundling libmp3lame, so the artifact matches mrrc's (`<freq>kHz_<date>_<time>.mp3`) |
| D3 | Timeline sample rate | **16 kHz mono** (option B) — mrrc parity, and mrrc's `voice_assistant_service.py` consumes `recordings/` directly, so keeping the same format lets those files flow into existing tooling |
| D4 | Retention | **Keep everything, delete only manually** (option A). The panel shows total disk usage; no automatic deletion of recordings |
| D5 | Capture strategy | **Incremental encode, write-through to disk** (approach 2) — constant RAM, crash-safe, no encode spike at stop |
| D6 | Control plane | **Hybrid** — start/stop over the existing authenticated `/WSradio` WebSocket plus a broadcast `recordingState`; list/stream/delete over REST |
| D7 | Ownership | **None** — any authenticated client may start/stop; recording is shared state and the state is broadcast to all (does not attempt to solve open issue I6) |
| D8 | Auto-recording | **Not in this change.** Manual REC only: "what counts as one QSO" is a product rule that deserves its own design once the manual flow is in use |
| D9 | Post-processing | **None** — no normalization/trimming, because incremental encoding cannot re-write earlier frames |

## 3. Root cause of the reported stutter (why browser recording is being replaced)

Field symptom: recorded MP3 playback "trembles" (颤抖/哆嗦). Code evidence in the current
implementation (`static/ft710_main.js`):

- The recorder is fed `f32` frames and **concatenates whatever arrives** — it carries no
  timestamps, so the file's timeline is whatever the delivery order happened to be.
- The RX delivery path can drop frames: the ScriptProcessor fallback trims its queue
  (`while (queue.length > 50) queue.shift()`), and a WebSocket stall produces nothing at all.
- The playback jitter buffer (`rx_worklet_processor.js`) deliberately pads/duplicates frames to
  smooth playout, but the recorder does **not** pad — the same jitter that is inaudible live
  is baked into the file as time compression/expansion.

Rate mismatch was ruled out: `AudioRX_sampleRate` is a constant `48000` and the Opus decoder
emits 48 kHz, so the MP3 header rate always matched the fed samples.

A server-side recorder removes the whole class of failure: it taps the sound-card PCM before
any network path and places every block on a monotonic timeline, filling real gaps with
silence.

## 4. Architecture and data flow

New protocol-neutral module `recorder.py` (root level, alongside `radio_state.py` /
`audio_resample.py`): `RecordingSession`, `_StreamingDecimator`, `parse_recording_name()`.

```
RX:  AudioHandler.read_rx_chunk()          device domain 44.1k (FT-710) / 48k (Icom)
       |  read when "rx clients exist OR recording active"
       +-> encode_rx_audio() -> /WSaudioRX                         (unchanged)
       +-> recorder.add_rx(pcm, dev_rate)                          (new)
              \ skipped while radio.tx_status != 0 (PTT or TUNE, from any client)
TX:  /WSaudioTX -> _opus_tx_decoder.decode() 48k -> audio.feed_tx_audio()   (unchanged)
       +-> recorder.add_tx(pcm)                                    (new)

inside recorder: 44.1k -> 48k (audio_resample) -> FIR decimate 48k -> 16k
                 -> timeline placement -> silence gap fill
                 -> lameenc incremental encode -> write()
```

**Timeline rules (ported from mrrc):** every block carries a `time.monotonic_ns()` timestamp
taken in the caller; a source switch or a real pause re-anchors to the timeline; a **50 ms
continuity tolerance** absorbs scheduler jitter (jitter is tolerated rather than turned into a
silence gap — this is the property that makes playback smooth); only genuine gaps are filled
with silence.

**Threading:** `add_rx`/`add_tx` only enqueue and record a timestamp (microseconds, event loop
stays free). A dedicated writer task — same shape as `_audio_tx_drain_loop` — drains the queue
and performs encoding + file writes through `asyncio.to_thread`, so encoder CPU can never stall
CAT or audio.

**Failure isolation:** no recorder exception may reach the audio path (guarded, rate-limited
logging); after repeated failures the session stops itself and broadcasts an error instead of
throwing per block.

## 5. RecordingSession and incremental encoding

- `start(freq)` — create `<dir>/<freq:05d>kHz_<YYYYmmdd>_<HHMMSS>.mp3` (mrrc naming; `freq = 0`
  becomes `00000kHz`, matching mrrc), create the lameenc encoder (mono, 16 kHz, 64 kbps CBR,
  quality 2), anchor the timeline.
- `add_audio(source, pcm, sample_rate, timestamp_ns)` — decimate, `_write_upto(offset)` emits
  encoder silence for any gap, encode the block, append to the file.
- `stop()` — pad trailing silence to the final duration, `flush()`, update the index, return
  file info for the broadcast.
- **Crash safety:** the file is on disk throughout (a missing Xing header does not stop browsers
  from playing it). Constant RAM (encoder state + FIR state + one block) versus mrrc's in-memory
  buffer, which holds ~115 MB for a one-hour recording — relevant because this project ships a
  Raspberry Pi image.
- **Session cap:** `MRRC_RECORDINGS_MAX_SESSION_MIN` (default 240, `0` = unlimited). This is a
  forgot-to-stop guard, not a retention policy: it stops the session and broadcasts, and never
  deletes anything (consistent with D4).
- **Encoder settings:** `MRRC_RECORDINGS_BITRATE` (default 64 kbps CBR) → ≈28.8 MB/hour. mrrc's
  `ffmpeg -q:a 0` (VBR ≈200 kbps) is deliberately **not** mirrored: unpredictable file sizes are
  at odds with "keep everything".
- **Index `recordings.json`** in the runtime directory (same place and pattern as
  `mem_channels.json`): `name`, `freq`, `started_at`, `duration`, `bytes`. When the index is
  missing or incomplete, duration is computed from the file size and the configured CBR
  bitrate; index entries whose files no longer exist are dropped — index bookkeeping only, the
  server never deletes a recording by itself (D4). MP3s the operator drops into the directory
  themselves (normal mrrc practice) therefore appear in the panel too.

## 6. Control plane and security

**WebSocket — reuse the authenticated `/WSradio` (no new endpoint):**

- Inbound: `{"type":"set","field":"recording","value":true|false}` — handled by the existing
  set-command router.
- Outbound: `recordingState` carrying `recording`, `started_at`, `duration`, `freq`, `name`,
  `bytes`. Broadcast on every change plus a 1 Hz refresh while recording (piggybacked on the RX
  audio loop's 20 ms tick every 50th iteration — no new task). The same fields ride in
  `fullState` so a new client or a page reload is immediately in sync, satisfying D1's
  "visible to every client".

**REST — session-cookie authenticated like `/api/devices`:**

| Route | Behaviour |
| --- | --- |
| `GET /api/recordings` | `{recordings: [...], total_bytes, count}`, newest first (mrrc ordering) |
| `GET /api/recordings/{name}` | `FileResponse`; Starlette 1.3.1 implements `Range`, so the inline player can seek |
| `DELETE /api/recordings/{name}` | delete + update index; **409 while that file is being recorded** |

**Path safety:** the name must match `^\d{5}kHz_\d{8}_\d{6}\.mp3$` and the resolved path must be
contained in the recordings directory (the I8 lesson). There is no "arbitrary filename" route.

**Multi-client:** no ownership (D7). Any authenticated client may start/stop; the trigger source
is logged.

## 7. Frontend

- **REC button** (`index.html`, next to TUNE — position unchanged): sends
  `{"type":"set","field":"recording", ...}`. `renderRecordingState()` is rewritten to read
  server state (`recordingState` / `fullState`), showing `STOP` plus a live timer while
  recording, and no longer touches `window.RXRecorder`.
- **Recordings panel** (new ☰ menu item), built exclusively with `createElement`/`textContent`
  (the repository's XSS lint gate; pattern of `showMemoryManager()`): header with total usage,
  count and refresh; one row per recording with frequency (`00000` shown as `—`), date/time,
  duration, size, and Play / Download / Delete actions; a single inline
  `<audio controls preload="none">` whose `src` is set on Play; Delete asks for confirmation and
  is disabled for the file currently recording.
- **Browser recorder removal:** `window.RXRecorder`, `feedRXRecorderFrame`,
  `feedTXRecorderFrame` and their three TX capture call sites, the `_loadLame()` lazy loader,
  `RX_MP3_BITRATE`, `RX_RECORDER_MIME`/`RX_RECORDER_EXT`, `_downloadRecording`, `_f32ToInt16`,
  and the now-dead `static/modules/lame.js` (530 KB — not listed in `sw.js`; the old loader pulled it in dynamically).
- **Cache-busting:** `ft710_main.js?v=28→29`, `ft710_ui.js?v=30→31`, `sw.js mrrc-v31→v32`, with
  the pinned assertions in `tests/test_server_ws_protocol.py` and `tests/test_audio.py` updated
  in the same change.

## 8. Dependencies and packaging

- `requirements.txt` gains `lameenc>=1.8.0` (prebuilt wheels for macOS arm64/x86_64, Windows
  x64, Linux aarch64 — including the Raspberry Pi image).
- PyInstaller: `lameenc` is a C extension (`lameenc._lameenc`), so the spec must collect it
  (`collect_submodules("lameenc")`). **Both packaged platforms must be verified** (macOS DMG via
  the local build script, Windows installer via the Win11 KVM VM) by recording a short sample
  from the frozen app and playing it back.
- Both launchers gain `env.setdefault("MRRC_RECORDINGS_DIR", str(user_data_dir() / "recordings"))`
  next to the existing `MRRC_MEM_FILE` / `MRRC_ATR1000_STORE` lines.
- `config.py` gains `RECORDINGS_BITRATE` (64) and `RECORDINGS_MAX_SESSION_MIN` (240);
  `server.py` defines `RECORDINGS_DIR` (it needs `_runtime_dir()`, next to `MEM_FILE`).
- Raspberry Pi: `/opt/mrrc_modern` is already owned by the `mrrc` user, so the default directory
  is writable; `docs/RASPBERRY_PI_GUIDE.md` gains a short section on where recordings land and
  what that means for SD-card capacity.

## 9. Test plan (no hardware; mocks at the audio and filesystem boundaries)

| Module | Coverage |
| --- | --- |
| `tests/test_recorder.py` (new) | decimator (mrrc vectors: passband fidelity, stopband rejection, state continuity across blocks); timeline (silence gap fill, 50 ms tolerance absorbing jitter, source-switch re-anchoring, monotonic ordering); incremental encoding (file grows while recording, `flush()` finalizes, duration ≈ expected, abandoned session still leaves a playable prefix); filename generation/parsing |
| `tests/test_recorder_api.py` (new) | list ordering and total usage; `Range` request → 206 + `Content-Range`; delete success / 404 / **409 while recording**; name whitelist rejects `../`, absolute paths and non-`.mp3`; unauthenticated 401 |
| `tests/test_server_ws_protocol.py` (extend) | `set{field:"recording"}` routing; `recordingState` broadcast on change and at 1 Hz; `fullState` carries the fields; **idle-skip regression** — PCM must still be read while recording with zero RX clients |
| `tests/test_audio.py` (modify) | replace the four lamejs assertions with the server-side contract; keep "REC sits right of TUNE"; assert `lame.js` is no longer referenced |
| `tests/test_config.py` (extend) | bitrate and session-cap env parsing and defaults |

## 10. Documentation synchronization

- New spec (this file) and a plan under `docs/superpowers/plans/`.
- `SDD/08`: **new AD-017** — server-side recording with incremental MP3, why a 16 kHz storage
  domain exists and how it relates to AD-011 (the recording tap is a write-only sink: 44.1→48
  goes through the sanctioned `audio_resample`, 48→16 is a purpose-built anti-aliasing
  decimator, and nothing from the recording path re-enters the codec or device domain).
- `SDD/14` new entry + `SDD/README` Quick Facts; `SDD/05` **NFR-066** (16 kHz mono MP3 ≤64 kbps,
  duration accuracy, observable disk usage); `SDD/07.2` (RecordingSession / RecordingFile
  entities); `SDD/09` (audio chain gains the recording tap) and `SDD/10`/`SDD/11` (Recorder
  component/service rows); `SDD/12` (`MRRC_RECORDINGS_DIR` and friends); `SDD/13` **R10** (disk
  growth — accepted deliberately per D4, mitigated by the panel's total-usage readout) and
  **A8** (prebuilt `lameenc` wheels exist for every target platform; verified during packaging).
- `AGENTS.md` (module table, env vars, WS message list), `README.md`, `tests/README.md`,
  `CHANGELOG.md`, `docs/OPERATION_GUIDE.md`, `docs/RASPBERRY_PI_GUIDE.md`.
- `website/guide.html` + `website/zh/guide.html` (the paragraph describing the browser download
  and the 500 KB lazy encoder) and regenerated `website/sdd/*` via `website/build_sdd.py`.
- `.agents/skills/sdd-guardian/harness/index.json`: route `recorder.py` to AD-017 / NFR-066 /
  R10 so a future change to recording cannot silently drop the design record.

## 11. Accepted risks and explicit boundaries

- **Disk growth is unbounded by design** (D4). The panel shows total usage; there is no
  auto-deletion. Registered as R10 with the operator decision recorded rather than "fixed".
- **No ownership arbitration** (D7): two clients pressing REC concurrently means the second
  start is refused (a session is already active); two clients pressing STOP means the second
  stop is a no-op. This is intentional and does not pretend to resolve I6.
- **No post-processing** (D9): no normalization, trimming or re-encoding of finished files.
- **No auto-segmentation** (D8): one file per REC press.
- **Not verified without hardware:** that the RX tap is glitch-free on a real radio/audio device
  (the device-domain PCM quality depends on the sound card and the V2.31 `restart_rx()` path),
  and that `lameenc` loads inside both frozen installers. Both are operator/build-machine checks
  listed in the plan.

## 12. Non-goals

- No ffmpeg dependency, no other encoder (WAV/Opus) in this change.
- No transcription, speech-to-text, voice-assistant integration or automatic upload.
- No edits to the codec/device audio domains: Opus encode/decode, `audio_resample.py` and the
  TX/RX stream handling keep their current rates and behaviour.
- No new WebSocket endpoint (and therefore no new auth surface).
- No change to the FT-710/Icom CAT paths, PTT safety layers, or the spectrum pipeline.

## 13. SDD traceability

| Reference | Relationship |
| --- | --- |
| AD-017 (new) | Defines the recording architecture and the 16 kHz storage domain |
| AD-011 | Related, not contradicted — the 16 kHz domain is reached *from* the 48 kHz codec domain by a write-only sink; `audio_resample.py` remains the only codec↔device SRC bridge |
| AD-004 | Unchanged — tagged dual-codec audio for transport; recording taps PCM before/after that path, never through it |
| AD-005 / AD-006 | Unchanged — spectrum path untouched |
| AD-016 | Reused — `recorder.py` is protocol-neutral, so both backends record identically: the tap reads the device rate from the audio handler (`capabilities.audio_rx_rate` → `AudioHandler._rx_dev_rate`), so the FT-710's 44.1 kHz and the Icom family's 48 kHz both work with no backend-specific code |
| NFR-001 / NFR-008 | Preserved — recorder work happens off the event loop and cannot add latency to audio or PTT |
| NFR-004 | Related — recordings travel over HTTP (Range-capable) rather than the WebSocket audio path |
| NFR-066 (new) | The recording quality/size/observability requirement this change introduces |
| NFR-041 | Extended — `MRRC_RECORDINGS_DIR`, `MRRC_RECORDINGS_BITRATE`, `MRRC_RECORDINGS_MAX_SESSION_MIN` |
| §7.2 | New entities RecordingSession / RecordingFile |
| §9.3 / §9.4 | Audio chains gain the recording tap description |
| §9.7 | WS protocol gains the `recordingState` message |
| §12 | Operational model gains the recordings directory and the three recordings REST routes in the browser↔server interface table |
| NFR-020 | Honoured — every recordings route requires the same session cookie as `/api/status`; no new unauthenticated surface |
| NFR-050 | Served — recording lives in one new protocol-neutral module (`recorder.py`) plus small taps in `server.py`; no audio-domain code is edited |
| R10 / A8 (new) | Disk growth (accepted) and the `lameenc` wheel assumption |
| I6 | Explicitly not addressed (D7) |
| I8 | Honoured — strict filename whitelist + containment check on every recordings route |
| I10 | Related — the recordings panel is a plain HTTP consumer, so the subchannel reconnect work does not apply |
| R8 | Honoured — cache-bust versions bumped with their pinned assertions |
