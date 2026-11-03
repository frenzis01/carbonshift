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
    "qos_profile_id",
    "solver_mode",
    "solver_status",
    "new_assignments",
    "greedy_fallback_assignments",
    "solver_elapsed_ms",
    "total_carbon_cost",
    "error_window_avg_after",
    "error_window_avg_after_real",
    "error_window_start_slot",
    "error_window_end_slot",
    "error_window_threshold",
    "profile_error_constraint_active",
];

const ASSIGNMENT_FIELDS: &[&str] = &[
    "run_id",
    "current_slot",
    "request_id",
    "qos_profile_id",
    "scheduled_slot",
    "flavour_name",
    "assigned_with_greedy_fallback",
    "flavour_duration_sec",
    "error",
    "carbon_cost",
    "arrival_slot",
    "deadline_slot",
];

// ─── MetricsLogger ───────────────────────────────────────────────────────────

pub struct MetricsLogger {
    pub enabled: bool,
    pub runs_file: String,
    pub assignments_file: String,
    lock: Mutex<()>,
}

impl MetricsLogger {
    pub fn new(enabled: bool, runs_file: String, assignments_file: String) -> Self {
        let logger = Self {
            enabled,
            runs_file,
            assignments_file,
            lock: Mutex::new(()),
        };
        if logger.enabled {
            ensure_header(&logger.runs_file, RUN_FIELDS);
            ensure_header(&logger.assignments_file, ASSIGNMENT_FIELDS);
        }
        logger
    }

    /// Log one solver run and the assignments committed by that run.
    ///
    /// Returns the generated `run_id` string (empty string when disabled).
    pub fn log_solver_run(
        &self,
        run_data: &HashMap<String, String>,
        assignment_rows: &[HashMap<String, String>],
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
        append_rows(
            &self.assignments_file,
            ASSIGNMENT_FIELDS,
            &augmented_assignments,
        );
        run_id
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
