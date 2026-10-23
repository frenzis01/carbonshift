//! Stateless scheduling algorithms adapted to the common solver contract.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicU64, Ordering};

use rand::SeedableRng;
use rand::rngs::SmallRng;

use crate::engine::dp_solver::{DpSolver, ErrorWindowBaseline, MockPool, SolveBatchInput};
use crate::engine::random_selection::{
    RandomSelectionMode, closest_to_uniform_flavour_probabilities, select_random_assignment,
};
use crate::engine::solver::{
    CommitReport, ProposedPlacement, SolveInput, SolverAlgorithm, SolverError, SolverProposal,
    SolverRegistry, SolverScope,
};
use crate::types::{Flavour, get_capacity_multiplier};

/// DP-specific search settings, kept separate from the application config.
#[derive(Debug, Clone)]
pub struct DpAlgorithmConfig {
    /// DP pruning method (`"none"`, `"beam"`, or `"kbest"`).
    pub pruning_method: String,
    /// Minimum batch size at which the configured pruning method is enabled.
    pub pruning_min_batch_size: usize,
    /// Maximum retained DP states when pruning is enabled.
    pub pruning_k: usize,
    /// Search timeout in seconds.
    pub timeout_secs: f64,
}

/// Stateless adapter that runs one joint DP solve for the complete input batch.
pub struct DpAlgorithm {
    pub config: DpAlgorithmConfig,
}

impl DpAlgorithm {
    fn effective_pruning(&self, batch_size: usize) -> String {
        if self.config.pruning_min_batch_size == 0
            || batch_size >= self.config.pruning_min_batch_size
        {
            self.config.pruning_method.clone()
        } else {
            "none".to_owned()
        }
    }
}

fn validate_solve_input(input: &SolveInput) -> Result<(), SolverError> {
    let horizon = usize::try_from(input.total_slots)
        .map_err(|_| SolverError::Algorithm("planning horizon must be positive".to_owned()))?;
    if horizon == 0 {
        return Err(SolverError::Algorithm(
            "planning horizon must be positive".to_owned(),
        ));
    }
    if input.carbon_forecast.len() != horizon {
        return Err(SolverError::Algorithm(format!(
            "forecast length {} does not match planning horizon {horizon}",
            input.carbon_forecast.len()
        )));
    }
    if !input.error_window.max_average_error.is_finite() {
        return Err(SolverError::Algorithm(
            "sliding-window error threshold must be finite".to_owned(),
        ));
    }
    if !input.error_window.baseline_error_sum.is_finite()
        || !input.error_window.baseline_request_count.is_finite()
        || input.error_window.baseline_request_count < 0.0
    {
        return Err(SolverError::Algorithm(
            "error-window baseline must have a finite sum and non-negative finite count".to_owned(),
        ));
    }
    if input.baseline_slot_counts.values().any(|count| *count < 0) {
        return Err(SolverError::Algorithm(
            "baseline slot counts cannot be negative".to_owned(),
        ));
    }

    for request in &input.requests {
        let flavours = flavours_for_request(input, request.request_id)?;
        if flavours.iter().any(|flavour| {
            !flavour.error.is_finite() || !flavour.duration.is_finite() || flavour.duration < 0.0
        }) {
            return Err(SolverError::Algorithm(format!(
                "request {} has a flavour with invalid error or duration",
                request.request_id
            )));
        }
    }
    Ok(())
}

fn request_slot_bounds(input: &SolveInput, arrival: i32, deadline: i32) -> (i32, i32) {
    (
        arrival.max(input.current_slot),
        deadline
            .min(input.assignment_max_slot.unwrap_or(input.total_slots - 1))
            .min(input.total_slots - 1),
    )
}

fn flavours_for_request(input: &SolveInput, request_id: u64) -> Result<&[Flavour], SolverError> {
    let flavours = input
        .request_flavours
        .get(&request_id)
        .map(Vec::as_slice)
        .unwrap_or(&input.default_flavours);
    if flavours.is_empty() {
        return Err(SolverError::Algorithm(format!(
            "request {request_id} has no available flavours"
        )));
    }
    Ok(flavours)
}

impl SolverAlgorithm for DpAlgorithm {
    type State = ();
    type Delta = ();

    fn initial_state(&self, _scope: &SolverScope) {}

    fn solve(
        &self,
        _snapshot: &(),
        input: &SolveInput,
    ) -> Result<(SolverProposal, ()), SolverError> {
        if input.requests.is_empty() {
            return Ok((SolverProposal::default(), ()));
        }
        validate_solve_input(input)?;
        for request in &input.requests {
            let (earliest, deadline) =
                request_slot_bounds(input, request.arrival_slot, request.deadline_slot);
            if earliest > deadline {
                return Err(SolverError::Algorithm(format!(
                    "request {} has no feasible slot within the planning horizon",
                    request.request_id
                )));
            }
        }

        let solver = DpSolver {
            flavours: input.default_flavours.clone(),
            carbon_forecast: input.carbon_forecast.clone(),
            window_size: input.total_slots,
            pruning: self.effective_pruning(input.requests.len()),
            pruning_k: self.config.pruning_k,
            timeout: self.config.timeout_secs,
            carbon_cost_scale: input.carbon_cost_duration_scale,
        };
        // Keep the legacy DP kernel input here: it is also used by the existing
        // scheduler path, and its mock-pool field is DP/recovery-specific.
        let mut assignments = solver.solve_batch(SolveBatchInput {
            requests: &input.requests,
            current_slot: input.current_slot,
            capacity_multiplier: 1.0,
            capacity_tiers: &input.capacity_tiers,
            baseline_slot_counts: &input.baseline_slot_counts,
            error_window_baseline: ErrorWindowBaseline {
                error_sum: input.error_window.baseline_error_sum,
                request_count: input.error_window.baseline_request_count,
            },
            max_error_threshold: Some(input.error_window.max_average_error),
            error_window_past: input.error_window.past_slots,
            error_window_future: input.error_window.future_slots,
            assignment_max_slot: input.assignment_max_slot,
            dynamic_mock_pool: MockPool::default(),
            request_flavours: &input.request_flavours,
        });

        let scheduled_ids: HashSet<u64> = assignments
            .iter()
            .map(|assignment| assignment.request_id)
            .collect();
        let unscheduled: Vec<_> = input
            .requests
            .iter()
            .filter(|request| !scheduled_ids.contains(&request.request_id))
            .copied()
            .collect();

        if !unscheduled.is_empty() {
            let mut fallback_counts: Vec<i32> = (0..input.total_slots)
                .map(|slot| input.baseline_slot_counts.get(&slot).copied().unwrap_or(0))
                .collect();
            for assignment in &assignments {
                let index = usize::try_from(assignment.slot).map_err(|_| {
                    SolverError::Algorithm(format!(
                        "DP returned invalid negative slot {}",
                        assignment.slot
                    ))
                })?;
                let count = fallback_counts.get_mut(index).ok_or_else(|| {
                    SolverError::Algorithm(format!(
                        "DP returned slot {} outside the planning horizon",
                        assignment.slot
                    ))
                })?;
                *count += 1;
            }

            assignments.extend(solver.greedy_fallback(
                &unscheduled,
                input.current_slot,
                &input.capacity_tiers,
                &fallback_counts,
                &input.request_flavours,
            ));
        }

        let proposal = SolverProposal {
            placements: assignments
                .into_iter()
                .map(|assignment| ProposedPlacement {
                    req_id: assignment.request_id,
                    scheduled_slot: assignment.slot,
                    flavour_name: assignment.flavour_name,
                })
                .collect(),
        };

        Ok((proposal, ()))
    }

    fn merge(&self, _state: &mut (), _delta: (), _committed: &CommitReport) {}
}

/// Exhaustive per-request strategy that minimizes carbon cost within the QoS policy.
pub struct SingletonBestAlgorithm;

impl SolverAlgorithm for SingletonBestAlgorithm {
    type State = ();
    type Delta = ();

    fn initial_state(&self, _scope: &SolverScope) {}

    fn solve(
        &self,
        _snapshot: &(),
        input: &SolveInput,
    ) -> Result<(SolverProposal, ()), SolverError> {
        if input.requests.is_empty() {
            return Ok((SolverProposal::default(), ()));
        }
        validate_solve_input(input)?;

        let mut placements = Vec::with_capacity(input.requests.len());
        let mut slot_count: HashMap<i32, i64> = input
            .baseline_slot_counts
            .iter()
            .map(|(&slot, &count)| (slot, i64::from(count)))
            .collect();
        let mut solved_errors = Vec::with_capacity(input.requests.len());

        for request in &input.requests {
            let flavours = flavours_for_request(input, request.request_id)?;
            let (start_slot, deadline) =
                request_slot_bounds(input, request.arrival_slot, request.deadline_slot);
            if start_slot > deadline {
                return Err(SolverError::Algorithm(format!(
                    "request {} has no feasible slot: earliest {start_slot}, deadline {deadline}",
                    request.request_id
                )));
            }

            let mut sorted_flavours: Vec<&Flavour> = flavours.iter().collect();
            sorted_flavours.sort_by(|left, right| left.duration.total_cmp(&right.duration));

            let mut best: Option<(f64, i32, &Flavour)> = None;

            for slot in start_slot..=deadline {
                // The current slot's observed value is already folded into the
                // effective forecast before the runner creates SolveInput.
                let slot_index = usize::try_from(slot)
                    .map_err(|_| SolverError::Algorithm(format!("invalid negative slot {slot}")))?;
                let carbon_intensity = *input.carbon_forecast.get(slot_index).ok_or_else(|| {
                    SolverError::Algorithm(format!("slot {slot} is outside the carbon forecast"))
                })?;
                let position = next_slot_position(&slot_count, slot)?;
                let multiplier = get_capacity_multiplier(&input.capacity_tiers, position);

                for flav in &sorted_flavours {
                    if input.cumulative_error_constraint_active
                        && flav.error > input.error_window.max_average_error
                    {
                        continue;
                    }

                    // Match the existing policy: the local window is anchored
                    // to this request's arrival slot.
                    let win_start = request
                        .arrival_slot
                        .saturating_sub(input.error_window.past_slots)
                        .max(0);
                    let win_end = request
                        .arrival_slot
                        .saturating_add(input.error_window.future_slots)
                        .min(input.total_slots - 1);
                    let mut win_sum = input.error_window.baseline_error_sum + flav.error;
                    let mut win_count = input.error_window.baseline_request_count + 1.0;
                    for &(placed_slot, placed_error) in &solved_errors {
                        if (win_start..=win_end).contains(&placed_slot) {
                            win_sum += placed_error;
                            win_count += 1.0;
                        }
                    }

                    // Reject this candidate if it would violate the window
                    // budget; another flavour or slot may still be feasible.
                    if win_count > 0.0 && win_sum / win_count > input.error_window.max_average_error
                    {
                        continue;
                    }

                    let cost = compute_cost(
                        carbon_intensity,
                        multiplier,
                        flav.duration,
                        input.carbon_cost_duration_scale,
                    );
                    if best.map(|(c, _, _)| cost < c).unwrap_or(true) {
                        best = Some((cost, slot, flav));
                    }
                }
            }

            let (chosen_slot, chosen_flavour) = if let Some((_, slot, flavour)) = best {
                (slot, flavour)
            } else {
                // Preserve the existing best-effort fallback: use the most
                // accurate flavour at the earliest legal slot. This fallback
                // can exceed the error threshold when no feasible candidate
                // exists; it does not change the threshold used above.
                (start_slot, get_fallback_flav(flavours)?)
            };

            increment_slot_count(&mut slot_count, chosen_slot)?;
            solved_errors.push((chosen_slot, chosen_flavour.error));
            placements.push(ProposedPlacement {
                req_id: request.request_id,
                scheduled_slot: chosen_slot,
                flavour_name: chosen_flavour.name.clone(),
            });
        }
        Ok((SolverProposal { placements }, ()))
    }

    fn merge(&self, _state: &mut (), _delta: (), _committed: &CommitReport) {}
}

// BIZ_RULE: computing cost, applying carbon_cost_duration_scale
fn compute_cost(ci: f64, mult: f64, dur: f64, scale: f64) -> f64 {
    ci * mult * dur * scale
}

fn get_fallback_flav(flavours: &[Flavour]) -> Result<&Flavour, SolverError> {
    flavours
        .iter()
        .min_by(|left, right| left.error.total_cmp(&right.error))
        .ok_or_else(|| SolverError::Algorithm("no fallback flavour is available".to_owned()))
}

fn next_slot_position(slot_count: &HashMap<i32, i64>, slot: i32) -> Result<i64, SolverError> {
    slot_count
        .get(&slot)
        .copied()
        .unwrap_or(0)
        .checked_add(1)
        .ok_or_else(|| SolverError::Algorithm(format!("slot {slot} occupancy overflowed i64")))
}

fn increment_slot_count(slot_count: &mut HashMap<i32, i64>, slot: i32) -> Result<(), SolverError> {
    let count = slot_count.entry(slot).or_insert(0);
    *count = count
        .checked_add(1)
        .ok_or_else(|| SolverError::Algorithm(format!("slot {slot} occupancy overflowed i64")))?;
    Ok(())
}

/// Random slot/flavour strategy with independent streams for concurrent solves.
pub struct RandomAlgorithm {
    mode: RandomSelectionMode,
    seed: u64,
    next_stream: AtomicU64,
}

impl RandomAlgorithm {
    /// Create a random strategy with a reproducible base seed.
    pub fn new(mode: RandomSelectionMode, seed: u64) -> Self {
        Self {
            mode,
            seed,
            next_stream: AtomicU64::new(0),
        }
    }
}

impl SolverAlgorithm for RandomAlgorithm {
    type State = ();
    type Delta = ();

    fn initial_state(&self, _scope: &SolverScope) {}

    fn solve(
        &self,
        _snapshot: &(),
        input: &SolveInput,
    ) -> Result<(SolverProposal, ()), SolverError> {
        if input.requests.is_empty() {
            return Ok((SolverProposal::default(), ()));
        }
        validate_solve_input(input)?;

        // Each solve gets a separate deterministic stream, so concurrent
        // batches do not need to hold a shared RNG lock while selecting.
        let stream = self
            .next_stream
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
                value.checked_add(1)
            })
            .map_err(|_| {
                SolverError::Algorithm("random solver stream counter exhausted".to_owned())
            })?;
        let mut rng = SmallRng::seed_from_u64(self.seed ^ stream);
        let mut placements = Vec::with_capacity(input.requests.len());

        for request in &input.requests {
            let available_flavours = flavours_for_request(input, request.request_id)?;
            let filtered_flavours: Vec<Flavour> = if input.cumulative_error_constraint_active {
                available_flavours
                    .iter()
                    .filter(|flavour| flavour.error <= input.error_window.max_average_error)
                    .cloned()
                    .collect()
            } else {
                Vec::new()
            };
            let flavours = if input.cumulative_error_constraint_active {
                filtered_flavours.as_slice()
            } else {
                available_flavours
            };
            if flavours.is_empty() {
                return Err(SolverError::Algorithm(format!(
                    "request {} has no flavour within the active cumulative error limit",
                    request.request_id
                )));
            }
            let (earliest, deadline) =
                request_slot_bounds(input, request.arrival_slot, request.deadline_slot);
            let latest = deadline.min(
                request
                    .arrival_slot
                    .saturating_add(input.assignment_max_future_slots),
            );
            if earliest > latest {
                return Err(SolverError::Algorithm(format!(
                    "request {} has no feasible random-selection slot",
                    request.request_id
                )));
            }

            let probabilities = if self.mode == RandomSelectionMode::Slot {
                vec![1.0 / flavours.len() as f64; flavours.len()]
            } else {
                let errors: Vec<f64> = flavours.iter().map(|flavour| flavour.error).collect();
                closest_to_uniform_flavour_probabilities(
                    &errors,
                    input.error_window.max_average_error,
                )
                .map_err(SolverError::Algorithm)?
            };
            let (slot, flavour_index) = select_random_assignment(
                self.mode,
                earliest,
                latest,
                flavours,
                &probabilities,
                &mut rng,
            )
            .map_err(SolverError::Algorithm)?;

            placements.push(ProposedPlacement {
                req_id: request.request_id,
                scheduled_slot: slot,
                flavour_name: flavours[flavour_index].name.clone(),
            });
        }

        Ok((SolverProposal { placements }, ()))
    }

    fn merge(&self, _state: &mut (), _delta: (), _committed: &CommitReport) {}
}

/// Register the stateless strategies without adding solver-specific branches
/// to the runner or the common runtime.
///
/// Each random mode gets a distinct deterministic seed stream; callers can
/// choose a different base seed for each run.
pub fn register_stateless_solvers(
    registry: &mut SolverRegistry,
    dp_config: DpAlgorithmConfig,
    random_seed: u64,
) -> Result<(), SolverError> {
    registry.register("dp", move || DpAlgorithm {
        config: dp_config.clone(),
    })?;
    registry.register("singleton_best", || SingletonBestAlgorithm)?;

    registry.register("random_flavour", move || {
        RandomAlgorithm::new(RandomSelectionMode::Flavour, random_seed)
    })?;
    registry.register("random_slot", move || {
        RandomAlgorithm::new(RandomSelectionMode::Slot, random_seed.wrapping_add(1))
    })?;
    registry.register("random_full", move || {
        RandomAlgorithm::new(RandomSelectionMode::Full, random_seed.wrapping_add(2))
    })?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use std::collections::{HashMap, HashSet};

    use crate::engine::qos::QosProfileId;
    use crate::engine::solver::{
        ErrorWindowInput, PendingProposal, SolveInput, SolveRequest, SolverRegistry, SolverRuntime,
        SolverScope,
    };
    use crate::types::{CapacityTier, Flavour};

    use super::{DpAlgorithmConfig, register_stateless_solvers};

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
            total_slots: 4,
            requests: vec![
                SolveRequest {
                    request_id: 10,
                    arrival_slot: 0,
                    deadline_slot: 3,
                },
                SolveRequest {
                    request_id: 11,
                    arrival_slot: 1,
                    deadline_slot: 3,
                },
            ],
            default_flavours: vec![Flavour {
                name: "Default".to_owned(),
                error: 0.0,
                duration: 10.0,
            }],
            request_flavours: HashMap::new(),
            carbon_forecast: vec![20.0, 10.0, 5.0, 1.0],
            capacity_tiers: vec![CapacityTier {
                max_requests: None,
                multiplier: 1.0,
            }],
            baseline_slot_counts: HashMap::new(),
            carbon_cost_duration_scale: 1.0,
            assignment_max_future_slots: 2,
            assignment_max_slot: Some(3),
            cumulative_error_constraint_active: false,
            error_window: ErrorWindowInput {
                baseline_error_sum: 0.0,
                baseline_request_count: 0.0,
                max_average_error: 4.0,
                past_slots: 0,
                future_slots: 0,
            },
        }
    }

    fn runtime_for(name: &str) -> Box<dyn SolverRuntime> {
        let mut registry = SolverRegistry::default();
        register_stateless_solvers(&mut registry, config(), 42).unwrap();
        registry.create(name, scope()).unwrap()
    }

    fn scope() -> SolverScope {
        SolverScope {
            profile_id: QosProfileId::default_profile(),
        }
    }

    fn runtime() -> Box<dyn SolverRuntime> {
        runtime_for("dp")
    }

    fn propose(runtime: &dyn SolverRuntime, input: &SolveInput) -> PendingProposal {
        runtime.propose(input).unwrap()
    }

    fn placements_for(name: &str, solve_input: &SolveInput) -> Vec<super::ProposedPlacement> {
        let runtime = runtime_for(name);
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
    fn dp_uses_request_specific_flavours() {
        let runtime = runtime();
        let mut solve_input = input();
        solve_input.request_flavours.insert(
            10,
            vec![Flavour {
                name: "TaskSpecific".to_owned(),
                error: 0.0,
                duration: 1.0,
            }],
        );

        let pending = propose(&*runtime, &solve_input);
        let placement = pending
            .proposal
            .placements
            .iter()
            .find(|placement| placement.req_id == 10)
            .unwrap();

        assert_eq!(placement.flavour_name, "TaskSpecific");
        runtime.discard(pending.id).unwrap();
    }

    #[test]
    fn singleton_best_uses_request_specific_flavours() {
        let mut solve_input = input();
        solve_input.requests.truncate(1);
        solve_input.request_flavours.insert(
            10,
            vec![Flavour {
                name: "TaskSpecific".to_owned(),
                error: 0.0,
                duration: 1.0,
            }],
        );

        let placements = placements_for("singleton_best", &solve_input);

        assert_eq!(placements.len(), 1);
        assert_eq!(placements[0].flavour_name, "TaskSpecific");
    }

    #[test]
    fn singleton_best_updates_slot_occupancy_after_each_request() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.total_slots = 2;
        solve_input.requests = vec![
            SolveRequest {
                request_id: 1,
                arrival_slot: 0,
                deadline_slot: 1,
            },
            SolveRequest {
                request_id: 2,
                arrival_slot: 0,
                deadline_slot: 1,
            },
        ];
        solve_input.default_flavours = vec![Flavour {
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
        solve_input.assignment_max_future_slots = 1;
        solve_input.assignment_max_slot = Some(1);

        let placements = placements_for("singleton_best", &solve_input);

        assert_eq!(placements[0].scheduled_slot, 0);
        assert_eq!(placements[1].scheduled_slot, 1);
    }

    #[test]
    fn singleton_best_uses_i64_for_capacity_positions() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.total_slots = 2;
        solve_input.requests.truncate(1);
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 1;
        solve_input.default_flavours = vec![Flavour {
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
        solve_input.baseline_slot_counts.insert(0, i32::MAX);
        solve_input.assignment_max_future_slots = 1;
        solve_input.assignment_max_slot = Some(1);

        let placements = placements_for("singleton_best", &solve_input);

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
            },
            SolveRequest {
                request_id: 2,
                arrival_slot: 0,
                deadline_slot: 1,
            },
        ];
        solve_input.default_flavours = vec![
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
        solve_input.error_window.max_average_error = 2.0;
        solve_input.error_window.future_slots = 1;

        let placements = placements_for("singleton_best", &solve_input);

        assert_eq!(placements[0].flavour_name, "Accurate");
        assert_eq!(placements[1].flavour_name, "Fast");
    }

    #[test]
    fn singleton_best_reports_infeasible_requests_instead_of_dropping_them() {
        let mut solve_input = input();
        solve_input.current_slot = 2;
        solve_input.requests.truncate(1);
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 1;

        let runtime = runtime_for("singleton_best");

        assert!(runtime.propose(&solve_input).is_err());
    }

    #[test]
    fn singleton_best_falls_back_to_the_lowest_error_flavour() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.requests.truncate(1);
        solve_input.default_flavours = vec![
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
        solve_input.error_window.max_average_error = 1.0;

        let placements = placements_for("singleton_best", &solve_input);

        assert_eq!(placements[0].flavour_name, "Accurate");
        assert_eq!(placements[0].scheduled_slot, 0);
    }

    #[test]
    fn registered_random_modes_produce_valid_placements_and_use_overrides() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.default_flavours = vec![
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
        solve_input.request_flavours.insert(
            10,
            vec![Flavour {
                name: "TaskSpecific".to_owned(),
                error: 4.0,
                duration: 2.0,
            }],
        );

        for name in ["random_flavour", "random_slot", "random_full"] {
            let placements = placements_for(name, &solve_input);
            assert_eq!(placements.len(), solve_input.requests.len());
            assert_eq!(placements[0].flavour_name, "TaskSpecific");
            for placement in placements {
                let request = solve_input
                    .requests
                    .iter()
                    .find(|request| request.request_id == placement.req_id)
                    .unwrap();
                let latest = request
                    .deadline_slot
                    .min(solve_input.assignment_max_slot.unwrap())
                    .min(request.arrival_slot + solve_input.assignment_max_future_slots);
                assert!(
                    (request.arrival_slot.max(solve_input.current_slot)..=latest)
                        .contains(&placement.scheduled_slot)
                );
            }
        }
    }

    #[test]
    fn random_full_accepts_a_request_with_only_one_legal_slot() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.total_slots = 1;
        solve_input.requests.truncate(1);
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 0;
        solve_input.default_flavours = vec![
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
        solve_input.assignment_max_future_slots = 0;
        solve_input.assignment_max_slot = Some(0);

        let placements = placements_for("random_full", &solve_input);

        assert_eq!(placements.len(), 1);
        assert_eq!(placements[0].scheduled_slot, 0);
    }

    #[test]
    fn random_flavour_returns_probability_errors_instead_of_panicking() {
        let mut solve_input = input();
        solve_input.requests.truncate(1);
        let runtime = runtime_for("random_flavour");

        assert!(runtime.propose(&solve_input).is_err());
    }
}
