/// Thread-safe CSV metrics logger.
///
/// Mirrors `metrics_logger.py::SolverMetricsLogger`.
///
/// All files are opened for append on each write (no persistent file handles)
/// and serialised through a single `Mutex`.  This keeps the API Send+Sync
/// without lifetime complexity.

use std::collections::HashMap;
use std::fs::OpenOptions;
use std::io::BufRead;
use std::path::Path;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

// ─── field order ─────────────────────────────────────────────────────────────

const RUN_FIELDS: &[&str] = &[
    "run_id",
    "run_sequence",
    "current_slot",
    "pending_batch_size",
    "total_assignments",
    "new_assignments",
    "replanned_assignments",
    "solver_status",
    "solver_mode",
    "lock_future_assignments",
    "solver_start_ts",
    "solver_end_ts",
    "solver_elapsed_ms",
    "avg_ms_per_new_request",
    "avg_ms_per_assignment",
    "total_carbon_cost",
    "carbon_cost_per_new_request",
    "carbon_cost_per_assignment",
    "error_window_avg_after",
    "error_window_avg_after_real",
    "error_window_start_slot",
    "error_window_end_slot",
    "error_window_threshold",
    "error_window_violated_after",
    "error_window_violated_after_real",
    "batches_processed_after",
    "total_scheduled_after",
    "global_error_before",
    "global_error_count_before",
    "global_error_constraint_active",
];

const ASSIGNMENT_FIELDS: &[&str] = &[
    "run_id",
    "current_slot",
    "solver_start_ts",
    "solver_end_ts",
    "request_id",
    "is_pending_request",
    "is_new_assignment_in_run",
    "scheduled_slot",
    "flavour_name",
    "flavour_duration",
    "error",
    "carbon_cost",
    "arrival_slot",
    "deadline_slot",
];

const SLOT_METRIC_FIELDS: &[&str] = &[
    "run_id",
    "current_slot",
    "scheduled_slot",
    "run_slot_count",
    "total_slot_count_after",
    "avg_error_in_slot",
    "run_avg_error_in_slot",
    "slot_has_assignments_after",
    "carbon_intensity",
    "capacity_multiplier_after",
    "capacity_level_max_requests",
    "request_ids",
    "flavour_breakdown",
];

const INFEASIBLE_DEBUG_FIELDS: &[&str] = &[
    "event_id",
    "timestamp",
    "current_slot",
    "pending_batch_size",
    "pending_request_details",
    "strict_threshold",
    "baseline_error_sum",
    "baseline_request_count",
    "baseline_average_error",
    "min_flavour_error",
    "max_flavour_error",
    "min_possible_avg_error_pending_only",
    "max_possible_avg_error_pending_only",
    "strict_infeasible_by_error_bound",
    "strict_scheduled_pending_count",
    "relaxed_scheduled_pending_count",
    "relaxed_success",
    "lock_future_assignments",
    "future_assignment_count",
    "future_slot_counts",
    "future_assignment_details",
    "all_active_slot_counts",
];

// ─── MetricsLogger ───────────────────────────────────────────────────────────

pub struct MetricsLogger {
    pub enabled: bool,
    pub runs_file: String,
    pub assignments_file: String,
    pub slot_metrics_file: String,
    pub infeasible_debug_file: Option<String>,
    lock: Mutex<()>,
}

impl MetricsLogger {
    pub fn new(
        enabled: bool,
        runs_file: String,
        assignments_file: String,
        slot_metrics_file: String,
        infeasible_debug_file: Option<String>,
    ) -> Self {
        let logger = Self {
            enabled,
            runs_file,
            assignments_file,
            slot_metrics_file,
            infeasible_debug_file,
            lock: Mutex::new(()),
        };
        if logger.enabled {
            ensure_header(&logger.runs_file, RUN_FIELDS);
            ensure_header(&logger.assignments_file, ASSIGNMENT_FIELDS);
            ensure_header(&logger.slot_metrics_file, SLOT_METRIC_FIELDS);
            if let Some(ref p) = logger.infeasible_debug_file {
                ensure_header(p, INFEASIBLE_DEBUG_FIELDS);
            }
        }
        logger
    }

    /// Log one solver run plus its per-assignment and per-slot-metric rows.
    ///
    /// Returns the generated `run_id` string (empty string when disabled).
    pub fn log_solver_run(
        &self,
        run_data: &HashMap<String, String>,
        assignment_rows: &[HashMap<String, String>],
        slot_metric_rows: &[HashMap<String, String>],
    ) -> String {
        if !self.enabled {
            return String::new();
        }
        let run_id = run_id_from_data(run_data);
        let _guard = self.lock.lock().unwrap();
        let mut full_run = run_data.clone();
        full_run.insert("run_id".to_string(), run_id.clone());
        append_rows(&self.runs_file, RUN_FIELDS, &[full_run]);

        let augmented_assignments: Vec<HashMap<String, String>> = assignment_rows
            .iter()
            .map(|r| {
                let mut row = r.clone();
                row.insert("run_id".to_string(), run_id.clone());
                row
            })
            .collect();
        append_rows(&self.assignments_file, ASSIGNMENT_FIELDS, &augmented_assignments);

        let augmented_slots: Vec<HashMap<String, String>> = slot_metric_rows
            .iter()
            .map(|r| {
                let mut row = r.clone();
                row.insert("run_id".to_string(), run_id.clone());
                row
            })
            .collect();
        append_rows(&self.slot_metrics_file, SLOT_METRIC_FIELDS, &augmented_slots);
        run_id
    }

    /// Log one infeasibility debug event. Returns the event_id string.
    pub fn log_infeasible_debug(&self, event_data: &HashMap<String, String>) -> String {
        if !self.enabled {
            return String::new();
        }
        if let Some(ref path) = self.infeasible_debug_file {
            let event_id = format!("inf_{}", unix_now_ms());
            let _guard = self.lock.lock().unwrap();
            let mut row = event_data.clone();
            row.insert("event_id".to_string(), event_id.clone());
            row.insert("timestamp".to_string(), unix_now_ms().to_string());
            append_rows(path, INFEASIBLE_DEBUG_FIELDS, &[row]);
            return event_id;
        }
        String::new()
    }
}

// ─── helpers ─────────────────────────────────────────────────────────────────

fn unix_now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

/// Generate a run_id from the run_data map (slot + sequence).
fn run_id_from_data(data: &HashMap<String, String>) -> String {
    let slot = data.get("current_slot").map(|s| s.as_str()).unwrap_or("0");
    let seq = data.get("run_sequence").map(|s| s.as_str()).unwrap_or("0");
    let ts = unix_now_ms();
    format!("run_s{slot}_n{seq}_{ts}")
}

/// Ensure `path` starts with the expected CSV header row.
///
/// If the file is missing or has a different header, it is backed up and a
/// fresh file is created with the correct header.
fn ensure_header(path: &str, fields: &[&str]) {
    if let Some(parent) = Path::new(path).parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent).ok();
        }
    }

    let expected = fields.join(",");
    let needs_create = if Path::new(path).exists() {
        let file = std::fs::File::open(path).ok();
        let first_line = file.and_then(|f| {
            let mut reader = std::io::BufReader::new(f);
            let mut line = String::new();
            reader.read_line(&mut line).ok()?;
            Some(line.trim().to_string())
        });
        first_line.map_or(true, |h| h != expected)
    } else {
        true
    };

    if needs_create {
        if Path::new(path).exists() {
            let backup = format!("{}.bak.{}", path, unix_now_ms());
            std::fs::rename(path, &backup).ok();
        }
        let mut wtr = csv::Writer::from_path(path).expect("Cannot create CSV file");
        wtr.write_record(fields).ok();
        wtr.flush().ok();
    }
}

/// Append `rows` to an existing CSV file (no header written).
fn append_rows(path: &str, fields: &[&str], rows: &[HashMap<String, String>]) {
    if rows.is_empty() {
        return;
    }
    let file = OpenOptions::new()
        .append(true)
        .create(true)
        .open(path)
        .expect("Cannot open CSV for append");

    let mut wtr = csv::WriterBuilder::new()
        .has_headers(false)
        .from_writer(file);

    for row in rows {
        let record: Vec<&str> = fields
            .iter()
            .map(|f| row.get(*f).map(|s| s.as_str()).unwrap_or(""))
            .collect();
        wtr.write_record(&record).ok();
    }
    wtr.flush().ok();
}
