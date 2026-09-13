# CQ Key: Server-Side CQ Call Playback

**Date:** 2026-09-13

**Status:** Approved for implementation (semantics A — one-shot call, completed notification)

**Scope:** Add a **CQ** button to the web UI that plays a packaged CQ recording into the radio's
transmit chain. The recording lives on the **server**, the button only starts/stops the call, and
the whole feature rides the existing PTT/TX safety architecture (`server.py` ownership + watchdog,
`AudioHandler`'s device-domain TX queue, SDD ch15). No client-side audio, no new transmit path.

Reference: the sibling project `mrrc` (`www/modules/tune_cq.js` + `cq_file = 'cq.wav'`; its server
answers `client.write_message("cq:complete")` when the call finishes; documented as "Start/stop CQ
playback (cq.wav)"). This design reproduces that behaviour with this project's safety rules.

Authoritative in-repo references:

- `audio_handler.py` — `start_tx()` / `stop_tx(graceful, drain_ms)` / `feed_tx_audio(pcm)`
  (**48 kHz Int16 in**, resampled internally to the radio's device rate), `has_pending_tx_audio()`,
  `write_tx_chunk()` (called by `_audio_tx_drain_loop`), `tx_stats()`.
- `server.py` — the `field == "ptt"` handler (gate → `_claim_tx_owner_for_token` → `cat.set_ptt(True)`
  → `radio.update(tx_status=1)` → `audio.start_tx()`; release = `stop_tx(graceful=True)` → unkey),
  `_max_tx_watchdog()`, the two disconnect paths that force RX, and the `recordingState` pattern
  (`_broadcast_recording_state` + 1 Hz during a session + `fullState` snapshot).
- `audio_resample.resample_pcm(pcm_bytes, in_rate, out_rate)` — the sanctioned SRC, used at asset
  load time only.
- `static/index.html` PTT footer (`#btn-ptt`, `#btn-tune`, `#btn-record`) and `static/ft710_ui.js`
  (`sendCommand()`, `renderRecordingState()`).

## 1. Objective

One tap → the radio keys, plays the CQ recording once, unkeys, and every client sees that the call
completed. Tapping again calls again. A second tap while calling aborts it.

## 2. Decisions taken during design review (2026-09-13)

| # | Decision | Rationale |
| --- | --- | --- |
| 1 | **One-shot semantics (A)** — one press = one call, `complete` broadcast at the end; no loop | Matches `mrrc`; every transmission has a defined end (no unattended carrier) |
| 2 | **Server owns the audio** (packaged asset + `MRRC_CQ_FILE` override) | Works headless/on the Pi, survives a backgrounded tab, single source of truth |
| 3 | **Ride the existing TX path** (`feed_tx_audio` + `start_tx`/`stop_tx`) instead of a new transmit route | Inherits device-rate conversion, drain-to-DAC, queue-drop protection, watchdog, ownership |
| 4 | **Microphone is exclusive during a call** | Prevents CQ + speech mixing; keeps one audio source per transmission |
| 5 | **Only the initiator can abort**; everyone can watch | Same ownership rule as PTT — two clients cannot fight over the carrier |
| 6 | Asset normalised to 48 kHz mono 16-bit **once at startup** | The playback loop stays allocation-light and frame-aligned |

## 3. Protocol

Up (existing single-field `set` message):

```json
{"type":"set","field":"cq","value":true}    // start (one call)
{"type":"set","field":"cq","value":false}   // abort
```

Down — new `cqState` message, broadcast on every change and at 1 Hz while calling:

```json
{"type":"cqState","cq":{
  "state":"idle|calling|complete|aborted",   // a refusal never becomes state
  "duration_s":6.22,      // total asset length
  "elapsed_s":1.40,       // calling: seconds already transmitted
  "frames_total":311, "frames_sent":70,
  "started_by":"7f3a2c",        // opaque per-client id (hex(id(ws))[:6]); only for "谁在呼叫" text
  "reason":null                 // aborted: machine-readable reason (client_gone, aborted_by_user, unkeyed, watchdog)
}}
```

`fullState` carries the same `cq` snapshot, so a client that connects or refreshes mid-call renders
the correct state. **Refusals do not change the broadcast state**: the requester gets the existing
`{"type":"error","message":…}` message (gate text, "CQ already in progress", "Radio is
transmitting", asset not ready) while every client keeps seeing the true state — the UI also disables
the button while a call is running.

## 4. Architecture

```
/WSradio  set{field:"cq",value:true}
        │
        ▼
 server.py  _execute_set_command("cq", …)
        │  gate → busy checks → claim TX owner → cat.set_ptt(True)
        │  → radio.update(tx_status=1) → audio.start_tx()
        ▼
 cq_player.CQPlayer.start()  ──► asyncio.Task (20 ms tick)
        │  loop: while frames_left and radio.is_transmitting
        │        if audio queued depth < CQ_LOW_WATER (=4 frames/80 ms): feed one frame
        │        every 50 ticks: broadcast cqState (1 Hz)
        │  (external unkey detected → stop feeding, state=aborted)
        ▼
 audio.stop_tx(graceful=True)   → tail drains to the DAC (word endings go out)
 cat.set_ptt(False) → radio.update(tx_status=0, meters→0)
 broadcast cqState{state:"complete"}
```

`cq_player.py` (new, ~200 lines) is the only new module; it owns asset loading, the frame list, the
playback task and the state snapshot. It receives the audio handler, the CAT controller, the radio
state, the backend (for the TX gate) and a broadcast callback — no globals, fully testable with fakes.

## 5. Asset handling

- Resolution order: `MRRC_CQ_FILE` (absolute, or relative to the repo/runtime dir) → else
  `<static dir>/audio/cq.wav`. Shipping the file under `static/` means it is already inside every
  PyInstaller bundle (**no spec/datas change**) and is only served to authenticated clients.
- Load once in the lifespan (after the audio handler exists), mirroring `_log_recording_readiness`:
  `CQ ready: <path> (6.2 s, 48 kHz mono)` — or a WARNING naming the reason (missing / unreadable /
  unsupported sample format / longer than `CQ_MAX_SECONDS = 30`) so the operator can see why a
  button would refuse **before** pressing it.
- Accepted input: RIFF/WAVE PCM 16-bit, mono or stereo (mixed down), any rate (resampled at load
  with `audio_resample.resample_pcm`). The last partial frame is zero-padded so writes stay
  20 ms-aligned.
- The source asset `CQCQ.m4a` (6.22 s, 44.1 kHz stereo ALAC) is converted once to
  `static/audio/cq.wav`; the command is documented in the operation guide so an operator can swap in
  their own call.

## 6. Safety (SDD ch15 — no new transmit route)

| Rule | Implementation |
| --- | --- |
| Unverified model never transmits | Same gate check as PTT/TUNE, refused **before** any CAT write, with the existing `_TX_GATE_MESSAGE` |
| One audio source per transmission | While `calling`, `/WSaudioTX` frames are dropped and counted (`_tx_cq_mic_drops`), reported in the TX-session log line |
| One TX owner | The initiator claims the uplink (`_claim_tx_owner_for_token`); a PTT press during the call is refused; a second `cq:true` is refused ("CQ in progress") |
| Never leave a carrier | `cq:false`, initiator disconnect, last-client disconnect, or an external unkey (watchdog / TUNE / another client) all stop the player and release PTT |
| Max TX watchdog | Unchanged: `MRRC_PTT_MAX_TX_SECONDS` still force-ungkeys; the player notices `radio.is_transmitting == False` and marks the call aborted |
| Clean tail | `stop_tx(graceful=True)` drains queued audio before `set_ptt(False)` (same as a PTT release) |
| TUNE mutex | `tune` set is refused while calling; `cq:true` is refused while TUNE is active |

## 7. UI (`static/index.html`, `ft710_ui.js`, `ft710.css`, i18n table)

- New `#btn-cq` in the PTT footer's `.ptt-side`, styled like `#btn-record` (mobile-first, no layout
  skeleton change).
- Tap = start; while calling the button reads「呼叫中 <elapsed>s」and a second tap aborts. On
  `complete` it flashes 「已完成」 for 3 s. All timing comes from `cqState` (no local timer, same
  rule as the recording panel).
- Disabled when the active backend reports `tx_gated` (mirrors PTT/TUNE), with the gate text as the
  tooltip.
- i18n keys added to the existing table: CQ / 呼叫 / 呼叫中 / 中止 / 已完成 / 不可用原因.

## 8. Test plan (TDD — failing test first, per behaviour)

1. **Asset** (`tests/test_cq_player.py`): mono 48 kHz passthrough; stereo downmix; 44.1 kHz →
   48 kHz resample; unsupported sample width rejected; > `CQ_MAX_SECONDS` rejected; truncated file
   rejected with an actionable message; frame count/padding for a 6.22 s asset (311 frames of 960).
2. **Lifecycle**: `start` keys PTT (fake CAT records the call), feeds every frame, calls
   `stop_tx(graceful=True)`, unkeys, ends in `complete`; feeding never exceeds the queue low-water
   mark (asserts `queue_drops == 0` with a fake audio handler).
3. **Guards**: tx-gated backend → refused, no PTT; already calling → refused; PTT held by someone
   else → refused; TUNE during a call → refused.
4. **Exclusivity**: `/WSaudioTX` frames arriving during a call are dropped and counted.
5. **Stops**: `cq:false`, initiator disconnect, last-client disconnect, and an external unkey each
   abort, unkey, and broadcast `aborted` (with the reason).
6. **Broadcast**: `cqState` on change + 1 Hz; `fullState.cq` snapshot for a fresh client.
7. **UI contract** (source-contract style used elsewhere): `#btn-cq` exists, `sendCommand('cq'`,
   `cqState` renderer present, bilingual strings present.

## 9. Documentation synchronization

CHANGELOG (next version), `README.md` (`MRRC_CQ_FILE`), `AGENTS.md` module table (`cq_player.py`),
SDD §9.2 (protocol: `cqState`), §9.4/§15 (TX path + safety), a new **AD-019** (server-side CQ call),
§12 + `docs/OPERATION_GUIDE.md` ("换成自己的呼号录音", with the ffmpeg command), `tests/README.md`
(counts + module entry), website guide (CQ paragraph) and the release notes.

## 10. Risks and explicit boundaries

- **Audio quality/latency on the real radio is unverified here**: the acceptance check is one real
  call with a receiver (operator item, as with every other TX feature).
- `TX_DRAIN_MS` must cover the longest asset tail; today's drain (SDD ch15) already covers a spoken
  word ending. If a future asset ends in a tone, the drain window is the place to adjust.
- A CQ longer than `MRRC_PTT_MAX_TX_SECONDS` (when that opt-in watchdog is enabled) will be cut
  short by design — documented, not "fixed".

## 11. Non-goals

- Looping / unattended CQ (would need its own carrier-duty design).
- CW/voice keyer macros, message sets, per-user assets, scheduling.
- Client-side (iOS/Android) CQ UI in this phase — the protocol and the server behaviour are ready
  for them, and both apps can be updated independently.

## 12. SDD traceability

AD-007 (PTT release safety) and AD-017 (server-side audio session) are reused; AD-019 records the
new decision. Requirements touched: §9.2 (WS protocol), §9.4 (TX audio chain), §12.x (operator
assets), §15 (PTT safety layering), §13 R-list (real-radio acceptance).
