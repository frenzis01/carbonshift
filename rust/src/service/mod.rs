//! REST/HTTP service layer (Phase 2+, see `rust/PLAN_SERVICE.md`).
//!
//! Turns the `engine` scheduling core into a dockerizable network service:
//! accepts job submissions over HTTP, replies immediately with the assigned
//! time slot, dispatches jobs to an external executor when their slot
//! arrives, and relays the executor's callback result back to the original
//! caller.

pub mod auth;
pub mod dispatcher;
pub mod handlers;
pub mod models;
pub mod server;
pub mod state;

