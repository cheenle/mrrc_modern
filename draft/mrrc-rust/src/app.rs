use crate::backends::{BackendKey, RadioCapabilities};
use crate::config::RuntimeConfig;
use crate::memory::MemoryStore;
use crate::service::session::SessionRegistry;
use crate::state::RadioState;

#[derive(Debug)]
pub struct AppRuntime {
    pub config: RuntimeConfig,
    pub backend_key: BackendKey,
    pub capabilities: RadioCapabilities,
    pub radio_state: RadioState,
    pub sessions: SessionRegistry,
    pub memory: MemoryStore,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AppRuntimeError {
    UnknownBackend(String),
}

impl AppRuntime {
    pub fn from_config(config: RuntimeConfig) -> Result<Self, AppRuntimeError> {
        let backend_key = BackendKey::parse(&config.radio_model)
            .ok_or_else(|| AppRuntimeError::UnknownBackend(config.radio_model.clone()))?;
        Ok(Self {
            capabilities: backend_key.capabilities(),
            backend_key,
            config,
            radio_state: RadioState::default(),
            sessions: SessionRegistry::default(),
            memory: MemoryStore::default(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runtime_selects_backend_from_config() {
        let mut config = RuntimeConfig::from_env();
        config.radio_model = "ic7300mk2".into();
        let runtime = AppRuntime::from_config(config).unwrap();
        assert_eq!(runtime.backend_key, BackendKey::Ic7300Mk2);
        assert_eq!(runtime.capabilities.default_baud, 115_200);
    }

    #[test]
    fn runtime_rejects_unknown_backend() {
        let mut config = RuntimeConfig::from_env();
        config.radio_model = "unknown".into();
        assert_eq!(
            AppRuntime::from_config(config).unwrap_err(),
            AppRuntimeError::UnknownBackend("unknown".into())
        );
    }
}
