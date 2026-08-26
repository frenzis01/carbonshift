//! Core scheduling engine: config, domain types, DP solver, shared state,
//! batch scheduler, online swarm strategies, metrics logging.
//!
//! This module has no knowledge of HTTP/REST or of simulated request
//! generation — it only knows how to accept requests into `SharedState` and
//! turn them into `Assignment`s. It is shared by the REST service
//! (`crate::service`) and by the simulation/benchmark tooling
//! (`crate::sim`, `src/bin/nshift`).

pub mod config;
pub mod dp_solver;
pub mod metrics_logger;
pub mod online_swarm;
pub mod online_swarmerge;
pub mod scheduler;
pub mod shared_state;
pub mod types;
