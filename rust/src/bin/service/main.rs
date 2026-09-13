//! `carbonshift-service` — dockerizable REST front-end for the CarbonShift
//! scheduling engine. See `rust/PLAN_SERVICE.md` for the architecture.
//!
//! Configuration is via environment variables (kept simple for containers):
//! - `LISTEN_ADDR` (default `0.0.0.0:8080`): HTTP bind address.
//! - `SELF_BASE_URL` (default `http://LISTEN_ADDR`): base URL this service is
//!   reachable at, used to build the executor callback URL.
//! - `EXECUTOR_URL` (optional): base URL of the downstream executor that
//!   receives dispatched jobs. Unset = dry-run mode (nothing is sent; used
//!   for tests).
//! - `SUBMIT_WAIT_TIMEOUT_SECS` (default `5`): how long `POST /v1/requests`
//!   waits for the solver to assign a slot before replying `202 pending`.
//! - `TOTAL_SLOTS` (default `8640`, i.e. 24h at 10s/slot): planning horizon
//!   size. The engine currently assumes a bounded horizon (see PLAN_SERVICE.md
//!   §Known limitations) — pick a value large enough for how long the
//!   service will run before a restart.
//! - `CARBONSHIFT_API_KEY` (optional): if set, `/v1/requests*` require an
//!   `X-API-Key` header matching this value.
//! - `CARBONSHIFT_EXECUTOR_TOKEN` (optional): if set, `/v1/callback/{id}`
//!   requires an `X-Executor-Token` header matching this value.
//! - `CARBONSHIFT_ALLOW_PRIVATE_CALLBACKS` (default `0`): allow
//!   `callback_url`s pointing at loopback/private addresses (dev/test only).
//! - `EXECUTOR_MAX_RETRIES` (default `5`), `EXECUTOR_RETRY_BASE_MS` (default
//!   `500`), `EXECUTOR_RETRY_MAX_MS` (default `30000`): dispatcher retry
//!   policy (exponential backoff) before giving up and marking a request
//!   `Failed`.
//! - `HORIZON_READY_THRESHOLD` (default `0.9`): fraction of `TOTAL_SLOTS`
//!   elapsed beyond which `GET /ready` returns `503`, so an orchestrator can
//!   roll a replacement instance before the finite planning horizon runs out
//!   (see PLAN_SERVICE.md Fase 6 — there is no in-engine rolling window).
//! - `SLOT_DURATION_SECONDS` (default `10`): length of one planning slot.
//!   Set to e.g. `1800` for 30-minute timeslots.
//! - `MANUAL_CLOCK` (default `0`): freezes the virtual clock except via
//!   `POST /v1/admin/advance-slot` — test/emulation mode only, see
//!   PLAN_SERVICE.md §"Emulazione a tempo fittizio".
//! - `DISPATCHER_POLL_INTERVAL_MS` (default `200`): how often the dispatcher
//!   checks for assignments ready to send to the executor; lower it (e.g.
//!   `20`) for snappier emulation tests.
//!
//! Shuts down gracefully on SIGINT or SIGTERM (the latter is what
//! `docker stop`/Kubernetes send), letting in-flight requests finish.

use std::sync::Arc;

use carbonshift_rs::engine::config::Config;
use carbonshift_rs::engine::metrics_logger::MetricsLogger;
use carbonshift_rs::engine::scheduler::BatchScheduler;
use carbonshift_rs::engine::shared_state::SharedState;
use carbonshift_rs::service::dispatcher;
use carbonshift_rs::service::server::build_router;
use carbonshift_rs::service::state::{AppState, ServiceConfig};

fn env_or(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
}

fn env_secret(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|s| !s.trim().is_empty())
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();

    let listen_addr = env_or("LISTEN_ADDR", "0.0.0.0:8080");
    let self_base_url = env_or("SELF_BASE_URL", &format!("http://{listen_addr}"));
    let executor_url = env_secret("EXECUTOR_URL");
    let submit_wait_timeout_secs: f64 = env_or("SUBMIT_WAIT_TIMEOUT_SECS", "5")
        .parse()
        .expect("SUBMIT_WAIT_TIMEOUT_SECS must be a number");
    let total_slots: i32 = env_or("TOTAL_SLOTS", "8640")
        .parse()
        .expect("TOTAL_SLOTS must be an integer");
    let slot_duration_seconds: f64 = env_or("SLOT_DURATION_SECONDS", "10")
        .parse()
        .expect("SLOT_DURATION_SECONDS must be a number");
    let manual_clock = env_or("MANUAL_CLOCK", "0") == "1";

    let mut cfg = Config::default();
    cfg.total_slots = total_slots;
    cfg.slot_duration_seconds = slot_duration_seconds;
    // Live service: the slot clock must track wall-clock time, never skip
    // ahead (skip_empty_slots is only correct for finite offline replays).
    cfg.skip_empty_slots = false;
    cfg.slot_speed_scale = 1.0;
    cfg.manual_clock = manual_clock;
    cfg.verbose = false;
    cfg.enable_progress_display = false;
    cfg.enable_solver_logging = env_or("CARBONSHIFT_ENABLE_SOLVER_LOGGING", "0") == "1";
    cfg.enable_infeasibility_debug_logging = false;
    let cfg = Arc::new(cfg);

    match &executor_url {
        Some(url) => tracing::info!(executor_url = %url, "executor dispatch enabled"),
        None => tracing::warn!("EXECUTOR_URL not set — running in dry-run mode (no dispatch will be sent)"),
    }
    if manual_clock {
        tracing::warn!(
            "MANUAL_CLOCK=1: virtual clock frozen except via POST /v1/admin/advance-slot \
             (test/emulation mode only — never enable in production)"
        );
    }
    if env_secret("CARBONSHIFT_API_KEY").is_none() {
        tracing::warn!("CARBONSHIFT_API_KEY not set — /v1/requests* is unauthenticated");
    }
    if env_secret("CARBONSHIFT_EXECUTOR_TOKEN").is_none() {
        tracing::warn!("CARBONSHIFT_EXECUTOR_TOKEN not set — /v1/callback/{{id}} is unauthenticated");
    }

    let shared_state = SharedState::new();
    let carbon_forecast = Arc::new(carbonshift_rs::engine::scheduler::generate_carbon_forecast(&cfg));
    let metrics_logger = Arc::new(MetricsLogger::new(
        cfg.enable_solver_logging,
        cfg.solver_runs_file.clone(),
        cfg.solver_assignments_file.clone(),
        cfg.solver_slot_metrics_file.clone(),
        None,
    ));

    // No RequestGenerator: HTTP submissions feed `shared_state` directly.
    let mut scheduler = BatchScheduler::new(
        shared_state.clone(),
        cfg.clone(),
        metrics_logger,
        Some((*carbon_forecast).clone()),
    );
    scheduler.start();

    let service_cfg = ServiceConfig {
        executor_url,
        self_base_url,
        submit_wait_timeout_secs,
        allow_private_callbacks: env_or("CARBONSHIFT_ALLOW_PRIVATE_CALLBACKS", "0") == "1",
        api_key: env_secret("CARBONSHIFT_API_KEY"),
        executor_token: env_secret("CARBONSHIFT_EXECUTOR_TOKEN"),
        executor_max_retries: env_or("EXECUTOR_MAX_RETRIES", "5").parse().expect("EXECUTOR_MAX_RETRIES must be an integer"),
        executor_retry_base_ms: env_or("EXECUTOR_RETRY_BASE_MS", "500").parse().expect("EXECUTOR_RETRY_BASE_MS must be an integer"),
        executor_retry_max_ms: env_or("EXECUTOR_RETRY_MAX_MS", "30000").parse().expect("EXECUTOR_RETRY_MAX_MS must be an integer"),
        horizon_ready_threshold: env_or("HORIZON_READY_THRESHOLD", "0.9").parse().expect("HORIZON_READY_THRESHOLD must be a number"),
        dispatcher_poll_interval_ms: env_or("DISPATCHER_POLL_INTERVAL_MS", "200").parse().expect("DISPATCHER_POLL_INTERVAL_MS must be an integer"),
    };
    let state = AppState::new(shared_state, cfg, service_cfg, carbon_forecast);
    tokio::spawn(dispatcher::run(state.clone()));

    let app = build_router(state);
    let listener = tokio::net::TcpListener::bind(&listen_addr)
        .await
        .unwrap_or_else(|e| panic!("cannot bind {listen_addr}: {e}"));
    tracing::info!(%listen_addr, "carbonshift-service listening");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .expect("server error");

    scheduler.stop();
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c().await.expect("failed to install Ctrl-C handler");
    };

    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => tracing::info!("SIGINT received"),
        _ = terminate => tracing::info!("SIGTERM received"),
    }
}
