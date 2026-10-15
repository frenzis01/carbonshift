from __future__ import annotations

import json
import stat

import pytest

from common.snapshots import (
    SnapshotError,
    export_emulation_snapshot,
    list_snapshots,
    load_snapshot,
    write_snapshot,
)


def test_shared_exporter_writes_matching_profile_views_and_client_summaries(
    monkeypatch,
    tmp_path,
):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_get(url, params=None, timeout=None):
        profile_id = (params or {}).get("qos_profile_id")
        if profile_id:
            return Response(
                {
                    "active_profiles": [{"profile_id": "profile-a"}],
                    "indicators": {"qos_profile_id": profile_id},
                }
            )
        return Response(
            {
                "active_profiles": [
                    {"profile_id": "profile-a"},
                    {"profile_id": "profile-b"},
                ],
                "indicators": {"qos_profile_id": None},
            }
        )

    monkeypatch.setattr("common.snapshots.requests.get", fake_get)
    record = export_emulation_snapshot(
        snapshots_dir=tmp_path,
        visualizer_url="http://visualizer:8500/",
        clients=[
            {"label": "_1", "task": "ner", "summary": {"completed": 3}},
            {"label": "_2", "task": "question_answering", "summary": {"completed": 2}},
        ],
        label="../../evening run",
    )

    snapshots = list_snapshots(tmp_path)
    assert len(snapshots) == 1
    assert snapshots[0]["snapshot_id"] == record["snapshot_id"]
    assert snapshots[0]["label"] == "../../evening run"
    assert ".." not in record["snapshot_filename"]
    assert record["summary_filename"].endswith("_summary.json")

    snapshot, summary = load_snapshot(tmp_path, record["snapshot_id"])
    assert set(snapshot["views"]["by_profile"]) == {"profile-a", "profile-b"}
    assert snapshot["views"]["by_profile"]["profile-a"]["indicators"]["qos_profile_id"] == "profile-a"
    assert [client["label"] for client in summary["clients"]] == ["_1", "_2"]
    assert summary["snapshot_id"] == snapshot["snapshot_id"]
    assert summary["captured_at_utc"] == snapshot["captured_at_utc"]
    assert summary["source"] == snapshot["source"] == "emulation"
    date_directory = tmp_path / record["captured_at_utc"][:10].replace("-", "")
    assert stat.S_IMODE((date_directory / record["snapshot_filename"]).stat().st_mode) == 0o666
    assert stat.S_IMODE((date_directory / record["summary_filename"]).stat().st_mode) == 0o666
    assert json.loads(
        (tmp_path / record["captured_at_utc"][:10].replace("-", "") / record["snapshot_filename"])
        .read_text(encoding="utf-8")
    )["label"] == "../../evening run"


def test_incomplete_snapshot_pair_is_not_listed_or_loadable(tmp_path):
    record = write_snapshot(
        snapshots_dir=tmp_path,
        views={"all": {"active_profiles": []}, "by_profile": {}},
        clients=[],
        label="incomplete",
    )
    summary_path = (
        tmp_path
        / record["captured_at_utc"][:10].replace("-", "")
        / record["summary_filename"]
    )
    summary_path.unlink()

    assert list_snapshots(tmp_path) == []
    with pytest.raises(FileNotFoundError):
        load_snapshot(tmp_path, record["snapshot_id"])


def test_snapshot_view_collection_rejects_missing_profile_ids():
    from common.snapshots import collect_dashboard_views

    with pytest.raises(SnapshotError, match="profile_id"):
        collect_dashboard_views(lambda _: {"active_profiles": [{"task_kind": "ner"}]})
