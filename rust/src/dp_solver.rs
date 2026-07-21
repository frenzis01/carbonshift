/// Rolling-window DP solver for the CarbonShift batch scheduler.
///
/// Mirrors `rolling_window_dp.py::RollingWindowDPScheduler`.
///
/// # Algorithm overview
/// One DP layer is processed per request in the batch.  Each layer expands
/// every live `DpState` by trying every feasible `(flavour, slot)` pair.
/// States with the same key are merged by keeping the minimum-cost path
/// (optimal sub-structure).  After each layer the state space is pruned to
/// at most `pruning_k` states (beam or kbest strategy).
///
/// # Warm-start variant
/// Before the main DP begins, a sequential (request-by-request) solution is
/// built.  This solution is used as a safety net:
/// • its DP state survives pruning even when it would normally be discarded;
/// • it is returned on timeout or infeasibility instead of a generic greedy
///   fallback.
///
/// # Error representation
/// Window error is tracked in *basis points* (integer, ×100) to avoid
/// floating-point accumulation drift in the per-state totals.
///
/// # Carbon cost
/// `cost [gCO₂] = carbon_intensity [gCO₂/kWh] × duration_seconds × scale`
/// where `scale = 1/3600`.  The scale factor comes from `Config`.

use std::collections::HashMap;
use std::time::Instant;

use crate::config::Config;
use crate::types::{get_capacity_multiplier, CapacityTier, Flavour, RequestAssignment};

// ─── DP state key ─────────────────────────────────────────────────────────────

/// Per-state key for the DP hash map.
///
/// All fields together uniquely identify the "progress" made so far:
/// - `error_sum_bp`     total error in window, multiplied by 100 (integer arith.)
/// - `error_count`      number of requests counted in that sum (×1000 for int)
/// - `mock_remaining`   synthetic baseline requests not yet consumed
/// - `inc_counts`       per-slot request count delta introduced by this batch
///
/// `error_count` is stored as `(f64 * 1000) as i64` to avoid f64 in HashMap
/// keys (f64 doesn't implement `Eq + Hash`).
///
/// `inc_durations` was removed: the new per-request tier model charges each
/// request `carbon × mult(position) × duration` directly, so slot duration
/// totals no longer affect the marginal cost formula.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct DpStateKey {
    error_sum_bp: i64,
    error_count_milli: i64, // error_count × 1000, rounded
    mock_remaining: i32,
    inc_counts: Vec<i32>,
}

impl DpStateKey {
    fn new(
        error_sum_bp: i64,
        error_count: f64,
        mock_remaining: i32,
        inc_counts: Vec<i32>,
    ) -> Self {
        Self {
            error_sum_bp,
            error_count_milli: (error_count * 1000.0).round() as i64,
            mock_remaining,
            inc_counts,
        }
    }

    /// Reconstruct error_count as f64.
    fn error_count(&self) -> f64 {
        self.error_count_milli as f64 / 1000.0
    }
}

// ─── solver ───────────────────────────────────────────────────────────────────

/// Configurable input for a single batch solve call.
pub struct SolveBatchInput<'a> {
    /// Requests to schedule: (id, deadline_slot).
    pub requests: &'a [(u64, i32)],
    pub current_slot: i32,
    /// Capacity multiplier fallback (used when capacity_tiers is empty).
    pub capacity_multiplier: f64,
    /// Capacity tiers for slot repricing.
    pub capacity_tiers: &'a [CapacityTier],
    /// Per-slot baseline request counts from already-fixed assignments.
    pub baseline_slot_counts: &'a HashMap<i32, i32>,
    /// Pre-computed error baseline for the window.
    pub error_window_baseline: ErrorWindowBaseline,
    /// Maximum allowed average window error (None = no constraint).
    pub max_error_threshold: Option<f64>,
    pub error_window_past: i32,
    pub error_window_future: i32,
    /// Hard upper bound on scheduled slot (None = window_size − 1).
    pub assignment_max_slot: Option<i32>,
    /// Optional synthetic mock pool for infeasibility recovery.
    pub dynamic_mock_pool: MockPool,
}

#[derive(Debug, Clone, Default)]
pub struct ErrorWindowBaseline {
    pub error_sum: f64,
    pub request_count: f64,
}

#[derive(Debug, Clone, Default)]
pub struct MockPool {
    pub initial_count: i32,
    pub error_per_request: f64,
}

pub struct DpSolver {
    pub flavours: Vec<Flavour>,
    pub carbon_forecast: Vec<f64>,
    pub window_size: i32,
    /// "beam" | "kbest" | "none"
    pub pruning: String,
    pub pruning_k: usize,
    pub timeout: f64,
    pub carbon_cost_scale: f64,
}

impl DpSolver {
    pub fn new(cfg: &Config) -> Self {
        Self {
            flavours: cfg.flavours.clone(),
            carbon_forecast: vec![0.0; cfg.total_slots as usize],
            window_size: cfg.total_slots,
            pruning: cfg.dp_pruning_method.clone(),
            pruning_k: cfg.dp_pruning_k,
            timeout: cfg.dp_timeout,
            carbon_cost_scale: cfg.carbon_cost_duration_scale,
        }
    }

    pub fn with_carbon_forecast(mut self, forecast: Vec<f64>) -> Self {
        assert_eq!(
            forecast.len(),
            self.window_size as usize,
            "carbon_forecast length must equal window_size"
        );
        self.carbon_forecast = forecast;
        self
    }

    // ── public API ────────────────────────────────────────────────────────

    /// Solve a batch of requests with DP.
    ///
    /// Returns an empty `Vec` only when *both* the full DP and the warm-start
    /// sequential solution are provably infeasible.
    pub fn solve_batch(&self, input: SolveBatchInput<'_>) -> Vec<RequestAssignment> {
        if input.requests.is_empty() {
            return vec![];
        }
        if input.current_slot >= self.window_size {
            return vec![];
        }

        let t = self.window_size as usize;

        // Resolve capacity tiers: fall back to flat multiplier if empty.
        let tiers: Vec<CapacityTier> = if input.capacity_tiers.is_empty() {
            vec![CapacityTier { max_requests: None, multiplier: input.capacity_multiplier }]
        } else {
            input.capacity_tiers.to_vec()
        };

        let window_start = (input.current_slot - input.error_window_past).max(0);
        let window_end = (input.current_slot + input.error_window_future).min(self.window_size - 1);
        let assignment_cap = input
            .assignment_max_slot
            .unwrap_or(self.window_size - 1)
            .min(self.window_size - 1)
            .max(input.current_slot);

        // Clamp every deadline into [current_slot, assignment_cap].
        let deadlines: Vec<i32> = input
            .requests
            .iter()
            .map(|(_, d)| (*d).max(input.current_slot).min(assignment_cap).min(self.window_size - 1))
            .collect();

        // Build baseline arrays.
        let mut base_counts = vec![0i32; t];
        for (&slot, &cnt) in input.baseline_slot_counts {
            if slot >= 0 && (slot as usize) < t {
                base_counts[slot as usize] = cnt;
            }
        }

        // Convert baseline to integer representation.
        let initial_error_sum_bp =
            (input.error_window_baseline.error_sum * 100.0).round() as i64;
        let initial_error_count = input.error_window_baseline.request_count;
        let initial_mock_count = input.dynamic_mock_pool.initial_count.max(0);
        let mock_error_bp =
            (input.dynamic_mock_pool.error_per_request * 100.0).round() as i64;

        // ── warm start: sequential (request-by-request) solution ─────────
        // This is equivalent to calling solve_batch once per request and
        // updating baseline_slot_counts each time.  It is fast and gives a
        // good upper bound on cost.
        let (warm_assignments, warm_keys) = self.compute_sequential_solution(
            &input,
            &deadlines,
            &base_counts,
            &tiers,
            initial_error_sum_bp,
            initial_error_count,
            initial_mock_count,
            mock_error_bp,
            window_start,
            window_end,
        );

        // With a single request the sequential solution is already exhaustive:
        // it evaluates every (flavour, slot) pair, which is exactly what the
        // one-layer DP would do.  No pruning can occur, and no state merging
        // can hide a better path.  We can return immediately.
        if input.requests.len() == 1 {
            // Honour the error threshold exactly as the full DP would.
            if let Some(threshold) = input.max_error_threshold {
                let ec = warm_keys[1].error_count();
                if ec > 0.0 && (warm_keys[1].error_sum_bp as f64 / 100.0) / ec > threshold {
                    return vec![];
                }
            }
            return warm_assignments;
        }

        // ── initial DP state ─────────────────────────────────────────────
        let init_key = DpStateKey::new(
            initial_error_sum_bp,
            initial_error_count,
            initial_mock_count,
            vec![0i32; t],
        );
        let mut dp_prev: HashMap<DpStateKey, (f64, Vec<RequestAssignment>)> = HashMap::new();
        dp_prev.insert(init_key, (0.0, vec![]));

        let start = Instant::now();

        // ── DP expansion: one layer per request ──────────────────────────
        for (req_idx, (req_id, _)) in input.requests.iter().enumerate() {
            let deadline = deadlines[req_idx];
            let mut dp_curr: HashMap<DpStateKey, (f64, Vec<RequestAssignment>)> = HashMap::new();

            for (state_key, (prev_cost, prev_assignments)) in &dp_prev {
                let inc_counts = state_key.inc_counts.clone();

                for flavour in &self.flavours {
                    let f_error_bp = (flavour.error * 100.0).round() as i64;
                    let f_duration = flavour.duration;

                    for slot in input.current_slot..=deadline {
                        let s = slot as usize;

                        let delta_cost = self.incremental_carbon_cost(
                            slot,
                            f_duration,
                            &base_counts,
                            &inc_counts,
                            &tiers,
                        );

                        // Update error accumulators.
                        let mut new_error_sum_bp = state_key.error_sum_bp;
                        let mut new_error_count = state_key.error_count();
                        let mut new_mock_remaining = state_key.mock_remaining;

                        if slot >= window_start && slot <= window_end {
                            new_error_sum_bp += f_error_bp;
                            new_error_count += 1.0;

                            // Optionally consume one synthetic mock request.
                            if new_mock_remaining > 0 && mock_error_bp > 0 {
                                new_error_sum_bp -= mock_error_bp;
                                new_error_count = (new_error_count - 1.0).max(0.0);
                                new_mock_remaining -= 1;
                            }
                        }

                        let mut new_inc_counts = inc_counts.clone();
                        new_inc_counts[s] += 1;

                        let new_cost = prev_cost + delta_cost;
                        let new_key = DpStateKey::new(
                            new_error_sum_bp,
                            new_error_count,
                            new_mock_remaining,
                            new_inc_counts,
                        );

                        let assignment = RequestAssignment {
                            request_id: *req_id,
                            flavour_name: flavour.name.clone(),
                            slot,
                            carbon_cost: delta_cost,
                            error: flavour.error,
                        };

                        let entry = dp_curr.entry(new_key).or_insert((f64::INFINITY, vec![]));
                        if new_cost < entry.0 {
                            let mut new_assignments = prev_assignments.clone();
                            new_assignments.push(assignment);
                            *entry = (new_cost, new_assignments);
                        }
                    }
                }
            }

            if dp_curr.is_empty() {
                // DP dead-end: the warm-start solution is our safety net.
                return warm_assignments;
            }

            // ── pruning with warm-start protection ───────────────────────
            if matches!(self.pruning.as_str(), "beam" | "kbest") && dp_curr.len() > self.pruning_k {
                let mut items: Vec<(DpStateKey, (f64, Vec<RequestAssignment>))> =
                    dp_curr.into_iter().collect();

                if self.pruning == "beam" {
                    items.sort_unstable_by(|a, b| a.1 .0.partial_cmp(&b.1 .0).unwrap());
                } else {
                    // kbest: sort by (cost, avg_error)
                    items.sort_unstable_by(|a, b| {
                        let a_avg = if a.0.error_count() > 0.0 {
                            a.0.error_sum_bp as f64 / a.0.error_count()
                        } else {
                            0.0
                        };
                        let b_avg = if b.0.error_count() > 0.0 {
                            b.0.error_sum_bp as f64 / b.0.error_count()
                        } else {
                            0.0
                        };
                        a.1 .0
                            .partial_cmp(&b.1 .0)
                            .unwrap()
                            .then(a_avg.partial_cmp(&b_avg).unwrap())
                    });
                }

                // Locate the warm-start state for this layer.
                let warm_key = &warm_keys[req_idx + 1];
                let warm_idx = items.iter().position(|(k, _)| k == warm_key);

                // If the warm state is outside the top-K, reserve one slot for it.
                let take_n = if warm_idx.map_or(false, |i| i >= self.pruning_k) {
                    self.pruning_k.saturating_sub(1)
                } else {
                    self.pruning_k
                };

                let mut trimmed: HashMap<_, _> =
                    items.drain(..take_n.min(items.len())).collect();

                // Inject the warm state if it was pruned away.
                if let Some(wi) = warm_idx {
                    if wi >= self.pruning_k && wi < take_n + items.len() {
                        let actual_idx = wi - take_n;
                        let (k, v) = items.remove(actual_idx);
                        trimmed.insert(k, v);
                    }
                }

                dp_curr = trimmed;
            }

            // ── timeout: fall back to warm-start solution ────────────────
            if start.elapsed().as_secs_f64() > self.timeout {
                return warm_assignments;
            }

            dp_prev = dp_curr;
        }

        // ── feasibility filter ───────────────────────────────────────────
        if let Some(threshold) = input.max_error_threshold {
            dp_prev.retain(|k, _| {
                let ec = k.error_count();
                ec == 0.0 || (k.error_sum_bp as f64 / 100.0) / ec <= threshold
            });
            if dp_prev.is_empty() {
                return warm_assignments;
            }
        }

        // Return the minimum-cost feasible assignment, or warm-start if DP fails.
        dp_prev
            .into_values()
            .min_by(|a, b| a.0.partial_cmp(&b.0).unwrap())
            .map(|(_, assignments)| assignments)
            .unwrap_or(warm_assignments)
    }

    // ── warm-start helper ─────────────────────────────────────────────────

    /// Build a sequential (request-by-request) solution.
    ///
    /// For each request we greedily pick the cheapest (flavour, slot) pair
    /// given the *current* incremental counts.  This is exactly optimal when
    /// a batch contains a single request, and it mirrors the behaviour the
    /// user observed to be cheaper than the full-batch DP (which likely
    /// hit timeout / pruning).
    ///
    /// Returns:
    /// • `assignments` – the full sequential assignment vector.
    /// • `keys`        – the `DpStateKey` *before* the first request and
    ///                   after each processed request (`len = N+1`).
    fn compute_sequential_solution(
        &self,
        input: &SolveBatchInput<'_>,
        deadlines: &[i32],
        base_counts: &[i32],
        tiers: &[CapacityTier],
        initial_error_sum_bp: i64,
        initial_error_count: f64,
        initial_mock_count: i32,
        mock_error_bp: i64,
        window_start: i32,
        window_end: i32,
    ) -> (Vec<RequestAssignment>, Vec<DpStateKey>) {
        let t = self.window_size as usize;

        let mut key = DpStateKey::new(
            initial_error_sum_bp,
            initial_error_count,
            initial_mock_count,
            vec![0i32; t],
        );
        let mut keys = vec![key.clone()];
        let mut assignments = Vec::new();

        for (req_idx, (req_id, _)) in input.requests.iter().enumerate() {
            let deadline = deadlines[req_idx];
            let mut best: Option<(f64, &Flavour, i32)> = None;

            for flavour in &self.flavours {
                for slot in input.current_slot..=deadline {
                    let cost = self.incremental_carbon_cost(
                        slot,
                        flavour.duration,
                        base_counts,
                        &key.inc_counts,
                        tiers,
                    );
                    if best.map_or(true, |(c, _, _)| cost < c) {
                        best = Some((cost, flavour, slot));
                    }
                }
            }

            if let Some((cost, flavour, slot)) = best {
                let s = slot as usize;
                let f_error_bp = (flavour.error * 100.0).round() as i64;

                let mut new_error_sum_bp = key.error_sum_bp;
                let mut new_error_count = key.error_count();
                let mut new_mock_remaining = key.mock_remaining;

                if slot >= window_start && slot <= window_end {
                    new_error_sum_bp += f_error_bp;
                    new_error_count += 1.0;
                    if new_mock_remaining > 0 && mock_error_bp > 0 {
                        new_error_sum_bp -= mock_error_bp;
                        new_error_count = (new_error_count - 1.0).max(0.0);
                        new_mock_remaining -= 1;
                    }
                }

                let mut new_inc = key.inc_counts.clone();
                new_inc[s] += 1;

                key = DpStateKey::new(
                    new_error_sum_bp,
                    new_error_count,
                    new_mock_remaining,
                    new_inc,
                );
                keys.push(key.clone());

                assignments.push(RequestAssignment {
                    request_id: *req_id,
                    flavour_name: flavour.name.clone(),
                    slot,
                    carbon_cost: cost,
                    error: flavour.error,
                });
            }
        }

        (assignments, keys)
    }

    /// Greedy fallback: assign each request to the cheapest feasible slot using
    /// the most accurate (longest duration) flavour.
    pub fn greedy_fallback(
        &self,
        requests: &[(u64, i32)],
        deadlines: &[i32],
        current_slot: i32,
        capacity_tiers: &[CapacityTier],
        base_counts: &[i32],
    ) -> Vec<RequestAssignment> {
        let mut inc_counts = base_counts.to_vec();
        // Most accurate = longest duration.
        let fallback_flavour = self
            .flavours
            .iter()
            .max_by_key(|f| f.duration)
            .expect("at least one flavour");

        let mut assignments = Vec::new();

        for (i, (req_id, _)) in requests.iter().enumerate() {
            let deadline = deadlines[i];
            let mut best: Option<(f64, i32)> = None;
            let empty_base = vec![0i32; self.window_size as usize];

            for slot in current_slot..=deadline {
                let cost = self.incremental_carbon_cost(
                    slot,
                    fallback_flavour.duration,
                    &empty_base,
                    &inc_counts,
                    capacity_tiers,
                );
                if best.map_or(true, |(c, _)| cost < c) {
                    best = Some((cost, slot));
                }
            }

            if let Some((cost, best_slot)) = best {
                let s = best_slot as usize;
                inc_counts[s] += 1;
                assignments.push(RequestAssignment {
                    request_id: *req_id,
                    flavour_name: fallback_flavour.name.clone(),
                    slot: best_slot,
                    carbon_cost: cost,
                    error: fallback_flavour.error,
                });
            }
        }
        assignments
    }

    // ── internal helpers ──────────────────────────────────────────────────

    /// Marginal carbon cost of placing one request in `slot`.
    ///
    /// Under the per-request tier model, each request is charged based solely
    /// on its position (1-indexed count) within the slot.  The request at
    /// position K pays `carbon[slot] × mult(K) × duration × scale`, where
    /// `mult(K)` is the multiplier of the capacity tier that K falls into.
    ///
    /// This is a pure additive model: placing a new request never retroactively
    /// reprices earlier requests in the same slot.
    fn incremental_carbon_cost(
        &self,
        slot: i32,
        add_duration: i32,
        base_counts: &[i32],
        inc_counts: &[i32],
        tiers: &[CapacityTier],
    ) -> f64 {
        let s = slot as usize;
        let position = (base_counts[s] + inc_counts[s]) as i64 + 1; // 1-indexed position of this request
        let mult = get_capacity_multiplier(tiers, position);
        let carbon = self.carbon_forecast[s];
        carbon * mult * add_duration as f64 * self.carbon_cost_scale
    }
}

// ─── tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;

    fn make_solver(forecast: Vec<f64>) -> DpSolver {
        let cfg = Config::default();
        DpSolver::new(&cfg).with_carbon_forecast(forecast)
    }

    fn flat_forecast(n: usize, value: f64) -> Vec<f64> {
        vec![value; n]
    }

    fn no_tiers() -> Vec<CapacityTier> {
        vec![CapacityTier { max_requests: None, multiplier: 1.0 }]
    }

    fn make_input_with_maps<'a>(
        requests: &'a [(u64, i32)],
        current_slot: i32,
        tiers: &'a [CapacityTier],
        counts: &'a HashMap<i32, i32>,
    ) -> SolveBatchInput<'a> {
        SolveBatchInput {
            requests,
            current_slot,
            capacity_multiplier: 1.0,
            capacity_tiers: tiers,
            baseline_slot_counts: counts,
            error_window_baseline: ErrorWindowBaseline::default(),
            max_error_threshold: Some(4.0),
            error_window_past: 2,
            error_window_future: 2,
            assignment_max_slot: None,
            dynamic_mock_pool: MockPool::default(),
        }
    }

    fn make_input<'a>(
        requests: &'a [(u64, i32)],
        current_slot: i32,
        tiers: &'a [CapacityTier],
        counts: &'a HashMap<i32, i32>,
    ) -> SolveBatchInput<'a> {
        make_input_with_maps(requests, current_slot, tiers, counts)
    }

    #[test]
    fn single_request_assigned_cheapest_slot() {
        // Carbon forecast has a valley at slot 2; solver should place there.
        let forecast = vec![100.0, 80.0, 20.0, 80.0, 100.0];
        let solver = DpSolver {
            flavours: vec![Flavour { name: "A".to_string(), error: 0.0, duration: 60 }],
            window_size: 5,
            carbon_forecast: forecast,
            pruning: "none".to_string(),
            pruning_k: 100,
            timeout: 5.0,
            carbon_cost_scale: 1.0 / 3600.0,
        };
        let requests = vec![(1u64, 4i32)];
        let tiers = no_tiers();
        let counts = HashMap::new();
        let result = solver.solve_batch(make_input(&requests, 0, &tiers, &counts));
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].slot, 2);
    }

    #[test]
    fn per_request_tier_model_no_repricing() {
        // Tier threshold at 1 request: the 2nd request uses multiplier 2.0
        // but the 1st request keeps multiplier 1.0 (no repricing).
        let tiers = vec![
            CapacityTier { max_requests: Some(1), multiplier: 1.0 },
            CapacityTier { max_requests: None,    multiplier: 2.0 },
        ];
        let forecast = vec![40.0; 5]; // flat carbon
        let solver = DpSolver {
            flavours: vec![Flavour { name: "A".to_string(), error: 0.0, duration: 60 }],
            window_size: 5,
            carbon_forecast: forecast,
            pruning: "none".to_string(),
            pruning_k: 1000,
            timeout: 5.0,
            carbon_cost_scale: 1.0 / 3600.0,
        };
        // First request is at position 1 → tier 1 (mult=1.0): cost = 40 * 1.0 * 60/3600
        let cost_first = solver.incremental_carbon_cost(
            0, 60, &vec![0; 5], &vec![0; 5], &tiers,
        );
        assert!((cost_first - 40.0 * 1.0 * 60.0 / 3600.0).abs() < 1e-9,
            "cost_first={cost_first}");

        // Second request is at position 2 → tier 2 (mult=2.0): cost = 40 * 2.0 * 60/3600
        // Previous request is NOT repriced — this is the per-request model.
        let cost_second = solver.incremental_carbon_cost(
            0, 60, &vec![0; 5], &vec![1; 5], &tiers,
        );
        let expected_second = 40.0 * 2.0 * 60.0 / 3600.0;
        assert!((cost_second - expected_second).abs() < 1e-9,
            "cost_second={cost_second}, expected={expected_second}");

        // Total cost for 2 requests = cost_first + cost_second (additive, no repricing)
        let total = cost_first + cost_second;
        let expected_total = 40.0 * (1.0 * 60.0 + 2.0 * 60.0) / 3600.0;
        assert!((total - expected_total).abs() < 1e-9,
            "total={total}, expected={expected_total}");
    }

    #[test]
    fn tier_crossing_does_not_reprice_earlier_requests() {
        // 3 tiers: ≤2 mult=1.0, ≤4 mult=2.0, else mult=5.0; duration=30
        let tiers = vec![
            CapacityTier { max_requests: Some(2), multiplier: 1.0 },
            CapacityTier { max_requests: Some(4), multiplier: 2.0 },
            CapacityTier { max_requests: None,    multiplier: 5.0 },
        ];
        let forecast = vec![60.0; 5];
        let solver = DpSolver {
            flavours: vec![Flavour { name: "F".to_string(), error: 0.0, duration: 30 }],
            window_size: 5, carbon_forecast: forecast.clone(), pruning: "none".to_string(),
            pruning_k: 1000, timeout: 5.0, carbon_cost_scale: 1.0 / 3600.0,
        };
        let scale = 1.0 / 3600.0;
        // positions 1,2 → mult 1.0; positions 3,4 → mult 2.0; position 5 → mult 5.0
        let expected_costs = [
            60.0 * 1.0 * 30.0 * scale, // position 1
            60.0 * 1.0 * 30.0 * scale, // position 2
            60.0 * 2.0 * 30.0 * scale, // position 3 (crosses tier)
            60.0 * 2.0 * 30.0 * scale, // position 4
            60.0 * 5.0 * 30.0 * scale, // position 5 (crosses tier)
        ];
        for (i, &expected) in expected_costs.iter().enumerate() {
            let mut inc_counts = vec![0i32; 5];
            inc_counts[0] = i as i32; // i requests already placed at slot 0
            let cost = solver.incremental_carbon_cost(0, 30, &vec![0; 5], &inc_counts, &tiers);
            assert!((cost - expected).abs() < 1e-9,
                "position={}: cost={cost}, expected={expected}", i + 1);
        }
    }

    #[test]
    fn error_constraint_filters_infeasible_states() {
        // Force deadline=2 so ALL valid slots (0..=2) are inside the error window.
        // With threshold=0.0, only Accurate (error=0) is feasible for any in-window slot.
        let forecast = flat_forecast(5, 40.0);
        let solver = DpSolver {
            flavours: vec![
                Flavour { name: "Accurate".to_string(), error: 0.0, duration: 60 },
                Flavour { name: "Fast".to_string(), error: 5.0, duration: 10 },
            ],
            window_size: 5,
            carbon_forecast: forecast,
            pruning: "none".to_string(),
            pruning_k: 1000,
            timeout: 5.0,
            carbon_cost_scale: 1.0 / 3600.0,
        };
        let requests = vec![(1u64, 2i32)]; // deadline=2, window_future=2 → all slots in window
        let tiers = no_tiers();
        let counts = HashMap::new();
        let mut input = make_input(&requests, 0, &tiers, &counts);
        input.max_error_threshold = Some(0.0);
        let result = solver.solve_batch(input);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].flavour_name, "Accurate");
        assert_eq!(result[0].error, 0.0);
    }

    #[test]
    fn beam_pruning_still_finds_feasible_solution() {
        // Forecast: slots 0-2 (in window) are expensive, slots 3+ are cheap.
        // Beam pruning will keep cheapest states → assigns to slots 3+, outside
        // the error window → error_count == 0 → feasibility filter always passes.
        let forecast = {
            let mut f = vec![100.0f64; 24];
            for v in &mut f[3..] {
                *v = 50.0;
            }
            f
        };
        let solver = DpSolver {
            flavours: vec![
                Flavour { name: "A".to_string(), error: 0.0, duration: 60 },
                Flavour { name: "B".to_string(), error: 2.5, duration: 30 },
                Flavour { name: "C".to_string(), error: 5.0, duration: 10 },
            ],
            window_size: 24,
            carbon_forecast: forecast,
            pruning: "beam".to_string(),
            pruning_k: 5,
            timeout: 10.0,
            carbon_cost_scale: 1.0 / 3600.0,
        };
        let requests: Vec<(u64, i32)> = (1..=5).map(|i| (i, 23)).collect();
        let tiers = no_tiers();
        let counts = HashMap::new();
        let result = solver.solve_batch(make_input(&requests, 0, &tiers, &counts));
        assert_eq!(result.len(), 5, "all requests must be scheduled");
        // All assignments should be in cheap slots (outside window).
        for a in &result {
            assert!(a.slot >= 3, "beam should prefer cheap slots outside error window");
        }
    }

    #[test]
    fn greedy_fallback_covers_all_requests() {
        let solver = DpSolver {
            flavours: vec![
                Flavour { name: "Accurate".to_string(), error: 0.0, duration: 60 },
                Flavour { name: "Fast".to_string(), error: 5.0, duration: 10 },
            ],
            window_size: 5,
            carbon_forecast: flat_forecast(5, 40.0),
            pruning: "none".to_string(),
            pruning_k: 1000,
            timeout: 5.0,
            carbon_cost_scale: 1.0 / 3600.0,
        };
        let requests = vec![(1u64, 0i32), (2u64, 1i32), (3u64, 2i32)];
        let deadlines = vec![0, 1, 2];
        let tiers = no_tiers();
        let base = vec![0i32; 5];
        let result = solver.greedy_fallback(&requests, &deadlines, 0, &tiers, &base);
        assert_eq!(result.len(), 3);
    }

    #[test]
    fn empty_batch_returns_empty() {
        let solver = make_solver(flat_forecast(24, 100.0));
        let tiers = no_tiers();
        let counts = HashMap::new();
        let result = solver.solve_batch(make_input(&[], 0, &tiers, &counts));
        assert!(result.is_empty());
    }

    #[test]
    fn current_slot_beyond_window_returns_empty() {
        let solver = DpSolver {
            flavours: vec![Flavour { name: "A".to_string(), error: 0.0, duration: 60 }],
            window_size: 5,
            carbon_forecast: flat_forecast(5, 100.0),
            pruning: "none".to_string(),
            pruning_k: 100,
            timeout: 5.0,
            carbon_cost_scale: 1.0 / 3600.0,
        };
        let tiers = no_tiers();
        let requests = vec![(1u64, 4i32)];
        let counts = HashMap::new();
        let result = solver.solve_batch(SolveBatchInput {
            current_slot: 5, // == window_size → early return
            ..make_input_with_maps(&requests, 5, &tiers, &counts)
        });
        assert!(result.is_empty());
    }
}
