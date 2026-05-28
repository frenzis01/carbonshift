/// Scenario loader — deserialises a pre-generated scenario JSON file.
///
/// The JSON format is produced by the Python `scenario_io.py` helper.
/// A `Scenario` bundles the carbon-intensity forecast, the pre-generated
/// request list, and the metadata that describes the generation parameters.
/// Loading a scenario lets the Rust runtime reproduce exactly the same
/// workload as a Python benchmark run.

use std::collections::HashMap;

use serde::Deserialize;

use crate::types::Request;

// ─── ScenarioMetadata ────────────────────────────────────────────────────────

/// Parameters recorded at scenario-generation time.
///
/// Fields mirror the Python `config.py` / `scenario_io.py` variables.
/// All fields present in the JSON are deserialised; unknown keys are ignored.
#[derive(Debug, Clone, Deserialize)]
pub struct ScenarioMetadata {
    pub total_slots: i32,
    pub slot_duration_seconds: f64,
    pub requests_per_slot: f64,
    pub request_rate_std_factor: f64,
    pub deadline_min_slack: i32,
    pub deadline_max_slack: i32,
    pub max_error_threshold: f64,
    pub error_window_past: i32,
    pub error_window_future: i32,
    pub error_window_past_decay_slots: i32,
    /// Maps to `Config::prehistory_use_virtual_past`.
    pub prehistory_enabled: bool,
    /// Maps to `Config::prehistory_error_ratio_of_threshold` and
    /// `Config::forecast_error_ratio_of_threshold`.
    pub prehistory_error_ratio: f64,
    /// Maps to `Config::prehistory_mock_influence`.
    pub prehistory_mock_influence: f64,
    pub carbon_intensity_cycle_slots: i32,
    pub carbon_random_noise_amplitude: f64,
    /// RNG seed used during generation — stored in `Config::prehistory_random_seed`.
    pub seed: u64,
}

// ─── ScenarioRequest ─────────────────────────────────────────────────────────

/// A single pre-generated request as stored in the JSON.
#[derive(Debug, Clone, Deserialize)]
pub struct ScenarioRequest {
    pub request_id: u64,
    pub arrival_slot: i32,
    pub deadline_slot: i32,
    #[serde(default)]
    #[allow(dead_code)]
    pub arrival_time: f64,
}

impl ScenarioRequest {
    /// Convert to the runtime `Request` type.
    pub fn to_request(&self) -> Request {
        Request::new(self.request_id, self.arrival_slot, self.deadline_slot)
    }
}

// ─── Scenario ────────────────────────────────────────────────────────────────

/// A complete scenario: forecast + requests + generation parameters.
#[derive(Debug, Clone, Deserialize)]
pub struct Scenario {
    pub metadata: ScenarioMetadata,
    pub carbon_forecast: Vec<f64>,
    pub requests: Vec<ScenarioRequest>,
}

impl Scenario {
    /// Load a scenario from a JSON file on disk.
    pub fn from_file(path: &str) -> Result<Self, Box<dyn std::error::Error>> {
        let data = std::fs::read_to_string(path)?;
        let s: Self = serde_json::from_str(&data)?;
        Ok(s)
    }

    /// Group requests by `arrival_slot` into a `Vec<Vec<Request>>` indexed by slot.
    ///
    /// The returned vector has length `metadata.total_slots`; slots with no
    /// arrivals hold an empty `Vec`.
    pub fn requests_by_slot(&self) -> Vec<Vec<Request>> {
        let n = self.metadata.total_slots as usize;
        let mut by_slot: Vec<Vec<Request>> = vec![Vec::new(); n];
        for sr in &self.requests {
            let idx = (sr.arrival_slot as usize).min(n - 1);
            by_slot[idx].push(sr.to_request());
        }
        by_slot
    }

    /// Summary of requests-per-slot (for diagnostics).
    pub fn arrival_distribution(&self) -> HashMap<i32, usize> {
        let mut counts: HashMap<i32, usize> = HashMap::new();
        for sr in &self.requests {
            *counts.entry(sr.arrival_slot).or_insert(0) += 1;
        }
        counts
    }
}
