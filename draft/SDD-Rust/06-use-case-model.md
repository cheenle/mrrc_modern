# 6. Use Case Model

## UC-R001 Start Control Session

The operator opens the existing UI, authenticates, and the Rust server sends `fullState` with backend capabilities. The frontend connects all subchannels without protocol changes.

## UC-R002 Remote Receive Audio

The audio actor captures mono PCM from the selected radio USB audio device, bridges to 48 kHz if required, encodes Opus when available, tags frames, and broadcasts over `/WSaudioRX`.

## UC-R003 Remote Transmit Audio

The PTT client claims TX audio ownership. `/WSaudioTX` frames from that client are decoded, queued through `TxJitterBuffer`, bridged to device rate, and written to radio USB audio output. Non-owner frames are dropped and counted.

## UC-R004 Change Frequency And Mode

The control service receives `set` messages, asks the radio actor to plan and write backend-specific commands, marks affected poll keys stale, updates dirty state, and broadcasts `stateUpdate`.

## UC-R005 View Spectrum

The spectrum service receives FT4222 or CI-V scope frames, encodes v1/v2 binary payloads, and sends real hardware data only when frame counters advance. Fallback scope remains available when real scope is unavailable.

## UC-R006 Manage Memory Channels

The memory service validates six slots, rejects malformed channel objects, writes JSON atomically, and broadcasts `memChannels` to connected control clients.

## UC-R007 Safe PTT Release

The system releases TX through normal UI command, disconnect dead-man, page-close beacon, and watchdog paths. The radio release write is not delayed by blocking verification loops.

## UC-R008 Optional ATR1000 Tune Assist

When enabled, the optional tuner service receives frequency/TX notifications and provides tune assist without coupling tuner data into `RadioState`.
