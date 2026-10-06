//! HTTP handlers: job submission, status polling, executor callback, health.

use std::collections::HashMap;
use std::net::IpAddr;
use std::sync::Arc;
use std::time::Duration;

use axum::Json;
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use serde::Deserialize;

use crate::engine::qos::{
    CumulativeErrorPolicy, ErrorWindowPolicy, QosProfile, QosProfileId, TaskKindId,
};
use crate::engine::types::{Flavour, Request as EngineRequest, get_capacity_multiplier};
use crate::service::models::{
    AssignmentItem, AssignmentsQuery, CallerCallbackPayload, CostMetricsResponse,
    ErrorHistoryResponse, ExecutorCallbackPayload, HorizonResponse, RegisterQosProfilePayload,
    RegisterTaskPayload, RequestStatus, RequestStatusResponse, SlotDetailResponse, SlotErrorItem,
    StatsResponse, SubmitRequestPayload, TaskConfigResponse,
};
use crate::service::state::{AppState, ProfileRegistration, ProfileRegistryError, TrackedRequest};

type ApiError = (StatusCode, Json<serde_json::Value>);

#[derive(Debug, Deserialize)]
pub struct AdvanceSlotBody {
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub kind: String,
    pub current_slot: Option<i64>,
    #[serde(default)]
    pub slot_start_utc: Option<String>,
    // observed is a dict with "slot", "observed_at_slot", and "actual" keys
    // the latter being the actual observed carbon intensity.
    #[serde(default)]
    pub observed: Option<ObservedPoint>,
    // forecast is a list of dictionaries of slot + forecast value
    #[serde(default)]
    // pub forecast: Option<std::collections::HashMap<usize, f64>>,
    pub forecast: Option<Vec<ForecastPoint>>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct ForecastPoint {
    pub slot: i64,
    pub forecast: f64,
}

#[derive(Debug, Deserialize, Clone)]
pub struct ObservedPoint {
    pub slot: i64,
    pub observed_at_slot: i64,
    pub actual: f64,
}

#[derive(Debug, Deserialize)]
pub struct CarbonIntensityQuery {
    /// Upper bound slot to return. If omitted, use the current virtual slot.
    #[serde(default)]
    pub slot: Option<i32>,
    /// Backwards-compatible alias for the same concept.
    #[serde(default)]
    pub until_slot: Option<i32>,
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
                return Err("callback_url points to a loopback/private address; set \
                     CARBONSHIFT_ALLOW_PRIVATE_CALLBACKS=1 to allow this for local testing"
                    .to_string());
            }
        }
    }
    Ok(())
}

pub async fn health() -> &'static str {
    "ok"
}

/// Register a stable, reusable scheduling and error-budget profile.
///
/// Identical repeated registrations are safe; changing an existing profile
/// requires a new ID so already-created requests are never reinterpreted.
pub async fn register_qos_profile(
    State(state): State<AppState>,
    Json(body): Json<RegisterQosProfilePayload>,
) -> Result<StatusCode, ApiError> {
    let profile_id = QosProfileId::parse(body.profile_id)
        .map_err(|error| api_error(StatusCode::BAD_REQUEST, error))?;
    let task_kind = TaskKindId::parse(body.task_kind)
        .map_err(|error| api_error(StatusCode::BAD_REQUEST, error))?;
    let profile = QosProfile {
        profile_id,
        task_kind,
        flavours: body.flavours,
        error_semantics: body.error_semantics,
        max_error_threshold: body.max_error_threshold,
        error_window: body.error_window,
        cumulative_error: body.cumulative_error,
    };

    match state.qos_profiles.register(profile) {
        Ok(ProfileRegistration::Created | ProfileRegistration::AlreadyRegistered) => {
            Ok(StatusCode::NO_CONTENT)
        }
        Err(ProfileRegistryError::Invalid(error)) => Err(api_error(StatusCode::BAD_REQUEST, error)),
        Err(ProfileRegistryError::Conflict(profile_id)) => Err(api_error(
            StatusCode::CONFLICT,
            format!("profile_id {profile_id} is already registered with different settings"),
        )),
    }
}

/// List the active QoS profiles, including Carbonshift's deterministic defaults.
pub async fn list_qos_profiles(State(state): State<AppState>) -> Json<Vec<QosProfile>> {
    Json(state.qos_profiles.list())
}

/// Retrieve one active profile by its stable, reusable identifier.
pub async fn get_qos_profile(
    State(state): State<AppState>,
    Path(profile_id): Path<String>,
) -> Result<Json<QosProfile>, ApiError> {
    let profile = state
        .qos_profiles
        .get_by_str(&profile_id)
        .map_err(|error| api_error(StatusCode::BAD_REQUEST, error))?
        .ok_or_else(|| api_error(StatusCode::NOT_FOUND, "unknown QoS profile"))?;
    Ok(Json(profile))
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
fn compute_baseline_carbon_cost(
    state: &AppState,
    arrival_slot: i32,
    flavours: &[Flavour],
) -> (f64, i32) {
    let position = {
        let mut counts = state.baseline_slot_counts.lock().unwrap();
        let c = counts.entry(arrival_slot).or_insert(0);
        *c += 1;
        *c
    };
    let carbon = state
        .carbon_forecast
        .read()
        .unwrap()
        .get(arrival_slot as usize)
        .copied()
        .unwrap_or(0.0);
    let mult = get_capacity_multiplier(&state.scheduler.capacity_tiers, position);
    let accurate = flavours
        .iter()
        .min_by(|a, b| a.error.partial_cmp(&b.error).unwrap())
        .expect("task must have at least one flavour");
    let cost =
        carbon * mult * accurate.duration as f64 * state.scheduler.carbon_cost_duration_scale;
    (cost, accurate.duration)
}

fn resolve_request_profile(
    state: &AppState,
    body: &SubmitRequestPayload,
) -> Result<Arc<QosProfile>, ApiError> {
    let explicit_profile_id = body
        .qos_profile_id
        .as_deref()
        .map(QosProfileId::parse)
        .transpose()
        .map_err(|error| api_error(StatusCode::BAD_REQUEST, error))?;
    let legacy_profile_id = body
        .task_id
        .as_deref()
        .filter(|task_id| *task_id != "default")
        .and_then(|task_id| state.profile_id_for_task(task_id));

    if let (Some(explicit), Some(legacy)) = (&explicit_profile_id, &legacy_profile_id) {
        if explicit != legacy {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "qos_profile_id conflicts with the registered legacy task_id profile",
            ));
        }
    }

    let selected_profile_id = explicit_profile_id.or(legacy_profile_id);
    let selected_profile = selected_profile_id
        .as_ref()
        .map(|profile_id| {
            state
                .qos_profiles
                .get(profile_id)
                .ok_or_else(|| api_error(StatusCode::NOT_FOUND, "unknown QoS profile"))
        })
        .transpose()?;

    let payload_task_kind = body
        .payload
        .get("task")
        .map(|value| {
            value.as_str().ok_or_else(|| {
                api_error(
                    StatusCode::BAD_REQUEST,
                    "payload.task must be a string task kind",
                )
            })
        })
        .transpose()?;
    if let (Some(explicit), Some(payload)) = (body.task_kind.as_deref(), payload_task_kind) {
        if explicit != payload {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "task_kind conflicts with payload.task",
            ));
        }
    }
    let requested_task_kind = body
        .task_kind
        .as_deref()
        .or(payload_task_kind)
        .map(TaskKindId::parse)
        .transpose()
        .map_err(|error| api_error(StatusCode::BAD_REQUEST, error))?;
    let task_kind = if let Some(task_kind) = requested_task_kind {
        task_kind
    } else if let Some(profile) = &selected_profile {
        profile.task_kind.clone()
    } else if let Some(task_id) = body.task_id.as_deref() {
        let candidate = TaskKindId::parse(task_id.to_string()).ok();
        candidate
            .filter(|kind| state.qos_profiles.default_for_task_kind(kind).is_some())
            .unwrap_or_else(|| TaskKindId::parse("text_generation").unwrap())
    } else {
        TaskKindId::parse("text_generation").unwrap()
    };

    let profile = match selected_profile {
        Some(profile) => profile,
        None => state
            .qos_profiles
            .default_for_task_kind(&task_kind)
            .ok_or_else(|| {
                api_error(
                    StatusCode::BAD_REQUEST,
                    format!(
                        "task kind {task_kind} has no default profile; register and select a QoS profile"
                    ),
                )
            })?,
    };
    if profile.task_kind != task_kind {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            format!(
                "QoS profile {} is for task kind {}, not {}",
                profile.profile_id, profile.task_kind, task_kind
            ),
        ));
    }
    Ok(Arc::new(profile))
}

fn resolve_profile_query(
    state: &AppState,
    query: &AssignmentsQuery,
) -> Result<Option<QosProfile>, ApiError> {
    if query.task_id.is_some() {
        state.legacy_task_id_usage.record_monitoring_query();
    }
    let requested = query
        .qos_profile_id
        .as_deref()
        .map(QosProfileId::parse)
        .transpose()
        .map_err(|error| api_error(StatusCode::BAD_REQUEST, error))?;
    let legacy = query
        .task_id
        .as_deref()
        .and_then(|task_id| state.profile_id_for_task(task_id));
    if let (Some(requested), Some(legacy)) = (&requested, &legacy) {
        if requested != legacy {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "qos_profile_id conflicts with the legacy task_id profile",
            ));
        }
    }
    let Some(profile_id) = requested.or(legacy) else {
        return Ok(None);
    };
    state
        .qos_profiles
        .get(&profile_id)
        .map(Some)
        .ok_or_else(|| api_error(StatusCode::NOT_FOUND, "unknown QoS profile"))
}

/// Legacy adapter that registers task flavours as a stable QoS profile.
///
/// New clients should use `POST /v1/profiles`; task_id remains supported as a
/// profile alias during migration.
pub async fn register_task(
    State(state): State<AppState>,
    Json(body): Json<RegisterTaskPayload>,
) -> Result<StatusCode, ApiError> {
    state.legacy_task_id_usage.record_task_api_call();
    if body.task_id == "default" {
        return Err(api_error(
            StatusCode::CONFLICT,
            "the default QoS profile is task-kind-specific and immutable; register a named /v1/profiles entry instead",
        ));
    }
    if body.flavours.is_empty() {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "flavours must not be empty",
        ));
    }

    let RegisterTaskPayload {
        task_id,
        flavours,
        task_kind,
        error_semantics,
        max_error_threshold,
        capacity_tiers,
    } = body;
    if capacity_tiers.is_some() {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "capacity tiers are global and cannot be registered per task/profile",
        ));
    }
    let profile_id = QosProfileId::parse(task_id.clone())
        .map_err(|error| api_error(StatusCode::BAD_REQUEST, error))?;
    let task_kind = TaskKindId::parse(task_kind.unwrap_or_else(|| task_id.clone()))
        .map_err(|error| api_error(StatusCode::BAD_REQUEST, error))?;
    let error_semantics = error_semantics.unwrap_or_else(|| {
        task_kind
            .default_error_semantics()
            .unwrap_or("legacy-unspecified-v1")
            .to_string()
    });
    let profile = QosProfile {
        profile_id: profile_id.clone(),
        task_kind,
        flavours: flavours.clone(),
        error_semantics,
        max_error_threshold: max_error_threshold.unwrap_or(state.scheduler.max_error_threshold),
        error_window: ErrorWindowPolicy {
            past_slots: state.scheduler.error_window_past,
            future_slots: state.scheduler.error_window_future,
            past_decay_slots: state.scheduler.error_window_past_decay_slots,
        },
        cumulative_error: CumulativeErrorPolicy {
            enabled: state.scheduler.cumulative_error_enabled,
            hard: state.scheduler.cumulative_error_hard,
        },
    };
    match state.qos_profiles.register(profile) {
        Ok(ProfileRegistration::Created | ProfileRegistration::AlreadyRegistered) => {}
        Err(ProfileRegistryError::Invalid(error)) => {
            return Err(api_error(StatusCode::BAD_REQUEST, error));
        }
        Err(ProfileRegistryError::Conflict(profile_id)) => {
            return Err(api_error(
                StatusCode::CONFLICT,
                format!("task_id {profile_id} is already registered with different settings"),
            ));
        }
    }

    state.task_flavours.lock().unwrap().insert(
        task_id,
        crate::service::state::TaskConfig {
            profile_id,
            flavours,
            max_error_threshold,
            capacity_tiers: None,
        },
    );
    Ok(StatusCode::NO_CONTENT)
}

fn compute_horizon(state: &AppState) -> HorizonResponse {
    let current_slot = state.shared_state.get_current_slot();
    let total_slots = state.scheduler.total_slots.max(1);
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
    let status = if h.near_exhaustion {
        StatusCode::SERVICE_UNAVAILABLE
    } else {
        StatusCode::OK
    };
    (status, Json(h))
}

/// `POST /v1/admin/advance-slot` — test-only: force the virtual clock to
/// the next slot boundary (`Config::simulation.manual_clock` must be enabled), then
/// block until every assignment produced for the slot just left has been
/// handed off to the executor (status `Dispatched`, not just `Scheduled`).
/// See PLAN_SERVICE.md "Emulazione a tempo fittizio" for the full protocol
/// this enables together with the executor's own `/admin/advance-slot`.
///
/// The JSON body carries the provider's current global slot, optional observed
/// reading, and forecast window. Observations correct committed costs after
/// execution; they are kept separate from the forecast used for scheduling.
pub async fn advance_slot(
    State(state): State<AppState>,
    // We must have a real body struct carrying slot, observed, forecast and current_slot, all #[serde(default)]
    Json(body): Json<AdvanceSlotBody>,
) -> Result<Json<serde_json::Value>, ApiError> {
    if !state.scheduler.manual_clock {
        return Err(api_error(
            StatusCode::CONFLICT,
            "MANUAL_CLOCK is not enabled on this instance",
        ));
    }
    // TODO: too much slot naming here... evaluate if it's possible to simplify.

    // Fail loudly if there we cannot set offset
    if body.current_slot.is_none() {
        tracing::error!("Current slot is not provided in the request body");
        return Err(api_error(
            StatusCode::CONFLICT,
            "Current slot is not provided in the request body",
        ));
    }

    let body_current_slot = body.current_slot.unwrap();

    // Update the slot epoch offset upon the first slot advancement.
    let slot_epoch_offset = update_slot_epoch_offset(&state, body_current_slot).unwrap_or(0);

    // let Some(offset) = slot_epoch_offset else { /* 409 */ };
    let target_engine_slot = (body_current_slot - slot_epoch_offset as i64) as i32; // i64

    // init here

    // The announce is the first message, before any advance
    if body.kind == "announce" {
        // Sync: the engine is already at the target (0). Do NOT advance. We only update the forecast
        // debug_assert_eq!(state.shared_state.get_current_slot() as i64, target_engine_slot);
        if state.shared_state.get_current_slot() != target_engine_slot {
            tracing::warn!(
                current_slot = state.shared_state.get_current_slot(),
                target_engine_slot,
                "engine slot does not match the target slot on announce"
            );
            // fail loudly
            return Err(api_error(
                StatusCode::CONFLICT,
                "engine slot does not match the target slot on announce",
            ));
        }
    } else {
        println!(
            "[Service] Advancing to new slot: {current_slot} -> {target_engine_slot} (global: {body_current_slot})",
            current_slot = state.shared_state.get_current_slot(),
            target_engine_slot = target_engine_slot,
            body_current_slot = body_current_slot
        );
        let new_slot = crate::engine::scheduler::advance_to_next_slot(
            &state.shared_state,
            state.scheduler.total_slots,
            state.scheduler.effective_slot_duration_secs,
        );
        if new_slot != target_engine_slot {
            tracing::warn!(
                new_slot,
                target_engine_slot,
                "engine slot disagrees with the announced slot"
            );
        }
    }

    let curr_slot = state.shared_state.get_current_slot();

    if let Some(observed) = &body.observed {
        // Set actual carbon intensity for the current slot, so that we can adjust
        // in executor callbacks the carbon intensity and the carbon cost.

        // assert, just for safety that observed.slot is the current slot
        if target_engine_slot != observed.slot as i32 {
            tracing::warn!("Observed slot does not match the current slot");
        }
        state
            .actual_carbon_intensity
            .lock()
            .unwrap()
            .insert(target_engine_slot, observed.actual);
        // Since we already know the actual carbon intensity for the current slot
        // we use the actual value for such slot for the scheduler to decide
        // However we cannot overwrite here the forecast for the current slot,
        // since it would silently drop the correction we do later on between forecast/actual values,
        // TODO: allow the scheduler to have access to the real value ONLY for the current slot.
    }

    if let Some(forecast) = &body.forecast {
        // Update the forecast for the next K(=24) slots in the state.carbon_forecast hashmap.
        let mut carbon_forecast = state.carbon_forecast.write().unwrap();
        for point in forecast {
            let idx = point.slot - slot_epoch_offset as i64;
            // range check for subtraction
            if idx < 0 {
                tracing::warn!(
                    slot = point.slot,
                    offset = slot_epoch_offset,
                    "forecast point precedes the engine horizon; skipping"
                );
                continue;
            } // before our horizon
            let idx = idx as usize;
            if idx < carbon_forecast.len() {
                carbon_forecast[idx] = point.forecast;
            }
        }
    }

    let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
    // Wait until all pending requests are solved and any request due at or before curr_slot
    // has been dispatched to the executor.
    loop {
        let assignments = state.shared_state.get_current_assignments();
        let still_dispatching = {
            let guard = state.tracked.lock().unwrap();
            state.shared_state.get_pending_count() > 0
                || assignments.iter().any(|(id, a)| {
                    a.scheduled_slot <= curr_slot
                        && guard.get(id).map_or(false, |t| {
                            matches!(t.status, RequestStatus::Pending | RequestStatus::Scheduled)
                        })
                })
        };
        if !still_dispatching || tokio::time::Instant::now() >= deadline {
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }

    Ok(Json(serde_json::json!({ "current_slot": curr_slot })))
}

/// Function invoked upon the first slot advancement to set the slot epoch offset.
/// Recall that the slot epoch offset is the difference between the provider's global slot numbering and carbonshift's local slot numbering.
fn update_slot_epoch_offset(state: &AppState, slot_0: i64) -> Option<i32> {
    let mut offset = state.slot_epoch_offset.lock().unwrap();
    if offset.is_none() {
        // TODO: change every i32 into i64 to avoid these issues... or every i64 in i32, but Python sends everything as i64
        *offset = Some((slot_0 - state.shared_state.get_current_slot() as i64) as i32);
        tracing::info!("Slot epoch offset set to {}", offset.unwrap());
    }
    return offset.clone();
}

/// `GET /v1/carbon-forecast` — the shared forecast indexed by engine slot.
/// The provider updates it through the advance-slot request's `forecast` field;
/// measured values are stored separately through its `observed` field.
pub async fn carbon_forecast(State(state): State<AppState>) -> Json<serde_json::Value> {
    Json(serde_json::json!({ "forecast": *state.carbon_forecast }))
}

/// `GET /v1/stats` — counts of tracked requests by lifecycle status.
pub async fn stats(State(state): State<AppState>) -> Json<StatsResponse> {
    let guard = state.tracked.lock().unwrap();
    let mut s = StatsResponse {
        total: guard.len(),
        ..Default::default()
    };
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
    // TODO: Get error stats from state.rs, where there is TaskConfig with the proper error threhshold set
    let g = state.shared_state.get_global_error_stats();
    s.global_error_count = g.count;
    s.global_error_avg = if g.count > 0 { Some(g.avg) } else { None };
    s.legacy_task_id_usage = state.legacy_task_id_usage.snapshot();
    Json(s)
}

/// `GET /v1/tasks/{task_id}` — the task's currently effective flavours and
/// error threshold (registered via `POST /v1/tasks`, or the built-in
/// defaults if it was never announced).
pub async fn get_task_config(
    State(state): State<AppState>,
    Path(task_id): Path<String>,
) -> Json<TaskConfigResponse> {
    state.legacy_task_id_usage.record_task_api_call();
    Json(TaskConfigResponse {
        qos_profile_id: state
            .profile_id_for_task(&task_id)
            .unwrap_or_else(QosProfileId::default_profile)
            .to_string(),
        flavours: state.flavours_for_task(&task_id),
        max_error_threshold: state
            .threshold_for_task(&task_id)
            .unwrap_or(state.scheduler.max_error_threshold),
        capacity_tiers: state
            .capacity_tiers_for_task(&task_id)
            .unwrap_or_else(|| state.scheduler.capacity_tiers.clone()),
        task_id,
    })
}

/// `GET /v1/carbon_intensity` — the carbon intensity up to a requested slot,
/// both forecast and actual values.
///
/// Use this to inspect the original forecast the scheduler planned against and
/// the actual value the client later reported for each slot.
/// Example response:
/// ```json
/// [
///   {"slot": 0, "forecast": 123.45, "actual": 120.12},
///   {"slot": 1, "forecast": 132.56, "actual": 130.11},
///   {"slot": 2, "forecast": 141.23, "actual": null}
/// ]
/// ```
pub async fn carbon_intensity(
    State(state): State<AppState>,
    Query(query): Query<CarbonIntensityQuery>,
) -> Json<Vec<serde_json::Value>> {
    let upper = query
        .slot
        .or(query.until_slot)
        .unwrap_or_else(|| state.shared_state.get_current_slot());
    let upper = upper.max(0);
    let forecast = state.carbon_forecast.as_ref();
    let actual = state.actual_carbon_intensity.lock().unwrap();

    let rows = (0..=upper)
        .map(|slot| {
            let forecast_ci = forecast
                .read()
                .unwrap()
                .get(slot as usize)
                .copied()
                .unwrap_or(0.0);
            let actual_ci = actual.get(&slot).copied();
            serde_json::json!({
                "slot": slot,
                "forecast": forecast_ci,
                "actual": actual_ci,
            })
        })
        .collect();

    Json(rows)
}

// ─── Fine-grained monitoring endpoints ─────────────────────

/// `GET /v1/assignments` — list assignments globally or for one QoS profile.
///
/// Supports query filters:
/// - `from_slot`: minimum scheduled slot (inclusive)
/// - `to_slot`: maximum scheduled slot (inclusive)
/// - `flavour`: filter by flavour name (e.g. "Fast", "Balanced", "Accurate")
/// - `qos_profile_id`: stable profile ID (`task_id` is a migration alias)
///
/// Returns a list of `AssignmentItem` records snapshot from `state.shared_state.get_current_assignments()`.
pub async fn get_assignments(
    State(_state): State<AppState>,
    Query(_query): Query<AssignmentsQuery>,
) -> Result<Json<Vec<AssignmentItem>>, ApiError> {
    let profile = resolve_profile_query(&_state, &_query)?;
    let curr_assignments = _state.shared_state.get_current_assignments();
    let filtered_assignments: Vec<_> = curr_assignments
        .into_iter()
        .filter(|(_, assignment)| {
            (_query
                .from_slot
                .map_or(true, |from| assignment.scheduled_slot >= from))
                && (_query
                    .to_slot
                    .map_or(true, |to| assignment.scheduled_slot <= to))
                && (_query
                    .flavour
                    .as_ref()
                    .map_or(true, |flavour| &assignment.flavour_name == flavour))
                && profile.as_ref().map_or(true, |profile| {
                    assignment.qos_profile_id == profile.profile_id
                })
        })
        .collect();

    let items = {
        let mut items: Vec<AssignmentItem> = filtered_assignments
            .into_iter()
            .map(|(_req_id, assignment)| AssignmentItem {
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
            })
            // .sort_by_key(|item| (item.scheduled_slot, item.request_id))
            .collect::<Vec<_>>();

        items.sort_by_key(|item| (item.scheduled_slot, item.request_id));
        items
    };

    Ok(Json(items))
}

/// `GET /v1/slots/:slot` — fine-grained breakdown and status for a specific time slot.
///
/// Returns:
/// - Number of total requests scheduled in this slot
/// - Breakdown of request counts by flavour (e.g. `{"Fast": 12, "Accurate": 3}`)
/// - Total carbon cost accumulated in this slot
/// - Capacity multiplier currently in effect based on slot occupancy
/// - Both forecasted and observed carbon intensity for this slot
pub async fn get_slot_detail(
    State(_state): State<AppState>,
    Path(_slot): Path<i32>,
) -> Result<Json<SlotDetailResponse>, ApiError> {
    let carbon_forecast = _state.carbon_forecast.read().unwrap().clone();

    // horizon is yielded by max(current_slot + assignment_max_future_slots, carbon_forecast length)
    let horizon = std::cmp::max(
        _state.shared_state.get_current_slot() + _state.scheduler.assignment_max_future_slots,
        carbon_forecast.len() as i32,
    );

    // Status code 400 Bad Request for invalid slot
    if _slot < 0 {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "Slot cannot be negative",
        ));
    }
    if _slot >= horizon {
        return Err(api_error(StatusCode::BAD_REQUEST, "Slot exceeds horizon"));
    }

    let assignments = _state.shared_state.get_requests_in_slot(_slot);
    // compute flavour counts
    let flavour_counts: HashMap<String, usize> =
        assignments
            .iter()
            .fold(HashMap::new(), |mut acc, assignment| {
                let flavour = assignment.flavour_name.clone();
                *acc.entry(flavour).or_insert(0) += 1;
                acc
            });

    let total_carbon_cost: f64 = assignments
        .iter()
        .map(|assignment| assignment.carbon_cost)
        .sum();

    let capacity_multiplier: f64 = _state
        .scheduler
        .capacity_tiers
        .iter()
        // find highest tier where assignments.len() <= tier.max_requests
        // max_requests being None means infinite
        .filter(|tier| assignments.len() as i64 <= tier.max_requests.unwrap_or(i64::MAX))
        .map(|tier| tier.multiplier)
        .max_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal))
        .unwrap_or(1.0);

    let actual_carbon_intensity = _state
        .actual_carbon_intensity
        .lock()
        .unwrap()
        .get(&_slot)
        .copied();

    Ok(Json(SlotDetailResponse {
        slot: _slot,
        total_requests: assignments.len(),
        flavour_counts,
        total_carbon_cost,
        capacity_multiplier,
        forecast_carbon_intensity: carbon_forecast.get(_slot as usize).copied(),
        actual_carbon_intensity,
    }))
}

/// `GET /v1/metrics/costs` — fine-grained carbon cost totals and savings.
///
/// Returns:
/// - `current_actual_carbon_cost`: sum of corrected actual carbon costs for completed requests
/// - `current_actual_baseline_carbon_cost`: sum of corrected actual baseline costs for completed requests
/// - `actual_carbon_saving_pct`: percentage saved: `(baseline - actual) / baseline * 100`
/// - `forecasted_pending_carbon_cost`: predicted carbon cost of requests still pending or scheduled for future slots
/// - `total_forecasted_carbon_cost`: total predicted cost of all scheduled requests
/// - `total_baseline_carbon_cost`: total baseline cost of all requests
///
/// An optional `qos_profile_id` restricts request totals to one profile. The
/// returned `capacity_tiers` are always global because slot occupancy is shared.
pub async fn get_cost_metrics(
    State(_state): State<AppState>,
    Query(_query): Query<AssignmentsQuery>,
) -> Result<Json<CostMetricsResponse>, ApiError> {
    let selected_profile = resolve_profile_query(&_state, &_query)?;
    let assignments = _state.shared_state.get_current_assignments();
    let tracked = _state.tracked.lock().unwrap();

    let mut current_actual_carbon_cost = 0.0;
    let mut current_actual_baseline_carbon_cost = 0.0;
    let mut forecasted_pending_carbon_cost = 0.0;
    let mut total_baseline_carbon_cost = 0.0;

    for (req_id, t) in tracked.iter() {
        if selected_profile
            .as_ref()
            .is_some_and(|profile| t.qos_profile_id != profile.profile_id)
        {
            continue;
        }

        total_baseline_carbon_cost += t.baseline_carbon_cost;

        if t.status == RequestStatus::Completed {
            current_actual_baseline_carbon_cost += t.baseline_carbon_cost;
            if let Some(a) = assignments.get(req_id) {
                current_actual_carbon_cost += a.carbon_cost;
            }
        } else if t.status != RequestStatus::Failed {
            if let Some(a) = assignments.get(req_id) {
                forecasted_pending_carbon_cost += a.carbon_cost;
            }
        }
    }

    let actual_carbon_saving_pct = if current_actual_baseline_carbon_cost > 0.0 {
        Some(
            ((current_actual_baseline_carbon_cost - current_actual_carbon_cost)
                / current_actual_baseline_carbon_cost)
                * 100.0,
        )
    } else {
        None
    };

    let total_forecasted_carbon_cost: f64 = assignments
        .values()
        .filter(|assignment| {
            selected_profile.as_ref().map_or(true, |profile| {
                assignment.qos_profile_id == profile.profile_id
            })
        })
        .map(|assignment| assignment.carbon_cost)
        .sum();

    Ok(Json(CostMetricsResponse {
        qos_profile_id: selected_profile.map(|profile| profile.profile_id.to_string()),
        current_actual_carbon_cost,
        current_actual_baseline_carbon_cost,
        actual_carbon_saving_pct,
        forecasted_pending_carbon_cost,
        total_forecasted_carbon_cost,
        total_baseline_carbon_cost,
        capacity_tiers: _state.scheduler.capacity_tiers.clone(),
    }))
}

/// `GET /v1/metrics/error-history` — fleet telemetry or one profile's error
/// history, selected using `qos_profile_id` (`task_id` remains a migration
/// alias). The fleet-wide average is descriptive only, not a cross-profile QoS
/// measure.
pub async fn get_error_history(
    State(_state): State<AppState>,
    Query(_query): Query<AssignmentsQuery>,
) -> Result<Json<ErrorHistoryResponse>, ApiError> {
    let current_slot = _state.shared_state.get_current_slot();
    let horizon = _state.scheduler.total_slots;

    let from = _query.from_slot.unwrap_or(0);
    let to = _query.to_slot.unwrap_or(current_slot);

    if from > to {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "`from_slot` cannot be greater than `to_slot`",
        ));
    }
    if to < 0 || from < 0 {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "`from_slot` and `to_slot` must be >= 0",
        ));
    }
    if to >= horizon {
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "`to_slot` exceeds the horizon",
        ));
    }

    let selected_profile = resolve_profile_query(&_state, &_query)?;
    let selected_profile_id = selected_profile.as_ref().map(|profile| &profile.profile_id);
    let g = _state.shared_state.get_global_error_stats();
    let global_error_avg = if g.count > 0 { Some(g.avg) } else { None };
    let profile_error_stats = selected_profile_id
        .map(|profile_id| _state.shared_state.get_profile_error_stats(profile_id));
    let profile_error_avg = profile_error_stats
        .as_ref()
        .and_then(|stats| (stats.count > 0).then_some(stats.avg));

    let task_thresholds: HashMap<String, f64> = _state
        .qos_profiles
        .list()
        .into_iter()
        .map(|profile| (profile.profile_id.to_string(), profile.max_error_threshold))
        .collect();
    let max_error_threshold = selected_profile
        .as_ref()
        .map(|profile| profile.max_error_threshold)
        .unwrap_or(_state.scheduler.max_error_threshold);
    let error_semantics = selected_profile
        .as_ref()
        .map(|profile| profile.error_semantics.clone());
    let window_past = selected_profile
        .as_ref()
        .map(|profile| profile.error_window.past_slots)
        .unwrap_or(_state.scheduler.error_window_past);
    let window_future = selected_profile
        .as_ref()
        .map(|profile| profile.error_window.future_slots)
        .unwrap_or(_state.scheduler.error_window_future);

    let mut slots = Vec::with_capacity((to - from + 1) as usize);
    let mut running_error_sum = 0.0;
    let mut running_count = 0u64;

    for s in 0..from {
        let stats = if let Some(profile_id) = selected_profile_id {
            _state
                .shared_state
                .get_profile_slot_error_stats(profile_id, s)
        } else {
            _state.shared_state.get_slot_error_stats(s)
        };
        running_error_sum += stats.average * (stats.count as f64);
        running_count += stats.count;
    }

    for slot in from..=to {
        let stats = if let Some(profile_id) = selected_profile_id {
            _state
                .shared_state
                .get_profile_slot_error_stats(profile_id, slot)
        } else {
            _state.shared_state.get_slot_error_stats(slot)
        };
        running_error_sum += stats.average * (stats.count as f64);
        running_count += stats.count;

        let cumulative_error = if running_count > 0 {
            Some(running_error_sum / running_count as f64)
        } else {
            None
        };

        let empty_exclusion = std::collections::HashSet::new();
        let win_stats = if let Some(profile_id) = selected_profile_id {
            _state.shared_state.get_profile_window_error_stats(
                profile_id,
                slot,
                window_past,
                window_future,
                &empty_exclusion,
            )
        } else {
            _state.shared_state.get_window_error_stats(
                slot,
                window_past,
                window_future,
                &empty_exclusion,
            )
        };
        let window_error = if win_stats.count > 0 {
            Some(win_stats.average)
        } else {
            None
        };

        slots.push(SlotErrorItem {
            slot,
            average_error: stats.average,
            request_count: stats.count as usize,
            window_error,
            cumulative_error,
        });
    }

    Ok(Json(ErrorHistoryResponse {
        current_slot,
        qos_profile_id: selected_profile.map(|profile| profile.profile_id.to_string()),
        profile_error_avg,
        error_semantics,
        global_error_avg,
        max_error_threshold,
        task_thresholds,
        window_past,
        window_future,
        slots,
    }))
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
        return Err(api_error(
            StatusCode::BAD_REQUEST,
            "deadline_seconds must be >= 0",
        ));
    }
    if let Some(cb) = &body.callback_url {
        validate_callback_url(cb, state.service_cfg.allow_private_callbacks)
            .map_err(|e| api_error(StatusCode::BAD_REQUEST, e))?;
    }
    let profile = resolve_request_profile(&state, &body)?;
    let profile_id = profile.profile_id.clone();

    let request_id = state.next_request_id();
    let current_slot = state.shared_state.get_current_slot();
    let arrival_slot = match (
        body.arrival_slot_global,
        *state.slot_epoch_offset.lock().unwrap(),
    ) {
        (Some(global), Some(offset)) => {
            let engine = global - offset;
            if engine < 0 || engine >= state.scheduler.total_slots {
                tracing::warn!(
                    global,
                    offset,
                    engine,
                    "arrival_slot_global out of range; falling back to current_slot"
                );
                current_slot
            } else {
                engine as i32
            }
        }
        _ => current_slot, // offset not yet known, or client didn't send it
    };

    let eff_slot_dur = state.scheduler.effective_slot_duration_secs;
    let slots_ahead = ((body.deadline_seconds / eff_slot_dur).ceil() as i32).max(1);
    let deadline_slot = (current_slot + slots_ahead).min(state.scheduler.total_slots - 1);
    let task_flavours = profile.flavours.clone();
    let (baseline_carbon_cost, baseline_duration) =
        compute_baseline_carbon_cost(&state, arrival_slot, &task_flavours);
    let mut executor_payload = body.payload.clone();
    if !executor_payload
        .as_object()
        .is_some_and(|payload| payload.contains_key("task"))
    {
        executor_payload["task"] = serde_json::Value::String(profile.task_kind.to_string());
    }

    state.tracked.lock().unwrap().insert(
        request_id,
        TrackedRequest::new(
            body.callback_url.clone(),
            executor_payload,
            baseline_carbon_cost,
            baseline_duration,
            arrival_slot,
            profile_id.clone(),
        ),
    );
    if body.task_id.is_some() {
        state.legacy_task_id_usage.record_request_submission();
    }

    state
        .shared_state
        .add_request(EngineRequest::new_for_qos_profile(
            request_id,
            arrival_slot,
            deadline_slot,
            profile,
        ));

    // TODO: remove this debug print
    println!("[Service] Submitted request ID: {}", request_id);

    // Poll for the solver's assignment; short sleeps so we don't block the
    // async runtime while waiting for the (std-threaded) engine to catch up.
    let deadline = tokio::time::Instant::now()
        + Duration::from_secs_f64(state.service_cfg.submit_wait_timeout_secs);
    loop {
        if let Some(assignment) = state
            .shared_state
            .get_current_assignments()
            .get(&request_id)
        {
            let eta = ((assignment.scheduled_slot - state.shared_state.get_current_slot()).max(0))
                as f64
                * eff_slot_dur;
            if let Some(t) = state.tracked.lock().unwrap().get_mut(&request_id) {
                t.status = RequestStatus::Scheduled;
            }
            // TODO: remove this debug print
            println!(
                "[Service] Request ID {} scheduled at slot {} / {current_slot}",
                request_id,
                assignment.scheduled_slot,
                current_slot = state.shared_state.get_current_slot()
            );
            return Ok((
                StatusCode::OK,
                Json(RequestStatusResponse {
                    request_id,
                    qos_profile_id: profile_id.to_string(),
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
                    qos_profile_id: profile_id.to_string(),
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
        (
            t.status,
            t.error.clone(),
            t.baseline_carbon_cost,
            t.qos_profile_id.clone(),
        )
    };
    let (status, error, baseline_carbon_cost, qos_profile_id) = tracked_status;

    let assignments = state.shared_state.get_current_assignments();
    let assignment = assignments.get(&request_id);
    let eff_slot_dur = state.scheduler.effective_slot_duration_secs;
    let eta = assignment.map(|a| {
        ((a.scheduled_slot - state.shared_state.get_current_slot()).max(0)) as f64 * eff_slot_dur
    });

    Ok(Json(RequestStatusResponse {
        request_id,
        qos_profile_id: qos_profile_id.to_string(),
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
/// carbon intensity for their slots (if reported via `advance_slot` or
/// `set_carbon_intensity`) AND the *actual* execution time measured by the
/// executor (`execution_time_seconds` and `baseline_execution_time_seconds`).
/// For `carbon_cost`, slot is `assignment.scheduled_slot` and duration is
/// `assignment.flavour_duration`. For `baseline_carbon_cost`, slot is
/// `t.arrival_slot` (the slot where baseline would execute immediately) and
/// duration is `t.baseline_duration`. Both corrected values are forwarded
/// to the caller so it can see the real cost, not just the forecast estimates.
pub async fn executor_callback(
    State(state): State<AppState>,
    Path(request_id): Path<u64>,
    Json(body): Json<ExecutorCallbackPayload>,
) -> Result<StatusCode, ApiError> {
    if let Some(actual_error) = body.result.get("actual_error_pct").and_then(|v| v.as_f64()) {
        // TODO: remove
        // For auditing purposes, we log the diff between the predicted and actual error.
        let predicted_err = state.shared_state.get_error_for_assignment(request_id);
        tracing::info!(
            "Request {}: predicted vs actual error: {:?} vs {:?}",
            request_id,
            predicted_err,
            actual_error
        );

        // Correct the assignment error with the actual error reported by the executor.
        state
            .shared_state
            .correct_assignment_error(request_id, actual_error);
    }

    let actual_execution_time = body
        .result
        .get("execution_time_seconds")
        .and_then(|v| v.as_f64());
    let baseline_execution_time = body
        .result
        .get("baseline_execution_time_seconds")
        .and_then(|v| v.as_f64())
        .or_else(|| {
            body.result
                .get("flavour")
                .and_then(|f| f.as_str())
                .and_then(|f| {
                    if f.eq_ignore_ascii_case("accurate") {
                        actual_execution_time
                    } else {
                        None
                    }
                })
        });

    // The logic behind the formula of actual carbon cost is as follows:
    // actual_carbon_cost = forecast_carbon_cost * (actual_ci / forecast_ci) * (actual_exec_time / forecast_exec_time)
    // being the forecast_carbon_cost = forecast_ci * forecast_exec_time * cap_level_multiplier * hourly_scale
    // we get that the actual carbon cost ultimately is:
    // actual_carbon_cost = actual_ci * actual_exec_time * cap_level_multiplier * hourly_scale
    //
    // The formula is less readable than the direct one, but avoids having here explicit cap_level_multiplier and hourly_scale values.

    let mut actual_carbon_cost = None;
    if let Some(assignment) = state
        .shared_state
        .get_current_assignments()
        .get(&request_id)
    {
        let ci_ratio = state.carbon_intensity_ratio(assignment.scheduled_slot);
        let time_ratio = match (actual_execution_time, assignment.flavour_duration) {
            (Some(t_exec), dur) if dur > 0 => Some(t_exec / dur as f64),
            _ => None,
        };

        if ci_ratio.is_some() || time_ratio.is_some() {
            let cost = assignment.carbon_cost * ci_ratio.unwrap_or(1.0) * time_ratio.unwrap_or(1.0);
            actual_carbon_cost = state
                .shared_state
                .correct_assignment_carbon_cost(request_id, cost);
        }
    }

    let (callback_url, actual_baseline_carbon_cost) = {
        let mut guard = state.tracked.lock().unwrap();
        let t = guard
            .get_mut(&request_id)
            .ok_or_else(|| api_error(StatusCode::NOT_FOUND, "unknown request_id"))?;
        t.status = if body.success {
            RequestStatus::Completed
        } else {
            RequestStatus::Failed
        };
        t.error = body.error.clone();

        let ci_ratio = state.carbon_intensity_ratio(t.arrival_slot);
        let time_ratio = match (baseline_execution_time, t.baseline_duration) {
            (Some(t_exec), dur) if dur > 0 => Some(t_exec / dur as f64),
            _ => None,
        };

        let actual_baseline = if ci_ratio.is_some() || time_ratio.is_some() {
            let cost = t.baseline_carbon_cost * ci_ratio.unwrap_or(1.0) * time_ratio.unwrap_or(1.0);
            t.baseline_carbon_cost = cost;
            Some(cost)
        } else {
            None
        };

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
            execution_time_seconds: actual_execution_time,
            baseline_execution_time_seconds: baseline_execution_time,
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
    use std::sync::RwLock;

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
        use crate::engine::scheduler::generate_carbon_intensity_forecast;
        use crate::engine::shared_state::SharedState;
        use crate::engine::types::Flavour;
        use crate::service::state::ServiceConfig;
        use std::sync::Arc;

        let mut cfg = Config::default();
        // Whichever flavour has the lowest `error` is the baseline,
        // regardless of its name — mirrors the DP/online solvers' own
        // "reference flavour" lookups (see `Config::flavours` doc).
        cfg.flavours = vec![
            Flavour {
                name: "Cheap".to_string(),
                error: 5.0,
                duration: 60,
            },
            Flavour {
                name: "Precise".to_string(),
                error: 0.0,
                duration: 10,
            },
        ];
        let cfg = Arc::new(cfg);
        let forecast = Arc::new(RwLock::new(generate_carbon_intensity_forecast(
            cfg.total_slots as usize,
            12,
            26,
            160.0,
            70.0,
            0.25,
            0.75,
            18.0,
            2.0,
            0.95,
            false,
            false,
        )));
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

        let (baseline, duration) = compute_baseline_carbon_cost(&state, 0, &cfg.flavours);
        let carbon = state.carbon_forecast.read().unwrap()[0];
        let expected = carbon * 10.0 * cfg.carbon_cost_duration_scale; // "Precise"'s duration (lowest error), position 1 => multiplier 1.0
        assert!(
            (baseline - expected).abs() < 1e-9,
            "baseline={baseline}, expected={expected}"
        );
        assert_eq!(duration, 10);
    }

    #[tokio::test]
    async fn carbon_intensity_endpoint_returns_forecast_and_actual_by_slot() {
        use crate::engine::config::Config;
        use crate::engine::shared_state::SharedState;
        use crate::service::state::ServiceConfig;
        use std::sync::Arc;

        let cfg = Arc::new(Config::default());
        let forecast = Arc::new(RwLock::new(vec![100.0, 110.0, 120.0, 130.0]));
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
        let state = AppState::new(SharedState::new(), cfg, service_cfg, forecast);
        state
            .actual_carbon_intensity
            .lock()
            .unwrap()
            .insert(1, 115.0);
        state
            .actual_carbon_intensity
            .lock()
            .unwrap()
            .insert(3, 128.0);

        let response = carbon_intensity(
            State(state),
            Query(CarbonIntensityQuery {
                slot: Some(3),
                until_slot: None,
            }),
        )
        .await;

        let payload = response.0;
        assert_eq!(payload.len(), 4);
        assert_eq!(payload[0]["forecast"], serde_json::json!(100.0));
        assert_eq!(payload[1]["actual"], serde_json::json!(115.0));
        assert_eq!(payload[3]["actual"], serde_json::json!(128.0));
    }
}
