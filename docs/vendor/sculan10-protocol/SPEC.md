# SCU-LAN10 Network Protocol — Specification

Reverse-engineered specification of the Yaesu SCU-LAN10 network remote
control protocol. All formulas in this document have been verified
against ~76,000 captured packets and confirmed against an
independently-written client.

> See [README.md](README.md) for background. This file is the spec.

---

## Table of Contents

- [Overview](#overview)
- [Supported Transceivers](#supported-transceivers)
- [Network Architecture](#network-architecture)
- [Packet Framing](#packet-framing)
- [8-Byte Header](#8-byte-header)
- [Encryption](#encryption)
- [Session Lifecycle](#session-lifecycle)
- [Steady-State Operation](#steady-state-operation)
- [Keepalives](#keepalives)
- [CAT Channel (Port +1)](#cat-channel-port-1)
- [Audio Channel (Port +2)](#audio-channel-port-2)
- [Scope Channel (Port +3)](#scope-channel-port-3)
- [Reconnection](#reconnection)
- [NAT / Firewall](#nat--firewall)
- [Reference Code](#reference-code)
- [Open Questions](#open-questions)

---

## Overview

The Yaesu SCU-LAN10 is a network remote control interface for Yaesu
transceivers. It bridges the radio's CAT, audio, and spectrum scope to a
remote client over **UDP** on four ports. All four ports share the same
packet framing and per-packet XOR encryption scheme.

This spec describes:

- The wire format (framing + header + encryption)
- The connection handshake (auth + per-port setup)
- The four logical channels (CTRL, CAT, AUDIO, SCOPE)
- Steady-state behavior (keepalives, CAT tunneling, audio, scope)

It does **not** describe the CAT command set itself — those commands
are standard Yaesu CAT (e.g., `FA007007000;` to set VFO-A to 7.007 MHz)
and are documented in the publicly-available CAT Operation Reference
manuals for each radio.

## Supported Transceivers

| Radio | Minimum firmware |
|-------|------------------|
| FTDX101MP / FTDX101D | MAIN V01-14, DISPLAY V01-06, SDR V02-03 |
| FTDX10 | MAIN V01-01, DISPLAY V01-01, SDR V01-00 |
| FT-710 | MAIN V01-11, DISPLAY V01-08, SDR V01-04 |

The radio must be configured with **CAT RATE 38400 bps (115200 over
LAN)**, **CAT TIMEOUT TIMER 10 ms**, and **CAT RTS ON**.

The protocol observed on the wire is identical across all three radios
(confirmed for FTDX10; FTDX101 and FT-710 contributions welcome — see
[Open Questions](#open-questions)).

## Network Architecture

The SCU-LAN10 listens on **four consecutive UDP ports**. Default base
port is 50000; configurable in the SCU-LAN10 web UI in the range
49990–50009 (each port can be individually offset within that range,
though factory default keeps them at base+0..3).

| Offset | Port (default) | Channel | Channel ID (b2 ^ key) | Direction |
|--------|----------------|---------|-----------------------|-----------|
| +0 | 50000 | CTRL | `0xFC` | Bidirectional — auth, setup, keepalives |
| +1 | 50001 | CAT | `0xFD` | Bidirectional — Yaesu CAT commands |
| +2 | 50002 | AUDIO | `0xFE` | Bidirectional — RX + TX audio |
| +3 | 50003 | SCOPE | `0xFA` | Server → Client data; Client → Server keepalive only |

There is **no TCP**. Every packet is UDP. The client is responsible for
retransmission where reliability matters (CAT commands are sent twice;
see [CAT Channel](#cat-channel-port-1)).

## Packet Framing

Every UDP datagram has this structure:

```
┌────────┬────────────┬──────────┬──────────────────┬──────────────────┐
│ Magic  │ Length     │ Type     │ Header (8 B)     │ Encrypted body   │
│ 5A 5A  │ LE u16     │ 00 01    │  unencrypted     │     variable     │
└────────┴────────────┴──────────┴──────────────────┴──────────────────┘
  2 bytes   2 bytes      2 bytes      8 bytes              N bytes
```

| Field | Bytes | Meaning |
|-------|-------|---------|
| Magic | 2 | Always `5A 5A` (ASCII "ZZ"). Used as a sentinel. |
| Length | 2 | Little-endian `u16`. Equals `2 (type) + 8 (header) + N (body) = N + 10`. |
| Type | 2 | Always `00 01` in observed traffic. |
| Header | 8 | Unencrypted metadata. See [§ 8-Byte Header](#8-byte-header). |
| Body | N | XOR-encrypted payload. See [§ Encryption](#encryption). |

The length field counts the type byte and everything after it (header +
body), i.e. `N + 10` total bytes on the wire, with the length field
reporting `N + 10 − 4 = N + 6`. In practice: `length_field = body_len + 10`.

> Some implementations and earlier docs describe the length as `body_len + 10`
> while others say `header + body + 2`. Both are correct restatements:
> `8 + N + 2 = N + 10`.

A single UDP datagram contains exactly one framed packet. No
fragmentation, no concatenation.

## 8-Byte Header

The header is **not encrypted**. It carries the encryption key for the
body, the per-channel sequence counter, channel routing, message type,
and the body length. Every header byte (except the sequence) is XOR'd
with the random per-packet key — this makes the wire form look random
even when the underlying values are constant.

```
┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┐
│  b0  │  b1  │  b2  │  b3  │  b4  │  b5  │  b6  │  b7  │
│ seq  │verify│ chan │ const│ type │ key  │len_lo│len_hi│
└──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┘
```

| Byte | Formula | Notes |
|------|---------|-------|
| b0 | `seq` | Per-channel sequence counter. Plain, not XOR'd. Wraps at `0xFF`. |
| b1 | `key ^ seq ^ 0xFC` | Verification byte; lets the server cross-check `seq` and `key`. |
| b2 | `key ^ channel_id` | Channel routing (`0xFC` CTRL, `0xFD` CAT, `0xFE` AUDIO, `0xFA` SCOPE). |
| b3 | `key ^ 0x03` | Constant. |
| b4 | `key ^ msg_type` | Message type — see table below. |
| b5 | `key` | The per-packet XOR encryption key. Random byte chosen by the sender. |
| b6 | `key ^ (body_len & 0xFF)` | Low byte of body length. |
| b7 | `key ^ (0x07 ^ (body_len >> 8))` | High byte of body length, additionally XOR'd with `0x07`. |

The server validates the header for consistency: if b1, b3, b6, or b7
don't match the implied `key` + the rest of the fields, the packet is
silently dropped.

### Sequence Counters

Each channel maintains an independent sequence counter per direction.
The counter starts at 0 (or 1) and wraps at `0xFF`.

- The 5 hardcoded init keepalives in [Phase 1](#phase-1--initial-keepalives-client--ctrl) use sequences `01`–`05`.
- The auth packet uses sequence `06`.
- Post-auth CTRL packets continue from `07`.
- CAT, AUDIO, SCOPE counters each start at 1 after their first packet
  (the port-init packet) and increment from there.

### Message Types

The message type lives in `b4 ^ key`. Bit 7 (`0x80`) signals direction:
client→server messages clear it, server→client responses set it.

| Value | Direction | Meaning |
|-------|-----------|---------|
| `0x01` | C → S | Setup / configuration command |
| `0x03` | C → S | Authentication request |
| `0x05` | C → S | Keepalive |
| `0x06` | C → S | Port initialization ("I'm listening on this port") |
| `0x07` | C → S | Data (CAT command, TX audio) |
| `0x81` | S → C | Setup acknowledgment |
| `0x83` | S → C | Authentication response |
| `0x85` | S → C | Keepalive response |
| `0x86` | S → C | Port-initialization ack |
| `0x87` | S → C | Data (CAT response, RX audio, scope) |

## Encryption

The body is encrypted with a symmetric XOR scheme:

```
encrypted[i] = plaintext[i] ^ key ^ gTable[i]
plaintext[i] = encrypted[i] ^ key ^ gTable[i]
```

- `key` is `b5` of the header (a fresh random byte per packet).
- `gTable[i]` is a position-dependent scramble table.

### The gTable

The scramble table is fully derived from a closed-form formula:

```
offsets = [0, 0xFF, 0xFE, 5]            # equivalently [0, -1, -2, +5] mod 256
gTable[i] = (4 * (i / 4) + 6 + offsets[i % 4]) & 0xFF
```

The first 16 values are:

```
06 05 04 0B  0A 09 08 0F  0E 0D 0C 13  12 11 10 17
```

For verification, here are the first 256 values:

```
06 05 04 0B 0A 09 08 0F 0E 0D 0C 13 12 11 10 17
16 15 14 1B 1A 19 18 1F 1E 1D 1C 23 22 21 20 27
26 25 24 2B 2A 29 28 2F 2E 2D 2C 33 32 31 30 37
36 35 34 3B 3A 39 38 3F 3E 3D 3C 43 42 41 40 47
46 45 44 4B 4A 49 48 4F 4E 4D 4C 53 52 51 50 57
56 55 54 5B 5A 59 58 5F 5E 5D 5C 63 62 61 60 67
66 65 64 6B 6A 69 68 6F 6E 6D 6C 73 72 71 70 77
76 75 74 7B 7A 79 78 7F 7E 7D 7C 83 82 81 80 87
86 85 84 8B 8A 89 88 8F 8E 8D 8C 93 92 91 90 97
96 95 94 9B 9A 99 98 9F 9E 9D 9C A3 A2 A1 A0 A7
A6 A5 A4 AB AA A9 A8 AF AE AD AC B3 B2 B1 B0 B7
B6 B5 B4 BB BA B9 B8 BF BE BD BC C3 C2 C1 C0 C7
C6 C5 C4 CB CA C9 C8 CF CE CD CC D3 D2 D1 D0 D7
D6 D5 D4 DB DA D9 D8 DF DE DD DC E3 E2 E1 E0 E7
E6 E5 E4 EB EA E9 E8 EF EE ED EC F3 F2 F1 F0 F7
F6 F5 F4 FB FA F9 F8 FF FE FD FC 03 02 01 00 07
```

You only need to precompute as many entries as your largest body —
4096 is enough for the scope channel; 8192 covers anything practical
with margin.

### What the Encryption Is For

It is **not** cryptographic security. The XOR scheme provides:

- **Header consistency checks** via b1/b3/b6/b7 — a malformed or
  modified packet is silently dropped.
- **Wire obfuscation** — every byte on the wire looks pseudo-random
  even when payloads are constant, which deters trivial inspection.

There is no message authentication, no replay protection, and no key
exchange. Anyone on the network path can decrypt and forge packets. **Do
not** treat SCU-LAN10 as a secure tunnel; tunnel it over a VPN if you're
exposing it to the internet.

## Session Lifecycle

The full session opens in seven phases, in strict order. All phases use
the CTRL port (50000) unless otherwise noted.

```
Client                                          SCU-LAN10
  │                                                │
  │── (1) 5 hardcoded init keepalives ───────────>│  CTRL
  │── (2) Auth packet ───────────────────────────>│  CTRL
  │<── (3) Auth response (session_id + caps) ─────│  CTRL
  │── (4) Continuation ──────────────────────────>│  CTRL
  │<── ack ───────────────────────────────────────│  CTRL
  │                                                │
  │── (5) CAT port-init ─────────────────────────>│  CAT  (50001)
  │── (5) CAT setup ─────────────────────────────>│  CTRL
  │── (5) Audio port-init ───────────────────────>│  AUDIO (50002)
  │── (5) Audio setup ───────────────────────────>│  CTRL
  │── (5) Scope port-init ───────────────────────>│  SCOPE (50003)
  │                                                │
  │── (6) Initial keepalives on all 4 ports ─────>│
  │── (7) First CAT command (e.g. "FA;") ────────>│  CAT (sent twice)
  │                                                │
  │<══ Scope data (≈25 pps) ═══════════════════════│  SCOPE
  │<══ Audio data (≈100 pps) ══════════════════════│  AUDIO
  │── Keepalives (1 Hz on all 4 ports) ──────────>│
```

### Phase 1 — Initial Keepalives (Client → CTRL)

Send exactly **five hardcoded packets** to the CTRL port, in order.
These have fixed keys and sequences (`01`–`05`), and their decrypted
payload is `[0x04, 0x04, 0x04, 0x04]`. They are identical across every
session and every observed FT-Control version — copy them byte-for-byte:

```
01:  5a5a0e00000101a0a15e585d595a5b585956
02:  5a5a0e000001024042bdbbbebab9b8bbbab5
03:  5a5a0e00000103e0e31c1a1f1b18191a1b14
04:  5a5a0e0000010480847b7d787c7f7e7d7c73
05:  5a5a0e000001052025dadcd9dddedfdcddd2
```

The server **does not respond** to these. They appear to wake / prime
the connection before authentication.

### Phase 2 — Authentication (Client → CTRL)

Single packet with `msg_type = 0x03` (auth), `channel = CTRL`,
`seq = 0x06`, and a fixed key of `0x3A`.

Plaintext body (68 bytes):

```
┌────────┬───────────────────────────────────┬───────────────────────────────────┐
│ 01 00  │  Username (33 bytes, null-padded) │  Password (33 bytes, null-padded) │
└────────┴───────────────────────────────────┴───────────────────────────────────┘
  2 bytes              33 bytes                              33 bytes
                                                              = 68 total
```

- Username and password are ASCII, max 32 chars each, null-padded to 33
  bytes.
- Factory defaults shipped with the SCU-LAN10: user `defaultuser`,
  password `defaultuser`.

### Phase 3 — Auth Response (Server → CTRL)

The server responds on CTRL with a packet whose decrypted body is 14
bytes:

```
┌──────────┬──────────────┬──────────────────────┐
│ session  │ 00 (×8)      │ 01 25 01 3F 51       │
│   ID     │ padding      │ capability bytes     │
└──────────┴──────────────┴──────────────────────┘
  1 byte      8 bytes              5 bytes
```

- **Byte 0 — session ID.** Unique per connection. Increments across
  reconnections (observed values: `0x22`, `0x24`, `0x26`, `0x27`,
  `0x28`, `0x29`). The session ID must be echoed in the first 4 bytes
  of nearly every subsequent payload (`[sid, 0, 0, 0, …]`).
- **Bytes 9–13 — capability bytes.** Observed constant `01 25 01 3F 51`
  across every captured session and across firmware versions. Likely a
  firmware / model / capability fingerprint; exact semantics
  unconfirmed.

If authentication fails (wrong credentials), the server simply does not
respond — there is no explicit error packet. Clients should time out
after a few seconds.

### Phase 4 — Continuation (Client → CTRL)

Tells the server the client wants to proceed to channel setup.

```
msg_type   = 0x01 (setup)
channel    = CAT (0xFD)      ← note: CAT, not CTRL
payload    = [sid, 00, 00, 00, 0x51, 0xC3, 0x00, 0x96, 00, 00]
            (10 bytes)
```

- `sid` is the session ID from Phase 3.
- `0x51` ('Q') appears to be a query / control-channel setup identifier.
- `0xC3` is a "command marker" — it appears in every setup command.
- `0x96` (150) is a configuration value; meaning unconfirmed.

The server responds with a 4-byte `00 00 00 00` ack (`msg_type=0x81`).

### Phase 5 — Channel Setup

Each data channel (CAT, AUDIO, SCOPE) is initialized with up to two
packets: a **port-init** to the data port itself, and a **setup**
packet to the CTRL port that registers the channel with the session.

| Step | Destination | b2 channel field | Payload | msg_type |
|------|-------------|------------------|---------|----------|
| CAT init   | port +1 (CAT)   | `0xFD` (CAT)   | `[sid, 00, 00, 00]`             | `0x06` |
| CAT setup  | port +0 (CTRL)  | `0xFE` (AUDIO) | `[sid, 00, 00, 00, 0x52, 0xC3]` | `0x01` |
| Audio init | port +2 (AUDIO) | `0xFE` (AUDIO) | `[sid, 00, 00, 00]`             | `0x06` |
| Audio setup| port +0 (CTRL)  | `0xFA` (SCOPE) | `[sid, 00, 00, 00, 0x53, 0xC3]` | `0x01` |
| Scope init | port +3 (SCOPE) | `0xFA` (SCOPE) | `[sid, 00, 00, 00]`             | `0x06` |

Notes:

- The **b2 channel field in each setup packet refers to the *next*
  channel being configured**, not the one being set up. This is a quirk
  of the wire protocol and is consistent across captures.
- Scope has no explicit CTRL setup packet — only the port-init.
- Port identifiers in setup payloads: `0x52` ('R') = CAT, `0x53` ('S') =
  AUDIO.
- Each init/setup pair is ack'd by the server with a 4-byte
  `00 00 00 00` (init: `msg_type=0x86`; setup: `msg_type=0x81`).
- Allow ~150–200 ms between successive setup steps; the server can
  silently miss them if hammered.

### Phase 6 — Initial Keepalives

Once all three data channels are set up, send one keepalive on each of
the four channels (`msg_type=0x05`, payload `[00, 00, 00, 00]`).

### Phase 7 — First CAT Command

The capture suggests the client immediately issues `FA;` (read VFO-A
frequency) as soon as the channels are up. This is not required, but
it's a good liveness check. CAT commands are sent **twice** (see
[CAT Channel](#cat-channel-port-1)).

After Phase 7, the session is in steady state: scope and audio stream
from the radio, CAT round-trips on demand, keepalives go out every
second.

## Steady-State Operation

In steady state the client must:

1. Send a keepalive on each of the 4 channels every ≈1 s.
2. Send CAT commands as needed (each twice).
3. Continuously receive audio (≈100 pps) and scope (≈25 pps) packets
   on their respective ports.

If keepalives stop, the SCU-LAN10 terminates the session after a few
seconds. Resuming the session requires the full handshake — there is no
resumption mechanism.

## Keepalives

```
msg_type = 0x05
payload  = [0x00, 0x00, 0x00, 0x00]
cadence  = 1 / sec on each of the 4 channels
```

In a typical client implementation the four keepalives are sent in a
burst within ~5 ms once per second.

The server responds to CTRL keepalives with a small ack (`msg_type=0x85`,
1 byte `00` of payload). It does not always ack data-channel
keepalives; absence of a response is not an error.

## CAT Channel (Port +1)

### Client → Server

```
┌─────────┬──────────┬──────────┬──────────┬──────────────────┐
│ session │ 00 00 00 │ cmd_seq  │ 00 00 00 │ CAT ASCII bytes  │
│   ID    │  pad     │ counter  │   pad    │ e.g. "FA;"       │
└─────────┴──────────┴──────────┴──────────┴──────────────────┘
  1 byte    3 bytes    1 byte    3 bytes      variable
```

- `cmd_seq` is a per-CAT-command counter (`0x01`, `0x02`, …) that the
  radio uses to dedupe duplicate sends.
- The ASCII bytes are a standard Yaesu CAT command (semicolon-terminated
  for most commands; see Yaesu's CAT Operation Reference manual).
- **Every CAT command is sent twice** on the wire for UDP reliability.
  Both packets carry the same `cmd_seq` (so the radio dedupes them) but
  use independently-incrementing wire-level header sequence numbers.

### Server → Client

```
┌──────────┬──────────┬──────────────────┐
│ resp_seq │ 00 00 00 │ CAT ASCII bytes  │
│ counter  │   pad    │   response       │
└──────────┴──────────┴──────────────────┘
  1 byte     3 bytes      variable
```

The server also emits **unsolicited** CAT messages — typically meter
readings (`RM1xxx;` through `RM6xxx;`) — even when the client hasn't
asked for them. Clients should treat any inbound CAT frame as if it
were a regular response and parse it the same way.

### Example

Reading VFO-A frequency:

```
client: FA;
server: FA007007000;        ← 7.007.000 Hz
```

### CAT Command Set

The CAT command set is **not** part of this protocol — it is the
standard Yaesu CAT protocol for whichever radio is connected. See the
appropriate "CAT Operation Reference" PDF on Yaesu's site:

| Radio | Document |
|-------|----------|
| FTDX10  | FTDX10 CAT Operation Reference Manual |
| FTDX101 | FTDX101MP/D CAT Operation Reference Manual |
| FT-710  | FT-710 CAT Operation Reference Manual |

Useful commands to know:

| Cmd | Meaning |
|-----|---------|
| `FA` | VFO-A frequency (9 digits, Hz) |
| `FB` | VFO-B frequency |
| `IF` | Composite status (frequency, mode, RIT, etc.) |
| `MD0` | Operating mode (MAIN VFO) |
| `SM0` | S-meter reading (0–255) |
| `SS`  | Spectrum scope settings (span, speed, color, mode) |
| `ID`  | Radio identification (`0670`=FTDX10, `0681`=FTDX101D, `0682`=FTDX101MP) |

## Audio Channel (Port +2)

### Server → Client (RX audio)

Each RX audio packet has a 664-byte body. After decryption, the body
contains a 24-byte **inner header** followed by 640 bytes of
**interleaved stereo Int16 LE PCM** at 16 kHz (160 frames = 10 ms per
packet, ≈100 packets/sec on the wire).

| Byte(s) | LE value | Meaning |
|---------|----------|---------|
| 0       | —        | `frame_seq`, wraps at `0xFF` |
| 1–3     | `00 01 02` | Codec / format ID; byte 2 likely "PCM Int16 LE", byte 3 = channels |
| 4–7     | `0x3E80` | Sample rate (16000), padded to u32 |
| 8–15    | zero     | Reserved |
| 16–17   | `0x07D0` | 2000 (buffer-related parameter) |
| 18–19   | `0x03E8` | 1000 (buffer-related parameter) |
| 20–21   | `0x6464` | Levels (100, 100) |
| 22–23   | `0x0280` | Payload byte count (640) |
| 24–663  | —        | 160 stereo frames of `[Ch0_lo Ch0_hi Ch1_lo Ch1_hi]` Int16 LE |

Observations:

- **Channel 0** carries the speaker audio (the same audio you'd hear
  through the radio's headphones).
- **Channel 1** is near-silent in normal RX; appears to carry the TX
  monitor / sub-receiver when active.
- The sample rate (`16000`) and frame size (`640`) match the
  **PCM(16kHz)** option in FT-Control's "Connection Setup". The protocol
  is presumed to support other formats (PCM 8 kHz, µ-Law 8/16 kHz) but
  this is **unconfirmed** — captures so far are PCM 16 kHz only. See
  [Open Questions](#open-questions).
- Default FT-Control buffer sizes: 200 ms send / 100 ms receive.

### Client → Server (TX audio)

TX audio packets are 668-byte bodies and are only sent while the radio
is transmitting. The exact inner-header layout has not been fully
reverse-engineered — likely mirrors the RX format with minor changes
(see [Open Questions](#open-questions)).

## Scope Channel (Port +3)

### Packet Format

Each scope packet has a **4096-byte body** (4110 bytes on the wire
including framing). One body = one complete spectrum sweep line.

Rate: **≈25 packets/sec** (≈40 ms interval).

### Amplitude Encoding

Each byte represents the amplitude of one frequency bin. The encoding
is **inverted**: lower values mean stronger signals.

| Raw value | Meaning |
|-----------|---------|
| `0` | Strongest measurable signal |
| `1` | Reserved (used as a boundary marker — see below) |
| 2–127 | Strong-to-medium signal |
| ≈200–240 | Typical noise floor |
| `255` | Weakest measurable signal |

For display, normalize as:

```
magnitude = 1.0 - (raw / 255.0)
```

### Active Bin Range

Only the first `N` bins of the 4096 carry actual spectrum data for the
configured scope span. The remainder is metadata, reference blocks, or
zero padding.

The **end of the active spectrum** is marked by a single bin with
value `≤ 1` ("boundary marker"), located somewhere between bins 500 and
3000 depending on the scope span. Search for the first such bin starting
at bin 500 (to avoid false positives from very strong signals at low
bin indices).

Pseudocode for finding the active range:

```python
def find_spectrum_end(packet):
    # Primary: first bin <= 1 after bin 500
    for i in range(500, min(len(packet), 3000)):
        if packet[i] <= 1:
            return i
    # Fallback: first 64-bin block with avg > 225 (reference block)
    block = 64
    for start in range(500, min(len(packet) - block, 3000), block):
        avg = sum(packet[start:start+block]) / block
        if avg > 225:
            return max(100, start - 100)
    return min(len(packet), 2000)
```

The marker typically lands somewhere around **byte 1700–1900** for typical scope spans (200 kHz–500 kHz). Its position is stable for a given scope span and does not move with VFO frequency.

### Bin Doubling

Empirically, the bytes from `0` up to the boundary marker are **packed at two bytes per visible bin** — only the first half of that range produces a clean spectrum line. The reference client we built and verified against a live FTDX10 does:

```
usable_bin_count = boundary_marker_position // 2
```

…and then reads `usable_bin_count` bytes starting at offset 0. So a marker at byte 1900 yields ~950 usable spectrum bins. The exact reason for the doubling (interleaved channels? oversampling artifact? padding for a higher-resolution radio?) is **not confirmed** — see [Open Questions](#open-questions). It is the empirical rule that produces a correct-looking waterfall.

### Beyond the Boundary (observation only)

Bytes past the boundary marker are **not currently decoded** by the verified reference client; everything below describes structure we observed in raw captures but never round-tripped through a working renderer. Treat it as a starting point for further reverse engineering rather than as part of the spec proper.

Captured data shows two distinct regions in the rest of the 4096-byte packet:

- A **reference / calibration block** (avg ≈ 233, low standard deviation) immediately after the marker.
- A **second encoding region** further along, where the data is centered on `128` instead of around the noise floor. This *may* be a secondary signal source (digital sub-scope? RX2? main-scope alternate representation?), and *may* be encoded as deviation from the center:

  ```
  secondary_magnitude = |raw - 128| / 128.0
  ```

  Comparing our raw captures against FT-Control's own pcap data shows both produce the same `~128` floor in this region, which makes corruption an unlikely explanation. But we have not built a decoder that demonstrably renders this region correctly, so the encoding hypothesis is unverified.

If you only need the main spectrum scope, stop at `boundary_marker // 2` and ignore everything else — that's what the reference client does.

### Bin-to-Frequency Mapping

Bins map linearly to frequency across the configured scope span. The
client must read the radio's current span/center via CAT (`SS` command,
or infer from `FA;` and the configured span setting) to map bin → Hz.

### Waterfall Rendering

A typical waterfall implementation:

1. Decrypt each scope packet → 4096 amplitude bytes.
2. Find the boundary marker with the heuristic above.
3. Compute `usable_bin_count = marker_position // 2` (see [Bin Doubling](#bin-doubling)).
4. Take bytes `0..usable_bin_count` from the packet.
5. Normalize each: `magnitude = 1.0 - raw/255.0`.
6. Map magnitude → color (inverted ramp: high magnitude → bright).
7. Scroll a 2D buffer one row per packet (≈25 rows/sec).

## Reconnection

Sessions are **exclusive** — only one client can be connected at a
time. The server appears to *allow* a new auth while a previous session
is still streaming (the auth response includes a new, higher session
ID), but the old session's packets stop being processed shortly
afterward.

Reconnecting requires the **full 7-phase handshake**. There is no
session resumption, no fast-reconnect, no token persistence. The
session ID is server-assigned and increments monotonically with each
new connection.

If the client misses keepalives for more than a few seconds, the server
terminates the session and a fresh handshake is required.

## NAT / Firewall

- All four UDP ports (default 50000–50003) must be reachable from the
  client to the SCU-LAN10. If the device sits behind NAT, forward all
  four ports.
- The SCU-LAN10 sends responses back to the **client's source IP and
  port** that it last received a packet from, so outbound NAT
  traversal works naturally — no STUN-like dance required.
- Some routers/firewalls have aggressive UDP timeouts (30 s) that can
  kill the session even with 1 Hz keepalives. Set UDP keep-state
  timeouts to at least 60 s if you have control of the router.
- **IPv6 is not supported** in any observed capture. Use IPv4.
- The protocol does not authenticate the network path — anyone able to
  reach the device can attempt to log in with the configured
  credentials. Don't expose port 50000–50003 directly to the public
  internet; tunnel over a VPN or SSH.

## Reference Code

### Python — Build the gTable

```python
def build_gtable(size: int = 8192) -> bytes:
    offsets = (0, 0xFF, 0xFE, 5)
    return bytes(((4 * (i // 4) + 6 + offsets[i % 4]) & 0xFF) for i in range(size))
```

### Python — Decrypt a Packet Body

```python
GTABLE = build_gtable()

def decrypt_body(packet: bytes) -> bytes:
    """packet = the bytes after the 5A 5A length type prefix (header + body)."""
    if len(packet) < 9:
        return b""
    key = packet[5]
    body = packet[8:]
    return bytes(b ^ key ^ GTABLE[i] for i, b in enumerate(body))
```

### Python — Build a Header

```python
import random

def build_header(seq: int, channel_id: int, msg_type: int, body_len: int,
                 key: int | None = None) -> bytes:
    if key is None:
        key = random.randint(0, 255)
    return bytes([
        seq,
        key ^ seq ^ 0xFC,
        key ^ channel_id,
        key ^ 0x03,
        key ^ msg_type,
        key,
        key ^ (body_len & 0xFF),
        key ^ (0x07 ^ ((body_len >> 8) & 0xFF)),
    ])

def frame_packet(header: bytes, plaintext: bytes) -> bytes:
    """Wrap header + encrypted body in the 5A 5A length type envelope."""
    key = header[5]
    encrypted = bytes(p ^ key ^ GTABLE[i] for i, p in enumerate(plaintext))
    payload = header + encrypted
    length = len(payload) + 2          # +2 = type field
    return b"\x5A\x5A" + length.to_bytes(2, "little") + b"\x00\x01" + payload
```

### Python — Build an Auth Packet

```python
CTRL, CAT, AUDIO, SCOPE = 0xFC, 0xFD, 0xFE, 0xFA
SETUP, AUTH, KEEPALIVE, PORT_INIT, DATA = 0x01, 0x03, 0x05, 0x06, 0x07

def build_auth(username: str, password: str) -> bytes:
    u = username.encode("ascii")[:32].ljust(33, b"\x00")
    p = password.encode("ascii")[:32].ljust(33, b"\x00")
    plaintext = b"\x01\x00" + u + p
    header = build_header(seq=0x06, channel_id=CTRL, msg_type=AUTH,
                          body_len=len(plaintext), key=0x3A)
    return frame_packet(header, plaintext)
```

### Python — Minimal Connection Skeleton

```python
import socket, time

INIT_PACKETS_HEX = [
    "5a5a0e00000101a0a15e585d595a5b585956",
    "5a5a0e000001024042bdbbbebab9b8bbbab5",
    "5a5a0e00000103e0e31c1a1f1b18191a1b14",
    "5a5a0e0000010480847b7d787c7f7e7d7c73",
    "5a5a0e000001052025dadcd9dddedfdcddd2",
]
INIT_PACKETS = [bytes.fromhex(h) for h in INIT_PACKETS_HEX]

def connect(host: str, base_port: int, user: str, password: str):
    ctrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ctrl.connect((host, base_port))

    # Phase 1: 5 hardcoded keepalives
    for pkt in INIT_PACKETS:
        ctrl.send(pkt)

    # Phase 2: auth
    ctrl.send(build_auth(user, password))

    # Phase 3: auth response
    ctrl.settimeout(5.0)
    resp = ctrl.recv(4096)
    body = decrypt_body(resp[6:])      # skip 5A 5A + length(2) + type(2)
    session_id = body[0]
    print(f"session 0x{session_id:02X}")

    # Phase 4+ ... see SPEC.md § Session Lifecycle
    return ctrl, session_id
```

This snippet covers Phases 1–3 only. The remaining phases follow the
tables in [§ Session Lifecycle](#session-lifecycle) — pick a sequence
counter, channel ID, and message type for each packet and build it with
`build_header` + `frame_packet`.

## Open Questions

Help wanted on any of the following:

- **Multi-radio confirmation.** All current captures are FTDX10.
  Captures from FTDX101MP/D or FT-710 would confirm the protocol is
  truly model-independent.
- **Audio quality negotiation.** FT-Control offers PCM 8 kHz, µ-Law
  8 kHz, µ-Law 16 kHz options in addition to the PCM 16 kHz default.
  How are these requested? What does the wire form look like? (Likely
  a CAT-like setting or a parameter in one of the setup packets.)
- **TX audio inner header.** RX audio inner header is fully decoded;
  TX (668 bytes) has not been characterized in detail.
- **Capability bytes** (`01 25 01 3F 51`). Constant across every
  observed capture, but their meaning — firmware? feature flags? radio
  model? — is unknown.
- **The `0x96` / `0x51 0xC3` constants** in the Phase 4 continuation
  packet. Function unclear; possibly buffer/timeout configuration.
- **Error responses.** Bad auth is silent. Are there explicit error
  packets for other failure modes (rate-limit, port conflict, firmware
  mismatch)?
- **IPv6 support.** Hypothesized to be absent. Confirmation either way
  welcome.
- **Concurrent client behavior.** What exactly happens when a second
  client authenticates while a first is streaming? Captures of that
  state transition would be useful.
- **Scope bin doubling.** Empirically, only the first `marker // 2` bytes produce a usable spectrum line. Why? Interleaved sources? Oversampling? Sub-pixel detail intended for a higher-resolution radio? We don't know — the halving was found by trial and error, not derived.
- **The "secondary" scope region (bytes past the marker).** Captures show structured data past the marker — a high-flat reference block, then a region centered on `128`. We have not built a decoder for it. If you can render that region into something sensible (sub-scope? RX2? main-scope alternate representation?), open a PR.

---

*Method: Wireshark packet captures of FT-Control ↔ SCU-LAN10 traffic
(six independent sessions, ~76,000 packets) analyzed with the help of
Claude (Anthropic's LLM), then validated against an independently
written Swift client connected to a real FTDX10. All formulas in this
document have been verified end-to-end on live hardware. See
[README.md § How this was done](README.md#how-this-was-done) for the
full methodology.*
