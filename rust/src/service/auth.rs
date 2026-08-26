//! Auth middleware: API key for callers, separate shared token for the
//! executor. Both are optional — unset means the check is disabled, which is
//! the default for local/dry-run use (see PLAN_SERVICE.md Fase 4).

use axum::extract::{Request, State};
use axum::http::StatusCode;
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};

use crate::service::state::AppState;

/// Guards `/v1/requests*`: callers must present `X-API-Key` matching
/// `ServiceConfig::api_key`, unless it's unset.
pub async fn require_api_key(State(state): State<AppState>, req: Request, next: Next) -> Response {
    require_header(&state.service_cfg.api_key, "x-api-key", req, next).await
}

/// Guards `/v1/callback/*`: only the configured executor may post results.
pub async fn require_executor_token(State(state): State<AppState>, req: Request, next: Next) -> Response {
    require_header(&state.service_cfg.executor_token, "x-executor-token", req, next).await
}

async fn require_header(expected: &Option<String>, header_name: &str, req: Request, next: Next) -> Response {
    match expected {
        None => next.run(req).await,
        Some(expected) => {
            let provided = req.headers().get(header_name).and_then(|v| v.to_str().ok());
            match provided {
                Some(p) if constant_time_eq(p, expected) => next.run(req).await,
                _ => (StatusCode::UNAUTHORIZED, format!("missing or invalid {header_name}")).into_response(),
            }
        }
    }
}

/// Avoids leaking key length/prefix via response-time side channels.
pub fn constant_time_eq(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b.iter()).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constant_time_eq_matches_equal_strings() {
        assert!(constant_time_eq("secret", "secret"));
    }

    #[test]
    fn constant_time_eq_rejects_different_strings() {
        assert!(!constant_time_eq("secret", "wrong"));
        assert!(!constant_time_eq("secret", "secre"));
        assert!(!constant_time_eq("secret", "secretx"));
    }
}
