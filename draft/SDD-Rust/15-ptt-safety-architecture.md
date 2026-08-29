# 15. PTT Safety Architecture

## 15.1 Safety Goal

The Rust implementation must never make PTT release slower or less reliable than production. PTT release is safety-critical and must remain protected by independent layers.

## 15.2 Layered Release Model

| Layer | Rust Responsibility |
|-------|---------------------|
| Normal UI release | Control WS sends `ptt=false`; radio actor writes release command with priority |
| TX audio text stop | `/WSaudioTX` text `s:` stops TX audio queue |
| Disconnect dead-man | WS close path releases PTT if client owned/held transmit |
| Browser unload/pagehide | Existing client behavior remains supported |
| Watchdog | Server-side max-TX watchdog remains configurable |
| Poll confirmation | TX status poll observes radio state but does not block release command |

## 15.3 TX Audio Ownership

`SessionRegistry` tracks authenticated WS clients and TX-audio clients. When a client keys PTT, the TX-audio socket with the same auth token claims uplink ownership. Non-owner mic frames are ignored and counted.

## 15.4 Priority Command Path

PTT and tune commands are planned with `priority=true`. The actor marks skip-poll windows and user-command pause before writing. Poll results arriving during the stale window are discarded.

## 15.5 Prohibited Regression

The Rust implementation must not add blocking post-release verification loops to the release path. Verification belongs to polling/watchdog layers, not to the release write itself.

## 15.6 Acceptance Tests

- Press/release PTT repeatedly under active polling.
- Disconnect the active control socket while TX is active.
- Close browser tab while TX is active.
- Simulate half-open TX audio owner and replacement socket.
- Trigger max-TX watchdog.
- Confirm RX audio restarts after TX to RX transition on affected Windows devices.
