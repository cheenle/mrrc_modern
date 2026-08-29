use std::env;

pub const DEFAULT_WEB_PASSWORD: &str = "changeme_please_use_strong_password!";
pub const AUTH_COOKIE: &str = "mrrc_auth";
pub const AUTH_TOKEN_BYTES: usize = 32;
pub const POLL_TIMEOUT_MS: u64 = 250;
pub const PTT_MAX_TX_SECONDS_DEFAULT: f64 = 0.0;

#[derive(Debug, Clone, PartialEq)]
pub struct RuntimeConfig {
    pub radio_model: String,
    pub serial_port: String,
    pub baud_rate: u32,
    pub web_host: String,
    pub web_port: u16,
    pub web_password: String,
    pub audio_rx_device: Option<String>,
    pub audio_tx_device: Option<String>,
    pub ptt_max_tx_seconds: f64,
}

impl RuntimeConfig {
    pub fn from_env() -> Self {
        let radio_model = env::var("MRRC_RADIO_MODEL")
            .unwrap_or_else(|_| "ft710".to_string())
            .trim()
            .to_ascii_lowercase();
        let default_baud = match radio_model.as_str() {
            "ic7300" | "ic7300mk2" => 115_200,
            _ => 38_400,
        };
        Self {
            radio_model,
            serial_port: env_mrrc("MRRC_SERIAL_PORT", "/dev/cu.SLAB_USBtoUART"),
            baud_rate: env_mrrc("MRRC_BAUD_RATE", &default_baud.to_string())
                .parse()
                .unwrap_or(default_baud),
            web_host: env_mrrc("MRRC_WEB_HOST", "::"),
            web_port: env_mrrc("MRRC_WEB_PORT", "8888").parse().unwrap_or(8888),
            web_password: env_mrrc("MRRC_WEB_PASSWORD", DEFAULT_WEB_PASSWORD),
            audio_rx_device: non_empty_env("MRRC_AUDIO_RX_DEVICE"),
            audio_tx_device: non_empty_env("MRRC_AUDIO_TX_DEVICE"),
            ptt_max_tx_seconds: env_mrrc("MRRC_PTT_MAX_TX_SECONDS", "0")
                .parse()
                .unwrap_or(PTT_MAX_TX_SECONDS_DEFAULT),
        }
    }
}

pub fn env_mrrc(name: &str, default: &str) -> String {
    if let Ok(value) = env::var(name) {
        return value;
    }
    if let Some(suffix) = name.strip_prefix("MRRC_") {
        let legacy = format!("FT710_{suffix}");
        if let Ok(value) = env::var(legacy) {
            return value;
        }
    }
    default.to_string()
}

fn non_empty_env(name: &str) -> Option<String> {
    let value = env_mrrc(name, "");
    if value.is_empty() {
        None
    } else {
        Some(value)
    }
}
