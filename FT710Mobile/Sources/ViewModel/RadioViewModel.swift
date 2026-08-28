import SwiftUI
import Combine
import AVFoundation

/// Central ViewModel — coordinates networking, audio, spectrum, and UI state for FT-710.
@MainActor
final class RadioViewModel: ObservableObject {
    let state = RadioState()
    let connection: ConnectionManager
    let audioPlayback = AudioPlaybackManager()
    let audioCapture = AudioCaptureManager()
    let spectrumProc = SpectrumProcessor()
    let memChannels = MemoryChannelsManager()
    private var cancellables = Set<AnyCancellable>()
    
    // Error handling state
    @Published var showErrorAlert = false
    @Published var errorTitle = ""
    @Published var errorMessage = ""
    @Published var errorActionTitle = "确定"

    // MARK: - Init

    init(serverHost: String = "radio.vlsc.net:8888",
         password: String? = nil) {
        connection = ConnectionManager(serverHost: serverHost, password: password)
        bindSockets()
    }

    // MARK: - Public API

    private var pingTimer: Timer?
    private let pingInterval: TimeInterval = 2.0

    /// Power on: connect all sockets + start audio session + start heartbeat.
    func powerOn() {
        state.powerOn = true
        connection.connectAll()
        audioPlayback.start()
        audioCapture.prepare()
        startPing()
    }

    /// Reconnect without toggling power state (used from Settings).
    /// Disconnects cleanly, waits briefly for sockets to close,
    /// then re-authenticates and reconnects.
    func reconnect() {
        connection.disconnectAll()
        audioPlayback.stop()
        audioCapture.stop()
        stopPing()

        Task {
            // Brief pause to let TCP sockets fully close
            try? await Task.sleep(nanoseconds: 600_000_000)  // 0.6s

            // Re-login to get fresh token
            let scheme = "https"
            guard let loginURL = URL(string: "\(scheme)://\(connection.serverHost)/api/auth/login") else {
                await MainActor.run {
                    self.connection.connectAll()
                    self.audioPlayback.start()
                    self.audioCapture.prepare()
                    self.startPing()
                }
                return
            }

            var req = URLRequest(url: loginURL)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            let body: [String: String] = ["password": connection.password ?? ""]
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)

            do {
                let (data, response) = try await URLSession.shared.data(for: req)
                var token: String?
                if let httpResp = response as? HTTPURLResponse,
                   httpResp.statusCode == 200,
                   let headerFields = httpResp.allHeaderFields as? [String: String],
                   let url = httpResp.url {
                    let cookies = HTTPCookie.cookies(withResponseHeaderFields: headerFields, for: url)
                    token = cookies.first(where: { $0.name == "ft710_auth" })?.value
                    if token == nil,
                       let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        token = json["token"] as? String
                    }
                }

                await MainActor.run {
                    if let tok = token, !tok.isEmpty {
                        self.connection.updateCredentials(password: tok)
                        self.bindSockets()
                    }
                    self.connection.connectAll()
                    self.audioPlayback.start()
                    self.audioCapture.prepare()
                    self.startPing()
                }
            } catch {
                await MainActor.run {
                    self.state.connectionError = "重连失败: \(error.localizedDescription)"
                    self.connection.connectAll()
                    self.audioPlayback.start()
                    self.audioCapture.prepare()
                    self.startPing()
                }
            }
        }
    }

    /// Async — logs in via API, gets session token, then connects.
    func powerOnAsync() {
        state.powerOn = true
        startPing()

        let scheme = "https"  // Production server uses HTTPS
        guard let loginURL = URL(string: "\(scheme)://\(connection.serverHost)/api/auth/login") else {
            connection.connectAll()
            audioCapture.prepare()
            return
        }

        var req = URLRequest(url: loginURL)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body: [String: String] = ["password": connection.password ?? ""]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)

        URLSession.shared.dataTask(with: req) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self = self else { return }

                // Handle network error
                if let err = error {
                    self.state.connectionError = "连接失败: \(err.localizedDescription)"
                    self.state.powerOn = false
                    return
                }

                // Extract token from Set-Cookie header
                var token: String?
                if let httpResp = response as? HTTPURLResponse,
                   httpResp.statusCode == 200,
                   let headerFields = httpResp.allHeaderFields as? [String: String],
                   let url = httpResp.url {
                    let cookies = HTTPCookie.cookies(withResponseHeaderFields: headerFields, for: url)
                    token = cookies.first(where: { $0.name == "ft710_auth" })?.value
                    // Also try JSON body fallback
                    if token == nil, let data = data,
                       let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        token = json["token"] as? String
                    }
                }

                if let tok = token, !tok.isEmpty {
                    self.connection.updateCredentials(password: tok)
                    self.bindSockets()  // re-bind after socket recreation!
                    self.connection.connectAll()
                    self.audioCapture.prepare()
                } else {
                    self.state.connectionError = "认证失败，请检查密码"
                    self.state.powerOn = false  // Reset power state on auth failure
                }
            }
            Task.detached { [weak self] in
                self?.audioPlayback.start()
            }
        }.resume()
    }

    /// Power off: disconnect + stop audio + stop heartbeat.
    func powerOff() {
        state.powerOn = false
        stopPing()
        connection.disconnectAll()
        audioPlayback.stop()
        audioCapture.shutdown()
    }
    
    /// Setup error observers
    private func setupErrorObservers() {
        NotificationCenter.default.addObserver(
            forName: .audioError,
            object: nil,
            queue: .main
        ) { [weak self] notification in
            if let error = notification.userInfo?["error"] as? String {
                self?.handleAudioError(error)
            }
        }
    }
    
    /// Show error alert
    func showError(title: String, message: String, actionTitle: String = "确定") {
        errorTitle = title
        errorMessage = message
        errorActionTitle = actionTitle
        showErrorAlert = true
    }
    
    /// Handle audio errors
    func handleAudioError(_ error: String) {
        showError(title: "音频错误", message: error, actionTitle: "重试")
    }
    
    /// Handle connection errors
    func handleConnectionError(_ error: String) {
        showError(title: "连接错误", message: error, actionTitle: "重新连接")
    }

    // MARK: - Control helpers

    /// Send a JSON {"type":"set","field":...,"value":...} command.
    private func sendSet(_ field: String, _ value: Any) {
        let msg: [String: Any] = ["type": "set", "field": field, "value": value]
        if let json = try? JSONSerialization.data(withJSONObject: msg),
           let str = String(data: json, encoding: .utf8) {
            connection.sendControl(str)
        }
    }

    // MARK: - Frequency

    /// Set RX frequency in Hz. Optional vfo parameter ("A" or "B").
    func setFrequency(_ hz: Int, vfo: String? = nil) {
        let field = (vfo == "B") ? "vfo_b_freq" : "freq"
        sendSet(field, hz)
    }

    /// Recall a memory channel by index (0-9).
    func recallMemory(_ index: Int) {
        guard let channel = memChannels.channels[index] else { return }
        setFrequency(channel.freq)
        setMode(channel.mode)
    }

    /// Save current frequency to a memory channel.
    func saveMemory(_ index: Int) {
        memChannels.storeFrequency(index, freq: state.activeFreq, mode: state.modeName)
    }

    /// Expose memory channels for UI binding.
    var memoryChannels: [MemoryChannelsManager.MemoryChannel?] {
        memChannels.channels
    }

    /// Step frequency up or down by `step` Hz.
    func stepFrequency(up: Bool, step: Int = 1000) {
        let delta = up ? step : -step
        setFrequency(state.activeFreq + delta)
    }

    /// Jump to a band frequency.
    func setBand(_ freq: Int) {
        setFrequency(freq)
    }

    // MARK: - Mode

    /// Set operating mode by name string: "USB", "LSB", "AM", "FM", "CW-U", etc.
    func setMode(_ modeName: String) {
        sendSet("mode", modeName)
    }

    // MARK: - PTT / Tune

    /// Toggle PTT. Starts mic capture on TX, stops on RX. Mutes RX audio during TX.
    func setPTT(_ tx: Bool) {
        sendSet("ptt", tx)
        // 乐观本地更新（I11）:不等服务端回显。松开 PTT 后按钮立即回到 RX
        // 态,且回显到达前的再次点按也能正确走按下分支;真实 tx_status 由
        // 500ms 轮询纠偏。
        state.txStatus = tx ? 1 : 0
        if tx {
            audioPlayback.isMuted = true
            audioCapture.start()
        } else {
            audioCapture.stop()
            audioPlayback.isMuted = false
        }
    }

    /// Tune (steady carrier for antenna tuning, no mic capture).
    func setTune(_ on: Bool) {
        sendSet("tune", on)
    }

    /// Toggle tuner on/off.
    func toggleTuner() {
        let next = state.tunerStatus == 0 ? 1 : 0
        setTuner(next)
    }

    // MARK: - Gain controls

    /// AF (volume) gain, 0–255.
    func setAFGain(_ v: Int) { sendSet("af_gain", v) }

    /// RF gain, 0–255.
    func setRFGain(_ v: Int) { sendSet("rf_gain", v) }

    /// RF power, 0–100 (percent).
    func setRFPower(_ v: Int) { sendSet("rf_power", v) }

    /// Squelch level, 0–255.
    func setSquelch(_ v: Int) { sendSet("squelch", v) }

    /// Mic gain, 0–100.
    func setMicGain(_ v: Int) { sendSet("mic_gain", v) }

    // MARK: - DSP / Filter

    /// Select filter by index into the current mode's filter width table.
    func setFilter(_ idx: Int) { sendSet("filter", idx) }

    /// Preamp: 0=OFF, 1=AMP1, 2=AMP2.
    func setPreamp(_ v: Int) { sendSet("preamp", v) }

    /// Attenuator: 0=OFF, 1=6dB, 2=12dB, 3=18dB.
    func setAttenuator(_ v: Int) { sendSet("att", v) }

    /// Set IPO (intermodulation protection optimizer) level.
    func setIPO(_ v: Int) { sendSet("ipo", v) }

    /// Noise blanker on/off.
    func setNoiseBlanker(_ on: Bool) { sendSet("nb", on) }

    /// Noise reduction on/off.
    func setNoiseReduction(_ on: Bool) { sendSet("nr", on) }

    /// Auto notch on/off.
    func setAutoNotch(_ on: Bool) { sendSet("an", on) }

    /// Compressor on/off.
    func setCompressor(_ on: Bool) { sendSet("comp", on) }

    /// AGC mode: 0=OFF, 1=FAST, 2=MED, 3=SLOW.
    func setAGC(_ mode: Int) { sendSet("agc", mode) }

    // MARK: - VFO / Split

    /// Tuner: 0=OFF, 1=ON, 2=Tuning.
    func setTuner(_ v: Int) { sendSet("tuner", v) }

    /// Select active VFO: "A" or "B".
    func setVFO(_ vfo: String) { sendSet("vfo", vfo.uppercased()) }

    /// Copy VFO-A frequency to VFO-B.
    func copyVFO() {
        sendSet("vfo_b_freq", state.vfoAFreq)
    }

    /// Split mode on/off.
    func setSplit(_ on: Bool) { sendSet("split", on) }

    /// Toggle recording.
    func toggleRecording() {
        if audioCapture.isRecording {
            audioCapture.isRecording = false
        } else {
            audioCapture.isRecording = true
        }
    }

    // MARK: - Scope

    /// Set scope span by index (maps to scope_spans table).
    func setScopeSpan(_ v: Int) { sendSet("scope_span", v) }

    // MARK: - Private

    private func startPing() {
        stopPing()
        pingTimer = Timer.scheduledTimer(withTimeInterval: pingInterval, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.state.powerOn, self.connection.ctrlConnected else { return }
                self.connection.sendControl(#"{"type":"ping"}"#)
            }
        }
    }

    private func stopPing() {
        pingTimer?.invalidate()
        pingTimer = nil
    }

    private func bindSockets() {
        cancellables.removeAll()

        // Relay nested ObservableObject changes so SwiftUI re-renders ContentView
        state.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }.store(in: &cancellables)

        memChannels.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }.store(in: &cancellables)

        // ── Connection status sync ────────────────────────────
        connection.$ctrlConnected.receive(on: RunLoop.main).sink { [weak self] in
            self?.state.ctrlConnected = $0
        }.store(in: &cancellables)
        connection.$audioRXConnected.receive(on: RunLoop.main).sink { [weak self] in
            self?.state.audioRXConnected = $0
        }.store(in: &cancellables)
        connection.$audioTXConnected.receive(on: RunLoop.main).sink { [weak self] in
            self?.state.audioTXConnected = $0
        }.store(in: &cancellables)
        connection.$spectrumConnected.receive(on: RunLoop.main).sink { [weak self] in
            self?.state.spectrumConnected = $0
        }.store(in: &cancellables)

        // ── Error forwarding ───────────────────────────────────
        connection.ctrl.onError = { [weak self] err in
            Task { @MainActor [weak self] in
                self?.state.connectionError = err.localizedDescription
                self?.handleConnectionError(err.localizedDescription)
            }
        }

        // ── Control text → state (JSON protocol) ──────────────
        connection.ctrl.onText = { [weak self] text in
            Task { @MainActor [weak self] in
                guard let self,
                      let data = text.data(using: .utf8),
                      let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
                else { return }
                let msgType = json["type"] as? String ?? ""
                switch msgType {
                case "fullState":
                    if let fullData = json["data"] as? [String: Any] {
                        self.state.applyFullState(fullData)
                    }
                    if let memChannels = json["memChannels"] as? [[String: Any]] {
                        self.memChannels.loadFromServer(memChannels)
                    }
                case "stateUpdate":
                    if let fields = json["fields"] as? [String: Any] {
                        self.state.applyStateUpdate(fields)
                    }
                case "pong":
                    break
                case "error":
                    self.state.connectionError = json["message"] as? String
                case "memChannels":
                    if let channels = json["channels"] as? [[String: Any]] {
                        self.memChannels.loadFromServer(channels)
                    }
                default:
                    break
                }
            }
        }

        // ── Audio RX binary → playback ────────────────────────
        connection.audioRX.onBinary = { [weak self] data in
            self?.audioPlayback.enqueue(int16Data: data)
        }

        // ── Mic capture → TX WebSocket ────────────────────────
        audioCapture.onFrame = { [weak self] pcmData in
            self?.connection.audioTX.send(binary: pcmData)
        }

        // ── Spectrum → processor ──────────────────────────────
        connection.spectrum.onBinary = { [weak self] data in
            self?.spectrumProc.feed(data: data, onImage: { img in
                self?.state.waterfallImage = img
            }, onFFT: { fft in
                self?.state.fftData = fft
            })
        }
    }
}
