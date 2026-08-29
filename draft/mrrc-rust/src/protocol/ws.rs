use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum RadioClientMessage {
    #[serde(rename = "set")]
    Set { field: String, value: Value },
    #[serde(rename = "get")]
    Get { field: String },
    #[serde(rename = "memSave")]
    MemSave {
        channels: Vec<Option<MemoryChannelWire>>,
    },
    #[serde(rename = "memDelete")]
    MemDelete { index: usize },
    #[serde(rename = "ping")]
    Ping,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum RadioServerMessage {
    #[serde(rename = "fullState")]
    FullState {
        data: Value,
        #[serde(rename = "radioModel")]
        radio_model: String,
        #[serde(rename = "radioDisplayName")]
        radio_display_name: String,
        capabilities: Value,
        bands: Value,
        modes: Value,
    },
    #[serde(rename = "stateUpdate")]
    StateUpdate { fields: Value, dirty: Vec<String> },
    #[serde(rename = "value")]
    Value { field: String, value: Value },
    #[serde(rename = "memChannels")]
    MemChannels {
        channels: Vec<Option<MemoryChannelWire>>,
    },
    #[serde(rename = "pong")]
    Pong,
    #[serde(rename = "error")]
    Error { message: String },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MemoryChannelWire {
    pub name: String,
    pub freq: u64,
    pub mode: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AudioCodecTag {
    Pcm,
    Opus,
}

impl AudioCodecTag {
    pub fn from_byte(byte: u8) -> Option<Self> {
        match byte {
            crate::audio::AUDIO_TAG_PCM => Some(Self::Pcm),
            crate::audio::AUDIO_TAG_OPUS => Some(Self::Opus),
            _ => None,
        }
    }

    pub fn as_byte(self) -> u8 {
        match self {
            Self::Pcm => crate::audio::AUDIO_TAG_PCM,
            Self::Opus => crate::audio::AUDIO_TAG_OPUS,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AudioTxTextMessage {
    StopTx,
    Settings(String),
    Unknown(String),
}

pub fn parse_audio_tx_text(input: &str) -> AudioTxTextMessage {
    if input == "s:" {
        AudioTxTextMessage::StopTx
    } else if let Some(rest) = input.strip_prefix("m:") {
        AudioTxTextMessage::Settings(rest.to_string())
    } else {
        AudioTxTextMessage::Unknown(input.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_legacy_radio_set_message() {
        let msg: RadioClientMessage =
            serde_json::from_str(r#"{"type":"set","field":"ptt","value":true}"#).unwrap();
        assert_eq!(
            msg,
            RadioClientMessage::Set {
                field: "ptt".into(),
                value: Value::Bool(true)
            }
        );
    }

    #[test]
    fn preserves_audio_tx_text_contract() {
        assert_eq!(parse_audio_tx_text("s:"), AudioTxTextMessage::StopTx);
        assert_eq!(
            parse_audio_tx_text("m:48000,opus"),
            AudioTxTextMessage::Settings("48000,opus".into())
        );
    }
}
