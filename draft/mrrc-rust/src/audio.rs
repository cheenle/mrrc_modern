use std::collections::VecDeque;

pub const CODEC_RATE: u32 = 48_000;
pub const FRAME_MS: u32 = 20;
pub const CODEC_FRAME_SAMPLES: usize = 960;
pub const AUDIO_TAG_PCM: u8 = 0x00;
pub const AUDIO_TAG_OPUS: u8 = 0x01;
pub const DEFAULT_BITRATE: u32 = 64_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AudioProfile {
    pub rx_device_rate: u32,
    pub tx_device_rate: u32,
    pub channels: u16,
}

impl AudioProfile {
    pub const FT710: Self = Self {
        rx_device_rate: 44_100,
        tx_device_rate: 44_100,
        channels: 1,
    };
    pub const IC7300: Self = Self {
        rx_device_rate: 48_000,
        tx_device_rate: 48_000,
        channels: 1,
    };

    pub fn samples_per_frame(rate: u32) -> usize {
        (rate * FRAME_MS / 1000) as usize
    }
}

pub fn resample_linear_i16(input: &[i16], from_rate: u32, to_rate: u32) -> Vec<i16> {
    if from_rate == to_rate || input.is_empty() {
        return input.to_vec();
    }
    let out_len = ((input.len() as u64 * u64::from(to_rate)) / u64::from(from_rate)) as usize;
    if out_len == 0 {
        return Vec::new();
    }
    let scale = (input.len() - 1) as f64 / (out_len.saturating_sub(1).max(1)) as f64;
    (0..out_len)
        .map(|i| {
            let pos = i as f64 * scale;
            let lo = pos.floor() as usize;
            let hi = (lo + 1).min(input.len() - 1);
            let frac = pos - lo as f64;
            let sample = input[lo] as f64 * (1.0 - frac) + input[hi] as f64 * frac;
            sample.round().clamp(i16::MIN as f64, i16::MAX as f64) as i16
        })
        .collect()
}

#[derive(Debug, Clone)]
pub struct TxJitterBuffer {
    frames: VecDeque<Vec<i16>>,
    queued_samples: usize,
    prebuffer_samples: usize,
    max_samples: usize,
    primed: bool,
    queue_drops: usize,
}

impl TxJitterBuffer {
    pub fn new(sample_rate: u32, prebuffer_ms: u32, max_buffer_ms: u32) -> Self {
        Self {
            frames: VecDeque::new(),
            queued_samples: 0,
            prebuffer_samples: (sample_rate * prebuffer_ms / 1000) as usize,
            max_samples: (sample_rate * max_buffer_ms / 1000) as usize,
            primed: false,
            queue_drops: 0,
        }
    }

    pub fn push_frame(&mut self, frame: Vec<i16>) {
        self.queued_samples += frame.len();
        self.frames.push_back(frame);
        while self.queued_samples > self.max_samples {
            if let Some(dropped) = self.frames.pop_front() {
                self.queued_samples = self.queued_samples.saturating_sub(dropped.len());
                self.queue_drops += 1;
            } else {
                break;
            }
        }
        if self.queued_samples >= self.prebuffer_samples {
            self.primed = true;
        }
    }

    pub fn pop_ready_frame(&mut self) -> Option<Vec<i16>> {
        if !self.primed {
            return None;
        }
        let frame = self.frames.pop_front()?;
        self.queued_samples = self.queued_samples.saturating_sub(frame.len());
        Some(frame)
    }

    pub fn queue_drops(&self) -> usize {
        self.queue_drops
    }

    pub fn queued_samples(&self) -> usize {
        self.queued_samples
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_frame_boundaries_for_ft710_bridge() {
        let codec_frame = vec![0i16; CODEC_FRAME_SAMPLES];
        assert_eq!(resample_linear_i16(&codec_frame, 48_000, 44_100).len(), 882);
        let device_frame = vec![0i16; 882];
        assert_eq!(
            resample_linear_i16(&device_frame, 44_100, 48_000).len(),
            960
        );
    }

    #[test]
    fn tx_jitter_buffer_prebuffers_and_drops_oldest_at_cap() {
        let mut buffer = TxJitterBuffer::new(48_000, 60, 80);
        buffer.push_frame(vec![1; 960]);
        buffer.push_frame(vec![2; 960]);
        assert!(buffer.pop_ready_frame().is_none());
        buffer.push_frame(vec![3; 960]);
        assert_eq!(buffer.pop_ready_frame().unwrap()[0], 1);
        buffer.push_frame(vec![4; 960]);
        buffer.push_frame(vec![5; 960]);
        buffer.push_frame(vec![6; 960]);
        assert!(buffer.queue_drops() > 0);
        assert!(buffer.queued_samples() <= 48_000 * 80 / 1000);
    }
}
