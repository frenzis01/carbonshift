use std::collections::HashSet;

use rand::{Rng, SeedableRng, rngs::SmallRng};

use crate::{
    engine::{
        solver::{
            CommitReport, ProposedPlacement, SolveInput, SolverAlgorithm, SolverError,
            SolverProposal, SolverScope,
        },
        solver_algorithms::{
            get_fallback_flav, get_slot_cost,
            shared::{pick_feasible_flavour, valid_slots},
            validate_solve_input,
        },
    },
    types::Flavour,
};

#[derive(Debug, Clone)]
pub struct AcoConfig {
    pub n_ants: usize,
    /// Number of ACO iterations per batch (default 1 is fine for online use
    /// since pheromone accumulates across many batches).
    pub n_iterations: usize,
    // TODO: what are these two?
    pub alpha: f64,
    pub beta: f64,
    // Pheromone evaporation rate
    pub rho: f64,
    // TODO: Is this the deposit quantity divided by solution cost?
    pub deposit: f64,
    pub initial_pheromone: f64,
    pub seed: u64,
}

#[derive(Clone)]
pub(super) struct AcoState {
    tau: Vec<f64>,
    /// Per-slot pheromone levels (updated after each batch)
    // eta: Vec<f64>,    /// Per-slot static heuristic (1/error) is computed at each solve
    rng: SmallRng,
}

pub(super) struct AcoDelta {
    proposed_request_ids: HashSet<u64>,
    evaporation_factor: f64,
    rng_after: SmallRng,
}

pub(super) struct AcoAlgorithm {
    config: AcoConfig,
}

impl AcoAlgorithm {
    pub(super) fn new(config: AcoConfig) -> Self {
        Self { config }
    }
}

impl SolverAlgorithm for AcoAlgorithm {
    type State = AcoState;
    type Delta = AcoDelta;

    fn initial_state(&self, scope: &SolverScope) -> Self::State {
        let len = scope.total_slots;
        AcoState {
            tau: vec![self.config.initial_pheromone; len],
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

        let mut sorted: Vec<&Flavour> = input.qos.flavours.iter().collect();
        sorted.sort_by(|a, b| a.duration.total_cmp(&b.duration));
        let fallback_flav = get_fallback_flav(&input.qos.flavours);

        // Working copy of pheromone, evolved through iterations exactly like
        // the serialized variant. `deposit_acc` separately tracks the net
        // additive deposit relative to `self.tau`, discounted for
        // evaporations that happen after each deposit within this call.
        let mut tau = snapshot.tau.clone();
        let mut rng = snapshot.rng.clone();
        let mut deposit_acc = vec![0f64; tau.len()];
        let mut evap_factor = 1.0f64;

        let mut best_cost = f64::INFINITY;
        let mut best_solution = Vec::with_capacity(input.requests.len());

        let cheapest_duration = sorted.first().unwrap().duration;

        // TODO: what does eta represent?
        let eta: Vec<f64> = (input.current_slot..input.assignment_max_future_slots)
            .map(|s| {
                // safe to unwrap because s is in range, input has been validated. If not, ok to panic
                let ci = input.carbon_forecast.get(s as usize).unwrap();
                let base = ci * cheapest_duration * input.carbon_cost_duration_scale;
                if base > 0.0 { 1.0 / base } else { 0.0 }
            })
            .collect();

        for _ in 0..self.config.n_iterations {
            let mut iter_best_cost = f64::INFINITY;
            let mut iter_best_solution: Vec<ProposedPlacement> = Vec::new(); // best path/ant
            for _ant in 0..self.config.n_ants {
                let mut wqos = input.qos.clone();
                // let mut slot_count = input.baseline_slot_counts.clone();
                let mut slot_counts = input.baseline_slot_counts.clone();
                let mut ant_solution: Vec<ProposedPlacement> =
                    Vec::with_capacity(input.requests.len());
                let mut ant_cost = 0.0f64;

                for req in &input.requests {
                    let candidates = valid_slots(
                        req.arrival_slot,
                        req.deadline_slot,
                        input.qos.error_window.future_slots,
                        scope.total_slots,
                    );
                    if candidates.is_empty() {
                        continue;
                    }

                    // compute weights for each candidate slot based on pheromone and eta
                    // weight = pheromone^alpha * eta^beta
                    // TODO: what do weights represent?
                    let weights: Vec<f64> = candidates
                        .iter()
                        .map(|&s| {
                            let t = tau.get(s as usize).copied().unwrap_or(1e-12).max(1e-12);
                            let e = eta.get(s as usize).copied().unwrap_or(1e-12);
                            t.powf(self.config.alpha) * e.powf(self.config.beta)
                        })
                        .collect();

                    let total_weight: f64 = weights.iter().sum();
                    // TODO: how can the weights be zero?
                    let chosen_slot = if total_weight <= 0.0 {
                        // fallback if weights are zero
                        candidates[rng.gen_range(0..candidates.len())]
                    } else {
                        let mut r = rng.r#gen::<f64>() * total_weight;
                        let mut chosen = *candidates.last().unwrap();
                        // TODO: Explain how this works
                        for (&s, &w) in candidates.iter().zip(weights.iter()) {
                            r -= w;
                            if r <= 0.0 {
                                chosen = s;
                                break;
                            }
                        }
                        chosen
                    };

                    let (chosen_flav, cost) = pick_feasible_flavour(
                        chosen_slot,
                        &input.carbon_forecast,
                        &input.capacity_tiers,
                        input.carbon_cost_duration_scale,
                        &wqos,
                    )
                    // The selected flavour borrows from `wqos`. Clone it to
                    // an owned value so that borrow ends before QoS is updated.
                    .map(|(flavour, cost)| (flavour.clone(), cost))
                    .unwrap_or_else(|| {
                        let fallback_flav = get_fallback_flav(&input.qos.flavours).unwrap();
                        let c = get_slot_cost(
                            input.current_slot,
                            &fallback_flav,
                            &input.carbon_forecast,
                            &input.capacity_tiers,
                            &slot_counts,
                            input.carbon_cost_duration_scale,
                        );
                        (fallback_flav.clone(), c)
                    });

                    ant_cost += cost;
                    ant_solution.push(ProposedPlacement::new(
                        req.request_id,
                        chosen_slot,
                        chosen_flav.name.clone(),
                    ));
                    // Indexing a HashMap gives a shared reference, so update
                    // occupancy through its mutable entry instead.
                    *slot_counts.entry(chosen_slot).or_default() += 1;
                    // TODO: Aside from borrowing error Is all this updating wqos correct? is err_by_slot redundant?
                    wqos.cumulative_err.count += 1.0;
                    wqos.cumulative_err.sum += chosen_flav.error;
                    let slot_error = wqos.err_by_slot.entry(chosen_slot).or_default();
                    slot_error.count += 1.0;
                    slot_error.sum += chosen_flav.error;
                    wqos.error_window.baseline_err.count += 1.0;
                    wqos.error_window.baseline_err.sum += chosen_flav.error;
                }

                if ant_cost < iter_best_cost {
                    iter_best_cost = ant_cost;
                    iter_best_solution = ant_solution;
                }
            }

            // Evaporate both the working pheromone copy and the deposit
            // accumulator (a deposit made this iteration must decay in every
            // later iteration of this same batch, exactly like it would if
            // it had been applied directly to a shared `tau`).
            for t in tau.iter_mut() {
                *t *= 1.0 - self.config.rho;
                *t = t.max(1e-12);
            }
            for d in deposit_acc.iter_mut() {
                *d *= 1.0 - self.config.rho;
            }
            evap_factor *= 1.0 - self.config.rho;

            // Deposit new pheromones on best-ant (solution) slots
            // TODO: how can it happen that the cost is negative or not finite?
            if iter_best_cost > 0.0 && iter_best_cost.is_finite() {
                let deposit = self.config.deposit / iter_best_cost;
                for p in &iter_best_solution {
                    if let Some(t) = tau.get_mut(p.scheduled_slot as usize) {
                        *t += deposit;
                    }
                    if let Some(d) = deposit_acc.get_mut(p.scheduled_slot as usize) {
                        *d += deposit;
                    }
                }
            }

            if iter_best_cost < best_cost {
                best_cost = iter_best_cost;
                best_solution = iter_best_solution;
            }
        }

        let proposed_request_ids: HashSet<u64> = best_solution.iter().map(|p| p.req_id).collect();
        Ok((
            SolverProposal {
                placements: best_solution,
            },
            AcoDelta {
                proposed_request_ids,
                evaporation_factor: evap_factor,
                rng_after: rng,
            },
        ))
    }

    fn merge(&self, _state: &mut Self::State, _delta: Self::Delta, _committed: &CommitReport) {
        todo!()
    }
}

#[cfg(test)]
mod tests {
    use crate::engine::qos::QosProfileId;

    use super::{AcoAlgorithm, AcoConfig};
    use crate::engine::solver::{SolverAlgorithm, SolverScope};

    #[test]
    fn initial_state_pheromones_match_the_scope_horizon() {
        let algorithm = AcoAlgorithm::new(AcoConfig {
            n_ants: 1,
            n_iterations: 1,
            alpha: 1.0,
            beta: 1.0,
            rho: 0.5,
            deposit: 1.0,
            initial_pheromone: 2.0,
            seed: 42,
        });
        let scope = SolverScope {
            profile_id: QosProfileId::default_profile(),
            total_slots: 3,
        };

        let state = algorithm.initial_state(&scope);

        assert_eq!(state.tau, vec![2.0; 3]);
    }
}
