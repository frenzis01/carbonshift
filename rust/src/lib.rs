/// CarbonShift RS — library crate.
///
/// All modules are `pub` so integration tests and downstream crates can access
/// the full API.  The binary target (`main.rs`) imports from this crate.

pub mod config;
pub mod dp_solver;
pub mod generator;
pub mod metrics_logger;
pub mod scenario;
pub mod scheduler;
pub mod shared_state;
pub mod types;
