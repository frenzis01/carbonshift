use super::shared::{pick_feasible_flavour, record_local_profile_error, valid_slots};

use std::collections::HashSet;
use std::sync::atomic::{AtomicU64, Ordering};

use rand::{Rng, SeedableRng, rngs::SmallRng};

use crate::engine::{
    solver::{
        CommitReport, ProposedPlacement, SolveInput, SolverAlgorithm, SolverError, SolverProposal,
        SolverScope,
    },
    solver_algorithms::{
        cheapest_feasible_flavour, get_fallback_flav, get_slot_cost, validate_solve_input,
    },
};

#[derive(Debug, Clone)]
pub struct BanditConfig {
    pub epsilon: f64,
    pub initial_q: f64,
    pub seed: u64,
}

#[derive(Clone)]
pub(super) struct BanditState {
    q: Vec<f64>,
    n: Vec<u64>,
}

pub(super) struct BanditDelta {
    proposed_request_ids: HashSet<u64>,
}

pub(super) struct BanditAlgorithm {
    config: BanditConfig,
    next_stream: AtomicU64,
}

impl BanditAlgorithm {
    pub(super) fn new(config: BanditConfig) -> Self {
        Self {
            config,
            next_stream: AtomicU64::new(0),
        }
    }

    fn validate_config(&self) -> Result<(), SolverError> {
        if !self.config.epsilon.is_finite() || !(0.0..=1.0).contains(&self.config.epsilon) {
            return Err(SolverError::Algorithm(
                "Bandit epsilon must be finite and between 0 and 1".to_owned(),
            ));
        }
        if !self.config.initial_q.is_finite() || self.config.initial_q < 0.0 {
            return Err(SolverError::Algorithm(
                "Bandit initial_q must be finite and non-negative".to_owned(),
            ));
        }
        Ok(())
    }

    fn next_rng(&self) -> Result<SmallRng, SolverError> {
        // Allocate streams outside clonable state so concurrent proposals do
        // not start from identical RNG snapshots.
        let stream = self
            .next_stream
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |next| {
                next.checked_add(1)
            })
            .map_err(|_| {
                SolverError::Algorithm("Bandit random stream counter exhausted".to_owned())
            })?;
        Ok(SmallRng::seed_from_u64(self.config.seed ^ stream))
    }
}

impl SolverAlgorithm for BanditAlgorithm {
    type State = BanditState;
    type Delta = BanditDelta;

    fn initial_state(&self, scope: &SolverScope) -> Self::State {
        let len = scope.total_slots;
        BanditState {
            q: vec![self.config.initial_q; len],
            n: vec![0; len],
        }
    }

    fn solve(
        &self,
        snapshot: &Self::State,
        scope: &SolverScope,
        input: &SolveInput,
    ) -> Result<(SolverProposal, Self::Delta), SolverError> {
        self.validate_config()?;
        validate_solve_input(input, scope)?;
        if input.requests.is_empty() {
            return Ok((
                SolverProposal::default(),
                BanditDelta {
                    proposed_request_ids: HashSet::new(),
                },
            ));
        }

        // Local working copies let later requests in this batch see earlier choices
        // without changing persistent learning before commit.
        let mut wq = snapshot.q.clone();
        let mut wn = snapshot.n.clone();
        let mut rng = self.next_rng()?;
        let mut slot_counts = input.baseline_slot_counts.clone();
        let mut qos = input.qos.clone();
        let mut late_window_sum = qos.error_window.baseline_err.sum;
        let mut late_window_count = qos.error_window.baseline_err.count;
        let mut placements = Vec::with_capacity(input.requests.len());
        let mut proposed_request_ids = HashSet::new();
        
        for request in &input.requests {
            // If request is late, use cheapest feasible flavour
            if input.current_slot > request.deadline_slot {
                let flavour = cheapest_feasible_flavour(
                    &qos.flavours,
                    qos.max_average_error,
                    qos.cumulative_error_constraint_active,
                    qos.cumulative_err.count as i64,
                    qos.cumulative_err.sum,
                    late_window_count,
                    late_window_sum,
                )?
                .clone();
                let slot = input.current_slot;
                let estimated_cost = get_slot_cost(
                    slot,
                    &flavour,
                    &input.carbon_forecast,
                    &input.capacity_tiers,
                    &slot_counts,
                    input.carbon_cost_duration_scale,
                );
                *slot_counts.entry(slot).or_default() += 1;
                record_local_profile_error(&mut qos, slot, flavour.error)?;
                late_window_count += 1.0;
                late_window_sum += flavour.error;
                update_working_mean(&mut wq, &mut wn, slot, estimated_cost)?;
                // Mark request as late
                placements.push(ProposedPlacement::late(
                    request.request_id,
                    slot,
                    flavour.name.clone(),
                ));
                proposed_request_ids.insert(request.request_id);
                continue;
            }

            let (earliest_slot, deadline_slot) = super::request_slot_bounds(input, request, scope)?;
            let candidates = valid_slots(
                earliest_slot,
                deadline_slot,
                input.assignment_max_future_slots,
                scope.total_slots,
            );
            // Should not happen, unless there has been improper configuration/input
            if candidates.is_empty() {
                return Err(SolverError::Algorithm(format!(
                    "request {} has no feasible Bandit slot",
                    request.request_id
                )));
            }

            // Filter candidate slots to check feasibility,
            // (slot, flavour, cost) for feasible slots
            let mut feasible: Vec<_> = candidates
                .iter()
                .filter_map(|&slot| {
                    pick_feasible_flavour(
                        slot,
                        &input.carbon_forecast,
                        &input.capacity_tiers,
                        &slot_counts,
                        input.carbon_cost_duration_scale,
                        &qos,
                    )
                    .map(|(flavour, cost)| (slot, flavour.clone(), cost))
                })
                .collect();
            let (chosen_slot, chosen_flav, cost) = if feasible.is_empty() {
                // Fall back after every legal slot has failed the QoS checks (hence no feasible tuple)
                // and use the earliest legal slot.
                let chosen_slot = earliest_slot;
                let fallback_flav = get_fallback_flav(&qos.flavours)?.clone();
                let cost = get_slot_cost(
                    chosen_slot,
                    &fallback_flav,
                    &input.carbon_forecast,
                    &input.capacity_tiers,
                    &slot_counts,
                    input.carbon_cost_duration_scale,
                );
                (chosen_slot, fallback_flav, cost)
            } else {
                // Explore among feasible placements; otherwise prefer the
                // feasible slot with the smallest learned average cost.
                let selected_index = if rng.r#gen::<f64>() < self.config.epsilon {
                    // EXPLORATION branch
                    // Explore randomly with probability epsilon
                    rng.gen_range(0..feasible.len())
                } else {
                    // EXPLOITATION branch
                    // Otherwise pick the best known feasible slot
                    feasible
                        .iter()
                        .enumerate()
                        .min_by(|(_, (left_slot, _, _)), (_, (right_slot, _, _))| {
                            let left_q = wq.get(*left_slot as usize).copied().unwrap_or(f64::MAX);
                            let right_q = wq.get(*right_slot as usize).copied().unwrap_or(f64::MAX);
                            left_q.total_cmp(&right_q)
                        })
                        .map(|(index, _)| index)
                        .expect("feasible placement list is non-empty")
                };
                feasible.swap_remove(selected_index)
            };

            update_working_mean(&mut wq, &mut wn, chosen_slot, cost)?;
            *slot_counts.entry(chosen_slot).or_default() += 1;
            record_local_profile_error(&mut qos, chosen_slot, chosen_flav.error)?;
            placements.push(ProposedPlacement::new(
                request.request_id,
                chosen_slot,
                chosen_flav.name.clone(),
            ));
            proposed_request_ids.insert(request.request_id);
        }

        Ok((
            SolverProposal { placements },
            BanditDelta {
                proposed_request_ids,
            },
        ))
    }

    fn merge(&self, state: &mut Self::State, delta: Self::Delta, report: &CommitReport) {
        let committed: Vec<_> = report
            .assignments
            .iter()
            .filter(|assignment| delta.proposed_request_ids.contains(&assignment.request_id))
            .collect();
        if committed.is_empty() {
            return;
        }

        for assignment in committed {
            let slot = assignment.scheduled_slot as usize;

            // Use the final price from the commit report, not the solve estimate.
            state.n[slot] += 1;
            state.q[slot] += (assignment.carbon_cost - state.q[slot]) / state.n[slot] as f64;
        }
    }
}

fn update_working_mean(
    q: &mut [f64],
    n: &mut [u64],
    slot: i32,
    cost: f64,
) -> Result<(), SolverError> {
    let index = usize::try_from(slot)
        .map_err(|_| SolverError::Algorithm(format!("invalid negative slot {slot}")))?;
    let (Some(q_value), Some(count)) = (q.get_mut(index), n.get_mut(index)) else {
        return Err(SolverError::Algorithm(format!(
            "slot {slot} exceeds Bandit's state horizon"
        )));
    };
    *count = count
        .checked_add(1)
        .ok_or_else(|| SolverError::Algorithm("Bandit sample count overflowed u64".to_owned()))?;
    *q_value += (cost - *q_value) / *count as f64;
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use crate::engine::qos::QosProfileId;
    use crate::engine::solver::{
        CommitReport, ErrorTotals, QoSProfileInput, SolveInput, SolveRequest,
    };
    use crate::types::{Assignment, CapacityTier, Flavour};
    use rand::RngCore;

    use super::{BanditAlgorithm, BanditConfig};
    use crate::engine::solver::{SolverAlgorithm, SolverScope};

    fn scope() -> SolverScope {
        SolverScope {
            profile_id: QosProfileId::default_profile(),
            total_slots: 3,
        }
    }

    fn input(requests: Vec<SolveRequest>) -> SolveInput {
        SolveInput {
            current_slot: 0,
            requests,
            carbon_forecast: vec![2.0, 4.0, 8.0],
            capacity_tiers: vec![CapacityTier {
                max_requests: None,
                multiplier: 1.0,
            }],
            baseline_slot_counts: HashMap::new(),
            carbon_cost_duration_scale: 1.0,
            assignment_max_future_slots: 2,
            assignment_max_slot: Some(2),
            qos: QoSProfileInput {
                profile_id: QosProfileId::default_profile(),
                flavours: vec![Flavour {
                    name: "Balanced".to_owned(),
                    error: 0.0,
                    duration: 3.0,
                }],
                cumulative_error_constraint_active: false,
                cumulative_err: ErrorTotals::default(),
                err_by_slot: HashMap::new(),
                max_average_error: 10.0,
                error_window: crate::engine::solver::ErrorWindowInput {
                    baseline_err: ErrorTotals::default(),
                    past_slots: 0,
                    future_slots: 2,
                },
            },
        }
    }

    fn assignment(request_id: u64, slot: i32, final_cost: f64) -> Assignment {
        Assignment::new(
            request_id,
            slot,
            "Balanced".to_owned(),
            final_cost,
            0.0,
            3.0,
            Some(0),
            Some(2),
        )
    }

    #[test]
    fn initial_state_vectors_match_the_scope_horizon() {
        let algorithm = BanditAlgorithm::new(BanditConfig {
            epsilon: 0.1,
            initial_q: 2.0,
            seed: 42,
        });
        let scope = scope();

        let state = algorithm.initial_state(&scope);

        assert_eq!(state.q, vec![2.0; 3]);
        assert_eq!(state.n, vec![0; 3]);
    }

    #[test]
    fn merge_learns_final_commit_price_and_ignores_unproposed_assignments() {
        let algorithm = BanditAlgorithm::new(BanditConfig {
            epsilon: 0.0,
            initial_q: 1.0,
            seed: 42,
        });
        let scope = scope();
        let mut state = algorithm.initial_state(&scope);
        let input = input(vec![
            SolveRequest {
                request_id: 1,
                arrival_slot: 1,
                deadline_slot: 1,
            },
            SolveRequest {
                request_id: 2,
                arrival_slot: 2,
                deadline_slot: 2,
            },
        ]);
        let (_, delta) = algorithm.solve(&state, &scope, &input).unwrap();

        algorithm.merge(
            &mut state,
            delta,
            &CommitReport {
                assignments: vec![assignment(1, 1, 17.0), assignment(999, 2, 91.0)],
            },
        );

        assert_eq!(state.n, vec![0, 1, 0]);
        assert_eq!(state.q, vec![1.0, 17.0, 1.0]);
    }

    #[test]
    fn no_qos_feasible_slot_uses_the_earliest_accurate_fallback() {
        let algorithm = BanditAlgorithm::new(BanditConfig {
            epsilon: 0.0,
            initial_q: 1.0,
            seed: 42,
        });
        let scope = scope();
        let mut state = algorithm.initial_state(&scope);
        state.q[1] = 100.0;
        state.q[2] = 0.0;
        let mut solve_input = input(vec![SolveRequest {
            request_id: 1,
            arrival_slot: 1,
            deadline_slot: 2,
        }]);
        solve_input.qos.cumulative_error_constraint_active = true;
        solve_input.qos.cumulative_err = ErrorTotals {
            sum: 10.0,
            count: 2.0,
        };
        solve_input.qos.max_average_error = 1.0;

        let (proposal, _) = algorithm.solve(&state, &scope, &solve_input).unwrap();

        assert_eq!(proposal.placements[0].scheduled_slot, 1);
        assert_eq!(proposal.placements[0].flavour_name, "Balanced");
        assert!(!proposal.placements[0].late);
    }

    #[test]
    fn empty_commit_does_not_update_bandit_learning() {
        let algorithm = BanditAlgorithm::new(BanditConfig {
            epsilon: 0.5,
            initial_q: 3.0,
            seed: 42,
        });
        let scope = scope();
        let mut state = algorithm.initial_state(&scope);
        let input = input(vec![SolveRequest {
            request_id: 1,
            arrival_slot: 1,
            deadline_slot: 1,
        }]);
        let (_, delta) = algorithm.solve(&state, &scope, &input).unwrap();

        algorithm.merge(&mut state, delta, &CommitReport::default());

        assert_eq!(state.n, vec![0; 3]);
        assert_eq!(state.q, vec![3.0; 3]);
    }

    #[test]
    fn each_solve_receives_a_distinct_random_stream() {
        let algorithm = BanditAlgorithm::new(BanditConfig {
            epsilon: 0.5,
            initial_q: 3.0,
            seed: 42,
        });
        let mut first = algorithm.next_rng().unwrap();
        let mut second = algorithm.next_rng().unwrap();

        assert_ne!(first.next_u64(), second.next_u64());
    }

    #[test]
    fn invalid_bandit_configuration_is_rejected() {
        let algorithm = BanditAlgorithm::new(BanditConfig {
            epsilon: 1.1,
            initial_q: 3.0,
            seed: 42,
        });

        let result = algorithm.solve(
            &algorithm.initial_state(&scope()),
            &scope(),
            &input(Vec::new()),
        );

        match result {
            Err(error) => assert!(error.to_string().contains("epsilon")),
            Ok(_) => panic!("invalid epsilon should be rejected"),
        }
    }
}
