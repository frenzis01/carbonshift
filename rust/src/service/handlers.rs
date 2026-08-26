//! HTTP handlers: job submission, status polling, executor callback, health.

use std::net::IpAddr;
use std::time::Duration;

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::Json;

use crate::engine::types::Request as EngineRequest;
use crate::service::models::{
    CallerCallbackPayload, ExecutorCallbackPayload, HorizonResponse, RequestStatus,
    RequestStatusResponse, StatsResponse, SubmitRequestPayload,
};
use crate::service::state::{AppState, TrackedRequest};

type ApiError = (StatusCode, Json<serde_json::Value>);

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
    Json(s)
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

    state.tracked.lock().unwrap().insert(
        request_id,
        TrackedRequest::new(body.callback_url.clone(), body.payload.clone()),
    );

    state
        .shared_state
        .add_request(EngineRequest::new(request_id, current_slot, deadline_slot));

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
        (t.status, t.error.clone())
    };
    let (status, error) = tracked_status;

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
        error,
    }))
}

/// `POST /v1/callback/{id}` — result callback from the executor.
///
/// Marks the request completed/failed and forwards the result to the
/// original caller's `callback_url` (if any) as a fire-and-forget task, so
/// a slow/unreachable caller endpoint never blocks the executor's request.
pub async fn executor_callback(
    State(state): State<AppState>,
    Path(request_id): Path<u64>,
    Json(body): Json<ExecutorCallbackPayload>,
) -> Result<StatusCode, ApiError> {
    let callback_url = {
        let mut guard = state.tracked.lock().unwrap();
        let t = guard
            .get_mut(&request_id)
            .ok_or_else(|| api_error(StatusCode::NOT_FOUND, "unknown request_id"))?;
        t.status = if body.success { RequestStatus::Completed } else { RequestStatus::Failed };
        t.error = body.error.clone();
        t.callback_url.clone()
    };

    if let Some(url) = callback_url {
        let http = state.http.clone();
        let payload = CallerCallbackPayload {
            request_id,
            success: body.success,
            result: body.result,
            error: body.error,
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
}
