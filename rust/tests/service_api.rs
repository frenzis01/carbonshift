//! Integration tests for the `service` REST layer: HTTP contract (auth,
//! validation, 404s) via in-process `tower::oneshot` calls against the
//! router, plus one end-to-end test with a real running scheduler.

use std::sync::{Arc, RwLock};
use std::time::Duration;

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
    cfg.logging.enable_solver_logging = false;
    cfg.logging.enable_infeasibility_debug_logging = false;
    cfg.logging.enable_progress_display = false;
    cfg.logging.verbose = false;
    cfg
}

fn test_forecast(total_slots: i32) -> Arc<RwLock<Vec<f64>>> {
    let forecast = carbonshift_rs::engine::scheduler::generate_carbon_intensity_forecast(
        total_slots as usize,
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
    );
    Arc::new(RwLock::new(forecast))
}

fn test_state(service_cfg: ServiceConfig) -> AppState {
    let cfg = Arc::new(test_engine_config());
    let forecast = test_forecast(cfg.total_slots);
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

fn qos_profile_payload(profile_id: &str, threshold: f64) -> String {
    serde_json::json!({
        "profile_id": profile_id,
        "task_kind": "question_answering",
        "flavours": [
            {"name": "Accurate", "error": 2.0, "duration": 120},
            {"name": "Fast", "error": 15.0, "duration": 30}
        ],
        "error_semantics": "word-overlap-f1-v1",
        "max_error_threshold": threshold,
        "error_window": {
            "past_slots": 3,
            "future_slots": 4,
            "past_decay_slots": 2
        },
        "cumulative_error": {
            "enabled": true,
            "hard": false
        }
    })
    .to_string()
}

#[tokio::test]
async fn health_check_ok() {
    let app = build_router(test_state(test_service_cfg()));
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/health")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
}

#[tokio::test]
async fn submit_rejects_negative_deadline() {
    let app = build_router(test_state(test_service_cfg()));
    let resp = app
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds": -1}"#,
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn submit_rejects_private_callback_url_by_default() {
    let app = build_router(test_state(test_service_cfg()));
    let body = r#"{"deadline_seconds": 5, "callback_url": "http://127.0.0.1:9/cb"}"#;
    let resp = app
        .oneshot(json_request("POST", "/v1/requests", body))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn submit_allows_private_callback_url_when_flag_set() {
    let mut svc_cfg = test_service_cfg();
    svc_cfg.allow_private_callbacks = true;
    let app = build_router(test_state(svc_cfg));
    let body = r#"{"deadline_seconds": 5, "callback_url": "http://127.0.0.1:9/cb"}"#;
    let resp = app
        .oneshot(json_request("POST", "/v1/requests", body))
        .await
        .unwrap();
    // No scheduler is running in this test, so the solver never assigns a
    // slot; the request is still accepted (202 pending) rather than rejected.
    assert_eq!(resp.status(), StatusCode::ACCEPTED);
}

#[tokio::test]
async fn unknown_request_id_returns_404() {
    let app = build_router(test_state(test_service_cfg()));
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/v1/requests/999999")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn callback_for_unknown_request_id_returns_404() {
    let app = build_router(test_state(test_service_cfg()));
    let body = r#"{"success": true, "result": {}}"#;
    let resp = app
        .oneshot(json_request("POST", "/v1/callback/12345", body))
        .await
        .unwrap();
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
    with_key
        .headers_mut()
        .insert("x-api-key", "secret".parse().unwrap());
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
    with_token
        .headers_mut()
        .insert("x-executor-token", "tok".parse().unwrap());
    let resp = app.oneshot(with_token).await.unwrap();
    // Wrong request id (never submitted) but past the auth gate -> 404, not 401.
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn stats_reports_tracked_request_counts() {
    let app = build_router(test_state(test_service_cfg()));
    app.clone()
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds": 5}"#,
        ))
        .await
        .unwrap();

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/v1/stats")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let json = json_body(resp).await;
    assert_eq!(json["total"], 1);
}

#[tokio::test]
async fn get_task_config_returns_default_when_not_registered() {
    let app = build_router(test_state(test_service_cfg()));
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/v1/tasks/never_registered")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let json = json_body(resp).await;
    assert_eq!(json["task_id"], "never_registered");
    assert_eq!(
        json["max_error_threshold"],
        test_engine_config().max_error_threshold
    );
    assert!(json["flavours"].as_array().unwrap().len() >= 1);
}

#[tokio::test]
async fn get_task_config_returns_registered_override() {
    let app = build_router(test_state(test_service_cfg()));
    let body = r#"{"task_id": "custom", "flavours": [{"name": "Only", "error": 20.0, "duration": 5}], "max_error_threshold": 17.5}"#;
    app.clone()
        .oneshot(json_request("POST", "/v1/tasks", body))
        .await
        .unwrap();

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/v1/tasks/custom")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let json = json_body(resp).await;
    assert_eq!(json["max_error_threshold"], 17.5);
    assert_eq!(json["flavours"][0]["name"], "Only");
}

#[tokio::test]
async fn qos_profile_defaults_are_stable_and_task_kind_specific() {
    let app = build_router(test_state(test_service_cfg()));
    let inactive = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/profiles")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(inactive.status(), StatusCode::OK);
    assert!(json_body(inactive).await.as_array().unwrap().is_empty());

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/v1/profiles?include_inactive=true")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let profiles = json_body(resp).await;
    let profiles = profiles.as_array().unwrap();
    assert_eq!(profiles.len(), 3);
    assert!(profiles.iter().all(|profile| profile["active"] == false));

    let by_kind: std::collections::HashMap<_, _> = profiles
        .iter()
        .map(|profile| (profile["task_kind"].as_str().unwrap(), profile))
        .collect();
    assert_eq!(by_kind.len(), 3);
    assert_eq!(
        by_kind["question_answering"]["profile_id"],
        "default-question-answering"
    );
    assert_eq!(
        by_kind["question_answering"]["error_semantics"],
        "word-overlap-f1-v1"
    );
    assert_eq!(by_kind["ner"]["profile_id"], "default-ner");
    assert_eq!(by_kind["ner"]["error_semantics"], "entity-set-f1-v1");
    assert_eq!(
        by_kind["text_generation"]["profile_id"],
        "default-text-generation"
    );
    assert_eq!(
        by_kind["text_generation"]["error_semantics"],
        "relative-confidence-degradation-v1"
    );
    for profile in profiles {
        assert_eq!(
            profile["max_error_threshold"],
            test_engine_config().max_error_threshold
        );
        assert_eq!(
            profile["error_window"]["past_slots"],
            test_engine_config().error_window_past
        );
        assert_eq!(
            profile["error_window"]["future_slots"],
            test_engine_config().error_window_future
        );
        assert_eq!(
            profile["error_window"]["past_decay_slots"],
            test_engine_config().error_window_past_decay_slots
        );
        assert_eq!(
            profile["cumulative_error"]["enabled"],
            test_engine_config().global_error_constraint_enabled
        );
        assert_eq!(
            profile["cumulative_error"]["hard"],
            test_engine_config().global_error_constraint_hard
        );
        assert!(profile.get("capacity_tiers").is_none());
    }
}

#[tokio::test]
async fn qos_profile_registration_is_reusable_idempotent_and_immutable() {
    let app = build_router(test_state(test_service_cfg()));
    let body = qos_profile_payload("qa-standard-v1", 17.5);

    for _ in 0..2 {
        let resp = app
            .clone()
            .oneshot(json_request("POST", "/v1/profiles", &body))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NO_CONTENT);
    }

    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/profiles/qa-standard-v1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let profile = json_body(resp).await;
    assert_eq!(profile["profile_id"], "qa-standard-v1");
    assert_eq!(profile["task_kind"], "question_answering");
    assert_eq!(profile["max_error_threshold"], 17.5);
    assert_eq!(profile["error_window"]["past_slots"], 3);
    assert_eq!(profile["error_window"]["future_slots"], 4);
    assert_eq!(profile["error_window"]["past_decay_slots"], 2);
    assert_eq!(profile["cumulative_error"]["enabled"], true);
    assert_eq!(profile["cumulative_error"]["hard"], false);
    assert_eq!(profile["active"], false);

    let conflicting_body = qos_profile_payload("qa-standard-v1", 19.0);
    let resp = app
        .clone()
        .oneshot(json_request("POST", "/v1/profiles", &conflicting_body))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CONFLICT);

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/v1/profiles/qa-standard-v1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let unchanged = json_body(resp).await;
    assert_eq!(unchanged["max_error_threshold"], 17.5);
}

#[tokio::test]
async fn profile_listing_includes_only_profiles_with_assignments_by_default() {
    use carbonshift_rs::engine::qos::QosProfileId;
    use carbonshift_rs::engine::types::Assignment;

    let state = test_state(test_service_cfg());
    state
        .shared_state
        .add_assignments(vec![Assignment::new_for_profile(
            91,
            0,
            "Accurate".to_string(),
            1.0,
            10.0,
            60,
            Some(0),
            Some(1),
            QosProfileId::parse("default-question-answering").unwrap(),
        )]);
    let app = build_router(state);

    let active_only = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/profiles")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(active_only.status(), StatusCode::OK);
    let active_profiles = json_body(active_only).await;
    assert_eq!(active_profiles.as_array().unwrap().len(), 1);
    assert_eq!(
        active_profiles[0]["profile_id"],
        "default-question-answering"
    );
    assert_eq!(active_profiles[0]["active"], true);

    let all_profiles = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/profiles?include_inactive=true")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(all_profiles.status(), StatusCode::OK);
    let all_profiles = json_body(all_profiles).await;
    assert_eq!(all_profiles.as_array().unwrap().len(), 3);
    let inactive = all_profiles
        .as_array()
        .unwrap()
        .iter()
        .find(|profile| profile["profile_id"] == "default-ner")
        .unwrap();
    assert_eq!(inactive["active"], false);

    let inactive_detail = app
        .oneshot(
            Request::builder()
                .uri("/v1/profiles/default-ner")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(inactive_detail.status(), StatusCode::OK);
    assert_eq!(json_body(inactive_detail).await["active"], false);
}

#[tokio::test]
async fn global_capacity_tier_setter_is_authenticated_atomic_and_readable() {
    let mut service_cfg = test_service_cfg();
    service_cfg.api_key = Some("caller-secret".to_string());
    let state = test_state(service_cfg);
    let shared_state = state.shared_state.clone();
    let app = build_router(state);
    let tiers = serde_json::json!([
        {"max_requests": 4, "multiplier": 1.0},
        {"max_requests": 6, "multiplier": 1.5},
        {"max_requests": null, "multiplier": 5.0}
    ]);
    let body = serde_json::json!({
        "capacity_tiers": tiers
    })
    .to_string();

    let unauthorized = app
        .clone()
        .oneshot(json_request("PUT", "/v1/admin/capacity-tiers", &body))
        .await
        .unwrap();
    assert_eq!(unauthorized.status(), StatusCode::UNAUTHORIZED);

    let mut authorized = json_request("PUT", "/v1/admin/capacity-tiers", &body);
    authorized
        .headers_mut()
        .insert("x-api-key", "caller-secret".parse().unwrap());
    let response = app.clone().oneshot(authorized).await.unwrap();
    assert_eq!(response.status(), StatusCode::NO_CONTENT);
    assert_eq!(
        serde_json::to_value(shared_state.capacity_tiers_snapshot()).unwrap(),
        tiers
    );

    let invalid_ladder = serde_json::json!({
        "capacity_tiers": [
            {"max_requests": 4, "multiplier": 1.0},
            {"max_requests": 4, "multiplier": 1.5},
            {"max_requests": null, "multiplier": 5.0}
        ]
    })
    .to_string();
    let mut invalid = json_request("PUT", "/v1/admin/capacity-tiers", &invalid_ladder);
    invalid
        .headers_mut()
        .insert("x-api-key", "caller-secret".parse().unwrap());
    let response = app.clone().oneshot(invalid).await.unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert_eq!(
        serde_json::to_value(shared_state.capacity_tiers_snapshot()).unwrap(),
        tiers
    );

    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/metrics/costs")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(json_body(response).await["capacity_tiers"], tiers);
}

#[tokio::test]
async fn new_baselines_use_the_replaced_global_tier_ladder() {
    let cfg = Arc::new(test_engine_config());
    let shared_state = SharedState::new();
    let forecast = Arc::new(RwLock::new(vec![100.0; cfg.total_slots as usize]));
    let app_state = AppState::new(shared_state.clone(), cfg, test_service_cfg(), forecast);
    let app = build_router(app_state);
    let tiers = serde_json::json!([
        {"max_requests": 1, "multiplier": 1.0},
        {"max_requests": null, "multiplier": 5.0}
    ]);
    let response = app
        .clone()
        .oneshot(json_request(
            "PUT",
            "/v1/admin/capacity-tiers",
            &serde_json::json!({"capacity_tiers": tiers}).to_string(),
        ))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NO_CONTENT);

    let submit = r#"{"deadline_seconds":5,"task_kind":"text_generation","payload":{"task":"text_generation","input":{"prompt":"p"}}}"#;
    let first = app
        .clone()
        .oneshot(json_request("POST", "/v1/requests", submit))
        .await
        .unwrap();
    let second = app
        .oneshot(json_request("POST", "/v1/requests", submit))
        .await
        .unwrap();
    assert_eq!(first.status(), StatusCode::ACCEPTED);
    assert_eq!(second.status(), StatusCode::ACCEPTED);
    let first_baseline = json_body(first).await["baseline_carbon_cost"]
        .as_f64()
        .unwrap();
    let second_baseline = json_body(second).await["baseline_carbon_cost"]
        .as_f64()
        .unwrap();
    assert_eq!(second_baseline, first_baseline * 5.0);
}

#[tokio::test]
async fn qos_profile_registration_rejects_invalid_ids_and_policy_values() {
    let app = build_router(test_state(test_service_cfg()));
    let invalid_id = qos_profile_payload("QA standard v1", 17.5);
    let resp = app
        .clone()
        .oneshot(json_request("POST", "/v1/profiles", &invalid_id))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);

    let profile_with_local_capacity_tiers = serde_json::json!({
        "profile_id": "qa-capacity-v1",
        "task_kind": "question_answering",
        "flavours": [{"name": "Accurate", "error": 1.0, "duration": 10}],
        "error_semantics": "word-overlap-f1-v1",
        "max_error_threshold": 10.0,
        "error_window": {"past_slots": 1, "future_slots": 1, "past_decay_slots": 0},
        "cumulative_error": {"enabled": true, "hard": true},
        "capacity_tiers": [{"max_requests": 10, "multiplier": 1.5}]
    })
    .to_string();
    let resp = app
        .clone()
        .oneshot(json_request(
            "POST",
            "/v1/profiles",
            &profile_with_local_capacity_tiers,
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);

    let invalid_threshold = qos_profile_payload("qa-invalid-v1", 101.0);
    let resp = app
        .clone()
        .oneshot(json_request("POST", "/v1/profiles", &invalid_threshold))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);

    let invalid_window = serde_json::json!({
        "profile_id": "qa-invalid-window-v1",
        "task_kind": "question_answering",
        "flavours": [{"name": "Accurate", "error": 1.0, "duration": 10}],
        "error_semantics": "word-overlap-f1-v1",
        "max_error_threshold": 10.0,
        "error_window": {"past_slots": -1, "future_slots": 2, "past_decay_slots": 0},
        "cumulative_error": {"enabled": true, "hard": true}
    })
    .to_string();
    let resp = app
        .clone()
        .oneshot(json_request("POST", "/v1/profiles", &invalid_window))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);

    let empty_flavours = serde_json::json!({
        "profile_id": "qa-no-flavours-v1",
        "task_kind": "question_answering",
        "flavours": [],
        "error_semantics": "word-overlap-f1-v1",
        "max_error_threshold": 10.0,
        "error_window": {"past_slots": 1, "future_slots": 1, "past_decay_slots": 0},
        "cumulative_error": {"enabled": true, "hard": true}
    })
    .to_string();
    let resp = app
        .oneshot(json_request("POST", "/v1/profiles", &empty_flavours))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn qos_profile_task_kind_is_an_open_validated_identifier() {
    let app = build_router(test_state(test_service_cfg()));
    let body = serde_json::json!({
        "profile_id": "summarization-standard-v1",
        "task_kind": "summarization",
        "flavours": [{"name": "Accurate", "error": 2.0, "duration": 60}],
        "error_semantics": "rouge-l-f1-v1",
        "max_error_threshold": 10.0,
        "error_window": {"past_slots": 2, "future_slots": 2, "past_decay_slots": 0},
        "cumulative_error": {"enabled": false, "hard": false}
    })
    .to_string();
    let resp = app
        .clone()
        .oneshot(json_request("POST", "/v1/profiles", &body))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NO_CONTENT);

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/v1/profiles/summarization-standard-v1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let profile = json_body(resp).await;
    assert_eq!(profile["task_kind"], "summarization");
}

#[tokio::test]
async fn qos_profile_registration_uses_configured_caller_authentication() {
    let mut service_cfg = test_service_cfg();
    service_cfg.api_key = Some("caller-secret".to_string());
    let app = build_router(test_state(service_cfg));
    let body = qos_profile_payload("qa-authenticated-v1", 17.5);

    let unauthorized = app
        .clone()
        .oneshot(json_request("POST", "/v1/profiles", &body))
        .await
        .unwrap();
    assert_eq!(unauthorized.status(), StatusCode::UNAUTHORIZED);

    let authorized = Request::builder()
        .method("POST")
        .uri("/v1/profiles")
        .header("content-type", "application/json")
        .header("x-api-key", "caller-secret")
        .body(Body::from(body))
        .unwrap();
    let resp = app.oneshot(authorized).await.unwrap();
    assert_eq!(resp.status(), StatusCode::NO_CONTENT);
}

/// Full pipeline with a real scheduler: after a request is scheduled, the
/// scheduler's own global error average must be observable via `/v1/stats`
/// (used to compare against a task's declared `max_error_threshold`).
#[tokio::test]
async fn stats_reports_global_error_avg_after_scheduling() {
    let mut cfg = test_engine_config();
    cfg.solver.batch_size = 1;
    let cfg = Arc::new(cfg);
    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(
        false,
        String::new(),
        String::new(),
        String::new(),
        None,
    ));
    let forecast = test_forecast(cfg.total_slots);
    let mut scheduler = BatchScheduler::new(
        shared_state.clone(),
        cfg.clone(),
        metrics_logger,
        forecast.clone(),
    );
    scheduler.start();

    let mut svc_cfg = test_service_cfg();
    svc_cfg.submit_wait_timeout_secs = 5.0;
    let state = AppState::new(shared_state, cfg, svc_cfg, forecast);
    let app = build_router(state);

    app.clone()
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds": 5}"#,
        ))
        .await
        .unwrap();

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/v1/stats")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let json = json_body(resp).await;
    assert_eq!(json["global_error_count"], 1);
    assert!(json["global_error_avg"].is_number());

    scheduler.stop();
}

#[tokio::test]
async fn ready_is_ok_at_start_of_horizon() {
    let app = build_router(test_state(test_service_cfg()));
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ready")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
}

#[tokio::test]
async fn ready_returns_503_near_horizon_exhaustion() {
    let mut svc_cfg = test_service_cfg();
    svc_cfg.horizon_ready_threshold = 0.9;
    let state = test_state(svc_cfg);
    state.shared_state.set_current_slot(46); // 46/50 = 92% > 90% threshold
    let app = build_router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/ready")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
}

/// Full pipeline with a real (started) scheduler: submit -> the DP solver
/// assigns a slot within the poll window -> `200 scheduled`.
#[tokio::test]
async fn end_to_end_submit_gets_scheduled() {
    let mut cfg = test_engine_config();
    cfg.solver.batch_size = 1; // schedule as soon as one request arrives
    let cfg = Arc::new(cfg);

    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(
        false,
        String::new(),
        String::new(),
        String::new(),
        None,
    ));
    let forecast = test_forecast(cfg.total_slots);
    let mut scheduler = BatchScheduler::new(
        shared_state.clone(),
        cfg.clone(),
        metrics_logger,
        forecast.clone(),
    );
    scheduler.start();

    let mut svc_cfg = test_service_cfg();
    svc_cfg.submit_wait_timeout_secs = 5.0;
    let state = AppState::new(shared_state, cfg, svc_cfg, forecast);
    let app = build_router(state);

    let resp = app
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds": 5}"#,
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let json = json_body(resp).await;
    assert_eq!(json["status"], "scheduled");
    assert!(json["scheduled_slot"].is_number());
    assert!(
        json["scheduled_at"].is_number(),
        "scheduled_at should be a unix timestamp, got {:?}",
        json["scheduled_at"]
    );

    scheduler.stop();
}

/// `baseline_carbon_cost` reflects the cost of running the request
/// immediately, at arrival, with the most accurate (most expensive) flavour
/// — the reference point the client uses to compute carbon savings.
#[tokio::test]
async fn submit_response_includes_positive_baseline_carbon_cost() {
    let app = build_router(test_state(test_service_cfg()));

    let resp = app
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds": 5}"#,
        ))
        .await
        .unwrap();
    let json = json_body(resp).await;
    let baseline = json["baseline_carbon_cost"]
        .as_f64()
        .expect("baseline_carbon_cost must be a number");
    assert!(
        baseline > 0.0,
        "baseline_carbon_cost should be positive, got {baseline}"
    );
}

/// A request that references a dynamically-registered task must be
/// scheduled with one of *that task's* flavours, not `Config::flavours`
/// (the default task) — this is what lets a client-announced task's
/// error/cost data actually reach the DP solver.
#[tokio::test]
async fn task_flavours_registered_via_v1_tasks_are_used_for_scheduling() {
    let mut cfg = test_engine_config();
    cfg.solver.batch_size = 1;
    let cfg = Arc::new(cfg);

    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(
        false,
        String::new(),
        String::new(),
        String::new(),
        None,
    ));
    let forecast = test_forecast(cfg.total_slots);
    let mut scheduler = BatchScheduler::new(
        shared_state.clone(),
        cfg.clone(),
        metrics_logger,
        forecast.clone(),
    );
    scheduler.start();

    let mut svc_cfg = test_service_cfg();
    svc_cfg.submit_wait_timeout_secs = 5.0;
    let state = AppState::new(shared_state, cfg, svc_cfg, forecast);
    let app = build_router(state);

    let register_body = r#"{"task_id": "custom_task", "flavours": [{"name": "OnlyThis", "error": 1.0, "duration": 45}]}"#;
    let resp = app
        .clone()
        .oneshot(json_request("POST", "/v1/tasks", register_body))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NO_CONTENT);

    let submit_body = r#"{"deadline_seconds": 5, "task_id": "custom_task"}"#;
    let resp = app
        .oneshot(json_request("POST", "/v1/requests", submit_body))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let json = json_body(resp).await;
    assert_eq!(json["status"], "scheduled");
    assert_eq!(json["flavour"], "OnlyThis");

    scheduler.stop();
}

#[tokio::test]
async fn explicit_qos_profile_is_preserved_through_request_scheduling() {
    let mut cfg = test_engine_config();
    cfg.solver.batch_size = 1;
    let cfg = Arc::new(cfg);
    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(
        false,
        String::new(),
        String::new(),
        String::new(),
        None,
    ));
    let forecast = test_forecast(cfg.total_slots);
    let mut scheduler = BatchScheduler::new(
        shared_state.clone(),
        cfg.clone(),
        metrics_logger,
        forecast.clone(),
    );
    scheduler.start();

    let mut service_cfg = test_service_cfg();
    service_cfg.submit_wait_timeout_secs = 5.0;
    let state = AppState::new(shared_state.clone(), cfg, service_cfg, forecast);
    let app = build_router(state);
    let profile_body = qos_profile_payload("qa-standard-v1", 17.5);
    let registered = app
        .clone()
        .oneshot(json_request("POST", "/v1/profiles", &profile_body))
        .await
        .unwrap();
    assert_eq!(registered.status(), StatusCode::NO_CONTENT);

    let submitted = app
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds":5,"qos_profile_id":"qa-standard-v1","task_kind":"question_answering","payload":{"input":{"question":"q","context":"c"}}}"#,
        ))
        .await
        .unwrap();
    assert_eq!(submitted.status(), StatusCode::OK);
    let result = json_body(submitted).await;
    assert_eq!(result["qos_profile_id"], "qa-standard-v1");
    assert_eq!(result["status"], "scheduled");

    let assignments = shared_state.get_current_assignments();
    let assignment = assignments.values().next().unwrap();
    assert_eq!(assignment.qos_profile_id.as_str(), "qa-standard-v1");
    assert_eq!(
        shared_state
            .get_profile_error_stats(
                &carbonshift_rs::engine::qos::QosProfileId::parse("qa-standard-v1").unwrap()
            )
            .count,
        1
    );

    scheduler.stop();
}

#[tokio::test]
async fn omitted_profile_uses_the_default_for_payload_task_kind() {
    let shared_state = SharedState::new();
    let cfg = Arc::new(test_engine_config());
    let state = AppState::new(
        shared_state.clone(),
        cfg.clone(),
        test_service_cfg(),
        test_forecast(cfg.total_slots),
    );
    let app = build_router(state);
    let resp = app
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds":5,"task_id":"default","payload":{"task":"ner","input":{"text":"Ada Lovelace"}}}"#,
        ))
        .await
        .unwrap();

    assert_eq!(resp.status(), StatusCode::ACCEPTED);
    let response = json_body(resp).await;
    assert_eq!(response["qos_profile_id"], "default-ner");
    let pending = shared_state.drain_pending_requests();
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0].qos_profile_id.as_str(), "default-ner");
    assert_eq!(pending[0].task_id, "ner");
}

#[tokio::test]
async fn legacy_task_id_equal_to_restored_profile_id_still_resolves_after_restart() {
    let app = build_router(test_state(test_service_cfg()));
    let profile_body = qos_profile_payload("qa-standard-v1", 17.5);
    let registered = app
        .clone()
        .oneshot(json_request("POST", "/v1/profiles", &profile_body))
        .await
        .unwrap();
    assert_eq!(registered.status(), StatusCode::NO_CONTENT);

    let submitted = app
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds":5,"task_id":"qa-standard-v1","payload":{"task":"question_answering","input":{"question":"q","context":"c"}}}"#,
        ))
        .await
        .unwrap();
    assert_eq!(submitted.status(), StatusCode::ACCEPTED);
    let response = json_body(submitted).await;
    assert_eq!(response["qos_profile_id"], "qa-standard-v1");
}

#[tokio::test]
async fn request_rejects_profile_with_a_different_task_kind() {
    let app = build_router(test_state(test_service_cfg()));
    let resp = app
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds":5,"qos_profile_id":"default-ner","task_kind":"question_answering","payload":{"task":"question_answering","input":{"question":"q","context":"c"}}}"#,
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn request_rejects_a_profile_for_another_task_kind() {
    let app = build_router(test_state(test_service_cfg()));
    let resp = app
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds":5,"qos_profile_id":"default-ner","task_kind":"question_answering","payload":{"task":"question_answering","input":{"question":"q","context":"c"}}}"#,
        ))
        .await
        .unwrap();

    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    let body = json_body(resp).await;
    assert!(
        body["error"]
            .as_str()
            .unwrap()
            .contains("not question_answering")
    );
}

#[tokio::test]
async fn assignment_and_error_history_queries_filter_by_qos_profile() {
    let shared_state = SharedState::new();
    shared_state.add_assignments(vec![
        carbonshift_rs::engine::types::Assignment::new_for_profile(
            40,
            0,
            "Accurate".to_string(),
            1.0,
            10.0,
            60,
            Some(0),
            Some(1),
            carbonshift_rs::engine::qos::QosProfileId::parse("qa-standard-v1").unwrap(),
        ),
        carbonshift_rs::engine::types::Assignment::new_for_profile(
            41,
            0,
            "NerFast".to_string(),
            1.0,
            90.0,
            10,
            Some(0),
            Some(1),
            carbonshift_rs::engine::qos::QosProfileId::parse("ner-standard-v1").unwrap(),
        ),
    ]);
    let cfg = Arc::new(test_engine_config());
    let state = AppState::new(
        shared_state,
        cfg.clone(),
        test_service_cfg(),
        test_forecast(cfg.total_slots),
    );
    let app = build_router(state);
    for (profile_id, task_kind, semantics, threshold) in [
        (
            "qa-standard-v1",
            "question_answering",
            "word-overlap-f1-v1",
            17.5,
        ),
        ("ner-standard-v1", "ner", "entity-set-f1-v1", 95.0),
    ] {
        let body = serde_json::json!({
            "profile_id": profile_id,
            "task_kind": task_kind,
            "flavours": [{"name": "Accurate", "error": 0.0, "duration": 60}],
            "error_semantics": semantics,
            "max_error_threshold": threshold,
            "error_window": {
                "past_slots": 1,
                "future_slots": 1,
                "past_decay_slots": 0
            },
            "cumulative_error": {"enabled": true, "hard": true}
        })
        .to_string();
        let resp = app
            .clone()
            .oneshot(json_request("POST", "/v1/profiles", &body))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NO_CONTENT);
    }

    let assignments = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/assignments?qos_profile_id=qa-standard-v1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(assignments.status(), StatusCode::OK);
    let assignments = json_body(assignments).await;
    assert_eq!(assignments.as_array().unwrap().len(), 1);
    assert_eq!(assignments[0]["qos_profile_id"], "qa-standard-v1");

    let history = app
        .oneshot(
            Request::builder()
                .uri(
                    "/v1/metrics/error-history?qos_profile_id=qa-standard-v1&from_slot=0&to_slot=0",
                )
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(history.status(), StatusCode::OK);
    let history = json_body(history).await;
    assert_eq!(history["qos_profile_id"], "qa-standard-v1");
    assert_eq!(history["error_semantics"], "word-overlap-f1-v1");
    assert_eq!(history["max_error_threshold"], 17.5);
    assert_eq!(history["profile_error_avg"], 10.0);
    assert_eq!(history["global_error_avg"], 50.0);
    assert_eq!(history["slots"][0]["request_count"], 1);
    assert_eq!(history["slots"][0]["average_error"], 10.0);
    assert_eq!(history["slots"][0]["window_error"], 10.0);
}

#[tokio::test]
async fn cost_metrics_filter_request_totals_but_keep_capacity_tiers_global() {
    use carbonshift_rs::engine::qos::QosProfileId;
    use carbonshift_rs::engine::types::Assignment;
    use carbonshift_rs::service::models::RequestStatus;
    use carbonshift_rs::service::state::TrackedRequest;

    let shared_state = SharedState::new();
    let text_profile = QosProfileId::parse("default-text-generation").unwrap();
    let ner_profile = QosProfileId::parse("default-ner").unwrap();
    shared_state.add_assignments(vec![
        Assignment::new_for_profile(
            40,
            0,
            "Accurate".to_string(),
            1.0,
            10.0,
            60,
            Some(0),
            Some(1),
            text_profile.clone(),
        ),
        Assignment::new_for_profile(
            41,
            0,
            "Fast".to_string(),
            7.0,
            20.0,
            30,
            Some(0),
            Some(1),
            ner_profile.clone(),
        ),
        Assignment::new_for_profile(
            42,
            0,
            "Balanced".to_string(),
            2.0,
            15.0,
            45,
            Some(0),
            Some(1),
            text_profile.clone(),
        ),
    ]);

    let cfg = Arc::new(test_engine_config());
    let state = AppState::new(
        shared_state,
        cfg.clone(),
        test_service_cfg(),
        test_forecast(cfg.total_slots),
    );
    for (request_id, profile_id, status, baseline) in [
        (40, text_profile.clone(), RequestStatus::Completed, 4.0),
        (41, ner_profile, RequestStatus::Completed, 8.0),
        (42, text_profile, RequestStatus::Scheduled, 3.0),
    ] {
        let mut tracked =
            TrackedRequest::new(None, serde_json::Value::Null, baseline, 60, 0, profile_id);
        tracked.status = status;
        state.tracked.lock().unwrap().insert(request_id, tracked);
    }

    let app = build_router(state);
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/metrics/costs?qos_profile_id=default-text-generation")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let costs = json_body(response).await;
    assert_eq!(costs["qos_profile_id"], "default-text-generation");
    assert_eq!(costs["current_actual_carbon_cost"], 1.0);
    assert_eq!(costs["current_actual_baseline_carbon_cost"], 4.0);
    assert_eq!(costs["forecasted_pending_carbon_cost"], 2.0);
    assert_eq!(costs["total_forecasted_carbon_cost"], 3.0);
    assert_eq!(costs["total_baseline_carbon_cost"], 7.0);
    assert_eq!(
        costs["capacity_tiers"],
        serde_json::to_value(test_engine_config().capacity_tiers).unwrap()
    );

    let unknown = app
        .oneshot(
            Request::builder()
                .uri("/v1/metrics/costs?qos_profile_id=unknown-profile")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(unknown.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn stats_expose_process_local_legacy_task_id_usage_for_migration() {
    let state = test_state(test_service_cfg());
    let app = build_router(state.clone());

    let legacy_submit = app
        .clone()
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds":5,"task_id":"text_generation","payload":{"task":"text_generation","input":{"prompt":"legacy"}}}"#,
        ))
        .await
        .unwrap();
    assert_eq!(legacy_submit.status(), StatusCode::ACCEPTED);

    let current_submit = app
        .clone()
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds":5,"task_kind":"text_generation","payload":{"task":"text_generation","input":{"prompt":"current"}}}"#,
        ))
        .await
        .unwrap();
    assert_eq!(current_submit.status(), StatusCode::ACCEPTED);

    let legacy_query = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/assignments?task_id=text_generation")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(legacy_query.status(), StatusCode::OK);

    let legacy_task_lookup = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/tasks/text_generation")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(legacy_task_lookup.status(), StatusCode::OK);

    let legacy_task_registration = app
        .clone()
        .oneshot(json_request(
            "POST",
            "/v1/tasks",
            r#"{"task_id":"legacy-profile-v1","task_kind":"text_generation","flavours":[{"name":"Accurate","error":1.0,"duration":60}]}"#,
        ))
        .await
        .unwrap();
    assert_eq!(legacy_task_registration.status(), StatusCode::NO_CONTENT);

    let stats = app
        .oneshot(
            Request::builder()
                .uri("/v1/stats")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(stats.status(), StatusCode::OK);
    let stats = json_body(stats).await;
    assert_eq!(stats["legacy_task_id_usage"]["request_submissions"], 1);
    assert_eq!(stats["legacy_task_id_usage"]["task_api_calls"], 2);
    assert_eq!(stats["legacy_task_id_usage"]["monitoring_queries"], 1);
}

#[tokio::test]
async fn scheduler_dispatches_profile_homogeneous_batches_for_distinct_qos_policies() {
    let mut cfg = test_engine_config();
    cfg.solver.batch_size = 2;
    let cfg = Arc::new(cfg);
    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(
        false,
        String::new(),
        String::new(),
        String::new(),
        None,
    ));
    let forecast = test_forecast(cfg.total_slots);
    let mut scheduler = BatchScheduler::new(
        shared_state.clone(),
        cfg.clone(),
        metrics_logger,
        forecast.clone(),
    );
    scheduler.start();

    let mut service_cfg = test_service_cfg();
    service_cfg.submit_wait_timeout_secs = 0.02;
    let state = AppState::new(shared_state.clone(), cfg, service_cfg, forecast);
    let app = build_router(state);
    let registrations = [
        qos_profile_payload("qa-standard-v1", 5.0),
        serde_json::json!({
            "profile_id": "ner-standard-v1",
            "task_kind": "ner",
            "flavours": [{"name": "NerFast", "error": 30.0, "duration": 15}],
            "error_semantics": "entity-set-f1-v1",
            "max_error_threshold": 35.0,
            "error_window": {
                "past_slots": 2,
                "future_slots": 3,
                "past_decay_slots": 0
            },
            "cumulative_error": {"enabled": true, "hard": true}
        })
        .to_string(),
    ];
    for registration in registrations {
        let resp = app
            .clone()
            .oneshot(json_request("POST", "/v1/profiles", &registration))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NO_CONTENT);
    }

    let requests = [
        ("qa-standard-v1", "question_answering", "Accurate"),
        ("ner-standard-v1", "ner", "NerFast"),
        ("qa-standard-v1", "question_answering", "Accurate"),
        ("ner-standard-v1", "ner", "NerFast"),
    ];
    for (profile_id, task_kind, _expected_flavour) in requests {
        let payload = serde_json::json!({
            "deadline_seconds": 30,
            "qos_profile_id": profile_id,
            "task_kind": task_kind,
            "payload": {
                "task": task_kind,
                "input": if task_kind == "ner" {
                    serde_json::json!({"text": "Ada Lovelace"})
                } else {
                    serde_json::json!({"question": "Who?", "context": "Ada Lovelace"})
                }
            }
        })
        .to_string();
        let resp = app
            .clone()
            .oneshot(json_request("POST", "/v1/requests", &payload))
            .await
            .unwrap();
        assert!(
            resp.status() == StatusCode::OK || resp.status() == StatusCode::ACCEPTED,
            "unexpected submission status: {}",
            resp.status()
        );
    }

    // Roll the virtual slot so the final incomplete QA batch exercises the
    // slot-end partial flush instead of relying on a timing race.
    shared_state.set_virtual_elapsed_ms(10_000);
    shared_state.set_current_slot(1);
    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    while shared_state.get_current_assignments().len() < 4 && tokio::time::Instant::now() < deadline
    {
        tokio::time::sleep(Duration::from_millis(10)).await;
    }

    let assignments = shared_state.get_current_assignments();
    assert_eq!(
        assignments.len(),
        4,
        "all profile batches should be committed"
    );
    let mut per_profile = std::collections::HashMap::new();
    for assignment in assignments.values() {
        *per_profile
            .entry(assignment.qos_profile_id.as_str().to_string())
            .or_insert(0usize) += 1;
        if assignment.qos_profile_id.as_str() == "qa-standard-v1" {
            assert_eq!(assignment.flavour_name, "Accurate");
        } else {
            assert_eq!(assignment.qos_profile_id.as_str(), "ner-standard-v1");
            assert_eq!(assignment.flavour_name, "NerFast");
        }
    }
    assert_eq!(per_profile.get("qa-standard-v1"), Some(&2));
    assert_eq!(per_profile.get("ner-standard-v1"), Some(&2));

    scheduler.stop();
}

/// An `actual_error_pct` reported via the executor callback must correct the
/// scheduler's global error average (predicted → actual), per-assignment,
/// without changing how many assignments are counted.
#[tokio::test]
async fn executor_callback_with_actual_error_pct_corrects_global_error() {
    let mut cfg = test_engine_config();
    cfg.solver.batch_size = 1;
    let cfg = Arc::new(cfg);

    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(
        false,
        String::new(),
        String::new(),
        String::new(),
        None,
    ));
    let forecast = test_forecast(cfg.total_slots);
    let mut scheduler = BatchScheduler::new(
        shared_state.clone(),
        cfg.clone(),
        metrics_logger,
        forecast.clone(),
    );
    scheduler.start();

    let mut svc_cfg = test_service_cfg();
    svc_cfg.submit_wait_timeout_secs = 5.0;
    let state = AppState::new(shared_state.clone(), cfg, svc_cfg, forecast);
    let app = build_router(state);

    let resp = app
        .clone()
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds": 5}"#,
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let json = json_body(resp).await;
    let request_id = json["request_id"].as_u64().unwrap();

    let stats_before = shared_state.get_global_error_stats();
    assert_eq!(stats_before.count, 1);
    assert!(
        stats_before.error_sum > 0.0,
        "expected a nonzero predicted error from the chosen flavour"
    );

    let callback_body = r#"{"success": true, "result": {"actual_error_pct": 0.0}}"#;
    let resp = app
        .oneshot(json_request(
            "POST",
            &format!("/v1/callback/{request_id}"),
            callback_body,
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let stats_after = shared_state.get_global_error_stats();
    assert_eq!(
        stats_after.count, 1,
        "correction must not change the assignment count"
    );
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
        .oneshot(json_request("POST", "/v1/admin/advance-slot", "{}"))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CONFLICT);
}

#[tokio::test]
async fn advance_slot_applies_forecast_and_observed_values_from_body() {
    let mut cfg = test_engine_config();
    cfg.simulation.manual_clock = true;
    let cfg = Arc::new(cfg);
    let forecast = test_forecast(cfg.total_slots);
    let state = AppState::new(
        SharedState::new(),
        cfg,
        test_service_cfg(),
        forecast.clone(),
    );
    let app = build_router(state);
    let body = r#"{"kind":"announce","current_slot":0,"observed":{"slot":0,"observed_at_slot":0,"actual":123.4},"forecast":[{"slot":0,"forecast":120.0}]}"#;
    let resp = app
        .clone()
        .oneshot(json_request("POST", "/v1/admin/advance-slot", body))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/v1/carbon_intensity?slot=0")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let rows = json_body(resp).await;
    assert_eq!(rows[0]["forecast"], 120.0);
    assert_eq!(rows[0]["actual"], 123.4);
}

/// The client's reported actual carbon intensity for an assignment's slot
/// rescales `carbon_cost` (forwarded to the caller) once its result arrives.
#[tokio::test]
async fn executor_callback_corrects_carbon_cost_with_actual_carbon_intensity() {
    let mut cfg = test_engine_config();
    cfg.solver.batch_size = 1;
    let cfg = Arc::new(cfg);
    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(
        false,
        String::new(),
        String::new(),
        String::new(),
        None,
    ));
    let forecast = test_forecast(cfg.total_slots);
    let mut scheduler = BatchScheduler::new(
        shared_state.clone(),
        cfg.clone(),
        metrics_logger,
        forecast.clone(),
    );
    scheduler.start();

    let mut svc_cfg = test_service_cfg();
    svc_cfg.submit_wait_timeout_secs = 5.0;
    let state = AppState::new(shared_state.clone(), cfg, svc_cfg, forecast.clone());
    let app = build_router(state.clone());

    let resp = app
        .clone()
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds": 5}"#,
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let json = json_body(resp).await;
    let request_id = json["request_id"].as_u64().unwrap();
    let scheduled_slot = json["scheduled_slot"].as_i64().unwrap() as i32;
    let predicted_cost = json["carbon_cost"].as_f64().unwrap();

    // Actual carbon intensity turns out to be double the forecast for that slot.
    let actual_ci = forecast.read().unwrap()[scheduled_slot as usize] * 2.0;
    state
        .actual_carbon_intensity
        .lock()
        .unwrap()
        .insert(scheduled_slot, actual_ci);

    let resp = app
        .oneshot(json_request(
            "POST",
            &format!("/v1/callback/{request_id}"),
            r#"{"success": true, "result": {}}"#,
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let corrected = shared_state.get_current_assignments()[&request_id].carbon_cost;
    assert!(
        (corrected - predicted_cost * 2.0).abs() < 1e-9,
        "corrected={corrected}, expected={}",
        predicted_cost * 2.0
    );

    scheduler.stop();
}

/// Full "fake time" protocol: submit under `manual_clock`, then advance —
/// the handler must block until the dispatcher has handed the assignment
/// off to the (dry-run) executor before returning.
#[tokio::test]
async fn advance_slot_moves_clock_and_waits_for_dispatch() {
    let mut cfg = test_engine_config();
    cfg.solver.batch_size = 1;
    cfg.simulation.manual_clock = true;
    let cfg = Arc::new(cfg);

    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(
        false,
        String::new(),
        String::new(),
        String::new(),
        None,
    ));
    let forecast = test_forecast(cfg.total_slots);
    let mut scheduler = BatchScheduler::new(
        shared_state.clone(),
        cfg.clone(),
        metrics_logger,
        forecast.clone(),
    );
    scheduler.start();

    let mut svc_cfg = test_service_cfg();
    svc_cfg.submit_wait_timeout_secs = 5.0;
    svc_cfg.dispatcher_poll_interval_ms = 5;
    let state = AppState::new(shared_state.clone(), cfg, svc_cfg, forecast);
    tokio::spawn(carbonshift_rs::service::dispatcher::run(state.clone()));
    let app = build_router(state.clone());

    // deadline_seconds=5 with the default 10s slot_duration -> deadline_slot
    // = current_slot + 1, so the DP solver is forced to place it at slot 0
    // or 1 — guaranteed <= 1 after a single advance.
    let resp = app
        .clone()
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds": 5}"#,
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(json_body(resp).await["status"], "scheduled");
    assert_eq!(shared_state.get_current_slot(), 0);

    let resp = app
        .oneshot(json_request(
            "POST",
            "/v1/admin/advance-slot",
            r#"{"current_slot":1}"#,
        ))
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
    cfg.simulation.manual_clock = true;
    let cfg = Arc::new(cfg);

    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(
        false,
        String::new(),
        String::new(),
        String::new(),
        None,
    ));
    let forecast = test_forecast(cfg.total_slots);
    let mut scheduler = BatchScheduler::new(
        shared_state.clone(),
        cfg.clone(),
        metrics_logger,
        forecast.clone(),
    );
    scheduler.start();

    let mut svc_cfg = test_service_cfg();
    svc_cfg.submit_wait_timeout_secs = 0.02; // times out well before any flush/advance
    svc_cfg.dispatcher_poll_interval_ms = 5;
    let state = AppState::new(shared_state.clone(), cfg, svc_cfg, forecast);
    tokio::spawn(carbonshift_rs::service::dispatcher::run(state.clone()));
    let app = build_router(state.clone());

    // First request: the scheduler's own startup quirk (current_slot(0) >
    // last_flush_slot(-1)) flushes it almost instantly, so it legitimately
    // becomes `scheduled` within the poll window.
    let resp = app
        .clone()
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds": 5}"#,
        ))
        .await
        .unwrap();
    assert_eq!(json_body(resp).await["status"], "scheduled");

    // Second request: no more automatic flush until the slot advances, so
    // the poll times out and it's left `pending`.
    let resp = app
        .clone()
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds": 5}"#,
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::ACCEPTED);
    assert_eq!(json_body(resp).await["status"], "pending");

    // Advancing the slot flushes + assigns it; the dispatcher must still
    // deliver it despite the tracked status never having become `Scheduled`.
    let resp = app
        .oneshot(json_request(
            "POST",
            "/v1/admin/advance-slot",
            r#"{"current_slot":1}"#,
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let all_dispatched = state
        .tracked
        .lock()
        .unwrap()
        .values()
        .all(|t| t.status == carbonshift_rs::service::models::RequestStatus::Dispatched);
    assert!(
        all_dispatched,
        "both requests should have been dispatched, including the one stuck Pending"
    );

    scheduler.stop();
}

#[tokio::test]
async fn executor_callback_corrects_carbon_cost_with_actual_execution_time() {
    let mut cfg = test_engine_config();
    cfg.solver.batch_size = 1;
    let cfg = Arc::new(cfg);
    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(
        false,
        String::new(),
        String::new(),
        String::new(),
        None,
    ));
    let forecast = test_forecast(cfg.total_slots);
    let mut scheduler = BatchScheduler::new(
        shared_state.clone(),
        cfg.clone(),
        metrics_logger,
        forecast.clone(),
    );
    scheduler.start();

    let mut svc_cfg = test_service_cfg();
    svc_cfg.submit_wait_timeout_secs = 5.0;
    let state = AppState::new(shared_state.clone(), cfg, svc_cfg, forecast.clone());
    let app = build_router(state.clone());

    let resp = app
        .clone()
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds": 5}"#,
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let json = json_body(resp).await;
    let request_id = json["request_id"].as_u64().unwrap();
    let predicted_cost = json["carbon_cost"].as_f64().unwrap();
    let assignment = shared_state.get_current_assignments()[&request_id].clone();
    let duration = assignment.flavour_duration as f64;

    // Actual execution time is 40% of nominal duration.
    let actual_exec_time = duration * 0.4;
    let callback_body = format!(
        r#"{{"success": true, "result": {{"execution_time_seconds": {actual_exec_time}}}}}"#
    );

    let resp = app
        .oneshot(json_request(
            "POST",
            &format!("/v1/callback/{request_id}"),
            &callback_body,
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let corrected = shared_state.get_current_assignments()[&request_id].carbon_cost;
    assert!(
        (corrected - predicted_cost * 0.4).abs() < 1e-9,
        "corrected={corrected}, expected={}",
        predicted_cost * 0.4
    );

    scheduler.stop();
}

#[tokio::test]
async fn executor_callback_corrects_baseline_carbon_cost_with_execution_time_and_arrival_ci() {
    let mut cfg = test_engine_config();
    cfg.solver.batch_size = 1;
    let cfg = Arc::new(cfg);
    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(
        false,
        String::new(),
        String::new(),
        String::new(),
        None,
    ));
    let forecast = test_forecast(cfg.total_slots);
    let mut scheduler = BatchScheduler::new(
        shared_state.clone(),
        cfg.clone(),
        metrics_logger,
        forecast.clone(),
    );
    scheduler.start();

    let mut svc_cfg = test_service_cfg();
    svc_cfg.submit_wait_timeout_secs = 5.0;
    let state = AppState::new(shared_state.clone(), cfg, svc_cfg, forecast.clone());
    let app = build_router(state.clone());

    let resp = app
        .clone()
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds": 5}"#,
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let json = json_body(resp).await;
    let request_id = json["request_id"].as_u64().unwrap();
    let initial_baseline = json["baseline_carbon_cost"].as_f64().unwrap();

    let (arrival_slot, baseline_dur) = {
        let guard = state.tracked.lock().unwrap();
        let t = &guard[&request_id];
        (t.arrival_slot, t.baseline_duration as f64)
    };

    // Actual CI for arrival_slot turns out to be 1.5x forecast
    let actual_ci = forecast.read().unwrap()[arrival_slot as usize] * 1.5;
    state
        .actual_carbon_intensity
        .lock()
        .unwrap()
        .insert(arrival_slot, actual_ci);

    // Baseline execution time turns out to be 0.75x nominal baseline duration
    let actual_baseline_time = baseline_dur * 0.75;
    let callback_body = format!(
        r#"{{"success": true, "result": {{"baseline_execution_time_seconds": {actual_baseline_time}}}}}"#
    );

    let resp = app
        .oneshot(json_request(
            "POST",
            &format!("/v1/callback/{request_id}"),
            &callback_body,
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let corrected_baseline = state.tracked.lock().unwrap()[&request_id].baseline_carbon_cost;
    let expected = initial_baseline * 1.5 * 0.75;
    assert!(
        (corrected_baseline - expected).abs() < 1e-9,
        "corrected_baseline={corrected_baseline}, expected={expected}"
    );

    scheduler.stop();
}

#[tokio::test]
async fn fine_grained_monitoring_endpoints_work() {
    let mut cfg = test_engine_config();
    cfg.solver.batch_size = 1;
    let cfg = Arc::new(cfg);

    let shared_state = SharedState::new();
    let metrics_logger = Arc::new(MetricsLogger::new(
        false,
        String::new(),
        String::new(),
        String::new(),
        None,
    ));
    let forecast = test_forecast(cfg.total_slots);
    let mut scheduler = BatchScheduler::new(
        shared_state.clone(),
        cfg.clone(),
        metrics_logger,
        forecast.clone(),
    );
    scheduler.start();

    let mut svc_cfg = test_service_cfg();
    svc_cfg.submit_wait_timeout_secs = 5.0;
    let state = AppState::new(shared_state.clone(), cfg.clone(), svc_cfg, forecast);
    let app = build_router(state.clone());

    // Submit a request to get an assignment
    let resp = app
        .clone()
        .oneshot(json_request(
            "POST",
            "/v1/requests",
            r#"{"deadline_seconds": 3600}"#,
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = json_body(resp).await;
    let req_id = body["request_id"].as_u64().unwrap();
    let scheduled_slot = body["scheduled_slot"].as_i64().unwrap() as i32;

    // 1. Test GET /v1/assignments
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/assignments")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let assignments = json_body(resp).await;
    assert_eq!(assignments.as_array().unwrap().len(), 1);
    assert_eq!(assignments[0]["request_id"], req_id);

    // 2. Test GET /v1/slots/:slot
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(&format!("/v1/slots/{scheduled_slot}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let slot_detail = json_body(resp).await;
    assert_eq!(slot_detail["slot"], scheduled_slot);
    assert_eq!(slot_detail["total_requests"], 1);

    // 3. Test GET /v1/metrics/costs
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/metrics/costs")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let costs = json_body(resp).await;
    assert!(costs["total_baseline_carbon_cost"].as_f64().unwrap() > 0.0);

    // 4. Test GET /v1/metrics/error-history
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/metrics/error-history")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let err_hist = json_body(resp).await;
    assert!(err_hist["slots"].as_array().unwrap().len() >= 1);

    scheduler.stop();
}
