/// Request generator — simulates Poisson-like arrivals.
///
/// Mirrors `request_generator.py::RequestGenerator`.
///
/// Generates requests at a configurable rate (Gaussian-distributed per slot)
/// and adds them to `SharedState`.  Runs in a background thread; call
/// `start()` / `stop()` to manage the lifecycle.

use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc,
};
use std::thread::JoinHandle;
use std::time::Duration;

use rand::SeedableRng;
use rand_distr::{Distribution, Normal};

use crate::config::Config;
use crate::shared_state::SharedState;
use crate::types::Request;

// ─── RequestGenerator ────────────────────────────────────────────────────────

pub struct RequestGenerator {
    shared_state: SharedState,
    cfg: Arc<Config>,
    /// Monotonically increasing request-id counter (shared with the thread).
    request_counter: Arc<AtomicU64>,
    running: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
    /// Pre-generated requests indexed by arrival slot (scenario replay mode).
    /// `None` → stochastic generation.  `Some(v)` → feed v[slot] each slot.
    scenario_by_slot: Option<Arc<Vec<Vec<Request>>>>,
}

impl RequestGenerator {
    pub fn new(shared_state: SharedState, cfg: Arc<Config>) -> Self {
        Self {
            shared_state,
            cfg,
            request_counter: Arc::new(AtomicU64::new(0)),
            running: Arc::new(AtomicBool::new(false)),
            thread: None,
            scenario_by_slot: None,
        }
    }

    /// Create a generator that replays pre-generated requests from a scenario.
    ///
    /// `by_slot` must be indexed by arrival_slot; slot `i` contains all requests
    /// that arrive at that slot.  Use `Scenario::requests_by_slot()` to build it.
    pub fn new_from_scenario(
        shared_state: SharedState,
        cfg: Arc<Config>,
        by_slot: Vec<Vec<Request>>,
    ) -> Self {
        Self {
            shared_state,
            cfg,
            request_counter: Arc::new(AtomicU64::new(0)),
            running: Arc::new(AtomicBool::new(false)),
            thread: None,
            scenario_by_slot: Some(Arc::new(by_slot)),
        }
    }

    /// Start the generator background thread.
    pub fn start(&mut self) {
        if self.running.swap(true, Ordering::SeqCst) {
            return; // already running
        }

        let running = self.running.clone();
        let shared_state = self.shared_state.clone();
        let cfg = self.cfg.clone();
        let counter = self.request_counter.clone();
        let scenario = self.scenario_by_slot.clone();

        if cfg.verbose {
            match &scenario {
                Some(_) => println!("[RequestGenerator] Started: scenario replay mode"),
                None => println!(
                    "[RequestGenerator] Started: {:.1} req/slot (stochastic)",
                    cfg.predicted_requests_per_slot
                ),
            }
        }

        self.thread = Some(std::thread::spawn(move || {
            generator_loop(running, shared_state, cfg, counter, scenario);
        }));
    }

    /// Stop the generator (joins the background thread).
    pub fn stop(&mut self) {
        self.running.store(false, Ordering::SeqCst);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
        if self.cfg.verbose {
            println!("[RequestGenerator] Stopped");
        }
    }

    /// Total requests generated so far.
    pub fn get_total_generated(&self) -> u64 {
        self.request_counter.load(Ordering::Relaxed)
    }
}

// ─── generator loop ──────────────────────────────────────────────────────────

fn generator_loop(
    running: Arc<AtomicBool>,
    shared_state: SharedState,
    cfg: Arc<Config>,
    counter: Arc<AtomicU64>,
    scenario_by_slot: Option<Arc<Vec<Vec<Request>>>>,
) {
    let slot_duration = cfg.effective_slot_duration_secs();
    let rate = cfg.predicted_requests_per_slot;
    let sigma = (rate * cfg.request_rate_std_factor).max(1.0);
    let dist = Normal::new(rate, sigma).expect("valid Normal distribution");
    let base_seed = cfg.prehistory_random_seed;

    let mut last_slot: i32 = -1;
    // True realtime pacing only makes sense when the virtual clock actually
    // tracks wall-clock time (skip_empty_slots=false); in fast/skip mode the
    // clock jumps ahead as soon as the queue drains, so bursting is correct.
    let realtime_pacing = !cfg.skip_empty_slots;
    const LOCK_BATCH_SIZE: usize = 50;

    while running.load(Ordering::Relaxed) {
        // Use the shared virtual clock so skip_empty_slots advances us too.
        let elapsed = shared_state.virtual_elapsed_secs();
        let raw_slot = (elapsed / slot_duration) as i32;
        // Clamp so the final slot is always processed below even if the
        // virtual clock has already moved past it by the time we check.
        let current_slot = raw_slot.min(cfg.total_slots - 1);

        if current_slot > last_slot {
            // Catch up on every slot boundary crossed since the last check.
            // In real-time pacing mode, sending one slot's requests can take
            // longer than that slot's real-time duration (chunking/locking
            // overhead), so the virtual clock may jump ahead by more than
            // one slot between iterations. Only fetching `current_slot`
            // would silently drop the skipped slots' requests entirely, so
            // process each crossed slot (bursting the ones we're already
            // behind on; pacing only the one we just caught up to).
            for slot in (last_slot + 1)..=current_slot {
                let mut requests: Vec<Request> = match &scenario_by_slot {
                    Some(by_slot) => by_slot.get(slot as usize).cloned().unwrap_or_default(),
                    None => {
                        let mut rng = rand::rngs::StdRng::seed_from_u64(
                            base_seed.wrapping_add(slot as u64),
                        );
                        let num = (dist.sample(&mut rng) as i32).max(1) as usize;
                        (0..num).map(|_| generate_request(slot, &cfg, &counter)).collect()
                    }
                };

                // Rispettiamo l'ordine di arrivo all'interno dello slot
                requests.sort_by(|a, b| a.arrival_time.partial_cmp(&b.arrival_time).unwrap());

                let num_requests = requests.len();

                if realtime_pacing && num_requests > 0 && slot == current_slot {
                    // Spread the slot's requests evenly across its real-time
                    // duration, sending at most `generator_realtime_chunk_size`
                    // at a time and sleeping the proportional inter-chunk delay.
                    let chunk_size = cfg.generator_realtime_chunk_size.max(1);
                    let per_request_secs = slot_duration / num_requests as f64;
                    for chunk in requests.chunks(chunk_size) {
                        if !running.load(Ordering::Relaxed) {
                            break;
                        }
                        shared_state.add_requests(chunk.to_vec());
                        sleep_interruptible(&running, per_request_secs * chunk.len() as f64);
                    }
                } else {
                    // Older, already-elapsed slots (or non-paced mode): burst
                    // immediately, pacing them further would only fall behind.
                    for chunk in requests.chunks(LOCK_BATCH_SIZE) {
                        shared_state.add_requests(chunk.to_vec()); // un solo lock per chunk
                    }
                }

                shared_state.set_generator_processed_slot(slot);

                if cfg.verbose {
                    println!("[RequestGenerator] Slot {slot}: {num_requests} requests");
                }
            }
            last_slot = current_slot;
        }

        // Stop once we've caught up through the last slot.
        if raw_slot >= cfg.total_slots {
            break;
        }

        std::thread::sleep(Duration::from_millis(10));
    }
}

/// Sleep for `secs` real seconds, checking `running` every 10ms so `stop()`
/// interrupts promptly instead of blocking for a whole slot.
fn sleep_interruptible(running: &Arc<AtomicBool>, secs: f64) {
    let mut remaining_ms = (secs * 1000.0).round() as i64;
    while remaining_ms > 0 && running.load(Ordering::Relaxed) {
        let step = remaining_ms.min(10);
        std::thread::sleep(Duration::from_millis(step as u64));
        remaining_ms -= step;
    }
}

fn generate_request(arrival_slot: i32, cfg: &Config, counter: &Arc<AtomicU64>) -> Request {
    let id = counter.fetch_add(1, Ordering::Relaxed);
    let slack_range = (cfg.deadline_max_slack - cfg.deadline_min_slack).max(0);
    // Deterministic slack based on request id so replays match.
    let slack = cfg.deadline_min_slack + (id as i32 % (slack_range + 1));
    let deadline_slot = (arrival_slot + slack).min(cfg.total_slots - 1);
    Request::new(id, arrival_slot, deadline_slot)
}
