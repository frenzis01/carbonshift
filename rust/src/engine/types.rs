/// Core types for the CarbonShift scheduler.
///
/// These mirror the Python dataclasses in `shared_state.py` and the flavour /
/// capacity-tier dicts in `config.py`.

use serde::{Deserialize, Serialize};
use std::time::{SystemTime, UNIX_EPOCH};

// ─── helpers ─────────────────────────────────────────────────────────────────

fn unix_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

// ─── Request ─────────────────────────────────────────────────────────────────

/// A scheduling request arriving at `arrival_slot` with a deadline.
#[derive(Debug, Clone)]
pub struct Request {
    pub id: u64,
    pub arrival_slot: i32,
    pub deadline_slot: i32,
    /// Wall-clock arrival time (seconds since UNIX epoch).
    pub arrival_time: f64,
    /// Identifies which task this request belongs to (e.g. "text_generation"),
    /// i.e. which entry of the dynamic task registry `flavours` was resolved
    /// from. `"default"` for requests with no task-specific flavours (CLI/
    /// simulation tools, or callers that never registered a task).
    pub task_id: String,
    /// Flavours available for this request's task, resolved once at intake
    /// time. Empty means "no task-specific override" — the solver falls
    /// back to `Config::flavours` (the predefined default task).
    pub flavours: Vec<Flavour>,
    /// Overrides `Config::max_error_threshold` (%) for this request's local/
    /// window feasibility check, if its task registered one (see
    /// `service::handlers::register_task`). `None` = use the global default.
    pub max_error_threshold: Option<f64>,
    /// Capacity tiers available for this request's task, resolved once at intake
    /// time. `None` means "no task-specific override" — the solver falls
    /// back to `Config::capacity_tiers` (the predefined default task).
    pub capacity_tiers: Option<Vec<CapacityTier>>,
}

impl Request {
    pub fn new(id: u64, arrival_slot: i32, deadline_slot: i32) -> Self {
        Self {
            id,
            arrival_slot,
            deadline_slot,
            arrival_time: unix_now(),
            task_id: "default".to_string(),
            flavours: Vec::new(),
            max_error_threshold: None,
            capacity_tiers: None,
        }
    }

    /// Builds a request tied to a specific (dynamically-registered) task,
    /// carrying the flavours (and optional error-threshold override) the
    /// solver must use for it.
    pub fn new_for_task(
        id: u64,
        arrival_slot: i32,
        deadline_slot: i32,
        task_id: String,
        flavours: Vec<Flavour>,
        max_error_threshold: Option<f64>,
        capacity_tiers: Option<Vec<CapacityTier>>,
    ) -> Self {
        Self {
            id,
            arrival_slot,
            deadline_slot,
            arrival_time: unix_now(),
            task_id,
            flavours,
            max_error_threshold,
            capacity_tiers,
        }
    }
}

// ─── Assignment ──────────────────────────────────────────────────────────────

/// A scheduling decision: request `request_id` assigned to `scheduled_slot`
/// with a given flavour.
#[derive(Debug, Clone)]
pub struct Assignment {
    pub request_id: u64,
    pub scheduled_slot: i32,
    pub flavour_name: String,
    pub carbon_cost: f64,
    pub error: f64,
    /// Duration in seconds of the assigned flavour (integer, as in FLAVOURS).
    pub flavour_duration: i32,
    pub arrival_slot: Option<i32>,
    pub deadline_slot: Option<i32>,
    pub assignment_time: f64,
}

impl Assignment {
    pub fn new(
        request_id: u64,
        scheduled_slot: i32,
        flavour_name: String,
        carbon_cost: f64,
        error: f64,
        flavour_duration: i32,
        arrival_slot: Option<i32>,
        deadline_slot: Option<i32>,
    ) -> Self {
        Self {
            request_id,
            scheduled_slot,
            flavour_name,
            carbon_cost,
            error,
            flavour_duration,
            arrival_slot,
            deadline_slot,
            assignment_time: unix_now(),
        }
    }
}

// ─── RequestAssignment (DP internal result) ───────────────────────────────────

/// Lightweight result of a single DP assignment step.
/// Converted to `Assignment` after the solver completes.
#[derive(Debug, Clone)]
pub struct RequestAssignment {
    pub request_id: u64,
    pub flavour_name: String,
    pub slot: i32,
    pub carbon_cost: f64,
    pub error: f64,
}

// ─── Flavour ─────────────────────────────────────────────────────────────────

/// Execution flavour definition.
///
/// `duration` is in seconds (integer) and is used as a relative cost weight
/// in the DP.  Carbon cost is reported in gCO₂ by the scale factor in Config.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Flavour {
    pub name: String,
    /// Approximation error introduced by this flavour (%).
    pub error: f64,
    /// Processing duration in seconds.
    pub duration: i32,
}

// ─── CapacityTier ────────────────────────────────────────────────────────────

/// A single tier in the step-function capacity/rebound multiplier.
///
/// A slot with `count` requests uses the multiplier of the first tier whose
/// `max_requests >= count`.  A tier with `max_requests = null` (JSON) / `None`
/// (Rust) is the overflow tier and matches all counts above the previous tier.
/// The implicit baseline multiplier 1.0 applies for counts up to the first tier.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct CapacityTier {
    pub max_requests: Option<i64>,
    pub multiplier: f64,
}

/// Look up the capacity/rebound multiplier for a slot holding `count`
/// requests (1-indexed position of the request being priced).
///
/// Tiers are checked in order; the first tier whose `max_requests >= count`
/// wins. A tier with `max_requests = None` is the overflow catch-all and
/// always matches. Falls back to the last tier's multiplier (or `1.0` if
/// `tiers` is empty) if no tier matches — this should not happen with a
/// well-formed tier list that ends in an overflow tier.
pub fn get_capacity_multiplier(tiers: &[CapacityTier], count: i64) -> f64 {
    for tier in tiers {
        match tier.max_requests {
            None => return tier.multiplier,
            Some(max) if count <= max => return tier.multiplier,
            _ => {}
        }
    }
    tiers.last().map(|t| t.multiplier).unwrap_or(1.0)
}
