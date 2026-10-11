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
///
/// The router attaches this middleware to caller-facing submission routes;
/// the shared helper below centralizes the optional-header and comparison
/// behavior so the executor callback can use a different credential.
pub async fn require_api_key(State(state): State<AppState>, req: Request, next: Next) -> Response {
    require_header(&state.service_cfg.api_key, "x-api-key", req, next).await
}

/// Guards `/v1/callback/*`: only the configured executor may post results.
///
/// This is intentionally separate from the caller API key: callers and the
/// executor have different trust boundaries and can rotate credentials
/// independently.
pub async fn require_executor_token(
    State(state): State<AppState>,
    req: Request,
    next: Next,
) -> Response {
    // Both wrappers supply their own setting and header name; keeping the
    // actual decision here prevents their missing-key behavior from drifting.
    require_header(
        &state.service_cfg.executor_token,
        "x-executor-token",
        req,
        next,
    )
    .await
}

/// Shared optional-secret check used by caller and executor middleware.
///
/// Keeping the configured secret and header name as inputs lets both routes
/// share identical failure behavior while remaining separately credentialed.
async fn require_header(
    expected: &Option<String>,
    header_name: &str,
    req: Request,
    next: Next,
) -> Response {
    match expected {
        // An unset secret disables authentication for local and dry-run use.
        // When configured, the request must prove possession before reaching
        // the handler; rejected requests never consume the body downstream.
        None => next.run(req).await,
        Some(expected) => {
            // Invalid header bytes are treated like a missing credential
            // rather than being converted lossily into a different string.
            let provided = req.headers().get(header_name).and_then(|v| v.to_str().ok());
            match provided {
                // Only the matching credential is allowed to continue through
                // the router's remaining middleware and handler.
                Some(p) if constant_time_eq(p, expected) => next.run(req).await,
                _ => (
                    StatusCode::UNAUTHORIZED,
                    format!("missing or invalid {header_name}"),
                )
                    .into_response(),
            }
        }
    }
}

/// Compare equal-length credentials without data-dependent early exit.
///
/// The byte length is checked first, so a length mismatch is distinguishable;
/// for equal-length inputs, all bytes are visited and differences are folded
/// together instead of returning at the first mismatching character.
pub fn constant_time_eq(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    if a.len() != b.len() {
        return false;
    }
    // XOR marks differing byte positions; OR accumulates all differences.
    // The final result is zero only when every compared byte was identical.
    a.iter()
        .zip(b.iter())
        .fold(0u8, |acc, (x, y)| acc | (x ^ y))
        == 0
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
