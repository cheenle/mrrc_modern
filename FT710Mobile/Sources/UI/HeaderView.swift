import SwiftUI

/// Top header: status dots + VFO A/B + power toggle.
/// (S 值不在这里重复显示——频谱下方的 SMeterView 已有。)
struct HeaderView: View {
    @EnvironmentObject var viewModel: RadioViewModel

    var body: some View {
        VStack(spacing: 2) {
            // Row 1: Status + meters (1.5× size)
            HStack(spacing: 12) {
                wsDot(viewModel.state.ctrlConnected, "C")
                wsDot(viewModel.state.spectrumConnected, "S")
                wsDot(viewModel.state.audioRXConnected, "R")
                wsDot(viewModel.state.audioTXConnected, "T")

                if viewModel.state.tunerStatus == 2 {
                    Text("TUNE").font(.system(size: 18, weight: .bold)).foregroundColor(.radioAccent)
                }

                if viewModel.state.serialConnected {
                    Circle().fill(Color.green).frame(width: 9, height: 9)
                }

                Spacer()

                // Settings gear button
                Button(action: { viewModel.state.showSettings = true }) {
                    Image(systemName: "gearshape")
                        .font(.title3)
                        .foregroundColor(.radioMuted)
                }

                // VFO A/B toggle
                HStack(spacing: 0) {
                    Button("A") { viewModel.setVFO("A") }
                        .font(.system(size: 14, weight: .bold))
                        .foregroundColor(viewModel.state.activeVFO == "A" ? .black : .radioMuted)
                        .frame(width: 42, height: 36)
                        .background(viewModel.state.activeVFO == "A" ? Color.radioAccent : Color.radioSurface)
                    Button("B") { viewModel.setVFO("B") }
                        .font(.system(size: 14, weight: .bold))
                        .foregroundColor(viewModel.state.activeVFO == "B" ? .black : .radioMuted)
                        .frame(width: 42, height: 36)
                        .background(viewModel.state.activeVFO == "B" ? Color.radioAccent : Color.radioSurface)
                }.cornerRadius(5)

                // Power toggle
                Button(action: {
                    viewModel.state.powerOn ? viewModel.powerOff() : viewModel.powerOnAsync()
                }) {
                    Image(systemName: viewModel.state.powerOn ? "power.circle.fill" : "power.circle")
                        .font(.title3).foregroundColor(viewModel.state.powerOn ? .green : .radioMuted)
                }
            }

            // Row 2: Frequency display (fills row)
            FrequencyDisplayView(freqHz: viewModel.state.activeFreq)
                .padding(.horizontal, 4)

            // Row 3: Band name
            HStack {
                Spacer()
                Text(viewModel.state.bandName)
                    .font(.system(size: 14, weight: .bold)).foregroundColor(.radioAccent)
            }.padding(.horizontal, 12)
        }
        .padding(.vertical, 4)
    }

    private func wsDot(_ on: Bool, _ label: String) -> some View {
        HStack(spacing: 3) {
            Circle().fill(on ? Color.green : Color.red).frame(width: 8, height: 8)
            Text(label).font(.system(size: 12, weight: .bold)).foregroundColor(.radioMuted)
        }
    }
}
