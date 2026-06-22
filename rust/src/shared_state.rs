/// Thread-safe shared state for the CarbonShift scheduler.
///
/// Mirrors `shared_state.py::SharedSchedulerState`.
///
/// All public methods acquire an internal `Mutex` and perform the operation
/// atomically.  Callers never need to manage locks themselves.
///
/// # Concurrency model
/// A single `Mutex<SharedStateInner>` wraps all mutable fields.  This matches
/// the Python `RLock` pattern: every public method takes the lock, does its
/// work, and releases it.  Lock granularity is deliberately coarse for
/// simplicity and correctness; hot-path profiling can guide future
/// optimisations without changing the public API.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicI32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use crate::types::{Assignment, CapacityTier, Request};

// ─── solver snapshot ─────────────────────────────────────────────────────────

/// Immutable snapshot of all shared-state data needed by `solve_dp`.
///
/// Captured in a **single** lock acquisition before the solver starts, so
/// every read inside the solver (future assignments, window error stats,
/// per-slot requests, global stats) sees a consistent view of the world at
/// the moment the batch was dispatched.  The lock is released before the DP
/// optimiser runs.
pub struct SolverSnapshot {
    pub assignments: HashMap<u64, Assignment>,
    pub global_error_sum: f64,
    pub global_assignment_count: u64,
}

impl SolverSnapshot {
    /// Assignments scheduled at or after `current_slot`.
    pub fn get_future_assignments(&self, current_slot: i32) -> Vec<Assignment> {
        self.assignments
            .values()
            .filter(|a| a.scheduled_slot >= current_slot)
            .cloned()
            .collect()
    }

    /// Weighted error stats for the window
    /// `[center − window_past, center + window_future]`.
    pub fn get_window_error_stats(
        &self,
        center_slot: i32,
        window_past: i32,
        window_future: i32,
        exclude: &HashSet<u64>,
    ) -> WindowErrorStats {
        let start = center_slot - window_past;
        let end   = center_slot + window_future;
        let mut error_sum = 0.0f64;
        let mut count = 0u64;
        for a in self.assignments.values() {
            if exclude.contains(&a.request_id) {
                continue;
            }
            if a.scheduled_slot >= start && a.scheduled_slot <= end {
                error_sum += a.error;
                count += 1;
            }
        }
        let average = if count > 0 { error_sum / count as f64 } else { 0.0 };
        WindowErrorStats { error_sum, count, average }
    }

    /// All assignments in exactly `slot`.
    pub fn get_requests_in_slot(&self, slot: i32) -> Vec<&Assignment> {
        self.assignments
            .values()
            .filter(|a| a.scheduled_slot == slot)
            .collect()
    }

    /// Cumulative error stats.
    pub fn get_global_error_stats(&self) -> GlobalErrorStats {
        let count = self.global_assignment_count;
        let error_sum = self.global_error_sum;
        let avg = if count > 0 { error_sum / count as f64 } else { 0.0 };
        GlobalErrorStats { error_sum, count, avg }
    }
}

// ─── inner (lock-guarded) data ────────────────────────────────────────────────

struct Inner {
    /// Requests waiting to be processed by a batch solver.
    pending: Vec<Request>,
    /// Active assignments (request_id → Assignment).
    assignments: HashMap<u64, Assignment>,
    /// Current time slot (updated by the scheduler loop).
    current_slot: i32,
    /// Cumulative statistics (never reset by archiving).
    total_received: u64,
    total_scheduled: u64,
    /// Running global error totals across all ever-assigned requests.
    global_error_sum: f64,
    global_assignment_count: u64,
    // ── rollback tracking ─────────────────────────────────────────────────
    /// Total number of rollbacks (tier-breach detections) that occurred.
    total_rollbacks: u64,
    /// Highest consecutive-rollback count seen for a single batch.
    max_consecutive_rollbacks_seen: u64,
    /// Request IDs that were ultimately committed after ≥1 rollback attempt.
    rolled_back_request_ids: HashSet<u64>,
}

impl Inner {
    fn new() -> Self {
        Self {
            pending: Vec::new(),
            assignments: HashMap::new(),
            current_slot: 0,
            total_received: 0,
            total_scheduled: 0,
            global_error_sum: 0.0,
            global_assignment_count: 0,
            total_rollbacks: 0,
            max_consecutive_rollbacks_seen: 0,
            rolled_back_request_ids: HashSet::new(),
        }
    }
}

// ─── public handle ────────────────────────────────────────────────────────────

/// Cloneable, cheaply-sharable handle to the shared scheduler state.
///
/// Clone this to share ownership across threads — each clone holds a reference
/// to the same underlying mutex.
///
/// `virtual_elapsed_ms` is a lock-free counter driven by the scheduler loop.
/// In normal mode it tracks wall-clock time; when `skip_empty_slots` is
/// enabled the scheduler advances it to the next slot boundary as soon as the
/// queue is empty, so the generator and monitor also "see" the jump.
#[derive(Clone)]
pub struct SharedState {
    inner: Arc<Mutex<Inner>>,
    /// Virtual elapsed time in milliseconds since the run started.
    /// Written only by the scheduler loop; read by generator and monitor.
    pub virtual_elapsed_ms: Arc<AtomicU64>,
    /// Last slot index fully processed by the generator.
    /// Used by the scheduler's skip-empty-slots logic to avoid skipping a slot
    /// before the generator has had a chance to add its requests.
    generator_processed_slot: Arc<AtomicI32>,
}

impl SharedState {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(Inner::new())),
            virtual_elapsed_ms: Arc::new(AtomicU64::new(0)),
            generator_processed_slot: Arc::new(AtomicI32::new(-1)),
        }
    }

    // ── virtual clock ────────────────────────────────────────────────────

    /// Set the virtual elapsed time (called every scheduler loop tick).
    #[inline]
    pub fn set_virtual_elapsed_ms(&self, ms: u64) {
        self.virtual_elapsed_ms.store(ms, Ordering::Relaxed);
    }

    /// Read virtual elapsed time in seconds.
    #[inline]
    pub fn virtual_elapsed_secs(&self) -> f64 {
        self.virtual_elapsed_ms.load(Ordering::Relaxed) as f64 / 1000.0
    }

    /// Record that the generator has finished processing `slot`.
    /// Called by the generator thread after adding all requests for a slot.
    #[inline]
    pub fn set_generator_processed_slot(&self, slot: i32) {
        self.generator_processed_slot.store(slot, Ordering::Release);
    }

    /// Return the last slot index confirmed processed by the generator.
    #[inline]
    pub fn generator_processed_slot(&self) -> i32 {
        self.generator_processed_slot.load(Ordering::Acquire)
    }

    // ── request queue ─────────────────────────────────────────────────────

    /// Append a new request to the pending queue.
    pub fn add_request(&self, request: Request) {
        let mut g = self.inner.lock().unwrap();
        g.pending.push(request);
        g.total_received += 1;
    }

    /// Atomically claim (remove) the first `count` pending requests.
    ///
    /// Returns fewer than `count` elements if the queue is shorter.
    pub fn claim_pending_requests(&self, count: usize) -> Vec<Request> {
        let mut g = self.inner.lock().unwrap();
        let n = count.min(g.pending.len());
        g.pending.drain(..n).collect()
    }

    /// Return `requests` to the front of the pending queue (preserving order).
    ///
    /// Used when a claimed batch could not be scheduled and must be retried.
    pub fn requeue_pending_requests_front(&self, mut requests: Vec<Request>) {
        if requests.is_empty() {
            return;
        }
        let mut g = self.inner.lock().unwrap();
        // Prepend: append existing queue to the end of `requests`, then replace.
        requests.extend(g.pending.drain(..));
        g.pending = requests;
    }

    /// Number of pending requests currently in the queue.
    pub fn get_pending_count(&self) -> usize {
        self.inner.lock().unwrap().pending.len()
    }

    /// Virtual age in milliseconds of the oldest pending request, or `None` if
    /// the queue is empty.  Uses the request's `arrival_time` field (seconds)
    /// vs. the provided `virtual_ms` for the comparison.
    pub fn get_oldest_pending_age_ms(&self, virtual_ms: u64) -> Option<u64> {
        let g = self.inner.lock().unwrap();
        g.pending.first().map(|r| {
            let arrival_ms = (r.arrival_time * 1000.0) as u64;
            virtual_ms.saturating_sub(arrival_ms)
        })
    }

    /// Remove and return all pending requests (used after the run ends to
    /// collect unprocessed requests for late scheduling).
    pub fn drain_pending_requests(&self) -> Vec<Request> {
        let mut g = self.inner.lock().unwrap();
        g.pending.drain(..).collect()
    }



    /// Record a batch of scheduling decisions.
    ///
    /// - Overwrites any existing assignment for the same request_id.
    /// - Increments global error totals only for *new* request ids (not
    ///   re-planned ones, to avoid double-counting).
    pub fn add_assignments(&self, assignments: Vec<Assignment>) {
        let mut g = self.inner.lock().unwrap();
        for a in assignments {
            let is_new = !g.assignments.contains_key(&a.request_id);
            if is_new {
                g.total_scheduled += 1;
                g.global_error_sum += a.error;
                g.global_assignment_count += 1;
            }
            g.assignments.insert(a.request_id, a);
        }
    }

    /// Snapshot of all active assignments.
    pub fn get_current_assignments(&self) -> HashMap<u64, Assignment> {
        self.inner.lock().unwrap().assignments.clone()
    }

    /// Capture all data needed by `solve_dp` in a **single** lock acquisition.
    ///
    /// Call this once at the start of each solver invocation to get a
    /// consistent view of the shared state.  The lock is not held after this
    /// method returns.
    pub fn snapshot_for_solver(&self) -> SolverSnapshot {
        let g = self.inner.lock().unwrap();
        SolverSnapshot {
            assignments: g.assignments.clone(),
            global_error_sum: g.global_error_sum,
            global_assignment_count: g.global_assignment_count,
        }
    }

    /// All assignments whose `scheduled_slot >= current_slot`.
    pub fn get_future_assignments(&self, current_slot: i32) -> Vec<Assignment> {
        self.inner
            .lock()
            .unwrap()
            .assignments
            .values()
            .filter(|a| a.scheduled_slot >= current_slot)
            .cloned()
            .collect()
    }

    // ── error stats ───────────────────────────────────────────────────────

    /// Weighted error statistics for a sliding window
    /// `[center_slot − window_past, center_slot + window_future]`.
    ///
    /// Assignments whose `request_id` is in `exclude` are skipped (used when
    /// re-planning movable future assignments).
    pub fn get_window_error_stats(
        &self,
        center_slot: i32,
        window_past: i32,
        window_future: i32,
        exclude: &std::collections::HashSet<u64>,
    ) -> WindowErrorStats {
        let g = self.inner.lock().unwrap();
        let start = center_slot - window_past;
        let end = center_slot + window_future;
        let mut error_sum = 0.0f64;
        let mut count = 0u64;
        for a in g.assignments.values() {
            if exclude.contains(&a.request_id) {
                continue;
            }
            if a.scheduled_slot >= start && a.scheduled_slot <= end {
                error_sum += a.error;
                count += 1;
            }
        }
        let average = if count > 0 { error_sum / count as f64 } else { 0.0 };
        WindowErrorStats { error_sum, count, average }
    }

    /// Cumulative error stats across all ever-assigned requests.
    pub fn get_global_error_stats(&self) -> GlobalErrorStats {
        let g = self.inner.lock().unwrap();
        let count = g.global_assignment_count;
        let error_sum = g.global_error_sum;
        let avg = if count > 0 { error_sum / count as f64 } else { 0.0 };
        GlobalErrorStats { error_sum, count, avg }
    }

    // ── slot management ───────────────────────────────────────────────────

    pub fn set_current_slot(&self, slot: i32) {
        self.inner.lock().unwrap().current_slot = slot;
    }

    pub fn get_current_slot(&self) -> i32 {
        self.inner.lock().unwrap().current_slot
    }

    // ── statistics ────────────────────────────────────────────────────────

    pub fn get_statistics(&self) -> Statistics {
        let g = self.inner.lock().unwrap();
        Statistics {
            total_received: g.total_received,
            total_scheduled: g.total_scheduled,
            pending: g.pending.len(),
            current_slot: g.current_slot,
        }
    }

    /// All assignments whose `scheduled_slot == slot`.
    pub fn get_requests_in_slot(&self, slot: i32) -> Vec<Assignment> {
        self.inner
            .lock()
            .unwrap()
            .assignments
            .values()
            .filter(|a| a.scheduled_slot == slot)
            .cloned()
            .collect()
    }

    // ── CSV export ────────────────────────────────────────────────────────

    /// Export all current assignments to a CSV file.
    pub fn export_to_csv(&self, path: &str) -> std::io::Result<()> {
        let g = self.inner.lock().unwrap();
        let mut wtr = csv::Writer::from_path(path)?;
        wtr.write_record([
            "request_id",
            "scheduled_slot",
            "flavour",
            "carbon_cost",
            "error",
            "assignment_time",
        ])?;
        for a in g.assignments.values() {
            wtr.write_record(&[
                a.request_id.to_string(),
                a.scheduled_slot.to_string(),
                a.flavour_name.clone(),
                a.carbon_cost.to_string(),
                a.error.to_string(),
                a.assignment_time.to_string(),
            ])?;
        }
        wtr.flush()?;
        Ok(())
    }
    // ── rollback ──────────────────────────────────────────────────────────────

    /// Atomically attempt to commit assignments, checking for unintended
    /// capacity-tier breaches caused by concurrent thread activity.
    ///
    /// `expected_per_slot` maps each slot to the occupancy the solver assumed
    /// after committing this batch:
    ///   expected[s] = baseline_slot_counts[s] + (assignments in batch to slot s)
    ///
    /// If the actual occupancy (current shared_state count + batch additions) for
    /// any slot would land in a *higher* capacity tier than the solver expected,
    /// the commit is refused and `CommitOutcome::RolledBack` is returned —
    /// nothing is written to the assignment map.
    ///
    /// `force_commit` bypasses the check (used once the per-batch rollback limit K
    /// is reached, so the batch is committed even with a tier breach).
    ///
    /// `consecutive_before` is the number of rollbacks this batch has already
    /// experienced; used to update `max_consecutive_rollbacks_seen`.
    ///
    /// Note: when `dp_lock_future_assignments=false` the check may be conservative
    /// (occasional spurious rollbacks) because re-planned future requests still
    /// appear at their old slots in the assignment map until overwritten.  The
    /// K-limit ensures the batch is eventually committed.
    pub fn try_add_assignments_checked(
        &self,
        assignments: &[Assignment],
        expected_per_slot: &HashMap<i32, i32>,
        tiers: &[CapacityTier],
        force_commit: bool,
        consecutive_before: usize,
    ) -> CommitOutcome {
        let mut g = self.inner.lock().unwrap();

        if !force_commit {
            for (&slot, &expected) in expected_per_slot {
                let current_before = g.assignments.values()
                    .filter(|a| a.scheduled_slot == slot)
                    .count() as i32;
                let batch_adds = assignments.iter()
                    .filter(|a| a.scheduled_slot == slot)
                    .count() as i32;
                let actual_after = current_before + batch_adds;

                if actual_after > expected {
                    let expected_mult = capacity_multiplier_for_count(expected, tiers);
                    let actual_mult   = capacity_multiplier_for_count(actual_after, tiers);
                    if actual_mult > expected_mult {
                        g.total_rollbacks += 1;
                        let consec = (consecutive_before + 1) as u64;
                        if consec > g.max_consecutive_rollbacks_seen {
                            g.max_consecutive_rollbacks_seen = consec;
                        }
                        return CommitOutcome::RolledBack;
                    }
                }
            }
        }

        // Commit the assignments.
        for a in assignments {
            let is_new = !g.assignments.contains_key(&a.request_id);
            if is_new {
                g.total_scheduled += 1;
                g.global_error_sum += a.error;
                g.global_assignment_count += 1;
            }
            g.assignments.insert(a.request_id, a.clone());
        }
        CommitOutcome::Committed
    }

    /// Mark request IDs as having been committed after ≥1 rollback.
    pub fn mark_requests_rolled_back(&self, request_ids: &[u64]) {
        if request_ids.is_empty() {
            return;
        }
        let mut g = self.inner.lock().unwrap();
        for &id in request_ids {
            g.rolled_back_request_ids.insert(id);
        }
    }

    /// Cumulative rollback statistics for the entire scenario run.
    pub fn get_rollback_stats(&self) -> RollbackStats {
        let g = self.inner.lock().unwrap();
        RollbackStats {
            total_rollbacks: g.total_rollbacks,
            max_consecutive_rollbacks: g.max_consecutive_rollbacks_seen,
            requests_assigned_with_rollback: g.rolled_back_request_ids.len() as u64,
        }
    }

    /// Set of request IDs that were committed after ≥1 rollback (for PerRequest tagging).
    pub fn get_rolled_back_request_ids(&self) -> HashSet<u64> {
        self.inner.lock().unwrap().rolled_back_request_ids.clone()
    }
}

impl Default for SharedState {
    fn default() -> Self {
        Self::new()
    }
}

// ─── capacity-tier helper (mirrors DpSolver::get_capacity_multiplier) ─────────

fn capacity_multiplier_for_count(count: i32, tiers: &[CapacityTier]) -> f64 {
    for tier in tiers {
        match tier.max_requests {
            None => return tier.multiplier,
            Some(max) if (count as i64) <= max => return tier.multiplier,
            _ => {}
        }
    }
    tiers.last().map(|t| t.multiplier).unwrap_or(1.0)
}

// ─── result structs ───────────────────────────────────────────────────────────

/// Outcome of [`SharedState::try_add_assignments_checked`].
#[derive(Debug, PartialEq)]
pub enum CommitOutcome {
    Committed,
    RolledBack,
}

/// Aggregated rollback statistics for a completed scenario run.
#[derive(Debug, Clone, Default)]
pub struct RollbackStats {
    /// Total number of tier-breach rollbacks detected across all batches.
    pub total_rollbacks: u64,
    /// Largest consecutive-rollback count seen for any single batch.
    pub max_consecutive_rollbacks: u64,
    /// Number of requests that were ultimately assigned after ≥1 rollback.
    pub requests_assigned_with_rollback: u64,
}

#[derive(Debug, Clone)]
pub struct WindowErrorStats {
    pub error_sum: f64,
    pub count: u64,
    pub average: f64,
}

#[derive(Debug, Clone)]
pub struct GlobalErrorStats {
    pub error_sum: f64,
    pub count: u64,
    pub avg: f64,
}

#[derive(Debug, Clone)]
pub struct Statistics {
    pub total_received: u64,
    pub total_scheduled: u64,
    pub pending: usize,
    pub current_slot: i32,
}

// ─── tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{Assignment, Request};
    use std::collections::HashSet;

    fn make_request(id: u64, arrival: i32, deadline: i32) -> Request {
        Request { id, arrival_slot: arrival, deadline_slot: deadline, arrival_time: 0.0 }
    }

    fn make_assignment(id: u64, slot: i32, error: f64) -> Assignment {
        Assignment::new(id, slot, "Accurate".to_string(), 1.0, error, 60, None, None)
    }

    #[test]
    fn add_and_claim_requests() {
        let state = SharedState::new();
        state.add_request(make_request(1, 0, 5));
        state.add_request(make_request(2, 0, 5));
        state.add_request(make_request(3, 0, 5));
        assert_eq!(state.get_pending_count(), 3);
        let claimed = state.claim_pending_requests(2);
        assert_eq!(claimed.len(), 2);
        assert_eq!(state.get_pending_count(), 1);
    }

    #[test]
    fn claim_returns_at_most_available() {
        let state = SharedState::new();
        state.add_request(make_request(1, 0, 5));
        let claimed = state.claim_pending_requests(10);
        assert_eq!(claimed.len(), 1);
        assert_eq!(state.get_pending_count(), 0);
    }

    #[test]
    fn requeue_preserves_order() {
        let state = SharedState::new();
        state.add_request(make_request(5, 0, 5));
        let r1 = make_request(1, 0, 5);
        let r2 = make_request(2, 0, 5);
        state.requeue_pending_requests_front(vec![r1.clone(), r2.clone()]);
        let claimed = state.claim_pending_requests(3);
        assert_eq!(claimed[0].id, 1);
        assert_eq!(claimed[1].id, 2);
        assert_eq!(claimed[2].id, 5);
    }

    #[test]
    fn add_assignments_updates_global_error() {
        let state = SharedState::new();
        state.add_assignments(vec![make_assignment(1, 0, 4.0), make_assignment(2, 0, 2.0)]);
        let g = state.get_global_error_stats();
        assert_eq!(g.count, 2);
        assert!((g.error_sum - 6.0).abs() < 1e-9);
        assert!((g.avg - 3.0).abs() < 1e-9);
    }

    #[test]
    fn replanned_request_not_double_counted() {
        let state = SharedState::new();
        state.add_assignments(vec![make_assignment(1, 0, 4.0)]);
        // Replan request 1 (same id, different slot/cost)
        state.add_assignments(vec![make_assignment(1, 1, 0.0)]);
        let g = state.get_global_error_stats();
        // Only counted once
        assert_eq!(g.count, 1);
        assert!((g.error_sum - 4.0).abs() < 1e-9);
    }

    #[test]
    fn window_error_stats_excludes_out_of_window() {
        let state = SharedState::new();
        // slot 5 is in window [3,7], slot 10 is outside
        state.add_assignments(vec![make_assignment(1, 5, 4.0), make_assignment(2, 10, 2.0)]);
        let stats = state.get_window_error_stats(5, 2, 2, &HashSet::new());
        assert_eq!(stats.count, 1);
        assert!((stats.error_sum - 4.0).abs() < 1e-9);
    }

    #[test]
    fn window_error_stats_respects_exclude_set() {
        let state = SharedState::new();
        state.add_assignments(vec![make_assignment(1, 5, 4.0), make_assignment(2, 5, 2.0)]);
        let mut exclude = HashSet::new();
        exclude.insert(1u64);
        let stats = state.get_window_error_stats(5, 2, 2, &exclude);
        assert_eq!(stats.count, 1);
        assert!((stats.error_sum - 2.0).abs() < 1e-9);
    }

    #[test]
    fn future_assignments_filtered_by_slot() {
        let state = SharedState::new();
        state.add_assignments(vec![
            make_assignment(1, 3, 0.0),
            make_assignment(2, 5, 0.0),
            make_assignment(3, 7, 0.0),
        ]);
        let future = state.get_future_assignments(5);
        let ids: Vec<u64> = future.iter().map(|a| a.request_id).collect();
        assert!(ids.contains(&2));
        assert!(ids.contains(&3));
        assert!(!ids.contains(&1));
    }

    // ── Rollback / try_add_assignments_checked tests ───────────────────────────

    fn tiers_with_implicit_one() -> Vec<CapacityTier> {
        // [{30,1.0}, {50,1.5}, {null,2.0}] — explicit 1.0 first tier
        vec![
            CapacityTier { max_requests: Some(30), multiplier: 1.0 },
            CapacityTier { max_requests: Some(50), multiplier: 1.5 },
            CapacityTier { max_requests: None,     multiplier: 2.0 },
        ]
    }

    fn tiers_no_implicit_one() -> Vec<CapacityTier> {
        // [{30,1.5}, {50,2.0}, {null,5.0}] — battery-style (no explicit 1.0)
        vec![
            CapacityTier { max_requests: Some(30), multiplier: 1.5 },
            CapacityTier { max_requests: Some(50), multiplier: 2.0 },
            CapacityTier { max_requests: None,     multiplier: 5.0 },
        ]
    }

    /// Commit `count` dummy assignments to `slot`, using sequential request IDs
    /// starting from `id_start`.
    fn prefill_slot(state: &SharedState, slot: i32, count: usize, id_start: u64) {
        let assignments: Vec<Assignment> = (0..count as u64)
            .map(|i| make_assignment(id_start + i, slot, 0.0))
            .collect();
        state.add_assignments(assignments);
    }

    #[test]
    fn rollback_no_race_commits() {
        // No concurrent writes: current_before == baseline => actual_after == expected => no rollback.
        let state = SharedState::new();
        let tiers = tiers_with_implicit_one();

        // Pre-fill slot 5 with 25 requests (baseline the solver will have observed).
        prefill_slot(&state, 5, 25, 1000);

        // Batch of 5 new requests for slot 5.
        let batch: Vec<Assignment> = (0..5u64).map(|i| make_assignment(i, 5, 0.0)).collect();
        // expected = baseline(25) + batch_adds(5) = 30
        let mut expected = HashMap::new();
        expected.insert(5i32, 30i32);

        let outcome = state.try_add_assignments_checked(&batch, &expected, &tiers, false, 0);
        assert_eq!(outcome, CommitOutcome::Committed);
    }

    #[test]
    fn rollback_race_same_tier_commits() {
        // Race occurred but both expected and actual_after land in the same tier => no rollback.
        let state = SharedState::new();
        let tiers = tiers_with_implicit_one();

        // Baseline solver saw: slot 5 has 31 requests (already past the 30-threshold → tier 1.5).
        // Race: 3 more were committed in the meantime → now 34.
        prefill_slot(&state, 5, 34, 1000);

        // Batch adds 5 more.
        let batch: Vec<Assignment> = (0..5u64).map(|i| make_assignment(i, 5, 0.0)).collect();
        // expected = baseline(31) + batch_adds(5) = 36
        let mut expected = HashMap::new();
        expected.insert(5i32, 36i32);

        // actual_after = 34 + 5 = 39; expected = 36
        // Both in tier 1.5 (31-50) => same tier => Committed.
        let outcome = state.try_add_assignments_checked(&batch, &expected, &tiers, false, 0);
        assert_eq!(outcome, CommitOutcome::Committed);
    }

    #[test]
    fn rollback_race_tier_breach_rolls_back() {
        // Race pushed slot past a tier boundary that the solver did NOT intend to cross.
        let state = SharedState::new();
        let tiers = tiers_with_implicit_one();

        // Baseline solver saw: slot 5 has 25 requests (tier 1.0, ≤30).
        // Race: 8 more were committed → now 33 (tier 1.5, >30).
        prefill_slot(&state, 5, 33, 1000);

        // Batch adds 5 more.
        let batch: Vec<Assignment> = (0..5u64).map(|i| make_assignment(i, 5, 0.0)).collect();
        // expected = baseline(25) + batch_adds(5) = 30 → capacity_multiplier(30) = 1.0
        // actual_after = 33 + 5 = 38 → capacity_multiplier(38) = 1.5
        // 1.5 > 1.0 => RolledBack
        let mut expected = HashMap::new();
        expected.insert(5i32, 30i32);

        let outcome = state.try_add_assignments_checked(&batch, &expected, &tiers, false, 0);
        assert_eq!(outcome, CommitOutcome::RolledBack);

        // Nothing should have been committed.
        assert_eq!(state.get_current_assignments().len(), 33);
    }

    #[test]
    fn rollback_force_commit_bypasses_tier_check() {
        // Even with a tier breach, force_commit=true always commits.
        let state = SharedState::new();
        let tiers = tiers_with_implicit_one();

        prefill_slot(&state, 5, 33, 1000);

        let batch: Vec<Assignment> = (0..5u64).map(|i| make_assignment(i, 5, 0.0)).collect();
        let mut expected = HashMap::new();
        expected.insert(5i32, 30i32); // same breach scenario as above

        let outcome = state.try_add_assignments_checked(&batch, &expected, &tiers, true, 0);
        assert_eq!(outcome, CommitOutcome::Committed);
        assert_eq!(state.get_current_assignments().len(), 38);
    }

    #[test]
    fn rollback_increments_stats_correctly() {
        // Each RolledBack outcome increments total_rollbacks and updates max_consecutive.
        let state = SharedState::new();
        let tiers = tiers_with_implicit_one();

        prefill_slot(&state, 5, 33, 1000);

        let batch: Vec<Assignment> = (0..5u64).map(|i| make_assignment(i, 5, 0.0)).collect();
        let mut expected = HashMap::new();
        expected.insert(5i32, 30i32);

        // First rollback (consecutive_before = 0)
        let _ = state.try_add_assignments_checked(&batch, &expected, &tiers, false, 0);
        // Second rollback (consecutive_before = 1)
        let _ = state.try_add_assignments_checked(&batch, &expected, &tiers, false, 1);

        let stats = state.get_rollback_stats();
        assert_eq!(stats.total_rollbacks, 2);
        assert_eq!(stats.max_consecutive_rollbacks, 2); // max(0+1, 1+1) = 2
    }

    #[test]
    fn rollback_battery_style_tiers_work() {
        // Battery config tiers don't have the explicit 1.0 first entry.
        // Test that tier-breach detection still functions correctly.
        let state = SharedState::new();
        let tiers = tiers_no_implicit_one(); // [{30,1.5}, {50,2.0}, {null,5.0}]

        // Baseline: slot 5 has 28 requests (tier 1.5, ≤30).
        // Race: 5 more committed → now 33 (tier 2.0, >30).
        prefill_slot(&state, 5, 33, 1000);

        // Batch adds 3 more.
        let batch: Vec<Assignment> = (0..3u64).map(|i| make_assignment(i, 5, 0.0)).collect();
        // expected = baseline(28) + batch_adds(3) = 31 → capacity_multiplier(31) = 2.0 (>30, ≤50)
        // actual_after = 33 + 3 = 36 → capacity_multiplier(36) = 2.0
        // Same tier → Committed (race didn't push to a NEW tier).
        let mut expected = HashMap::new();
        expected.insert(5i32, 31i32);
        let outcome = state.try_add_assignments_checked(&batch, &expected, &tiers, false, 0);
        assert_eq!(outcome, CommitOutcome::Committed, "same tier should not rollback");

        // Reset: now a breach scenario.
        let state2 = SharedState::new();
        prefill_slot(&state2, 5, 33, 1000);
        // expected = baseline(28) + batch(3) = 31, but we choose baseline=27 batch=3 → expected=30
        // expected=30 → tier 1.5 (30<=30); actual_after=36 → tier 2.0 (36>30, 36<=50) → BREACH
        let mut expected2 = HashMap::new();
        expected2.insert(5i32, 30i32); // baseline=27, batch=3
        let batch2: Vec<Assignment> = (3..6u64).map(|i| make_assignment(i, 5, 0.0)).collect();
        let outcome2 = state2.try_add_assignments_checked(&batch2, &expected2, &tiers, false, 0);
        assert_eq!(outcome2, CommitOutcome::RolledBack, "tier breach should rollback");
    }

    #[test]
    fn snapshot_captures_consistent_state() {
        let state = SharedState::new();

        // Pre-populate assignments across multiple slots.
        state.add_assignments(vec![
            make_assignment(1, 3, 0.1),
            make_assignment(2, 4, 0.2),
            make_assignment(3, 7, 0.3),
        ]);

        let snap = state.snapshot_for_solver();

        // Future assignments from snapshot match those from direct call.
        let mut direct = state.get_future_assignments(5);
        let mut from_snap = snap.get_future_assignments(5);
        direct.sort_by_key(|a| a.request_id);
        from_snap.sort_by_key(|a| a.request_id);
        assert_eq!(direct.len(), from_snap.len());
        for (d, s) in direct.iter().zip(from_snap.iter()) {
            assert_eq!(d.request_id, s.request_id);
            assert_eq!(d.scheduled_slot, s.scheduled_slot);
        }

        // Window error stats match.
        let excl = HashSet::new();
        let direct_ws = state.get_window_error_stats(5, 3, 3, &excl);
        let snap_ws   = snap.get_window_error_stats(5, 3, 3, &excl);
        assert!((direct_ws.error_sum - snap_ws.error_sum).abs() < 1e-12);
        assert_eq!(direct_ws.count, snap_ws.count);

        // Global stats match.
        let direct_gs = state.get_global_error_stats();
        let snap_gs   = snap.get_global_error_stats();
        assert!((direct_gs.error_sum - snap_gs.error_sum).abs() < 1e-12);
        assert_eq!(direct_gs.count, snap_gs.count);

        // Per-slot lookup.
        assert_eq!(snap.get_requests_in_slot(3).len(), 1);
        assert_eq!(snap.get_requests_in_slot(4).len(), 1);
        assert_eq!(snap.get_requests_in_slot(99).len(), 0);
    }
}
