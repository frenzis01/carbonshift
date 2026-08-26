/// CarbonShift RS — library crate.
///
/// All modules are `pub` so integration tests and downstream crates can access
/// the full API.  Binary targets (`src/bin/*`) import from this crate.
///
/// Source is organised in three layers:
/// - `engine`: the core scheduling logic (config, types, DP solver, shared
///   state, batch scheduler) — no I/O beyond CSV metrics logging.
/// - `sim`: simulation-only helpers (synthetic/replayed request generation,
///   scenario file loading) used by benchmark/offline tooling.
/// - `service`: the REST/HTTP layer that turns `engine` into a dockerizable
///   network service.
///
/// The `pub use engine::*` / `pub use sim::*` re-exports below keep the
/// original flat `crate::config`, `crate::types`, ... paths working, so
/// engine/sim internals and existing binaries don't need per-file import
/// changes after the module reorganisation.
pub mod engine;
pub mod service;
pub mod sim;

pub use engine::config;
pub use engine::dp_solver;
pub use engine::metrics_logger;
pub use engine::online_swarm;
pub use engine::online_swarmerge;
pub use engine::scheduler;
pub use engine::shared_state;
pub use engine::types;
pub use sim::generator;
pub use sim::scenario;
