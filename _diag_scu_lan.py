#!/usr/bin/env python3
"""
SCU-LAN10 self-check — session, CAT, spectrum, audio, PTT read-back
==================================================================
One-shot field diagnostic for a radio behind a Yaesu SCU-LAN10 (FTDX101MP/D,
FTDX10, FT-710).  It answers the questions that cannot be settled offline, in
the order they can fail:

1. **Does the unit complete the seven-phase handshake?**  (five init packets →
   auth → session id → continuation → CAT/audio/scope setup → keepalives)
2. **Does CAT answer over the tunnel?**  `ID`, `FA`, `MD`, `SM`, `SS` — with
   round-trip times, because the poll scheduler runs at 100 ms.
3. **What does the sweep actually look like?**  Body length, boundary-marker
   position, usable bin count and amplitude range for the current span; with
   `--spans` it repeats that for every `SS` span code, which is the only way to
   learn whether the "usable = marker ÷ 2" rule (measured on an FTDX10) also
   holds on an FTDX101.
4. **What does the RX audio carry?**  Packet rate, peak/RMS and optionally a WAV.
5. *(with `--tx --allow-tx`)* **Does a key-up actually land, and does `TX;`
   read back?**  That read-back is the guard the server's release path depends
   on.  Transmit audio is **not** sent (its frame format is still unknown), so
   this keys the radio for at most `--tx-seconds`.

Usage::

    python _diag_scu_lan.py --host 192.168.1.50 --user defaultuser --pass secret
    python _diag_scu_lan.py --host ... --user ... --pass ... --spans
    python _diag_scu_lan.py --host ... --user ... --pass ... --wav rx.wav
    python _diag_scu_lan.py --host ... --user ... --pass ... --tx --allow-tx

The report is Markdown so it can be pasted into a ticket.  Nothing here needs
the repo's server to be running, and nothing is written except the files you
name.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
import wave
from collections import deque
from typing import Optional

import numpy as np

from backends.yaesu.scu_lan import protocol as proto
from backends.yaesu.scu_lan import scope_format
from backends.yaesu.scu_lan.client import ScuLanClient

#: How long to wait for the seven-phase handshake before calling it a failure.
#: Module-level so a test can shorten it (a unit with a wrong password answers
#: nothing, and the wait is the whole point of that check).
CONNECT_TIMEOUT_S = 10.0
AUTH_TIMEOUT_S = 6.0
#: Per-command CAT wait.  A poll on the server is 1 s, so 0.8 s is already
#: past "the tunnel is slow" and into "the tunnel is not answering".
CAT_TIMEOUT_S = 0.8


class CatProbe:
    """Sequential CAT request/response over the tunnel.

    The radio identifies a reply by its command mnemonic, never by a request
    id, so matching is by the two-letter prefix — the same rule the serial CAT
    core uses for its AI-frame filter.
    """

    def __init__(self, client: ScuLanClient) -> None:
        self._client = client
        self._waiters: dict[str, deque] = {}
        self._loop = asyncio.get_running_loop()
        client.on_cat = self._on_frame
        self.unsolicited: list[str] = []

    def _on_frame(self, text: str) -> None:
        prefix = text[:2].upper()
        queue = self._waiters.get(prefix)
        if queue:
            future = queue.popleft()
            if not future.done():
                future.set_result(text)
            return
        if len(self.unsolicited) < 200:
            self.unsolicited.append(text)

    async def ask(self, command: str, timeout: float = 0.8) -> Optional[str]:
        command = command if command.endswith(";") else command + ";"
        prefix = command[:2].upper()
        future = self._loop.create_future()
        self._waiters.setdefault(prefix, deque()).append(future)
        if not self._client.send_cat(command):
            return None
        done, _pending = await asyncio.wait({future}, timeout=timeout)
        if not done:
            queue = self._waiters.get(prefix)
            if queue and future in queue:
                queue.remove(future)
            return None
        return future.result()


class Collector:
    """Scope bodies, audio payloads and the times they arrived."""

    def __init__(self, limit: int = 400) -> None:
        self.scope: deque = deque(maxlen=limit)
        self.audio: deque = deque(maxlen=limit)
        self.scope_times: deque = deque(maxlen=limit)
        self.audio_times: deque = deque(maxlen=limit)

    def attach(self, client: ScuLanClient) -> None:
        client.on_scope = self._on_scope
        client.on_audio = self._on_audio

    def _on_scope(self, body: bytes) -> None:
        self.scope.append(body)
        self.scope_times.append(time.monotonic())

    def _on_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)
        self.audio_times.append(time.monotonic())

    def clear(self) -> None:
        self.scope.clear()
        self.audio.clear()
        self.scope_times.clear()
        self.audio_times.clear()

    def rate(self, times: deque) -> float:
        if len(times) < 2:
            return 0.0
        span = times[-1] - times[0]
        return (len(times) - 1) / span if span > 0 else 0.0


def _hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def _sweep_report(body: bytes) -> dict:
    marker = scope_format.find_spectrum_end(body)
    usable = scope_format.usable_bin_count(body)
    bins = scope_format.signal_bins(body)
    return {
        "len": len(body),
        "marker": marker,
        "usable": usable,
        "min": min(bins) if bins else None,
        "max": max(bins) if bins else None,
        "mean": round(sum(bins) / len(bins), 1) if bins else None,
    }


_CONVERTER = None


def _pcm_to_48k_mono(payload: bytes) -> np.ndarray:
    """The audio adapter's own conversion (16 kHz stereo → 48 kHz mono)."""
    global _CONVERTER
    if _CONVERTER is None:
        from backends.yaesu.scu_lan.audio import ScuLanAudioHandler
        _CONVERTER = ScuLanAudioHandler(None)
    return _CONVERTER.convert(payload)


async def run(args) -> int:
    client = ScuLanClient(args.host, args.user, args.password,
                          base_port=args.port, auth_timeout=AUTH_TIMEOUT_S)
    collector = Collector()
    collector.attach(client)
    report: list[str] = []
    exit_code = 0

    def say(line: str = "") -> None:
        print(line, flush=True)
        report.append(line)

    say(f"# SCU-LAN10 self-check — {args.host}:{args.port}")
    say()
    say(f"- time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    say(f"- user: `{args.user}`")
    say(f"- radio model (from MRRC_RADIO_MODEL, informational): "
        f"`{args.model or '(unset)'}`")
    say()

    await client.start()
    connected = await client.wait_until_connected(
        timeout=CONNECT_TIMEOUT_S)
    if not connected:
        say("## 1. Session — FAILED")
        say()
        if client.auth_failed:
            say("The unit stayed silent on the auth packet.  That is what a "
                "wrong username/password looks like (there is no error reply).")
        else:
            say("No answer at all.  Check, in this order: the address is an "
                "**IPv4** address of the unit; the box can reach it (same LAN "
                "or a route); UDP 50000-50003 are not firewalled; the unit is "
                "powered and its LAN LED is on.")
        say()
        await client.stop()
        print("\n".join(report))
        return 2

    snaps = client.snapshot()
    say("## 1. Session — OK")
    say()
    say(f"- session id: `0x{snaps['session_id']:02X}`")
    say(f"- handshake: {snaps['handshakes']}, reconnects: {snaps['reconnects']}")
    say(f"- capability bytes: `{_hex(bytes.fromhex(snaps['capability']))}` "
        f"(constant across all known captures; meaning unknown)")
    say()

    probe = CatProbe(client)
    say("## 2. CAT over the tunnel")
    say()
    say("| command | answer | round trip |")
    say("| --- | --- | --- |")
    latencies: list[float] = []
    answers: dict[str, Optional[str]] = {}
    for command in ("ID", "FA", "MD0", "SM0", "SS05", "SS06"):
        started = time.monotonic()
        answer = await probe.ask(command, timeout=CAT_TIMEOUT_S)
        elapsed = (time.monotonic() - started) * 1000
        answers[command] = answer
        if answer:
            latencies.append(elapsed)
        say(f"| `{command};` | `{answer or '(no answer)'}` | "
            f"{elapsed:.0f} ms |")
    say()
    if latencies:
        say(f"- median round trip: {sorted(latencies)[len(latencies)//2]:.0f} ms "
            f"(the poll scheduler runs at 100 ms)")
    else:
        say("- **no CAT answer at all** — the session is up but the CAT channel "
            "is not; report the session id above, it identifies this run.")
        exit_code = 3
    say()

    say("## 3. Spectrum")
    say()
    await asyncio.sleep(args.seconds)
    if not collector.scope:
        say("- **no scope bodies arrived.**  Check that the radio's scope "
            "display is on, and that the unit is not streaming to another "
            "client.")
        exit_code = max(exit_code, 3)
    else:
        first = collector.scope[-1]
        info = _sweep_report(first)
        say(f"- bodies in {args.seconds:.0f} s: {len(collector.scope)} "
            f"({collector.rate(collector.scope_times):.1f}/s)")
        say(f"- body length: {info['len']} bytes (protocol says 4096)")
        say(f"- boundary marker at bin {info['marker']}; usable bins "
            f"{info['usable']} (= marker ÷ 2); resampled to "
            f"{scope_format.SPECTRUM_BINS}")
        say(f"- amplitude range (inverted, higher = stronger): "
            f"{info['min']}..{info['max']} mean {info['mean']}")
        say(f"- first 16 bytes: `{_hex(first[:16])}`")
        say()
        say("Read it like this: a marker between 500 and 3000 and a usable "
            "count in the hundreds means the FTDX10 rule holds on this radio.  "
            "A marker stuck at 400 (the fallback) or a flat amplitude range "
            "means it does not, and the sweep is encoded differently.")
        if args.csv:
            import csv
            try:
                with open(args.csv, "w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(["bin", "value"])
                    for index, value in enumerate(
                            scope_format.signal_bins(first)):
                        writer.writerow([index, value])
            except OSError as exc:
                say(f"- could not write `{args.csv}`: {exc}")
            else:
                say(f"- native-resolution bins written to `{args.csv}`")
    say()

    if args.spans:
        say("## 3b. Per-span sweep shape")
        say()
        say("| SS span code | set | read back | marker | usable bins | max |")
        say("| --- | --- | --- | --- | --- | --- |")
        for code in range(9):
            await probe.ask(f"SS05{code}", timeout=0.6)
            readback = await probe.ask("SS05", timeout=0.6)
            collector.clear()
            await asyncio.sleep(0.6)
            if collector.scope:
                info = _sweep_report(collector.scope[-1])
                say(f"| {code} | yes | `{readback or '(none)'}` | "
                    f"{info['marker']} | {info['usable']} | {info['max']} |")
            else:
                say(f"| {code} | yes | `{readback or '(none)'}` | — | — | — |")
        say()

    say("## 4. RX audio")
    say()
    if not collector.audio:
        say("- **no audio bodies arrived** (the audio channel may have been "
            "refused by the unit).")
    else:
        payload = np.frombuffer(
            b"".join(list(collector.audio)[:20]), dtype="<i2")
        peak = np.abs(payload.astype(np.int32)).max() if payload.size else 0
        rms = (np.sqrt(np.mean(payload.astype(np.float32) ** 2))
               if payload.size else 0.0)
        say(f"- bodies in {args.seconds:.0f} s: {len(collector.audio)} "
            f"({collector.rate(collector.audio_times):.1f}/s, 100/s expected)")
        rate = int(getattr(client, "audio_rate", 0) or 0)
        say(f"- payload per body: {len(collector.audio[-1])} bytes")
        if rate:
            frames = len(collector.audio[-1]) // 4
            say(f"- declared rate: {rate} Hz ({frames} stereo frames = "
                f"{frames * 1000 / rate:.1f} ms per body)")
            if rate != 16000:
                say("    (the official client offers PCM 48 kHz too; the "
                    "server follows the declared rate, but the reverse-engineered "
                    "captures were all 16 kHz — worth a note in the report)")
        else:
            say("- declared rate: (no valid rate in the inner header)")
        say(f"- peak {peak} / rms {rms:.0f} (16-bit scale)")
        if args.wav:
            pcm = np.concatenate([_pcm_to_48k_mono(body)
                                  for body in list(collector.audio)])
            pcm = np.clip(pcm, -32768, 32767).astype("<i2")
            with wave.open(args.wav, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(48000)
                handle.writeframes(pcm.tobytes())
            say(f"- 48 kHz mono WAV written to `{args.wav}` "
                f"({pcm.size / 48000:.1f} s)")
    say()

    if args.tx:
        if not args.allow_tx:
            say("## 5. PTT read-back — SKIPPED (needs `--allow-tx`)")
            say()
        else:
            say("## 5. PTT read-back")
            say()
            before = await probe.ask("TX", timeout=1.0)
            await probe.ask("TX1", timeout=1.0)
            await asyncio.sleep(0.3)
            keyed = await probe.ask("TX", timeout=1.0)
            await asyncio.sleep(args.tx_seconds)
            await probe.ask("TX0", timeout=1.0)
            await asyncio.sleep(0.3)
            released = await probe.ask("TX", timeout=1.0)
            say(f"- before: `{before or '(no answer)'}`")
            say(f"- after TX1: `{keyed or '(no answer)'}`")
            say(f"- after TX0: `{released or '(no answer)'}`")
            say()
            say("`TX` answers start with the state: `TX0…` = receive, `TX1…` = "
                "transmit (on the DB9 units the answer carries more digits; the "
                "first digit is the state).  The server's release path depends "
                "on this read-back, so record what you saw here.")
            say()

    if probe.unsolicited:
        say("## 6. Unsolicited CAT frames (first 10)")
        say()
        say("```")
        for line in list(probe.unsolicited)[:10]:
            say(line)
        say("```")
        say()

    await client.stop()
    say("## 7. Link counters")
    say()
    for key, value in client.snapshot().items():
        say(f"- `{key}`: {value}")

    if args.report:
        try:
            with open(args.report, "w", encoding="utf-8") as handle:
                handle.write("\n".join(report) + "\n")
        except OSError as exc:
            print(f"\nCould not write {args.report}: {exc}")
        else:
            print(f"\nReport written to {args.report}")
    return exit_code


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="SCU-LAN10 session/CAT/spectrum/audio/PTT self-check")
    parser.add_argument("--host", required=True, help="unit's IPv4 address")
    parser.add_argument("--user", default="defaultuser",
                        help="login user set in the SCU-LAN10 (factory: defaultuser)")
    parser.add_argument("--pass", dest="password", default="defaultuser",
                        help="login password (factory: defaultuser)")
    parser.add_argument("--port", type=int, default=50000,
                        help="base UDP port (factory 50000; unit listens on +0..+3)")
    parser.add_argument("--model", default="",
                        help="radio model key, for the report only")
    parser.add_argument("--seconds", type=float, default=5.0,
                        help="how long to collect scope/audio (default 5)")
    parser.add_argument("--csv", default="", help="write one sweep's bins to CSV")
    parser.add_argument("--wav", default="", help="write the RX audio to a WAV")
    parser.add_argument("--spans", action="store_true",
                        help="sweep every SS span code and report the marker "
                             "position for each")
    parser.add_argument("--tx", action="store_true",
                        help="key the radio briefly and check the TX read-back")
    parser.add_argument("--allow-tx", action="store_true",
                        help="required for --tx: this puts the radio on air")
    parser.add_argument("--tx-seconds", type=float, default=2.0,
                        help="how long to stay keyed with --tx (max 5)")
    parser.add_argument("--report", default="", help="write the report to a file")
    args = parser.parse_args(argv)
    if args.tx_seconds > 5.0:
        parser.error("--tx-seconds must be <= 5")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted — the client was stopped, no PTT was involved.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
