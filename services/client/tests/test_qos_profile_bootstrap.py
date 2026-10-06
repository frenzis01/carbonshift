"""Client startup must restore the same calibrated profiles after restarts."""
from __future__ import annotations

import json

import pytest

from app import qos_profile_bootstrap
from app.carbonshift_client import CarbonshiftError
from app.config import settings


def _write_calibration(path):
    path.write_text(json.dumps({
        "qa-accurate": {
            "task": "question_answering",
            "flavour": "accurate",
            "error_pct": 4.0,
            "avg_execution_time_seconds": 0.3,
            "error_semantics": "word-overlap-f1-v1",
        },
        "qa-fast": {
            "task": "question_answering",
            "flavour": "fast",
            "error_pct": 20.0,
            "avg_execution_time_seconds": 0.05,
            "error_semantics": "word-overlap-f1-v1",
        },
    }))


def test_startup_registers_stable_profiles_and_retries_transient_errors(tmp_path, monkeypatch):
    stats_path = tmp_path / "model_stats.json"
    _write_calibration(stats_path)
    monkeypatch.setattr(settings, "qos_profile_stats_path", str(stats_path))
    monkeypatch.setattr(settings, "qos_profile_definitions_path", None)
    monkeypatch.setattr(settings, "qos_profile_version", "v3")
    monkeypatch.setattr(settings, "qos_profile_registration_attempts", 3)
    monkeypatch.setattr(settings, "qos_profile_registration_retry_seconds", 0.25)

    calls = []
    delays = []

    def register(profile):
        calls.append(profile)
        if len(calls) == 1:
            raise CarbonshiftError("Carbonshift is still starting")

    registered = qos_profile_bootstrap.register_configured_qos_profiles(
        register=register,
        sleep=delays.append,
    )

    assert registered == ["question_answering-calibrated-v3"]
    assert [profile["profile_id"] for profile in calls] == [
        "question_answering-calibrated-v3",
        "question_answering-calibrated-v3",
    ]
    assert calls[0] == calls[1]
    assert delays == [0.25]


def test_missing_calibration_file_keeps_builtin_defaults_available(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings,
        "qos_profile_stats_path",
        str(tmp_path / "not-present.json"),
    )
    monkeypatch.setattr(settings, "qos_profile_definitions_path", None)
    calls = []

    registered = qos_profile_bootstrap.register_configured_qos_profiles(
        register=calls.append,
        sleep=lambda _: pytest.fail("missing calibration file must not retry"),
    )

    assert registered == []
    assert calls == []


def test_startup_adds_explicit_shared_profiles_to_the_local_recovery_catalog(tmp_path, monkeypatch):
    definitions_path = tmp_path / "profiles.json"
    definition = {
        "profile_id": "qa-shared-v2",
        "task_kind": "question_answering",
        "flavours": [{"name": "Accurate", "error": 4.0, "duration": 120}],
        "error_semantics": "word-overlap-f1-v1",
        "max_error_threshold": 10.0,
        "error_window": {"past_slots": 12, "future_slots": 14, "past_decay_slots": 12},
        "cumulative_error": {"enabled": True, "hard": True},
    }
    definitions_path.write_text(json.dumps([definition]))
    monkeypatch.setattr(settings, "qos_profile_stats_path", str(tmp_path / "missing.json"))
    monkeypatch.setattr(settings, "qos_profile_definitions_path", str(definitions_path))

    registered = []
    ids = qos_profile_bootstrap.register_configured_qos_profiles(register=registered.append)

    assert ids == ["qa-shared-v2"]
    assert registered == [definition]


def test_conflicting_custom_profile_and_calibration_id_is_rejected(tmp_path, monkeypatch):
    stats_path = tmp_path / "model_stats.json"
    definitions_path = tmp_path / "profiles.json"
    _write_calibration(stats_path)
    conflicting_definition = {
        "profile_id": "question_answering-calibrated-v1",
        "task_kind": "question_answering",
        "flavours": [],
        "error_semantics": "word-overlap-f1-v1",
        "max_error_threshold": 1.0,
        "error_window": {"past_slots": 1, "future_slots": 1, "past_decay_slots": 0},
        "cumulative_error": {"enabled": True, "hard": True},
    }
    definitions_path.write_text(json.dumps([conflicting_definition]))
    monkeypatch.setattr(settings, "qos_profile_stats_path", str(stats_path))
    monkeypatch.setattr(settings, "qos_profile_definitions_path", str(definitions_path))

    with pytest.raises(ValueError, match="conflicting local definitions"):
        qos_profile_bootstrap.register_configured_qos_profiles(
            register=lambda _: pytest.fail("conflicting local policy must fail before HTTP"),
        )


def test_profile_conflict_fails_startup_without_retrying(tmp_path, monkeypatch):
    stats_path = tmp_path / "model_stats.json"
    _write_calibration(stats_path)
    monkeypatch.setattr(settings, "qos_profile_stats_path", str(stats_path))
    monkeypatch.setattr(settings, "qos_profile_registration_attempts", 5)

    calls = []

    def register(profile):
        calls.append(profile)
        raise CarbonshiftError("profile ID was registered with another definition", status_code=409)

    with pytest.raises(CarbonshiftError, match="could not restore QoS profile") as exc:
        qos_profile_bootstrap.register_configured_qos_profiles(
            register=register,
            sleep=lambda _: pytest.fail("immutable profile conflicts are not transient"),
        )

    assert exc.value.status_code == 409
    assert len(calls) == 1
