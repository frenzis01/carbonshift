//! Solver algorithm registration and shared helpers for the common contract.
pub mod aco;
pub mod bandit;
mod dp;
mod hazard;
mod shared;
mod singleton_best;

pub use dp::{DpAlgorithm, DpAlgorithmConfig};
pub use hazard::HazardAlgorithm;
pub use singleton_best::{SingletonBestAlgorithm, SingletonBestFlavourPolicy};

use crate::engine::config::SwarmConfig;
use crate::engine::solver::{SolverError, SolverRegistry};

/// Register deterministic strategies without adding solver-specific branches
/// to either runner.
pub fn register_stateless_solvers(
    registry: &mut SolverRegistry,
    dp_config: DpAlgorithmConfig,
    hazard_seed: u64,
) -> Result<(), SolverError> {
    registry.register("dp", move || DpAlgorithm {
        config: dp_config.clone(),
    })?;
    registry.register("singleton_best", || {
        SingletonBestAlgorithm::new(SingletonBestFlavourPolicy::AllProfileFlavours)
    })?;
    registry.register("singleton_best_accurate", || {
        SingletonBestAlgorithm::new(SingletonBestFlavourPolicy::MostAccurateOnly)
    })?;
    registry.register("hazard", move || HazardAlgorithm::new(hazard_seed))?;

    Ok(())
}

/// Register the persistent online solvers without exposing their concrete
/// state or delta types to either runner.
pub fn register_stateful_solvers(
    registry: &mut SolverRegistry,
    config: &SwarmConfig,
) -> Result<(), SolverError> {
    let bandit_config = bandit::BanditConfig {
        epsilon: config.bandit_epsilon,
        initial_q: config.bandit_initial_q,
        seed: config.bandit_seed,
    };
    registry.register("bandit", move || {
        bandit::BanditAlgorithm::new(bandit_config.clone())
    })?;

    let aco_config = aco::AcoConfig {
        n_ants: config.aco_n_ants,
        n_iterations: config.aco_n_iterations,
        alpha: config.aco_alpha,
        beta: config.aco_beta,
        rho: config.aco_rho,
        q_deposit: config.aco_q,
        initial_pheromone: config.aco_tau0,
        seed: config.aco_seed,
    };
    registry.register("ant_colony", move || {
        aco::AcoAlgorithm::new(aco_config.clone())
    })?;

    Ok(())
}

/// Compose the full engine registry from the shared solver configuration.
///
/// The function only registers implementations. Each runner remains
/// responsible for choosing an engine name, creating a scoped runtime, and
/// settling proposed placements through its own commit adapter.
pub fn register_all_solvers(
    registry: &mut SolverRegistry,
    dp_config: DpAlgorithmConfig,
    swarm_config: &SwarmConfig,
    hazard_seed: u64,
) -> Result<(), SolverError> {
    register_stateless_solvers(registry, dp_config, hazard_seed)?;
    register_stateful_solvers(registry, swarm_config)?;
    Ok(())
}

// ----------------------------------------------------------------------------
// ----------------------------------------------------------------------------
// ---------------------------------TESTS--------------------------------------

#[cfg(test)]
mod tests {
    use std::collections::{HashMap, HashSet};

    use crate::engine::qos::QosProfileId;
    use crate::engine::solver::{
        ErrorTotals, ErrorWindowInput, PendingProposal, QoSProfileInput, SolveInput, SolveRequest,
        SolverRegistry, SolverRuntime, SolverScope,
    };
    use crate::types::{CapacityTier, Flavour};

    use super::hazard::inverse_carbon_slot_probabilities;
    use super::{DpAlgorithmConfig, register_all_solvers, register_stateless_solvers};

    fn config() -> DpAlgorithmConfig {
        DpAlgorithmConfig {
            pruning_method: "none".to_owned(),
            pruning_min_batch_size: 0,
            pruning_k: 100,
            timeout_secs: 5.0,
        }
    }

    fn input() -> SolveInput {
        SolveInput {
            current_slot: 1,
            requests: vec![
                SolveRequest {
                    request_id: 10,
                    arrival_slot: 0,
                    deadline_slot: 3,
                    assignment_deadline_slot: 3,
                },
                SolveRequest {
                    request_id: 11,
                    arrival_slot: 1,
                    deadline_slot: 3,
                    assignment_deadline_slot: 3,
                },
            ],
            carbon_forecast: vec![20.0, 10.0, 5.0, 1.0],
            capacity_tiers: vec![CapacityTier {
                max_requests: None,
                multiplier: 1.0,
            }],
            baseline_slot_counts: HashMap::new(),
            carbon_cost_duration_scale: 1.0,
            forecast_max_slot: Some(3),
            qos: QoSProfileInput {
                profile_id: QosProfileId::default_profile(),
                flavours: vec![Flavour {
                    name: "Default".to_owned(),
                    error: 0.0,
                    duration: 10.0,
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

    fn runtime_for(name: &str) -> Box<dyn SolverRuntime> {
        runtime_for_scope(name, scope())
    }

    fn runtime_for_scope(name: &str, solver_scope: SolverScope) -> Box<dyn SolverRuntime> {
        let mut registry = SolverRegistry::default();
        register_stateless_solvers(&mut registry, config(), 42).unwrap();
        registry.create(name, solver_scope).unwrap()
    }

    fn scope() -> SolverScope {
        scope_with_total_slots(4)
    }

    fn scope_with_total_slots(total_slots: usize) -> SolverScope {
        SolverScope {
            profile_id: QosProfileId::default_profile(),
            total_slots,
        }
    }

    fn runtime() -> Box<dyn SolverRuntime> {
        runtime_for("dp")
    }

    fn propose(runtime: &dyn SolverRuntime, input: &SolveInput) -> PendingProposal {
        runtime.propose(input).unwrap()
    }

    fn placements_for(
        name: &str,
        solve_input: &SolveInput,
    ) -> Vec<crate::engine::solver::ProposedPlacement> {
        placements_for_scope(name, solve_input, scope())
    }

    fn placements_for_scope(
        name: &str,
        solve_input: &SolveInput,
        solver_scope: SolverScope,
    ) -> Vec<crate::engine::solver::ProposedPlacement> {
        let runtime = runtime_for_scope(name, solver_scope);
        let pending = propose(&*runtime, solve_input);
        let placements = pending.proposal.placements.clone();
        runtime.discard(pending.id).unwrap();
        placements
    }

    #[test]
    fn dp_proposes_all_requests_from_one_joint_batch() {
        let runtime = runtime();
        let pending = propose(&*runtime, &input());
        let ids: HashSet<u64> = pending
            .proposal
            .placements
            .iter()
            .map(|placement| placement.req_id)
            .collect();

        assert_eq!(pending.proposal.placements.len(), 2);
        assert_eq!(ids, HashSet::from([10, 11]));
        assert!(pending.proposal.placements.iter().all(|placement| {
            (1..=3).contains(&placement.scheduled_slot) && placement.flavour_name == "Default"
        }));

        runtime.discard(pending.id).unwrap();
    }

    #[test]
    fn dp_uses_the_profile_flavour_set_for_the_whole_batch() {
        let runtime = runtime();
        let mut solve_input = input();
        solve_input.qos.flavours = vec![Flavour {
            name: "ProfileFlavour".to_owned(),
            error: 0.0,
            duration: 1.0,
        }];

        let pending = propose(&*runtime, &solve_input);

        assert!(
            pending
                .proposal
                .placements
                .iter()
                .all(|placement| placement.flavour_name == "ProfileFlavour")
        );
        runtime.discard(pending.id).unwrap();
    }

    #[test]
    fn singleton_best_uses_the_profile_flavour_set_for_every_request() {
        let mut solve_input = input();
        solve_input.qos.flavours = vec![Flavour {
            name: "ProfileFlavour".to_owned(),
            error: 0.0,
            duration: 1.0,
        }];

        let placements = placements_for("singleton_best", &solve_input);

        assert_eq!(placements.len(), solve_input.requests.len());
        assert!(
            placements
                .iter()
                .all(|placement| placement.flavour_name == "ProfileFlavour")
        );
    }

    #[test]
    fn singleton_best_updates_slot_occupancy_after_each_request() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.requests = vec![
            SolveRequest {
                request_id: 1,
                arrival_slot: 0,
                deadline_slot: 1,
                assignment_deadline_slot: 1,
            },
            SolveRequest {
                request_id: 2,
                arrival_slot: 0,
                deadline_slot: 1,
                assignment_deadline_slot: 1,
            },
        ];
        solve_input.qos.flavours = vec![Flavour {
            name: "One".to_owned(),
            error: 0.0,
            duration: 1.0,
        }];
        solve_input.carbon_forecast = vec![1.0, 1.0];
        solve_input.capacity_tiers = vec![
            CapacityTier {
                max_requests: Some(1),
                multiplier: 1.0,
            },
            CapacityTier {
                max_requests: None,
                multiplier: 10.0,
            },
        ];
        solve_input.qos.error_window.future_slots = 1;
        solve_input.forecast_max_slot = Some(1);

        let placements =
            placements_for_scope("singleton_best", &solve_input, scope_with_total_slots(2));

        assert_eq!(placements[0].scheduled_slot, 0);
        assert_eq!(placements[1].scheduled_slot, 1);
    }

    #[test]
    fn singleton_best_handles_occupancy_above_i32_max() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.requests.truncate(1);
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 1;
        solve_input.qos.flavours = vec![Flavour {
            name: "One".to_owned(),
            error: 0.0,
            duration: 1.0,
        }];
        solve_input.carbon_forecast = vec![1.0, 2.0];
        solve_input.capacity_tiers = vec![
            CapacityTier {
                max_requests: Some(i64::from(i32::MAX)),
                multiplier: 1.0,
            },
            CapacityTier {
                max_requests: None,
                multiplier: 10.0,
            },
        ];
        solve_input
            .baseline_slot_counts
            .insert(0, i64::from(i32::MAX) + 1);
        solve_input.qos.error_window.future_slots = 1;
        solve_input.forecast_max_slot = Some(1);

        let placements =
            placements_for_scope("singleton_best", &solve_input, scope_with_total_slots(2));

        assert_eq!(placements[0].scheduled_slot, 1);
    }

    #[test]
    fn singleton_best_counts_the_candidate_and_prior_placements_in_the_window() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.requests = vec![
            SolveRequest {
                request_id: 1,
                arrival_slot: 0,
                deadline_slot: 1,
                assignment_deadline_slot: 1,
            },
            SolveRequest {
                request_id: 2,
                arrival_slot: 0,
                deadline_slot: 1,
                assignment_deadline_slot: 1,
            },
        ];
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Accurate".to_owned(),
                error: 0.0,
                duration: 10.0,
            },
            Flavour {
                name: "Fast".to_owned(),
                error: 4.0,
                duration: 1.0,
            },
        ];
        solve_input.carbon_forecast = vec![1.0; 4];
        solve_input.qos.max_average_error = 2.0;
        solve_input.qos.error_window.future_slots = 1;

        let placements = placements_for("singleton_best", &solve_input);

        assert_eq!(placements[0].flavour_name, "Accurate");
        assert_eq!(placements[1].flavour_name, "Fast");
    }

    #[test]
    fn singleton_best_preserves_fractional_weighted_error_counts() {
        let mut solve_input = input();
        solve_input.requests.truncate(1);
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Accurate".to_owned(),
                error: 0.0,
                duration: 10.0,
            },
            Flavour {
                name: "Fast".to_owned(),
                error: 4.0,
                duration: 1.0,
            },
        ];
        solve_input.qos.max_average_error = 2.0;
        solve_input.qos.error_window.baseline_err = ErrorTotals {
            count: 1.5,
            sum: 1.0,
        };

        let placements = placements_for("singleton_best", &solve_input);

        assert_eq!(placements[0].flavour_name, "Fast");
    }

    #[test]
    fn expired_requests_use_the_cheapest_flavour_feasible_for_profile_budgets() {
        let mut solve_input = input();
        solve_input.current_slot = 2;
        solve_input.requests.truncate(1);
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 1;
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Cheap".to_owned(),
                error: 8.0,
                duration: 1.0,
            },
            Flavour {
                name: "Safe".to_owned(),
                error: 2.0,
                duration: 5.0,
            },
        ];
        solve_input.qos.cumulative_error_constraint_active = true;
        solve_input.qos.cumulative_err.count = 1.0;
        solve_input.qos.cumulative_err.sum = 5.0;
        solve_input.qos.error_window.baseline_err.count = 1.0;
        solve_input.qos.error_window.baseline_err.sum = 1.0;

        for name in ["dp", "singleton_best", "hazard"] {
            let placements = placements_for(name, &solve_input);
            assert_eq!(placements.len(), 1);
            assert_eq!(placements[0].scheduled_slot, solve_input.current_slot);
            assert_eq!(placements[0].flavour_name, "Safe");
            assert!(placements[0].late);
        }
    }

    #[test]
    fn passed_qos_window_does_not_mark_a_request_late_before_its_actual_deadline() {
        let mut solve_input = input();
        solve_input.current_slot = 2;
        solve_input.requests.truncate(1);
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 3;
        solve_input.requests[0].assignment_deadline_slot = 1;
        solve_input.qos.error_window.future_slots = 1;

        for name in ["dp", "singleton_best", "hazard"] {
            let runtime = runtime_for(name);
            assert!(
                runtime.propose(&solve_input).is_err(),
                "{name} must not report late work before the caller's deadline"
            );
        }
    }

    #[test]
    fn all_algorithms_cap_on_time_deadlines_to_the_qos_assignment_horizon() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.qos.error_window.future_slots = 1;
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Accurate".to_owned(),
                error: 0.0,
                duration: 10.0,
            },
            Flavour {
                name: "Fast".to_owned(),
                error: 8.0,
                duration: 1.0,
            },
        ];
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 3;
        solve_input.requests.truncate(1);

        for name in ["dp", "singleton_best", "hazard"] {
            let placements = placements_for(name, &solve_input);
            assert_eq!(placements.len(), 1);
            assert!(placements[0].scheduled_slot <= 1);
            assert!(!placements[0].late);
        }
    }

    #[test]
    fn qos_assignment_horizon_is_anchored_to_arrival_not_current_slot() {
        let mut solve_input = input();
        solve_input.current_slot = 1;
        solve_input.requests.truncate(1);
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 3;
        solve_input.qos.error_window.future_slots = 1;
        solve_input.qos.max_average_error = 0.0;
        solve_input.forecast_max_slot = Some(3);

        for name in ["dp", "singleton_best", "hazard"] {
            let placements = placements_for(name, &solve_input);

            assert_eq!(placements.len(), 1);
            assert_eq!(
                placements[0].scheduled_slot, 1,
                "{name} must not schedule beyond arrival_slot + future_slots"
            );
            assert!(!placements[0].late);
        }
    }

    #[test]
    fn a_shorter_forecast_caps_assignments_without_expanding_the_qos_horizon() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.requests.truncate(1);
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 3;
        solve_input.qos.error_window.future_slots = 3;
        solve_input.qos.max_average_error = 0.0;
        solve_input.forecast_max_slot = Some(1);

        for name in ["dp", "singleton_best", "hazard"] {
            let placements = placements_for(name, &solve_input);

            assert_eq!(placements.len(), 1);
            assert!(
                placements[0].scheduled_slot <= 1,
                "{name} must not schedule where no forecast is available"
            );
        }
    }

    #[test]
    fn singleton_best_falls_back_to_the_lowest_error_flavour() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.requests.truncate(1);
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Fast".to_owned(),
                error: 4.0,
                duration: 1.0,
            },
            Flavour {
                name: "Accurate".to_owned(),
                error: 2.0,
                duration: 3.0,
            },
        ];
        solve_input.qos.max_average_error = 1.0;

        let placements = placements_for("singleton_best", &solve_input);

        assert_eq!(placements[0].flavour_name, "Accurate");
        assert_eq!(placements[0].scheduled_slot, 0);
    }

    #[test]
    fn singleton_best_accurate_uses_only_the_profile_flavours_with_minimum_error() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.requests.truncate(1);
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Fast".to_owned(),
                error: 4.0,
                duration: 1.0,
            },
            Flavour {
                name: "Accurate".to_owned(),
                error: 0.0,
                duration: 10.0,
            },
        ];
        solve_input.carbon_forecast = vec![1.0; 4];

        let placements = placements_for("singleton_best_accurate", &solve_input);

        assert_eq!(placements.len(), 1);
        assert_eq!(placements[0].flavour_name, "Accurate");
    }

    #[test]
    fn dp_coverage_fallback_is_tagged_and_uses_minimum_error_not_duration() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.requests.truncate(1);
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 0;
        solve_input.qos.error_window.future_slots = 0;
        solve_input.qos.max_average_error = 0.0;
        solve_input.qos.flavours = vec![
            Flavour {
                name: "LongestButInaccurate".to_owned(),
                error: 10.0,
                duration: 100.0,
            },
            Flavour {
                name: "LowestError".to_owned(),
                error: 1.0,
                duration: 1.0,
            },
        ];

        let placements = placements_for("dp", &solve_input);

        assert_eq!(placements.len(), 1);
        assert_eq!(placements[0].flavour_name, "LowestError");
        assert_eq!(
            placements[0].origin,
            crate::engine::solver::PlacementOrigin::GreedyFallback
        );
    }

    #[test]
    fn registered_hazard_produces_valid_profile_flavour_placements() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Accurate".to_owned(),
                error: 0.0,
                duration: 10.0,
            },
            Flavour {
                name: "Fast".to_owned(),
                error: 8.0,
                duration: 1.0,
            },
        ];
        solve_input.qos.error_window.future_slots = 3;
        solve_input.qos.cumulative_error_constraint_active = true;

        let placements = placements_for("hazard", &solve_input);
        assert_eq!(placements.len(), solve_input.requests.len());
        for placement in placements {
            let request = solve_input
                .requests
                .iter()
                .find(|request| request.request_id == placement.req_id)
                .unwrap();
            let latest = request
                .deadline_slot
                .min(solve_input.forecast_max_slot.unwrap())
                .min(request.arrival_slot + solve_input.qos.error_window.future_slots);
            assert!(
                (request.arrival_slot.max(solve_input.current_slot)..=latest)
                    .contains(&placement.scheduled_slot)
            );
            assert!(["Accurate", "Fast"].contains(&placement.flavour_name.as_str()));
            assert!(!placement.late);
        }
    }

    #[test]
    fn hazard_accepts_a_request_with_only_one_legal_slot() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.requests.truncate(1);
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 0;
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Accurate".to_owned(),
                error: 0.0,
                duration: 10.0,
            },
            Flavour {
                name: "Fast".to_owned(),
                error: 8.0,
                duration: 1.0,
            },
        ];
        solve_input.carbon_forecast = vec![1.0];
        solve_input.qos.error_window.future_slots = 0;
        solve_input.forecast_max_slot = Some(0);

        let placements = placements_for_scope("hazard", &solve_input, scope_with_total_slots(1));

        assert_eq!(placements.len(), 1);
        assert_eq!(placements[0].scheduled_slot, 0);
    }

    #[test]
    fn hazard_rejects_a_request_with_no_slot_inside_its_shift_window() {
        let mut solve_input = input();
        solve_input.current_slot = 2;
        solve_input.requests.truncate(1);
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 3;
        solve_input.qos.error_window.future_slots = 1;
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Accurate".to_owned(),
                error: 0.0,
                duration: 10.0,
            },
            Flavour {
                name: "Fast".to_owned(),
                error: 4.0,
                duration: 1.0,
            },
        ];

        let runtime = runtime_for("hazard");
        assert!(runtime.propose(&solve_input).is_err());
    }

    #[test]
    fn hazard_uses_nearest_attainable_error_when_target_is_outside_flavour_range() {
        let mut solve_input = input();
        solve_input.requests.truncate(1);
        let runtime = runtime_for("hazard");

        let proposal = runtime.propose(&solve_input).unwrap();
        assert_eq!(proposal.proposal.placements.len(), 1);
        assert_eq!(
            proposal.proposal.placements[0].flavour_name, "Default",
            "the Accurate-only flavour is the nearest attainable target to 4%"
        );
    }

    #[test]
    fn hazard_clamps_below_the_minimum_flavour_error() {
        let mut solve_input = input();
        solve_input.requests.truncate(1);
        solve_input.qos.max_average_error = 0.0;
        solve_input.qos.flavours = vec![Flavour {
            name: "Fast".to_owned(),
            error: 5.0,
            duration: 1.0,
        }];

        let proposal = runtime_for("hazard").propose(&solve_input).unwrap();
        assert_eq!(proposal.proposal.placements.len(), 1);
        assert_eq!(proposal.proposal.placements[0].flavour_name, "Fast");
    }

    #[test]
    fn hazard_slot_probabilities_use_capacity_adjusted_forecast_cost() {
        let tiers = vec![
            CapacityTier {
                max_requests: Some(1),
                multiplier: 1.0,
            },
            CapacityTier {
                max_requests: None,
                multiplier: 4.0,
            },
        ];
        let slot_counts = HashMap::from([(1, 1)]);

        // Slot 0's next position is in the first tier: 10 × 1 = 10.
        // Slot 1 already has one request, so its next position is in the
        // overflow tier: 5 × 4 = 20. Inverse costs therefore normalize 2:1.
        let probabilities =
            inverse_carbon_slot_probabilities(&[0, 1], &[10.0, 5.0], &tiers, &slot_counts).unwrap();

        assert!((probabilities[0] - 2.0 / 3.0).abs() < 1e-12);
        assert!((probabilities[1] - 1.0 / 3.0).abs() < 1e-12);
    }

    #[test]
    fn hazard_assigns_all_probability_to_zero_effective_cost_slots() {
        let probabilities =
            inverse_carbon_slot_probabilities(&[0, 1, 2], &[0.0, 10.0, 0.0], &[], &HashMap::new())
                .unwrap();

        assert_eq!(probabilities, vec![0.5, 0.0, 0.5]);
    }

    #[test]
    fn all_engines_are_available_from_the_common_registry_composition() {
        let application_config = crate::engine::config::Config::default();
        let mut registry = SolverRegistry::default();
        register_all_solvers(&mut registry, config(), &application_config.swarm, 42).unwrap();
        let scope = scope();

        for name in [
            "dp",
            "singleton_best",
            "singleton_best_accurate",
            "hazard",
            "bandit",
            "ant_colony",
        ] {
            assert!(
                registry.create(name, scope.clone()).is_ok(),
                "engine {name} should be registered"
            );
        }
    }
}
