use crate::audio::{AUDIO_TAG_OPUS, AUDIO_TAG_PCM, CODEC_FRAME_SAMPLES, CODEC_RATE};
use crate::backends::ft710::{compressor_command, set_filter_width_command, tuner_command};
use crate::scheduler::{PollIntervals, PollTaskKind};
use crate::scope::SCOPE_BINS;
use crate::web::{WS_ATR1000, WS_AUDIO_RX, WS_AUDIO_TX, WS_RADIO, WS_SPECTRUM};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompatibilityReport {
    pub endpoints: [&'static str; 5],
    pub audio_tags: [u8; 2],
    pub codec_rate: u32,
    pub codec_frame_samples: usize,
    pub scope_v1_len: usize,
    pub scope_v2_len: usize,
    pub poll_task_count: usize,
}

impl CompatibilityReport {
    pub fn current() -> Self {
        Self {
            endpoints: [WS_RADIO, WS_SPECTRUM, WS_AUDIO_RX, WS_AUDIO_TX, WS_ATR1000],
            audio_tags: [AUDIO_TAG_PCM, AUDIO_TAG_OPUS],
            codec_rate: CODEC_RATE,
            codec_frame_samples: CODEC_FRAME_SAMPLES,
            scope_v1_len: 1 + SCOPE_BINS,
            scope_v2_len: 1 + SCOPE_BINS * 2,
            poll_task_count: PollIntervals::default().task_plan().len(),
        }
    }
}

pub fn ft710_forbidden_command_forms() -> [&'static [u8]; 4] {
    [b"DN;", b"PR02;", b"AC010;", b"AC011;"]
}

pub fn generated_ft710_guarded_commands() -> Vec<Vec<u8>> {
    vec![
        set_filter_width_command(5).expect("valid filter"),
        compressor_command(false),
        compressor_command(true),
        tuner_command(0).expect("valid tuner off"),
        tuner_command(1).expect("valid tuner on"),
        tuner_command(2).expect("valid tuner tune"),
    ]
}

pub fn poll_task_kinds() -> Vec<PollTaskKind> {
    PollIntervals::default()
        .task_plan()
        .iter()
        .map(|task| task.kind)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn protocol_constants_match_production_contract() {
        let report = CompatibilityReport::current();
        assert_eq!(
            report.endpoints,
            [
                "/WSradio",
                "/WSspectrum",
                "/WSaudioRX",
                "/WSaudioTX",
                "/WSatr1000"
            ]
        );
        assert_eq!(report.audio_tags, [0x00, 0x01]);
        assert_eq!(report.codec_rate, 48_000);
        assert_eq!(report.codec_frame_samples, 960);
        assert_eq!(report.scope_v1_len, 851);
        assert_eq!(report.scope_v2_len, 1701);
        assert_eq!(report.poll_task_count, 7);
    }

    #[test]
    fn guarded_ft710_builders_never_emit_known_bad_forms() {
        let generated = generated_ft710_guarded_commands();
        for forbidden in ft710_forbidden_command_forms() {
            assert!(!generated.iter().any(|cmd| cmd.as_slice() == forbidden));
        }
        assert!(generated.iter().any(|cmd| cmd.as_slice() == b"SH0005;"));
        assert!(generated.iter().any(|cmd| cmd.as_slice() == b"PR01;"));
        assert!(generated.iter().any(|cmd| cmd.as_slice() == b"AC003;"));
    }

    #[test]
    fn poll_plan_keeps_expected_task_order() {
        assert_eq!(
            poll_task_kinds(),
            vec![
                PollTaskKind::IfFast,
                PollTaskKind::Vfo,
                PollTaskKind::TxStatus,
                PollTaskKind::TxMeters,
                PollTaskKind::Settings,
                PollTaskKind::SlowTelemetry,
                PollTaskKind::ConnectionWatchdog,
            ]
        );
    }
}
