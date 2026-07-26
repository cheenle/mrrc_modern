import AVFoundation
import Accelerate

/// Plays Int16 PCM audio received from /WSaudioRX using AVAudioPlayerNode.
/// Uses vDSP for fast Int16→Float32 conversion and RMS calculation.
final class AudioPlaybackManager: NSObject, ObservableObject, @unchecked Sendable {
    private let engine = AVAudioEngine()
    private let playerNode = AVAudioPlayerNode()
    private let ioQueue = DispatchQueue(label: "audio.playback", qos: .userInitiated)
    private var isAttached = false

    private let sourceSampleRate: Double = 48000  // matches server RX_OUT_RATE
    private var playbackFormat: AVAudioFormat!

    private(set) var isStarted = false

    // ── Opus decoder ──────────────────────────────────────────
    private let opusDecoder = OpusDecoder()

    // ── Diagnostics ───────────────────────────────────────────
    private var frameCount: Int = 0
    private var opusDropCount: Int = 0
    private var lastDiagTime = Date()

    // ── Observable state ──────────────────────────────────────
    @Published var isMuted: Bool = false {
        didSet { applyVolume() }
    }
    /// Local phone volume (0.0–1.0), independent of radio's af_gain.
    @Published var appVolume: Float = 0.5 {
        didSet { applyVolume() }
    }
    @Published var rmsLevel: Float = 0.0
    @Published var audioError: String?

    /// 10× boost matches web frontend AUDIO_GAIN_BOOST to compensate
    /// for quiet FT-710 USB audio.
    private let audioGainBoost: Float = 10.0
    private let audioGainMax: Float = 10.0

    private func applyVolume() {
        if isMuted {
            playerNode.volume = 0
        } else {
            playerNode.volume = min(audioGainMax, appVolume * audioGainBoost)
        }
    }

    // ── Recording ─────────────────────────────────────────────
    @Published var isRecording: Bool = false
    private var recordBuffer: [Float] = []
    private let recordLock = NSLock()

    func startRecording() {
        recordLock.lock(); recordBuffer.removeAll(keepingCapacity: true); recordLock.unlock()
        isRecording = true
    }

    func stopRecording() -> Data? {
        isRecording = false
        recordLock.lock(); let samples = recordBuffer; recordLock.unlock()
        guard !samples.isEmpty else { return nil }
        return makeWAV(samples: samples)
    }

    var recordingDuration: TimeInterval {
        recordLock.lock(); let c = recordBuffer.count; recordLock.unlock()
        return TimeInterval(c) / sourceSampleRate
    }

    // MARK: - Start / Stop

    /// Engine lifecycle is serialized on ioQueue (same queue as buffer
    /// scheduling) so the two entry points that can fire concurrently —
    /// powerOn() and the login-completion Task.detached — cannot race
    /// into a double attach / double connect.
    func start() {
        ioQueue.async { self._start() }
    }

    private func _start() {
        guard !isStarted else { return }
        frameCount = 0; opusDropCount = 0; lastDiagTime = Date()

        playbackFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                        sampleRate: sourceSampleRate,
                                        channels: 1,
                                        interleaved: false)!

        configureSession()
        // stop() keeps the node attached; re-attaching an attached node
        // throws (reconnect / power-off-on path).
        if !isAttached {
            engine.attach(playerNode)
            isAttached = true
        }
        engine.connect(playerNode, to: engine.mainMixerNode, format: playbackFormat)
        applyVolume()

        do {
            try engine.start()
            playerNode.play()
            isStarted = true
            let outFmt = playerNode.outputFormat(forBus: 0)
            print("🔊 Audio playback started @ \(sourceSampleRate) Hz, player output \(outFmt.channelCount)ch @ \(Int(outFmt.sampleRate)) Hz")
        } catch {
            let msg = "Engine start: \(error.localizedDescription)"
            print("⚠️ \(msg)")
            DispatchQueue.main.async {
                self.audioError = msg
                NotificationCenter.default.post(name: .audioError, object: nil, userInfo: ["error": msg])
            }
        }
    }

    func stop() {
        ioQueue.async { self._stop() }
    }

    private func _stop() {
        guard isStarted else { return }
        isStarted = false
        playerNode.stop()
        engine.stop()
        engine.reset()
        print("🔇 Audio playback stopped  frames=\(frameCount) opusDrops=\(opusDropCount)")
    }

    // MARK: - Enqueue PCM data

    /// Frame format: 1-byte codec tag (0x00=PCM, 0x01=Opus) + payload.
    func enqueue(int16Data: Data) {
        guard isStarted, int16Data.count >= 3 else { return }
        let codec = int16Data[int16Data.startIndex]
        if codec == 0x01 {
            if let pcmSamples = opusDecoder.decode(int16Data.dropFirst()) {
                let pcmData = pcmSamples.withUnsafeBufferPointer { Data(buffer: $0) }
                processPCM(pcmData)
            } else {
                opusDropCount += 1
                if opusDropCount == 10 {
                    print("⚠️ Audio: Opus decode failed 10× — check libopus")
                }
            }
            return
        }

        let pcmBytes = int16Data.dropFirst()
        processPCM(pcmBytes)
    }

    /// Process decoded PCM data (Int16 LE).
    private func processPCM(_ pcmBytes: Data) {
        let sampleCount = pcmBytes.count / 2
        guard sampleCount > 0 else { return }

        // ── vDSP-accelerated Int16 LE → Float32 ───────────────
        var samples = [Float](repeating: 0, count: sampleCount)
        pcmBytes.withUnsafeBytes { raw in
            let base = raw.baseAddress!.assumingMemoryBound(to: Int16.self)
            var scale = Float(1.0 / 32768.0)
            vDSP_vflt16(base, 1, &samples, 1, vDSP_Length(sampleCount))
            vDSP_vsmul(samples, 1, &scale, &samples, 1, vDSP_Length(sampleCount))
        }

        // Recording
        if isRecording {
            recordLock.lock()
            recordBuffer.append(contentsOf: samples)
            recordLock.unlock()
        }

        // ── vDSP RMS ──────────────────────────────────────────
        var rms: Float = 0
        vDSP_rmsqv(samples, 1, &rms, vDSP_Length(sampleCount))
        DispatchQueue.main.async { [weak self] in self?.rmsLevel = rms }

        // ── Schedule buffer directly ─────────────────────────
        // Copy samples to a heap buffer so we can move it into ioQueue safely
        let heapSamples = samples  // copies

        ioQueue.async { [weak self] in
            guard let self = self, self.isStarted else { return }
            // Build the buffer in the player's LIVE output format.  iOS can
            // connect/renegotiate the player→mixer bus to the hardware
            // format (e.g. stereo under .voiceChat), and scheduleBuffer
            // raises a fatal NSException on any channel-count mismatch —
            // this was the crash observed right after login
            // (AVAEInternal ScheduleBuffer: channelCount mismatch).
            let outFmt = self.playerNode.outputFormat(forBus: 0)
            let channels = Int(outFmt.channelCount)
            guard outFmt.commonFormat == .pcmFormatFloat32,
                  channels >= 1, outFmt.sampleRate > 0 else { return }
            if outFmt.channelCount != self.lastLoggedOutChannels {
                self.lastLoggedOutChannels = outFmt.channelCount
                print("ℹ️ Player output format: \(outFmt.channelCount)ch @ \(Int(outFmt.sampleRate)) Hz interleaved=\(outFmt.isInterleaved)")
            }

            // Resample if the bus rate differs from the 48 kHz source.
            let outSamples: [Float]
            if abs(outFmt.sampleRate - self.sourceSampleRate) > 1.0 {
                outSamples = self.resample(heapSamples, from: self.sourceSampleRate, to: outFmt.sampleRate)
            } else {
                outSamples = heapSamples
            }
            let outFrames = AVAudioFrameCount(outSamples.count)

            guard let buf = AVAudioPCMBuffer(pcmFormat: outFmt, frameCapacity: outFrames) else { return }
            buf.frameLength = outFrames
            if outFmt.isInterleaved {
                if let base = buf.floatChannelData?.pointee {
                    for i in 0..<outSamples.count {
                        let s = outSamples[i]
                        for ch in 0..<channels { base[i * channels + ch] = s }
                    }
                }
            } else {
                // Mono bus → single copy; multi-channel bus → duplicate mono.
                if let channelData = buf.floatChannelData {
                    for ch in 0..<channels {
                        channelData[ch].initialize(from: outSamples, count: outSamples.count)
                    }
                }
            }
            self.playerNode.scheduleBuffer(buf)
        }

        // ── 5-second diagnostic ───────────────────────────────
        frameCount += 1
        let now = Date()
        if now.timeIntervalSince(lastDiagTime) >= 5.0 {
            let fps = Double(frameCount) / now.timeIntervalSince(lastDiagTime)
            print("🔊 Audio: \(frameCount) frames @ \(String(format:"%.1f",fps))fps  sampleCount=\(sampleCount)  opusDrops=\(opusDropCount)")
            frameCount = 0; opusDropCount = 0; lastDiagTime = now
        }
    }

    // MARK: - Private

    private var lastLoggedOutChannels: UInt32 = 0

    /// Lightweight linear resampler (same approach as AudioCaptureManager).
    private func resample(_ input: [Float], from inRate: Double, to outRate: Double) -> [Float] {
        guard !input.isEmpty, inRate != outRate else { return input }
        let ratio = outRate / inRate
        let outLen = max(1, Int(Double(input.count) * ratio))
        var out = [Float](repeating: 0, count: outLen)
        let step = 1.0 / ratio
        for i in 0..<outLen {
            let pos = Double(i) * step
            let idx = Int(pos)
            let frac = Float(pos - Double(idx))
            let a = input[min(idx, input.count - 1)]
            let b = input[min(idx + 1, input.count - 1)]
            out[i] = a + (b - a) * frac
        }
        return out
    }

    private func configureSession() {
        do {
            try AudioSessionManager.shared.configureForTransceiver()
        } catch {
            audioError = "Audio session config: \(error.localizedDescription)"
            print("⚠️ \(audioError!)")
        }
    }

    // MARK: - WAV export

    private func makeWAV(samples: [Float]) -> Data {
        let sr = UInt32(sourceSampleRate)
        let ch: UInt16 = 1, bps: UInt16 = 16
        let dataSize = UInt32(samples.count * 2)
        var data = Data()
        data.append("RIFF".data(using: .ascii)!)
        data.append(withUnsafeBytes(of: UInt32(36 + dataSize).littleEndian) { Data($0) })
        data.append("WAVE".data(using: .ascii)!)
        data.append("fmt ".data(using: .ascii)!)
        data.append(withUnsafeBytes(of: UInt32(16).littleEndian) { Data($0) })
        data.append(withUnsafeBytes(of: ch.littleEndian) { Data($0) })
        data.append(withUnsafeBytes(of: bps.littleEndian) { Data($0) })
        data.append(withUnsafeBytes(of: UInt16(1).littleEndian) { Data($0) }) // PCM
        let byteRate = sr * UInt32(ch) * UInt32(bps / 8)
        data.append(withUnsafeBytes(of: byteRate.littleEndian) { Data($0) })
        data.append(withUnsafeBytes(of: UInt16(ch * (bps / 8)).littleEndian) { Data($0) })
        data.append("data".data(using: .ascii)!)
        data.append(withUnsafeBytes(of: dataSize.littleEndian) { Data($0) })
        for s in samples {
            let v = Int16(max(-1, min(1, s)) * 32767).littleEndian
            data.append(withUnsafeBytes(of: v) { Data($0) })
        }
        return data
    }
}

// MARK: - Notification

extension Notification.Name {
    static let audioOpusDetected = Notification.Name("audioOpusDetected")
    static let audioError = Notification.Name("audioError")
}
