use crate::protocol::ws::MemoryChannelWire;
use serde::{Deserialize, Serialize};
use std::fs;
use std::io;
use std::path::Path;

pub const MEM_CHANNEL_COUNT: usize = 6;
pub const MIN_FREQ_HZ: u64 = 30_000;
pub const MAX_FREQ_HZ: u64 = 75_000_000;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MemoryStore {
    channels: Vec<Option<MemoryChannelWire>>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct MemoryFile {
    channels: Vec<Option<MemoryChannelWire>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MemoryError {
    WrongSlotCount { got: usize, expected: usize },
    InvalidName { index: usize },
    InvalidFrequency { index: usize, freq: u64 },
    InvalidMode { index: usize },
    InvalidIndex { index: usize },
}

impl Default for MemoryStore {
    fn default() -> Self {
        Self {
            channels: vec![None; MEM_CHANNEL_COUNT],
        }
    }
}

impl MemoryStore {
    pub fn channels(&self) -> &[Option<MemoryChannelWire>] {
        &self.channels
    }

    pub fn replace_all(
        &mut self,
        channels: Vec<Option<MemoryChannelWire>>,
    ) -> Result<(), MemoryError> {
        validate_channels(&channels)?;
        self.channels = channels;
        Ok(())
    }

    pub fn delete(&mut self, index: usize) -> Result<(), MemoryError> {
        let Some(slot) = self.channels.get_mut(index) else {
            return Err(MemoryError::InvalidIndex { index });
        };
        *slot = None;
        Ok(())
    }

    pub fn load_json_file(path: &Path) -> Result<Self, MemoryIoError> {
        let text = fs::read_to_string(path).map_err(MemoryIoError::Io)?;
        let file: MemoryFile = serde_json::from_str(&text).map_err(MemoryIoError::Json)?;
        validate_channels(&file.channels).map_err(MemoryIoError::Validation)?;
        Ok(Self {
            channels: file.channels,
        })
    }

    pub fn save_json_file_atomic(&self, path: &Path) -> Result<(), MemoryIoError> {
        validate_channels(&self.channels).map_err(MemoryIoError::Validation)?;
        let parent = path.parent().unwrap_or_else(|| Path::new("."));
        fs::create_dir_all(parent).map_err(MemoryIoError::Io)?;
        let tmp_path = path.with_extension("json.tmp");
        let payload = serde_json::to_vec_pretty(&MemoryFile {
            channels: self.channels.clone(),
        })
        .map_err(MemoryIoError::Json)?;
        fs::write(&tmp_path, payload).map_err(MemoryIoError::Io)?;
        fs::rename(&tmp_path, path).map_err(MemoryIoError::Io)?;
        Ok(())
    }
}

#[derive(Debug)]
pub enum MemoryIoError {
    Io(io::Error),
    Json(serde_json::Error),
    Validation(MemoryError),
}

pub fn validate_channels(channels: &[Option<MemoryChannelWire>]) -> Result<(), MemoryError> {
    if channels.len() != MEM_CHANNEL_COUNT {
        return Err(MemoryError::WrongSlotCount {
            got: channels.len(),
            expected: MEM_CHANNEL_COUNT,
        });
    }
    for (index, channel) in channels.iter().enumerate() {
        let Some(channel) = channel else { continue };
        if channel.name.trim().is_empty() || channel.name.len() > 32 {
            return Err(MemoryError::InvalidName { index });
        }
        if !(MIN_FREQ_HZ..=MAX_FREQ_HZ).contains(&channel.freq) {
            return Err(MemoryError::InvalidFrequency {
                index,
                freq: channel.freq,
            });
        }
        if channel.mode.trim().is_empty() || channel.mode.len() > 12 {
            return Err(MemoryError::InvalidMode { index });
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_slot_count_and_frequency() {
        assert_eq!(
            validate_channels(&[]),
            Err(MemoryError::WrongSlotCount {
                got: 0,
                expected: 6
            })
        );
        let mut channels = vec![None; MEM_CHANNEL_COUNT];
        channels[0] = Some(MemoryChannelWire {
            name: "20m".into(),
            freq: 14_200_000,
            mode: "USB".into(),
        });
        assert!(validate_channels(&channels).is_ok());
        channels[0].as_mut().unwrap().freq = 1;
        assert_eq!(
            validate_channels(&channels),
            Err(MemoryError::InvalidFrequency { index: 0, freq: 1 })
        );
    }

    #[test]
    fn persists_memory_file_atomically() {
        let mut store = MemoryStore::default();
        let mut channels = vec![None; MEM_CHANNEL_COUNT];
        channels[1] = Some(MemoryChannelWire {
            name: "forty".into(),
            freq: 7_050_000,
            mode: "LSB".into(),
        });
        store.replace_all(channels).unwrap();
        let path =
            std::env::temp_dir().join(format!("mrrc-memory-test-{}.json", std::process::id()));
        store.save_json_file_atomic(&path).unwrap();
        let loaded = MemoryStore::load_json_file(&path).unwrap();
        assert_eq!(loaded.channels()[1].as_ref().unwrap().freq, 7_050_000);
        let _ = fs::remove_file(path);
    }
}
