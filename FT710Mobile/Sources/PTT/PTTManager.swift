import Foundation
import Combine

/// PTT state machine mirroring the web client's ptt_manager.js semantics.
/// Release is safety-critical: unconditional TX0 + watchdog verify.
/// See docs/superpowers/specs/2026-07-20-ios-ptt-safety-design.md.
@MainActor
final class PTTManager: ObservableObject {

    enum Phase: Equatable {
        case idle, keying, keyed, releasing
    }

    @Published private(set) var phase: Phase = .idle
    var isTX: Bool { phase == .keying || phase == .keyed }

    var sendPTT: (Bool) -> Void
    var sendTXAudioStop: () -> Void
    var setTXAudioActive: (Bool) -> Void
    var serverTXStatus: () -> Int
    var isCtrlConnected: () -> Bool
    var onStuckTX: () -> Void

    var watchdogInterval: TimeInterval = 0.5
    var maxRetries = 3

    init(sendPTT: @escaping (Bool) -> Void,
         sendTXAudioStop: @escaping () -> Void,
         setTXAudioActive: @escaping (Bool) -> Void,
         serverTXStatus: @escaping () -> Int,
         isCtrlConnected: @escaping () -> Bool,
         onStuckTX: @escaping () -> Void) {
        self.sendPTT = sendPTT
        self.sendTXAudioStop = sendTXAudioStop
        self.setTXAudioActive = setTXAudioActive
        self.serverTXStatus = serverTXStatus
        self.isCtrlConnected = isCtrlConnected
        self.onStuckTX = onStuckTX
    }

    /// Touch down. Optimistic: fire-and-forget, no server-echo wait (NFR-008 <100ms).
    /// Accepts re-press from .releasing (cancels the watchdog — legal re-key).
    func press() {
        guard phase == .idle || phase == .releasing else { return }
        // Refuse when we can't command the radio: an optimistic TX we can't
        // control is the most dangerous state.
        guard isCtrlConnected() else { return }
        cancelWatchdog()
        phase = .keying
        sendPTT(true)
        setTXAudioActive(true)
        phase = .keyed
    }

    /// Touch up. Unconditional release + watchdog (SDD ch15 Layers 2/3/7).
    func release() {
        guard phase != .idle else { return }
        sendRelease()
        startWatchdog()
    }

    /// Idempotent emergency release from ANY state (scenePhase / error paths).
    /// TX0 is fire-and-forget; one per call, harmless when already in RX.
    func forceRelease() {
        sendRelease()
        startWatchdog()
    }

    // MARK: - Private

    private var watchdogTask: Task<Void, Never>?

    private func sendRelease() {
        sendPTT(false)
        sendTXAudioStop()        // 's:' on /WSaudioTX — SDD ch15 Layer 7 (server.py:1796)
        setTXAudioActive(false)
        phase = .releasing
    }

    /// Layer 3: after release, verify the server's tx_status echo returns to
    /// RX (0); re-send TX0 up to maxRetries times, then report stuck TX.
    private func startWatchdog() {
        cancelWatchdog()
        watchdogTask = Task { [weak self] in
            guard let self else { return }
            for _ in 1...self.maxRetries {
                try? await Task.sleep(nanoseconds: UInt64(self.watchdogInterval * 1_000_000_000))
                if Task.isCancelled { return }
                if self.serverTXStatus() == 0 {
                    self.phase = .idle
                    return
                }
                self.sendPTT(false)  // re-send TX0
            }
            if self.serverTXStatus() != 0 {
                self.onStuckTX()
            }
            self.phase = .idle
        }
    }

    private func cancelWatchdog() {
        watchdogTask?.cancel()
        watchdogTask = nil
    }
}
