use std::collections::HashMap;
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PollIntervals {
    pub if_poll: Duration,
    pub vfo: Duration,
    pub tx_status: Duration,
    pub tx_meters: Duration,
    pub settings: Duration,
    pub slow: Duration,
    pub timeout: Duration,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PollTaskKind {
    IfFast,
    Vfo,
    TxStatus,
    TxMeters,
    Settings,
    SlowTelemetry,
    ConnectionWatchdog,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PollTaskPlan {
    pub kind: PollTaskKind,
    pub interval: Duration,
    pub timeout: Duration,
}

impl PollIntervals {
    pub fn task_plan(self) -> [PollTaskPlan; 7] {
        [
            PollTaskPlan {
                kind: PollTaskKind::IfFast,
                interval: self.if_poll,
                timeout: self.timeout,
            },
            PollTaskPlan {
                kind: PollTaskKind::Vfo,
                interval: self.vfo,
                timeout: self.timeout,
            },
            PollTaskPlan {
                kind: PollTaskKind::TxStatus,
                interval: self.tx_status,
                timeout: self.timeout,
            },
            PollTaskPlan {
                kind: PollTaskKind::TxMeters,
                interval: self.tx_meters,
                timeout: self.timeout,
            },
            PollTaskPlan {
                kind: PollTaskKind::Settings,
                interval: self.settings,
                timeout: self.timeout,
            },
            PollTaskPlan {
                kind: PollTaskKind::SlowTelemetry,
                interval: self.slow,
                timeout: self.timeout,
            },
            PollTaskPlan {
                kind: PollTaskKind::ConnectionWatchdog,
                interval: self.slow,
                timeout: self.timeout,
            },
        ]
    }
}

impl Default for PollIntervals {
    fn default() -> Self {
        Self {
            if_poll: Duration::from_millis(100),
            vfo: Duration::from_millis(500),
            tx_status: Duration::from_millis(500),
            tx_meters: Duration::from_millis(500),
            settings: Duration::from_secs(2),
            slow: Duration::from_secs(5),
            timeout: Duration::from_millis(250),
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct StaleReadGuard {
    skip_until: HashMap<String, Instant>,
    user_command_pause_until: Option<Instant>,
}

impl StaleReadGuard {
    pub fn note_user_command(&mut self, now: Instant, pause: Duration) {
        self.user_command_pause_until = Some(now + pause);
    }

    pub fn skip_next_poll(&mut self, field: impl Into<String>, now: Instant, duration: Duration) {
        self.skip_until.insert(field.into(), now + duration);
    }

    pub fn should_discard_after_await(&self, field: &str, now: Instant) -> bool {
        self.skip_until.get(field).is_some_and(|until| now < *until)
            || self
                .user_command_pause_until
                .is_some_and(|until| now < until)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn discards_in_flight_poll_after_user_command() {
        let mut guard = StaleReadGuard::default();
        let now = Instant::now();
        guard.skip_next_poll("filter_width", now, Duration::from_secs(2));
        assert!(guard.should_discard_after_await("filter_width", now + Duration::from_millis(150)));
        assert!(!guard.should_discard_after_await("filter_width", now + Duration::from_secs(3)));
    }

    #[test]
    fn default_poll_plan_has_seven_tasks_and_bounded_timeout() {
        let plan = PollIntervals::default().task_plan();
        assert_eq!(plan.len(), 7);
        assert_eq!(plan[0].kind, PollTaskKind::IfFast);
        assert_eq!(plan[0].interval, Duration::from_millis(100));
        assert!(plan
            .iter()
            .all(|task| task.timeout == Duration::from_millis(250)));
    }
}
