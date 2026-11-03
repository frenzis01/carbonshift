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

use crate::engine::qos::QosProfile;

/// Version of the CSV and JSON output schema consumed by the analysis tools.
pub const OUTPUT_SCHEMA_VERSION: u32 = 2;

// ─── field order ─────────────────────────────────────────────────────────────

const RUN_FIELDS: &[&str] = &[
    "run_id",
    "run_sequence",
    "solver_mode",
    "solver_status",
    "new_assignments",
    "fallback_assigned_count",
    "solver_elapsed_ms",
    "total_carbon_cost",
    "error_window_avg_after",
    "error_window_avg_after_real",
];

const ASSIGNMENT_FIELDS: &[&str] = &[
    "run_id",
    "current_slot",
    "request_id",
    "qos_profile_id",
    "scheduled_slot",
    "flavour_name",
    "fallback_assigned",
    "late_assigned",
    "carbon_cost",
    "arrival_slot",
    "deadline_slot",
];

const PROFILE_FIELDS: &[&str] = &[
    "profile_id",
    "task_kind",
    "error_semantics",
    "max_error_threshold",
    "error_window_past",
    "error_window_future",
    "error_window_past_decay_slots",
    "cumulative_error_enabled",
    "cumulative_error_hard",
];

const PROFILE_FLAVOUR_FIELDS: &[&str] = &["profile_id", "flavour_name", "error", "duration"];

/// Write a complete immutable profile catalog for a standalone batch run.
pub fn write_qos_profile_catalog(
    profiles_file: &str,
    profile_flavours_file: &str,
    profiles: &[QosProfile],
) {
    reset_header(profiles_file, PROFILE_FIELDS);
    reset_header(profile_flavours_file, PROFILE_FLAVOUR_FIELDS);
    for profile in profiles {
        let (profile_row, flavour_rows) = profile_rows(profile);
        append_rows(profiles_file, PROFILE_FIELDS, &[profile_row]);
        append_rows(profile_flavours_file, PROFILE_FLAVOUR_FIELDS, &flavour_rows);
    }

    fn reset_header(path: &str, fields: &[&str]) {
        if let Some(parent) = Path::new(path).parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent).expect("Cannot create CSV directory");
            }
        }
        let mut writer = csv::Writer::from_path(path).expect("Cannot create CSV file");
        writer
            .write_record(fields)
            .expect("Cannot write CSV header");
        writer.flush().expect("Cannot flush CSV header");
    }
}

// ─── MetricsLogger ───────────────────────────────────────────────────────────

pub struct MetricsLogger {
    pub enabled: bool,
    pub runs_file: String,
    pub assignments_file: String,
    pub profiles_file: String,
    pub profile_flavours_file: String,
    lock: Mutex<()>,
}

impl MetricsLogger {
    pub fn new(enabled: bool, runs_file: String, assignments_file: String) -> Self {
        Self::new_with_profile_files(
            enabled,
            runs_file,
            assignments_file,
            String::new(),
            String::new(),
        )
    }

    pub fn new_with_profile_files(
        enabled: bool,
        runs_file: String,
        assignments_file: String,
        profiles_file: String,
        profile_flavours_file: String,
    ) -> Self {
        let logger = Self {
            enabled,
            runs_file,
            assignments_file,
            profiles_file,
            profile_flavours_file,
            lock: Mutex::new(()),
        };
        if logger.enabled {
            ensure_header(&logger.runs_file, RUN_FIELDS);
            ensure_header(&logger.assignments_file, ASSIGNMENT_FIELDS);
            if !logger.profiles_file.is_empty() {
                ensure_header(&logger.profiles_file, PROFILE_FIELDS);
            }
            if !logger.profile_flavours_file.is_empty() {
                ensure_header(&logger.profile_flavours_file, PROFILE_FLAVOUR_FIELDS);
            }
        }
        logger
    }

    /// Record one immutable profile definition and its flavour rows.
    ///
    /// Call this only when a profile is first created, not when an identical
    /// idempotent registration is repeated.
    pub fn log_qos_profile(&self, profile: &QosProfile) {
        if !self.enabled {
            return;
        }
        assert!(
            !self.profiles_file.is_empty() && !self.profile_flavours_file.is_empty(),
            "QoS profile logging requires configured catalog paths"
        );

        let (profile_row, flavour_rows) = profile_rows(profile);

        let _guard = self.lock.lock().unwrap();
        append_rows(&self.profiles_file, PROFILE_FIELDS, &[profile_row]);
        append_rows(
            &self.profile_flavours_file,
            PROFILE_FLAVOUR_FIELDS,
            &flavour_rows,
        );
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

fn profile_rows(profile: &QosProfile) -> (HashMap<String, String>, Vec<HashMap<String, String>>) {
    let profile_row = HashMap::from([
        ("profile_id".to_owned(), profile.profile_id.to_string()),
        ("task_kind".to_owned(), profile.task_kind.to_string()),
        (
            "error_semantics".to_owned(),
            profile.error_semantics.clone(),
        ),
        (
            "max_error_threshold".to_owned(),
            profile.max_error_threshold.to_string(),
        ),
        (
            "error_window_past".to_owned(),
            profile.error_window.past_slots.to_string(),
        ),
        (
            "error_window_future".to_owned(),
            profile.error_window.future_slots.to_string(),
        ),
        (
            "error_window_past_decay_slots".to_owned(),
            profile.error_window.past_decay_slots.to_string(),
        ),
        (
            "cumulative_error_enabled".to_owned(),
            profile.cumulative_error.enabled.to_string(),
        ),
        (
            "cumulative_error_hard".to_owned(),
            profile.cumulative_error.hard.to_string(),
        ),
    ]);
    let flavour_rows = profile
        .flavours
        .iter()
        .map(|flavour| {
            HashMap::from([
                ("profile_id".to_owned(), profile.profile_id.to_string()),
                ("flavour_name".to_owned(), flavour.name.clone()),
                ("error".to_owned(), flavour.error.to_string()),
                ("duration".to_owned(), flavour.duration.to_string()),
            ])
        })
        .collect();
    (profile_row, flavour_rows)
}

fn unix_now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

/// Generate a run_id from the process-local sequence plus a wall-clock nonce.
fn run_id_from_data(data: &HashMap<String, String>) -> String {
    let seq = data.get("run_sequence").map(|s| s.as_str()).unwrap_or("0");
    let ts = unix_now_ms();
    format!("run_n{seq}_{ts}")
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
