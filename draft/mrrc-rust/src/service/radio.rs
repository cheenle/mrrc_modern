use crate::backends::ft710::{
    compressor_command, set_filter_width_command, tuner_command, Ft710Backend,
};
use crate::backends::{CommandError, PttState, RadioBackend, Vfo};
use serde_json::Value;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RadioCommandPlan {
    pub bytes: Vec<u8>,
    pub priority: bool,
    pub skip_poll_key: Option<&'static str>,
}

pub fn plan_ft710_set_command(
    backend: &Ft710Backend,
    field: &str,
    value: &Value,
    active_vfo: Vfo,
) -> Result<RadioCommandPlan, RadioCommandError> {
    match field {
        "filter" | "filter_width" => {
            let index = value
                .as_u64()
                .and_then(|n| u8::try_from(n).ok())
                .ok_or_else(|| RadioCommandError::InvalidValue(field.into()))?;
            Ok(RadioCommandPlan {
                bytes: set_filter_width_command(index).map_err(RadioCommandError::Backend)?,
                priority: false,
                skip_poll_key: Some("filter_width"),
            })
        }
        "comp" | "compressor" => {
            let enabled = value
                .as_bool()
                .ok_or_else(|| RadioCommandError::InvalidValue(field.into()))?;
            Ok(RadioCommandPlan {
                bytes: compressor_command(enabled),
                priority: false,
                skip_poll_key: Some("compressor"),
            })
        }
        "tuner" => {
            let status = value
                .as_u64()
                .and_then(|n| u8::try_from(n).ok())
                .ok_or_else(|| RadioCommandError::InvalidValue(field.into()))?;
            Ok(RadioCommandPlan {
                bytes: tuner_command(status).map_err(RadioCommandError::Backend)?,
                priority: status == 2,
                skip_poll_key: Some("tuner_status"),
            })
        }
        _ => plan_set_command(backend, field, value, active_vfo),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RadioCommandError {
    UnknownField(String),
    InvalidValue(String),
    Backend(CommandError),
}

pub fn plan_set_command<B: RadioBackend>(
    backend: &B,
    field: &str,
    value: &Value,
    active_vfo: Vfo,
) -> Result<RadioCommandPlan, RadioCommandError> {
    match field {
        "freq" => {
            let hz = value
                .as_u64()
                .ok_or_else(|| RadioCommandError::InvalidValue(field.into()))?;
            Ok(RadioCommandPlan {
                bytes: backend
                    .set_frequency_command(hz, active_vfo)
                    .map_err(RadioCommandError::Backend)?,
                priority: false,
                skip_poll_key: Some("if"),
            })
        }
        "vfo_a_freq" => plan_freq_for_vfo(backend, value, Vfo::A),
        "vfo_b_freq" => plan_freq_for_vfo(backend, value, Vfo::B),
        "mode" => {
            let mode = value
                .as_u64()
                .and_then(|n| u8::try_from(n).ok())
                .ok_or_else(|| RadioCommandError::InvalidValue(field.into()))?;
            Ok(RadioCommandPlan {
                bytes: backend
                    .set_mode_command(mode)
                    .map_err(RadioCommandError::Backend)?,
                priority: false,
                skip_poll_key: Some("if"),
            })
        }
        "ptt" => {
            let tx = value
                .as_bool()
                .ok_or_else(|| RadioCommandError::InvalidValue(field.into()))?;
            Ok(RadioCommandPlan {
                bytes: backend
                    .set_ptt_command(if tx { PttState::Tx } else { PttState::Rx })
                    .map_err(RadioCommandError::Backend)?,
                priority: true,
                skip_poll_key: Some("tx_status"),
            })
        }
        _ => Err(RadioCommandError::UnknownField(field.into())),
    }
}

fn plan_freq_for_vfo<B: RadioBackend>(
    backend: &B,
    value: &Value,
    vfo: Vfo,
) -> Result<RadioCommandPlan, RadioCommandError> {
    let hz = value
        .as_u64()
        .ok_or_else(|| RadioCommandError::InvalidValue("freq".into()))?;
    Ok(RadioCommandPlan {
        bytes: backend
            .set_frequency_command(hz, vfo)
            .map_err(RadioCommandError::Backend)?,
        priority: false,
        skip_poll_key: Some("vfo"),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backends::ft710::Ft710Backend;
    use serde_json::json;

    #[test]
    fn ptt_is_planned_as_priority_command() {
        let plan = plan_set_command(&Ft710Backend, "ptt", &json!(true), Vfo::A).unwrap();
        assert_eq!(plan.bytes, b"TX1;");
        assert!(plan.priority);
        assert_eq!(plan.skip_poll_key, Some("tx_status"));
    }

    #[test]
    fn active_vfo_frequency_uses_current_vfo() {
        let plan = plan_set_command(&Ft710Backend, "freq", &json!(7_050_000), Vfo::B).unwrap();
        assert_eq!(plan.bytes, b"FB007050000;");
        assert_eq!(plan.skip_poll_key, Some("if"));
    }

    #[test]
    fn ft710_extra_planner_preserves_errata_guards() {
        let backend = Ft710Backend;
        assert_eq!(
            plan_ft710_set_command(&backend, "filter_width", &json!(5), Vfo::A)
                .unwrap()
                .bytes,
            b"SH0005;"
        );
        assert_eq!(
            plan_ft710_set_command(&backend, "compressor", &json!(true), Vfo::A)
                .unwrap()
                .bytes,
            b"PR01;"
        );
        let tune = plan_ft710_set_command(&backend, "tuner", &json!(2), Vfo::A).unwrap();
        assert_eq!(tune.bytes, b"AC003;");
        assert!(tune.priority);
    }
}
