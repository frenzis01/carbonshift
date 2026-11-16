//! Reference strategy that randomizes flavour choice until either QoS average is over budget.

use std::sync::atomic::{AtomicU64, Ordering};

use rand::{Rng, SeedableRng, rngs::SmallRng};

use crate::engine::solver::{
    PlacementOrigin, ProposedPlacement, SolveInput, SolverAlgorithm, SolverError, SolverProposal,
    SolverScope,
};

use super::shared::{get_fallback_flav, request_slot_bounds, validate_solve_input};

/// Slot policy for the reference random-assignment strategy.
#[derive(Debug, Clone, Copy)]
pub enum RandomAssignmentMode {
    /// Sample uniformly from the request's legal slot range.
    RandomSlot,
    /// Use the earliest legal slot, normally the request's arrival slot.
    ArrivalSlot,
}

/// Random flavour selection with a fallback after either QoS average exceeds its threshold.
pub struct RandomAssignmentAlgorithm {
    mode: RandomAssignmentMode,
    seed: u64,
    next_stream: AtomicU64,
}

impl RandomAssignmentAlgorithm {
    /// Create a reproducible random-assignment strategy with the requested slot policy.
    pub fn new(mode: RandomAssignmentMode, seed: u64) -> Self {
        Self {
            mode,
            seed,
            next_stream: AtomicU64::new(0),
        }
    }

    fn next_rng(&self) -> Result<SmallRng, SolverError> {
        let stream = self
            .next_stream
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |next| {
                next.checked_add(1)
            })
            .map_err(|_| {
                SolverError::Algorithm("random-assignment stream counter exhausted".to_owned())
            })?;
        Ok(SmallRng::seed_from_u64(self.seed ^ stream))
    }
}

impl SolverAlgorithm for RandomAssignmentAlgorithm {
    type State = ();
    type Delta = ();

    fn initial_state(&self, _scope: &SolverScope) {}

    fn solve(
        &self,
        _snapshot: &(),
        scope: &SolverScope,
        input: &SolveInput,
    ) -> Result<(SolverProposal, ()), SolverError> {
        if input.requests.is_empty() {
            return Ok((SolverProposal::default(), ()));
        }
        validate_solve_input(input, scope)?;

        let mut rng = self.next_rng()?;
        let mut placements = Vec::with_capacity(input.requests.len());
        let mut cumulative_count = input.qos.cumulative_err.count;
        let mut cumulative_sum = input.qos.cumulative_err.sum;
        let mut window_count = input.qos.error_window.baseline_err.count;
        let mut window_sum = input.qos.error_window.baseline_err.sum;
        let window_start = input
            .current_slot
            .saturating_sub(input.qos.error_window.past_slots)
            .max(0);
        let window_end = input
            .current_slot
            .saturating_add(input.qos.error_window.future_slots)
            .min(scope.total_slots as i32 - 1);

        for request in &input.requests {
            let (earliest_slot, latest_slot) = request_slot_bounds(input, request, scope)?;
            let late = input.current_slot > request.deadline_slot;
            let scheduled_slot = match self.mode {
                RandomAssignmentMode::RandomSlot => rng.gen_range(earliest_slot..=latest_slot),
                RandomAssignmentMode::ArrivalSlot => earliest_slot,
            };

            let cumulative_average = if cumulative_count > 0.0 {
                cumulative_sum / cumulative_count
            } else {
                0.0
            };
            let window_average = if window_count > 0.0 {
                window_sum / window_count
            } else {
                0.0
            };
            let fallback = cumulative_average > input.qos.max_average_error
                || window_average > input.qos.max_average_error;

            let flavour = if fallback {
                get_fallback_flav(&input.qos.flavours)?
            } else {
                let flavour_index = rng.gen_range(0..input.qos.flavours.len());
                &input.qos.flavours[flavour_index]
            };

            placements.push(ProposedPlacement {
                req_id: request.request_id,
                scheduled_slot,
                flavour_name: flavour.name.clone(),
                late,
                origin: if fallback {
                    PlacementOrigin::GreedyFallback
                } else {
                    PlacementOrigin::Solver
                },
            });

            cumulative_count += 1.0;
            cumulative_sum += flavour.error;
            if (window_start..=window_end).contains(&scheduled_slot) {
                window_count += 1.0;
                window_sum += flavour.error;
            }
        }

        Ok((SolverProposal { placements }, ()))
    }

    fn merge(&self, _state: &mut (), _delta: (), _committed: &crate::engine::solver::CommitReport) {
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use crate::engine::qos::QosProfileId;
    use crate::engine::solver::{
        ErrorTotals, ErrorWindowInput, QoSProfileInput, SolveInput, SolveRequest, SolverAlgorithm,
        SolverScope,
    };
    use crate::types::{CapacityTier, Flavour};

    use super::{RandomAssignmentAlgorithm, RandomAssignmentMode};

    fn input(requests: Vec<SolveRequest>) -> SolveInput {
        SolveInput {
            current_slot: 0,
            requests,
            carbon_forecast: vec![10.0; 4],
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
                    name: "Only".to_owned(),
                    error: 5.0,
                    duration: 1.0,
                }],
                cumulative_error_constraint_active: false,
                cumulative_err: ErrorTotals::default(),
                err_by_slot: HashMap::new(),
                max_average_error: 4.0,
                error_window: ErrorWindowInput {
                    baseline_err: ErrorTotals::default(),
                    past_slots: 0,
                    future_slots: 3,
                },
            },
        }
    }

    fn request(id: u64, arrival_slot: i32, deadline_slot: i32) -> SolveRequest {
        SolveRequest {
            request_id: id,
            arrival_slot,
            deadline_slot,
            assignment_deadline_slot: deadline_slot,
        }
    }

    fn scope() -> SolverScope {
        SolverScope {
            profile_id: QosProfileId::default_profile(),
            total_slots: 4,
        }
    }

    #[test]
    fn assignment_that_crosses_threshold_is_random_then_fallbacks() {
        let input = input(vec![request(1, 0, 3), request(2, 0, 3)]);
        let algorithm = RandomAssignmentAlgorithm::new(RandomAssignmentMode::ArrivalSlot, 17);
        let (proposal, _) = algorithm.solve(&(), &scope(), &input).unwrap();

        assert_eq!(proposal.placements.len(), 2);
        assert_eq!(
            proposal.placements[0].origin,
            crate::engine::solver::PlacementOrigin::Solver
        );
        assert_eq!(
            proposal.placements[1].origin,
            crate::engine::solver::PlacementOrigin::GreedyFallback
        );
    }

    #[test]
    fn equality_with_threshold_still_uses_random_selection() {
        let mut input = input(vec![request(1, 0, 3), request(2, 0, 3)]);
        input.qos.flavours[0].error = input.qos.max_average_error;
        let algorithm = RandomAssignmentAlgorithm::new(RandomAssignmentMode::ArrivalSlot, 17);
        let (proposal, _) = algorithm.solve(&(), &scope(), &input).unwrap();

        assert!(
            proposal
                .placements
                .iter()
                .all(|placement| placement.origin == crate::engine::solver::PlacementOrigin::Solver)
        );
    }

    #[test]
    fn under_budget_requests_sample_from_the_whole_flavour_set() {
        let requests = (0..256).map(|id| request(id, 0, 3)).collect();
        let mut input = input(requests);
        input.qos.max_average_error = 100.0;
        input.qos.flavours = vec![
            Flavour {
                name: "Accurate".to_owned(),
                error: 0.0,
                duration: 10.0,
            },
            Flavour {
                name: "Fast".to_owned(),
                error: 1.0,
                duration: 1.0,
            },
        ];
        let algorithm = RandomAssignmentAlgorithm::new(RandomAssignmentMode::ArrivalSlot, 99);
        let (proposal, _) = algorithm.solve(&(), &scope(), &input).unwrap();
        let accurate = proposal
            .placements
            .iter()
            .filter(|placement| placement.flavour_name == "Accurate")
            .count();
        let fast = proposal
            .placements
            .iter()
            .filter(|placement| placement.flavour_name == "Fast")
            .count();

        assert!(accurate > 0);
        assert!(fast > 0);
        assert!(
            proposal
                .placements
                .iter()
                .all(|placement| placement.origin == crate::engine::solver::PlacementOrigin::Solver)
        );
    }

    #[test]
    fn cumulative_error_above_threshold_triggers_fallback_even_if_window_is_under() {
        let mut input = input(vec![request(1, 0, 3)]);
        input.qos.cumulative_err = ErrorTotals {
            sum: 5.0,
            count: 1.0,
        };
        let algorithm = RandomAssignmentAlgorithm::new(RandomAssignmentMode::ArrivalSlot, 17);
        let (proposal, _) = algorithm.solve(&(), &scope(), &input).unwrap();

        assert_eq!(
            proposal.placements[0].origin,
            crate::engine::solver::PlacementOrigin::GreedyFallback
        );
    }

    #[test]
    fn window_error_above_threshold_triggers_fallback_even_if_cumulative_is_under() {
        let mut input = input(vec![request(1, 0, 3)]);
        input.qos.error_window.baseline_err = ErrorTotals {
            sum: 5.0,
            count: 1.0,
        };
        let algorithm = RandomAssignmentAlgorithm::new(RandomAssignmentMode::ArrivalSlot, 17);
        let (proposal, _) = algorithm.solve(&(), &scope(), &input).unwrap();

        assert_eq!(
            proposal.placements[0].origin,
            crate::engine::solver::PlacementOrigin::GreedyFallback
        );
    }

    #[test]
    fn arrival_mode_uses_the_earliest_legal_slot_and_random_mode_stays_in_range() {
        let input = input(vec![request(1, 1, 3)]);
        let arrival = RandomAssignmentAlgorithm::new(RandomAssignmentMode::ArrivalSlot, 17)
            .solve(&(), &scope(), &input)
            .unwrap()
            .0
            .placements;
        let random = RandomAssignmentAlgorithm::new(RandomAssignmentMode::RandomSlot, 17)
            .solve(&(), &scope(), &input)
            .unwrap()
            .0
            .placements;

        assert_eq!(arrival[0].scheduled_slot, 1);
        assert!((1..=3).contains(&random[0].scheduled_slot));
    }

    #[test]
    fn random_slot_fallback_keeps_the_randomly_selected_slot() {
        let mut under_budget = input(vec![request(1, 1, 3)]);
        under_budget.qos.flavours = vec![
            Flavour {
                name: "Accurate".to_owned(),
                error: 0.0,
                duration: 10.0,
            },
            Flavour {
                name: "Fast".to_owned(),
                error: 5.0,
                duration: 1.0,
            },
        ];
        let mut over_budget = under_budget.clone();
        over_budget.qos.cumulative_err = ErrorTotals {
            sum: 5.0,
            count: 1.0,
        };

        let under_slot = RandomAssignmentAlgorithm::new(RandomAssignmentMode::RandomSlot, 29)
            .solve(&(), &scope(), &under_budget)
            .unwrap()
            .0
            .placements[0]
            .scheduled_slot;
        let fallback = RandomAssignmentAlgorithm::new(RandomAssignmentMode::RandomSlot, 29)
            .solve(&(), &scope(), &over_budget)
            .unwrap()
            .0
            .placements
            .remove(0);

        assert_eq!(fallback.scheduled_slot, under_slot);
        assert_eq!(fallback.flavour_name, "Accurate");
        assert_eq!(
            fallback.origin,
            crate::engine::solver::PlacementOrigin::GreedyFallback
        );
    }

    #[test]
    fn expired_work_is_assigned_at_current_slot_and_marked_late() {
        let mut input = input(vec![request(1, 0, 1)]);
        input.current_slot = 2;
        let scope = SolverScope {
            profile_id: QosProfileId::default_profile(),
            total_slots: 4,
        };
        let algorithm = RandomAssignmentAlgorithm::new(RandomAssignmentMode::RandomSlot, 17);
        let (proposal, _) = algorithm.solve(&(), &scope, &input).unwrap();

        assert_eq!(proposal.placements[0].scheduled_slot, 2);
        assert!(proposal.placements[0].late);
    }
}
