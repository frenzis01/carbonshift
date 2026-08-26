//! Simulation-only tooling: synthetic/replayed request generation and
//! scenario file loading. Used by benchmark binaries (`src/bin/nshift`) and
//! the standalone simulator (`src/bin/simulate`) — never by the live REST
//! service, which receives real requests over HTTP instead.

pub mod generator;
pub mod scenario;
