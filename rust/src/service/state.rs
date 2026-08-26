//! Shared application state for the REST service.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use crate::engine::config::Config;
use crate::engine::shared_state::SharedState;
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
}

impl TrackedRequest {
    pub fn new(callback_url: Option<String>, payload: serde_json::Value) -> Self {
        Self {
            callback_url,
            payload,
            status: RequestStatus::Pending,
            error: None,
            dispatch_attempts: 0,
            next_attempt_at: None,
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
}

#[derive(Clone)]
pub struct AppState {
    pub shared_state: SharedState,
    pub cfg: Arc<Config>,
    pub http: reqwest::Client,
    pub service_cfg: Arc<ServiceConfig>,
    pub tracked: Arc<Mutex<HashMap<u64, TrackedRequest>>>,
    next_id: Arc<AtomicU64>,
}

impl AppState {
    pub fn new(shared_state: SharedState, cfg: Arc<Config>, service_cfg: ServiceConfig) -> Self {
        Self {
            shared_state,
            cfg,
            http: reqwest::Client::new(),
            service_cfg: Arc::new(service_cfg),
            tracked: Arc::new(Mutex::new(HashMap::new())),
            next_id: Arc::new(AtomicU64::new(1)),
        }
    }

    pub fn next_request_id(&self) -> u64 {
        self.next_id.fetch_add(1, Ordering::Relaxed)
    }
}
