use std::time::{Duration, Instant};

use crate::service::session::ClientId;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PttEvent {
    Press { client: ClientId, now: Instant },
    Release { client: ClientId },
    Disconnect { client: ClientId },
    WatchdogTick { now: Instant },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PttAction {
    None,
    KeyRadio { client: ClientId },
    ReleaseRadio { reason: ReleaseReason },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReleaseReason {
    Normal,
    DisconnectDeadMan,
    MaxTxWatchdog,
    OwnerReplaced,
}

#[derive(Debug, Clone)]
pub struct PttSafetyMachine {
    owner: Option<ClientId>,
    tx_since: Option<Instant>,
    max_tx: Option<Duration>,
}

impl PttSafetyMachine {
    pub fn new(max_tx: Option<Duration>) -> Self {
        Self {
            owner: None,
            tx_since: None,
            max_tx,
        }
    }

    pub fn owner(&self) -> Option<ClientId> {
        self.owner
    }

    pub fn is_transmitting(&self) -> bool {
        self.owner.is_some()
    }

    pub fn apply(&mut self, event: PttEvent) -> PttAction {
        match event {
            PttEvent::Press { client, now } => {
                if self.owner == Some(client) {
                    return PttAction::None;
                }
                if self.owner.is_some() {
                    self.owner = Some(client);
                    self.tx_since = Some(now);
                    return PttAction::ReleaseRadio {
                        reason: ReleaseReason::OwnerReplaced,
                    };
                }
                self.owner = Some(client);
                self.tx_since = Some(now);
                PttAction::KeyRadio { client }
            }
            PttEvent::Release { client } => {
                if self.owner != Some(client) {
                    return PttAction::None;
                }
                self.owner = None;
                self.tx_since = None;
                PttAction::ReleaseRadio {
                    reason: ReleaseReason::Normal,
                }
            }
            PttEvent::Disconnect { client } => {
                if self.owner != Some(client) {
                    return PttAction::None;
                }
                self.owner = None;
                self.tx_since = None;
                PttAction::ReleaseRadio {
                    reason: ReleaseReason::DisconnectDeadMan,
                }
            }
            PttEvent::WatchdogTick { now } => {
                let Some(max_tx) = self.max_tx else {
                    return PttAction::None;
                };
                let Some(tx_since) = self.tx_since else {
                    return PttAction::None;
                };
                if now.duration_since(tx_since) <= max_tx {
                    return PttAction::None;
                }
                self.owner = None;
                self.tx_since = None;
                PttAction::ReleaseRadio {
                    reason: ReleaseReason::MaxTxWatchdog,
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disconnect_deadman_releases_active_ptt() {
        let now = Instant::now();
        let mut ptt = PttSafetyMachine::new(Some(Duration::from_secs(60)));
        assert_eq!(
            ptt.apply(PttEvent::Press {
                client: ClientId(1),
                now
            }),
            PttAction::KeyRadio {
                client: ClientId(1)
            }
        );
        assert_eq!(
            ptt.apply(PttEvent::Disconnect {
                client: ClientId(1)
            }),
            PttAction::ReleaseRadio {
                reason: ReleaseReason::DisconnectDeadMan
            }
        );
        assert!(!ptt.is_transmitting());
    }

    #[test]
    fn max_tx_watchdog_releases_without_blocking_verify() {
        let now = Instant::now();
        let mut ptt = PttSafetyMachine::new(Some(Duration::from_secs(2)));
        ptt.apply(PttEvent::Press {
            client: ClientId(1),
            now,
        });
        assert_eq!(
            ptt.apply(PttEvent::WatchdogTick {
                now: now + Duration::from_secs(3)
            }),
            PttAction::ReleaseRadio {
                reason: ReleaseReason::MaxTxWatchdog
            }
        );
    }
}
