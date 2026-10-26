use super::shared::{pick_feasible_flavour, valid_slots};

use std::{collections::HashSet, f32::consts::E};

use rand::{Rng, SeedableRng, rngs::SmallRng};

use crate::{engine::{
    solver::{
        CommitReport, ProposedPlacement, SolveInput, SolverAlgorithm, SolverError, SolverProposal,
        SolverScope,
    }, solver_algorithms::{get_fallback_flav, get_slot_cost, validate_solve_input},
}, types::Flavour};

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
    rng: SmallRng,
}

pub(super) struct BanditDelta {
    proposed_request_ids: HashSet<u64>,
    rng_after: SmallRng,
}

pub(super) struct BanditAlgorithm {
    config: BanditConfig,
}

impl BanditAlgorithm {
    pub(super) fn new(config: BanditConfig) -> Self {
        Self { config }
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
            rng: SmallRng::seed_from_u64(self.config.seed),
        }
    }

    fn solve(
        &self,
        snapshot: &Self::State,
        scope: &SolverScope,
        input: &SolveInput,
    ) -> Result<(SolverProposal, Self::Delta), SolverError> {
        validate_solve_input(&input, &scope)?;
        if input.requests.is_empty() {
            return Err(SolverError::Algorithm("no requests".to_owned()));
        }

        // Working copies of Q and N for this solve call.
        let mut wq = snapshot.q.clone();
        let mut wn = snapshot.n.clone();
        let mut rng = snapshot.rng.clone();
        // initialize placements and proposed ids
        let mut placements: Vec<ProposedPlacement> = Vec::with_capacity(input.requests.len());
        let mut proposed_request_ids: HashSet<u64> = HashSet::new();


        for req in &input.requests {
            // Choose a slot with epsilon-greedy.

            let candidates = valid_slots(
                req.arrival_slot,
                req.deadline_slot,
                input.assignment_max_future_slots,
                scope.total_slots,
            );
            if candidates.is_empty() {
                continue;
            }

            // ε-greedy slot selection.
            let chosen_slot = if rng.r#gen::<f64>() < self.config.epsilon {
                candidates[rng.gen_range(0..candidates.len())]
            } else {
                // TODO: what is happening here?
                *candidates
                    .iter()
                    .min_by(|&&a, &&b| {
                        let qa = wq.get(a as usize).copied().unwrap_or(f64::MAX);
                        let qb = wq.get(b as usize).copied().unwrap_or(f64::MAX);
                        qa.partial_cmp(&qb).unwrap_or(std::cmp::Ordering::Equal)
                    })
                    .unwrap()
            };
            
            // choose a feasible profile flavour using the QoS snapshot.
            let (chosen_flav, cost) = pick_feasible_flavour(
                chosen_slot,
                &input.carbon_forecast,
                &input.capacity_tiers,
                input.carbon_cost_duration_scale,
                &input.qos,
            )
            .unwrap_or_else(|| {
                // unwrap is safe because we checked flavours are non-empty
                let fallback_flav = get_fallback_flav(&input.qos.flavours).unwrap();
                let c = get_slot_cost(
                    input.current_slot,
                    &fallback_flav,
                    &input.carbon_forecast,
                    &input.capacity_tiers,
                    &input.baseline_slot_counts,
                    input.carbon_cost_duration_scale,
                );
                (fallback_flav, c)
            });

            // Update working_q/working_n using the forecast cost so later
            //       requests in this batch can see earlier local choices.
            
            // Incremental Q-update (running mean) on the working copy.
            let idx = chosen_slot as usize;
            if idx < wn.len() {
                wn[idx] += 1;
                wq[idx] += (cost - wq[idx]) / wn[idx] as f64;
            }

            
            // TODO: this should be later done by the commit... right?
            // slot_count.entry(chosen_slot).or_insert(0) += 1;
            // slot_errors
            //     .entry(chosen_slot)
            //     .or_default()
            //     .push(chosen_flav.error);
            // global_error_sum += chosen_flav.error;
            // global_count += 1;

            placements.push(ProposedPlacement::new(
                req.request_id,
                chosen_slot,
                chosen_flav.name.clone(),
            ));
            proposed_request_ids.insert(req.request_id);
        }

        Ok((
            SolverProposal { placements },
            BanditDelta {
                proposed_request_ids,
                rng_after: rng,
            },
        ))
    }

    fn merge(&self, state: &mut Self::State, delta: Self::Delta, report: &CommitReport) {
        for assignment in report
            .assignments
            .iter()
            .filter(|assignment| delta.proposed_request_ids.contains(&assignment.request_id))
        {
            let slot = assignment.scheduled_slot as usize;

            // Use the final price from the commit report, not the solve estimate.
            state.n[slot] += 1;
            state.q[slot] += (assignment.carbon_cost - state.q[slot]) / state.n[slot] as f64;
        }

        state.rng = delta.rng_after;
    }
}

#[cfg(test)]
mod tests {
    use crate::engine::qos::QosProfileId;

    use super::{BanditAlgorithm, BanditConfig};
    use crate::engine::solver::{SolverAlgorithm, SolverScope};

    #[test]
    fn initial_state_vectors_match_the_scope_horizon() {
        let algorithm = BanditAlgorithm::new(BanditConfig {
            epsilon: 0.1,
            initial_q: 2.0,
            seed: 42,
        });
        let scope = SolverScope {
            profile_id: QosProfileId::default_profile(),
            total_slots: 3,
        };

        let state = algorithm.initial_state(&scope);

        assert_eq!(state.q, vec![2.0; 3]);
        assert_eq!(state.n, vec![0; 3]);
    }
}
