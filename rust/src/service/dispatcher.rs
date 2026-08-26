//! Background task: dispatches scheduled jobs to the external executor when
//! their assigned slot arrives, and relays the outcome to `TrackedRequest`.
//!
//! Runs as a `tokio` task polling the (std-threaded) engine's committed
//! assignments — the engine has no async hooks, so a short poll loop is the
//! simplest safe bridge between the two. Failed dispatches are retried with
//! exponential backoff up to `ServiceConfig::executor_max_retries`, after
//! which the request is marked `Failed` instead of retried forever.

use std::time::{Duration, Instant};

use crate::engine::types::Assignment;
use crate::service::models::RequestStatus;
use crate::service::models::ExecutorDispatchPayload;
use crate::service::state::AppState;

const POLL_INTERVAL: Duration = Duration::from_millis(200);

/// Runs forever; spawn with `tokio::spawn(dispatcher::run(state))`.
pub async fn run(state: AppState) {
    let mut interval = tokio::time::interval(POLL_INTERVAL);
    let mut warned_horizon = false;
    loop {
        interval.tick().await;
        let current_slot = state.shared_state.get_current_slot();
        warn_if_near_horizon(&state, current_slot, &mut warned_horizon);

        let now = Instant::now();
        let assignments = state.shared_state.get_current_assignments();
        let due: Vec<(u64, Assignment)> = {
            let guard = state.tracked.lock().unwrap();
            assignments
                .iter()
                .filter(|(id, a)| {
                    a.scheduled_slot <= current_slot
                        && guard
                            .get(*id)
                            .map(|t| {
                                t.status == RequestStatus::Scheduled
                                    && t.next_attempt_at.map(|at| at <= now).unwrap_or(true)
                            })
                            .unwrap_or(false)
                })
                .map(|(id, a)| (*id, a.clone()))
                .collect()
        };

        for (request_id, assignment) in due {
            dispatch_one(&state, request_id, &assignment).await;
        }
    }
}

/// Once past the configured horizon threshold, new requests can no longer be
/// placed (the DP solver caps deadlines at `total_slots - 1`) — this only
/// logs a heads-up; `GET /ready` (see `handlers::ready`) is what actually
/// gates traffic for an orchestrator (PLAN_SERVICE.md Fase 6: the engine has
/// no rolling horizon, so the mitigation is "roll a replacement instance
/// before this happens", not "keep running forever").
fn warn_if_near_horizon(state: &AppState, current_slot: i32, warned: &mut bool) {
    if *warned {
        return;
    }
    let total = state.cfg.total_slots;
    if total > 0 && current_slot as f64 >= total as f64 * state.service_cfg.horizon_ready_threshold {
        tracing::warn!(
            current_slot,
            total_slots = total,
            "approaching the planning horizon (TOTAL_SLOTS); GET /ready now reports \
             not-ready so an orchestrator can roll a replacement instance \
             (see PLAN_SERVICE.md, Fase 6: no rolling horizon yet)"
        );
        *warned = true;
    }
}

async fn dispatch_one(state: &AppState, request_id: u64, assignment: &Assignment) {
    let payload = state
        .tracked
        .lock()
        .unwrap()
        .get(&request_id)
        .map(|t| t.payload.clone())
        .unwrap_or(serde_json::Value::Null);

    let dispatch_payload = ExecutorDispatchPayload {
        request_id,
        scheduled_slot: assignment.scheduled_slot,
        flavour: assignment.flavour_name.clone(),
        carbon_cost: assignment.carbon_cost,
        callback_url: format!("{}/v1/callback/{request_id}", state.service_cfg.self_base_url),
        payload,
    };

    match &state.service_cfg.executor_url {
        None => {
            // Dry-run / test mode: no downstream executor configured.
            tracing::info!(request_id, ?dispatch_payload, "dry-run: not dispatching to any executor");
            set_status(state, request_id, RequestStatus::Dispatched, None);
        }
        Some(url) => match state.http.post(url).json(&dispatch_payload).send().await {
            Ok(resp) if resp.status().is_success() => {
                set_status(state, request_id, RequestStatus::Dispatched, None);
            }
            Ok(resp) => record_failure(state, request_id, format!("executor returned {}", resp.status())),
            Err(e) => record_failure(state, request_id, e.to_string()),
        },
    }
}

fn set_status(state: &AppState, request_id: u64, status: RequestStatus, error: Option<String>) {
    if let Some(t) = state.tracked.lock().unwrap().get_mut(&request_id) {
        t.status = status;
        t.error = error;
    }
}

/// Records a failed dispatch attempt; gives up (marks `Failed`) once
/// `executor_max_retries` is reached, otherwise schedules the next attempt
/// after an exponential backoff delay.
fn record_failure(state: &AppState, request_id: u64, error: String) {
    let max_retries = state.service_cfg.executor_max_retries;
    let base_ms = state.service_cfg.executor_retry_base_ms;
    let max_ms = state.service_cfg.executor_retry_max_ms;

    let mut guard = state.tracked.lock().unwrap();
    let Some(t) = guard.get_mut(&request_id) else { return };
    t.dispatch_attempts += 1;

    if t.dispatch_attempts >= max_retries {
        t.status = RequestStatus::Failed;
        t.error = Some(format!("dispatch failed after {} attempt(s): {error}", t.dispatch_attempts));
        tracing::error!(request_id, attempts = t.dispatch_attempts, %error, "giving up dispatching to executor");
    } else {
        let delay = backoff_delay(t.dispatch_attempts, base_ms, max_ms);
        t.next_attempt_at = Some(Instant::now() + delay);
        tracing::warn!(
            request_id,
            attempt = t.dispatch_attempts,
            delay_ms = delay.as_millis() as u64,
            %error,
            "dispatch failed; will retry"
        );
    }
}

/// Exponential backoff: `base_ms * 2^(attempt - 1)`, capped at `max_ms`.
pub fn backoff_delay(attempt: u32, base_ms: u64, max_ms: u64) -> Duration {
    let multiplier = 1u64.checked_shl(attempt.saturating_sub(1)).unwrap_or(u64::MAX);
    let ms = base_ms.saturating_mul(multiplier).min(max_ms);
    Duration::from_millis(ms)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_grows_exponentially_and_caps() {
        assert_eq!(backoff_delay(1, 100, 10_000), Duration::from_millis(100));
        assert_eq!(backoff_delay(2, 100, 10_000), Duration::from_millis(200));
        assert_eq!(backoff_delay(3, 100, 10_000), Duration::from_millis(400));
        assert_eq!(backoff_delay(10, 100, 10_000), Duration::from_millis(10_000));
    }

    #[test]
    fn backoff_never_overflows_on_large_attempt_counts() {
        let d = backoff_delay(1000, 100, 60_000);
        assert_eq!(d, Duration::from_millis(60_000));
    }
}

