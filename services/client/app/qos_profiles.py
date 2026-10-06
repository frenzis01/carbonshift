"""Build stable, reusable QoS profiles from executor calibration results.

Both the client startup path and the profile-push CLI use these functions.
Keeping profile construction here ensures they register byte-for-byte
equivalent policy definitions for the same calibration snapshot.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import settings


DEFAULT_MODEL_STATS = Path(__file__).resolve().parent.parent / "model_stats.json"


def _latest_entries_by_task_flavour(stats: dict[str, dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """Keep only the most recently measured model for each task/flavour pair."""
    by_task: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for entry in stats.values():
        task = entry["task"]
        flavour = entry["flavour"]
        existing = by_task[task].get(flavour)
        if existing is None or entry.get("measured_at", "") > existing.get("measured_at", ""):
            by_task[task][flavour] = entry
    return by_task


def _flavour_definition(entry: dict[str, Any]) -> dict[str, Any]:
    """Convert a measured row to Carbonshift's duration-in-milliseconds API."""
    return {
        "name": entry["flavour"].capitalize(),
        "error": entry["error_pct"],
        "duration": max(round(entry["avg_execution_time_seconds"] * 1000), 1),
    }


def build_task_flavours(stats: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group calibration rows by task and convert each to a flavour definition."""
    by_task = _latest_entries_by_task_flavour(stats)
    return {
        task_id: [_flavour_definition(entry) for entry in entries.values()]
        for task_id, entries in by_task.items()
    }


def default_error_threshold(flavours: list[dict[str, Any]], position: float) -> float:
    """Interpolate between the minimum and maximum measured flavour errors."""
    if not flavours:
        raise ValueError("at least one calibrated flavour is required")
    if not 0.0 <= position <= 1.0:
        raise ValueError("threshold position must be between 0 and 1")
    errors = [flavour["error"] for flavour in flavours]
    return min(errors) + position * (max(errors) - min(errors))


def build_task_profiles(
    stats: dict[str, dict[str, Any]],
    threshold_position: float = 0.75,
    profile_version: str = "v1",
) -> dict[str, dict[str, Any]]:
    """Build complete immutable profile definitions from calibration data.

    Window and cumulative settings are explicit rather than inherited from
    whatever Carbonshift configuration happens to be running. This makes an
    identical registration reusable across clients and scheduler restarts.
    """
    allowed_version_characters = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
    if (
        not profile_version
        or profile_version.lower() != profile_version
        or any(character not in allowed_version_characters for character in profile_version)
    ):
        raise ValueError(
            "profile version must use lowercase ASCII letters, digits, '.', '_' or '-'"
        )

    profiles: dict[str, dict[str, Any]] = {}
    for task_id, entries_by_flavour in _latest_entries_by_task_flavour(stats).items():
        flavours = [_flavour_definition(entry) for entry in entries_by_flavour.values()]
        semantics = {
            entry["error_semantics"]
            for entry in entries_by_flavour.values()
            if entry.get("error_semantics")
        }
        if len(semantics) > 1:
            raise ValueError(f"task {task_id!r} has inconsistent calibrated error semantics")

        if semantics:
            error_semantics = next(iter(semantics))
        else:
            # Older calibration files predate this field. Only known tasks
            # have a safe legacy mapping; new task kinds must state semantics.
            error_semantics = {
                "text_generation": "relative-confidence-degradation-v1",
                "ner": "entity-set-f1-v1",
                "question_answering": "word-overlap-f1-v1",
            }.get(task_id)
        if error_semantics is None:
            raise ValueError(
                f"task {task_id!r} has no error_semantics; recalibrate or add the versioned metric ID"
            )

        profiles[task_id] = {
            "profile_id": f"{task_id}-calibrated-{profile_version}",
            "task_kind": task_id,
            "flavours": flavours,
            "error_semantics": error_semantics,
            "max_error_threshold": default_error_threshold(
                flavours,
                threshold_position,
            ),
            "error_window": {
                "past_slots": 12,
                "future_slots": 14,
                "past_decay_slots": 12,
            },
            "cumulative_error": {"enabled": True, "hard": True},
        }
    return profiles


def load_configured_profiles(
    stats_path: str | Path | None = None,
    definitions_path: str | Path | None = None,
    threshold_position: float | None = None,
    profile_version: str | None = None,
) -> list[dict[str, Any]]:
    """Load this client's profile catalog from configured local definitions.

    Calibrated profiles are reconstructed from model measurements. An
    optional JSON array adds caller-defined policies that are not derived
    from those models. If both sources contain an ID, their full definitions
    must match exactly. A missing file is allowed; a present but malformed or
    empty file is an error rather than a reason to silently change policy.
    """
    configured_by_id: dict[str, dict[str, Any]] = {}

    calibration_path = Path(stats_path or settings.qos_profile_stats_path)
    if calibration_path.exists():
        if not calibration_path.is_file():
            raise ValueError(f"calibration path is not a file: {calibration_path}")
        try:
            stats = json.loads(calibration_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot load calibration data from {calibration_path}: {exc}") from exc
        if not isinstance(stats, dict) or not stats:
            raise ValueError(
                f"calibration data in {calibration_path} must be a non-empty JSON object"
            )

        calibrated = build_task_profiles(
            stats,
            threshold_position=(
                settings.qos_profile_threshold_position
                if threshold_position is None
                else threshold_position
            ),
            profile_version=profile_version or settings.qos_profile_version,
        )
        configured_by_id.update(
            (profile["profile_id"], profile)
            for profile in calibrated.values()
        )

    custom_path_value = definitions_path or settings.qos_profile_definitions_path
    if custom_path_value is not None:
        custom_path = Path(custom_path_value)
        if not custom_path.is_file():
            raise ValueError(f"QoS profile definitions file does not exist: {custom_path}")
        try:
            custom_profiles = json.loads(custom_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot load QoS profile definitions from {custom_path}: {exc}") from exc
        if not isinstance(custom_profiles, list) or not custom_profiles:
            raise ValueError(
                f"QoS profile definitions in {custom_path} must be a non-empty JSON array"
            )
        for profile in custom_profiles:
            if not isinstance(profile, dict) or not isinstance(profile.get("profile_id"), str):
                raise ValueError(
                    f"every QoS profile in {custom_path} must be an object with a string profile_id"
                )
            profile_id = profile["profile_id"]
            existing = configured_by_id.get(profile_id)
            if existing is not None and existing != profile:
                raise ValueError(
                    f"QoS profile {profile_id!r} has conflicting local definitions"
                )
            configured_by_id[profile_id] = profile

    return [configured_by_id[profile_id] for profile_id in sorted(configured_by_id)]
