//! Shared application state for the REST service.

use std::collections::HashMap;
use std::fmt;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::Instant;

use crate::engine::config::Config;
use crate::engine::qos::{
    CumulativeErrorPolicy, DEFAULT_TASK_ERROR_SEMANTICS, ErrorWindowPolicy, QosProfile,
    QosProfileId, TaskKindId,
};
use crate::engine::shared_state::SharedState;
use crate::engine::types::Flavour;
use crate::service::models::RequestStatus;

/// Per-request bookkeeping that lives outside the scheduling engine: the
/// caller's callback URL, the opaque payload to forward to the executor, and
/// where the request currently stands in the dispatch/callback lifecycle.
/// The engine (`SharedState`) only knows about `Request`/`Assignment`; it has
/// no notion of HTTP callbacks.
pub struct TrackedRequest {
    pub callback_url: Option<String>,
    pub payload: serde_json::Value,
    pub status: RequestStatus,
    pub error: Option<String>,
    /// Number of failed dispatch attempts to the executor so far.
    pub dispatch_attempts: u32,
    /// Backoff gate: the dispatcher skips this request until this instant.
    pub next_attempt_at: Option<Instant>,
    /// What this request would have cost with the "no carbonshift" baseline:
    /// most-accurate flavour, executed immediately at its arrival slot (see
    /// `handlers::compute_baseline_carbon_cost`). Computed once at submit
    /// time so it stays comparable even after the real assignment changes.
    pub baseline_carbon_cost: f64,
    /// Duration in seconds of the accurate flavour used to compute baseline_carbon_cost.
    pub baseline_duration: i32,
    /// Slot the request arrived at — needed to look up the *actual* (not
    /// forecast) carbon intensity for that slot once known, to correct
    /// `baseline_carbon_cost` the same way `Assignment::carbon_cost` gets
    /// corrected (see `executor_callback`).
    pub arrival_slot: i32,
    /// QoS budget identity selected when the request was accepted.
    pub qos_profile_id: QosProfileId,
}

impl TrackedRequest {
    pub fn new(
        callback_url: Option<String>,
        payload: serde_json::Value,
        baseline_carbon_cost: f64,
        baseline_duration: i32,
        arrival_slot: i32,
        qos_profile_id: QosProfileId,
    ) -> Self {
        Self {
            callback_url,
            payload,
            status: RequestStatus::Pending,
            error: None,
            dispatch_attempts: 0,
            next_attempt_at: None,
            baseline_carbon_cost,
            baseline_duration,
            arrival_slot,
            qos_profile_id,
        }
    }
}

/// HTTP/service-layer knobs, kept separate from the engine's `Config` since
/// they govern the REST/dispatch machinery rather than scheduling itself.
#[derive(Clone)]
pub struct ServiceConfig {
    /// Base URL of the external executor. `None` = dry-run: the dispatcher
    /// logs what it would have sent instead of making a request.
    pub executor_url: Option<String>,
    /// Base URL this service is reachable at, used to build the
    /// `callback_url` handed to the executor (e.g. `http://localhost:8080`).
    pub self_base_url: String,
    /// How long `POST /v1/requests` waits for the solver to assign a slot
    /// before returning a `pending` response instead of `scheduled`.
    pub submit_wait_timeout_secs: f64,
    /// Allow `callback_url`s pointing at loopback/private addresses
    /// (otherwise rejected as a basic SSRF guard). Only for local testing.
    pub allow_private_callbacks: bool,
    /// If set, `POST /v1/requests` and `GET /v1/requests/{id}` require an
    /// `X-API-Key` header matching this value.
    pub api_key: Option<String>,
    /// If set, `POST /v1/callback/{id}` requires an `X-Executor-Token`
    /// header matching this value.
    pub executor_token: Option<String>,
    /// Give up on dispatching a request to the executor after this many
    /// failed attempts, marking it `Failed`.
    pub executor_max_retries: u32,
    /// Exponential backoff base delay between dispatch retries.
    pub executor_retry_base_ms: u64,
    /// Cap on the exponential backoff delay.
    pub executor_retry_max_ms: u64,
    /// Fraction of `Config::total_slots` used (by wall-clock elapsed time)
    /// beyond which `GET /ready` starts returning 503, signalling an
    /// orchestrator to roll a replacement instance before the horizon is
    /// exhausted (see PLAN_SERVICE.md Fase 6).
    pub horizon_ready_threshold: f64,
    /// How often the dispatcher polls for newly-committed assignments ready
    /// to send to the executor. Lower this (e.g. to 10-20ms) for snappier
    /// "fake time" emulation tests; the default is fine for real-time use.
    pub dispatcher_poll_interval_ms: u64,
}

/// The subset of scheduler configuration needed by the HTTP service.
#[derive(Clone)]
pub struct ServiceSchedulerConfig {
    /// Number of slots in the scheduler's finite planning horizon.
    pub total_slots: i32,
    /// Effective wall-clock duration of one slot after speed scaling.
    pub effective_slot_duration_secs: f64,
    /// Whether slot advancement is controlled through the manual-clock API.
    pub manual_clock: bool,
    /// Default execution flavours for requests without a task override.
    pub flavours: Vec<Flavour>,
    /// Conversion factor from intensity and duration to carbon cost.
    pub carbon_cost_duration_scale: f64,
    /// Global maximum average error accepted by the scheduler.
    pub max_error_threshold: f64,
    /// Number of preceding slots included in the error window.
    pub error_window_past: i32,
    /// Number of following slots included in the error window.
    pub error_window_future: i32,
    /// Additional preceding slots included with linear decay.
    pub error_window_past_decay_slots: i32,
    /// Whether to enforce a per-profile cumulative error constraint.
    pub cumulative_error_enabled: bool,
    /// Whether the per-profile cumulative error constraint is hard.
    pub cumulative_error_hard: bool,
    /// Maximum number of slots into the future for an assignment.
    pub assignment_max_future_slots: i32,
}

/// Outcome of registering a profile under a stable shared ID.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProfileRegistration {
    Created,
    AlreadyRegistered,
}

/// Invalid profile data or conflicting reuse of an existing profile ID.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProfileRegistryError {
    Invalid(String),
    Conflict(QosProfileId),
}

impl fmt::Display for ProfileRegistryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid(message) => f.write_str(message),
            Self::Conflict(profile_id) => {
                write!(
                    f,
                    "profile_id {profile_id} is already registered with different settings"
                )
            }
        }
    }
}

/// In-memory registry for reusable QoS profiles.
///
/// Registration is idempotent for identical definitions and immutable for a
/// given ID, preventing one client from silently changing another client's
/// budget or the interpretation of existing assignments.
#[derive(Clone)]
pub struct QosProfileRegistry {
    profiles: Arc<RwLock<HashMap<QosProfileId, QosProfile>>>,
}

impl QosProfileRegistry {
    fn from_config(cfg: &Config) -> Self {
        let profiles = DEFAULT_TASK_ERROR_SEMANTICS
            .iter()
            .map(|(task_kind_name, error_semantics)| {
                let task_kind =
                    TaskKindId::parse(*task_kind_name).expect("valid default task kind");
                let task_suffix = task_kind.as_str().replace('_', "-");
                let profile_id = QosProfileId::parse(format!("default-{task_suffix}"))
                    .expect("valid default profile ID");
                let profile = QosProfile {
                    profile_id: profile_id.clone(),
                    task_kind,
                    flavours: cfg.flavours.clone(),
                    error_semantics: (*error_semantics).to_string(),
                    max_error_threshold: cfg.max_error_threshold,
                    error_window: ErrorWindowPolicy {
                        past_slots: cfg.error_window_past,
                        future_slots: cfg.error_window_future,
                        past_decay_slots: cfg.error_window_past_decay_slots,
                    },
                    cumulative_error: CumulativeErrorPolicy {
                        enabled: cfg.global_error_constraint_enabled,
                        hard: cfg.global_error_constraint_hard,
                    },
                };
                (profile_id, profile)
            })
            .collect();
        Self {
            profiles: Arc::new(RwLock::new(profiles)),
        }
    }

    pub fn register(
        &self,
        profile: QosProfile,
    ) -> Result<ProfileRegistration, ProfileRegistryError> {
        QosProfileId::parse(profile.profile_id.as_str().to_string())
            .map_err(ProfileRegistryError::Invalid)?;
        profile.validate().map_err(ProfileRegistryError::Invalid)?;

        let mut profiles = self.profiles.write().unwrap();
        match profiles.get(&profile.profile_id) {
            Some(existing) if existing == &profile => Ok(ProfileRegistration::AlreadyRegistered),
            Some(_) => Err(ProfileRegistryError::Conflict(profile.profile_id)),
            None => {
                profiles.insert(profile.profile_id.clone(), profile);
                Ok(ProfileRegistration::Created)
            }
        }
    }

    pub fn get(&self, profile_id: &QosProfileId) -> Option<QosProfile> {
        self.profiles.read().unwrap().get(profile_id).cloned()
    }

    pub fn get_by_str(&self, profile_id: &str) -> Result<Option<QosProfile>, String> {
        let profile_id = QosProfileId::parse(profile_id.to_string())?;
        Ok(self.get(&profile_id))
    }

    pub fn default_for_task_kind(&self, task_kind: &TaskKindId) -> Option<QosProfile> {
        let suffix = task_kind.as_str().replace('_', "-");
        let profile_id = QosProfileId::parse(format!("default-{suffix}")).ok()?;
        self.get(&profile_id)
    }

    pub fn list(&self) -> Vec<QosProfile> {
        let mut profiles: Vec<_> = self.profiles.read().unwrap().values().cloned().collect();
        profiles.sort_by(|left, right| left.profile_id.cmp(&right.profile_id));
        profiles
    }
}

impl ServiceSchedulerConfig {
    fn from_config(cfg: &Config) -> Self {
        Self {
            total_slots: cfg.total_slots,
            effective_slot_duration_secs: cfg.effective_slot_duration_secs(),
            manual_clock: cfg.simulation.manual_clock,
            flavours: cfg.flavours.clone(),
            carbon_cost_duration_scale: cfg.carbon_cost_duration_scale,
            max_error_threshold: cfg.max_error_threshold,
            error_window_past: cfg.error_window_past,
            error_window_future: cfg.error_window_future,
            error_window_past_decay_slots: cfg.error_window_past_decay_slots,
            cumulative_error_enabled: cfg.global_error_constraint_enabled,
            cumulative_error_hard: cfg.global_error_constraint_hard,
            assignment_max_future_slots: cfg.assignment_max_future_slots,
        }
    }
}

#[derive(Clone)]
pub struct AppState {
    pub shared_state: SharedState,
    pub scheduler: ServiceSchedulerConfig,
    pub qos_profiles: QosProfileRegistry,
    pub http: reqwest::Client,
    pub service_cfg: Arc<ServiceConfig>,
    pub tracked: Arc<Mutex<HashMap<u64, TrackedRequest>>>,
    /// Precomputed carbon-intensity forecast (index = slot), used only to
    /// compute `TrackedRequest::baseline_carbon_cost` — the real scheduling
    /// decision is entirely the engine's own concern.
    pub carbon_forecast: Arc<RwLock<Vec<f64>>>,
    /// How many requests have arrived at each slot so far, used to give the
    /// hypothetical baseline the same per-slot capacity-tier repricing an
    /// immediate/no-batching execution would have faced.
    pub baseline_slot_counts: Arc<Mutex<HashMap<i32, i64>>>,
    // TODO: actually now we have the provider...
    /// Real (not forecast) carbon intensity per slot, reported by the client
    /// piggybacked on `POST /v1/admin/advance-slot` (see
    /// `handlers::advance_slot`). Used only to correct already-committed
    /// `carbon_cost`/`baseline_carbon_cost` after the fact (see
    /// `handlers::executor_callback`) — never fed back into the live DP
    /// solver, which keeps planning against the original forecast.
    pub actual_carbon_intensity: Arc<Mutex<HashMap<i32, f64>>>,

    /// This is the offset between the provider's global slot numbering (based on the fixed epoch)
    /// and carbonshift's local slot numbering.
    /// We cannot use it inside cfg because cfg is shared and immutable, while this
    /// offset is discovered at runtime
    /// carbonshift's `current_slot` starts at **0** and counts up from process start
    /// (`virtual_elapsed_ms` is an uptime counter). The provider publishes **global**
    /// slots aligned to a fixed epoch — measured live, `118015` right now. The two are
    /// off by the process-uptime offset, so a pushed forecast indexed by global slot
    /// would be read by the scheduler at the wrong index — and because it would land
    /// outside its array it would be **silently ignored**, not obviously broken.
    pub slot_epoch_offset: Arc<Mutex<Option<i32>>>,
    next_id: Arc<AtomicU64>,
}

impl AppState {
    pub fn new(
        shared_state: SharedState,
        cfg: Arc<Config>,
        service_cfg: ServiceConfig,
        carbon_forecast: Arc<RwLock<Vec<f64>>>,
    ) -> Self {
        // Tests and integrations can construct AppState without a live
        // BatchScheduler. Initialize the shared policy here too; in the
        // service binary the scheduler constructor has already done so.
        shared_state.initialize_capacity_tiers(cfg.capacity_tiers.clone());
        let qos_profiles = QosProfileRegistry::from_config(&cfg);
        Self {
            slot_epoch_offset: Arc::new(Mutex::new(None)),
            shared_state,
            scheduler: ServiceSchedulerConfig::from_config(&cfg),
            qos_profiles,
            http: reqwest::Client::new(),
            service_cfg: Arc::new(service_cfg),
            tracked: Arc::new(Mutex::new(HashMap::new())),
            carbon_forecast,
            baseline_slot_counts: Arc::new(Mutex::new(HashMap::new())),
            actual_carbon_intensity: Arc::new(Mutex::new(HashMap::new())),
            next_id: Arc::new(AtomicU64::new(1)),
        }
    }

    pub fn next_request_id(&self) -> u64 {
        self.next_id.fetch_add(1, Ordering::Relaxed)
    }

    /// Ratio of actual-to-forecast carbon intensity at `slot`, if the client
    /// ever reported an actual reading for it — `None` otherwise (nothing
    /// to correct with).
    pub fn carbon_intensity_ratio(&self, slot: i32) -> Option<f64> {
        let actual = *self.actual_carbon_intensity.lock().unwrap().get(&slot)?;
        let forecast = *self.carbon_forecast.read().unwrap().get(slot as usize)?;
        if forecast <= 0.0 {
            None
        } else {
            Some(actual / forecast)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::config::Config;
    use crate::types::CapacityTier;

    #[test]
    fn service_scheduler_projection_preserves_custom_runtime_settings() {
        let mut cfg = Config::default();
        cfg.total_slots = 37;
        cfg.slot_duration_seconds = 120.0;
        cfg.simulation.slot_speed_scale = 0.25;
        cfg.simulation.manual_clock = true;
        cfg.flavours = vec![Flavour {
            name: "ServiceSentinel".to_string(),
            error: 3.75,
            duration: 91,
        }];
        cfg.capacity_tiers = vec![
            CapacityTier {
                max_requests: Some(9),
                multiplier: 2.25,
            },
            CapacityTier {
                max_requests: None,
                multiplier: 7.0,
            },
        ];
        cfg.carbon_cost_duration_scale = 0.375;
        cfg.max_error_threshold = 8.25;
        cfg.error_window_past = 5;
        cfg.error_window_future = 7;
        cfg.assignment_max_future_slots = 11;

        let service = ServiceSchedulerConfig::from_config(&cfg);

        assert_eq!(service.total_slots, 37);
        assert_eq!(service.effective_slot_duration_secs, 30.0);
        assert!(service.manual_clock);
        assert_eq!(service.flavours.len(), 1);
        assert_eq!(service.flavours[0].name, "ServiceSentinel");
        assert_eq!(service.flavours[0].error, 3.75);
        assert_eq!(service.flavours[0].duration, 91);
        assert_eq!(service.carbon_cost_duration_scale, 0.375);
        assert_eq!(service.max_error_threshold, 8.25);
        assert_eq!(service.error_window_past, 5);
        assert_eq!(service.error_window_future, 7);
        assert_eq!(
            service.error_window_past_decay_slots,
            cfg.error_window_past_decay_slots
        );
        assert_eq!(
            service.cumulative_error_enabled,
            cfg.global_error_constraint_enabled
        );
        assert_eq!(
            service.cumulative_error_hard,
            cfg.global_error_constraint_hard
        );
        assert_eq!(service.assignment_max_future_slots, 11);
    }
}
