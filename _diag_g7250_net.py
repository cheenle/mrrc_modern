#!/usr/bin/env python3
"""
G7250 network self-check — handshake, CI-V tunnel, audio
========================================================
One-shot field diagnostic for the Xiegu G7250's LAN port (Icom's network-radio
protocol).  It answers the three questions that cannot be settled offline, in
the order they can fail:

1. **Does the radio complete the login handshake?**  (are-you-there → I-am-here
   → I-am-ready → login → token → capabilities → conninfo → stream request)
2. **Does its CI-V tunnel (port 50002) answer `0x27`?**  — i.e. can we get a real
   waterfall over the network.  The USB port of the field unit ignored `0x27`
   while wfview's waterfall came from here, so this is *the* question.
3. **What does the audio stream actually carry?**  Packet count, sample rate,
   peak/RMS and (optionally) a WAV we can listen to.

Usage::

    python _diag_g7250_net.py --host 192.168.110.199 --user G7250 \
        --pass beta_tester [--seconds 5] [--wav out.wav] [--no-civ]

Nothing here transmits.  The CI-V probes are read-only plus the two
scope-enable commands (`27 10 01`, `27 11 01`), which the server sends anyway
and which `finally` turns back to their safe state only if we changed them.

The report is Markdown so it can be pasted into a ticket; addresses/credentials
are printed because a field report without them cannot be acted on (the radio's
network password is the radio's own, not a customer credential).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from array import array

from backends.icom_net import protocol as proto
from backends.icom_net.client import IcomNetworkClient

#: Read-only probes first; the two `01` writes enable the scope stream the same
#: way the server does, so an answer here means the server's waterfall works.
CIV_PROBES = [
    ("read frequency (must answer)", bytes.fromhex("FEFE94E003FD")),
    ("read scope state 27 00", bytes.fromhex("FEFE94E02700FD")),
    ("scope display ON 27 10 01", bytes.fromhex("FEFE94E0271001FD")),
    ("scope data output ON 27 11 01", bytes.fromhex("FEFE94E0271101FD")),
    ("read mode 04", bytes.fromhex("FEFE94E004FD")),
]


def _hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


class Collector:
    def __init__(self) -> None:
        self.audio: list[bytes] = []
        self.civ: list[bytes] = []
        self.states: list[tuple[float, str]] = []
        self.started = time.monotonic()

    def on_audio(self, pcm: bytes, rate: int) -> None:
        self.audio.append(pcm)

    def on_civ(self, frame: bytes) -> None:
        self.civ.append(frame)


async def probe(args) -> dict:
    got = Collector()
    client = IcomNetworkClient(
        args.host, args.user, args.passwd,
        on_audio=got.on_audio,
        client_name="mrrc-diag",
        login_timeout=args.login_timeout,
        audio_stall_timeout=args.seconds + 5,
    )
    if not args.no_civ:
        client.on_civ = got.on_civ

    report: dict = {"host": args.host, "user": args.user, "steps": [], "civ": [],
                    "audio": {}, "packets": []}

    def step(name: str, ok: bool, detail: str = "") -> None:
        got.states.append((time.monotonic() - got.started, name))
        report["steps"].append({"at": round(got.states[-1][0], 2), "step": name,
                                "ok": ok, "detail": detail})
        print(f"[{got.states[-1][0]:5.2f}s] {'ok  ' if ok else 'FAIL'} {name}"
              f"{(' — ' + detail) if detail else ''}")

    step("client start", True)
    await client.start()
    deadline = time.monotonic() + min(args.login_timeout + 6, 12)
    while time.monotonic() < deadline and not client.connected:
        await asyncio.sleep(0.1)
    st = client.stats
    step("radio answered", bool(st.radio_name or client.remote_id),
         f"id=0x{client.remote_id:08X}")
    step("logged in", st.logins > 0)
    step("capabilities", bool(st.radio_name),
         f"{st.radio_name!r} CI-V=0x{st.radio_civ_address:02X} "
         f"{st.rx_sample_rate} Hz codec=0x{st.rx_codec:02X}")
    step("stream open", client.connected,
         f"radio CI-V port {client.civ_port}")
    report["radio"] = {"name": st.radio_name,
                       "civ": st.radio_civ_address,
                       "sample_rate": st.rx_sample_rate,
                       "codec": st.rx_codec,
                       "civ_port": client.civ_port,
                       "audio_port": getattr(client, "radio_audio_port", 0)}

    # ── CI-V tunnel ────────────────────────────────────────────────
    if not args.no_civ and client.civ_port:
        await client.open_civ()
        await asyncio.sleep(0.4)
        for name, frame in CIV_PROBES:
            before = len(got.civ)
            client.send_civ(frame)
            await asyncio.sleep(args.civ_wait)
            answers = [f for f in got.civ[before:] if f]
            report["civ"].append({"probe": name, "sent": _hex(frame),
                                  "answers": [_hex(a) for a in answers]})
            print(f"  CI-V {name}: {_hex(frame)}")
            if answers:
                for a in answers:
                    print(f"      <- {_hex(a)}")
            else:
                print("      <- (no answer)")
        report["civ_silent"] = not got.civ

    # ── audio ──────────────────────────────────────────────────────
    t0 = time.monotonic()
    seen = 0
    while time.monotonic() - t0 < args.seconds:
        await asyncio.sleep(0.25)
        if len(got.audio) != seen:
            seen = len(got.audio)
    pcm = b"".join(got.audio)
    rate = client.stats.rx_sample_rate or client.rx_sample
    info: dict = {"packets": len(got.audio), "bytes": len(pcm), "rate": rate}
    if pcm:
        samples = array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        peak = max((abs(s) for s in samples), default=0)
        rms = (sum(s * s for s in samples) / max(1, len(samples))) ** 0.5
        info.update(peak=peak, rms=round(rms, 1), seconds=round(len(samples) / rate, 2))
        if args.wav:
            with wave.open(args.wav, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(pcm)
            info["wav"] = args.wav
    report["audio"] = info
    step("audio flowing", bool(info.get("packets")),
         f"{info.get('packets', 0)} packets, peak {info.get('peak', 0)}, "
         f"rms {info.get('rms', 0)}")

    await client.stop()
    return report


def render_markdown(rep: dict) -> str:
    lines = ["## G7250 网口自检", ""]
    lines.append(f"- 电台：`{rep['host']}`，用户名 `{rep['user']}`")
    radio = rep.get("radio") or {}
    if radio:
        lines.append(f"- 电台自报：`{radio.get('name')}`，CI-V "
                     f"`0x{(radio.get('civ') or 0):02X}`，"
                     f"{radio.get('sample_rate')} Hz，codec "
                     f"`0x{(radio.get('codec') or 0):02X}`")
        lines.append(f"- 端口：CI-V `{radio.get('civ_port')}`，音频 "
                     f"`{radio.get('audio_port')}`")
    lines.append("")
    lines.append("| 步骤 | 结果 | 说明 |")
    lines.append("| --- | --- | --- |")
    for s in rep["steps"]:
        lines.append(f"| {s['step']} | {'✅' if s['ok'] else '❌'} | {s['detail']} |")
    if rep.get("civ"):
        lines.append("")
        lines.append("### CI-V 隧道（0x27 是否应答）")
        lines.append("")
        for c in rep["civ"]:
            ans = "<br>".join(f"`{a}`" for a in c["answers"]) or "（无应答）"
            lines.append(f"- {c['probe']}：发送 `{c['sent']}` → {ans}")
    audio = rep.get("audio") or {}
    lines.append("")
    lines.append("### 音频")
    lines.append("")
    if audio.get("packets"):
        lines.append(f"- {audio['packets']} 包 / {audio['bytes']} 字节，"
                     f"{audio.get('seconds')} s @ {audio.get('rate')} Hz，"
                     f"peak {audio.get('peak')}，rms {audio.get('rms')}")
        if audio.get("wav"):
            lines.append(f"- WAV：`{audio['wav']}`")
    else:
        lines.append("- **没有音频包**")
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="radio IP")
    ap.add_argument("--user", default="G7250", help="radio network user")
    ap.add_argument("--pass", dest="passwd", default="beta_tester",
                    help="radio network password")
    ap.add_argument("--seconds", type=float, default=5.0, help="audio capture time")
    ap.add_argument("--civ-wait", type=float, default=0.7, help="wait per CI-V probe")
    ap.add_argument("--login-timeout", type=float, default=4.0)
    ap.add_argument("--wav", default="", help="write the captured audio here")
    ap.add_argument("--no-civ", action="store_true", help="skip the CI-V tunnel")
    ap.add_argument("--json", action="store_true", help="also print the raw report")
    args = ap.parse_args(argv)

    rep = asyncio.run(probe(args))
    print()
    print(render_markdown(rep))
    if args.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    return 0 if rep["steps"] and all(s["ok"] for s in rep["steps"]) else 1


if __name__ == "__main__":
    sys.exit(main())
