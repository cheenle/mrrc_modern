use super::{
    CommandError, FilterModel, PttState, RadioBackend, RadioCapabilities, ScopeType, TuneVia, Vfo,
};

pub const CAPABILITIES: RadioCapabilities = RadioCapabilities {
    model_name: "ft710",
    display_name: "Yaesu FT-710",
    default_baud: 38_400,
    audio_rx_rate: 44_100,
    audio_tx_rate: 44_100,
    audio_name_hints: &["FT-710", "FT710", "YAESU"],
    has_atu: true,
    has_auto_notch: true,
    has_vd_id_meters: true,
    vfo_b_direct: true,
    filter_model: FilterModel::WidthTable,
    att_steps: &[0, 6, 12, 18],
    preamp_steps: &["OFF", "AMP1", "AMP2"],
    scope_type: ScopeType::Ft4222,
    tune_via: TuneVia::Tx2,
};

#[derive(Debug, Default)]
pub struct Ft710Backend;

impl RadioBackend for Ft710Backend {
    fn capabilities(&self) -> RadioCapabilities {
        CAPABILITIES
    }

    fn set_frequency_command(&self, hz: u64, vfo: Vfo) -> Result<Vec<u8>, CommandError> {
        if !(30_000..=75_000_000).contains(&hz) {
            return Err(CommandError::OutOfRange("frequency"));
        }
        let prefix = match vfo {
            Vfo::A => "FA",
            Vfo::B => "FB",
        };
        Ok(format!("{prefix}{hz:09};").into_bytes())
    }

    fn set_mode_command(&self, mode: u8) -> Result<Vec<u8>, CommandError> {
        if mode > 0x0f {
            return Err(CommandError::OutOfRange("mode"));
        }
        Ok(format!("MD0{mode:X};").into_bytes())
    }

    fn set_ptt_command(&self, state: PttState) -> Result<Vec<u8>, CommandError> {
        let cmd = match state {
            PttState::Rx => "TX0;",
            PttState::Tx => "TX1;",
            PttState::Tune => "TX2;",
        };
        Ok(cmd.as_bytes().to_vec())
    }
}

pub fn set_filter_width_command(index: u8) -> Result<Vec<u8>, CommandError> {
    if index > 23 {
        return Err(CommandError::OutOfRange("filter_width"));
    }
    // SDD guard: FT-710 SET format is SH00NN, not SH0NN.
    Ok(format!("SH00{index:02};").into_bytes())
}

pub fn compressor_command(enabled: bool) -> Vec<u8> {
    // SDD guard: PR00=OFF / PR01=ON. PR02 is known to kill TX audio.
    if enabled {
        b"PR01;".to_vec()
    } else {
        b"PR00;".to_vec()
    }
}

pub fn tuner_command(status: u8) -> Result<Vec<u8>, CommandError> {
    let cmd = match status {
        0 => "AC000;",
        1 => "AC001;",
        2 => "AC003;",
        _ => return Err(CommandError::OutOfRange("tuner")),
    };
    Ok(cmd.as_bytes().to_vec())
}

pub fn parse_frequency_response(response: &str, prefix: &str) -> Option<u64> {
    let body = response.strip_suffix(';').unwrap_or(response);
    let digits = body.strip_prefix(prefix)?;
    if digits.len() != 9 || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    digits.parse().ok()
}

pub fn parse_mode_response(response: &str) -> Option<u8> {
    let body = response.strip_suffix(';').unwrap_or(response);
    let raw = body.strip_prefix("MD0")?;
    u8::from_str_radix(raw, 16).ok()
}

pub fn parse_bool_response(response: &str, prefix: &str) -> Option<bool> {
    let body = response.strip_suffix(';').unwrap_or(response);
    match body.strip_prefix(prefix)? {
        "0" | "00" => Some(false),
        "1" | "01" => Some(true),
        _ => None,
    }
}

pub fn parse_meter_response(response: &str, prefix: &str) -> Option<u8> {
    let body = response.strip_suffix(';').unwrap_or(response);
    let digits = body.strip_prefix(prefix)?;
    let value: u16 = digits.parse().ok()?;
    u8::try_from(value).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_guarded_ft710_commands() {
        let backend = Ft710Backend;
        assert_eq!(
            backend.set_frequency_command(14_200_000, Vfo::A).unwrap(),
            b"FA014200000;"
        );
        assert_eq!(backend.set_ptt_command(PttState::Rx).unwrap(), b"TX0;");
        assert_eq!(set_filter_width_command(5).unwrap(), b"SH0005;");
        assert_eq!(compressor_command(false), b"PR00;");
        assert_eq!(compressor_command(true), b"PR01;");
        assert_eq!(tuner_command(2).unwrap(), b"AC003;");
    }

    #[test]
    fn parses_ft710_responses() {
        assert_eq!(
            parse_frequency_response("FA014200000;", "FA"),
            Some(14_200_000)
        );
        assert_eq!(
            parse_frequency_response("FB007050000", "FB"),
            Some(7_050_000)
        );
        assert_eq!(parse_frequency_response("FA14200000;", "FA"), None);
        assert_eq!(parse_mode_response("MD02;"), Some(2));
        assert_eq!(parse_bool_response("PR01;", "PR0"), Some(true));
        assert_eq!(parse_bool_response("PR00;", "PR0"), Some(false));
        assert_eq!(parse_meter_response("SM0255;", "SM0"), Some(255));
    }
}
