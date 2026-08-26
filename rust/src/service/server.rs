//! Axum router wiring for the REST service.

use axum::middleware;
use axum::routing::{get, post};
use axum::Router;

use crate::service::{auth, handlers};
use crate::service::state::AppState;

pub fn build_router(state: AppState) -> Router {
    // `/v1/requests*` requires the caller's API key (if configured);
    // `/v1/callback/*` requires the executor's shared token instead — the
    // two audiences must never be able to use each other's credential.
    let caller_routes = Router::new()
        .route("/v1/requests", post(handlers::submit_request))
        .route("/v1/requests/:id", get(handlers::get_request_status))
        .route_layer(middleware::from_fn_with_state(state.clone(), auth::require_api_key));

    let executor_routes = Router::new()
        .route("/v1/callback/:id", post(handlers::executor_callback))
        .route_layer(middleware::from_fn_with_state(state.clone(), auth::require_executor_token));

    let public_routes = Router::new()
        .route("/health", get(handlers::health))
        .route("/ready", get(handlers::ready))
        .route("/v1/stats", get(handlers::stats))
        .route("/v1/horizon", get(handlers::horizon));

    public_routes.merge(caller_routes).merge(executor_routes).with_state(state)
}
