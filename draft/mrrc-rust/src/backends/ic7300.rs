use super::{
    CommandError, FilterModel, PttState, RadioBackend, RadioCapabilities, ScopeType, TuneVia, Vfo,
};
use crate::protocol::civ::{build_frame, encode_freq_bcd};

pub const IC7300_CAPABILITIES: RadioCapabilities = RadioCapabilities {
    model_name: "ic7300",
    display_name: "Icom IC-7300",
    default_baud: 115_200,
    audio_rx_rate: 48_000,
    audio_tx_rate: 48_000,
    audio_name_hints: &["IC-7300", "USB Audio"],
    has_atu: true,
    has_auto_notch: false,
    has_vd_id_meters: false,
    vfo_b_direct: false,
    filter_model: FilterModel::Fil123,
    att_steps: &[0, 20],
    preamp_steps: &["OFF", "P.AMP"],
    scope_type: ScopeType::Civ27,
    tune_via: TuneVia::Atu,
};

pub const IC7300_MK2_CAPABILITIES: RadioCapabilities = RadioCapabilities {
    model_name: "ic7300mk2",
    display_name: "Icom IC-7300MK2",
    ..IC7300_CAPABILITIES
};

#[derive(Debug, Clone)]
pub struct Ic7300Backend {
    pub civ_addr: u8,
}

impl Default for Ic7300Backend {
    fn default() -> Self {
        Self { civ_addr: 0x94 }
    }
}

impl RadioBackend for Ic7300Backend {
    fn capabilities(&self) -> RadioCapabilities {
        IC7300_CAPABILITIES
    }

    fn set_frequency_command(&self, hz: u64, _vfo: Vfo) -> Result<Vec<u8>, CommandError> {
        let hz = u32::try_from(hz).map_err(|_| CommandError::OutOfRange("frequency"))?;
        Ok(build_frame(0x05, &encode_freq_bcd(hz), self.civ_addr, 0xE0))
    }

    fn set_mode_command(&self, mode: u8) -> Result<Vec<u8>, CommandError> {
        Ok(build_frame(0x06, &[mode, 0x01], self.civ_addr, 0xE0))
    }

    fn set_ptt_command(&self, state: PttState) -> Result<Vec<u8>, CommandError> {
        let value = match state {
            PttState::Rx => 0x00,
            PttState::Tx | PttState::Tune => 0x01,
        };
        Ok(build_frame(0x1C, &[0x00, value], self.civ_addr, 0xE0))
    }
}
