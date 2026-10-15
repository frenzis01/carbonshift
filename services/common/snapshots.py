"""Versioned, paired snapshot persistence shared by emulation and visualizer."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

SNAPSHOT_FORMAT_VERSION = 1
_SNAPSHOT_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


class SnapshotError(Exception):
    """Raised when a snapshot cannot be captured, read, or validated."""


def default_snapshot_directory() -> Path:
    """Resolve the shared snapshot directory from the service environment."""
    configured_directory = os.environ.get("SNAPSHOTS_DIR")
    if configured_directory:
        return Path(configured_directory)
    return Path(__file__).resolve().parents[2] / "snapshots"


def collect_dashboard_views(
    fetch_view: Callable[[str | None], dict[str, Any]],
) -> dict[str, Any]:
    """Capture the all-profile view and every active profile's filtered view."""
    all_profiles_view = fetch_view(None)
    if not isinstance(all_profiles_view, dict):
        raise SnapshotError("the dashboard all-profile response must be a JSON object")

    active_profiles = all_profiles_view.get("active_profiles", [])
    if not isinstance(active_profiles, list):
        raise SnapshotError("the dashboard active_profiles field must be a list")

    profile_views: dict[str, dict[str, Any]] = {}
    for profile in active_profiles:
        if not isinstance(profile, dict):
            raise SnapshotError("each active QoS profile must be a JSON object")
        profile_id = profile.get("profile_id")
        if not isinstance(profile_id, str) or not profile_id:
            raise SnapshotError("each active QoS profile must have a non-empty profile_id")
        if profile_id not in profile_views:
            view = fetch_view(profile_id)
            if not isinstance(view, dict):
                raise SnapshotError(
                    f"the dashboard response for QoS profile {profile_id!r} must be a JSON object"
                )
            profile_views[profile_id] = view

    return {"all": all_profiles_view, "by_profile": profile_views}


def fetch_dashboard_views(visualizer_url: str, timeout: float = 30.0) -> dict[str, Any]:
    """Fetch dashboard views from the visualizer for an emulation export."""
    base_url = visualizer_url.rstrip("/")

    def fetch_view(profile_id: str | None) -> dict[str, Any]:
        params = {"qos_profile_id": profile_id} if profile_id else None
        try:
            response = requests.get(
                f"{base_url}/api/data",
                params=params,
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            profile_context = f" for profile {profile_id!r}" if profile_id else ""
            raise SnapshotError(
                f"could not capture visualizer dashboard data{profile_context}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise SnapshotError("the visualizer dashboard response must be a JSON object")
        return payload

    return collect_dashboard_views(fetch_view)


def export_emulation_snapshot(
    snapshots_dir: Path,
    visualizer_url: str,
    clients: list[dict[str, Any]],
    label: str | None = None,
) -> dict[str, str]:
    """Capture dashboard views and persist them with labelled client summaries."""
    views = fetch_dashboard_views(visualizer_url)
    return write_snapshot(
        snapshots_dir=snapshots_dir,
        views=views,
        clients=clients,
        label=label,
        source="emulation",
    )


def write_snapshot(
    snapshots_dir: Path,
    views: dict[str, Any],
    clients: list[dict[str, Any]],
    label: str | None = None,
    source: str = "visualizer",
    source_snapshot_id: str | None = None,
) -> dict[str, str]:
    """Atomically publish a snapshot/summary pair; the snapshot file is the commit marker."""
    _validate_views(views)
    if not isinstance(clients, list) or any(not isinstance(client, dict) for client in clients):
        raise SnapshotError("snapshot client summaries must be a list of JSON objects")
    if source_snapshot_id is not None and not _SNAPSHOT_ID_PATTERN.fullmatch(source_snapshot_id):
        raise SnapshotError("source_snapshot_id must be a 32-character lowercase hexadecimal ID")

    captured_at = datetime.now(timezone.utc)
    captured_at_text = captured_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    snapshot_id = uuid.uuid4().hex
    date_directory = snapshots_dir / captured_at.strftime("%Y%m%d")
    safe_label = _filename_label(label)
    stem = f"{captured_at.strftime('%H%M_%S')}_{safe_label}_{snapshot_id}"
    snapshot_path = date_directory / f"{stem}_snapshot.json"
    summary_path = date_directory / f"{stem}_summary.json"

    snapshot_document: dict[str, Any] = {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "snapshot_id": snapshot_id,
        "captured_at_utc": captured_at_text,
        "label": label.strip() if isinstance(label, str) and label.strip() else None,
        "source": source,
        "views": views,
    }
    if source_snapshot_id is not None:
        snapshot_document["source_snapshot_id"] = source_snapshot_id
    summary_document = {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "snapshot_id": snapshot_id,
        "captured_at_utc": captured_at_text,
        "source": source,
        "clients": clients,
    }

    date_directory.mkdir(parents=True, exist_ok=True)
    temporary_files: list[Path] = []
    try:
        summary_temporary = _write_temporary_json(summary_path, summary_document)
        temporary_files.append(summary_temporary)
        snapshot_temporary = _write_temporary_json(snapshot_path, snapshot_document)
        temporary_files.append(snapshot_temporary)
        os.replace(summary_temporary, summary_path)
        temporary_files.remove(summary_temporary)
        os.replace(snapshot_temporary, snapshot_path)
        temporary_files.remove(snapshot_temporary)
    finally:
        for temporary_path in temporary_files:
            temporary_path.unlink(missing_ok=True)

    return {
        "snapshot_id": snapshot_id,
        "captured_at_utc": captured_at_text,
        "label": snapshot_document["label"] or "",
        "source": source,
        "snapshot_filename": snapshot_path.name,
        "summary_filename": summary_path.name,
    }


def list_snapshots(snapshots_dir: Path) -> list[dict[str, Any]]:
    """Return only valid snapshots with a matching, valid summary sidecar."""
    if not snapshots_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for snapshot_path in snapshots_dir.rglob("*_snapshot.json"):
        try:
            snapshot, summary = _read_snapshot_pair(snapshot_path)
        except (OSError, SnapshotError) as exc:
            logger.warning("ignoring incomplete or invalid snapshot %s: %s", snapshot_path, exc)
            continue
        records.append({
            "snapshot_id": snapshot["snapshot_id"],
            "captured_at_utc": snapshot["captured_at_utc"],
            "label": snapshot.get("label") or "",
            "source": snapshot.get("source", "unknown"),
            "clients_count": len(summary["clients"]),
            "snapshot_filename": snapshot_path.name,
            "summary_filename": _summary_path(snapshot_path).name,
        })
    records.sort(key=lambda record: record["captured_at_utc"], reverse=True)
    return records


def load_snapshot(snapshots_dir: Path, snapshot_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load a complete paired snapshot by its opaque ID."""
    if not _SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id):
        raise FileNotFoundError("snapshot not found")
    matches = list(snapshots_dir.rglob(f"*_{snapshot_id}_snapshot.json"))
    if not matches:
        raise FileNotFoundError("snapshot not found")
    if not _summary_path(matches[0]).is_file():
        raise FileNotFoundError("complete snapshot not found")
    snapshot, summary = _read_snapshot_pair(matches[0])
    return snapshot, summary


def _validate_views(views: Any) -> None:
    if not isinstance(views, dict) or not isinstance(views.get("all"), dict):
        raise SnapshotError("snapshot views must contain an all-profile JSON object")
    if not isinstance(views.get("by_profile"), dict):
        raise SnapshotError("snapshot views must contain a by_profile JSON object")
    if any(not isinstance(view, dict) for view in views["by_profile"].values()):
        raise SnapshotError("each profile-specific snapshot view must be a JSON object")


def _filename_label(label: str | None) -> str:
    if not isinstance(label, str) or not label.strip():
        return "snapshot"
    return re.sub(r"[^A-Za-z0-9_-]+", "-", label.strip()).strip("-_")[:48] or "snapshot"


def _summary_path(snapshot_path: Path) -> Path:
    return snapshot_path.with_name(snapshot_path.name.removesuffix("_snapshot.json") + "_summary.json")


def _write_temporary_json(target: Path, payload: dict[str, Any]) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary_path = Path(temporary_name)
    completed = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            json.dump(payload, temporary_file, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            os.fchmod(temporary_file.fileno(), 0o666)
            os.fsync(temporary_file.fileno())
        completed = True
    finally:
        if not completed:
            temporary_path.unlink(missing_ok=True)
    return temporary_path


def _read_snapshot_pair(snapshot_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    summary_path = _summary_path(snapshot_path)
    if not summary_path.is_file():
        raise SnapshotError("matching summary file is missing")
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"paired JSON is invalid: {exc}") from exc
    if not isinstance(snapshot, dict) or not isinstance(summary, dict):
        raise SnapshotError("paired snapshot and summary files must contain JSON objects")
    snapshot_id = snapshot.get("snapshot_id")
    if (
        snapshot.get("format_version") != SNAPSHOT_FORMAT_VERSION
        or summary.get("format_version") != SNAPSHOT_FORMAT_VERSION
        or not isinstance(snapshot_id, str)
        or not _SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id)
        or summary.get("snapshot_id") != snapshot_id
    ):
        raise SnapshotError("snapshot and summary metadata do not form a supported pair")
    _validate_views(snapshot.get("views"))
    if not isinstance(summary.get("clients"), list):
        raise SnapshotError("summary clients must be a list")
    return snapshot, summary
