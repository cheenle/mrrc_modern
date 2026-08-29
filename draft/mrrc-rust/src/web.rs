pub const WS_RADIO: &str = "/WSradio";
pub const WS_SPECTRUM: &str = "/WSspectrum";
pub const WS_AUDIO_RX: &str = "/WSaudioRX";
pub const WS_AUDIO_TX: &str = "/WSaudioTX";
pub const WS_ATR1000: &str = "/WSatr1000";

pub const AUDIO_TAG_PCM: u8 = crate::audio::AUDIO_TAG_PCM;
pub const AUDIO_TAG_OPUS: u8 = crate::audio::AUDIO_TAG_OPUS;

use std::path::{Component, Path, PathBuf};

use serde_json::{Map, Value};

use crate::protocol::ws::RadioServerMessage;

pub fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    let max_len = left.len().max(right.len());
    let mut diff = left.len() ^ right.len();
    for i in 0..max_len {
        let a = left.get(i).copied().unwrap_or(0);
        let b = right.get(i).copied().unwrap_or(0);
        diff |= usize::from(a ^ b);
    }
    diff == 0
}

pub fn resolve_static_path(static_dir: &Path, request_path: &str) -> Option<PathBuf> {
    let relative = request_path.trim_start_matches('/');
    let mut safe = PathBuf::new();
    for component in Path::new(relative).components() {
        match component {
            Component::Normal(part) => safe.push(part),
            Component::CurDir => {}
            Component::Prefix(_) | Component::RootDir | Component::ParentDir => return None,
        }
    }
    Some(static_dir.join(safe))
}

pub fn state_update_message(fields: Map<String, Value>) -> RadioServerMessage {
    let dirty = fields.keys().cloned().collect();
    RadioServerMessage::StateUpdate {
        fields: Value::Object(fields),
        dirty,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compares_passwords_without_early_exit_semantics() {
        assert!(constant_time_eq(b"abc", b"abc"));
        assert!(!constant_time_eq(b"abc", b"abd"));
        assert!(!constant_time_eq(b"abc", b"abcd"));
    }

    #[test]
    fn rejects_static_path_traversal() {
        let root = Path::new("/srv/static");
        assert_eq!(
            resolve_static_path(root, "/index.html").unwrap(),
            PathBuf::from("/srv/static/index.html")
        );
        assert!(resolve_static_path(root, "/../server.py").is_none());
        assert!(resolve_static_path(root, "/nested/../../server.py").is_none());
    }

    #[test]
    fn builds_state_update_message_with_dirty_list() {
        let mut fields = Map::new();
        fields.insert("tx_status".into(), Value::Number(1.into()));
        let msg = state_update_message(fields);
        match msg {
            RadioServerMessage::StateUpdate { fields, dirty } => {
                assert_eq!(dirty, vec!["tx_status".to_string()]);
                assert_eq!(fields["tx_status"], Value::Number(1.into()));
            }
            _ => panic!("expected state update"),
        }
    }
}
