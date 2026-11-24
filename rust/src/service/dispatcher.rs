//! Background task: dispatches scheduled jobs to the external executor when
//! their assigned slot arrives, and relays the outcome to `TrackedRequest`.
//!
//! Runs as a `tokio` task polling the (std-threaded) engine's committed
//! assignments — the engine has no async hooks, so a short poll loop is the
//! simplest safe bridge between the two. Failed dispatches are retried with
//! exponential backoff up to `ServiceConfig::executor_max_retries`, after
//! which the request is marked `Failed` instead of retried forever.

use std::collections::HashSet;
use std::time::{Duration, Instant};

use crate::engine::types::Assignment;
use crate::service::models::ExecutorDispatchPayload;
use crate::service::models::RequestStatus;
use crate::service::state::AppState;

/// Runs forever; spawn with `tokio::spawn(dispatcher::run(state))`.
pub async fn run(state: AppState) {
    let mut interval = tokio::time::interval(Duration::from_millis(
        state.service_cfg.dispatcher_poll_interval_ms,
    ));
    let mut warned_horizon = false;
    loop {
        interval.tick().await;
        let current_slot = state.shared_state.get_current_slot();
        warn_if_near_horizon(&state, current_slot, &mut warned_horizon);

        let now = Instant::now();
        let eligible_ids: HashSet<u64> = {
            let guard = state.tracked.lock().unwrap();
            guard
                .iter()
                .filter_map(|(request_id, tracked)| {
                    // Not `== Scheduled`: a close-slot flush can assign work
                    // after submit_request's initial wait has returned.
                    let eligible = matches!(
                        tracked.status,
                        RequestStatus::Pending | RequestStatus::Scheduled
                    ) && tracked.next_attempt_at.map(|at| at <= now).unwrap_or(true);
                    eligible.then_some(*request_id)
                })
                .collect()
        };
        // Release the tracked-request lock before acquiring shared scheduler
        // state. Only the selected IDs are looked up, and only due assignments
        // are cloned for dispatch.
        let due: Vec<Assignment> = state
            .shared_state
            .assignments_for_ids_due_by(&eligible_ids, current_slot);

        for assignment in due {
            dispatch_one(&state, assignment.request_id, &assignment).await;
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
    let total = state.scheduler.total_slots;
    if total > 0 && current_slot as f64 >= total as f64 * state.service_cfg.horizon_ready_threshold
    {
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
    let execute_at = match state.execute_at_for_slot(assignment.scheduled_slot) {
        Ok(execute_at) => execute_at,
        Err(error) => {
            tracing::error!(request_id, %error, "could not derive executor slot boundary");
            set_status(state, request_id, RequestStatus::Failed, Some(error));
            return;
        }
    };

    let dispatch_payload = ExecutorDispatchPayload {
        request_id,
        scheduled_slot: assignment.scheduled_slot,
        execute_at,
        flavour: assignment.flavour_name.clone(),
        carbon_cost: assignment.carbon_cost,
        callback_url: format!(
            "{}/v1/callback/{request_id}",
            state.service_cfg.self_base_url
        ),
        payload,
    };

    match &state.service_cfg.executor_url {
        None => {
            // Dry-run / test mode: no downstream executor configured.
            tracing::info!(
                request_id,
                ?dispatch_payload,
                "dry-run: not dispatching to any executor"
            );
            set_status(state, request_id, RequestStatus::Dispatched, None);
        }
        Some(url) => match state.http.post(url).json(&dispatch_payload).send().await {
            Ok(resp) if resp.status().is_success() => {
                set_status(state, request_id, RequestStatus::Dispatched, None);
            }
            Ok(resp) => record_failure(
                state,
                request_id,
                format!("executor returned {}", resp.status()),
            ),
            Err(e) => record_failure(state, request_id, e.to_string()),
        },
    }
}

fn set_status(state: &AppState, request_id: u64, status: RequestStatus, error: Option<String>) {
    let dry_run_dispatch =
        state.service_cfg.executor_url.is_none() && status == RequestStatus::Dispatched;
    let should_notify = {
        let mut tracked = state.tracked.lock().unwrap();
        if let Some(t) = tracked.get_mut(&request_id) {
            if matches!(status, RequestStatus::Dispatched | RequestStatus::Failed) {
                // The payload is only needed while a dispatch may be retried.
                // Keep the task label separately for history responses.
                t.payload = serde_json::Value::Null;
            }
            if !t.executor_callback_received {
                t.status = status;
                t.error = error;
                dry_run_dispatch
            } else {
                false
            }
        } else {
            false
        }
    };
    if should_notify {
        state.notify_archive_sweep();
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
    let Some(t) = guard.get_mut(&request_id) else {
        return;
    };
    // A concurrent/late dispatch response must not undo an accepted dispatch
    // or a terminal lifecycle transition.
    if t.executor_callback_received
        || !matches!(t.status, RequestStatus::Pending | RequestStatus::Scheduled)
    {
        return;
    }
    t.dispatch_attempts += 1;

    if t.dispatch_attempts >= max_retries {
        t.status = RequestStatus::Failed;
        t.error = Some(format!(
            "dispatch failed after {} attempt(s): {error}",
            t.dispatch_attempts
        ));
        t.payload = serde_json::Value::Null;
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
    let multiplier = 1u64
        .checked_shl(attempt.saturating_sub(1))
        .unwrap_or(u64::MAX);
    let ms = base_ms.saturating_mul(multiplier).min(max_ms);
    Duration::from_millis(ms)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, RwLock};

    use crate::engine::config::Config;
    use crate::engine::qos::QosProfileId;
    use crate::engine::shared_state::SharedState;
    use crate::service::state::{ServiceConfig, TrackedRequest};

    fn test_state(max_retries: u32) -> AppState {
        AppState::new(
            SharedState::new(),
            Arc::new(Config::default()),
            ServiceConfig {
                executor_url: None,
                self_base_url: "http://localhost:8080".to_owned(),
                submit_wait_timeout_secs: 0.1,
                allow_private_callbacks: false,
                api_key: None,
                executor_token: None,
                executor_max_retries: max_retries,
                executor_retry_base_ms: 10,
                executor_retry_max_ms: 100,
                horizon_ready_threshold: 0.9,
                dispatcher_poll_interval_ms: 20,
                request_archive_dir: String::new(),
            },
            Arc::new(RwLock::new(Vec::new())),
        )
    }

    fn tracked_request() -> TrackedRequest {
        TrackedRequest::new(
            None,
            serde_json::json!({"task": "text_generation", "input": "large prompt"}),
            1.0,
            10.0,
            0,
            QosProfileId::default_profile(),
        )
    }

    #[test]
    fn backoff_grows_exponentially_and_caps() {
        assert_eq!(backoff_delay(1, 100, 10_000), Duration::from_millis(100));
        assert_eq!(backoff_delay(2, 100, 10_000), Duration::from_millis(200));
        assert_eq!(backoff_delay(3, 100, 10_000), Duration::from_millis(400));
        assert_eq!(
            backoff_delay(10, 100, 10_000),
            Duration::from_millis(10_000)
        );
    }

    #[test]
    fn backoff_never_overflows_on_large_attempt_counts() {
        let d = backoff_delay(1000, 100, 60_000);
        assert_eq!(d, Duration::from_millis(60_000));
    }

    #[test]
    fn accepted_dispatch_releases_payload_but_keeps_task_label() {
        let state = test_state(3);
        state.tracked.lock().unwrap().insert(1, tracked_request());

        set_status(&state, 1, RequestStatus::Dispatched, None);

        let tracked = state.tracked.lock().unwrap();
        let request = tracked.get(&1).unwrap();
        assert_eq!(request.payload, serde_json::Value::Null);
        assert_eq!(request.task.as_deref(), Some("text_generation"));
    }

    #[test]
    fn late_dispatch_failure_does_not_replace_an_accepted_dispatch() {
        let state = test_state(1);
        state.tracked.lock().unwrap().insert(1, tracked_request());
        set_status(&state, 1, RequestStatus::Dispatched, None);

        record_failure(&state, 1, "duplicate dispatch was rejected".to_owned());

        let tracked = state.tracked.lock().unwrap();
        let request = tracked.get(&1).unwrap();
        assert_eq!(request.status, RequestStatus::Dispatched);
        assert!(request.error.is_none());
        assert_eq!(request.dispatch_attempts, 0);
    }

    #[test]
    fn payload_is_kept_for_retries_then_released_after_final_failure() {
        let state = test_state(2);
        state.tracked.lock().unwrap().insert(1, tracked_request());

        record_failure(&state, 1, "temporary".to_owned());
        assert!(
            !state
                .tracked
                .lock()
                .unwrap()
                .get(&1)
                .unwrap()
                .payload
                .is_null()
        );

        record_failure(&state, 1, "final".to_owned());
        let tracked = state.tracked.lock().unwrap();
        let request = tracked.get(&1).unwrap();
        assert_eq!(request.status, RequestStatus::Failed);
        assert_eq!(request.payload, serde_json::Value::Null);
        assert_eq!(request.task.as_deref(), Some("text_generation"));
    }
}
