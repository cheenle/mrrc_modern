use std::collections::{HashMap, HashSet};
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ClientId(pub u64);

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct AuthToken(pub String);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TokenTtl(pub Duration);

#[derive(Debug, Default)]
pub struct AuthTokenStore {
    tokens: HashMap<AuthToken, Instant>,
}

impl AuthTokenStore {
    pub fn insert(&mut self, token: AuthToken, now: Instant, ttl: TokenTtl) {
        self.tokens.insert(token, now + ttl.0);
    }

    pub fn contains(&self, token: &AuthToken, now: Instant) -> bool {
        self.tokens
            .get(token)
            .is_some_and(|expires| now <= *expires)
    }

    pub fn clear(&mut self) {
        self.tokens.clear();
    }

    pub fn prune_expired(&mut self, now: Instant) {
        self.tokens.retain(|_, expires| now <= *expires);
    }
}

#[derive(Debug, Default)]
pub struct SessionRegistry {
    valid_tokens: HashSet<AuthToken>,
    ws_tokens: HashMap<ClientId, AuthToken>,
    audio_tx_clients: HashSet<ClientId>,
    tx_owner: Option<ClientId>,
}

impl SessionRegistry {
    pub fn add_token(&mut self, token: AuthToken) {
        self.valid_tokens.insert(token);
    }

    pub fn is_authorized(&self, token: &AuthToken) -> bool {
        self.valid_tokens.contains(token)
    }

    pub fn connect_ws(&mut self, client: ClientId, token: AuthToken) -> bool {
        if !self.is_authorized(&token) {
            return false;
        }
        self.ws_tokens.insert(client, token);
        true
    }

    pub fn connect_audio_tx(&mut self, client: ClientId, token: AuthToken) -> bool {
        if !self.connect_ws(client, token.clone()) {
            return false;
        }
        self.audio_tx_clients.insert(client);
        let owner_token = self.tx_owner.and_then(|owner| self.ws_tokens.get(&owner));
        if self.tx_owner.is_none() || owner_token == Some(&token) {
            self.tx_owner = Some(client);
        }
        true
    }

    pub fn claim_tx_owner_for_token(&mut self, token: &AuthToken) -> Option<ClientId> {
        let owner = self
            .audio_tx_clients
            .iter()
            .copied()
            .find(|client| self.ws_tokens.get(client) == Some(token));
        if let Some(owner) = owner {
            self.tx_owner = Some(owner);
        }
        owner
    }

    pub fn disconnect(&mut self, client: ClientId) {
        self.ws_tokens.remove(&client);
        self.audio_tx_clients.remove(&client);
        if self.tx_owner == Some(client) {
            self.tx_owner = self.audio_tx_clients.iter().next().copied();
        }
    }

    pub fn tx_owner(&self) -> Option<ClientId> {
        self.tx_owner
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ptt_client_claims_tx_audio_ownership() {
        let mut registry = SessionRegistry::default();
        let token_a = AuthToken("a".into());
        let token_b = AuthToken("b".into());
        registry.add_token(token_a.clone());
        registry.add_token(token_b.clone());
        assert!(registry.connect_audio_tx(ClientId(1), token_a));
        assert_eq!(registry.tx_owner(), Some(ClientId(1)));
        assert!(registry.connect_audio_tx(ClientId(2), token_b.clone()));
        assert_eq!(registry.tx_owner(), Some(ClientId(1)));
        assert_eq!(
            registry.claim_tx_owner_for_token(&token_b),
            Some(ClientId(2))
        );
        assert_eq!(registry.tx_owner(), Some(ClientId(2)));
    }

    #[test]
    fn auth_tokens_expire_and_clear_on_restart() {
        let now = Instant::now();
        let token = AuthToken("token".into());
        let mut store = AuthTokenStore::default();
        store.insert(token.clone(), now, TokenTtl(Duration::from_secs(30)));
        assert!(store.contains(&token, now + Duration::from_secs(1)));
        assert!(!store.contains(&token, now + Duration::from_secs(31)));
        store.clear();
        assert!(!store.contains(&token, now + Duration::from_secs(1)));
    }
}
