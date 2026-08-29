use std::time::Duration;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransportError {
    Disconnected,
    Timeout,
    Io(String),
}

pub trait SerialTransport {
    fn is_connected(&self) -> bool;
    fn write_set(&mut self, bytes: &[u8], priority: bool) -> Result<(), TransportError>;
    fn query(&mut self, bytes: &[u8], timeout: Duration) -> Result<Vec<u8>, TransportError>;
}

#[derive(Debug, Default)]
pub struct MockTransport {
    connected: bool,
    pub writes: Vec<(Vec<u8>, bool)>,
    pub queries: Vec<(Vec<u8>, Duration)>,
    pub next_query_response: Option<Vec<u8>>,
}

impl MockTransport {
    pub fn connected() -> Self {
        Self {
            connected: true,
            ..Self::default()
        }
    }
}

impl SerialTransport for MockTransport {
    fn is_connected(&self) -> bool {
        self.connected
    }

    fn write_set(&mut self, bytes: &[u8], priority: bool) -> Result<(), TransportError> {
        if !self.connected {
            return Err(TransportError::Disconnected);
        }
        self.writes.push((bytes.to_vec(), priority));
        Ok(())
    }

    fn query(&mut self, bytes: &[u8], timeout: Duration) -> Result<Vec<u8>, TransportError> {
        if !self.connected {
            return Err(TransportError::Disconnected);
        }
        self.queries.push((bytes.to_vec(), timeout));
        self.next_query_response
            .take()
            .ok_or(TransportError::Timeout)
    }
}
