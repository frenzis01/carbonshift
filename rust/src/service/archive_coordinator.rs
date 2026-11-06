use crate::{
    service::{
        archive::ArchivedRequest,
        models::{AssignmentItem, RequestStatus, TrackedRequestResponse},
        state::{AppState, TrackedRequest},
    },
    types::Assignment,
};
use std::collections::{HashMap, HashSet};
use std::{error::Error, io, time::Duration};

/// Periodically sweeps the archive
pub async fn run(state: AppState, interval: Duration) {
    let mut ticker = tokio::time::interval(interval);

    loop {
        ticker.tick().await;
        let state = state.clone();

        match tokio::task::spawn_blocking(move || sweep_once(&state)).await {
            Ok(Ok(())) => {}
            Ok(Err(error)) => tracing::error!(%error, "archive sweep failed"),
            Err(error) => tracing::error!(%error, "archive sweep task failed"),
        }
    }
}

fn sweep_once(state: &AppState) -> Result<(), Box<dyn Error + Send + Sync>> {
    // Test/embedded AppState constructors may deliberately disable persistence;
    // the production sweep must fail visibly rather than silently drop rows.
    let archive = state.request_archive.as_ref().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::NotFound,
            "request archive is disabled for this AppState",
        )
    })?;

    // Do not substitute current_slot: a worker claimed earlier may still
    // commit into an older slot.
    let settled_through_slot = state.shared_state.get_settled_through_slot();

    // Copy IDs under the service lock, then release it before taking the
    // engine lock. The response snapshot below excludes large payloads and
    // callback URLs.
    let ids: HashSet<_> = state.tracked.lock().unwrap().keys().copied().collect();

    // Fetch only due assignments by request ID; the index avoids cloning the
    // whole assignment map.
    let assignments = state
        .shared_state
        .assignments_for_ids_due_by(&ids, settled_through_slot);

    // Apply the lifecycle rule and snapshot response fields under one short
    // tracked lock. Release it before archive file I/O.
    let (mut requests_by_id, settled_assignments) = {
        let tracked = state.tracked.lock().unwrap();
        let mut settled_assignments = Vec::new();
        let mut responses = HashMap::new();
        for assignment in assignments {
            let request = tracked.get(&assignment.request_id).ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::NotFound,
                    format!(
                        "tracked request {} disappeared during archive snapshot",
                        assignment.request_id
                    ),
                )
            })?;
            if !is_settled(
                request,
                Some(&assignment),
                settled_through_slot,
                state.service_cfg.executor_url.is_some(),
            ) {
                continue;
            }
            responses.insert(
                assignment.request_id,
                TrackedRequestResponse {
                    request_id: assignment.request_id,
                    task: request.task.clone(),
                    qos_profile_id: request.qos_profile_id.to_string(),
                    status: request.status,
                    arrival_slot: request.arrival_slot,
                    scheduled_slot: Some(assignment.scheduled_slot),
                    flavour: Some(assignment.flavour_name.clone()),
                    carbon_cost: Some(assignment.carbon_cost),
                    actual_carbon_cost: request.actual_carbon_cost,
                    baseline_carbon_cost: request.baseline_carbon_cost,
                    actual_baseline_carbon_cost: request.actual_baseline_carbon_cost,
                    actual_error_pct: request.actual_error_pct,
                    execution_time_sec: request.execution_time_sec,
                    baseline_execution_time_sec: request.baseline_execution_time_sec,
                    scheduled_at: Some(assignment.assignment_time),
                    error: request.error.clone(),
                },
            );
            settled_assignments.push(assignment);
        }
        (responses, settled_assignments)
    };

    // Build and append one archive record at a time. This avoids keeping a
    // second vector containing every archived row in memory, and the tracked
    // mutex has already been released before any file I/O occurs.
    for assignment in settled_assignments {
        let request = requests_by_id
            .remove(&assignment.request_id)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::NotFound,
                    format!(
                        "request {} has no archived response snapshot",
                        assignment.request_id
                    ),
                )
            })?;
        let request_id = request.request_id;
        let row = ArchivedRequest {
            schema_version: 1,
            run_id: archive.run_id().to_owned(),
            request,
            assignment: Some(AssignmentItem {
                request_id: assignment.request_id,
                qos_profile_id: assignment.qos_profile_id.to_string(),
                scheduled_slot: assignment.scheduled_slot,
                flavour_name: assignment.flavour_name,
                carbon_cost: assignment.carbon_cost,
                error: assignment.error,
                flavour_duration: assignment.flavour_duration,
                arrival_slot: assignment.arrival_slot,
                deadline_slot: assignment.deadline_slot,
                assignment_time: assignment.assignment_time,
            }),
        };
        archive.append_once(&row)?;

        // The archive is durable before live memory is released. If eviction
        // fails, append_once makes the next sweep retry safe and idempotent.
        state
            .shared_state
            .evict_settled_assignment(request_id, settled_through_slot)?;
        state.tracked.lock().unwrap().remove(&request_id);
    }
    Ok(())
}

fn is_settled(
    request: &TrackedRequest,
    assignment: Option<&Assignment>,
    settled_through_slot: i32,
    executor_configured: bool,
) -> bool {
    let terminal = match request.status {
        RequestStatus::Completed | RequestStatus::Failed => request.executor_callback_received,
        RequestStatus::Dispatched => !executor_configured,
        RequestStatus::Pending | RequestStatus::Scheduled => false,
    };
    let slot_has_passed = assignment
        .map(|item| item.scheduled_slot <= settled_through_slot)
        .unwrap_or(request.arrival_slot <= settled_through_slot);
    terminal && slot_has_passed
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::config::Config;
    use crate::engine::qos::QosProfileId;
    use crate::engine::shared_state::SharedState;
    use crate::service::archive::RequestArchive;
    use crate::service::models::AssignmentItem;
    use crate::service::state::ServiceConfig;
    use std::sync::{Arc, RwLock};
    use std::time::{SystemTime, UNIX_EPOCH};

    fn archive_directory(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "carbonshift-{name}-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }

    fn app_state(archive: Arc<RequestArchive>, executor: bool) -> AppState {
        let mut config = Config::default();
        config.total_slots = 8;
        let config = Arc::new(config);
        let service_cfg = ServiceConfig {
            executor_url: executor.then(|| "http://executor/dispatch".to_owned()),
            self_base_url: "http://localhost:8080".to_owned(),
            submit_wait_timeout_secs: 0.1,
            allow_private_callbacks: false,
            api_key: None,
            executor_token: None,
            executor_max_retries: 3,
            executor_retry_base_ms: 10,
            executor_retry_max_ms: 100,
            horizon_ready_threshold: 0.9,
            dispatcher_poll_interval_ms: 20,
            request_archive_dir: String::new(),
        };
        let mut state = AppState::new(
            SharedState::new(),
            config,
            service_cfg,
            Arc::new(RwLock::new(vec![100.0; 8])),
        );
        state.request_archive = Some(archive);
        state
    }

    fn insert_tracked(state: &AppState, id: u64, request: TrackedRequest) {
        state.tracked.lock().unwrap().insert(id, request);
    }

    fn archived_row(archive: &RequestArchive, assignment: &Assignment) -> ArchivedRequest {
        ArchivedRequest {
            schema_version: 1,
            run_id: archive.run_id().to_owned(),
            request: TrackedRequestResponse {
                request_id: assignment.request_id,
                task: Some("text_generation".to_owned()),
                qos_profile_id: assignment.qos_profile_id.to_string(),
                status: RequestStatus::Completed,
                arrival_slot: assignment.arrival_slot.unwrap_or(0),
                scheduled_slot: Some(assignment.scheduled_slot),
                flavour: Some(assignment.flavour_name.clone()),
                carbon_cost: Some(assignment.carbon_cost),
                actual_carbon_cost: Some(assignment.carbon_cost),
                baseline_carbon_cost: 2.0,
                actual_baseline_carbon_cost: Some(2.0),
                actual_error_pct: Some(assignment.error),
                execution_time_sec: Some(1.0),
                baseline_execution_time_sec: Some(1.0),
                scheduled_at: Some(assignment.assignment_time),
                error: None,
            },
            assignment: Some(AssignmentItem {
                request_id: assignment.request_id,
                qos_profile_id: assignment.qos_profile_id.to_string(),
                scheduled_slot: assignment.scheduled_slot,
                flavour_name: assignment.flavour_name.clone(),
                carbon_cost: assignment.carbon_cost,
                error: assignment.error,
                flavour_duration: assignment.flavour_duration,
                arrival_slot: assignment.arrival_slot,
                deadline_slot: assignment.deadline_slot,
                assignment_time: assignment.assignment_time,
            }),
        }
    }

    fn tracked(status: RequestStatus, callback_received: bool) -> TrackedRequest {
        let mut request = TrackedRequest::new(
            None,
            serde_json::Value::Null,
            1.0,
            1.0,
            3,
            QosProfileId::default_profile(),
        );
        request.status = status;
        request.executor_callback_received = callback_received;
        request
    }

    fn assignment(slot: i32) -> Assignment {
        Assignment::new(1, slot, "Fast".to_owned(), 1.0, 1.0, 1.0, Some(3), Some(5))
    }

    #[test]
    fn settled_requires_terminal_lifecycle_and_settled_slot() {
        assert!(is_settled(
            &tracked(RequestStatus::Completed, true),
            Some(&assignment(3)),
            3,
            true,
        ));
        assert!(!is_settled(
            &tracked(RequestStatus::Completed, false),
            Some(&assignment(3)),
            3,
            true,
        ));
        assert!(!is_settled(
            &tracked(RequestStatus::Dispatched, false),
            Some(&assignment(3)),
            3,
            true,
        ));
        assert!(is_settled(
            &tracked(RequestStatus::Dispatched, false),
            Some(&assignment(3)),
            3,
            false,
        ));
        assert!(!is_settled(
            &tracked(RequestStatus::Completed, true),
            Some(&assignment(4)),
            3,
            true,
        ));
    }

    #[test]
    fn sweep_keeps_executor_request_live_until_callback() {
        let directory = archive_directory("sweep-inflight");
        let archive = Arc::new(RequestArchive::open(&directory).unwrap());
        let state = app_state(archive.clone(), true);
        state.shared_state.add_assignments(vec![assignment(0)]);
        state.shared_state.advance_settled_through_slot(0);
        insert_tracked(&state, 1, tracked(RequestStatus::Dispatched, false));

        sweep_once(&state).unwrap();

        assert!(archive.read_all().unwrap().is_empty());
        assert!(state.shared_state.get_assignment(1).is_some());
        assert!(state.tracked.lock().unwrap().contains_key(&1));
        drop(state);
        drop(archive);
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn sweep_keeps_assignment_in_unsettled_slot_live() {
        let directory = archive_directory("sweep-unsettled");
        let archive = Arc::new(RequestArchive::open(&directory).unwrap());
        let state = app_state(archive.clone(), false);
        state.shared_state.add_assignments(vec![assignment(0)]);
        state.shared_state.advance_settled_through_slot(-1);
        insert_tracked(&state, 1, tracked(RequestStatus::Dispatched, false));

        sweep_once(&state).unwrap();

        assert!(archive.read_all().unwrap().is_empty());
        assert!(state.shared_state.get_assignment(1).is_some());
        assert!(state.tracked.lock().unwrap().contains_key(&1));
        drop(state);
        drop(archive);
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn sweep_archives_then_evicts_and_keeps_error_aggregates() {
        let directory = archive_directory("sweep-completed");
        let archive = Arc::new(RequestArchive::open(&directory).unwrap());
        let state = app_state(archive.clone(), true);
        let assignment = Assignment::new_for_profile(
            1,
            0,
            "Fast".to_owned(),
            1.0,
            4.5,
            1.0,
            Some(0),
            Some(1),
            QosProfileId::default_profile(),
        );
        state.shared_state.add_assignments(vec![assignment]);
        state.shared_state.advance_settled_through_slot(0);
        insert_tracked(&state, 1, tracked(RequestStatus::Completed, true));

        sweep_once(&state).unwrap();

        let rows = archive.read_all().unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].request.request_id, 1);
        assert!(state.shared_state.get_assignment(1).is_none());
        assert!(!state.tracked.lock().unwrap().contains_key(&1));

        let profile_id = QosProfileId::default_profile();
        let error = state
            .shared_state
            .get_profile_slot_error_stats(&profile_id, 0);
        assert_eq!(error.count, 1);
        assert_eq!(error.error_sum, 4.5);
        assert_eq!(state.shared_state.get_slot_error_stats(0).count, 1);
        assert_eq!(
            state
                .shared_state
                .get_profile_error_stats(&profile_id)
                .count,
            1
        );

        drop(state);
        drop(archive);
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn append_then_failed_eviction_can_retry_without_duplicate_archive_row() {
        let directory = archive_directory("sweep-retry");
        let archive = RequestArchive::open(&directory).unwrap();
        let shared_state = SharedState::new();
        let assignment = assignment(3);
        shared_state.add_assignments(vec![assignment.clone()]);
        let row = archived_row(&archive, &assignment);

        assert!(archive.append_once(&row).unwrap());
        assert!(shared_state.evict_settled_assignment(1, 2).is_err());
        assert!(!archive.append_once(&row).unwrap());
        assert_eq!(archive.read_all().unwrap().len(), 1);

        assert!(
            shared_state
                .evict_settled_assignment(1, 3)
                .unwrap()
                .is_some()
        );
        assert!(shared_state.get_assignment(1).is_none());
        assert_eq!(archive.read_all().unwrap().len(), 1);

        drop(archive);
        std::fs::remove_dir_all(directory).unwrap();
    }
}
