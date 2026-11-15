# SCU-LAN10 Network Protocol

An independent, reverse-engineered specification of the Yaesu SCU-LAN10
network remote control protocol — the UDP wire format used by the
**FT-Control** iOS/desktop app to talk to a SCU-LAN10 box (and through
it, to an FTDX101MP/D, FTDX10, or FT-710 transceiver).

The full spec lives in [**SPEC.md**](SPEC.md).

## Why this exists

The SCU-LAN10 is an Ethernet bridge for Yaesu radios, but its a proprietary protocol.

The official FT-Control app is the only sanctioned client, which makes it impossible to integrate the hardware into third-party logging, contesting, or SDR software.

This is my best attempt at writing the protocol down. The formulas in [SPEC.md](SPEC.md) were derived from ~76,000 captured packets across six sessions, and confirmed by a Swift client I wrote that authenticates, streams audio, decodes the spectrum scope, and tunnels CAT to an FTDX10. Where I wasn't sure, I've flagged it — see [Open Questions](SPEC.md#open-questions).

The goal is to make it easier to build software that talks to Yaesu radios through an SCU-LAN10.

## How this was done

We're noobs to reversing protocols. I captured a few sessions with FT-Control and Wireshark.

1. **Wireshark** to capture UDP traffic between FT-Control (the iOS app) and a real SCU-LAN10 on my LAN. Six sessions across different radio states (idle, RX, TX, scope-on, scope-off, reconnect) gave me ~76,000 packets to work with.
2. **Claude** (the LLM) as my pattern-finding partner. I exported the captures to PCAP/JSON, fed them to Claude, and bounced hypotheses off it: "what's invariant across these packets?", "what changes between client→server and server→client?", "what byte position correlates with the session ID?". When a hypothesis looked plausible, I tested it by running candidate decryption functions over the captures and checking whether the output made sense — ASCII CAT commands, believable amplitude distributions for the scope, valid PCM audio.
3. **A Swift client against a live FTDX10** as the falsification engine. Anything that could authenticate, stream audio, and decode the scope was treated as correct; anything that couldn't sent me back to the captures.

Note that there was no firmware extraction, proprietary tooling, or FT-Control disassembly. I hope that allows others with the hardware to be able to do the same analysis.

If you want to do the same kind of reverse engineering on a different
protocol, this is a viable recipe.

## What's covered

- **Packet framing** — `5A 5A` magic, length, type, 8-byte header
- **Header encoding** — sequence, channel, message type, encryption key
- **XOR encryption** — per-packet key + position-dependent scramble table
- **Session lifecycle** — auth handshake, channel setup, keepalives
- **CAT tunneling** — how Yaesu CAT commands are wrapped on UDP
- **Audio stream** — 16 kHz PCM Int16 LE, stereo interleave, inner header
- **Scope/waterfall data** — 4096-byte spectrum packets, amplitude encoding

## What's *not* covered

- The **CAT command set itself** — those are documented in Yaesu's own
  CAT Operation Reference manuals (FTDX10, FTDX101MP/D, FT-710), which
  are publicly available. This spec only describes the transport.
- **TX audio** — partially observed (668-byte frames) but not fully
  characterized yet.
- Firmware versions prior to those listed in [SPEC.md](SPEC.md).

## Status

The spec should be **complete enough to build a working client**. The [Carrier Wave](https://carrierwave.app) macOS app ("CW Sweep") uses this protocol in production: authenticates, streams the FTDX10's spectrum scope, plays audio, and round-trips CAT commands.

Open issues / known unknowns are tracked in [SPEC.md § Open Questions](SPEC.md#open-questions).

## How to use this spec

The protocol is straightforward once you have the formulas — a minimal client is a few hundred lines of code in any language with UDP sockets. Suggested reading order:

1. [§ Overview](SPEC.md#overview) — the four UDP ports
2. [§ Packet Framing](SPEC.md#packet-framing) — the wire envelope
3. [§ Header](SPEC.md#8-byte-header) — what each header byte means
4. [§ Encryption](SPEC.md#encryption) — the XOR + scramble table
5. [§ Session Lifecycle](SPEC.md#session-lifecycle) — handshake walkthrough
6. Then jump to whichever channel you care about (CAT / Audio / Scope)

A reference Python implementation of the decryption is in
[§ Reference Code](SPEC.md#reference-code).

## Contributing

We'd love to make this more robust! Corrections, additional captures (especially from radios other than the FTDX10), and notes on undocumented behavior are very welcome. Open an issue or PR.

Things that would be especially useful:

- Captures from **FTDX101MP/D** or **FT-710** to confirm the protocol is
  identical across all three radios
- TX audio captures with the inner header fully characterized
- Captures of error conditions (bad auth, simultaneous client, etc.)
- The semantics of the `01 25 01 3F 51` capability bytes in the auth
  response — these are constant across all known captures but their
  meaning is unconfirmed
- Whether the protocol works over IPv6 (current assumption: no)

## Disclaimer

This is an independent reverse-engineering effort. The authors are not
affiliated with, endorsed by, or sponsored by Yaesu Musen Co., Ltd. or
Yaesu USA. "SCU-LAN10", "FT-Control", "FTDX10", "FTDX101MP", "FTDX101D",
and "FT-710" are trademarks of their respective owners.

No Yaesu firmware, source code, or other proprietary material is
included in this repository. The spec was derived solely from
observation of network traffic generated by legally-purchased hardware
and freely-available client software.

Use at your own risk. The protocol may change in firmware updates; no
compatibility guarantees are made.

## License

MIT — see [LICENSE](LICENSE).

The spec text is also offered under MIT for maximum reuse. If you'd
prefer a Creative Commons license for the documentation alone, file an
issue.
