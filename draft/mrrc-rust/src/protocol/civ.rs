pub const PREAMBLE: [u8; 2] = [0xFE, 0xFE];
pub const END_OF_MESSAGE: u8 = 0xFD;
pub const CONTROLLER_ADDR: u8 = 0xE0;
pub const RADIO_ADDR_IC7300: u8 = 0x94;
pub const DEFAULT_MAX_FRAME: usize = 64;
pub const SCOPE_CMD: u8 = 0x27;
pub const SCOPE_SUB_DATA: u8 = 0x00;
pub const SCOPE_AMPLITUDE_MAX: u8 = 160;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CivFrame {
    pub to: u8,
    pub from_addr: u8,
    pub command: u8,
    pub data: Vec<u8>,
}

impl CivFrame {
    pub fn to_bytes(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(self.data.len() + 6);
        out.extend_from_slice(&PREAMBLE);
        out.push(self.to);
        out.push(self.from_addr);
        out.push(self.command);
        out.extend_from_slice(&self.data);
        out.push(END_OF_MESSAGE);
        out
    }
}

pub fn build_frame(command: u8, data: &[u8], to: u8, from_addr: u8) -> Vec<u8> {
    CivFrame {
        to,
        from_addr,
        command,
        data: data.to_vec(),
    }
    .to_bytes()
}

#[derive(Debug, Default)]
pub struct CivFrameParser {
    max_frame_size: usize,
    buf: Vec<u8>,
    pub discarded_bytes: usize,
}

impl CivFrameParser {
    pub fn new() -> Self {
        Self {
            max_frame_size: DEFAULT_MAX_FRAME,
            buf: Vec::new(),
            discarded_bytes: 0,
        }
    }

    pub fn feed(&mut self, data: &[u8]) -> Vec<CivFrame> {
        self.buf.extend_from_slice(data);
        let mut frames = Vec::new();
        loop {
            let Some(start) = find_preamble(&self.buf) else {
                let keep = usize::from(self.buf.last() == Some(&0xFE));
                self.discarded_bytes += self.buf.len().saturating_sub(keep);
                self.buf.drain(..self.buf.len().saturating_sub(keep));
                break;
            };
            if start > 0 {
                self.discarded_bytes += start;
                self.buf.drain(..start);
            }
            if self.buf.len() < 6 {
                break;
            }
            let mut end = None;
            let scan_limit = self.max_frame_size.min(self.buf.len() - 2);
            for i in 2..(2 + scan_limit) {
                if self.buf[i] == END_OF_MESSAGE {
                    end = Some(i);
                    break;
                }
                if i > 2 && self.buf[i] == 0xFE && self.buf.get(i + 1) == Some(&0xFE) {
                    self.discarded_bytes += i;
                    self.buf.drain(..i);
                    end = None;
                    break;
                }
            }
            let Some(end_idx) = end else {
                if self.buf.len() > self.max_frame_size + 2 {
                    self.discarded_bytes += self.max_frame_size + 2;
                    self.buf.drain(..self.max_frame_size + 2);
                    continue;
                }
                break;
            };
            if end_idx < 5 {
                self.discarded_bytes += end_idx + 1;
                self.buf.drain(..end_idx + 1);
                continue;
            }
            frames.push(CivFrame {
                to: self.buf[2],
                from_addr: self.buf[3],
                command: self.buf[4],
                data: self.buf[5..end_idx].to_vec(),
            });
            self.buf.drain(..end_idx + 1);
        }
        frames
    }
}

fn find_preamble(buf: &[u8]) -> Option<usize> {
    buf.windows(2).position(|w| w == PREAMBLE)
}

pub fn encode_freq_bcd(hz: u32) -> [u8; 5] {
    let mut out = [0u8; 5];
    for (i, byte) in out.iter_mut().enumerate() {
        let low = (hz / 10_u32.pow((2 * i) as u32)) % 10;
        let high = (hz / 10_u32.pow((2 * i + 1) as u32)) % 10;
        *byte = ((high as u8) << 4) | (low as u8);
    }
    out
}

pub fn decode_freq_bcd(data: &[u8]) -> Result<u64, &'static str> {
    if data.len() > 5 {
        return Err("frequency BCD longer than five bytes");
    }
    let mut hz = 0u64;
    for (i, b) in data.iter().enumerate() {
        hz += u64::from(b & 0x0f) * 10_u64.pow((2 * i) as u32);
        hz += u64::from(b >> 4) * 10_u64.pow((2 * i + 1) as u32);
    }
    Ok(hz)
}

pub fn encode_level_bcd(value: u16) -> Result<[u8; 2], &'static str> {
    if value > 255 {
        return Err("level out of range");
    }
    Ok([
        (value / 100) as u8,
        ((((value / 10) % 10) << 4) | (value % 10)) as u8,
    ])
}

pub fn decode_level_bcd(data: &[u8]) -> Result<u16, &'static str> {
    match data {
        [one] => Ok(u16::from(one >> 4) * 10 + u16::from(one & 0x0f)),
        [hundreds, rest] => {
            Ok(u16::from(*hundreds) * 100 + u16::from(rest >> 4) * 10 + u16::from(rest & 0x0f))
        }
        _ => Err("level BCD must be one or two bytes"),
    }
}

pub fn is_echo(frame: &CivFrame, last_sent: &[u8]) -> bool {
    frame.from_addr == CONTROLLER_ADDR && frame.to_bytes() == last_sent
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScopeSegment {
    pub sequence: u8,
    pub sequence_max: u8,
    pub bins: Vec<u8>,
    pub is_division_start: bool,
    pub scope_mode: Option<u8>,
    pub center_freq_hz: Option<u64>,
    pub span_hz: Option<u64>,
    pub low_edge_hz: Option<u64>,
    pub high_edge_hz: Option<u64>,
    pub out_of_range: bool,
}

impl ScopeSegment {
    pub fn is_last(&self) -> bool {
        self.sequence == self.sequence_max
    }
}

pub fn parse_scope_segment(frame: &CivFrame) -> Option<ScopeSegment> {
    if frame.command != SCOPE_CMD || frame.data.len() < 4 || frame.data[0] != SCOPE_SUB_DATA {
        return None;
    }
    let sequence = bcd_byte_to_int(frame.data[2])?;
    let sequence_max = bcd_byte_to_int(frame.data[3])?;
    if sequence == 0 || sequence > sequence_max {
        return None;
    }
    if sequence == 1 {
        if frame.data.len() < 16 {
            return None;
        }
        let scope_mode = frame.data[4];
        let first = decode_freq_bcd(&frame.data[5..10]).ok()?;
        let second = decode_freq_bcd(&frame.data[10..15]).ok()?;
        let out_of_range = frame.data[15] != 0;
        let (center_freq_hz, span_hz, low_edge_hz, high_edge_hz) = match scope_mode {
            0x00 => (Some(first), Some(second.saturating_mul(2)), None, None),
            _ => (None, None, Some(first), Some(second)),
        };
        return Some(ScopeSegment {
            sequence,
            sequence_max,
            bins: Vec::new(),
            is_division_start: true,
            scope_mode: Some(scope_mode),
            center_freq_hz,
            span_hz,
            low_edge_hz,
            high_edge_hz,
            out_of_range,
        });
    }
    let bins = frame.data[4..].to_vec();
    if bins.iter().any(|v| *v > SCOPE_AMPLITUDE_MAX) {
        return None;
    }
    Some(ScopeSegment {
        sequence,
        sequence_max,
        bins,
        is_division_start: false,
        scope_mode: None,
        center_freq_hz: None,
        span_hz: None,
        low_edge_hz: None,
        high_edge_hz: None,
        out_of_range: false,
    })
}

fn bcd_byte_to_int(byte: u8) -> Option<u8> {
    let high = byte >> 4;
    let low = byte & 0x0f;
    if high <= 9 && low <= 9 {
        Some(high * 10 + low)
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bcd_frequency_round_trips() {
        let encoded = encode_freq_bcd(14_074_000);
        assert_eq!(encoded, [0x00, 0x40, 0x07, 0x14, 0x00]);
        assert_eq!(decode_freq_bcd(&encoded).unwrap(), 14_074_000);
    }

    #[test]
    fn parser_handles_split_frames_and_garbage() {
        let mut parser = CivFrameParser::new();
        assert!(parser.feed(&[0x00, 0xFE]).is_empty());
        let frames = parser.feed(&[0xFE, 0x94, 0xE0, 0x03, 0xFD]);
        assert_eq!(frames.len(), 1);
        assert_eq!(frames[0].command, 0x03);
        assert_eq!(parser.discarded_bytes, 1);
    }

    #[test]
    fn parses_scope_metadata_and_bin_segments() {
        let mut data = vec![SCOPE_SUB_DATA, 0x00, 0x01, 0x11, 0x00];
        data.extend_from_slice(&encode_freq_bcd(14_200_000));
        data.extend_from_slice(&encode_freq_bcd(50_000));
        data.push(0x00);
        let segment = parse_scope_segment(&CivFrame {
            to: CONTROLLER_ADDR,
            from_addr: RADIO_ADDR_IC7300,
            command: SCOPE_CMD,
            data,
        })
        .unwrap();
        assert!(segment.is_division_start);
        assert_eq!(segment.center_freq_hz, Some(14_200_000));
        assert_eq!(segment.span_hz, Some(100_000));

        let bins = parse_scope_segment(&CivFrame {
            to: CONTROLLER_ADDR,
            from_addr: RADIO_ADDR_IC7300,
            command: SCOPE_CMD,
            data: vec![SCOPE_SUB_DATA, 0x00, 0x02, 0x11, 1, 2, 160],
        })
        .unwrap();
        assert_eq!(bins.bins, vec![1, 2, 160]);
    }
}
