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
    AppState::new(SharedState::new(), Arc::new(test_engine_config()), service_cfg)
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
    let state = AppState::new(shared_state, cfg, svc_cfg);
    let app = build_router(state);

    let resp = app
        .oneshot(json_request("POST", "/v1/requests", r#"{"deadline_seconds": 5}"#))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let json = json_body(resp).await;
    assert_eq!(json["status"], "scheduled");
    assert!(json["scheduled_slot"].is_number());

    scheduler.stop();
}
