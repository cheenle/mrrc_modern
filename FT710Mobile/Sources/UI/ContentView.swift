import SwiftUI

/// Main container — tabs for RX, DSP, and Settings.
struct ContentView: View {
    @EnvironmentObject var viewModel: RadioViewModel
    @State private var selectedTab = 0
    @State private var tuneStep: Int = 1000  // Hz, matches web default
    @State private var tuneHeld = false      // TUNE press-and-hold local state

    var body: some View {
        ZStack {
            Color.radioBg.ignoresSafeArea()
            if !viewModel.state.powerOn { offState }
            else { onState }
            
            // Error alert overlay
            ErrorAlertView(
                showError: $viewModel.showErrorAlert,
                title: viewModel.errorTitle,
                message: viewModel.errorMessage,
                actionTitle: viewModel.errorActionTitle
            )
        }
    }

    private var offState: some View {
        VStack(spacing: 20) {
            Spacer()
            Image(systemName: "antenna.radiowaves.left.and.right")
                .font(.system(size: 56, weight: .thin)).foregroundColor(.radioAccent)
            Text("FT-710").font(.title.weight(.semibold)).foregroundColor(.radioText)
            Text("Remote Control").font(.subheadline).foregroundColor(.radioMuted)
            if let err = viewModel.state.connectionError {
                Text(err).font(.caption).foregroundColor(.radioRed).padding(.horizontal)
            }
            Button(action: { viewModel.powerOnAsync() }) {
                Label("连接电台", systemImage: "power").font(.headline).foregroundColor(.black)
                    .frame(maxWidth: 240).frame(height: 50)
                    .background(Color.radioAccent).clipShape(RoundedRectangle(cornerRadius: 14))
            }
            Spacer()
        }
    }

    private var onState: some View {
        VStack(spacing: 0) {
            // Fixed header (frequency + status), like the web header bar
            HeaderView().padding(.horizontal, 10).padding(.top, 2).background(Color.radioBg)

            // Compact single-screen layout, no scrolling
            ZStack(alignment: .bottom) {
                VStack(spacing: 4) {
                    // ── FFT + Waterfall / Spectrum ──
                    VStack(spacing: 0) {
                        FFTLineView()
                            .frame(height: 66)
                        WaterfallView()
                            .frame(height: 67)
                    }
                    .frame(height: 133)
                    .padding(.horizontal, 6)

                    // ── S-meter ──
                    SMeterView()

                    // ── Multi-meter (always visible) ──
                    MeterBarView()

                    // ── Quick controls row (Mode·Band·Filter·ATT·IPO) ──
                    QuickControlsRow()

                    // ── DSP toggles ──
                    HStack(spacing: 4) {
                        Button(action: { viewModel.setNoiseReduction(!viewModel.state.noiseReduction) }) {
                            Text("NR").font(.system(size: 11, weight: .bold))
                                .foregroundColor(viewModel.state.noiseReduction ? .black : .radioMuted)
                                .frame(maxWidth: .infinity).frame(height: 28)
                                .background(viewModel.state.noiseReduction ? Color.radioAccent : Color.radioSurface)
                                .cornerRadius(4)
                        }
                        Button(action: { viewModel.setNoiseBlanker(!viewModel.state.noiseBlanker) }) {
                            Text("NB").font(.system(size: 11, weight: .bold))
                                .foregroundColor(viewModel.state.noiseBlanker ? .black : .radioMuted)
                                .frame(maxWidth: .infinity).frame(height: 28)
                                .background(viewModel.state.noiseBlanker ? Color.radioAccent : Color.radioSurface)
                                .cornerRadius(4)
                        }
                        Button(action: { viewModel.setAutoNotch(!viewModel.state.autoNotch) }) {
                            Text("AN").font(.system(size: 11, weight: .bold))
                                .foregroundColor(viewModel.state.autoNotch ? .black : .radioMuted)
                                .frame(maxWidth: .infinity).frame(height: 28)
                                .background(viewModel.state.autoNotch ? Color.radioAccent : Color.radioSurface)
                                .cornerRadius(4)
                        }
                        Button(action: { viewModel.setCompressor(!viewModel.state.compressor) }) {
                            Text("COMP").font(.system(size: 11, weight: .bold))
                                .foregroundColor(viewModel.state.compressor ? .black : .radioMuted)
                                .frame(maxWidth: .infinity).frame(height: 28)
                                .background(viewModel.state.compressor ? Color.radioAccent : Color.radioSurface)
                                .cornerRadius(4)
                        }
                        Button(action: { viewModel.setTuner(viewModel.state.tunerStatus == 1 ? 0 : 1) }) {
                            Text("ATU").font(.system(size: 11, weight: .bold))
                                .foregroundColor(viewModel.state.tunerStatus == 1 ? .black : .radioMuted)
                                .frame(maxWidth: .infinity).frame(height: 28)
                                .background(viewModel.state.tunerStatus == 1 ? Color.radioAccent : Color.radioSurface)
                                .cornerRadius(4)
                        }
                    }.padding(.horizontal, 6)

                    // ── Volume slider (slim) ──
                    HStack(spacing: 6) {
                        Image(systemName: viewModel.audioPlayback.isMuted ? "speaker.slash.fill" : "speaker.wave.1.fill")
                            .font(.system(size: 10)).foregroundColor(.radioMuted)
                        Slider(value: Binding(
                            get: { Double(viewModel.audioPlayback.appVolume) },
                            set: { viewModel.audioPlayback.appVolume = Float($0) }
                        ), in: 0...1)
                            .tint(.radioAccent)
                    }.padding(.horizontal, 12)

                    // ── Tuning controls (matches web: ◀◀ ◀ [step] ▶ ▶▶) ──
                    HStack(spacing: 12) {
                        // Fast left (-5× step)
                        Button(action: { viewModel.stepFrequency(up: false, step: tuneStep * 5) }) {
                            Image(systemName: "chevron.left.2")
                                .font(.system(size: 14, weight: .bold)).foregroundColor(.radioAccent)
                                .frame(width: 44, height: 38).background(Color.radioSurface).cornerRadius(4)
                        }
                        // Slow left (-1× step)
                        Button(action: { viewModel.stepFrequency(up: false, step: tuneStep) }) {
                            Image(systemName: "chevron.left")
                                .font(.system(size: 14, weight: .bold)).foregroundColor(.radioAccent)
                                .frame(width: 44, height: 38).background(Color.radioSurface).cornerRadius(4)
                        }
                        // Step selector — cycles through preset step sizes (matches web)
                        Button(action: {
                            let steps = [10, 100, 1000, 5000, 10000, 25000]
                            if let idx = steps.firstIndex(of: tuneStep) {
                                tuneStep = steps[(idx + 1) % steps.count]
                            } else {
                                tuneStep = 1000
                            }
                        }) {
                            Text(stepLabel(tuneStep))
                                .font(.system(size: 17, weight: .bold, design: .monospaced))
                                .foregroundColor(.radioAccent)
                                .frame(width: 74, height: 38).background(Color.radioAccent.opacity(0.2)).cornerRadius(4)
                        }
                        // Slow right (+1× step)
                        Button(action: { viewModel.stepFrequency(up: true, step: tuneStep) }) {
                            Image(systemName: "chevron.right")
                                .font(.system(size: 14, weight: .bold)).foregroundColor(.radioAccent)
                                .frame(width: 44, height: 38).background(Color.radioSurface).cornerRadius(4)
                        }
                        // Fast right (+5× step)
                        Button(action: { viewModel.stepFrequency(up: true, step: tuneStep * 5) }) {
                            Image(systemName: "chevron.right.2")
                                .font(.system(size: 14, weight: .bold)).foregroundColor(.radioAccent)
                                .frame(width: 44, height: 38).background(Color.radioSurface).cornerRadius(4)
                        }
                    }.padding(.horizontal, 6)

                    // ── VFO buttons ──
                    HStack(spacing: 4) {
                        Button(action: { viewModel.setVFO("A") }) {
                            Text("VFO-A").font(.system(size: 11, weight: .bold))
                                .foregroundColor(viewModel.state.activeVFO == "A" ? .black : .radioMuted)
                                .frame(maxWidth: .infinity).frame(height: 28)
                                .background(viewModel.state.activeVFO == "A" ? Color.radioAccent : Color.radioSurface)
                                .cornerRadius(4)
                        }
                        Button(action: { viewModel.setVFO("B") }) {
                            Text("VFO-B").font(.system(size: 11, weight: .bold))
                                .foregroundColor(viewModel.state.activeVFO == "B" ? .black : .radioMuted)
                                .frame(maxWidth: .infinity).frame(height: 28)
                                .background(viewModel.state.activeVFO == "B" ? Color.radioAccent : Color.radioSurface)
                                .cornerRadius(4)
                        }
                        Button(action: { viewModel.setVFO(viewModel.state.activeVFO == "A" ? "B" : "A") }) {
                            Text("A=B").font(.system(size: 11, weight: .bold))
                                .foregroundColor(.radioMuted)
                                .frame(maxWidth: .infinity).frame(height: 28)
                                .background(Color.radioSurface)
                                .cornerRadius(4)
                        }
                        Button(action: { viewModel.setSplit(!viewModel.state.split) }) {
                            Text("SPLIT").font(.system(size: 11, weight: .bold))
                                .foregroundColor(viewModel.state.split ? .black : .radioMuted)
                                .frame(maxWidth: .infinity).frame(height: 28)
                                .background(viewModel.state.split ? Color.radioAccent : Color.radioSurface)
                                .cornerRadius(4)
                        }
                    }.padding(.horizontal, 6)

                    // ── Memory channels grid ──
                    LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], spacing: 4) {
                        ForEach(0..<6, id: \.self) { index in
                            Button(action: { viewModel.recallMemory(index) }) {
                                VStack(spacing: 2) {
                                    Text("M\(index + 1)").font(.system(size: 11, weight: .bold)).foregroundColor(.radioAccent)
                                    if let channel = viewModel.memoryChannels[index] {
                                        Text(channel.freqDisplay).font(.system(size: 10, design: .monospaced)).foregroundColor(Color.radioText)
                                    }
                                }.frame(maxWidth: .infinity).frame(height: 40)
                                    .background(Color.radioSurface).cornerRadius(4)
                            }
                        }
                    }.padding(.horizontal, 6)

                    Spacer(minLength: 0)
                }
                .background(Color.radioBg)

                // ── Fixed PTT footer (2× height for easy operation) ──
                VStack(spacing: 0) {
                    if viewModel.state.txStatus > 0 {
                        Rectangle().fill(Color.red.opacity(0.08)).frame(height: 8)
                    }
                    HStack(spacing: 8) {
                        // PTT: press-and-hold — touch down TX, release RX
                        Text(viewModel.state.txStatus > 0 ? "● TX ●" : "PTT")
                            .font(.system(size: 32, weight: .heavy, design: .rounded))
                            .foregroundColor(.white)
                            .frame(maxWidth: .infinity).frame(height: 96)
                            .background(viewModel.state.txStatus > 0 ? Color.red : Color.red.opacity(0.8))
                            .cornerRadius(8)
                            .gesture(
                                DragGesture(minimumDistance: 0)
                                    .onChanged { _ in
                                        if viewModel.state.txStatus == 0 { viewModel.setPTT(true) }
                                    }
                                    .onEnded { _ in
                                        if viewModel.state.txStatus > 0 { viewModel.setPTT(false) }
                                    }
                            )
                        // TUNE: press-and-hold — touch down starts the tuning
                        // sequence (server: TX2 carrier + AC003 tuning start),
                        // release drops the carrier (AC000 + TX0).
                        // 修复:此前误用 toggleTuner() 只会 AC001 打开天调,不执行调谐。
                        // 按住状态用本地 tuneHeld 跟踪,不依赖轮询回来的 txStatus
                        // (快速点按时 poll 尚未刷新会导致漏发停止、载波常开)。
                        Text(viewModel.state.txStatus == 2 ? "TUNING" : "TUNE")
                            .font(.system(size: 18, weight: .bold)).foregroundColor(.black)
                            .frame(width: 80, height: 96)
                            .background(viewModel.state.txStatus == 2 ? Color.orange : Color.yellow)
                            .cornerRadius(8)
                            .gesture(
                                DragGesture(minimumDistance: 0)
                                    .onChanged { _ in
                                        if !tuneHeld {
                                            tuneHeld = true
                                            viewModel.setTune(true)
                                        }
                                    }
                                    .onEnded { _ in
                                        if tuneHeld {
                                            tuneHeld = false
                                            viewModel.setTune(false)
                                        }
                                    }
                            )
                        Button(action: { viewModel.toggleRecording() }) {
                            Text("REC").font(.system(size: 18, weight: .bold))
                                .foregroundColor(viewModel.audioCapture.isRecording ? .red : .radioText)
                                .frame(width: 80, height: 96).background(Color.radioSurface).cornerRadius(8)
                        }
                    }.padding(.horizontal, 8).padding(.vertical, 4).background(Color.radioBg.opacity(0.97))
                }
            }
        }
        .sheet(isPresented: Binding(
            get: { viewModel.state.showSettings },
            set: { viewModel.state.showSettings = $0 }
        )) {
            SettingsView()
        }
    }

    /// Format step size label matching web: 10→"10Hz", 100→"100Hz", 1000→"1kHz", etc.
    private func stepLabel(_ hz: Int) -> String {
        if hz >= 1000 {
            let khz = hz / 1000
            return "\(khz)kHz"
        }
        return "\(hz)Hz"
    }
}

// MARK: - PTT Bar (reused from reference with minor adaptations)

struct PTTBar: View {
    let pressing: Bool; let txLevel: Float; let txPower: Float
    let onPress: () -> Void; let onRelease: () -> Void

    var body: some View {
        VStack(spacing: 2) {
            if pressing {
                HStack(spacing: 8) {
                    Text(String(format: "%.0fW", txPower)).font(.caption.monospaced().bold()).foregroundColor(.radioRed)
                    Capsule().fill(Color.white.opacity(0.1)).frame(height: 6)
                        .overlay(alignment: .leading) {
                            Capsule().fill(Color.radioRed)
                                .frame(width: max(6, CGFloat(txLevel * 3) * 200), height: 6)
                        }
                }.padding(.horizontal, 40)
            }
            Button(action: {}) {
                Text(pressing ? "● TX ●" : "PTT")
                    .font(.system(size: 28, weight: .heavy, design: .rounded))
                    .foregroundColor(pressing ? .black : Color.red)
                    .frame(maxWidth: 320).frame(height: 84)
                    .background(pressing ? Color.red : Color.red.opacity(0.12))
                    .clipShape(Capsule())
            }
            .buttonStyle(PTTPressStyle(onPress: onPress, onRelease: onRelease))
        }
        .padding(.horizontal, 12).padding(.vertical, 4)
        .background(Color.radioBg.opacity(0.97))
    }
}

struct PTTPressStyle: ButtonStyle {
    let onPress: () -> Void; let onRelease: () -> Void
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.95 : 1.0)
            .animation(.easeOut(duration: 0.1), value: configuration.isPressed)
            .onChange(of: configuration.isPressed) { _, p in if p { onPress() } else { onRelease() } }
    }
}
