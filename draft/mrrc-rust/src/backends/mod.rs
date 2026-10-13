pub mod ft710;
pub mod ic7300;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RadioCapabilities {
    pub model_name: &'static str,
    pub display_name: &'static str,
    pub default_baud: u32,
    pub audio_rx_rate: u32,
    pub audio_tx_rate: u32,
    pub audio_name_hints: &'static [&'static str],
    pub has_atu: bool,
    pub has_auto_notch: bool,
    pub has_vd_id_meters: bool,
    pub vfo_b_direct: bool,
    pub filter_model: FilterModel,
    pub att_steps: &'static [u8],
    pub preamp_steps: &'static [&'static str],
    pub scope_type: ScopeType,
    pub tune_via: TuneVia,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FilterModel {
    WidthTable,
    Fil123,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScopeType {
    Ft4222,
    Civ27,
    None,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TuneVia {
    Tx2,
    Atu,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Vfo {
    A,
    B,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PttState {
    Rx,
    Tx,
    Tune,
}

pub trait RadioBackend {
    fn capabilities(&self) -> RadioCapabilities;
    fn set_frequency_command(&self, hz: u64, vfo: Vfo) -> Result<Vec<u8>, CommandError>;
    fn set_mode_command(&self, mode: u8) -> Result<Vec<u8>, CommandError>;
    fn set_ptt_command(&self, state: PttState) -> Result<Vec<u8>, CommandError>;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackendKey {
    Ft710,
    Ic7300,
    Ic7300Mk2,
}

impl BackendKey {
    pub fn parse(input: &str) -> Option<Self> {
        match input.trim().to_ascii_lowercase().as_str() {
            "ft710" => Some(Self::Ft710),
            "ic7300" => Some(Self::Ic7300),
            "ic7300mk2" => Some(Self::Ic7300Mk2),
            _ => None,
        }
    }

    pub fn capabilities(self) -> RadioCapabilities {
        match self {
            Self::Ft710 => ft710::CAPABILITIES,
            Self::Ic7300 => ic7300::IC7300_CAPABILITIES,
            Self::Ic7300Mk2 => ic7300::IC7300_MK2_CAPABILITIES,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CommandError {
    OutOfRange(&'static str),
    Unsupported(&'static str),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backend_key_selects_capabilities() {
        assert_eq!(BackendKey::parse("FT710"), Some(BackendKey::Ft710));
        assert_eq!(BackendKey::parse("ic7300mk2"), Some(BackendKey::Ic7300Mk2));
        assert_eq!(BackendKey::parse("bad"), None);
        assert_eq!(BackendKey::Ft710.capabilities().default_baud, 38_400);
        assert_eq!(BackendKey::Ic7300.capabilities().audio_rx_rate, 48_000);
    }
}
