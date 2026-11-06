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
    use crate::engine::qos::QosProfileId;

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
}
