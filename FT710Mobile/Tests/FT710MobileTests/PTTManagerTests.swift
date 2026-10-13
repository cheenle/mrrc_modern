import XCTest

@MainActor
final class PTTManagerTests: XCTestCase {

    /// Records all injected-callback invocations for assertions.
    @MainActor private final class Harness {
        var sentPTT: [Bool] = []
        var txAudioStopCount = 0
        var captureActive: [Bool] = []
        var stuckTXCount = 0
        var echoStatus = 0          // simulated server tx_status echo
        var connected = true

        func make(interval: TimeInterval = 0.01, retries: Int = 3) -> PTTManager {
            let mgr = PTTManager(
                sendPTT: { [weak self] tx in self?.sentPTT.append(tx) },
                sendTXAudioStop: { [weak self] in self?.txAudioStopCount += 1 },
                setTXAudioActive: { [weak self] on in self?.captureActive.append(on) },
                serverTXStatus: { [weak self] in self?.echoStatus ?? 0 },
                isCtrlConnected: { [weak self] in self?.connected ?? true },
                onStuckTX: { [weak self] in self?.stuckTXCount += 1 }
            )
            mgr.watchdogInterval = interval
            mgr.maxRetries = retries
            return mgr
        }
    }

    func testPressRefusedWhenDisconnected() {
        let h = Harness(); h.connected = false
        let mgr = h.make()
        mgr.press()
        XCTAssertEqual(mgr.phase, .idle)
        XCTAssertTrue(h.sentPTT.isEmpty)
        XCTAssertTrue(h.captureActive.isEmpty)
    }

    func testPressIsOptimistic() {
        let h = Harness()
        let mgr = h.make()
        mgr.press()
        XCTAssertEqual(mgr.phase, .keyed)
        XCTAssertEqual(h.sentPTT, [true])
        XCTAssertEqual(h.captureActive, [true])
        XCTAssertTrue(mgr.isTX)
    }

    /// Regression for analysis §2.1: rapid press→release must ALWAYS send
    /// ptt:false — the old code dropped it when the tx_status echo hadn't arrived.
    func testRapidPressReleaseAlwaysSendsTX0() {
        let h = Harness(); h.echoStatus = 0  // echo hasn't caught up — the bug scenario
        let mgr = h.make()
        mgr.press()
        mgr.release()
        XCTAssertEqual(h.sentPTT, [true, false])
        XCTAssertEqual(h.captureActive, [true, false])
        XCTAssertEqual(h.txAudioStopCount, 1)
    }

    func testReleaseWithRXEchoGoesIdleWithoutRetry() async throws {
        let h = Harness(); h.echoStatus = 0
        let mgr = h.make()
        mgr.press(); mgr.release()
        try await Task.sleep(nanoseconds: 50_000_000)  // > 1 watchdog tick
        XCTAssertEqual(mgr.phase, .idle)
        XCTAssertEqual(h.sentPTT, [true, false])  // no re-send
        XCTAssertEqual(h.stuckTXCount, 0)
    }

    func testWatchdogResendsUntilRXEcho() async throws {
        let h = Harness(); h.echoStatus = 1  // radio still TX
        let mgr = h.make()
        mgr.press(); mgr.release()
        try await Task.sleep(nanoseconds: 25_000_000)  // ~2 ticks
        h.echoStatus = 0                              // radio finally RX
        try await Task.sleep(nanoseconds: 30_000_000)
        XCTAssertEqual(mgr.phase, .idle)
        XCTAssertGreaterThanOrEqual(h.sentPTT.filter({ $0 == false }).count, 2)
        XCTAssertEqual(h.stuckTXCount, 0)
    }

    func testWatchdogGivesUpAfterMaxRetriesAndReports() async throws {
        let h = Harness(); h.echoStatus = 1
        let mgr = h.make()
        mgr.press(); mgr.release()
        try await Task.sleep(nanoseconds: 100_000_000)  // > 3 ticks
        // 1 original release + 3 watchdog re-sends = 4 TX0
        XCTAssertEqual(h.sentPTT.filter({ $0 == false }).count, 4)
        XCTAssertEqual(h.stuckTXCount, 1)
        XCTAssertEqual(mgr.phase, .idle)
    }

    func testPressDuringWatchdogCancelsRetries() async throws {
        let h = Harness(); h.echoStatus = 1
        let mgr = h.make(interval: 0.03)
        mgr.press(); mgr.release()
        try await Task.sleep(nanoseconds: 35_000_000)  // ~1 tick: 1 re-send
        let countBefore = h.sentPTT.count
        h.echoStatus = 0
        mgr.press()  // re-key during watchdog window
        XCTAssertEqual(mgr.phase, .keyed)
        try await Task.sleep(nanoseconds: 100_000_000)  // watchdog must stay cancelled
        XCTAssertEqual(h.sentPTT.count, countBefore + 1)  // only the press's `true`
        XCTAssertEqual(h.sentPTT.last, true)
    }

    func testForceReleaseFromIdleIsIdempotent() async throws {
        let h = Harness(); h.echoStatus = 0
        let mgr = h.make()
        mgr.forceRelease()
        mgr.forceRelease()
        XCTAssertEqual(h.sentPTT, [false, false])  // one TX0 per call, no crash
        XCTAssertEqual(h.txAudioStopCount, 2)
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(mgr.phase, .idle)
        XCTAssertEqual(h.sentPTT.count, 2)  // echo RX → watchdog exits clean
    }
}
