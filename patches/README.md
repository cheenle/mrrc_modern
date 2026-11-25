# patches/terminal_media_tx_safety.patch

> **2026-11-25：已合入仓库并随 `1.28.98 · FP85` 发布。** 两份补丁的全部改动现在在
> `server.py` / `audio_levels.py` 里（`_terminal_offer` 的新溢出策略、持键 socket 停发
> 频谱、400 ms 弹跳守卫、按键序列号去重、释放路径 `try/except`、0 dB 峰值判据、
> deflate 关闭、Opus `to_thread`、GC 探针 + `freeze`/`threshold`），并有 12 条新测试盯着。
> 这两个文件保留为**现场取证记录**：它们记的是当时在部署文件上手改了什么、为什么，
> 以及每条改动背后的读数。`patches/` 已加入 `stage-tree.sh` 的排除清单 —— 镜像里不该
> 出现开发材料。

Applied to the **deployed** box (`192.168.1.217:/opt/mrrc_modern`) on 2026-11-23
and verified there. It is kept here because the working copy of `server.py` in
this repository is 409 lines away from the deployed one (this tree carries other
in-progress work), so `git apply` does **not** land cleanly — merge it by hand or
rebase onto the deployed file rather than forcing the hunks.

What it changes, and the measurement behind each one:

1. **Terminal media queue: keep the freshest audio.**
   `_terminal_offer()` used to enqueue with `put_nowait` and give up when full —
   dropping the *newest* frame and keeping up to 64 slots (1.28 s) of the oldest
   audio. On a 1430 s session over a cellular path the terminal received
   48.2 audio frames/s while the radio produced 50/s: 2537 frames (3.7%) dropped,
   the queue never got shorter, and the operator heard up to 1.28 s of history.
   Now overflow discards the *oldest* slot (freshness wins) and spectrum never
   displaces audio (repeatable picture vs. unrepeatable voice, one queue, one TCP).

2. **Spectrum yields to the keyed socket (half duplex).**
   AD-004 gated the *audio* downlink for the socket holding the PTT; the spectrum
   fan-out never got the same gate, so a keyed terminal still received waterfall
   frames on the same TCP that carries its 32 KB/s uplink. Measured:
   `Terminal client disconnected during TX! Forcing RX.` at 11:30:23, right at the
   tier change, with no session record left behind.

3. **Clip monitor is no longer blind at 0 dB.**
   `apply_gain()` returns early when the gain is exactly 0.0 (identity, no numpy
   work), so `clipped` is always 0; `ClipMonitor.note()` additionally refused to
   warn when `gain_db <= 0`. Together that made a full-scale TX silent in the log.
   Now the TX path measures `peak_of(pcm)` on the identity path and warns once when
   the source sits at the rail: "no headroom left; lower the source, not the box".

4. **One `TX session` line per transmission.** The release path runs more than once
   per release (device release + watchdog + cleanup) and each call logged the same
   finished session: a 17.7 s transmission produced three identical `frames=884`
   lines, so any session count taken from the log was wrong. Guarded by
   `(frames, monotonic)` with a 5 s window.

5. **The release path cannot raise.** On 2026-11-23 a `NameError` inside this block
   (a missing `global` declaration, from item 4) left the radio keyed for **15.2 s**
   until the transmit liveness watchdog forced RX. Release is the one path where an
   exception is a safety event (SDD chapter 15 §15.3, `mrrc-passport`), so the
   stats/logging block is now wrapped in `try/except` and `await cat.set_ptt(False)`
   always runs.

Rollback: every edit kept a timestamped backup beside the deployed file
(`/var/tmp/server.py.bak_*`, `/var/tmp/audio_levels.py.bak_*`).

6. **A keyer releasing its own PTT 0.1 s after keying is its watchdog.** While a
   browser tab was open, transmissions were ending after 5 frames (0.1 s): the tab
   re-sent `ptt:false` from stale local state, and because that socket *was* the
   keyer, `_ptt_release_allowed()` let it through. Releases from the keyer within
   400 ms of its own key-up are now ignored, logged, and answered with the
   authoritative `tx_status` so the sender resyncs. The 5 s / 15 s liveness
   watchdogs still cover a genuinely stuck carrier.

7. **One `TX session` line per key-up, by serial.** A session could end before
   `start_tx` finished resetting the audio counters, producing lines like
   `frames=5 written=322`, and the 5 s time-window dedupe missed the same session
   logged 6 s later. Both come from the same cause — no session identity — so the
   key-up now bumps a serial and the line is written once per serial.

8. **Key-up broadcasts the keyer to everyone else.** `radio.update(tx_status=1)`
   does reach the other sockets through the periodic dirty-field broadcast, but a
   browser tab's PTT watchdog keys off its own local state and kept re-sending
   `ptt:false` — the 0.1 s transmissions again. Key-up now pushes
   `{"fields": {"tx_status": 1}, "ptt_keyed_by_other": true}` to every control
   socket except the keyer, which is the same frame the foreign-release path
   already used. The 400 ms bounce guard stays as the second line of defence.

9. **A press that carried no audio says so.** `TX session: frames=0 peak=0%`
   looked like any other session, so "I pressed and nothing went out" had no
   explanation. Anything under 10 frames (200 ms) now also logs
   `TX session carried almost no audio: frames=N peak=P%`.

10. **permessage-deflate off, Opus off the event loop.** py-spy on the box (25 s,
    880 samples) showed `PerMessageDeflate.encode` at 1.9 % and
    `RxOpusEncoder.push` at 9.1 %, both inside the event loop; the server's
    payloads are 20 ms audio frames and spectrum blocks, neither of which
    compresses. `uvicorn.Config(ws_per_message_deflate=False)` and
    `await asyncio.to_thread(audio.encode_rx_audio, …)` — the encoder is stateful
    but the RX loop is its only caller and awaits before the next tick.

11. **The 200 ms event-loop stalls are gen2 garbage collection.** Proved by
    correlation, not by argument:

    ```
    GC pause 148 ms (gen 2, collected=20)   10:35:54,726
    Event loop stall 200 ms                 10:35:54,763   <- 37 ms later
    ```

    Each stall costs ~10 audio frames, which is exactly the "occasional stutter"
    at the far end. `gc.freeze()` after the app is built (moves the startup graph
    out of the scan set) plus `gc.set_threshold(2000, 20, 20)` (defaults are
    700/10/10, so gen2 passes become rarer). Stalls were ~1/hour before; none in
    the first 25 minutes after — a short window for an hourly event, so this is
    "not yet contradicted" rather than "proved gone".
