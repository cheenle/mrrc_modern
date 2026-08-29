pub const SCOPE_BINS: usize = 850;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScopeFrame {
    pub wf1: [u8; SCOPE_BINS],
    pub wf2: Option<[u8; SCOPE_BINS]>,
    pub frame_count: u64,
    pub start_freq_hz: Option<u64>,
}

impl ScopeFrame {
    pub fn encode_ws_payload(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(if self.wf2.is_some() { 1701 } else { 851 });
        if let Some(wf2) = &self.wf2 {
            out.push(0x02);
            out.extend_from_slice(&self.wf1);
            out.extend_from_slice(wf2);
        } else {
            out.push(0x01);
            out.extend_from_slice(&self.wf1);
        }
        out
    }
}

#[derive(Debug, Clone, Default)]
pub struct ScopeBroadcasterState {
    last_sent_frame_count: u64,
}

impl ScopeBroadcasterState {
    pub fn should_send_real_frame(&mut self, frame_count: u64) -> bool {
        if frame_count == self.last_sent_frame_count {
            return false;
        }
        self.last_sent_frame_count = frame_count;
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_v1_and_v2_payload_sizes() {
        let frame = ScopeFrame {
            wf1: [0; SCOPE_BINS],
            wf2: None,
            frame_count: 1,
            start_freq_hz: None,
        };
        assert_eq!(frame.encode_ws_payload().len(), 851);
        let frame = ScopeFrame {
            wf1: [0; SCOPE_BINS],
            wf2: Some([1; SCOPE_BINS]),
            frame_count: 2,
            start_freq_hz: None,
        };
        assert_eq!(frame.encode_ws_payload().len(), 1701);
    }

    #[test]
    fn only_sends_advancing_real_scope_frames() {
        let mut state = ScopeBroadcasterState::default();
        assert!(state.should_send_real_frame(1));
        assert!(!state.should_send_real_frame(1));
        assert!(state.should_send_real_frame(2));
    }
}
