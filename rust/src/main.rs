use std::sync::Arc;
use std::time::Duration;

use carbonshift_rs::config::Config;
use carbonshift_rs::generator::RequestGenerator;
use carbonshift_rs::metrics_logger::MetricsLogger;
use carbonshift_rs::scenario::Scenario;
use carbonshift_rs::scheduler::BatchScheduler;
use carbonshift_rs::shared_state::SharedState;

struct Online2System {
    generator: RequestGenerator,
    scheduler: BatchScheduler,
    cfg: Arc<Config>,
}

impl Online2System {
    fn new(cfg: Config, scenario_forecast: Option<Vec<f64>>, scenario_requests: Option<Vec<Vec<carbonshift_rs::types::Request>>>) -> Self {
        let cfg = Arc::new(cfg);
        let shared_state = SharedState::new();

        let ml = Arc::new(MetricsLogger::new(
            cfg.enable_solver_logging,
            cfg.solver_runs_file.clone(),
            cfg.solver_assignments_file.clone(),
            cfg.solver_slot_metrics_file.clone(),
            if cfg.enable_infeasibility_debug_logging {
                Some(cfg.solver_infeasible_debug_file.clone())
            } else {
                None
            },
        ));

        let generator = match scenario_requests {
            Some(by_slot) => RequestGenerator::new_from_scenario(
                shared_state.clone(),
                cfg.clone(),
                by_slot,
            ),
            None => RequestGenerator::new(shared_state.clone(), cfg.clone()),
        };
        let scheduler = BatchScheduler::new(shared_state, cfg.clone(), ml, scenario_forecast);

        Self { generator, scheduler, cfg }
    }

    fn start(&mut self) {
        println!("[Online2System] Starting CarbonShift RS…");
        self.scheduler.start();
        self.generator.start();
    }

    fn stop(&mut self) {
        println!("[Online2System] Stopping…");
        self.generator.stop();
        self.scheduler.stop();
        let stats = self.scheduler.get_statistics();
        println!(
            "[Online2System] Done. batches={}, scheduled={}, runs={}, \
             last_solver_ms={:.2}ms",
            stats.batches_processed,
            stats.total_scheduled,
            stats.solver_runs,
            stats.last_solver_elapsed_ms
        );
    }

    /// Runs until `total_slots` have elapsed or `stop_flag` is set.
    fn monitor_loop(&self, stop_flag: &std::sync::atomic::AtomicBool) {
        let total_slots = self.cfg.total_slots;
        let eff_slot_dur = self.cfg.effective_slot_duration_secs();
        let total_duration = total_slots as f64 * eff_slot_dur;

        loop {
            std::thread::sleep(Duration::from_millis(500));

            if stop_flag.load(std::sync::atomic::Ordering::Relaxed) {
                break;
            }

            let stats = self.scheduler.get_statistics();
            if self.cfg.verbose {
                let elapsed = self.scheduler.shared_state_virtual_elapsed_secs();
                let current_slot = (elapsed / eff_slot_dur) as i32;
                println!(
                    "[Monitor] slot={}/{}, batches={}, scheduled={}, \
                     avg_ms/batch={:.2}, active_workers={}/{}",
                    current_slot,
                    total_slots,
                    stats.batches_processed,
                    stats.total_scheduled,
                    stats.avg_solver_ms_per_batch,
                    stats.active_batch_workers,
                    stats.max_batch_parallelism
                );
            }

            if self.scheduler.shared_state_virtual_elapsed_secs() >= total_duration {
                break;
            }
        }
    }
}

fn main() {
    // CLI: [scenario_path] [--no-skip] [--speed-scale <f64>]
    let args: Vec<String> = std::env::args().collect();
    let mut scenario_path: Option<String> = None;
    let mut no_skip = false;
    let mut speed_scale: f64 = 1.0;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--no-skip" => { no_skip = true; }
            "--speed-scale" => {
                i += 1;
                speed_scale = args.get(i)
                    .and_then(|s| s.parse().ok())
                    .unwrap_or_else(|| { eprintln!("--speed-scale requires a numeric value"); std::process::exit(1); });
            }
            arg if !arg.starts_with("--") => { scenario_path = Some(arg.to_string()); }
            other => { eprintln!("Unknown argument: {other}"); std::process::exit(1); }
        }
        i += 1;
    }

    let (cfg, scenario_forecast, scenario_requests) = match scenario_path {
        Some(ref path) => {
            let scenario = Scenario::from_file(path).unwrap_or_else(|e| {
                eprintln!("Error loading scenario '{}': {e}", path);
                std::process::exit(1);
            });
            let mut c = Config::default();
            c.apply_scenario_metadata(&scenario.metadata);
            if no_skip { c.skip_empty_slots = false; }
            c.slot_speed_scale = speed_scale;
            let by_slot = scenario.requests_by_slot();
            println!(
                "Loaded scenario: {} slots, {} requests ({})",
                scenario.metadata.total_slots,
                scenario.requests.len(),
                path
            );
            (c, Some(scenario.carbon_forecast), Some(by_slot))
        }
        None => {
            let mut c = Config::default();
            if no_skip { c.skip_empty_slots = false; }
            c.slot_speed_scale = speed_scale;
            (c, None, None)
        }
    };

    // batch_size can still be overridden here if needed; all scenario params
    // are already applied via apply_scenario_metadata above.
    // cfg.batch_size = 8;  // example override

    if cfg.verbose {
        println!(
            "CarbonShift RS — batch_size={}, total_slots={}, flavours={:?}",
            cfg.batch_size,
            cfg.total_slots,
            cfg.flavours.iter().map(|f| f.name.as_str()).collect::<Vec<_>>()
        );
    }

    let stop_flag = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let sf2 = stop_flag.clone();
    ctrlc::set_handler(move || {
        println!("\n[signal] Ctrl-C received — shutting down…");
        sf2.store(true, std::sync::atomic::Ordering::SeqCst);
    })
    .expect("Error setting Ctrl-C handler");

    let mut system = Online2System::new(cfg, scenario_forecast, scenario_requests);
    system.start();
    system.monitor_loop(&stop_flag);
    system.stop();
}
