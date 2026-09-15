//! Request/response DTOs for the REST API.

use serde::{Deserialize, Serialize};

/// Body of `POST /v1/requests`.
#[derive(Debug, Deserialize)]
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
    /// Which registered task (see `POST /v1/tasks`) this request belongs to
    /// — selects which flavours the solver chooses among for it. Omitted or
    /// unregistered ⇒ falls back to the `"default"` task (`Config::flavours`).
    #[serde(default)]
    pub task_id: Option<String>,
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
}

/// Body of `POST /v1/tasks`: announces (or updates) a task's available
/// flavours. Clients call this once per task before submitting requests
/// that reference it via `SubmitRequestPayload::task_id`.
#[derive(Debug, Deserialize)]
pub struct RegisterTaskPayload {
    pub task_id: String,
    pub flavours: Vec<crate::engine::types::Flavour>,
    /// Overrides `Config::max_error_threshold` (%) for this task's own
    /// requests. `None` = keep using the global default — useful since
    /// different tasks' calibrated flavours can have very different error
    /// ranges (see client/scripts/push_flavours.py for how this is chosen).
    #[serde(default)]
    pub max_error_threshold: Option<f64>,
}

/// Body POSTed by this service to the executor at dispatch time, and in turn
/// (wrapped as `result`) forwarded to the original caller's `callback_url`.
#[derive(Debug, Serialize)]
pub struct ExecutorDispatchPayload {
    pub request_id: u64,
    pub scheduled_slot: i32,
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
}

/// Response of `GET /v1/stats` — counts of tracked requests by status, plus
/// the scheduler's own (task-agnostic, by design) global error average.
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

/// Response of `GET /v1/tasks/{task_id}` — the task's currently effective
/// scheduling parameters (whether registered via `POST /v1/tasks` or
/// falling back to defaults), so callers can tell what's actually enforced.
#[derive(Debug, Serialize)]
pub struct TaskConfigResponse {
    pub task_id: String,
    pub flavours: Vec<crate::engine::types::Flavour>,
    /// Always a concrete value: the task's own override if registered,
    /// otherwise `Config::max_error_threshold` (the global default).
    pub max_error_threshold: f64,
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

