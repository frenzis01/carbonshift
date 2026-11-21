/// ACO algorithm wrap up for slot assignment
///  
/// Every ant chooses QoS-feasible slots using the candidate's estimated carbon
/// cost and pheromone deposited by previous best solutions.
///     - tau represents the pheromone deposit for each slot, and gets updated
///         - locally at every iteration
///         - globally after commit (n_iterations)
///     - eta represents the heuristic desirability of a slot, and is re-computed at each request assignment choice.
/// Every ant is a solution, i.e. a SolverProposal: Vec<ProposedPlacement>
/// 1. Each ant chooses a legal slot using a QoS-feasible flavour, or the
///    minimum-error flavour when no legal slot can satisfy QoS.
/// 2. The iteration’s best ant has cost (L).
/// 3. The working pheromone evaporates: tau[i] *= 1 - rho.
/// 4. Each placement in that best solution adds a deposit of Q / L to its slot, where Q is q_deposit.
/// This is repeated each iteration.
/// Local changes are applied per iteration, not globally. Only the committed best solution globally updates pheromone.
use std::collections::HashSet;
use std::sync::atomic::{AtomicU64, Ordering};

use rand::{Rng, SeedableRng, rngs::SmallRng};

use crate::{
    engine::solver::{
        CommitReport, ProposedPlacement, SolveInput, SolverAlgorithm, SolverError, SolverProposal,
        SolverScope,
    },
    types::Flavour,
};

use super::shared::{
    cheapest_feasible_flavour, get_fallback_flav, get_slot_cost, pick_feasible_flavour,
    record_local_profile_error, request_slot_bounds, valid_slots, validate_solve_input,
};

#[derive(Debug, Clone)]
pub struct AcoConfig {
    pub n_ants: usize,
    /// Local search iterations for one proposal; this is separate from the
    /// persistent pheromone update, which happens once after commit.
    pub n_iterations: usize,
    /// Exponent for pheromone influence in slot-selection probabilities.
    /// The higher, the more pheromone matters in slot choice
    pub alpha: f64,
    /// Exponent for inverse-estimated-cost influence in slot probabilities.
    /// The higher, the more cost heuristic matters in slot choice
    pub beta: f64,
    /// Fraction of persistent pheromone evaporated after one committed batch.
    pub rho: f64,
    /// Classical ACO deposit constant Q. A solution deposits Q / L, where L
    /// is the sum of its final committed carbon costs.
    pub q_deposit: f64,
    /// Starting pheromone on every slot.
    pub initial_pheromone: f64,
    pub seed: u64,
}

#[derive(Clone)]
pub(super) struct AcoState {
    // tau[i] is the learned attractiveness of assigning work to slot i
    tau: Vec<f64>,
}

pub(super) struct AcoDelta {
    proposed_request_ids: HashSet<u64>,
}

pub(super) struct AcoAlgorithm {
    config: AcoConfig,
    next_stream: AtomicU64,
}

impl AcoAlgorithm {
    pub(super) fn new(config: AcoConfig) -> Self {
        Self {
            config,
            next_stream: AtomicU64::new(0),
        }
    }

    fn next_rng(&self) -> Result<SmallRng, SolverError> {
        // The allocator belongs to the shared algorithm, not its clonable
        // state snapshot, so concurrent proposals cannot reuse one RNG stream.
        let stream = self
            .next_stream
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |next| {
                next.checked_add(1)
            })
            .map_err(|_| {
                SolverError::Algorithm("ACO random stream counter exhausted".to_owned())
            })?;
        Ok(SmallRng::seed_from_u64(self.config.seed ^ stream))
    }

    fn validate_config(&self) -> Result<(), SolverError> {
        if self.config.n_ants == 0 || self.config.n_iterations == 0 {
            return Err(SolverError::Algorithm(
                "ACO requires at least one ant and iteration".to_owned(),
            ));
        }
        if !self.config.alpha.is_finite()
            || self.config.alpha < 0.0
            || !self.config.beta.is_finite()
            || self.config.beta < 0.0
        {
            return Err(SolverError::Algorithm(
                "ACO alpha and beta must be finite and non-negative".to_owned(),
            ));
        }
        if !self.config.rho.is_finite() || !(0.0..1.0).contains(&self.config.rho) {
            return Err(SolverError::Algorithm(
                "ACO rho must be finite and between 0 and 1".to_owned(),
            ));
        }
        if !self.config.q_deposit.is_finite() || self.config.q_deposit <= 0.0 {
            return Err(SolverError::Algorithm(
                "ACO q_deposit must be finite and positive".to_owned(),
            ));
        }
        if !self.config.initial_pheromone.is_finite() || self.config.initial_pheromone <= 0.0 {
            return Err(SolverError::Algorithm(
                "ACO initial pheromone must be finite and positive".to_owned(),
            ));
        }
        Ok(())
    }
}

impl SolverAlgorithm for AcoAlgorithm {
    type State = AcoState;
    type Delta = AcoDelta;

    fn initial_state(&self, scope: &SolverScope) -> Self::State {
        AcoState {
            tau: vec![self.config.initial_pheromone; scope.total_slots],
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
                AcoDelta {
                    proposed_request_ids: HashSet::new(),
                },
            ));
        }

        // Working copy of pheromone levels
        let mut tau = snapshot.tau.clone();
        let mut rng = self.next_rng()?;
        // These `late_` vars mark the starting state after the late requests are handled
        let mut late_qos = input.qos.clone();
        let mut late_slot_counts = input.baseline_slot_counts.clone();
        let mut late_cost = 0.0;
        let mut late_window_sum = late_qos.error_window.baseline_err.sum;
        let mut late_window_count = late_qos.error_window.baseline_err.count;
        // These track the state of late requests theirselves
        let mut late_placements = Vec::new();
        let mut late_request_ids = HashSet::new();

        // Expired requests are fixed at the current slot before ants search
        // ordinary requests, matching the DP and singleton policies.
        for request in input
            .requests
            .iter()
            .filter(|request| input.current_slot > request.deadline_slot)
        {
            // Find cheapest feasible flavour and mark request as late
            let flavour = cheapest_feasible_flavour(
                &late_qos.flavours,
                late_qos.max_average_error,
                late_qos.cumulative_error_constraint_active,
                late_qos.cumulative_err.count as i64,
                late_qos.cumulative_err.sum,
                late_window_count,
                late_window_sum,
            )?
            .clone();
            let cost = get_slot_cost(
                input.current_slot,
                &flavour,
                &input.carbon_forecast,
                &input.capacity_tiers,
                &late_slot_counts,
                input.carbon_cost_duration_scale,
            );
            // Update slot counts and errors
            *late_slot_counts.entry(input.current_slot).or_default() += 1;
            record_local_profile_error(&mut late_qos, input.current_slot, flavour.error)?;
            late_window_sum += flavour.error;
            late_window_count += 1.0;
            late_cost += cost;
            late_request_ids.insert(request.request_id);
            // Record as late
            late_placements.push(ProposedPlacement::late(
                request.request_id,
                input.current_slot,
                flavour.name.clone(),
            ));
        }

        // Filter on-time requests to exclude
        let on_time_requests: Vec<_> = input
            .requests
            .iter()
            .filter(|request| !late_request_ids.contains(&request.request_id))
            .collect();
        if on_time_requests.is_empty() {
            return Ok((
                SolverProposal {
                    placements: late_placements,
                },
                AcoDelta {
                    proposed_request_ids: late_request_ids,
                },
            ));
        }

        // ACO works on a double loop: iterations and ants
        // Each ant builds a solution for all on-time requests
        // Iterations are loop over ants
        // We get n_iterations X n_ants solutions

        // Initialize best solution tracking
        let mut best_cost = f64::INFINITY;
        let mut best_solution = Vec::new();

        // Ants may use forecasts while searching. Only the final proposal's
        // accepted assignments teach persistent pheromone state in `merge`;
        // We keep pheromone updates local to this run as well as counts
        for _ in 0..self.config.n_iterations {
            let mut iteration_best_cost = f64::INFINITY;
            let mut iteration_best_solution = Vec::new();

            // Loop over ants
            for _ in 0..self.config.n_ants {
                let mut qos = late_qos.clone();
                let mut slot_counts = late_slot_counts.clone();
                let mut solution = late_placements.clone();
                let mut ant_cost = late_cost;

                for request in &on_time_requests {
                    let (earliest, deadline) = request_slot_bounds(input, request, scope)?;
                    let candidates = valid_slots(earliest, deadline, scope.total_slots);
                    // Should not happen unless input is inconsistent
                    if candidates.is_empty() {
                        return Err(SolverError::Algorithm(format!(
                            "request {} has no feasible ACO slot",
                            request.request_id
                        )));
                    }

                    // Filter feasible slots based on QoS and cost,
                    // getting (slot, flavour, cost) tuples for feasible slots
                    let mut feasible: Vec<_> = candidates
                        .iter()
                        .filter_map(|&slot| {
                            pick_feasible_flavour(
                                slot,
                                request.arrival_slot.max(input.current_slot),
                                &input.carbon_forecast,
                                &input.capacity_tiers,
                                &slot_counts,
                                input.carbon_cost_duration_scale,
                                &qos,
                            )
                            .map(|(flavour, cost)| (slot, flavour.clone(), cost))
                        })
                        .collect();

                    let used_infeasibility_fallback = feasible.is_empty();
                    if used_infeasibility_fallback {
                        let fallback = get_fallback_flav(&qos.flavours)?.clone();
                        feasible = candidates
                            .iter()
                            .map(|&slot| {
                                let cost = get_slot_cost(
                                    slot,
                                    &fallback,
                                    &input.carbon_forecast,
                                    &input.capacity_tiers,
                                    &slot_counts,
                                    input.carbon_cost_duration_scale,
                                );
                                (slot, fallback.clone(), cost)
                            })
                            .collect();
                    }

                    // The ant chooses the slot normally, whether the flavour
                    // came from the feasible set or the min-error fallback.
                    let chosen_slot = choose_slot(
                        &feasible,
                        &tau,
                        &mut rng,
                        self.config.alpha,
                        self.config.beta,
                    )?;
                    let selected_index = feasible
                        .iter()
                        .position(|(slot, _, _)| *slot == chosen_slot)
                        .expect("ACO selected a slot from its legal candidates");
                    let (_, flavour, cost) = feasible.swap_remove(selected_index);

                    // Update ant cost and solution
                    ant_cost += cost;
                    solution.push(if used_infeasibility_fallback {
                        ProposedPlacement::infeasibility_fallback(
                            request.request_id,
                            chosen_slot,
                            flavour.name.clone(),
                        )
                    } else {
                        ProposedPlacement::new(
                            request.request_id,
                            chosen_slot,
                            flavour.name.clone(),
                        )
                    });
                    *slot_counts.entry(chosen_slot).or_default() += 1;
                    record_local_profile_error(&mut qos, chosen_slot, flavour.error)?;
                }

                // Update best ant in this iteration
                if ant_cost < iteration_best_cost {
                    iteration_best_cost = ant_cost;
                    iteration_best_solution = solution;
                }
            }

            // Local pheromone search: each iteration evaporates the working
            // copy and reinforces that iteration's best ant.
            for pheromone in &mut tau {
                *pheromone = (*pheromone * (1.0 - self.config.rho)).max(1e-12);
            }
            if iteration_best_cost.is_finite() && iteration_best_cost > 0.0 {
                let deposit = self.config.q_deposit / iteration_best_cost;
                for placement in &iteration_best_solution {
                    if let Some(pheromone) = tau.get_mut(placement.scheduled_slot as usize) {
                        *pheromone += deposit;
                    }
                }
            }

            // Update best solution among all iterations
            if iteration_best_cost < best_cost {
                best_cost = iteration_best_cost;
                best_solution = iteration_best_solution;
            }
        }

        // Return the best solution found
        let proposed_request_ids = best_solution
            .iter()
            .map(|placement| placement.req_id)
            .collect();
        Ok((
            SolverProposal {
                placements: best_solution,
            },
            AcoDelta {
                proposed_request_ids,
            },
        ))
    }

    fn merge(&self, state: &mut Self::State, delta: Self::Delta, report: &CommitReport) {
        // A commit report contains final assignments; keep only the ones
        // proposed by this solve so unrelated work cannot train this state.
        let committed: Vec<_> = report
            .assignments
            .iter()
            .filter(|assignment| delta.proposed_request_ids.contains(&assignment.request_id))
            .collect();
        if committed.is_empty() {
            // A rejected proposal is not feedback, so it must neither
            // evaporate existing pheromones nor add new deposits.
            return;
        }

        // The runtime serializes commits, which defines the order of these
        // intentionally order-sensitive persistent updates. Each accepted
        // proposal is one learning event; local search iterations do not
        // cause repeated evaporation of the persistent state.
        for pheromone in &mut state.tau {
            *pheromone = (*pheromone * (1.0 - self.config.rho)).max(1e-12);
        }

        // Use the commit-time prices because capacity-tier repricing can make
        // them differ from the estimates the ants used while searching.
        let total_committed_cost: f64 = committed
            .iter()
            .map(|assignment| assignment.carbon_cost)
            .sum();
        if total_committed_cost.is_finite() && total_committed_cost > 0.0 {
            // In classic ACO, a lower-cost solution (smaller L) leaves a
            // larger Q/L trace on each slot it used.
            let deposit = self.config.q_deposit / total_committed_cost;
            for assignment in committed {
                if let Ok(slot) = usize::try_from(assignment.scheduled_slot) {
                    if let Some(pheromone) = state.tau.get_mut(slot) {
                        // Deposit once per accepted request; requests sharing
                        // a slot reinforce that slot once each.
                        *pheromone += deposit;
                    }
                }
            }
        }
    }
}

/// Chooses a slot based on pheromone and candidate-specific feasible costs.
/// Returns the chosen slot or an error if none are available
fn choose_slot<R: Rng + ?Sized>(
    candidates: &[(i32, Flavour, f64)],
    pheromone: &[f64],
    rng: &mut R,
    alpha: f64,
    beta: f64,
) -> Result<i32, SolverError> {
    // 1. Compute log weights for each candidate slot
    // Keep the log weights aligned with `candidates`; the matching slot is
    // selected after converting them back to ordinary positive weights.
    let mut log_weights = Vec::with_capacity(candidates.len());
    for (slot, _, cost) in candidates {
        log_weights.push(slot_log_weight(*slot, *cost, pheromone, alpha, beta)?);
    }

    // 2. Convert log weights to normal weights safely
    // Get the largest log weight for stability
    let largest_log_weight = log_weights
        .iter()
        .copied()
        .max_by(f64::total_cmp)
        .ok_or_else(|| SolverError::Algorithm("ACO has no candidate slots".to_owned()))?;
    // Subtracting the maximum changes all weights by the same factor, so the
    // probabilities stay the same while exponentiation remains numerically safe.
    let weights: Vec<f64> = log_weights
        .iter()
        .map(|weight| (weight - largest_log_weight).exp())
        .collect();
    let total_weight: f64 = weights.iter().sum();

    // 3. Draw a random slot based on weights
    // Roulette-wheel selection draws a point in the total weight and walks
    // the candidates until their cumulative weight crosses that point.
    let mut draw = rng.r#gen::<f64>() * total_weight;

    // Walk through candidates until the draw is below zero
    for ((slot, _, _), weight) in candidates.iter().zip(weights) {
        draw -= weight;
        if draw < 0.0 {
            return Ok(*slot);
        }
    }
    // A rounding remainder may be left after subtracting all normalized weights.
    candidates
        .last()
        .map(|(slot, _, _)| *slot)
        .ok_or_else(|| SolverError::Algorithm("ACO has no candidate slots".to_owned()))
}

fn slot_log_weight(
    slot: i32,
    candidate_cost: f64,
    pheromone: &[f64],
    alpha: f64,
    beta: f64,
) -> Result<f64, SolverError> {
    // Slots are absolute indexes in the pheromone vector.
    let index = usize::try_from(slot)
        .map_err(|_| SolverError::Algorithm(format!("negative ACO slot {slot}")))?;
    if !candidate_cost.is_finite() || candidate_cost < 0.0 {
        return Err(SolverError::Algorithm(format!(
            "ACO feasible candidate cost for slot {slot} must be finite and non-negative"
        )));
    }
    // ETA uses the actual cheapest QoS-feasible flavour for this candidate,
    // including its forecast intensity and current capacity-tier position.
    let eta = 1.0 / candidate_cost.max(1e-12);

    // 3. Combine pheromone tau and heuristic eta
    // Floors keep both logarithms defined; the log form represents
    // `tau^alpha * eta^beta` without multiplying potentially large values.
    let tau = pheromone
        .get(index)
        .copied()
        .ok_or_else(|| SolverError::Algorithm(format!("ACO state misses slot {slot}")))?
        .max(1e-12);

    // Apply alpha to pheromone and beta to eta
    let log_weight = alpha * tau.ln() + beta * eta.ln();
    if !log_weight.is_finite() {
        return Err(SolverError::Algorithm(format!(
            "ACO selection weight for slot {slot} is not finite"
        )));
    }
    Ok(log_weight)
}

#[cfg(test)]
mod tests {
    use std::collections::{HashMap, HashSet};

    use rand::RngCore;

    use crate::engine::qos::QosProfileId;
    use crate::engine::solver::{
        CommitReport, ErrorTotals, PlacementOrigin, QoSProfileInput, SolveInput, SolveRequest,
        SolverAlgorithm, SolverScope,
    };
    use crate::types::{Assignment, CapacityTier, Flavour};

    use super::{AcoAlgorithm, AcoConfig, AcoDelta};

    fn config() -> AcoConfig {
        AcoConfig {
            n_ants: 2,
            n_iterations: 4,
            alpha: 1.0,
            beta: 1.0,
            rho: 0.2,
            q_deposit: 10.0,
            initial_pheromone: 2.0,
            seed: 42,
        }
    }

    fn scope() -> SolverScope {
        SolverScope {
            profile_id: QosProfileId::default_profile(),
            total_slots: 4,
        }
    }

    fn input() -> SolveInput {
        SolveInput {
            current_slot: 0,
            requests: vec![SolveRequest {
                request_id: 1,
                arrival_slot: 0,
                deadline_slot: 3,
                assignment_deadline_slot: 3,
            }],
            carbon_forecast: vec![1.0, 4.0, 2.0, 8.0],
            capacity_tiers: vec![CapacityTier {
                max_requests: None,
                multiplier: 1.0,
            }],
            baseline_slot_counts: HashMap::new(),
            carbon_cost_duration_scale: 1.0,
            forecast_max_slot: Some(3),
            qos: QoSProfileInput {
                profile_id: QosProfileId::default_profile(),
                flavours: vec![
                    Flavour {
                        name: "Accurate".into(),
                        error: 0.0,
                        duration: 4.0,
                    },
                    Flavour {
                        name: "Fast".into(),
                        error: 2.0,
                        duration: 1.0,
                    },
                ],
                cumulative_error_constraint_active: false,
                cumulative_err: ErrorTotals::default(),
                err_by_slot: HashMap::new(),
                max_average_error: 10.0,
                error_window: crate::engine::solver::ErrorWindowInput {
                    baseline_err: ErrorTotals::default(),
                    past_slots: 0,
                    future_slots: 3,
                },
            },
        }
    }

    fn committed_assignment(request_id: u64, slot: i32, cost: f64) -> Assignment {
        Assignment::new(
            request_id,
            slot,
            "Fast".to_owned(),
            cost,
            2.0,
            1.0,
            Some(0),
            Some(3),
        )
    }

    #[test]
    fn initial_state_pheromones_match_scope_horizon() {
        let algorithm = AcoAlgorithm::new(config());
        let state = algorithm.initial_state(&scope());
        assert_eq!(state.tau, vec![2.0; 4]);
    }

    #[test]
    fn each_solve_receives_a_distinct_random_stream() {
        let algorithm = AcoAlgorithm::new(config());
        let mut first = algorithm.next_rng().unwrap();
        let mut second = algorithm.next_rng().unwrap();

        assert_ne!(first.next_u64(), second.next_u64());
    }

    #[test]
    fn heuristic_uses_candidate_specific_cost() {
        let pheromone = vec![1.0; 4];

        let lower_cost_weight = super::slot_log_weight(2, 8.0, &pheromone, 0.0, 1.0).unwrap();
        let higher_cost_weight = super::slot_log_weight(2, 10.0, &pheromone, 0.0, 1.0).unwrap();

        assert!(lower_cost_weight > higher_cost_weight);
    }

    #[test]
    fn solve_returns_price_free_placements_within_the_slot_window() {
        let algorithm = AcoAlgorithm::new(config());
        let scope = scope();
        let state = algorithm.initial_state(&scope);
        let mut solve_input = input();
        solve_input.current_slot = 2;
        solve_input.requests[0].arrival_slot = 2;
        solve_input.qos.error_window.future_slots = 1;
        solve_input.qos.error_window.future_slots = 1;

        let (proposal, delta) = algorithm.solve(&state, &scope, &solve_input).unwrap();

        assert_eq!(proposal.placements.len(), 1);
        assert_eq!(proposal.placements[0].req_id, 1);
        assert!((2..=3).contains(&proposal.placements[0].scheduled_slot));
        assert!(!proposal.placements[0].late);
        assert_eq!(delta.proposed_request_ids, HashSet::from([1]));
    }

    #[test]
    fn no_qos_feasible_flavour_still_uses_aco_slot_selection() {
        let mut algorithm_config = config();
        algorithm_config.n_ants = 1;
        algorithm_config.n_iterations = 1;
        algorithm_config.alpha = 0.0;
        algorithm_config.beta = 100.0;
        let algorithm = AcoAlgorithm::new(algorithm_config);
        let scope = scope();
        let mut state = algorithm.initial_state(&scope);
        let mut solve_input = input();
        solve_input.current_slot = 1;
        solve_input.requests[0].arrival_slot = 1;
        solve_input.requests[0].deadline_slot = 2;
        solve_input.qos.cumulative_error_constraint_active = true;
        solve_input.qos.cumulative_err = ErrorTotals {
            sum: 10.0,
            count: 2.0,
        };
        solve_input.qos.max_average_error = 1.0;

        let (proposal, delta) = algorithm.solve(&state, &scope, &solve_input).unwrap();

        assert_eq!(proposal.placements[0].scheduled_slot, 2);
        assert_eq!(proposal.placements[0].flavour_name, "Accurate");
        assert!(!proposal.placements[0].late);
        assert_eq!(
            proposal.placements[0].origin,
            PlacementOrigin::InfeasibilityFallback
        );

        algorithm.merge(
            &mut state,
            delta,
            &CommitReport {
                assignments: vec![committed_assignment(1, 2, 8.0)],
            },
        );
        assert!(state.tau[2] > 1.6);
    }

    #[test]
    fn online_window_center_is_current_slot_when_arrival_is_older() {
        let algorithm = AcoAlgorithm::new(config());
        let scope = scope();
        let state = algorithm.initial_state(&scope);
        let mut solve_input = input();
        solve_input.current_slot = 1;
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 2;
        solve_input.requests[0].assignment_deadline_slot = 2;
        solve_input.qos.error_window.future_slots = 1;
        solve_input.qos.max_average_error = 0.0;
        solve_input.qos.err_by_slot.insert(
            2,
            ErrorTotals {
                sum: 1.0,
                count: 1.0,
            },
        );

        let (proposal, _) = algorithm.solve(&state, &scope, &solve_input).unwrap();

        assert_eq!(
            proposal.placements[0].origin,
            PlacementOrigin::InfeasibilityFallback
        );
    }

    #[test]
    fn merge_uses_final_committed_cost_and_evaporates_once_per_batch() {
        let algorithm = AcoAlgorithm::new(config());
        let mut state = algorithm.initial_state(&scope());
        let delta = AcoDelta {
            proposed_request_ids: HashSet::from([1, 2]),
        };

        algorithm.merge(
            &mut state,
            delta,
            &CommitReport {
                assignments: vec![
                    committed_assignment(1, 1, 2.0),
                    committed_assignment(2, 2, 3.0),
                    committed_assignment(999, 3, 100.0),
                ],
            },
        );

        // Q / L = 10 / (2 + 3) = 2. The four local search iterations do
        // not cause four persistent evaporations: one committed batch applies rho once.
        assert!((state.tau[0] - 1.6).abs() < 1e-12);
        assert!((state.tau[1] - 3.6).abs() < 1e-12);
        assert!((state.tau[2] - 3.6).abs() < 1e-12);
        assert!((state.tau[3] - 1.6).abs() < 1e-12);
    }

    #[test]
    fn merge_uses_only_the_committed_subset() {
        let algorithm = AcoAlgorithm::new(config());
        let mut state = algorithm.initial_state(&scope());
        let delta = AcoDelta {
            proposed_request_ids: HashSet::from([1, 2]),
        };

        algorithm.merge(
            &mut state,
            delta,
            &CommitReport {
                assignments: vec![committed_assignment(1, 1, 2.0)],
            },
        );

        // The accepted subset has L=2, so Q/L=5 is deposited only at slot 1.
        assert!((state.tau[0] - 1.6).abs() < 1e-12);
        assert!((state.tau[1] - 6.6).abs() < 1e-12);
        assert!((state.tau[2] - 1.6).abs() < 1e-12);
        assert!((state.tau[3] - 1.6).abs() < 1e-12);
    }

    #[test]
    fn consecutive_commits_apply_one_ordered_evaporation_each() {
        let algorithm = AcoAlgorithm::new(config());
        let mut state = algorithm.initial_state(&scope());

        algorithm.merge(
            &mut state,
            AcoDelta {
                proposed_request_ids: HashSet::from([1]),
            },
            &CommitReport {
                assignments: vec![committed_assignment(1, 1, 2.0)],
            },
        );
        algorithm.merge(
            &mut state,
            AcoDelta {
                proposed_request_ids: HashSet::from([2]),
            },
            &CommitReport {
                assignments: vec![committed_assignment(2, 2, 3.0)],
            },
        );

        assert!((state.tau[0] - 1.28).abs() < 1e-12);
        assert!((state.tau[1] - 5.28).abs() < 1e-12);
        assert!((state.tau[2] - (1.28 + 10.0 / 3.0)).abs() < 1e-12);
        assert!((state.tau[3] - 1.28).abs() < 1e-12);
    }

    #[test]
    fn rejected_proposal_does_not_change_persistent_pheromones() {
        let algorithm = AcoAlgorithm::new(config());
        let mut state = algorithm.initial_state(&scope());
        let before = state.tau.clone();
        let delta = AcoDelta {
            proposed_request_ids: HashSet::from([1]),
        };

        algorithm.merge(&mut state, delta, &CommitReport::default());

        assert_eq!(state.tau, before);
    }
}
