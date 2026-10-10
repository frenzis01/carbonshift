//! Request/response DTOs for the REST API.

use serde::{Deserialize, Serialize};

use crate::engine::qos::{CumulativeErrorPolicy, ErrorWindowPolicy, QosProfile};
use crate::engine::types::Flavour;
use crate::types::CapacityTier;

/// Registration body for a stable, reusable QoS profile.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RegisterQosProfilePayload {
    pub profile_id: String,
    pub task_kind: String,
    pub flavours: Vec<Flavour>,
    /// Versioned metric label; descriptive metadata, never executable code.
    pub error_semantics: String,
    pub max_error_threshold: f64,
    pub error_window: ErrorWindowPolicy,
    pub cumulative_error: CumulativeErrorPolicy,
}

/// Body of the administrative global-capacity-tier replacement endpoint.
///
/// Capacity tiers intentionally live outside `QosProfile`: one update changes
/// the shared slot-pricing policy for every profile.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SetCapacityTiersPayload {
    pub capacity_tiers: Vec<CapacityTier>,
}

/// Optional profile-catalog listing behavior.
#[derive(Debug, Deserialize, Default)]
pub struct QosProfilesQuery {
    /// Include registered profiles that have not been assigned yet.
    /// Ordinary callers see only active profiles.
    #[serde(default)]
    pub include_inactive: bool,
}

/// Public profile metadata with its runtime activity state.
#[derive(Debug, Serialize, Clone)]
pub struct QosProfileResponse {
    /// Keep the profile's existing fields at the top level of the JSON
    /// response while adding activity as derived runtime metadata.
    #[serde(flatten)]
    pub profile: QosProfile,
    /// True once Carbonshift has at least one committed assignment for this ID.
    pub active: bool,
}

/// Body of `POST /v1/requests`.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SubmitRequestPayload {
    /// Seconds from now by which the job must complete. Converted internally
    /// to a deadline slot using the service's own real-time slot clock.
    pub deadline_seconds: f64,
    /// URL the service will POST the final result to once a result callback
    /// is received from the executor. Optional: if omitted, the caller must
    /// poll `GET /v1/requests/{id}` instead.
    #[serde(default)]
    pub callback_url: Option<String>,
    /// Opaque payload forwarded verbatim to the executor at dispatch time.
    #[serde(default)]
    pub payload: serde_json::Value,
    /// Stable reusable QoS budget. Omitted requests select the profile for
    /// their task kind, or the task kind's default profile.
    #[serde(default)]
    pub qos_profile_id: Option<String>,
    /// Executor operation. If omitted, `payload.task` is used when present.
    #[serde(default)]
    pub task_kind: Option<String>,

    // TODO: i32 or i64?
    #[serde(default)]
    pub arrival_slot_global: Option<i32>,
}

/// Status returned to callers, mirroring `TrackedRequest`'s lifecycle.
#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RequestStatus {
    /// Accepted, waiting for the batch solver to assign a slot.
    Pending,
    /// A slot/flavour has been assigned; waiting for the slot to arrive.
    Scheduled,
    /// Sent to the executor (or, in dry-run mode, would-have-been-sent).
    Dispatched,
    /// A callback result was received and (if configured) forwarded.
    Completed,
    /// The executor could not be reached, or reported failure.
    Failed,
}

/// Response of `POST /v1/requests` and `GET /v1/requests/{id}`.
#[derive(Debug, Serialize)]
pub struct RequestStatusResponse {
    pub request_id: u64,
    pub qos_profile_id: String,
    pub status: RequestStatus,
    pub scheduled_slot: Option<i32>,
    /// Estimated seconds from now until the assigned slot executes.
    pub eta_seconds: Option<f64>,
    pub flavour: Option<String>,
    pub carbon_cost: Option<f64>,
    /// Unix epoch seconds when the DP solver committed this assignment
    /// (`Assignment::assignment_time`); `None` while still `Pending`.
    pub scheduled_at: Option<f64>,
    /// What this request would have cost with the "no carbonshift" baseline
    /// (most-accurate flavour, executed immediately at arrival) — always
    /// present (computed at submit time), even while `carbon_cost` is still
    /// `None` because the DP solver hasn't assigned a slot yet.
    pub baseline_carbon_cost: f64,
    pub error: Option<String>,
}

/// Body of `POST /v1/callback/{request_id}`, sent by the executor.
#[derive(Debug, Deserialize)]
pub struct ExecutorCallbackPayload {
    #[serde(default)]
    pub success: bool,
    #[serde(default)]
    pub result: serde_json::Value,
    #[serde(default)]
    pub error: Option<String>,
    /// Actual execution time reported by the executor, if available.
    #[serde(default)]
    pub execution_time_seconds: Option<f64>,
    /// Actual baseline execution time, if available.
    #[serde(default)]
    pub baseline_execution_time_seconds: Option<f64>,
}

/// Body POSTed by this service to the executor at dispatch time, and in turn
/// (wrapped as `result`) forwarded to the original caller's `callback_url`.
#[derive(Debug, Serialize)]
pub struct ExecutorDispatchPayload {
    pub request_id: u64,
    pub scheduled_slot: i32,
    /// RFC 3339 UTC instant at which this assignment's slot begins.
    pub execute_at: String,
    pub flavour: String,
    pub carbon_cost: f64,
    /// Where the executor should POST its `ExecutorCallbackPayload` result.
    pub callback_url: String,
    pub payload: serde_json::Value,
}

/// Body this service forwards to the original caller's `callback_url`.
#[derive(Debug, Serialize)]
pub struct CallerCallbackPayload {
    pub request_id: u64,
    pub success: bool,
    pub result: serde_json::Value,
    pub error: Option<String>,
    /// `carbon_cost` rescaled by actual/forecast carbon intensity for the
    /// scheduled slot, if the client ever reported an actual reading for it
    /// (see `handlers::advance_slot`). `None` if no actual reading is known.
    pub actual_carbon_cost: Option<f64>,
    /// Same rescaling applied to `baseline_carbon_cost`, using the actual
    /// carbon intensity at the request's *arrival* slot instead.
    pub actual_baseline_carbon_cost: Option<f64>,
    /// Actual execution time reported by the executor, if available.
    pub execution_time_seconds: Option<f64>,
    /// Actual baseline execution time, if available.
    pub baseline_execution_time_seconds: Option<f64>,
}

/// Response of `GET /v1/stats` — counts of tracked requests by status plus
/// descriptive fleet-wide error telemetry. This aggregate is not a QoS limit:
/// hard error constraints are scoped to each request's QoS profile.
#[derive(Debug, Serialize, Default)]
pub struct StatsResponse {
    pub total: usize,
    pub pending: usize,
    pub scheduled: usize,
    pub dispatched: usize,
    pub completed: usize,
    pub failed: usize,
    /// Average `error` (%) across every assignment ever committed, updated
    /// in place by real outcomes when available (see `POST /v1/callback/{id}`).
    /// `null` if nothing has been scheduled yet.
    pub global_error_avg: Option<f64>,
    pub global_error_count: u64,
}

/// Response of `GET /v1/horizon` and (in abbreviated form) `GET /ready`.
///
/// The engine's DP solver allocates arrays sized to the whole planning
/// horizon (`Config::total_slots`) on every batch solve (see PLAN_SERVICE.md
/// Fase 6), so `total_slots` is a hard, finite ceiling rather than a true
/// rolling window. This endpoint lets an orchestrator (k8s, docker-compose)
/// detect the horizon running out and roll a replacement instance before it
/// does, using `GET /ready` as the readiness probe.
#[derive(Debug, Serialize)]
pub struct HorizonResponse {
    pub current_slot: i32,
    pub total_slots: i32,
    pub used_fraction: f64,
    /// `used_fraction >= ServiceConfig::horizon_ready_threshold`.
    pub near_exhaustion: bool,
}

// ─── Fine-grained monitoring DTOs ───────────────────────────────────────────

/// Query parameters for assignment listing and error-history endpoints.
#[derive(Debug, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct AssignmentsQuery {
    #[serde(default)]
    pub from_slot: Option<i32>,
    #[serde(default)]
    pub to_slot: Option<i32>,
    #[serde(default)]
    pub flavour: Option<String>,
    /// Filter by stable QoS budget ID.
    #[serde(default)]
    pub qos_profile_id: Option<String>,
}

/// DTO for a single assignment returned in `GET /v1/assignments`.
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct AssignmentItem {
    pub request_id: u64,
    pub qos_profile_id: String,
    pub scheduled_slot: i32,
    pub flavour_name: String,
    pub carbon_cost: f64,
    pub error: f64,
    pub flavour_duration: i32,
    pub arrival_slot: Option<i32>,
    pub deadline_slot: Option<i32>,
    pub assignment_time: f64,
}

/// Detailed slot status for `GET /v1/slots/{slot}`.
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct SlotDetailResponse {
    pub slot: i32,
    pub total_requests: usize,
    pub flavour_counts: std::collections::HashMap<String, usize>,
    pub total_carbon_cost: f64,
    pub capacity_multiplier: f64,
    pub forecast_carbon_intensity: Option<f64>,
    pub actual_carbon_intensity: Option<f64>,
}

/// Fine-grained cost breakdown for `GET /v1/metrics/costs`.
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct CostMetricsResponse {
    /// `None` means totals cover every QoS profile; otherwise all request
    /// totals are restricted to this profile. Capacity tiers remain global.
    pub qos_profile_id: Option<String>,
    pub current_actual_carbon_cost: f64,
    pub current_actual_baseline_carbon_cost: f64,
    pub actual_carbon_saving_pct: Option<f64>,
    pub forecasted_pending_carbon_cost: f64,
    pub total_forecasted_carbon_cost: f64,
    pub total_baseline_carbon_cost: f64,
    /// Shared slot-pricing policy. It is intentionally not profile-scoped.
    pub capacity_tiers: Vec<crate::types::CapacityTier>,
}

/// Slot error item for `GET /v1/metrics/error-history`.
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct SlotErrorItem {
    pub slot: i32,
    pub request_count: usize,
    pub average_error: f64,
    #[serde(default)]
    pub window_error: Option<f64>,
    #[serde(default)]
    pub cumulative_error: Option<f64>,
}

/// Error history response for `GET /v1/metrics/error-history`.
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct ErrorHistoryResponse {
    pub current_slot: i32,
    /// Selected budget identity; `None` means this is descriptive fleet telemetry.
    #[serde(default)]
    pub qos_profile_id: Option<String>,
    /// Cumulative error for the selected QoS budget, if one was selected.
    #[serde(default)]
    pub profile_error_avg: Option<f64>,
    #[serde(default)]
    pub error_semantics: Option<String>,
    /// Fleet-wide descriptive average. Do not compare it against a profile QoS threshold.
    pub global_error_avg: Option<f64>,
    /// Legacy profile/task threshold lookup by stable profile ID.
    pub max_error_threshold: f64,
    pub task_thresholds: std::collections::HashMap<String, f64>,
    #[serde(default)]
    pub window_past: i32,
    #[serde(default)]
    pub window_future: i32,
    pub slots: Vec<SlotErrorItem>,
}
