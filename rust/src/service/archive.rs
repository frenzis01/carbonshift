//! Per-run JSONL storage for immutable request-history records.

use std::collections::HashSet;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use chrono::Utc;
use serde::{Deserialize, Serialize};

use crate::service::models::{AssignmentItem, TrackedRequestResponse};

/// One completed request and its final assignment as written to the run log.
#[derive(Debug, Serialize, Deserialize)]
pub struct ArchivedRequest {
    pub schema_version: u32,
    pub run_id: String,
    pub request: TrackedRequestResponse,
    pub assignment: Option<AssignmentItem>,
}

/// Append-only writer for one CarbonShift service run.
pub struct RequestArchive {
    path: PathBuf,
    run_id: String,
    /// Serializes append and append-once operations and tracks rows already
    /// committed during this process run.
    archived_ids: Mutex<HashSet<u64>>,
}

impl RequestArchive {
    /// Create a timestamped per-run JSONL archive under `directory`, failing
    /// before the HTTP server starts if the directory or run file cannot be
    /// created.
    pub fn open(directory: &Path) -> io::Result<Self> {
        fs::create_dir_all(directory)?;
        let started_at = Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
        let first_suffix = rand::random::<u8>();
        let mut run_directory = None;
        for offset in 0..=u8::MAX {
            let suffix = first_suffix.wrapping_add(offset);
            let run_id = format!("{started_at}-{suffix:02x}");
            let candidate = directory.join(&run_id);
            match fs::create_dir(&candidate) {
                Ok(()) => {
                    run_directory = Some((run_id, candidate));
                    break;
                }
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error),
            }
        }
        let (run_id, run_directory) = run_directory.ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::AlreadyExists,
                "all one-byte archive IDs are already used for this timestamp",
            )
        })?;
        let path = run_directory.join("requests.jsonl");
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)?;

        Ok(Self {
            path,
            run_id,
            archived_ids: Mutex::new(HashSet::new()),
        })
    }

    /// Stable identifier attached to every row in this archive.
    pub fn run_id(&self) -> &str {
        &self.run_id
    }

    /// Append exactly one JSON object followed by a newline.
    ///
    /// The lock covers the whole row so concurrent callbacks cannot interleave
    /// their serialized bytes. Errors are returned to the caller; callers must
    /// retain the live source record when this operation fails.
    pub fn append(&self, row: &ArchivedRequest) -> io::Result<()> {
        self.append_record(row, false).map(|_| ())
    }

    /// Append only if this request ID has not already been committed in this
    /// run. Returns `false` on a retry after a successful append.
    pub fn append_once(&self, row: &ArchivedRequest) -> io::Result<bool> {
        self.append_record(row, true)
    }

    fn append_record(&self, row: &ArchivedRequest, only_once: bool) -> io::Result<bool> {
        let mut archived_ids = self
            .archived_ids
            .lock()
            .map_err(|_| io::Error::other("archive lock poisoned"))?;

        let request_id = row.request.request_id;
        if only_once && archived_ids.contains(&request_id) {
            return Ok(false);
        }

        let mut bytes = serde_json::to_vec(row)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        bytes.push(b'\n');

        let mut file = OpenOptions::new()
            .append(true)
            .read(true)
            .open(&self.path)?;
        let original_len = file.metadata()?.len();

        if let Err(write_error) = file.write_all(&bytes).and_then(|()| file.sync_data()) {
            if let Err(rollback_error) = file.set_len(original_len).and_then(|()| file.sync_data())
            {
                return Err(io::Error::other(format!(
                    "archive append failed ({write_error}); rollback failed ({rollback_error})"
                )));
            }
            return Err(write_error);
        }

        archived_ids.insert(request_id);
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::service::models::RequestStatus;

    #[test]
    fn append_writes_one_jsonl_record_with_run_identity() {
        use std::time::{SystemTime, UNIX_EPOCH};

        let directory = std::env::temp_dir().join(format!(
            "carbonshift-archive-test-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let archive = RequestArchive::open(&directory).unwrap();
        let run_directory = archive.path.parent().unwrap();
        assert_eq!(
            run_directory.file_name().unwrap().to_string_lossy(),
            archive.run_id()
        );
        let suffix = archive.run_id().rsplit('-').next().unwrap();
        assert_eq!(suffix.len(), 2);
        assert!(u8::from_str_radix(suffix, 16).is_ok());
        let row = ArchivedRequest {
            schema_version: 1,
            run_id: archive.run_id().to_owned(),
            request: TrackedRequestResponse {
                request_id: 7,
                task: Some("text_generation".to_owned()),
                qos_profile_id: "default-text-generation".to_owned(),
                status: RequestStatus::Completed,
                arrival_slot: 0,
                scheduled_slot: Some(0),
                flavour: Some("Fast".to_owned()),
                carbon_cost: Some(1.0),
                actual_carbon_cost: Some(1.0),
                baseline_carbon_cost: 2.0,
                actual_baseline_carbon_cost: Some(2.0),
                actual_error_pct: Some(1.5),
                execution_time_sec: Some(1.0),
                baseline_execution_time_sec: Some(2.0),
                scheduled_at: Some(123.0),
                error: None,
            },
            assignment: None,
        };

        assert!(archive.append_once(&row).unwrap());
        assert!(!archive.append_once(&row).unwrap());

        let contents = fs::read_to_string(&archive.path).unwrap();
        assert_eq!(contents.lines().count(), 1);
        let written: ArchivedRequest = serde_json::from_str(contents.trim()).unwrap();
        assert_eq!(written.run_id, archive.run_id());
        assert_eq!(written.request.request_id, 7);

        drop(archive);
        fs::remove_dir_all(directory).unwrap();
    }
}
