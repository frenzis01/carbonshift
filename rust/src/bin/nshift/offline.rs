//! Whole-scenario solver path for the nshift benchmark.

use super::*;

/// Separate seed salt keeps the hazard stream reproducible without sharing the
/// scenario generator's random sequence.
pub(super) const HAZARD_SEED_SALT: u64 = 0xA3F1_95C7_4D2B_6801;

/// Build the immutable default profile used by offline scenario requests.
pub(super) fn default_qos_profile(config: &Config) -> carbonshift_rs::engine::qos::QosProfile {
    carbonshift_rs::engine::qos::QosProfile {
        profile_id: QosProfileId::default_profile(),
        task_kind: carbonshift_rs::engine::qos::TaskKindId::parse("text_generation")
            .expect("built-in task kind is valid"),
        flavours: config.flavours.clone(),
        error_semantics: "relative-confidence-degradation-v1".to_owned(),
        max_error_threshold: config.max_error_threshold,
        error_window: carbonshift_rs::engine::qos::ErrorWindowPolicy {
            past_slots: config.error_window_past,
            future_slots: config.error_window_future,
            past_decay_slots: config.error_window_past_decay_slots,
        },
        cumulative_error: carbonshift_rs::engine::qos::CumulativeErrorPolicy {
            enabled: config.global_error_constraint_enabled,
            hard: config.global_error_constraint_hard,
        },
    }
}

/// Keep runner-specific search settings out of the strategy implementations.
/// The offline runner registers the common engine factories and uses the same
/// registry contract as other callers.
pub(super) fn build_solver_registry(
    config: &Config,
    hazard_seed: u64,
) -> Result<SolverRegistry, SolverError> {
    let dp_config = DpAlgorithmConfig {
        pruning_method: config.solver.dp_pruning_method.clone(),
        pruning_min_batch_size: config.solver.dp_pruning_min_batch_size,
        pruning_k: config.solver.dp_pruning_k,
        timeout_secs: config.solver.dp_timeout,
    };
    let mut registry = SolverRegistry::default();
    register_all_solvers(&mut registry, dp_config, &config.swarm, hazard_seed)?;
    Ok(registry)
}

/// Convert one offline scenario into the profile-scoped, runner-independent
/// contract that every common engine consumes.
pub(super) fn project_scenario_to_solve_input(
    scenario: &Scenario,
    assignment: &AssignmentPolicy<'_>,
) -> Result<(SolverScope, SolveInput), SolverError> {
    let total_slots = usize::try_from(assignment.total_slots).map_err(|_| {
        SolverError::Algorithm("planning horizon must be a positive slot count".to_owned())
    })?;
    if total_slots == 0 {
        return Err(SolverError::Algorithm(
            "planning horizon must contain at least one slot".to_owned(),
        ));
    }
    if scenario.carbon_forecast.len() != total_slots {
        return Err(SolverError::Algorithm(format!(
            "scenario forecast has {} slots but the assignment policy has {total_slots}",
            scenario.carbon_forecast.len()
        )));
    }

    let profile_id = QosProfileId::default_profile();
    let scope = SolverScope {
        profile_id: profile_id.clone(),
        total_slots,
    };

    // A stable order makes seeded algorithms independent of the JSON request
    // array's ordering and gives stateful algorithms a repeatable work sequence.
    let mut requests = scenario.requests.clone();
    requests.sort_by_key(|request| (request.arrival_slot, request.request_id));

    let forecast_max_slot = assignment.total_slots - 1;
    let requests = requests
        .iter()
        .map(|request| {
            let assignment_deadline_slot = carbonshift_rs::engine::solver::cap_assignment_deadline(
                request.request_id,
                request.arrival_slot,
                request.deadline_slot,
                0,
                assignment.error_window_future,
                forecast_max_slot,
                assignment.total_slots,
            )?;
            Ok(SolveRequest {
                request_id: request.request_id,
                arrival_slot: request.arrival_slot,
                deadline_slot: request.deadline_slot,
                assignment_deadline_slot,
            })
        })
        .collect::<Result<Vec<_>, SolverError>>()?;

    let input = SolveInput {
        current_slot: 0,
        requests,
        carbon_forecast: scenario.carbon_forecast.clone(),
        capacity_tiers: assignment.capacity_tiers.to_vec(),
        baseline_slot_counts: HashMap::new(),
        carbon_cost_duration_scale: assignment.carbon_cost_duration_scale,
        forecast_max_slot: Some(assignment.total_slots - 1),
        qos: QoSProfileInput {
            profile_id,
            flavours: assignment.flavours.to_vec(),
            cumulative_error_constraint_active: assignment.global_error_constraint_enabled
                && assignment.global_error_constraint_hard,
            cumulative_err: ErrorTotals::default(),
            err_by_slot: HashMap::new(),
            max_average_error: assignment.max_error_threshold,
            error_window: ErrorWindowInput {
                baseline_err: ErrorTotals::default(),
                past_slots: assignment.error_window_past,
                future_slots: assignment.error_window_future,
            },
        },
    };

    Ok((scope, input))
}

/// A final assignment plus the solver's late marker, which the domain's
/// `Assignment` intentionally does not store.
pub(super) struct OfflineEnginePlacement {
    pub(super) assignment: Assignment,
    pub(super) arrival_slot: i32,
    pub(super) deadline_slot: i32,
    pub(super) late: bool,
    pub(super) origin: PlacementOrigin,
}

/// Information validated once for each proposed placement and then used to
/// create the accepted, priced domain assignment.
struct ValidatedProposedPlacement<'request, 'flavour> {
    request: &'request ScenarioRequest,
    flavour: &'flavour Flavour,
    next_position: i64,
    carbon_cost: f64,
}

/// Check a single engine placement against its request, time bounds, profile,
/// forecast, and next capacity-tier position.
///
/// Keeping these checks together gives the materializer one clear boundary:
/// it either receives all the trusted data needed to build an `Assignment`,
/// or it gets a descriptive solver error and commits nothing.
fn validate_proposed_placement<'request, 'flavour>(
    placement: &carbonshift_rs::engine::solver::ProposedPlacement,
    solve_input: &SolveInput,
    scope: &SolverScope,
    requests_by_id: &HashMap<u64, &'request ScenarioRequest>,
    assignment: &'flavour AssignmentPolicy<'_>,
    slot_counts: &HashMap<i32, i64>,
    seen_request_ids: &mut HashSet<u64>,
) -> Result<ValidatedProposedPlacement<'request, 'flavour>, SolverError> {
    if !seen_request_ids.insert(placement.req_id) {
        return Err(SolverError::Algorithm(format!(
            "engine proposed request {} more than once",
            placement.req_id
        )));
    }

    let request = requests_by_id
        .get(&placement.req_id)
        .copied()
        .ok_or_else(|| {
            SolverError::Algorithm(format!(
                "engine proposed unknown request {}",
                placement.req_id
            ))
        })?;

    // `late` is explicit proposal metadata, not something inferred later from
    // the output row. Late placements must be at the current slot and after
    // their deadline; normal placements must satisfy every upper bound.
    if placement.late {
        if placement.scheduled_slot != solve_input.current_slot
            || placement.scheduled_slot <= request.deadline_slot
        {
            return Err(SolverError::Algorithm(format!(
                "engine marked request {} late at a non-late slot",
                placement.req_id
            )));
        }
    } else {
        let earliest = request.arrival_slot.max(solve_input.current_slot);
        let shift_limit = request
            .arrival_slot
            .checked_add(solve_input.qos.error_window.future_slots)
            .ok_or_else(|| {
                SolverError::Algorithm(format!(
                    "request {} QoS assignment horizon overflowed",
                    placement.req_id
                ))
            })?;
        let planning_horizon_end = i32::try_from(scope.total_slots)
            .ok()
            .and_then(|slot_count| slot_count.checked_sub(1))
            .ok_or_else(|| {
                SolverError::Algorithm("planning horizon must contain valid slots".to_owned())
            })?;
        let latest = request
            .deadline_slot
            .min(shift_limit)
            .min(
                solve_input
                    .forecast_max_slot
                    .unwrap_or(planning_horizon_end),
            )
            .min(planning_horizon_end);

        if placement.scheduled_slot < earliest || placement.scheduled_slot > latest {
            return Err(SolverError::Algorithm(format!(
                "engine placed request {} outside its feasible slot window",
                placement.req_id
            )));
        }
    }

    let flavour = assignment
        .flavours
        .iter()
        .find(|flavour| flavour.name == placement.flavour_name)
        .ok_or_else(|| {
            SolverError::Algorithm(format!(
                "engine selected unknown flavour '{}' for request {}",
                placement.flavour_name, placement.req_id
            ))
        })?;
    let slot_index = usize::try_from(placement.scheduled_slot).map_err(|_| {
        SolverError::Algorithm(format!(
            "engine selected invalid slot {} for request {}",
            placement.scheduled_slot, placement.req_id
        ))
    })?;
    let carbon_intensity = solve_input
        .carbon_forecast
        .get(slot_index)
        .copied()
        .ok_or_else(|| {
            SolverError::Algorithm(format!(
                "engine selected a slot outside the forecast for request {}",
                placement.req_id
            ))
        })?;

    let current_count = slot_counts
        .get(&placement.scheduled_slot)
        .copied()
        .unwrap_or(0);
    if current_count < 0 {
        return Err(SolverError::Algorithm(format!(
            "slot {} occupancy cannot be negative",
            placement.scheduled_slot
        )));
    }
    let next_position = current_count.checked_add(1).ok_or_else(|| {
        SolverError::Algorithm(format!(
            "slot {} occupancy overflowed i64",
            placement.scheduled_slot
        ))
    })?;
    let capacity_multiplier = get_capacity_multiplier(assignment.capacity_tiers, next_position);
    let duration_scale = assignment.carbon_cost_duration_scale;
    if !carbon_intensity.is_finite()
        || carbon_intensity < 0.0
        || !capacity_multiplier.is_finite()
        || capacity_multiplier < 0.0
        || !flavour.error.is_finite()
        || !flavour.duration.is_finite()
        || flavour.duration < 0.0
        || !duration_scale.is_finite()
        || duration_scale < 0.0
    {
        return Err(SolverError::Algorithm(format!(
            "request {} has invalid forecast, capacity, flavour, or cost-scale data",
            placement.req_id
        )));
    }
    let carbon_cost = carbon_intensity * capacity_multiplier * flavour.duration * duration_scale;
    if !carbon_cost.is_finite() {
        return Err(SolverError::Algorithm(format!(
            "request {} produced a non-finite carbon price",
            placement.req_id
        )));
    }

    Ok(ValidatedProposedPlacement {
        request,
        flavour,
        next_position,
        carbon_cost,
    })
}

/// Apply the offline runner's accept-and-price rule to a proposal.
///
/// Proposed placements deliberately have no carbon price. This adapter walks
/// them in proposal order, prices each at the next occupancy position for its
/// slot, then increments that slot's count. The resulting prices are exactly
/// what stateful engines receive in `CommitReport`, so their learned costs are
/// based on accepted, capacity-adjusted assignments rather than estimates.
pub(super) fn materialize_offline_proposal(
    proposal: &carbonshift_rs::engine::solver::SolverProposal,
    solve_input: &SolveInput,
    scope: &SolverScope,
    scenario: &Scenario,
    assignment: &AssignmentPolicy<'_>,
) -> Result<Vec<OfflineEnginePlacement>, SolverError> {
    let requests_by_id: HashMap<u64, _> = scenario
        .requests
        .iter()
        .map(|request| (request.request_id, request))
        .collect();
    if requests_by_id.len() != scenario.requests.len() {
        return Err(SolverError::Algorithm(
            "scenario contains duplicate request IDs".to_owned(),
        ));
    }

    let mut seen_request_ids = HashSet::with_capacity(proposal.placements.len());
    let mut slot_counts = solve_input.baseline_slot_counts.clone();
    let mut materialized = Vec::with_capacity(proposal.placements.len());

    for placement in &proposal.placements {
        let validated = validate_proposed_placement(
            placement,
            solve_input,
            scope,
            &requests_by_id,
            assignment,
            &slot_counts,
            &mut seen_request_ids,
        )?;
        *slot_counts.entry(placement.scheduled_slot).or_insert(0) = validated.next_position;
        materialized.push(OfflineEnginePlacement {
            assignment: Assignment::new_for_profile(
                placement.req_id,
                placement.scheduled_slot,
                placement.flavour_name.clone(),
                validated.carbon_cost,
                validated.flavour.error,
                validated.flavour.duration,
                Some(validated.request.arrival_slot),
                Some(validated.request.deadline_slot),
                solve_input.qos.profile_id.clone(),
            ),
            arrival_slot: validated.request.arrival_slot,
            deadline_slot: validated.request.deadline_slot,
            late: placement.late,
            origin: placement.origin,
        });
    }

    if seen_request_ids.len() != scenario.requests.len() {
        return Err(SolverError::Algorithm(format!(
            "engine proposed {} of {} scenario requests",
            seen_request_ids.len(),
            scenario.requests.len()
        )));
    }

    Ok(materialized)
}

/// Convert accepted common-engine assignments to the established `nshift`
/// per-request and summary output records.
pub(super) fn convert_engine_results_to_outputs(
    mut placements: Vec<OfflineEnginePlacement>,
    strategy_name: &str,
    scenario: &Scenario,
    assignment: &AssignmentPolicy<'_>,
    slot_duration_seconds: f64,
    realtime_slots: bool,
    realtime_speed_scale: f64,
    solver_elapsed_ms: f64,
) -> (Vec<PerRequest>, RunSummary) {
    placements.sort_by_key(|placement| (placement.arrival_slot, placement.assignment.request_id));

    let most_accurate = assignment
        .flavours
        .iter()
        .min_by(|left, right| left.error.total_cmp(&right.error))
        .expect("a valid engine input has at least one flavour");
    let mut per_request = Vec::with_capacity(placements.len());

    for placement in &placements {
        let assignment_result = &placement.assignment;
        let arrival_slot = placement.arrival_slot;
        let deadline_slot = placement.deadline_slot;
        let sequence = 1;
        let queue_wait_slots = 0;
        let final_wait_slots = (assignment_result.scheduled_slot - arrival_slot).max(0);
        let lateness_slots = if placement.late {
            assignment_result.scheduled_slot - deadline_slot
        } else {
            0
        };

        per_request.push(PerRequest {
            request_id: assignment_result.request_id,
            qos_profile_id: assignment_result.qos_profile_id.to_string(),
            arrival_time: arrival_slot as f64 * slot_duration_seconds,
            arrival_slot,
            deadline_slot,
            included_in_batch_slot: 0,
            batch_sequence: sequence,
            scheduled_slot: assignment_result.scheduled_slot,
            queue_wait_slots,
            queue_wait_seconds: queue_wait_slots as f64 * slot_duration_seconds,
            final_wait_slots,
            final_wait_seconds: final_wait_slots as f64 * slot_duration_seconds,
            flavour_name: assignment_result.flavour_name.clone(),
            error: assignment_result.error,
            carbon_cost: assignment_result.carbon_cost,
            assignment_solver_status: "ok".to_owned(),
            fallback_assigned: placement.origin == PlacementOrigin::GreedyFallback,
            late_assigned: placement.late,
            lateness_slots,
        });
    }
    let mut solver_timings = SolverTimingSummary::default();
    solver_timings.record(solver_elapsed_ms);

    let summary = compute_summary(
        strategy_name,
        0,
        realtime_slots,
        realtime_speed_scale,
        &per_request,
        solver_timings,
        scenario.requests.len(),
        &most_accurate.name,
        most_accurate.duration,
        most_accurate.error,
        0,
        0.0,
    );
    (per_request, summary)
}

/// Normalize offline labels at the runner boundary, then create and run the
/// requested shared engine.
pub(super) fn canonical_offline_engine_name(
    name: &str,
) -> Result<(&'static str, &'static str), SolverError> {
    match name.trim().to_ascii_lowercase().as_str() {
        "offline_singleton_best" => Ok(("singleton_best", "offline_singleton_best")),
        "singleton_best" => Ok(("singleton_best", "singleton_best")),
        "offline_singleton_best_accurate" => {
            Ok(("singleton_best_accurate", "offline_singleton_best_accurate"))
        }
        "singleton_best_accurate" => Ok(("singleton_best_accurate", "singleton_best_accurate")),
        "offline_random_slot" => Ok(("random_slot", "offline_random_slot")),
        "random_slot" => Ok(("random_slot", "random_slot")),
        "offline_arrival_slot" => Ok(("arrival_slot", "offline_arrival_slot")),
        "arrival_slot" => Ok(("arrival_slot", "arrival_slot")),
        "hazard" => Ok(("hazard", "hazard")),
        "dp" => Ok(("dp", "dp")),
        "bandit" => Ok(("bandit", "bandit")),
        "ant_colony" => Ok(("ant_colony", "ant_colony")),
        _ => Err(SolverError::UnknownEngine(name.to_owned())),
    }
}

/// Run one offline scenario through the common solver registry and lifecycle.
pub(super) fn run_strategy(
    registry: &SolverRegistry,
    name: &str,
    scenario: &Scenario,
    config: &Config,
    slot_duration_seconds: f64,
    realtime_slots: bool,
    realtime_speed_scale: f64,
) -> Result<(Vec<PerRequest>, RunSummary), SolverError> {
    let (engine_name, output_name) = canonical_offline_engine_name(name)?;
    let assignment = config.assignment_policy();
    let (scope, solve_input) = project_scenario_to_solve_input(scenario, &assignment)?;
    let runtime = registry.create(engine_name, scope.clone())?;
    let solve_started = std::time::Instant::now();
    let pending = runtime.propose(&solve_input)?;
    let solver_elapsed_ms = solve_started.elapsed().as_secs_f64() * 1000.0;

    // If conversion rejects a proposal, settle its pending state delta as
    // discarded before returning the explicit conversion error.
    let materialized = match materialize_offline_proposal(
        &pending.proposal,
        &solve_input,
        &scope,
        scenario,
        &assignment,
    ) {
        Ok(assignments) => assignments,
        Err(error) => {
            runtime.discard(pending.id)?;
            return Err(error);
        }
    };

    let commit_report = CommitReport {
        assignments: materialized
            .iter()
            .map(|placement| placement.assignment.clone())
            .collect(),
    };
    runtime.commit(pending.id, commit_report)?;

    Ok(convert_engine_results_to_outputs(
        materialized,
        output_name,
        scenario,
        &assignment,
        slot_duration_seconds,
        realtime_slots,
        realtime_speed_scale,
        solver_elapsed_ms,
    ))
}
