//! Common, runner-independent contracts for scheduling algorithms.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use crate::engine::qos::QosProfileId;
use crate::engine::types::{Assignment, CapacityTier, Flavour};

/// A request's original arrival and deadline slots.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SolveRequest {
    pub request_id: u64,
    pub arrival_slot: i32,
    pub deadline_slot: i32,
}

#[derive(Debug, Clone)]
pub struct ErrorWindowInput {
    pub baseline_err: ErrorTotals,
    pub past_slots: i32,
    pub future_slots: i32,
}

#[derive(Debug, Clone, Default)]
pub struct ErrorTotals {
    pub sum: f64,   // error sum
    pub count: f64, // request count, which may be fractional for weighted history
}

#[derive(Debug, Clone)]
pub struct QoSProfileInput {
    pub profile_id: QosProfileId,
    /// Candidate flavours from the single QoS profile shared by this batch.
    pub flavours: Vec<Flavour>,
    pub cumulative_error_constraint_active: bool,
    pub cumulative_err: ErrorTotals,
    pub err_by_slot: HashMap<i32, ErrorTotals>,
    pub max_average_error: f64,
    pub error_window: ErrorWindowInput,
}

// Input data shared by solver algorithms
#[derive(Debug, Clone)]
pub struct SolveInput {
    pub current_slot: i32,
    pub requests: Vec<SolveRequest>,

    // pub flavours: Vec<Flavour>,
    pub carbon_forecast: Vec<f64>,
    pub capacity_tiers: Vec<CapacityTier>,
    pub baseline_slot_counts: HashMap<i32, i64>,

    pub carbon_cost_duration_scale: f64,
    pub assignment_max_future_slots: i32,
    pub assignment_max_slot: Option<i32>,
    pub qos: QoSProfileInput,
}

// Identifies the profile and planning horizon for one runtime instance
#[derive(Debug, Clone)]
pub struct SolverScope {
    pub profile_id: QosProfileId,
    pub total_slots: usize,
}

//  A proposed request-to-slot-and-flavour decision; it has no final price yet
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProposedPlacement {
    pub req_id: u64,
    pub scheduled_slot: i32,
    pub flavour_name: String,
    /// True when the solver had to place this request after its deadline.
    pub late: bool,
}

impl ProposedPlacement {
    /// Build an on-time placement without requiring callers to set `late`.
    pub fn new(req_id: u64, scheduled_slot: i32, flavour_name: String) -> Self {
        Self {
            req_id,
            scheduled_slot,
            flavour_name,
            late: false,
        }
    }

    /// Build a placement for work whose deadline has already passed.
    pub fn late(req_id: u64, scheduled_slot: i32, flavour_name: String) -> Self {
        Self {
            req_id,
            scheduled_slot,
            flavour_name,
            late: true,
        }
    }
}

//  The algorithm's common, price-independent output
#[derive(Debug, Clone, Default)]
pub struct SolverProposal {
    pub placements: Vec<ProposedPlacement>,
}

// Runtime-owned handle for a proposal whose state delta is awaiting settlement
#[derive(Debug, Clone)]
pub struct PendingProposal {
    pub id: ProposalId,
    pub proposal: SolverProposal,
}

// Uniquely identifies a pending proposal
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ProposalId(pub u64);

//  Final assignments returned by the commit path, including their final prices
#[derive(Debug, Clone, Default)]
pub struct CommitReport {
    pub assignments: Vec<Assignment>,
}

//  Errors that the engine registry and runtime must report explicitly
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SolverError {
    EmpyEngineName,
    DuplicateEngine(String),
    UnknownEngine(String),
    UnknownProposal(ProposalId),
    ProposalIdExhausted,
    LockPoisoned,
    Algorithm(String),
    EmptyFlavours,
}

impl std::fmt::Display for SolverError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EmpyEngineName => write!(f, "Engine name cannot be empty"),
            Self::DuplicateEngine(name) => write!(f, "Duplicate engine: {name}"),
            Self::UnknownEngine(name) => write!(f, "Unknown engine: {name}"),
            Self::UnknownProposal(id) => {
                write!(f, "Proposal: {:?} is unknown or already settled", id)
            }
            Self::ProposalIdExhausted => write!(f, "Proposal ID space exhausted"),
            Self::LockPoisoned => write!(f, "Solver runtime lock was poisoned"),
            Self::Algorithm(msg) => write!(f, "Algorithm error: {msg}"),
        }
    }
}

impl std::error::Error for SolverError {}

/// Typed algorithm contract. Concrete state and deltas stay inside the algorithm.
/// State and Delta are needed by ACO and Bandit which are stateful algorithms.
/// Carbonshift DP solver does not need them
pub trait SolverAlgorithm: Send + Sync {
    type State: Clone + Send + Sync + 'static;
    type Delta: Send + 'static;

    fn initial_state(&self, scope: &SolverScope) -> Self::State;
    fn solve(
        &self,
        snapshot: &Self::State,
        scope: &SolverScope,
        input: &SolveInput,
    ) -> Result<(SolverProposal, Self::Delta), SolverError>;
    fn merge(&self, state: &mut Self::State, delta: Self::Delta, committed: &CommitReport);
}

/// Object-safe interface that runners will use
pub trait SolverRuntime: Send + Sync {
    fn propose(&self, input: &SolveInput) -> Result<PendingProposal, SolverError>;
    fn commit(&self, id: ProposalId, report: CommitReport) -> Result<(), SolverError>;
    fn discard(&self, id: ProposalId) -> Result<(), SolverError>;
}

struct RuntimeData<S, D> {
    state: S,
    pending_deltas: HashMap<ProposalId, D>,
}

/// Holds typed state behind an object-safe runtime boundary
pub struct TypedSolverRuntime<A: SolverAlgorithm> {
    algorithm: A,
    scope: SolverScope,
    data: Mutex<RuntimeData<A::State, A::Delta>>,
    next_id: AtomicU64,
}

impl<A: SolverAlgorithm> TypedSolverRuntime<A> {
    pub fn new(algorithm: A, scope: &SolverScope) -> Result<Self, SolverError> {
        let state = algorithm.initial_state(scope);
        Ok(Self {
            algorithm,
            scope: scope.clone(),
            data: Mutex::new(RuntimeData {
                state,
                pending_deltas: HashMap::new(),
            }),
            next_id: AtomicU64::new(0),
        })
    }
}

impl<A: SolverAlgorithm> SolverRuntime for TypedSolverRuntime<A> {
    fn propose(&self, input: &SolveInput) -> Result<PendingProposal, SolverError> {
        // Take the lock to have a snapshot
        let snapshot = {
            // Operator ? performs pattern matching
            // Ok(guard) -> guard,
            // Err(_) -> return LockPoisoned
            let data = self.data.lock().map_err(|_| SolverError::LockPoisoned)?;
            data.state.clone()
        };
        // Block is closed, lock is released

        let (proposal, delta) = self.algorithm.solve(&snapshot, &self.scope, input)?;
        // Here we do not use fetch_add because it wraps on overflow
        // u64::MAX + 1 == 0
        // So we prefer fetch_update + checked_add
        let raw_id = self
            .next_id // get next ProposalId
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |next| {
                next.checked_add(1)
            })
            .map_err(|_| SolverError::ProposalIdExhausted)?; // notice that raw_id will take the older value
        let id = ProposalId(raw_id);

        let mut data = self.data.lock().map_err(|_| SolverError::LockPoisoned)?;
        data.pending_deltas.insert(id, delta);

        Ok(PendingProposal { id, proposal })
    }

    /// Apply the Deltas from ProposalId, if any
    fn commit(&self, id: ProposalId, report: CommitReport) -> Result<(), SolverError> {
        let mut data = self.data.lock().map_err(|_| SolverError::LockPoisoned)?;
        let delta = data
            .pending_deltas
            .remove(&id)
            .ok_or(SolverError::UnknownProposal(id))?;

        self.algorithm.merge(&mut data.state, delta, &report);
        // TODO: is this Ok(()) ok?
        Ok(())
    }

    /// Discard ProposalId. Unlikely with current solvers, but relevant for completeness
    fn discard(&self, id: ProposalId) -> Result<(), SolverError> {
        let mut data = self.data.lock().map_err(|_| SolverError::LockPoisoned)?;
        data.pending_deltas
            .remove(&id)
            .ok_or(SolverError::UnknownProposal(id))?;
        Ok(())
    }
}

type RuntimeFactory =
    Arc<dyn Fn(SolverScope) -> Result<Box<dyn SolverRuntime>, SolverError> + Send + Sync>;

#[derive(Default)]
pub struct SolverRegistry {
    factories: HashMap<String, RuntimeFactory>,
}

impl SolverRegistry {
    pub fn register<A, F>(&mut self, name: impl Into<String>, build: F) -> Result<(), SolverError>
    where
        // The algorithm type must not contain non-'static borrowed references.
        // The boxed runtime returned by create() is 'static by default
        A: SolverAlgorithm + 'static,
        // `build` is a factory: calling it creates an algorithm.
        // Send + Sync allow the closure to be shared safely across threads.
        // 'static means it cannot borrow short-lived local data.
        F: Fn() -> A + Send + Sync + 'static,
    {
        let name = normalize_name(&name.into());
        if name.is_empty() {
            return Err(SolverError::EmpyEngineName);
        }
        if self.factories.contains_key(&name) {
            return Err(SolverError::DuplicateEngine(name));
        }

        // Store a factory closure in the registry.
        //
        // `move` makes the closure take ownership of `build`, so it can keep
        // calling the factory later, after this register() call has returned.
        //
        // When invoked, the closure:
        // 1. Calls build() to create a fresh algorithm of type A.
        // 2. Wraps that algorithm in a TypedSolverRuntime.
        // 3. Boxes it as the type-erased `dyn SolverRuntime`, allowing the
        //    registry to store runtimes for different algorithm types together
        self.factories.insert(
            name,
            Arc::new(move |scope| {
                let runtime = TypedSolverRuntime::new(build(), &scope)?;
                Ok(Box::new(runtime) as Box<dyn SolverRuntime>)
            }),
        );
        Ok(())
    }

    pub fn create(
        &self,
        name: &str,
        scope: SolverScope,
    ) -> Result<Box<dyn SolverRuntime>, SolverError> {
        let key = normalize_name(name);
        let factory = self
            .factories
            .get(&key)
            .ok_or_else(|| SolverError::UnknownEngine(name.to_owned()))?;

        factory(scope)
    }
}

fn normalize_name(name: &str) -> String {
    name.trim().to_ascii_lowercase()
}

// ----------------------------------------------------------------------------
// ----------------------------------TEST--------------------------------------
// ----------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    struct UnitEngine;

    impl SolverAlgorithm for UnitEngine {
        type State = ();
        type Delta = ();

        fn initial_state(&self, _scope: &SolverScope) {}

        fn solve(
            &self,
            _snapshot: &(),
            _scope: &SolverScope,
            input: &SolveInput,
        ) -> Result<(SolverProposal, ()), SolverError> {
            let placements = input
                .requests
                .iter()
                .map(|request| {
                    ProposedPlacement::new(
                        request.request_id,
                        request.arrival_slot.max(input.current_slot),
                        "test".to_owned(),
                    )
                })
                .collect();

            Ok((SolverProposal { placements }, ()))
        }

        fn merge(&self, _state: &mut (), _delta: (), _committed: &CommitReport) {}
    }

    #[derive(Clone)]
    struct CounterState(u64);

    struct CounterDelta(u64);

    struct CounterEngine;

    impl SolverAlgorithm for CounterEngine {
        type State = CounterState;
        type Delta = CounterDelta;

        fn initial_state(&self, _scope: &SolverScope) -> Self::State {
            CounterState(0)
        }

        fn solve(
            &self,
            snapshot: &CounterState,
            _scope: &SolverScope,
            input: &SolveInput,
        ) -> Result<(SolverProposal, CounterDelta), SolverError> {
            let placements = input
                .requests
                .iter()
                .map(|request| {
                    ProposedPlacement::new(
                        request.request_id,
                        request.arrival_slot.max(input.current_slot),
                        snapshot.0.to_string(),
                    )
                })
                .collect();

            Ok((SolverProposal { placements }, CounterDelta(1)))
        }

        fn merge(&self, state: &mut CounterState, delta: CounterDelta, committed: &CommitReport) {
            if !committed.assignments.is_empty() {
                state.0 += delta.0;
            }
        }
    }

    fn input() -> SolveInput {
        SolveInput {
            current_slot: 1,
            requests: vec![SolveRequest {
                request_id: 7,
                arrival_slot: 1,
                deadline_slot: 3,
            }],
            carbon_forecast: vec![1.0; 4],
            capacity_tiers: vec![CapacityTier {
                max_requests: None,
                multiplier: 1.0,
            }],
            baseline_slot_counts: HashMap::new(),
            carbon_cost_duration_scale: 1.0,
            assignment_max_future_slots: 3,
            assignment_max_slot: Some(3),
            qos: QoSProfileInput {
                profile_id: QosProfileId::default_profile(),
                flavours: vec![Flavour {
                    name: "test".to_owned(),
                    error: 0.0,
                    duration: 1.0,
                }],
                cumulative_error_constraint_active: false,
                cumulative_err: ErrorTotals {
                    count: 0.0,
                    sum: 0.0,
                },
                err_by_slot: HashMap::new(),
                max_average_error: 4.0,
                error_window: ErrorWindowInput {
                    baseline_err: ErrorTotals {
                        count: 0.0,
                        sum: 0.0,
                    },
                    past_slots: 0,
                    future_slots: 3,
                },
            },
        }
    }

    fn scope() -> SolverScope {
        SolverScope {
            profile_id: QosProfileId::default_profile(),
            total_slots: 4,
        }
    }

    fn committed_report() -> CommitReport {
        CommitReport {
            assignments: vec![Assignment::new(
                7,
                1,
                "test".to_owned(),
                1.0,
                0.0,
                1.0,
                Some(1),
                Some(3),
            )],
        }
    }

    #[test]
    fn unit_state_engine_runs_through_object_safe_registry() {
        let mut registry = SolverRegistry::default();
        registry
            .register::<UnitEngine, _>("unit", || UnitEngine)
            .unwrap();

        let runtime = registry.create("UNIT", scope()).unwrap();
        let pending = runtime.propose(&input()).unwrap();

        assert_eq!(pending.proposal.placements.len(), 1);
        assert_eq!(pending.proposal.placements[0].scheduled_slot, 1);

        runtime.discard(pending.id).unwrap();

        assert!(matches!(
            runtime.discard(pending.id),
            Err(SolverError::UnknownProposal(_))
        ));
    }

    #[test]
    fn state_delta_is_discarded_or_merged_exactly_once() {
        let mut registry = SolverRegistry::default();
        registry
            .register::<CounterEngine, _>("counter", || CounterEngine)
            .unwrap();

        let runtime = registry.create("counter", scope()).unwrap();

        let rejected = runtime.propose(&input()).unwrap();
        assert_eq!(rejected.proposal.placements[0].flavour_name, "0");
        runtime.discard(rejected.id).unwrap();

        let accepted = runtime.propose(&input()).unwrap();
        assert_eq!(accepted.proposal.placements[0].flavour_name, "0");
        runtime.commit(accepted.id, committed_report()).unwrap();

        let next = runtime.propose(&input()).unwrap();
        assert_eq!(next.proposal.placements[0].flavour_name, "1");
        runtime.discard(next.id).unwrap();
    }

    #[test]
    fn registry_reports_unknown_engine_names() {
        let registry = SolverRegistry::default();

        assert!(matches!(
            registry.create("missing", scope()),
            Err(SolverError::UnknownEngine(name)) if name == "missing"
        ));
    }
}
