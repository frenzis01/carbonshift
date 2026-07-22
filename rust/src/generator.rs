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

    while running.load(Ordering::Relaxed) {
        // Use the shared virtual clock so skip_empty_slots advances us too.
        let elapsed = shared_state.virtual_elapsed_secs();
        let current_slot = (elapsed / slot_duration) as i32;

        // Stop once we've passed the last slot.
        if current_slot >= cfg.total_slots {
            break;
        }
        
        const BATCH_SIZE: usize = 100;

        if current_slot > last_slot {
            last_slot = current_slot;
            let mut requests: Vec<Request> = match &scenario_by_slot {
                Some(by_slot) => by_slot.get(current_slot as usize).cloned().unwrap_or_default(),
                None => {
                    let mut rng = rand::rngs::StdRng::seed_from_u64(
                        base_seed.wrapping_add(current_slot as u64),
                    );
                    let num = (dist.sample(&mut rng) as i32).max(1) as usize;
                    (0..num).map(|_| generate_request(current_slot, &cfg, &counter)).collect()
                }
            };
        
            // Rispettiamo l'ordine di arrivo all'interno dello slot
            requests.sort_by(|a, b| a.arrival_time.partial_cmp(&b.arrival_time).unwrap());
        
            let num_requests = requests.len();
            for chunk in requests.chunks(BATCH_SIZE) {
                shared_state.add_requests(chunk.to_vec()); // un solo lock per chunk
            }
        
            shared_state.set_generator_processed_slot(current_slot);
        
            if cfg.verbose {
                println!("[RequestGenerator] Slot {current_slot}: {num_requests} requests");
            }
        }

        std::thread::sleep(std::time::Duration::from_millis(10));
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
