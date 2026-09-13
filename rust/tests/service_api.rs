//! Integration tests for the `service` REST layer: HTTP contract (auth,
//! validation, 404s) via in-process `tower::oneshot` calls against the
//! router, plus one end-to-end test with a real running scheduler.

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use tower::ServiceExt;

use carbonshift_rs::engine::config::Config;
use carbonshift_rs::engine::metrics_logger::MetricsLogger;
use carbonshift_rs::engine::scheduler::BatchScheduler;
use carbonshift_rs::engine::shared_state::SharedState;
use carbonshift_rs::service::server::build_router;
use carbonshift_rs::service::state::{AppState, ServiceConfig};

fn test_service_cfg() -> ServiceConfig {
    ServiceConfig {
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
    }
}

fn test_engine_config() -> Config {
    let mut cfg = Config::default();
    cfg.total_slots = 50;
    cfg.enable_solver_logging = false;
    cfg.enable_infeasibility_debug_logging = false;
    cfg.enable_progress_display = false;
    cfg.verbose = false;
    cfg
}

fn test_state(service_cfg: ServiceConfig) -> AppState {
    let cfg = Arc::new(test_engine_config());
    let forecast = Arc::new(carbonshift_rs::engine::scheduler::generate_carbon_forecast(&cfg));
    AppState::new(SharedState::new(), cfg, service_cfg, forecast)
}

async fn json_body(resp: axum::response::Response) -> serde_json::Value {
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    serde_json::from_slice(&bytes).unwrap()
}

fn json_request(method: &str, uri: &str, body: &str) -> Request<Body> {
    Request::builder()
        .method(method)
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap()
}

#[tokio::test]
async fn health_check_ok() {
    let app = build_router(test_state(test_service_cfg()));
    let resp = app
        .oneshot(Request::builder().uri("/health").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
}

#[tokio::test]
async fn submit_rejects_negative_deadline() {
    let app = build_router(test_state(test_service_cfg()));
    let resp = app
        .oneshot(json_request("POST", "/v1/requests", r#"{"deadline_seconds": -1}"#))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn submit_rejects_private_callback_url_by_default() {
    let app = build_router(test_state(test_service_cfg()));
    let body = r#"{"deadline_seconds": 5, "callback_url": "http://127.0.0.1:9/cb"}"#;
    let resp = app.oneshot(json_request("POST", "/v1/requests", body)).await.unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn submit_allows_private_callback_url_when_flag_set() {
    let mut svc_cfg = test_service_cfg();
    svc_cfg.allow_private_callbacks = true;
    let app = build_router(test_state(svc_cfg));
    let body = r#"{"deadline_seconds": 5, "callback_url": "http://127.0.0.1:9/cb"}"#;
    let resp = app.oneshot(json_request("POST", "/v1/requests", body)).await.unwrap();
    // No scheduler is running in this test, so the solver never assigns a
    // slot; the request is still accepted (202 pending) rather than rejected.
    assert_eq!(resp.status(), StatusCode::ACCEPTED);
}

#[tokio::test]
async fn unknown_request_id_returns_404() {
    let app = build_router(test_state(test_service_cfg()));
    let resp = app
        .oneshot(Request::builder().uri("/v1/requests/999999").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn callback_for_unknown_request_id_returns_404() {
    let app = build_router(test_state(test_service_cfg()));
    let body = r#"{"success": true, "result": {}}"#;
    let resp = app.oneshot(json_request("POST", "/v1/callback/12345", body)).await.unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn api_key_required_when_configured() {
    let mut svc_cfg = test_service_cfg();
    svc_cfg.api_key = Some("secret".to_string());
    let app = build_router(test_state(svc_cfg));

    let no_key = json_request("POST", "/v1/requests", r#"{"deadline_seconds": 5}"#);
    let resp = app.clone().oneshot(no_key).await.unwrap();
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);

    let mut with_key = json_request("POST", "/v1/requests", r#"{"deadline_seconds": 5}"#);
    with_key.headers_mut().insert("x-api-key", "secret".parse().unwrap());
    let resp = app.oneshot(with_key).await.unwrap();
    assert_ne!(resp.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn executor_token_required_when_configured() {
    let mut svc_cfg = test_service_cfg();
    svc_cfg.executor_token = Some("tok".to_string());
    let app = build_router(test_state(svc_cfg));

    let body = r#"{"success": true, "result": {}}"#;
    let no_token = json_request("POST", "/v1/callback/1", body);
    let resp = app.clone().oneshot(no_token).await.unwrap();
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);

    let mut with_token = json_request("POST", "/v1/callback/1", body);
    with_token.headers_mut().insert("x-executor-token", "tok".parse().unwrap());
    let resp = app.oneshot(with_token).await.unwrap();
    // Wrong request id (never submitted) but past the auth gate -> 404, not 401.
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn stats_reports_tracked_request_counts() {
    let app = build_router(test_state(test_service_cfg()));
    app.clone()
        .oneshot(json_request("POST", "/v1/requests", r#"{"deadline_seconds": 5}"#))
        .await
        .unwrap();

    let resp = app.oneshot(Request::builder().uri("/v1/stats").body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let json = json_body(resp).await;
    assert_eq!(json["total"], 1);
}

#[tokio::test]
async fn get_task_config_returns_default_when_not_registered() {
    let app = build_router(test_state(test_service_cfg()));
    let resp = app
        .oneshot(Request::builder().uri("/v1/tasks/never_registered").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let json = json_body(resp).await;
    assert_eq!(json["task_id"], "never_registered");
    assert_eq!(json["max_error_threshold"], test_engine_config().max_error_threshold);
    assert!(json["flavours"].as_array().unwrap().len() >= 1);
}

#[tokio::test]
async fn get_task_config_returns_registered_override() {
    let app = build_router(test_state(test_service_cfg()));
    let body = r#"{"task_id": "custom", "flavours": [{"name": "Only", "error": 20.0, "duration": 5}], "max_error_threshold": 17.5}"#;
    app.clone().oneshot(json_request("POST", "/v1/tasks", body)).await.unwrap();

    let resp = app
        .oneshot(Request::builder().uri("/v1/tasks/custom").body(Body::empty()).unwrap())
        .await
        .unwrap();
    let json = json_body(resp).await;
    assert_eq!(json["max_error_threshold"], 17.5);
    assert_eq!(json["flavours"][0]["name"], "Only");
}

/// Full pipeline with a real scheduler: after a request is scheduled, the
/// scheduler's own global error average must be observable via `/v1/stats`
/// (used to compare against a task's declared `max_error_threshold`).
#[tokio::test]
async fn stats_reports_global_error_avg_after_scheduling() {
    let mut cfg = test_engine_config();
    cfg.batch_size = 1;
    let cfg = Arc::new(cfg);
    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(false, String::new(), String::new(), String::new(), None));
    let mut scheduler = BatchScheduler::new(shared_state.clone(), cfg.clone(), metrics_logger, None);
    scheduler.start();

    let mut svc_cfg = test_service_cfg();
    svc_cfg.submit_wait_timeout_secs = 5.0;
    let forecast = Arc::new(carbonshift_rs::engine::scheduler::generate_carbon_forecast(&cfg));
    let state = AppState::new(shared_state, cfg, svc_cfg, forecast);
    let app = build_router(state);

    app.clone()
        .oneshot(json_request("POST", "/v1/requests", r#"{"deadline_seconds": 5}"#))
        .await
        .unwrap();

    let resp = app.oneshot(Request::builder().uri("/v1/stats").body(Body::empty()).unwrap()).await.unwrap();
    let json = json_body(resp).await;
    assert_eq!(json["global_error_count"], 1);
    assert!(json["global_error_avg"].is_number());

    scheduler.stop();
}

#[tokio::test]
async fn ready_is_ok_at_start_of_horizon() {
    let app = build_router(test_state(test_service_cfg()));
    let resp = app.oneshot(Request::builder().uri("/ready").body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
}

#[tokio::test]
async fn ready_returns_503_near_horizon_exhaustion() {
    let mut svc_cfg = test_service_cfg();
    svc_cfg.horizon_ready_threshold = 0.9;
    let state = test_state(svc_cfg);
    state.shared_state.set_current_slot(46); // 46/50 = 92% > 90% threshold
    let app = build_router(state);
    let resp = app.oneshot(Request::builder().uri("/ready").body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
}

/// Full pipeline with a real (started) scheduler: submit -> the DP solver
/// assigns a slot within the poll window -> `200 scheduled`.
#[tokio::test]
async fn end_to_end_submit_gets_scheduled() {
    let mut cfg = test_engine_config();
    cfg.batch_size = 1; // schedule as soon as one request arrives
    let cfg = Arc::new(cfg);

    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(false, String::new(), String::new(), String::new(), None));
    let mut scheduler = BatchScheduler::new(shared_state.clone(), cfg.clone(), metrics_logger, None);
    scheduler.start();

    let mut svc_cfg = test_service_cfg();
    svc_cfg.submit_wait_timeout_secs = 5.0;
    let forecast = Arc::new(carbonshift_rs::engine::scheduler::generate_carbon_forecast(&cfg));
    let state = AppState::new(shared_state, cfg, svc_cfg, forecast);
    let app = build_router(state);

    let resp = app
        .oneshot(json_request("POST", "/v1/requests", r#"{"deadline_seconds": 5}"#))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let json = json_body(resp).await;
    assert_eq!(json["status"], "scheduled");
    assert!(json["scheduled_slot"].is_number());
    assert!(json["scheduled_at"].is_number(), "scheduled_at should be a unix timestamp, got {:?}", json["scheduled_at"]);

    scheduler.stop();
}

/// `baseline_carbon_cost` reflects the cost of running the request
/// immediately, at arrival, with the most accurate (most expensive) flavour
/// — the reference point the client uses to compute carbon savings.
#[tokio::test]
async fn submit_response_includes_positive_baseline_carbon_cost() {
    let app = build_router(test_state(test_service_cfg()));

    let resp = app
        .oneshot(json_request("POST", "/v1/requests", r#"{"deadline_seconds": 5}"#))
        .await
        .unwrap();
    let json = json_body(resp).await;
    let baseline = json["baseline_carbon_cost"].as_f64().expect("baseline_carbon_cost must be a number");
    assert!(baseline > 0.0, "baseline_carbon_cost should be positive, got {baseline}");
}

/// A request that references a dynamically-registered task must be
/// scheduled with one of *that task's* flavours, not `Config::flavours`
/// (the default task) — this is what lets a client-announced task's
/// error/cost data actually reach the DP solver.
#[tokio::test]
async fn task_flavours_registered_via_v1_tasks_are_used_for_scheduling() {
    let mut cfg = test_engine_config();
    cfg.batch_size = 1;
    let cfg = Arc::new(cfg);

    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(false, String::new(), String::new(), String::new(), None));
    let mut scheduler = BatchScheduler::new(shared_state.clone(), cfg.clone(), metrics_logger, None);
    scheduler.start();

    let mut svc_cfg = test_service_cfg();
    svc_cfg.submit_wait_timeout_secs = 5.0;
    let forecast = Arc::new(carbonshift_rs::engine::scheduler::generate_carbon_forecast(&cfg));
    let state = AppState::new(shared_state, cfg, svc_cfg, forecast);
    let app = build_router(state);

    let register_body = r#"{"task_id": "custom_task", "flavours": [{"name": "OnlyThis", "error": 1.0, "duration": 45}]}"#;
    let resp = app.clone().oneshot(json_request("POST", "/v1/tasks", register_body)).await.unwrap();
    assert_eq!(resp.status(), StatusCode::NO_CONTENT);

    let submit_body = r#"{"deadline_seconds": 5, "task_id": "custom_task"}"#;
    let resp = app.oneshot(json_request("POST", "/v1/requests", submit_body)).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let json = json_body(resp).await;
    assert_eq!(json["status"], "scheduled");
    assert_eq!(json["flavour"], "OnlyThis");

    scheduler.stop();
}

/// An `actual_error_pct` reported via the executor callback must correct the
/// scheduler's global error average (predicted → actual), per-assignment,
/// without changing how many assignments are counted.
#[tokio::test]
async fn executor_callback_with_actual_error_pct_corrects_global_error() {
    let mut cfg = test_engine_config();
    cfg.batch_size = 1;
    let cfg = Arc::new(cfg);

    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(false, String::new(), String::new(), String::new(), None));
    let mut scheduler = BatchScheduler::new(shared_state.clone(), cfg.clone(), metrics_logger, None);
    scheduler.start();

    let mut svc_cfg = test_service_cfg();
    svc_cfg.submit_wait_timeout_secs = 5.0;
    let forecast = Arc::new(carbonshift_rs::engine::scheduler::generate_carbon_forecast(&cfg));
    let state = AppState::new(shared_state.clone(), cfg, svc_cfg, forecast);
    let app = build_router(state);

    let resp = app
        .clone()
        .oneshot(json_request("POST", "/v1/requests", r#"{"deadline_seconds": 5}"#))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let json = json_body(resp).await;
    let request_id = json["request_id"].as_u64().unwrap();

    let stats_before = shared_state.get_global_error_stats();
    assert_eq!(stats_before.count, 1);
    assert!(stats_before.error_sum > 0.0, "expected a nonzero predicted error from the chosen flavour");

    let callback_body = r#"{"success": true, "result": {"actual_error_pct": 0.0}}"#;
    let resp = app
        .oneshot(json_request("POST", &format!("/v1/callback/{request_id}"), callback_body))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let stats_after = shared_state.get_global_error_stats();
    assert_eq!(stats_after.count, 1, "correction must not change the assignment count");
    assert!(
        stats_after.error_sum.abs() < 1e-9,
        "actual_error_pct=0.0 => error_sum should become ~0, got {}",
        stats_after.error_sum
    );

    scheduler.stop();
}

#[tokio::test]
async fn advance_slot_rejects_when_manual_clock_disabled() {
    let app = build_router(test_state(test_service_cfg()));
    let resp = app
        .oneshot(Request::builder().method("POST").uri("/v1/admin/advance-slot").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CONFLICT);
}

/// Full "fake time" protocol: submit under `manual_clock`, then advance —
/// the handler must block until the dispatcher has handed the assignment
/// off to the (dry-run) executor before returning.
#[tokio::test]
async fn advance_slot_moves_clock_and_waits_for_dispatch() {
    let mut cfg = test_engine_config();
    cfg.batch_size = 1;
    cfg.manual_clock = true;
    let cfg = Arc::new(cfg);

    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(false, String::new(), String::new(), String::new(), None));
    let mut scheduler = BatchScheduler::new(shared_state.clone(), cfg.clone(), metrics_logger, None);
    scheduler.start();

    let mut svc_cfg = test_service_cfg();
    svc_cfg.submit_wait_timeout_secs = 5.0;
    svc_cfg.dispatcher_poll_interval_ms = 5;
    let forecast = Arc::new(carbonshift_rs::engine::scheduler::generate_carbon_forecast(&cfg));
    let state = AppState::new(shared_state.clone(), cfg, svc_cfg, forecast);
    tokio::spawn(carbonshift_rs::service::dispatcher::run(state.clone()));
    let app = build_router(state.clone());

    // deadline_seconds=5 with the default 10s slot_duration -> deadline_slot
    // = current_slot + 1, so the DP solver is forced to place it at slot 0
    // or 1 — guaranteed <= 1 after a single advance.
    let resp = app
        .clone()
        .oneshot(json_request("POST", "/v1/requests", r#"{"deadline_seconds": 5}"#))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(json_body(resp).await["status"], "scheduled");
    assert_eq!(shared_state.get_current_slot(), 0);

    let resp = app
        .oneshot(Request::builder().method("POST").uri("/v1/admin/advance-slot").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(json_body(resp).await["current_slot"], 1);

    // The handler only returns once nothing is left `Scheduled` (all handed
    // off to the dry-run executor), so this must already hold true here.
    let no_longer_scheduled = state
        .tracked
        .lock()
        .unwrap()
        .values()
        .all(|t| t.status != carbonshift_rs::service::models::RequestStatus::Scheduled);
    assert!(no_longer_scheduled);

    scheduler.stop();
}

/// Regression test: a request whose tracked status is stuck at `Pending`
/// (its own submit poll timed out before the DP solver assigned it) must
/// still be picked up by the dispatcher once an assignment appears — found
/// via live "fake time" emulation testing, where this is the *common* case
/// (submissions routinely outlive a short poll window, relying on the
/// explicit slot-advance flush instead of the synchronous poll).
#[tokio::test]
async fn dispatcher_picks_up_requests_stuck_pending_after_poll_timeout() {
    let mut cfg = test_engine_config();
    cfg.manual_clock = true;
    let cfg = Arc::new(cfg);

    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(false, String::new(), String::new(), String::new(), None));
    let mut scheduler = BatchScheduler::new(shared_state.clone(), cfg.clone(), metrics_logger, None);
    scheduler.start();

    let mut svc_cfg = test_service_cfg();
    svc_cfg.submit_wait_timeout_secs = 0.02; // times out well before any flush/advance
    svc_cfg.dispatcher_poll_interval_ms = 5;
    let forecast = Arc::new(carbonshift_rs::engine::scheduler::generate_carbon_forecast(&cfg));
    let state = AppState::new(shared_state.clone(), cfg, svc_cfg, forecast);
    tokio::spawn(carbonshift_rs::service::dispatcher::run(state.clone()));
    let app = build_router(state.clone());

    // First request: the scheduler's own startup quirk (current_slot(0) >
    // last_flush_slot(-1)) flushes it almost instantly, so it legitimately
    // becomes `scheduled` within the poll window.
    let resp = app
        .clone()
        .oneshot(json_request("POST", "/v1/requests", r#"{"deadline_seconds": 5}"#))
        .await
        .unwrap();
    assert_eq!(json_body(resp).await["status"], "scheduled");

    // Second request: no more automatic flush until the slot advances, so
    // the poll times out and it's left `pending`.
    let resp = app
        .clone()
        .oneshot(json_request("POST", "/v1/requests", r#"{"deadline_seconds": 5}"#))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::ACCEPTED);
    assert_eq!(json_body(resp).await["status"], "pending");

    // Advancing the slot flushes + assigns it; the dispatcher must still
    // deliver it despite the tracked status never having become `Scheduled`.
    let resp = app
        .oneshot(Request::builder().method("POST").uri("/v1/admin/advance-slot").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let all_dispatched = state
        .tracked
        .lock()
        .unwrap()
        .values()
        .all(|t| t.status == carbonshift_rs::service::models::RequestStatus::Dispatched);
    assert!(all_dispatched, "both requests should have been dispatched, including the one stuck Pending");

    scheduler.stop();
}
