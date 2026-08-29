# 12. Operational Model

## 12.1 Configuration

Rust reads the same environment variables as production. `MRRC_*` names take priority, with legacy aliases retained during migration.

## 12.2 Startup Sequence

1. Load `RuntimeConfig`.
2. Select backend through `BackendKey`.
3. Assemble `AppRuntime`.
4. Initialize auth token store and memory store.
5. Start web runtime.
6. Start radio, poll, audio, and scope actors.
7. Broadcast initial state on client connect.

## 12.3 Shutdown Sequence

1. Stop accepting new WS connections.
2. Release PTT if active.
3. Stop audio TX/RX streams.
4. Stop scope producer.
5. Close serial transport.
6. Clear auth tokens.
7. Flush memory writes if pending.

## 12.4 Logging And Diagnostics

Rust runtime logs must include:

- selected backend and capabilities;
- serial port and baud without hardcoded local assumptions;
- audio device selection and actual rates;
- TX session stats including queue drops and non-owner drops;
- scope producer start/stop/reconnect;
- auth default-password warnings;
- memory validation errors.

## 12.5 Deployment

Initial Rust deployment is draft-only. Production cutover requires parallel shadow runs and rollback to Python scripts.
