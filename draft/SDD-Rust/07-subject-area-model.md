# 7. Subject Area Model

## 7.1 Subject Areas

| Subject Area | Rust Modules | Key Data |
|--------------|--------------|----------|
| Configuration | `config`, `app` | `RuntimeConfig`, `AppRuntime` |
| Radio Backend | `backends`, `service::radio` | `RadioBackend`, `RadioCapabilities`, `RadioCommandPlan` |
| Serial Transport | `transport`, future device actors | `SerialTransport`, priority write metadata |
| State | `state`, `web` | `RadioState`, `StateValue`, dirty JSON object |
| Web Protocol | `protocol::ws`, `web` | client/server JSON messages, audio tags, endpoint constants |
| Audio | `audio` | `AudioProfile`, resampling, `TxJitterBuffer` |
| Spectrum | `scope`, `protocol::civ` | `ScopeFrame`, `ScopeSegment`, payload encoder |
| Memory | `memory` | `MemoryStore`, slot validation, atomic JSON save |
| Session/Auth | `service::session` | `AuthTokenStore`, `SessionRegistry`, TX owner |
| Polling | `scheduler` | `PollIntervals`, `PollTaskPlan`, `StaleReadGuard` |

## 7.2 State Model

`RadioState` remains a dirty-field model. Mutations must go through `update`, which records changed fields and enables compact `stateUpdate` messages. Derived fields will be added as typed accessors in later increments.

## 7.3 Backend Capability Model

Capabilities are immutable data sent during `fullState` and used by the frontend to avoid hardcoded per-radio UI assumptions. The Rust model keeps audio rates, scope type, filter model, ATT/PRE steps, and tune behavior in `RadioCapabilities`.
