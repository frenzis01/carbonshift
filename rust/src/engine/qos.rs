//! Stable QoS identities and immutable policy snapshots carried with work.

use serde::Serialize;

use crate::types::Flavour;

/// Built-in task kinds and their stable executor error-metric identifiers.
///
/// This data table seeds per-kind defaults; the scheduler does not branch on
/// the task-kind values, and client-defined task kinds can register profiles.
pub const DEFAULT_TASK_ERROR_SEMANTICS: &[(&str, &str)] = &[
    ("text_generation", "relative-confidence-degradation-v1"),
    ("ner", "entity-set-f1-v1"),
    ("question_answering", "word-overlap-f1-v1"),
];

/// Stable, caller-chosen ID for a reusable QoS budget.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct QosProfileId(String);

impl QosProfileId {
    pub fn parse(value: impl Into<String>) -> Result<Self, String> {
        let value = value.into();
        if !is_valid_stable_identifier(&value) {
            return Err(
                "profile_id must be 1-64 lowercase ASCII letters, digits, '.', '_' or '-', \
                 and start with a letter or digit"
                    .to_string(),
            );
        }
        Ok(Self(value))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Built-in profile used by unprofiled offline/simulation requests.
    pub fn default_profile() -> Self {
        Self("default-text-generation".to_string())
    }
}

impl std::fmt::Display for QosProfileId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// Open identifier for an executor operation, distinct from a QoS profile ID.
///
/// The scheduler compares task-kind IDs but does not branch on their values;
/// execution support is owned by the executor's task-handler registry.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct TaskKindId(String);

impl TaskKindId {
    pub fn parse(value: impl Into<String>) -> Result<Self, String> {
        let value = value.into();
        if !is_valid_stable_identifier(&value) {
            return Err(
                "task_kind must be 1-64 lowercase ASCII letters, digits, '.', '_' or '-', \
                 and start with a letter or digit"
                    .to_string(),
            );
        }
        Ok(Self(value))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    pub fn default_error_semantics(&self) -> Option<&'static str> {
        DEFAULT_TASK_ERROR_SEMANTICS
            .iter()
            .find_map(|(kind, semantics)| (*kind == self.0).then_some(*semantics))
    }
}

impl std::fmt::Display for TaskKindId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

fn is_valid_stable_identifier(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() <= 64
        && bytes
            .first()
            .is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
        && bytes.iter().all(|byte| {
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || matches!(*byte, b'-' | b'_' | b'.')
        })
}

/// Profile-specific sliding-window shape; capacity tiers remain global.
#[derive(Debug, Clone, serde::Deserialize, serde::Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ErrorWindowPolicy {
    pub past_slots: i32,
    pub future_slots: i32,
    pub past_decay_slots: i32,
}

/// Optional per-profile cumulative constraint, separate from its sliding window.
#[derive(Debug, Clone, serde::Deserialize, serde::Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CumulativeErrorPolicy {
    pub enabled: bool,
    pub hard: bool,
}

/// Immutable scheduling and error-budget policy shared by compatible requests.
#[derive(Debug, Clone, serde::Serialize, PartialEq)]
pub struct QosProfile {
    pub profile_id: QosProfileId,
    pub task_kind: TaskKindId,
    pub flavours: Vec<Flavour>,
    /// Versioned metric label; descriptive metadata, never executable code.
    pub error_semantics: String,
    pub max_error_threshold: f64,
    pub error_window: ErrorWindowPolicy,
    pub cumulative_error: CumulativeErrorPolicy,
}

impl QosProfile {
    pub fn validate(&self) -> Result<(), String> {
        if self.flavours.is_empty() {
            return Err("flavours must not be empty".to_string());
        }
        let mut flavour_names = std::collections::HashSet::new();
        for flavour in &self.flavours {
            if flavour.name.trim().is_empty() {
                return Err("flavour names must not be empty".to_string());
            }
            if !flavour_names.insert(flavour.name.to_ascii_lowercase()) {
                return Err(format!("duplicate flavour name: {}", flavour.name));
            }
            if !flavour.error.is_finite() || !(0.0..=100.0).contains(&flavour.error) {
                return Err(format!(
                    "flavour {} error must be finite and between 0 and 100",
                    flavour.name
                ));
            }
            if flavour.duration <= 0 {
                return Err(format!(
                    "flavour {} duration must be positive",
                    flavour.name
                ));
            }
        }
        if !is_valid_stable_identifier(&self.error_semantics) {
            return Err(
                "error_semantics must be a stable lowercase identifier such as \
                 word-overlap-f1-v1"
                    .to_string(),
            );
        }
        if !self.max_error_threshold.is_finite()
            || !(0.0..=100.0).contains(&self.max_error_threshold)
        {
            return Err("max_error_threshold must be finite and between 0 and 100".to_string());
        }
        if self.error_window.past_slots < 0
            || self.error_window.future_slots < 0
            || self.error_window.past_decay_slots < 0
        {
            return Err("error-window slot counts must not be negative".to_string());
        }
        Ok(())
    }
}
