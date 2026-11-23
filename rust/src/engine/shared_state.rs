/// Thread-safe shared state for the CarbonShift scheduler.
///
/// Mirrors `shared_state.py::SharedSchedulerState`.
///
/// Domain methods acquire the internal `Mutex`; global capacity-tier
/// reads/writes use a separate `RwLock`. Callers never manage either lock.
///
/// # Concurrency model
/// A coarse `Mutex<SharedStateInner>` protects assignments and request queues.
/// Batch workers hold the capacity-tier read lock through solve and commit;
/// the administrative setter takes the write lock. This prevents changing
/// the pricing curve halfway through a batch transaction.
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::atomic::{AtomicI32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, RwLock, RwLockReadGuard};

use crate::engine::qos::{ErrorWindowPolicy, QosProfileId};
use crate::engine::solver::SolverProposal;
use crate::types::{Assignment, CapacityTier, Flavour, Request, get_capacity_multiplier};

// Assignment IDs grouped by slot and profile
type AssignmentIdsBySlotProfile = BTreeMap<i32, HashMap<QosProfileId, HashSet<u64>>>;

#[derive(Clone, Copy, Default)]
struct SlotErrorTotals {
    error_sum: f64,
    count: u64,
}

// ─── solver snapshot ─────────────────────────────────────────────────────────

/// Immutable projection of shared-state data needed by one solver batch.
///
/// Captured in a **single** lock acquisition before the solver starts, so
/// future capacity, profile error windows, and cumulative statistics see a
/// consistent view at the moment the batch was dispatched. The lock is
/// released before the optimiser runs.
pub struct SolverSnapshot {
    /// Global occupancy by slot for capacity-tier pricing.
    pub baseline_slot_counts: HashMap<i32, i64>,
    pub global_error_sum: f64,
    pub global_assignment_count: u64,
    pub profile_error_stats: HashMap<QosProfileId, GlobalErrorStats>,
    profile_slot_errors: HashMap<QosProfileId, BTreeMap<i32, SlotErrorTotals>>,
    global_slot_errors: BTreeMap<i32, SlotErrorTotals>,
}

struct SolverSnapshotScope {
    /// Only this profile's error history belongs in the scoped aggregates.
    profile_id: QosProfileId,
    /// The batch is solved against this point in virtual time.
    current_slot: i32,
    /// Oldest slot needed for the active past window and decayed history.
    history_from_slot: i32,
    /// Conservative latest target slot needed for this batch's capacity baseline.
    assignment_through_slot: i32,
    /// Latest slot needed to evaluate error windows for candidate assignments.
    error_through_slot: i32,
}

/// Compute the slot ranges needed to build one batch's solver snapshot.
///
/// The request queue is grouped by profile, so one immutable error-window
/// policy applies to the whole batch. Global occupancy counts are needed
/// through a conservative upper bound on its target slots for capacity
/// pricing. Error totals need a wider range: the past window plus its decay
/// lookback, and the candidate slots' future error windows.
fn solver_snapshot_scope(
    profile_id: &QosProfileId,
    requests: &[Request],
    current_slot: i32,
    total_slots: i32,
    error_window: &ErrorWindowPolicy,
) -> SolverSnapshotScope {
    // The horizon is finite and zero-indexed, so its last valid slot is
    // total_slots - 1. Keep the range non-negative even for invalid/empty
    // horizon values; the solver will reject an invalid planning horizon.
    let last_planning_slot = total_slots.saturating_sub(1).max(0);
    let future_slots = error_window.future_slots.max(0);

    // `future_slots` limits a request relative to its arrival slot, not the
    // scheduler's current slot. Use the latest arrival in the batch so this
    // remains safe when a client submits work for a future slot. The current
    // slot is also included because overdue requests may be assigned late now.
    let latest_arrival_slot = requests
        .iter()
        .map(|request| request.arrival_slot)
        .max()
        .unwrap_or(current_slot);
    let assignment_through_slot = current_slot
        .max(latest_arrival_slot.saturating_add(future_slots))
        .min(last_planning_slot);

    // A candidate at the latest assignment slot can still be constrained by
    // its future error window, so include those later slots in the aggregates.
    let error_through_slot = assignment_through_slot
        .saturating_add(future_slots)
        .min(last_planning_slot);

    // Both configured past ranges are non-negative policies. Saturating
    // arithmetic avoids overflowing slot indices at extreme configurations.
    let history_slots = error_window
        .past_slots
        .max(0)
        .saturating_add(error_window.past_decay_slots.max(0));
    let history_from_slot = current_slot.saturating_sub(history_slots);

    SolverSnapshotScope {
        profile_id: profile_id.clone(),
        current_slot,
        history_from_slot,
        assignment_through_slot,
        error_through_slot,
    }
}

impl SolverSnapshot {
    /// Weighted error stats for the window
    /// `[center − window_past, center + window_future]`.
    pub fn get_window_error_stats(
        &self,
        center_slot: i32,
        window_past: i32,
        window_future: i32,
    ) -> WindowErrorStats {
        let start = center_slot - window_past;
        let end = center_slot + window_future;
        let (error_sum, count) = sum_slot_error_totals(&self.global_slot_errors, start, end);
        let average = if count > 0 {
            error_sum / count as f64
        } else {
            0.0
        };
        WindowErrorStats {
            error_sum,
            count,
            average,
        }
    }

    /// Cumulative error stats.
    pub fn get_global_error_stats(&self) -> GlobalErrorStats {
        let count = self.global_assignment_count;
        let error_sum = self.global_error_sum;
        let avg = if count > 0 {
            error_sum / count as f64
        } else {
            0.0
        };
        GlobalErrorStats {
            error_sum,
            count,
            avg,
        }
    }

    /// Cumulative error stats for one QoS profile.
    pub fn get_profile_error_stats(&self, profile_id: &QosProfileId) -> GlobalErrorStats {
        self.profile_error_stats
            .get(profile_id)
            .cloned()
            .unwrap_or(GlobalErrorStats {
                error_sum: 0.0,
                count: 0,
                avg: 0.0,
            })
    }

    /// Weighted window stats for assignments owned by one profile only.
    pub fn get_profile_window_error_stats(
        &self,
        profile_id: &QosProfileId,
        center_slot: i32,
        window_past: i32,
        window_future: i32,
    ) -> WindowErrorStats {
        let start = center_slot - window_past;
        let end = center_slot + window_future;
        let (error_sum, count) = self
            .profile_slot_errors
            .get(profile_id)
            .map(|slots| sum_slot_error_totals(slots, start, end))
            .unwrap_or_default();
        WindowErrorStats {
            error_sum,
            count,
            average: if count > 0 {
                error_sum / count as f64
            } else {
                0.0
            },
        }
    }

    /// Compact per-slot error totals for one profile, ordered by slot.
    pub fn get_profile_error_totals(&self, profile_id: &QosProfileId) -> Vec<(i32, f64, u64)> {
        self.profile_slot_errors
            .get(profile_id)
            .into_iter()
            .flat_map(|slots| {
                slots
                    .iter()
                    .map(|(slot, totals)| (*slot, totals.error_sum, totals.count))
            })
            .collect()
    }

    /// Error totals for one profile in one slot.
    pub fn get_profile_slot_error_stats(
        &self,
        profile_id: &QosProfileId,
        slot: i32,
    ) -> SlotErrorStats {
        self.profile_slot_errors
            .get(profile_id)
            .and_then(|slots| slots.get(&slot))
            .copied()
            .map(SlotErrorStats::from)
            .unwrap_or_default()
    }
}

fn sum_slot_error_totals(
    slots: &BTreeMap<i32, SlotErrorTotals>,
    start: i32,
    end: i32,
) -> (f64, u64) {
    if start > end {
        return (0.0, 0);
    }
    slots
        .range(start..=end)
        .fold((0.0, 0), |(sum, count), (_, totals)| {
            (sum + totals.error_sum, count + totals.count)
        })
}

/// Weight for a slot at `distance` before the active past window.
pub(crate) fn linear_past_decay_weight(past_decay_slots: i32, distance: i32) -> f64 {
    if past_decay_slots <= 0 || distance <= 0 || distance > past_decay_slots {
        return 0.0;
    }
    (f64::from(past_decay_slots) - f64::from(distance) + 1.0) / (f64::from(past_decay_slots) + 1.0)
}

fn window_error_average_with_decayed_past(
    slots: &BTreeMap<i32, SlotErrorTotals>,
    center_slot: i32,
    window_past: i32,
    window_future: i32,
    past_decay_slots: i32,
) -> Option<f64> {
    let active_start = center_slot.saturating_sub(window_past.max(0));
    let active_end = center_slot.saturating_add(window_future.max(0));
    let (mut weighted_error_sum, count) = sum_slot_error_totals(slots, active_start, active_end);
    let mut weighted_count = count as f64;
    let past_decay_slots = past_decay_slots.max(0);

    if past_decay_slots > 0 {
        let decay_start = active_start.saturating_sub(past_decay_slots);
        let decay_end = active_start.saturating_sub(1);
        if decay_start <= decay_end {
            for (slot, totals) in slots.range(decay_start..=decay_end) {
                let distance = active_start.saturating_sub(*slot);
                let weight = linear_past_decay_weight(past_decay_slots, distance);
                weighted_error_sum += totals.error_sum * weight;
                weighted_count += totals.count as f64 * weight;
            }
        }
    }

    (weighted_count > 0.0).then_some(weighted_error_sum / weighted_count)
}

/// Requests claimed by a worker together with their atomic shared-state snapshot.
pub struct ClaimedRequestBatch {
    pub requests: Vec<Request>,
    pub profile_pending_count: usize,
    pub snapshot: SolverSnapshot,
}

// ─── inner (lock-guarded) data ────────────────────────────────────────────────

struct Inner {
    /// Requests waiting to be processed by a batch solver.
    pending: Vec<Request>,
    /// Virtual enqueue time for each pending request. This is distinct from
    /// `Request::arrival_time`, which is wall-clock/domain metadata and not a
    /// reliable timer for the scheduler's virtual clock.
    pending_since_virtual_ms: HashMap<u64, u64>,
    /// Active assignments (request_id → Assignment).
    assignments: HashMap<u64, Assignment>,
    /// Index - Assignment IDs grouped by slot and profile.
    assignment_ids_by_slot_profile: AssignmentIdsBySlotProfile,
    /// Current time slot (updated by the scheduler loop).
    current_slot: i32, // TODO: consider changing to i64 to match other slot representations...
    // Probably won't use 64 bit slots in practice...
    /// Cumulative statistics (never reset by archiving).
    total_received: u64,
    total_scheduled: u64,
    /// Descriptive fleet-wide error totals; never used as a cross-profile QoS limit.
    global_error_sum: f64,
    global_assignment_count: u64,
    /// Cumulative hard/window QoS totals partitioned by profile.
    profile_error_stats: HashMap<QosProfileId, GlobalErrorStats>,
    profile_slot_errors: HashMap<QosProfileId, BTreeMap<i32, SlotErrorTotals>>,
    global_slot_errors: BTreeMap<i32, SlotErrorTotals>,
}

impl Inner {
    fn record_assignment(&mut self, assignment: Assignment) {
        let previous = self.assignments.get(&assignment.request_id).cloned();
        let mut assignment = assignment;
        if let Some(previous) = previous {
            // A request's budget identity is immutable after its first
            // commitment; replanning changes its live assignment, not which
            // historical/profile aggregate it belongs to.
            assignment.qos_profile_id = previous.qos_profile_id.clone();
            self.unindex_assignment(&previous);
            self.remove_slot_error_totals(&previous);
            let error_delta = assignment.error - previous.error;
            self.global_error_sum += error_delta;
            let stats = self
                .profile_error_stats
                .entry(assignment.qos_profile_id.clone())
                .or_default();
            stats.error_sum += error_delta;
            stats.avg = if stats.count > 0 {
                stats.error_sum / stats.count as f64
            } else {
                0.0
            };
        } else {
            self.total_scheduled += 1;
            self.global_error_sum += assignment.error;
            self.global_assignment_count += 1;
            let stats = self
                .profile_error_stats
                .entry(assignment.qos_profile_id.clone())
                .or_default();
            stats.error_sum += assignment.error;
            stats.count += 1;
        }
        let profile_id = assignment.qos_profile_id.clone();
        let stats = self
            .profile_error_stats
            .get_mut(&profile_id)
            .expect("record_assignment initializes profile error totals");
        stats.avg = if stats.count > 0 {
            stats.error_sum / stats.count as f64
        } else {
            0.0
        };
        self.add_slot_error_totals(&assignment);
        self.index_assignment(&assignment);
        self.assignments.insert(assignment.request_id, assignment);
    }

    fn add_slot_error_totals(&mut self, assignment: &Assignment) {
        let global = self
            .global_slot_errors
            .entry(assignment.scheduled_slot)
            .or_default();
        global.error_sum += assignment.error;
        global.count += 1;

        let profile = self
            .profile_slot_errors
            .entry(assignment.qos_profile_id.clone())
            .or_default()
            .entry(assignment.scheduled_slot)
            .or_default();
        profile.error_sum += assignment.error;
        profile.count += 1;
    }

    fn remove_slot_error_totals(&mut self, assignment: &Assignment) {
        if let Some(totals) = self.global_slot_errors.get_mut(&assignment.scheduled_slot) {
            totals.error_sum -= assignment.error;
            totals.count -= 1;
            if totals.count == 0 {
                self.global_slot_errors.remove(&assignment.scheduled_slot);
            }
        }

        let mut remove_profile = false;
        if let Some(slots) = self.profile_slot_errors.get_mut(&assignment.qos_profile_id) {
            if let Some(totals) = slots.get_mut(&assignment.scheduled_slot) {
                totals.error_sum -= assignment.error;
                totals.count -= 1;
                if totals.count == 0 {
                    slots.remove(&assignment.scheduled_slot);
                }
            }
            remove_profile = slots.is_empty();
        }
        if remove_profile {
            self.profile_slot_errors.remove(&assignment.qos_profile_id);
        }
    }

    fn correct_slot_error(&mut self, profile_id: &QosProfileId, slot: i32, delta: f64) {
        if let Some(totals) = self.global_slot_errors.get_mut(&slot) {
            totals.error_sum += delta;
        }
        if let Some(totals) = self
            .profile_slot_errors
            .get_mut(profile_id)
            .and_then(|slots| slots.get_mut(&slot))
        {
            totals.error_sum += delta;
        }
    }

    fn assignment_count_in_slot(&self, slot: i32) -> Result<i64, String> {
        let Some(by_profile) = self.assignment_ids_by_slot_profile.get(&slot) else {
            return Ok(0);
        };
        by_profile.values().try_fold(0i64, |total, request_ids| {
            let profile_count = i64::try_from(request_ids.len())
                .map_err(|_| format!("slot {slot} occupancy exceeds i64"))?;
            total
                .checked_add(profile_count)
                .ok_or_else(|| format!("slot {slot} occupancy exceeds i64"))
        })
    }

    fn assignment_counts_in_range(
        &self,
        from_slot: i32,
        through_slot: i32,
    ) -> Result<HashMap<i32, i64>, String> {
        let slots: Vec<_> = self
            .assignment_ids_by_slot_profile
            .range(from_slot..=through_slot)
            .map(|(slot, _)| *slot)
            .collect();
        slots
            .into_iter()
            .map(|slot| {
                self.assignment_count_in_slot(slot)
                    .map(|count| (slot, count))
            })
            .collect()
    }

    fn index_assignment(&mut self, assignment: &Assignment) {
        self.assignment_ids_by_slot_profile
            .entry(assignment.scheduled_slot)
            .or_default()
            .entry(assignment.qos_profile_id.clone())
            .or_default()
            .insert(assignment.request_id);
    }

    fn unindex_assignment(&mut self, assignment: &Assignment) {
        let slot = assignment.scheduled_slot;
        let slot_is_empty = match self.assignment_ids_by_slot_profile.get_mut(&slot) {
            Some(by_profile) => {
                let profile_is_empty = match by_profile.get_mut(&assignment.qos_profile_id) {
                    Some(ids) => {
                        let removed = ids.remove(&assignment.request_id);
                        debug_assert!(removed, "assignment index was missing a live request");
                        ids.is_empty()
                    }
                    None => false,
                };
                if profile_is_empty {
                    by_profile.remove(&assignment.qos_profile_id);
                }
                by_profile.is_empty()
            }
            None => false,
        };
        if slot_is_empty {
            self.assignment_ids_by_slot_profile.remove(&slot);
        }
    }
}

impl Default for GlobalErrorStats {
    fn default() -> Self {
        Self {
            error_sum: 0.0,
            count: 0,
            avg: 0.0,
        }
    }
}

impl Inner {
    fn new() -> Self {
        Self {
            pending: Vec::new(),
            pending_since_virtual_ms: HashMap::new(),
            assignments: HashMap::new(),
            assignment_ids_by_slot_profile: BTreeMap::new(),
            current_slot: 0,
            total_received: 0,
            total_scheduled: 0,
            global_error_sum: 0.0,
            global_assignment_count: 0,
            profile_error_stats: HashMap::new(),
            profile_slot_errors: HashMap::new(),
            global_slot_errors: BTreeMap::new(),
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
    /// One live, global capacity-tier ladder shared by the service and every
    /// batch worker. `None` only exists before a scheduler/service constructor
    /// installs the Config defaults.
    capacity_tiers: Arc<RwLock<Option<Vec<CapacityTier>>>>,
    /// Monotonic marker used to invalidate cost-learned online solver state
    /// after an administrative tier replacement.
    capacity_tier_revision: Arc<AtomicU64>,
    /// Virtual elapsed milliseconds from the engine's slot-zero boundary.
    /// Realtime startup seeds the active-slot fraction; manual emulation starts
    /// at zero. Written by the scheduler loop and manual advance handler.
    pub virtual_elapsed_ms: Arc<AtomicU64>,
    /// Last slot index fully processed by the generator.
    /// Used by the scheduler's skip-empty-slots logic to avoid skipping a slot
    /// before the generator has had a chance to add its requests.
    generator_processed_slot: Arc<AtomicI32>,
    /// Highest slot guaranteed not to receive any later solver commits.
    /// Published by `BatchScheduler` after accounting for active batch slots.
    settled_through_slot: Arc<AtomicI32>,
    /// Slot whose client submissions have closed and whose partial batch must
    /// be flushed without advancing the manual clock.
    flush_requested_slot: Arc<AtomicI32>,
}

impl SharedState {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(Inner::new())),
            capacity_tiers: Arc::new(RwLock::new(None)),
            capacity_tier_revision: Arc::new(AtomicU64::new(0)),
            virtual_elapsed_ms: Arc::new(AtomicU64::new(0)),
            generator_processed_slot: Arc::new(AtomicI32::new(-1)),
            settled_through_slot: Arc::new(AtomicI32::new(-1)),
            flush_requested_slot: Arc::new(AtomicI32::new(-1)),
        }
    }

    /// Install Config's initial global ladder unless another constructor
    /// already initialized it. Scheduler and service setup both call this;
    /// the second call must not overwrite a runtime replacement.
    pub fn initialize_capacity_tiers(&self, tiers: Vec<CapacityTier>) {
        let mut current = self.capacity_tiers.write().unwrap();
        if current.is_none() {
            *current = Some(tiers);
        }
    }

    /// Replace the global ladder for future baselines and scheduling work.
    ///
    /// Active batch workers hold a read guard through solve and commit, so this
    /// write waits for them to finish; a batch cannot be solved with one ladder
    /// and priced with another. Existing committed assignment costs are stored
    /// values and are intentionally not repriced here.
    pub fn replace_capacity_tiers(&self, tiers: Vec<CapacityTier>) {
        let mut current = self.capacity_tiers.write().unwrap();
        *current = Some(tiers);
        self.capacity_tier_revision.fetch_add(1, Ordering::Release);
    }

    /// Borrow the runtime ladder. A solver keeps this guard until its commit
    /// finishes so an administrative replacement cannot change the pricing
    /// policy mid-transaction.
    pub fn capacity_tiers_guard(&self) -> RwLockReadGuard<'_, Option<Vec<CapacityTier>>> {
        self.capacity_tiers.read().unwrap()
    }

    /// Return an owned point-in-time copy for short service calculations and
    /// API responses that must not hold the runtime lock while serializing.
    pub fn capacity_tiers_snapshot(&self) -> Vec<CapacityTier> {
        self.capacity_tiers
            .read()
            .unwrap()
            .as_ref()
            .cloned()
            .unwrap_or_default()
    }

    /// Version of the global tier policy used to discard online solver
    /// estimates learned under a previous carbon-cost curve.
    #[inline]
    pub fn capacity_tier_revision(&self) -> u64 {
        self.capacity_tier_revision.load(Ordering::Acquire)
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

    /// Request the scheduler loop to flush partial batches for `slot`.
    ///
    /// The request is separate from advancing virtual time so manual-clock
    /// emulation can finish scheduling the active slot before closing it.
    #[inline]
    pub fn request_pending_flush(&self, slot: i32) {
        self.flush_requested_slot.store(slot, Ordering::Release);
    }

    /// Whether the scheduler loop has a close-slot flush pending for `slot`.
    #[inline]
    pub fn pending_flush_requested(&self, slot: i32) -> bool {
        self.flush_requested_slot.load(Ordering::Acquire) == slot
    }

    /// Clear a completed flush request without disturbing a newer slot's
    /// request.
    #[inline]
    pub fn clear_pending_flush(&self, slot: i32) {
        let _ = self.flush_requested_slot.compare_exchange(
            slot,
            -1,
            Ordering::AcqRel,
            Ordering::Acquire,
        );
    }

    // ── request queue ─────────────────────────────────────────────────────

    /// Append a new request to the pending queue.
    pub fn add_request(&self, request: Request) {
        let mut g = self.inner.lock().unwrap();
        let enqueued_at_ms = self.virtual_elapsed_ms.load(Ordering::Acquire);
        g.pending_since_virtual_ms
            .entry(request.id)
            .or_insert(enqueued_at_ms);
        g.pending.push(request);
        g.total_received += 1;
    }

    /// Append multiple requests to the pending queue.
    pub fn add_requests(&self, requests: Vec<Request>) {
        let mut g = self.inner.lock().unwrap();
        let enqueued_at_ms = self.virtual_elapsed_ms.load(Ordering::Acquire);
        g.total_received += requests.len() as u64;
        for request in &requests {
            g.pending_since_virtual_ms
                .entry(request.id)
                .or_insert(enqueued_at_ms);
        }
        g.pending.extend(requests);
    }

    /// Atomically claim (remove) the first `count` pending requests.
    ///
    /// Returns fewer than `count` elements if the queue is shorter.
    pub fn claim_pending_requests(&self, count: usize) -> Vec<Request> {
        let mut g = self.inner.lock().unwrap();
        let n = count.min(g.pending.len());
        g.pending.drain(..n).collect()
    }

    /// Pending count per stable profile ID, sorted for deterministic fair dispatch.
    pub fn pending_profile_counts(&self) -> Vec<(QosProfileId, usize)> {
        let g = self.inner.lock().unwrap();
        let mut counts: HashMap<QosProfileId, usize> = HashMap::new();
        for request in &g.pending {
            *counts.entry(request.qos_profile_id()).or_default() += 1;
        }
        let mut counts: Vec<_> = counts.into_iter().collect();
        counts.sort_by(|left, right| left.0.cmp(&right.0));
        counts
    }

    /// Atomically claim at most `count` requests from one profile, preserving
    /// queue order within that profile and leaving other profiles untouched.
    pub fn claim_pending_requests_for_profile(
        &self,
        profile_id: &QosProfileId,
        count: usize,
    ) -> Vec<Request> {
        let mut g = self.inner.lock().unwrap();
        let mut claimed = Vec::with_capacity(count.min(g.pending.len()));
        let mut remaining = Vec::with_capacity(g.pending.len());
        for request in g.pending.drain(..) {
            if request.qos_profile_id() == *profile_id && claimed.len() < count {
                claimed.push(request);
            } else {
                remaining.push(request);
            }
        }
        g.pending = remaining;
        claimed
    }

    /// Atomically claim one profile-homogeneous batch and capture the exact
    /// committed-state snapshot that its worker must use.
    pub fn claim_pending_requests_for_profile_with_snapshot(
        &self,
        profile_id: &QosProfileId,
        count: usize,
        total_slots: i32,
        default_error_window: &ErrorWindowPolicy,
    ) -> Result<ClaimedRequestBatch, String> {
        let mut g = self.inner.lock().unwrap();
        let profile_pending_count = g
            .pending
            .iter()
            .filter(|request| request.qos_profile_id() == *profile_id)
            .count();
        let mut requests = Vec::with_capacity(count.min(profile_pending_count));
        let mut remaining = Vec::with_capacity(g.pending.len());
        for request in g.pending.drain(..) {
            if request.qos_profile_id() == *profile_id && requests.len() < count {
                requests.push(request);
            } else {
                remaining.push(request);
            }
        }
        g.pending = remaining;

        // Requests in this batch share a profile ID. Prefer the immutable
        // policy captured on the request; unprofiled simulation requests use
        // the default policy supplied by the scheduler.
        let error_window = requests
            .first()
            .and_then(|request| request.qos_profile.as_ref())
            .map(|profile| &profile.error_window)
            .unwrap_or(default_error_window);
        let scope = solver_snapshot_scope(
            profile_id,
            &requests,
            g.current_slot,
            total_slots,
            error_window,
        );

        // Capacity pricing needs only global occupancy counts. Sum the
        // per-profile ID-bucket lengths so the snapshot contains one count
        // per occupied slot instead of cloning every Assignment row.
        let baseline_slot_counts = if scope.current_slot <= scope.assignment_through_slot {
            g.assignment_counts_in_range(scope.current_slot, scope.assignment_through_slot)
        } else {
            Ok(HashMap::new())
        };
        let baseline_slot_counts = match baseline_slot_counts {
            Ok(counts) => counts,
            Err(error) => {
                // Snapshot construction failed after the claim. Restore the
                // claimed requests before surfacing the explicit error.
                requests.extend(g.pending.drain(..));
                g.pending = requests;
                return Err(error);
            }
        };

        // Initialize profile slot errors map
        let mut profile_slot_errors = HashMap::new();
        if scope.history_from_slot <= scope.error_through_slot {
            if let Some(slots) = g.profile_slot_errors.get(&scope.profile_id) {
                // Error history is profile-specific and compact, so copy its
                // per-slot sums/counts rather than historical Assignment rows.
                let selected_slots = slots
                    .range(scope.history_from_slot..=scope.error_through_slot)
                    .map(|(slot, totals)| (*slot, *totals))
                    .collect();
                profile_slot_errors.insert(scope.profile_id.clone(), selected_slots);
            }
        }

        // Initialize global slot errors map
        let global_slot_errors = if scope.history_from_slot <= scope.error_through_slot {
            g.global_slot_errors
                .range(scope.history_from_slot..=scope.error_through_slot)
                .map(|(slot, totals)| (*slot, *totals))
                .collect()
        } else {
            BTreeMap::new()
        };

        let snapshot = SolverSnapshot {
            baseline_slot_counts,
            global_error_sum: g.global_error_sum,
            global_assignment_count: g.global_assignment_count,
            profile_error_stats: g.profile_error_stats.clone(),
            profile_slot_errors,
            global_slot_errors,
        };
        Ok(ClaimedRequestBatch {
            requests,
            profile_pending_count,
            snapshot,
        })
    }

    /// Return `requests` to the front of the pending queue (preserving order).
    ///
    /// Used when a claimed batch could not be scheduled and must be retried.
    pub fn requeue_pending_requests_front(&self, mut requests: Vec<Request>) {
        if requests.is_empty() {
            return;
        }
        let mut g = self.inner.lock().unwrap();
        let enqueued_at_ms = self.virtual_elapsed_ms.load(Ordering::Acquire);
        for request in &requests {
            g.pending_since_virtual_ms
                .entry(request.id)
                .or_insert(enqueued_at_ms);
        }
        // Prepend: append existing queue to the end of `requests`, then replace.
        requests.extend(g.pending.drain(..));
        g.pending = requests;
    }

    /// Number of pending requests currently in the queue.
    pub fn get_pending_count(&self) -> usize {
        self.inner.lock().unwrap().pending.len()
    }

    /// Return profiles with an aged partial batch.
    ///
    /// Timeout age starts when the request enters the scheduler queue, not at
    /// its domain `arrival_time`: that value may use wall-clock epoch seconds,
    /// while this timer follows the scheduler's virtual clock.
    pub fn timed_out_partial_profiles(
        &self,
        virtual_ms: u64,
        batch_size: usize,
        timeout_ms: u64,
    ) -> HashSet<QosProfileId> {
        if batch_size == 0 || timeout_ms == 0 {
            return HashSet::new();
        }

        let g = self.inner.lock().unwrap();
        let mut profile_ages: HashMap<QosProfileId, (usize, u64)> = HashMap::new();
        for request in &g.pending {
            let enqueued_at_ms = g
                .pending_since_virtual_ms
                .get(&request.id)
                .copied()
                .unwrap_or(virtual_ms);
            let age = profile_ages
                .entry(request.qos_profile_id())
                .or_insert((0, enqueued_at_ms));
            age.0 += 1;
            age.1 = age.1.min(enqueued_at_ms);
        }
        profile_ages
            .into_iter()
            .filter_map(|(profile_id, (count, oldest_enqueued_at_ms))| {
                (count < batch_size
                    && virtual_ms.saturating_sub(oldest_enqueued_at_ms) >= timeout_ms)
                    .then_some(profile_id)
            })
            .collect()
    }

    /// Remove and return all pending requests (used after the run ends to
    /// collect unprocessed requests for late scheduling).
    pub fn drain_pending_requests(&self) -> Vec<Request> {
        let mut g = self.inner.lock().unwrap();
        let drained: Vec<_> = g.pending.drain(..).collect();
        for request in &drained {
            g.pending_since_virtual_ms.remove(&request.id);
        }
        drained
    }

    /// Record a batch of scheduling decisions.
    ///
    /// - Overwrites any existing assignment for the same request_id.
    /// - Increments global error totals only for *new* request ids (not
    ///   re-planned ones, to avoid double-counting).
    pub fn add_assignments(&self, assignments: Vec<Assignment>) {
        let mut g = self.inner.lock().unwrap();
        for assignment in assignments {
            g.record_assignment(assignment);
        }
    }

    /// Snapshot of all active assignments.
    pub fn get_current_assignments(&self) -> HashMap<u64, Assignment> {
        self.inner.lock().unwrap().assignments.clone()
    }

    /// Get assignment by ID.
    pub fn get_assignment(&self, id: u64) -> Option<Assignment> {
        self.inner.lock().unwrap().assignments.get(&id).cloned()
    }

    /// Get assignments for only the requested IDs.
    pub fn get_assignments_for_ids(&self, request_ids: &HashSet<u64>) -> HashMap<u64, Assignment> {
        let guard = self.inner.lock().unwrap();
        request_ids
            .iter()
            .filter_map(|request_id| {
                guard
                    .assignments
                    .get(request_id)
                    .map(|assignment| (*request_id, assignment.clone()))
            })
            .collect()
    }

    /// Return the subset of the supplied IDs that currently have assignments.
    pub fn get_assigned_request_ids(&self, request_ids: &HashSet<u64>) -> HashSet<u64> {
        let guard = self.inner.lock().unwrap();
        request_ids
            .iter()
            .filter(|request_id| guard.assignments.contains_key(*request_id))
            .copied()
            .collect()
    }

    /// Get assignments due by `current_slot`, using the slot index to avoid
    /// cloning future assignments that are not relevant to the barrier.
    pub fn get_assignments_due_by(&self, current_slot: i32) -> Vec<Assignment> {
        let guard = self.inner.lock().unwrap();
        guard
            .assignment_ids_by_slot_profile
            .range(..=current_slot)
            .flat_map(|(_, by_profile)| by_profile.values())
            .flat_map(|request_ids| request_ids.iter())
            .filter_map(|request_id| guard.assignments.get(request_id))
            .cloned()
            .collect()
    }

    /// Get assignments within an optional slot range and profile filter.
    ///
    /// Slot and profile filters are applied to the index before assignment
    /// rows are cloned. Flavour filtering remains a caller-side filter because
    /// the index is intentionally keyed only by slot and QoS profile.
    pub fn get_assignments_in_range(
        &self,
        from_slot: Option<i32>,
        to_slot: Option<i32>,
        profile_id: Option<&QosProfileId>,
    ) -> Vec<Assignment> {
        let start = from_slot.unwrap_or(i32::MIN);
        let end = to_slot.unwrap_or(i32::MAX);
        if start > end {
            return Vec::new();
        }

        let guard = self.inner.lock().unwrap();
        let mut assignments = Vec::new();
        for (_, by_profile) in guard.assignment_ids_by_slot_profile.range(start..=end) {
            if let Some(profile_id) = profile_id {
                if let Some(request_ids) = by_profile.get(profile_id) {
                    assignments.extend(
                        request_ids
                            .iter()
                            .filter_map(|request_id| guard.assignments.get(request_id).cloned()),
                    );
                }
            } else {
                for request_ids in by_profile.values() {
                    assignments.extend(
                        request_ids
                            .iter()
                            .filter_map(|request_id| guard.assignments.get(request_id).cloned()),
                    );
                }
            }
        }
        assignments
    }

    /// Sum committed assignment costs without cloning assignment rows.
    pub fn total_assignment_carbon_cost(&self, profile_id: Option<&QosProfileId>) -> f64 {
        self.inner
            .lock()
            .unwrap()
            .assignments
            .values()
            .filter(|assignment| {
                profile_id.map_or(true, |profile_id| assignment.qos_profile_id == *profile_id)
            })
            .map(|assignment| assignment.carbon_cost)
            .sum()
    }

    /// Carbon costs for selected request IDs, without cloning full assignments.
    pub fn assignment_carbon_costs_for_ids(&self, request_ids: &HashSet<u64>) -> HashMap<u64, f64> {
        let guard = self.inner.lock().unwrap();
        request_ids
            .iter()
            .filter_map(|request_id| {
                guard
                    .assignments
                    .get(request_id)
                    .map(|assignment| (*request_id, assignment.carbon_cost))
            })
            .collect()
    }

    /// IDs of QoS profiles with at least one current assignment.
    pub fn get_profiles(&self) -> HashSet<QosProfileId> {
        self.inner
            .lock()
            .unwrap()
            .assignment_ids_by_slot_profile
            .values()
            .flat_map(|m| m.keys())
            .cloned()
            .collect()
    }

    /// Return assignments among the requested IDs that are due by this slot.
    pub fn assignments_for_ids_due_by(
        &self,
        request_ids: &HashSet<u64>,
        current_slot: i32,
    ) -> Vec<Assignment> {
        let guard = self.inner.lock().unwrap();
        request_ids
            .iter()
            .filter_map(|request_id| guard.assignments.get(request_id))
            .filter(|assignment| assignment.scheduled_slot <= current_slot)
            .cloned()
            .collect()
    }
    /// Capture all data needed by `solve_dp` in a **single** lock acquisition.
    ///
    /// Call this once at the start of each solver invocation to get a
    /// consistent view of the shared state.  The lock is not held after this
    /// method returns.
    pub fn snapshot_for_solver(&self) -> SolverSnapshot {
        let g = self.inner.lock().unwrap();
        let baseline_slot_counts = g
            .assignment_counts_in_range(i32::MIN, i32::MAX)
            .expect("slot occupancy must fit the scheduler count type");
        SolverSnapshot {
            baseline_slot_counts,
            global_error_sum: g.global_error_sum,
            global_assignment_count: g.global_assignment_count,
            profile_error_stats: g.profile_error_stats.clone(),
            profile_slot_errors: g.profile_slot_errors.clone(),
            global_slot_errors: g.global_slot_errors.clone(),
        }
    }

    /// All assignments whose `scheduled_slot >= current_slot`.
    pub fn get_future_assignments(&self, current_slot: i32) -> Vec<Assignment> {
        let guard = self.inner.lock().unwrap();
        guard
            .assignment_ids_by_slot_profile
            .range(current_slot..)
            .flat_map(|(_, by_profile)| by_profile.values())
            .flat_map(|request_ids| request_ids.iter())
            .filter_map(|request_id| guard.assignments.get(request_id))
            .cloned()
            .collect()
    }

    /// Highest slot that is known to be settled across every solver worker.
    ///
    /// This is distinct from `current_slot`: a worker may still be solving a
    /// batch claimed at an earlier slot and commit an assignment into it.
    pub fn get_settled_through_slot(&self) -> i32 {
        self.settled_through_slot.load(Ordering::Acquire)
    }

    /// Publish a settled-slot watermark without allowing it to move backwards.
    ///
    /// The scheduler computes the value from its per-slot active-batch counts.
    /// Manual-clock close-slot barriers may also advance it after they have
    /// drained scheduling, dispatch, and callbacks for the closed slot.
    pub fn advance_settled_through_slot(&self, slot: i32) {
        self.settled_through_slot.fetch_max(slot, Ordering::AcqRel);
    }

    /// Evict an assignment if its slot is settled.
    /// Returns the assignment if removed.
    /// Needed to free up memory and archive old stuff
    pub fn evict_settled_assignment(
        &self,
        request_id: u64,
        settled_through_slot: i32,
    ) -> Result<Option<Assignment>, String> {
        let mut inner = self.inner.lock().unwrap();

        let Some(assignment) = inner.assignments.get(&request_id).cloned() else {
            return Ok(None); // Already evicted; safe for a retry.
        };

        if assignment.scheduled_slot > settled_through_slot {
            return Err(format!(
                "request {request_id} is scheduled in unsettled slot {}",
                assignment.scheduled_slot
            ));
        }

        inner.unindex_assignment(&assignment);
        inner.assignments.remove(&request_id);

        // Keep global/profile cumulative and per-slot error totals: they represent
        // history and must continue to constrain QoS after row eviction.
        Ok(Some(assignment))
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
        let (error_sum, count) = if exclude.is_empty() {
            sum_slot_error_totals(&g.global_slot_errors, start, end)
        } else {
            g.assignments
                .values()
                .filter(|assignment| {
                    !exclude.contains(&assignment.request_id)
                        && (start..=end).contains(&assignment.scheduled_slot)
                })
                .fold((0.0, 0), |(sum, count), assignment| {
                    (sum + assignment.error, count + 1)
                })
        };
        let average = if count > 0 {
            error_sum / count as f64
        } else {
            0.0
        };
        WindowErrorStats {
            error_sum,
            count,
            average,
        }
    }

    /// Weighted window statistics restricted to assignments for one profile.
    pub fn get_profile_window_error_stats(
        &self,
        profile_id: &QosProfileId,
        center_slot: i32,
        window_past: i32,
        window_future: i32,
        exclude: &HashSet<u64>,
    ) -> WindowErrorStats {
        let g = self.inner.lock().unwrap();
        let start = center_slot - window_past;
        let end = center_slot + window_future;
        let (error_sum, count) = if exclude.is_empty() {
            g.profile_slot_errors
                .get(profile_id)
                .map(|slots| sum_slot_error_totals(slots, start, end))
                .unwrap_or_default()
        } else {
            g.assignments
                .values()
                .filter(|assignment| {
                    assignment.qos_profile_id == *profile_id
                        && !exclude.contains(&assignment.request_id)
                        && (start..=end).contains(&assignment.scheduled_slot)
                })
                .fold((0.0, 0), |(sum, count), assignment| {
                    (sum + assignment.error, count + 1)
                })
        };
        WindowErrorStats {
            error_sum,
            count,
            average: if count > 0 {
                error_sum / count as f64
            } else {
                0.0
            },
        }
    }

    /// Window error average including linearly decayed observations before the
    /// configured past window, matching the scheduler's QoS baseline policy.
    pub fn get_window_error_average_with_decay(
        &self,
        center_slot: i32,
        window_past: i32,
        window_future: i32,
        past_decay_slots: i32,
    ) -> Option<f64> {
        let g = self.inner.lock().unwrap();
        window_error_average_with_decayed_past(
            &g.global_slot_errors,
            center_slot,
            window_past,
            window_future,
            past_decay_slots,
        )
    }

    /// Profile-scoped window average including the policy's decayed past.
    pub fn get_profile_window_error_average_with_decay(
        &self,
        profile_id: &QosProfileId,
        center_slot: i32,
        window_past: i32,
        window_future: i32,
        past_decay_slots: i32,
    ) -> Option<f64> {
        let g = self.inner.lock().unwrap();
        let slots = g.profile_slot_errors.get(profile_id)?;
        window_error_average_with_decayed_past(
            slots,
            center_slot,
            window_past,
            window_future,
            past_decay_slots,
        )
    }

    /// Assignment error stats for one slot and QoS profile.
    pub fn get_profile_slot_error_stats(
        &self,
        profile_id: &QosProfileId,
        slot: i32,
    ) -> SlotErrorStats {
        let g = self.inner.lock().unwrap();
        g.profile_slot_errors
            .get(profile_id)
            .and_then(|slots| slots.get(&slot))
            .copied()
            .map(SlotErrorStats::from)
            .unwrap_or_default()
    }

    /// Cumulative error stats across all ever-assigned requests.
    pub fn get_global_error_stats(&self) -> GlobalErrorStats {
        let g = self.inner.lock().unwrap();
        let count = g.global_assignment_count;
        let error_sum = g.global_error_sum;
        let avg = if count > 0 {
            error_sum / count as f64
        } else {
            0.0
        };
        GlobalErrorStats {
            error_sum,
            count,
            avg,
        }
    }

    /// Cumulative error statistics restricted to one QoS profile.
    pub fn get_profile_error_stats(&self, profile_id: &QosProfileId) -> GlobalErrorStats {
        self.inner
            .lock()
            .unwrap()
            .profile_error_stats
            .get(profile_id)
            .cloned()
            .unwrap_or(GlobalErrorStats {
                error_sum: 0.0,
                count: 0,
                avg: 0.0,
            })
    }

    /// Replaces a committed assignment's *predicted* error with the
    /// *actual* error measured once the request was really executed (e.g.
    /// derived from the executor's `quality_score` in an emulation/real
    /// deployment — never available in offline simulation, which is why
    /// this is only ever called from the executor-callback handler).
    ///
    /// Updates both bookkeeping mechanisms consistently:
    /// - the per-slot error totals used by window-history queries are adjusted
    ///   by the same delta;
    /// - the global running sum (`global_error_sum`), which is *not*
    ///   recomputed live, is adjusted by the delta (subtract the old
    ///   predicted contribution, add the new actual one).
    ///
    /// No-op if `request_id` has no committed assignment (e.g. it was never
    /// scheduled, or was already archived).
    pub fn correct_assignment_error(&self, request_id: u64, actual_error: f64) {
        let mut g = self.inner.lock().unwrap();
        let Some((profile_id, scheduled_slot, delta)) =
            g.assignments.get_mut(&request_id).map(|assignment| {
                let delta = actual_error - assignment.error;
                assignment.error = actual_error;
                (
                    assignment.qos_profile_id.clone(),
                    assignment.scheduled_slot,
                    delta,
                )
            })
        else {
            return;
        };
        g.global_error_sum += delta;
        g.correct_slot_error(&profile_id, scheduled_slot, delta);
        if let Some(stats) = g.profile_error_stats.get_mut(&profile_id) {
            stats.error_sum += delta;
            stats.avg = if stats.count > 0 {
                stats.error_sum / stats.count as f64
            } else {
                0.0
            };
        }
    }

    /// Returns the predicted error for a given assignment
    pub fn get_error_for_assignment(&self, request_id: u64) -> Option<f64> {
        let g = self.inner.lock().unwrap();
        g.assignments.get(&request_id).map(|a| a.error)
    }

    /// Replaces a committed assignment's *predicted* `carbon_cost` (forecast
    /// carbon intensity × nominal duration) with the *actual* one, once the
    /// real carbon intensity for its slot is known (see
    /// `AppState::carbon_intensity_ratio` / `handlers::executor_callback`).
    /// Returns the new value (so the caller can forward it to the client)
    /// or `None` if there's no committed assignment for `request_id`.
    pub fn correct_assignment_carbon_cost(
        &self,
        request_id: u64,
        actual_carbon_cost: f64,
    ) -> Option<f64> {
        let mut g = self.inner.lock().unwrap();
        let a = g.assignments.get_mut(&request_id)?;
        a.carbon_cost = actual_carbon_cost;
        Some(actual_carbon_cost)
    }

    /// Returns the error statistics for a given slot, including the number of assignments and the average error.
    /// TODO: write tests
    pub fn get_slot_error_stats(&self, slot: i32) -> SlotErrorStats {
        let g = self.inner.lock().unwrap();
        g.global_slot_errors
            .get(&slot)
            .copied()
            .map(SlotErrorStats::from)
            .unwrap_or_default()
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
        let g = self.inner.lock().unwrap();
        g.assignment_ids_by_slot_profile
            .get(&slot)
            .into_iter()
            .flat_map(|by_profile| by_profile.values())
            .flat_map(|request_ids| request_ids.iter())
            .filter_map(|id| g.assignments.get(id))
            .cloned()
            .collect()
    }

    /// Price and commit a solver proposal against current global occupancy.
    ///
    /// The proposal is deliberately cost-free. Each placement is priced at
    /// its actual next position in the slot while holding the assignment
    /// mutex, then the whole materialized batch is committed before releasing
    /// the lock. A proposal may have been solved from an older snapshot; it is
    /// accepted as-is rather than re-solved or rejected for intervening QoS
    /// changes.
    pub fn materialize_and_commit_proposal(
        &self,
        proposal: &SolverProposal,
        pending_requests: &[Request],
        profile_id: &QosProfileId,
        current_slot: i32,
        flavours: &[Flavour],
        carbon_forecast: &[f64],
        tiers: &[CapacityTier],
        carbon_cost_duration_scale: f64,
    ) -> Result<Vec<Assignment>, String> {
        let mut g = self.inner.lock().unwrap();
        let pending_by_id: HashMap<u64, &Request> = pending_requests
            .iter()
            .map(|request| (request.id, request))
            .collect();
        if pending_by_id.len() != pending_requests.len() {
            return Err("claimed batch contains duplicate request IDs".to_string());
        }

        let mut seen_request_ids = HashSet::with_capacity(proposal.placements.len());
        let mut affected_slots =
            HashSet::with_capacity(proposal.placements.len().saturating_mul(2));
        for placement in &proposal.placements {
            affected_slots.insert(placement.scheduled_slot);
            if let Some(previous) = g.assignments.get(&placement.req_id) {
                affected_slots.insert(previous.scheduled_slot);
            }
        }
        let mut slot_counts = HashMap::with_capacity(affected_slots.len());
        for slot in affected_slots {
            slot_counts.insert(slot, g.assignment_count_in_slot(slot)?);
        }
        let mut materialized = Vec::with_capacity(proposal.placements.len());

        for placement in &proposal.placements {
            if !seen_request_ids.insert(placement.req_id) {
                return Err(format!(
                    "solver proposal contains request {} more than once",
                    placement.req_id
                ));
            }
            if placement.scheduled_slot < 0 {
                return Err(format!(
                    "solver proposal placed request {} in negative slot {}",
                    placement.req_id, placement.scheduled_slot
                ));
            }
            let slot_index = usize::try_from(placement.scheduled_slot)
                .map_err(|_| "solver proposal has an invalid slot index".to_string())?;
            let carbon_intensity = carbon_forecast.get(slot_index).copied().ok_or_else(|| {
                format!(
                    "solver proposal placed request {} outside the carbon forecast",
                    placement.req_id
                )
            })?;

            let request = pending_by_id.get(&placement.req_id).ok_or_else(|| {
                format!(
                    "solver proposal references unclaimed request {}",
                    placement.req_id
                )
            })?;
            let arrival_slot = request.arrival_slot;
            let deadline_slot = request.deadline_slot;
            let request_profile_id = request.qos_profile_id();
            if request_profile_id != *profile_id {
                return Err(format!(
                    "solver proposal request {} belongs to profile {}, not {}",
                    placement.req_id, request_profile_id, profile_id
                ));
            }
            if placement.late {
                if placement.scheduled_slot != current_slot
                    || placement.scheduled_slot <= deadline_slot
                {
                    return Err(format!(
                        "solver proposal marked request {} late at a non-late slot",
                        placement.req_id
                    ));
                }
            } else {
                let earliest_slot = arrival_slot.max(current_slot);
                if placement.scheduled_slot < earliest_slot
                    || placement.scheduled_slot > deadline_slot
                {
                    return Err(format!(
                        "solver proposal placed request {} outside its feasible slot window",
                        placement.req_id
                    ));
                }
            }

            let flavour = flavours
                .iter()
                .find(|flavour| flavour.name == placement.flavour_name)
                .ok_or_else(|| {
                    format!(
                        "solver proposal selected unknown flavour '{}' for request {}",
                        placement.flavour_name, placement.req_id
                    )
                })?;
            if let Some(previous) = g.assignments.get(&placement.req_id) {
                if previous.qos_profile_id != *profile_id {
                    return Err(format!(
                        "request {} is already committed to another QoS profile",
                        placement.req_id
                    ));
                }
                let previous_count = slot_counts.entry(previous.scheduled_slot).or_default();
                *previous_count = previous_count.checked_sub(1).ok_or_else(|| {
                    format!(
                        "slot {} occupancy underflow while replacing request {}",
                        previous.scheduled_slot, placement.req_id
                    )
                })?;
            }

            let next_position = slot_counts
                .get(&placement.scheduled_slot)
                .copied()
                .unwrap_or(0)
                .checked_add(1)
                .ok_or_else(|| {
                    format!(
                        "slot {} occupancy overflow while pricing request {}",
                        placement.scheduled_slot, placement.req_id
                    )
                })?;
            let capacity_multiplier = get_capacity_multiplier(tiers, next_position);
            if !carbon_intensity.is_finite()
                || carbon_intensity < 0.0
                || !capacity_multiplier.is_finite()
                || capacity_multiplier < 0.0
                || !flavour.error.is_finite()
                || !flavour.duration.is_finite()
                || flavour.duration < 0.0
                || !carbon_cost_duration_scale.is_finite()
                || carbon_cost_duration_scale < 0.0
            {
                return Err(format!(
                    "request {} has invalid forecast, capacity, flavour, or cost-scale data",
                    placement.req_id
                ));
            }
            let carbon_cost = carbon_intensity
                * capacity_multiplier
                * flavour.duration
                * carbon_cost_duration_scale;
            if !carbon_cost.is_finite() {
                return Err(format!(
                    "request {} produced a non-finite carbon cost",
                    placement.req_id
                ));
            }

            *slot_counts.entry(placement.scheduled_slot).or_default() = next_position;
            materialized.push(Assignment::new_for_profile(
                placement.req_id,
                placement.scheduled_slot,
                placement.flavour_name.clone(),
                carbon_cost,
                flavour.error,
                flavour.duration,
                Some(arrival_slot),
                Some(deadline_slot),
                profile_id.clone(),
            ));
        }

        if let Some(missing) = pending_requests
            .iter()
            .find(|request| !seen_request_ids.contains(&request.id))
        {
            return Err(format!(
                "solver proposal omitted claimed request {}",
                missing.id
            ));
        }

        for assignment in &materialized {
            g.record_assignment(assignment.clone());
        }
        for request in pending_requests {
            g.pending_since_virtual_ms.remove(&request.id);
        }
        Ok(materialized)
    }
}

impl Default for SharedState {
    fn default() -> Self {
        Self::new()
    }
}

// ─── result structs ───────────────────────────────────────────────────────────

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
pub struct SlotErrorStats {
    /// Sum of assignment errors in the slot.
    pub error_sum: f64,
    pub count: u64,
    pub average: f64,
}

impl From<SlotErrorTotals> for SlotErrorStats {
    fn from(totals: SlotErrorTotals) -> Self {
        Self {
            error_sum: totals.error_sum,
            count: totals.count,
            average: if totals.count > 0 {
                totals.error_sum / totals.count as f64
            } else {
                0.0
            },
        }
    }
}

impl Default for SlotErrorStats {
    fn default() -> Self {
        Self {
            error_sum: 0.0,
            count: 0,
            average: 0.0,
        }
    }
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
    use crate::engine::qos::{CumulativeErrorPolicy, ErrorWindowPolicy, QosProfile, TaskKindId};
    use crate::engine::solver::ProposedPlacement;
    use crate::types::{Assignment, Request};
    use std::collections::HashSet;
    use std::sync::Arc;
    use std::sync::Barrier;
    use std::sync::mpsc;
    use std::time::Duration;

    fn make_request(id: u64, arrival: i32, deadline: i32) -> Request {
        Request {
            id,
            arrival_slot: arrival,
            deadline_slot: deadline,
            arrival_time: 0.0,
            qos_profile: None,
            qos_profile_id: QosProfileId::default_profile(),
        }
    }

    fn make_assignment(id: u64, slot: i32, error: f64) -> Assignment {
        Assignment::new(
            id,
            slot,
            "Accurate".to_string(),
            1.0,
            error,
            60.0,
            None,
            None,
        )
    }

    fn make_profiled_assignment(id: u64, slot: i32, error: f64, profile_id: &str) -> Assignment {
        Assignment::new_for_profile(
            id,
            slot,
            "Accurate".to_string(),
            1.0,
            error,
            60.0,
            Some(slot),
            Some(slot + 1),
            QosProfileId::parse(profile_id).unwrap(),
        )
    }

    #[test]
    fn replacing_tiers_waits_for_solver_read_snapshot_and_keeps_initialization_idempotent() {
        let state = SharedState::new();
        let initial = vec![
            CapacityTier {
                max_requests: Some(10),
                multiplier: 1.0,
            },
            CapacityTier {
                max_requests: None,
                multiplier: 2.0,
            },
        ];
        let replacement = vec![
            CapacityTier {
                max_requests: Some(3),
                multiplier: 1.0,
            },
            CapacityTier {
                max_requests: None,
                multiplier: 5.0,
            },
        ];
        state.initialize_capacity_tiers(initial.clone());

        let read_snapshot = state.capacity_tiers_guard();
        let setter_state = state.clone();
        let (started_tx, started_rx) = mpsc::channel();
        let (finished_tx, finished_rx) = mpsc::channel();
        let replacement_for_thread = replacement.clone();
        let setter = std::thread::spawn(move || {
            started_tx.send(()).unwrap();
            setter_state.replace_capacity_tiers(replacement_for_thread);
            finished_tx.send(()).unwrap();
        });

        started_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        assert!(
            finished_rx.recv_timeout(Duration::from_millis(20)).is_err(),
            "the setter must wait until the active solver releases its tier snapshot"
        );
        assert_eq!(
            read_snapshot.as_ref().as_ref().unwrap()[0].max_requests,
            Some(10)
        );
        drop(read_snapshot);
        finished_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        setter.join().unwrap();

        // Constructor initialization must not reset a ladder later installed
        // by the admin endpoint.
        state.initialize_capacity_tiers(initial);
        assert_eq!(state.capacity_tiers_snapshot()[0].max_requests, Some(3));
    }

    fn test_profile(profile_id: &str) -> QosProfile {
        QosProfile {
            profile_id: QosProfileId::parse(profile_id).unwrap(),
            task_kind: TaskKindId::parse("question_answering").unwrap(),
            flavours: vec![crate::types::Flavour {
                name: "Accurate".to_string(),
                error: 0.0,
                duration: 10.0,
            }],
            error_semantics: "word-overlap-f1-v1".to_string(),
            max_error_threshold: 10.0,
            error_window: ErrorWindowPolicy {
                past_slots: 2,
                future_slots: 2,
                past_decay_slots: 0,
            },
            cumulative_error: CumulativeErrorPolicy {
                enabled: true,
                hard: true,
            },
        }
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
    fn profile_batch_claims_preserve_each_profile_order_and_leave_others_queued() {
        let state = SharedState::new();
        let mut a1 = make_request(1, 0, 2);
        a1.qos_profile = Some(Arc::new(test_profile("qa-standard-v1")));
        a1.qos_profile_id = QosProfileId::parse("qa-standard-v1").unwrap();
        let mut b1 = make_request(2, 0, 2);
        b1.qos_profile = Some(Arc::new(test_profile("ner-standard-v1")));
        b1.qos_profile_id = QosProfileId::parse("ner-standard-v1").unwrap();
        let mut a2 = make_request(3, 0, 2);
        a2.qos_profile = a1.qos_profile.clone();
        a2.qos_profile_id = a1.qos_profile_id.clone();
        state.add_requests(vec![a1, b1, a2]);

        assert_eq!(
            state.pending_profile_counts(),
            vec![
                (QosProfileId::parse("ner-standard-v1").unwrap(), 1),
                (QosProfileId::parse("qa-standard-v1").unwrap(), 2),
            ]
        );
        let qa = state
            .claim_pending_requests_for_profile(&QosProfileId::parse("qa-standard-v1").unwrap(), 1);
        assert_eq!(
            qa.iter().map(|request| request.id).collect::<Vec<_>>(),
            vec![1]
        );
        assert_eq!(
            state
                .claim_pending_requests_for_profile(
                    &QosProfileId::parse("ner-standard-v1").unwrap(),
                    4,
                )
                .iter()
                .map(|request| request.id)
                .collect::<Vec<_>>(),
            vec![2]
        );
        assert_eq!(
            state
                .claim_pending_requests_for_profile(
                    &QosProfileId::parse("qa-standard-v1").unwrap(),
                    4,
                )
                .iter()
                .map(|request| request.id)
                .collect::<Vec<_>>(),
            vec![3]
        );
        assert_eq!(state.get_pending_count(), 0);
    }

    #[test]
    fn profile_batch_claim_returns_its_committed_state_snapshot() {
        let state = SharedState::new();
        state.add_assignments(vec![make_assignment(99, 2, 1.0)]);
        state.add_requests(vec![make_request(1, 0, 2), make_request(2, 0, 2)]);

        let default_error_window = ErrorWindowPolicy {
            past_slots: 0,
            future_slots: 2,
            past_decay_slots: 0,
        };
        let batch = state
            .claim_pending_requests_for_profile_with_snapshot(
                &QosProfileId::default_profile(),
                1,
                24,
                &default_error_window,
            )
            .unwrap();

        assert_eq!(
            batch
                .requests
                .iter()
                .map(|request| request.id)
                .collect::<Vec<_>>(),
            vec![1]
        );
        assert_eq!(batch.profile_pending_count, 2);
        assert_eq!(batch.snapshot.baseline_slot_counts.get(&2), Some(&1));
        assert_eq!(state.get_pending_count(), 1);

        state.add_assignments(vec![make_assignment(100, 2, 2.0)]);
        assert_eq!(batch.snapshot.baseline_slot_counts.get(&2), Some(&1));
    }

    #[test]
    fn profile_batch_snapshot_uses_profile_window_and_bounded_slot_ranges() {
        let state = SharedState::new();
        state.set_current_slot(10);
        state.add_assignments(vec![
            make_profiled_assignment(1, 5, 1.0, "window-profile"),
            make_profiled_assignment(2, 6, 2.0, "window-profile"),
            make_profiled_assignment(3, 9, 3.0, "window-profile"),
            make_profiled_assignment(4, 10, 4.0, "window-profile"),
            make_profiled_assignment(5, 11, 5.0, "window-profile"),
            make_profiled_assignment(6, 12, 6.0, "window-profile"),
            make_profiled_assignment(7, 14, 7.0, "window-profile"),
            make_profiled_assignment(8, 10, 8.0, "other-profile"),
        ]);
        let mut profile = test_profile("window-profile");
        profile.error_window = ErrorWindowPolicy {
            past_slots: 2,
            future_slots: 2,
            past_decay_slots: 2,
        };
        state.add_request(Request::new_for_qos_profile(100, 12, 14, Arc::new(profile)));

        let default_error_window = ErrorWindowPolicy {
            past_slots: 0,
            future_slots: 0,
            past_decay_slots: 0,
        };
        let batch = state
            .claim_pending_requests_for_profile_with_snapshot(
                &QosProfileId::parse("window-profile").unwrap(),
                1,
                24,
                &default_error_window,
            )
            .unwrap();

        assert_eq!(
            batch.snapshot.baseline_slot_counts,
            HashMap::from([(10, 2), (11, 1), (12, 1), (14, 1)])
        );
        assert_eq!(
            batch
                .snapshot
                .get_profile_error_totals(&QosProfileId::parse("window-profile").unwrap()),
            vec![
                (6, 2.0, 1),
                (9, 3.0, 1),
                (10, 4.0, 1),
                (11, 5.0, 1),
                (12, 6.0, 1),
                (14, 7.0, 1)
            ]
        );

        let window = batch.snapshot.get_profile_window_error_stats(
            &QosProfileId::parse("window-profile").unwrap(),
            10,
            2,
            2,
        );
        assert_eq!(window.count, 4);
        assert_eq!(window.error_sum, 18.0);
    }

    #[test]
    fn solver_snapshot_scope_clamps_future_arrivals_to_the_planning_horizon() {
        let profile_id = QosProfileId::parse("horizon-profile").unwrap();
        let requests = [make_request(1, 23, 23)];
        let error_window = ErrorWindowPolicy {
            past_slots: 3,
            future_slots: 5,
            past_decay_slots: 2,
        };

        let scope = solver_snapshot_scope(&profile_id, &requests, 22, 24, &error_window);

        assert_eq!(scope.profile_id, profile_id);
        assert_eq!(scope.current_slot, 22);
        assert_eq!(scope.history_from_slot, 17);
        assert_eq!(scope.assignment_through_slot, 23);
        assert_eq!(scope.error_through_slot, 23);
    }

    #[test]
    fn assignment_index_tracks_insert_replan_and_error_correction() {
        let state = SharedState::new();
        let first_profile = QosProfileId::parse("index-profile-a").unwrap();
        let second_profile = QosProfileId::parse("index-profile-b").unwrap();

        state.add_assignments(vec![
            make_profiled_assignment(1, 2, 3.0, "index-profile-a"),
            make_profiled_assignment(2, 2, 4.0, "index-profile-b"),
            make_profiled_assignment(3, 3, 5.0, "index-profile-a"),
        ]);
        let slot_two_ids: HashSet<_> = state
            .get_requests_in_slot(2)
            .into_iter()
            .map(|assignment| assignment.request_id)
            .collect();
        assert_eq!(slot_two_ids, HashSet::from([1, 2]));
        let profile_a_slot_two =
            state.get_assignments_in_range(Some(2), Some(2), Some(&first_profile));
        assert_eq!(
            profile_a_slot_two
                .iter()
                .map(|assignment| assignment.request_id)
                .collect::<Vec<_>>(),
            vec![1]
        );

        state.add_assignments(vec![make_profiled_assignment(1, 4, 6.0, "index-profile-a")]);

        let slot_two_ids: HashSet<_> = state
            .get_requests_in_slot(2)
            .into_iter()
            .map(|assignment| assignment.request_id)
            .collect();
        assert_eq!(slot_two_ids, HashSet::from([2]));
        assert_eq!(
            state
                .get_requests_in_slot(4)
                .iter()
                .map(|assignment| assignment.request_id)
                .collect::<Vec<_>>(),
            vec![1]
        );
        assert_eq!(
            state.get_profiles(),
            HashSet::from([first_profile.clone(), second_profile])
        );

        state.correct_assignment_error(1, 2.0);
        let slot_four = state.get_requests_in_slot(4);
        assert_eq!(slot_four.len(), 1);
        assert_eq!(slot_four[0].error, 2.0);
    }

    #[test]
    fn slot_error_aggregates_follow_replacements_and_actual_error_corrections() {
        let state = SharedState::new();
        let profile_id = QosProfileId::parse("aggregate-profile").unwrap();
        state.add_assignments(vec![make_profiled_assignment(
            1,
            1,
            3.0,
            "aggregate-profile",
        )]);
        state.add_assignments(vec![
            make_profiled_assignment(1, 2, 5.0, "aggregate-profile"),
            make_profiled_assignment(2, 2, 7.0, "aggregate-profile"),
        ]);

        assert_eq!(state.get_profile_slot_error_stats(&profile_id, 1).count, 0);
        let moved = state.get_profile_slot_error_stats(&profile_id, 2);
        assert_eq!(moved.count, 2);
        assert_eq!(moved.error_sum, 12.0);

        state.correct_assignment_error(1, 2.0);
        let corrected = state.get_profile_slot_error_stats(&profile_id, 2);
        assert_eq!(corrected.count, 2);
        assert_eq!(corrected.error_sum, 9.0);
        assert_eq!(state.get_slot_error_stats(2).error_sum, 9.0);
    }

    #[test]
    fn evict_settled_assignment_removes_live_row_but_preserves_error_history() {
        let state = SharedState::new();
        let profile_id = QosProfileId::parse("archive-profile").unwrap();
        state.add_assignments(vec![make_profiled_assignment(1, 3, 7.5, "archive-profile")]);

        assert!(
            state
                .evict_settled_assignment(1, 2)
                .unwrap_err()
                .contains("unsettled slot")
        );
        assert!(state.get_assignment(1).is_some());

        let removed = state.evict_settled_assignment(1, 3).unwrap().unwrap();
        assert_eq!(removed.request_id, 1);
        assert!(state.get_assignment(1).is_none());
        assert!(state.get_requests_in_slot(3).is_empty());

        let profile_slot = state.get_profile_slot_error_stats(&profile_id, 3);
        assert_eq!(profile_slot.count, 1);
        assert_eq!(profile_slot.error_sum, 7.5);
        let global_slot = state.get_slot_error_stats(3);
        assert_eq!(global_slot.count, 1);
        assert_eq!(global_slot.error_sum, 7.5);
        let cumulative = state.get_profile_error_stats(&profile_id);
        assert_eq!(cumulative.count, 1);
        assert_eq!(cumulative.error_sum, 7.5);

        assert!(state.evict_settled_assignment(1, 3).unwrap().is_none());
    }

    #[test]
    fn batch_timeout_tracks_virtual_enqueue_age_per_profile_and_only_partial_batches() {
        let state = SharedState::new();
        let default_profile = QosProfileId::default_profile();
        let partial_profile = Arc::new(test_profile("timeout-partial"));
        let full_profile = Arc::new(test_profile("timeout-full"));
        state.add_request(make_request(1, 0, 3));

        state.set_virtual_elapsed_ms(500);
        state.add_request(Request::new_for_qos_profile(
            2,
            0,
            3,
            partial_profile.clone(),
        ));
        for id in 10..13 {
            state.add_request(Request::new_for_qos_profile(id, 0, 3, full_profile.clone()));
        }

        // A claim/requeue must retain the request's original queue age.
        let claimed = state.claim_pending_requests_for_profile(&default_profile, 1);
        state.set_virtual_elapsed_ms(900);
        state.requeue_pending_requests_front(claimed);
        assert!(state.timed_out_partial_profiles(999, 3, 1_000).is_empty());

        let timed_out = state.timed_out_partial_profiles(1_000, 3, 1_000);
        assert_eq!(timed_out, HashSet::from([default_profile.clone()]));

        let timed_out = state.timed_out_partial_profiles(1_500, 3, 1_000);
        assert_eq!(
            timed_out,
            HashSet::from([default_profile, partial_profile.profile_id.clone()])
        );
        assert!(!timed_out.contains(&full_profile.profile_id));
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
    fn profile_error_totals_and_windows_exclude_other_profiles() {
        let state = SharedState::new();
        let qa = QosProfileId::parse("qa-standard-v1").unwrap();
        let ner = QosProfileId::parse("ner-standard-v1").unwrap();
        state.add_assignments(vec![
            make_profiled_assignment(1, 3, 10.0, "qa-standard-v1"),
            make_profiled_assignment(2, 3, 90.0, "ner-standard-v1"),
            make_profiled_assignment(3, 4, 30.0, "qa-standard-v1"),
        ]);

        let qa_stats = state.get_profile_error_stats(&qa);
        let ner_stats = state.get_profile_error_stats(&ner);
        let qa_window = state.get_profile_window_error_stats(&qa, 3, 0, 1, &HashSet::new());

        assert_eq!(qa_stats.count, 2);
        assert!((qa_stats.avg - 20.0).abs() < 1e-9);
        assert_eq!(ner_stats.count, 1);
        assert!((ner_stats.avg - 90.0).abs() < 1e-9);
        assert_eq!(qa_window.count, 2);
        assert!((qa_window.average - 20.0).abs() < 1e-9);
        assert_eq!(state.get_global_error_stats().count, 3);
    }

    #[test]
    fn error_correction_updates_only_the_assignment_profile_aggregate() {
        let state = SharedState::new();
        let qa = QosProfileId::parse("qa-standard-v1").unwrap();
        let ner = QosProfileId::parse("ner-standard-v1").unwrap();
        state.add_assignments(vec![
            make_profiled_assignment(1, 3, 10.0, "qa-standard-v1"),
            make_profiled_assignment(2, 3, 80.0, "ner-standard-v1"),
        ]);

        state.correct_assignment_error(1, 50.0);

        let qa_stats = state.get_profile_error_stats(&qa);
        let ner_stats = state.get_profile_error_stats(&ner);
        assert_eq!(qa_stats.count, 1);
        assert!((qa_stats.error_sum - 50.0).abs() < 1e-9);
        assert_eq!(ner_stats.count, 1);
        assert!((ner_stats.error_sum - 80.0).abs() < 1e-9);
        assert!((state.get_global_error_stats().error_sum - 130.0).abs() < 1e-9);
    }

    #[test]
    fn correct_assignment_error_adjusts_global_sum_by_delta() {
        let state = SharedState::new();
        state.add_assignments(vec![make_assignment(1, 0, 3.2), make_assignment(2, 0, 2.0)]);
        // Real measured error (2.1) replaces the predicted one (3.2) for request 1.
        state.correct_assignment_error(1, 2.1);
        let g = state.get_global_error_stats();
        assert_eq!(
            g.count, 2,
            "correction must not change the assignment count"
        );
        assert!(
            (g.error_sum - 4.1).abs() < 1e-9,
            "error_sum={}",
            g.error_sum
        ); // 3.2 - 3.2 + 2.1 + 2.0
        // The window average (recomputed live from Assignment::error) must
        // also reflect the corrected value.
        let window = state.get_window_error_stats(0, 2, 2, &HashSet::new());
        assert!((window.error_sum - 4.1).abs() < 1e-9);
    }

    #[test]
    fn correct_assignment_error_is_noop_for_unknown_request() {
        let state = SharedState::new();
        state.add_assignments(vec![make_assignment(1, 0, 3.2)]);
        state.correct_assignment_error(999, 0.0); // unknown request_id
        let g = state.get_global_error_stats();
        assert!((g.error_sum - 3.2).abs() < 1e-9);
    }

    #[test]
    fn correct_assignment_carbon_cost_updates_in_place() {
        let state = SharedState::new();
        state.add_assignments(vec![make_assignment(1, 0, 0.0)]); // carbon_cost starts at 1.0
        let result = state.correct_assignment_carbon_cost(1, 2.5);
        assert_eq!(result, Some(2.5));
        assert_eq!(state.get_current_assignments()[&1].carbon_cost, 2.5);
    }

    #[test]
    fn correct_assignment_carbon_cost_is_none_for_unknown_request() {
        let state = SharedState::new();
        assert_eq!(state.correct_assignment_carbon_cost(999, 2.5), None);
    }

    #[test]
    fn replanned_request_updates_error_without_double_counting() {
        let state = SharedState::new();
        state.add_assignments(vec![make_assignment(1, 0, 4.0)]);
        // Replan request 1 (same id, different slot/error/cost).
        state.add_assignments(vec![make_assignment(1, 1, 0.0)]);
        let g = state.get_global_error_stats();
        // Only counted once
        assert_eq!(g.count, 1);
        assert!(g.error_sum.abs() < 1e-9);
    }

    #[test]
    fn window_error_stats_excludes_out_of_window() {
        let state = SharedState::new();
        // slot 5 is in window [3,7], slot 10 is outside
        state.add_assignments(vec![
            make_assignment(1, 5, 4.0),
            make_assignment(2, 10, 2.0),
        ]);
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

    #[test]
    fn concurrent_commits_price_at_current_occupancy() {
        let state = SharedState::new();
        let profile_id = QosProfileId::default_profile();
        let barrier = Arc::new(Barrier::new(2));
        let flavours = Arc::new(vec![Flavour {
            name: "Accurate".to_string(),
            error: 5.0,
            duration: 10.0,
        }]);
        let forecast = Arc::new(vec![100.0]);
        let tiers = Arc::new(vec![
            CapacityTier {
                max_requests: Some(1),
                multiplier: 1.0,
            },
            CapacityTier {
                max_requests: None,
                multiplier: 2.0,
            },
        ]);

        let workers: Vec<_> = (0..2)
            .map(|id| {
                let state = state.clone();
                let barrier = barrier.clone();
                let profile_id = profile_id.clone();
                let flavours = flavours.clone();
                let forecast = forecast.clone();
                let tiers = tiers.clone();
                std::thread::spawn(move || {
                    let request = Request::new(id, 0, 0);
                    let proposal = SolverProposal {
                        placements: vec![ProposedPlacement::new(id, 0, "Accurate".to_string())],
                    };
                    barrier.wait();
                    state.materialize_and_commit_proposal(
                        &proposal,
                        &[request],
                        &profile_id,
                        0,
                        &flavours,
                        &forecast,
                        &tiers,
                        1.0,
                    )
                })
            })
            .collect();

        let committed: Vec<_> = workers
            .into_iter()
            .map(|worker| worker.join().unwrap().unwrap())
            .collect();
        let mut costs: Vec<_> = committed
            .into_iter()
            .map(|assignments| assignments[0].carbon_cost)
            .collect();
        costs.sort_by(f64::total_cmp);
        assert_eq!(costs, vec![1000.0, 2000.0]);
        assert_eq!(state.get_current_assignments().len(), 2);
        assert_eq!(state.get_profile_error_stats(&profile_id).avg, 5.0);
    }

    #[test]
    fn proposal_validation_rejects_out_of_window_on_time_placements() {
        let state = SharedState::new();
        let profile_id = QosProfileId::default_profile();
        let pending = [Request::new(1, 0, 1)];
        let proposal = SolverProposal {
            placements: vec![ProposedPlacement::new(1, 2, "Accurate".to_string())],
        };
        let flavours = [Flavour {
            name: "Accurate".to_string(),
            error: 0.0,
            duration: 10.0,
        }];

        let error = state
            .materialize_and_commit_proposal(
                &proposal,
                &pending,
                &profile_id,
                0,
                &flavours,
                &[100.0, 100.0, 100.0],
                &[],
                1.0,
            )
            .unwrap_err();

        assert!(error.contains("outside its feasible slot window"));
        assert!(state.get_current_assignments().is_empty());
    }

    #[test]
    fn proposal_validation_rejects_unclaimed_ids_even_if_already_assigned() {
        let state = SharedState::new();
        state.add_assignments(vec![make_assignment(99, 0, 1.0)]);
        let profile_id = QosProfileId::default_profile();
        let pending = [Request::new(1, 0, 0)];
        let proposal = SolverProposal {
            placements: vec![ProposedPlacement::new(99, 0, "Balanced".to_owned())],
        };
        let flavours = [Flavour {
            name: "Balanced".to_owned(),
            error: 0.0,
            duration: 10.0,
        }];

        let error = state
            .materialize_and_commit_proposal(
                &proposal,
                &pending,
                &profile_id,
                0,
                &flavours,
                &[100.0],
                &[],
                1.0,
            )
            .unwrap_err();

        assert!(error.contains("references unclaimed request 99"));
        assert_eq!(state.get_assignment(99).unwrap().carbon_cost, 1.0);
    }

    #[test]
    fn proposal_validation_accepts_late_placement_at_current_slot() {
        let state = SharedState::new();
        let profile_id = QosProfileId::default_profile();
        let pending = [Request::new(1, 0, 0)];
        let proposal = SolverProposal {
            placements: vec![ProposedPlacement::late(1, 1, "Accurate".to_string())],
        };
        let flavours = [Flavour {
            name: "Accurate".to_string(),
            error: 0.0,
            duration: 10.0,
        }];

        let assignments = state
            .materialize_and_commit_proposal(
                &proposal,
                &pending,
                &profile_id,
                1,
                &flavours,
                &[100.0, 100.0],
                &[],
                1.0,
            )
            .unwrap();

        assert_eq!(assignments[0].scheduled_slot, 1);
        assert_eq!(assignments[0].deadline_slot, Some(0));
    }

    #[test]
    fn incomplete_proposal_is_rejected_without_partial_commit() {
        let state = SharedState::new();
        let profile_id = QosProfileId::default_profile();
        let pending = [Request::new(1, 0, 0), Request::new(2, 0, 0)];
        let proposal = SolverProposal {
            placements: vec![ProposedPlacement::new(1, 0, "Accurate".to_string())],
        };
        let flavours = [Flavour {
            name: "Accurate".to_string(),
            error: 0.0,
            duration: 10.0,
        }];

        let error = state
            .materialize_and_commit_proposal(
                &proposal,
                &pending,
                &profile_id,
                0,
                &flavours,
                &[100.0],
                &[],
                1.0,
            )
            .unwrap_err();

        assert!(error.contains("omitted claimed request 2"));
        assert!(state.get_current_assignments().is_empty());
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

        // The snapshot keeps only per-slot occupancy counts, not assignment rows.
        let expected_counts = state.get_future_assignments(5).into_iter().fold(
            HashMap::new(),
            |mut counts, assignment| {
                *counts.entry(assignment.scheduled_slot).or_insert(0) += 1;
                counts
            },
        );
        let snapshot_counts = snap
            .baseline_slot_counts
            .iter()
            .filter(|(slot, _)| **slot >= 5)
            .map(|(slot, count)| (*slot, *count))
            .collect::<HashMap<_, _>>();
        assert_eq!(snapshot_counts, expected_counts);

        // Window error stats match.
        let direct_ws = state.get_window_error_stats(5, 3, 3, &HashSet::new());
        let snap_ws = snap.get_window_error_stats(5, 3, 3);
        assert!((direct_ws.error_sum - snap_ws.error_sum).abs() < 1e-12);
        assert_eq!(direct_ws.count, snap_ws.count);

        // Global stats match.
        let direct_gs = state.get_global_error_stats();
        let snap_gs = snap.get_global_error_stats();
        assert!((direct_gs.error_sum - snap_gs.error_sum).abs() < 1e-12);
        assert_eq!(direct_gs.count, snap_gs.count);

        assert_eq!(snap.baseline_slot_counts.get(&3), Some(&1));
        assert_eq!(snap.baseline_slot_counts.get(&4), Some(&1));
        assert_eq!(snap.baseline_slot_counts.get(&99), None);
    }
}
