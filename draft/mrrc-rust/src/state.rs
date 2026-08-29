use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value};

#[derive(Debug, Clone, PartialEq)]
pub enum StateValue {
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
}

#[derive(Debug, Clone)]
pub struct RadioState {
    values: BTreeMap<String, StateValue>,
    dirty: BTreeSet<String>,
}

impl Default for RadioState {
    fn default() -> Self {
        let mut values = BTreeMap::new();
        values.insert("vfo_a_freq".into(), StateValue::Int(14_200_000));
        values.insert("vfo_b_freq".into(), StateValue::Int(7_050_000));
        values.insert("active_vfo".into(), StateValue::Text("A".into()));
        values.insert("mode".into(), StateValue::Int(1));
        values.insert("tx_status".into(), StateValue::Int(0));
        values.insert("serial_connected".into(), StateValue::Bool(false));
        values.insert("rx_audio_silent".into(), StateValue::Bool(false));
        Self {
            values,
            dirty: BTreeSet::new(),
        }
    }
}

impl RadioState {
    pub fn update(&mut self, field: impl Into<String>, value: StateValue) -> bool {
        let field = field.into();
        if self.values.get(&field) == Some(&value) {
            return false;
        }
        self.values.insert(field.clone(), value);
        self.dirty.insert(field);
        true
    }

    pub fn get(&self, field: &str) -> Option<&StateValue> {
        self.values.get(field)
    }

    pub fn dirty_snapshot(&self) -> BTreeMap<String, StateValue> {
        self.dirty
            .iter()
            .filter_map(|key| {
                self.values
                    .get(key)
                    .map(|value| (key.clone(), value.clone()))
            })
            .collect()
    }

    pub fn take_dirty(&mut self) -> BTreeSet<String> {
        std::mem::take(&mut self.dirty)
    }

    pub fn to_json_value(&self, field: &str) -> Option<Value> {
        self.values.get(field).map(state_value_to_json)
    }

    pub fn dirty_json_object(&self) -> Map<String, Value> {
        self.dirty
            .iter()
            .filter_map(|field| {
                self.to_json_value(field)
                    .map(|value| (field.clone(), value))
            })
            .collect()
    }
}

fn state_value_to_json(value: &StateValue) -> Value {
    match value {
        StateValue::Bool(v) => Value::Bool(*v),
        StateValue::Int(v) => Value::Number((*v).into()),
        StateValue::Float(v) => serde_json::Number::from_f64(*v)
            .map(Value::Number)
            .unwrap_or(Value::Null),
        StateValue::Text(v) => Value::String(v.clone()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tracks_only_changed_fields() {
        let mut state = RadioState::default();
        assert!(!state.update("tx_status", StateValue::Int(0)));
        assert!(state.update("tx_status", StateValue::Int(1)));
        assert_eq!(state.dirty_snapshot().len(), 1);
        assert!(state.take_dirty().contains("tx_status"));
        assert!(state.dirty_snapshot().is_empty());
    }

    #[test]
    fn creates_dirty_json_payload() {
        let mut state = RadioState::default();
        state.update("tx_status", StateValue::Int(1));
        let json = state.dirty_json_object();
        assert_eq!(json.get("tx_status"), Some(&Value::Number(1.into())));
    }
}
