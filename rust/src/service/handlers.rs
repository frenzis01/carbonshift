//! HTTP handlers: job submission, status polling, executor callback, health.

use std::net::IpAddr;
use std::time::Duration;

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::Json;
use serde::Deserialize;

use crate::engine::types::{get_capacity_multiplier, Flavour, Request as EngineRequest};
use crate::service::models::{
    CallerCallbackPayload, ExecutorCallbackPayload, HorizonResponse, RegisterTaskPayload,
    RequestStatus, RequestStatusResponse, StatsResponse, SubmitRequestPayload, TaskConfigResponse,
};
use crate::service::state::{AppState, TrackedRequest};

type ApiError = (StatusCode, Json<serde_json::Value>);

#[derive(Debug, Deserialize)]
pub struct AdvanceSlotQuery {
    #[serde(default)]
    pub actual_carbon_intensity: Option<f64>,
}

fn api_error(status: StatusCode, msg: impl Into<String>) -> ApiError {
    (status, Json(serde_json::json!({ "error": msg.into() })))
}

/// Rejects obviously unsafe callback URLs (SSRF guard).
///
/// This is a best-effort check, not a full SSRF defence: it rejects non-
/// http(s) schemes and literal loopback/private/link-local IPs. Hostnames
/// are allowed through as-is (blocking them would make the feature useless
/// for realistic deployments); a production deployment should additionally
/// restrict callback URLs to an operator-configured allowlist of trusted
/// domains, since a hostname can still resolve to an internal address.
fn validate_callback_url(raw: &str, allow_private: bool) -> Result<(), String> {
    let url = reqwest::Url::parse(raw).map_err(|e| format!("invalid callback_url: {e}"))?;
    if url.scheme() != "http" && url.scheme() != "https" {
        return Err("callback_url must use http or https".to_string());
    }
    if allow_private {
        return Ok(());
    }
    // `Url::host()` (unlike `host_str()`) gives IPv6 addresses back parsed
    // and unbracketed, so `[::1]` is correctly recognised as loopback.
    if let Some(host) = url.host() {
        let ip: Option<IpAddr> = match host {
            url::Host::Ipv4(v4) => Some(IpAddr::V4(v4)),
            url::Host::Ipv6(v6) => Some(IpAddr::V6(v6)),
            url::Host::Domain(_) => None,
        };
        if let Some(ip) = ip {
            let is_private = match ip {
                IpAddr::V4(v4) => {
                    v4.is_loopback() || v4.is_private() || v4.is_link_local() || v4.is_unspecified()
                }
                IpAddr::V6(v6) => v6.is_loopback() || v6.is_unspecified(),
            };
            if is_private {
                return Err(
                    "callback_url points to a loopback/private address; set \
                     CARBONSHIFT_ALLOW_PRIVATE_CALLBACKS=1 to allow this for local testing"
                        .to_string(),
                );
            }
        }
    }
    Ok(())
}

pub async fn health() -> &'static str {
    "ok"
}

/// What `arrival_slot` would have cost under the "no carbonshift" baseline:
/// the most-accurate (lowest-error) flavour, executed immediately at
/// arrival with no batching/carbon optimisation. `position` (1-indexed,
/// tracked per arrival slot) mirrors the same capacity-tier repricing an
/// immediate execution would face, so this is comparable to the DP
/// solver's own per-request cost model.
///
/// `flavours` is the request's own resolved flavour set (its task's, or the
/// default task's) — the same set the DP solver chooses among for it, so
/// the baseline and the real assignment are always comparable apples-to-apples.
fn compute_baseline_carbon_cost(state: &AppState, arrival_slot: i32, flavours: &[Flavour]) -> f64 {
    let position = {
        let mut counts = state.baseline_slot_counts.lock().unwrap();
        let c = counts.entry(arrival_slot).or_insert(0);
        *c += 1;
        *c
    };
    let carbon = state.carbon_forecast.get(arrival_slot as usize).copied().unwrap_or(0.0);
    let mult = get_capacity_multiplier(&state.cfg.capacity_tiers, position);
    let accurate = flavours
        .iter()
        .min_by(|a, b| a.error.partial_cmp(&b.error).unwrap())
        .expect("task must have at least one flavour");
    carbon * mult * accurate.duration as f64 * state.cfg.carbon_cost_duration_scale
}

/// `POST /v1/tasks` — announce (or update) a task's available flavours.
/// Clients call this once per task, before submitting requests that
/// reference it via `SubmitRequestPayload::task_id`. Overwrites any
/// previous registration for the same `task_id`; the built-in `"default"`
/// task (seeded from `Config::flavours`) can be overwritten too.
pub async fn register_task(
    State(state): State<AppState>,
    Json(body): Json<RegisterTaskPayload>,
) -> Result<StatusCode, ApiError> {
    if body.flavours.is_empty() {
        return Err(api_error(StatusCode::BAD_REQUEST, "flavours must not be empty"));
    }
    state.task_flavours.lock().unwrap().insert(
        body.task_id,
        crate::service::state::TaskConfig { flavours: body.flavours, max_error_threshold: body.max_error_threshold },
    );
    Ok(StatusCode::NO_CONTENT)
}

fn compute_horizon(state: &AppState) -> HorizonResponse {
    let current_slot = state.shared_state.get_current_slot();
    let total_slots = state.cfg.total_slots.max(1);
    let used_fraction = (current_slot as f64 / total_slots as f64).clamp(0.0, 1.0);
    HorizonResponse {
        current_slot,
        total_slots,
        used_fraction,
        near_exhaustion: used_fraction >= state.service_cfg.horizon_ready_threshold,
    }
}

/// `GET /v1/horizon` — how much of the finite planning horizon has elapsed.
pub async fn horizon(State(state): State<AppState>) -> Json<HorizonResponse> {
    Json(compute_horizon(&state))
}

/// `GET /ready` — readiness probe: `503` once the horizon is nearly
/// exhausted, so an orchestrator stops routing new traffic here and can
/// roll a replacement instance (see PLAN_SERVICE.md Fase 6).
pub async fn ready(State(state): State<AppState>) -> (StatusCode, Json<HorizonResponse>) {
    let h = compute_horizon(&state);
    let status = if h.near_exhaustion { StatusCode::SERVICE_UNAVAILABLE } else { StatusCode::OK };
    (status, Json(h))
}

/// `POST /v1/admin/advance-slot` — test-only: force the virtual clock to
/// the next slot boundary (`Config::manual_clock` must be enabled), then
/// block until every assignment produced for the slot just left has been
/// handed off to the executor (status `Dispatched`, not just `Scheduled`).
/// See PLAN_SERVICE.md "Emulazione a tempo fittizio" for the full protocol
/// this enables together with the executor's own `/admin/advance-slot`.
///
/// `?actual_carbon_intensity=<f64>` (optional): the client's real (not
/// forecast) carbon intensity reading for the slot being advanced into —
/// piggybacked here since the client already calls this once per slot, to
/// avoid a separate synchronization channel (see PLAN_SERVICE.md). Used to
/// correct already-committed `carbon_cost` once a request in that slot
/// completes (see `executor_callback`); never fed back into live scheduling.
pub async fn advance_slot(
    State(state): State<AppState>,
    Query(query): Query<AdvanceSlotQuery>,
) -> Result<Json<serde_json::Value>, ApiError> {
    if !state.cfg.manual_clock {
        return Err(api_error(StatusCode::CONFLICT, "MANUAL_CLOCK is not enabled on this instance"));
    }
    let new_slot = crate::engine::scheduler::advance_to_next_slot(&state.shared_state, &state.cfg);
    if let Some(ci) = query.actual_carbon_intensity {
        state.actual_carbon_intensity.lock().unwrap().insert(new_slot, ci);
    }

    let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
    loop {
        let still_dispatching = state
            .tracked
            .lock()
            .unwrap()
            .values()
            .any(|t| matches!(t.status, RequestStatus::Pending | RequestStatus::Scheduled));
        if !still_dispatching || tokio::time::Instant::now() >= deadline {
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }

    Ok(Json(serde_json::json!({ "current_slot": new_slot })))
}

/// `GET /v1/carbon-forecast` — the forecast the DP solver is scheduling
/// against (index = slot). Read-only: lets the client derive a plausible
/// "actual" carbon intensity series (e.g. the forecast with small jitter)
/// to report back via `POST /v1/admin/advance-slot?actual_carbon_intensity=`.
pub async fn carbon_forecast(State(state): State<AppState>) -> Json<serde_json::Value> {
    Json(serde_json::json!({ "forecast": *state.carbon_forecast }))
}

/// `GET /v1/stats` — counts of tracked requests by lifecycle status.
pub async fn stats(State(state): State<AppState>) -> Json<StatsResponse> {
    let guard = state.tracked.lock().unwrap();
    let mut s = StatsResponse { total: guard.len(), ..Default::default() };
    for t in guard.values() {
        match t.status {
            RequestStatus::Pending => s.pending += 1,
            RequestStatus::Scheduled => s.scheduled += 1,
            RequestStatus::Dispatched => s.dispatched += 1,
            RequestStatus::Completed => s.completed += 1,
            RequestStatus::Failed => s.failed += 1,
        }
    }
    drop(guard);
    let g = state.shared_state.get_global_error_stats();
    s.global_error_count = g.count;
    s.global_error_avg = if g.count > 0 { Some(g.avg) } else { None };
    Json(s)
}

/// `GET /v1/tasks/{task_id}` — the task's currently effective flavours and
/// error threshold (registered via `POST /v1/tasks`, or the built-in
/// defaults if it was never announced).
pub async fn get_task_config(
    State(state): State<AppState>,
    Path(task_id): Path<String>,
) -> Json<TaskConfigResponse> {
    Json(TaskConfigResponse {
        flavours: state.flavours_for_task(&task_id),
        max_error_threshold: state.threshold_for_task(&task_id).unwrap_or(state.cfg.max_error_threshold),
        task_id,
    })
}

/// `POST /v1/requests` — submit a job, get back the assigned slot.
///
/// Adds the request to the engine's pending queue and waits (polling, with a
/// short async sleep between checks) up to `submit_wait_timeout_secs` for the
/// batch solver to commit an assignment. If the solver hasn't produced one
/// yet (e.g. still waiting for a full batch), responds `202 Accepted` with
/// `status: pending`; the caller can poll `GET /v1/requests/{id}` or wait
/// for the callback once the slot is eventually dispatched.
pub async fn submit_request(
    State(state): State<AppState>,
    Json(body): Json<SubmitRequestPayload>,
) -> Result<(StatusCode, Json<RequestStatusResponse>), ApiError> {
    if body.deadline_seconds < 0.0 {
        return Err(api_error(StatusCode::BAD_REQUEST, "deadline_seconds must be >= 0"));
    }
    if let Some(cb) = &body.callback_url {
        validate_callback_url(cb, state.service_cfg.allow_private_callbacks)
            .map_err(|e| api_error(StatusCode::BAD_REQUEST, e))?;
    }

    let request_id = state.next_request_id();
    let current_slot = state.shared_state.get_current_slot();
    let eff_slot_dur = state.cfg.effective_slot_duration_secs();
    let slots_ahead = ((body.deadline_seconds / eff_slot_dur).ceil() as i32).max(1);
    let deadline_slot = (current_slot + slots_ahead).min(state.cfg.total_slots - 1);
    let task_id = body.task_id.clone().unwrap_or_else(|| "default".to_string());
    let task_flavours = state.flavours_for_task(&task_id);
    let task_threshold = state.threshold_for_task(&task_id);
    let baseline_carbon_cost = compute_baseline_carbon_cost(&state, current_slot, &task_flavours);

    state.tracked.lock().unwrap().insert(
        request_id,
        TrackedRequest::new(body.callback_url.clone(), body.payload.clone(), baseline_carbon_cost, current_slot),
    );

    state.shared_state.add_request(EngineRequest::new_for_task(
        request_id,
        current_slot,
        deadline_slot,
        task_id,
        task_flavours,
        task_threshold,
    ));

    // Poll for the solver's assignment; short sleeps so we don't block the
    // async runtime while waiting for the (std-threaded) engine to catch up.
    let deadline = tokio::time::Instant::now()
        + Duration::from_secs_f64(state.service_cfg.submit_wait_timeout_secs);
    loop {
        if let Some(assignment) = state.shared_state.get_current_assignments().get(&request_id) {
            let eta = ((assignment.scheduled_slot - state.shared_state.get_current_slot()).max(0)) as f64
                * eff_slot_dur;
            if let Some(t) = state.tracked.lock().unwrap().get_mut(&request_id) {
                t.status = RequestStatus::Scheduled;
            }
            return Ok((
                StatusCode::OK,
                Json(RequestStatusResponse {
                    request_id,
                    status: RequestStatus::Scheduled,
                    scheduled_slot: Some(assignment.scheduled_slot),
                    eta_seconds: Some(eta),
                    flavour: Some(assignment.flavour_name.clone()),
                    carbon_cost: Some(assignment.carbon_cost),
                    scheduled_at: Some(assignment.assignment_time),
                    baseline_carbon_cost,
                    error: None,
                }),
            ));
        }
        if tokio::time::Instant::now() >= deadline {
            return Ok((
                StatusCode::ACCEPTED,
                Json(RequestStatusResponse {
                    request_id,
                    status: RequestStatus::Pending,
                    scheduled_slot: None,
                    eta_seconds: None,
                    flavour: None,
                    carbon_cost: None,
                    scheduled_at: None,
                    baseline_carbon_cost,
                    error: None,
                }),
            ));
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

/// `GET /v1/requests/{id}` — poll current status of a previously submitted job.
pub async fn get_request_status(
    State(state): State<AppState>,
    Path(request_id): Path<u64>,
) -> Result<Json<RequestStatusResponse>, ApiError> {
    let tracked_status = {
        let guard = state.tracked.lock().unwrap();
        let t = guard
            .get(&request_id)
            .ok_or_else(|| api_error(StatusCode::NOT_FOUND, "unknown request_id"))?;
        (t.status, t.error.clone(), t.baseline_carbon_cost)
    };
    let (status, error, baseline_carbon_cost) = tracked_status;

    let assignments = state.shared_state.get_current_assignments();
    let assignment = assignments.get(&request_id);
    let eff_slot_dur = state.cfg.effective_slot_duration_secs();
    let eta = assignment.map(|a| {
        ((a.scheduled_slot - state.shared_state.get_current_slot()).max(0)) as f64 * eff_slot_dur
    });

    Ok(Json(RequestStatusResponse {
        request_id,
        status,
        scheduled_slot: assignment.map(|a| a.scheduled_slot),
        eta_seconds: eta,
        flavour: assignment.map(|a| a.flavour_name.clone()),
        carbon_cost: assignment.map(|a| a.carbon_cost),
        scheduled_at: assignment.map(|a| a.assignment_time),
        baseline_carbon_cost,
        error,
    }))
}

/// `POST /v1/callback/{id}` — result callback from the executor.
///
/// Marks the request completed/failed and forwards the result to the
/// original caller's `callback_url` (if any) as a fire-and-forget task, so
/// a slow/unreachable caller endpoint never blocks the executor's request.
///
/// Also corrects the scheduler's global/window error average with the
/// *real* error measured for this request, if the executor reported a
/// `result.actual_error_pct` (task-appropriate — e.g. word-overlap F1 based
/// for QA/NER, confidence-degradation based for open-ended text_generation;
/// see executor/app/inference.py's `run_task` docstring for why a single
/// formula doesn't fit every task): the DP solver only ever knows the
/// flavour's *predicted* error at scheduling time, so this is the one place
/// real outcomes feed back into it. This only ever runs for real/emulated
/// executions (this endpoint is never hit by offline simulation, which has
/// no executor to call back).
///
/// Also corrects `carbon_cost`/`baseline_carbon_cost` with the *actual*
/// carbon intensity for their slots, if the client ever reported one (see
/// `advance_slot`) — rescaled by `actual_ci / forecast_ci`, since execution
/// time/capacity multiplier are unaffected by carbon intensity. Both
/// corrected values are forwarded to the caller so it can see the real cost,
/// not just the DP's forecast-time estimate.
pub async fn executor_callback(
    State(state): State<AppState>,
    Path(request_id): Path<u64>,
    Json(body): Json<ExecutorCallbackPayload>,
) -> Result<StatusCode, ApiError> {
    if let Some(actual_error) = body.result.get("actual_error_pct").and_then(|v| v.as_f64()) {
        state.shared_state.correct_assignment_error(request_id, actual_error);
    }

    let mut actual_carbon_cost = None;
    if let Some(assignment) = state.shared_state.get_current_assignments().get(&request_id) {
        if let Some(ratio) = state.carbon_intensity_ratio(assignment.scheduled_slot) {
            actual_carbon_cost = state
                .shared_state
                .correct_assignment_carbon_cost(request_id, assignment.carbon_cost * ratio);
        }
    }

    let (callback_url, actual_baseline_carbon_cost) = {
        let mut guard = state.tracked.lock().unwrap();
        let t = guard
            .get_mut(&request_id)
            .ok_or_else(|| api_error(StatusCode::NOT_FOUND, "unknown request_id"))?;
        t.status = if body.success { RequestStatus::Completed } else { RequestStatus::Failed };
        t.error = body.error.clone();
        let actual_baseline = state.carbon_intensity_ratio(t.arrival_slot).map(|ratio| {
            t.baseline_carbon_cost *= ratio;
            t.baseline_carbon_cost
        });
        (t.callback_url.clone(), actual_baseline)
    };

    if let Some(url) = callback_url {
        let http = state.http.clone();
        let payload = CallerCallbackPayload {
            request_id,
            success: body.success,
            result: body.result,
            error: body.error,
            actual_carbon_cost,
            actual_baseline_carbon_cost,
        };
        tokio::spawn(async move {
            if let Err(e) = http.post(&url).json(&payload).send().await {
                tracing::warn!(request_id, %url, error = %e, "failed to forward callback to caller");
            }
        });
    }

    Ok(StatusCode::OK)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_non_http_scheme() {
        assert!(validate_callback_url("ftp://example.com/cb", false).is_err());
    }

    #[test]
    fn rejects_loopback_by_default() {
        assert!(validate_callback_url("http://127.0.0.1:9000/cb", false).is_err());
        assert!(validate_callback_url("http://[::1]:9000/cb", false).is_err());
    }

    #[test]
    fn rejects_private_ip_by_default() {
        assert!(validate_callback_url("http://10.0.0.5:9000/cb", false).is_err());
        assert!(validate_callback_url("http://192.168.1.5:9000/cb", false).is_err());
    }

    #[test]
    fn allows_private_ip_when_flag_set() {
        assert!(validate_callback_url("http://127.0.0.1:9000/cb", true).is_ok());
    }

    #[test]
    fn allows_public_hostname() {
        assert!(validate_callback_url("https://example.com/cb", false).is_ok());
    }

    #[test]
    fn baseline_carbon_cost_uses_lowest_error_flavour() {
        use crate::engine::config::Config;
        use crate::engine::scheduler::generate_carbon_forecast;
        use crate::engine::shared_state::SharedState;
        use crate::engine::types::Flavour;
        use crate::service::state::ServiceConfig;
        use std::sync::Arc;

        let mut cfg = Config::default();
        // Whichever flavour has the lowest `error` is the baseline,
        // regardless of its name — mirrors the DP/online solvers' own
        // "reference flavour" lookups (see `Config::flavours` doc).
        cfg.flavours = vec![
            Flavour { name: "Cheap".to_string(), error: 5.0, duration: 60 },
            Flavour { name: "Precise".to_string(), error: 0.0, duration: 10 },
        ];
        let cfg = Arc::new(cfg);
        let forecast = Arc::new(generate_carbon_forecast(&cfg));
        let service_cfg = ServiceConfig {
            executor_url: None,
            self_base_url: "http://localhost:0".to_string(),
            submit_wait_timeout_secs: 0.2,
            allow_private_callbacks: false,
            api_key: None,
            executor_token: None,
            executor_max_retries: 3,
            executor_retry_base_ms: 10,
            executor_retry_max_ms: 100,
            horizon_ready_threshold: 0.9,
            dispatcher_poll_interval_ms: 20,
        };
        let state = AppState::new(SharedState::new(), cfg.clone(), service_cfg, forecast);

        let baseline = compute_baseline_carbon_cost(&state, 0, &cfg.flavours);
        let carbon = state.carbon_forecast[0];
        let expected = carbon * 10.0 * cfg.carbon_cost_duration_scale; // "Precise"'s duration (lowest error), position 1 => multiplier 1.0
        assert!((baseline - expected).abs() < 1e-9, "baseline={baseline}, expected={expected}");
    }
}
