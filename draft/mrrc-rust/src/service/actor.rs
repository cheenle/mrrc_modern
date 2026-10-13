use std::time::{Duration, Instant};

use serde_json::Value;

use crate::backends::{RadioBackend, Vfo};
use crate::scheduler::StaleReadGuard;
use crate::service::radio::{plan_set_command, RadioCommandError};
use crate::transport::{SerialTransport, TransportError};

#[derive(Debug)]
pub struct RadioActor<B, T> {
    backend: B,
    transport: T,
    active_vfo: Vfo,
    stale_guard: StaleReadGuard,
    user_command_pause: Duration,
}

#[derive(Debug, PartialEq, Eq)]
pub enum ActorError {
    Command(RadioCommandError),
    Transport(TransportError),
}

impl<B: RadioBackend, T: SerialTransport> RadioActor<B, T> {
    pub fn new(backend: B, transport: T) -> Self {
        Self {
            backend,
            transport,
            active_vfo: Vfo::A,
            stale_guard: StaleReadGuard::default(),
            user_command_pause: Duration::from_millis(300),
        }
    }

    pub fn set_active_vfo(&mut self, active_vfo: Vfo) {
        self.active_vfo = active_vfo;
    }

    pub fn apply_set(
        &mut self,
        field: &str,
        value: &Value,
        now: Instant,
    ) -> Result<(), ActorError> {
        let plan = plan_set_command(&self.backend, field, value, self.active_vfo)
            .map_err(ActorError::Command)?;
        self.stale_guard
            .note_user_command(now, self.user_command_pause);
        if let Some(key) = plan.skip_poll_key {
            self.stale_guard
                .skip_next_poll(key, now, Duration::from_secs(2));
        }
        self.transport
            .write_set(&plan.bytes, plan.priority)
            .map_err(ActorError::Transport)
    }

    pub fn should_discard_poll_result(&self, field: &str, now: Instant) -> bool {
        self.stale_guard.should_discard_after_await(field, now)
    }

    pub fn into_transport(self) -> T {
        self.transport
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backends::ft710::Ft710Backend;
    use crate::transport::MockTransport;
    use serde_json::json;

    #[test]
    fn actor_writes_priority_ptt_and_marks_tx_poll_stale() {
        let now = Instant::now();
        let mut actor = RadioActor::new(Ft710Backend, MockTransport::connected());
        actor.apply_set("ptt", &json!(true), now).unwrap();
        assert!(actor.should_discard_poll_result("tx_status", now + Duration::from_millis(50)));
        let transport = actor.into_transport();
        assert_eq!(transport.writes, vec![(b"TX1;".to_vec(), true)]);
    }
}
