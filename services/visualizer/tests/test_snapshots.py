from __future__ import annotations

from fastapi.testclient import TestClient

from app import data
from app import main as main_module
from common.snapshots import load_snapshot, write_snapshot


def _views():
    all_view = {
        "active_profiles": [
            {"profile_id": "profile-a", "task_kind": "ner"},
            {"profile_id": "profile-b", "task_kind": "question_answering"},
        ],
        "indicators": {"qos_profile_id": None},
    }
    return {
        "all": all_view,
        "by_profile": {
            profile["profile_id"]: {
                "active_profiles": all_view["active_profiles"],
                "indicators": {"qos_profile_id": profile["profile_id"]},
            }
            for profile in all_view["active_profiles"]
        },
    }


def test_snapshot_api_preserves_profile_selection_and_summary_download(monkeypatch, tmp_path):
    monkeypatch.setattr(main_module.settings, "snapshots_dir", tmp_path)
    record = write_snapshot(
        snapshots_dir=tmp_path,
        views=_views(),
        clients=[{"label": "_1", "task": "ner", "summary": {"completed": 4}}],
        label="named run",
    )

    with TestClient(main_module.app) as client:
        listed = client.get("/api/snapshots")
        profile_data = client.get(
            "/api/data",
            params={"snapshot_id": record["snapshot_id"], "qos_profile_id": "profile-a"},
        )
        summary = client.get(f"/api/snapshots/{record['snapshot_id']}/summary")

    assert listed.status_code == 200
    assert listed.json()[0]["snapshot_id"] == record["snapshot_id"]
    assert profile_data.status_code == 200
    assert profile_data.json()["indicators"]["qos_profile_id"] == "profile-a"
    assert summary.status_code == 200
    assert summary.json()["clients"][0]["label"] == "_1"


def test_visualizer_export_uses_shared_store_and_can_copy_a_saved_snapshot(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(main_module.settings, "snapshots_dir", tmp_path)

    def fake_dashboard_data(qos_profile_id=None):
        views = _views()
        return views["all"] if qos_profile_id is None else views["by_profile"][qos_profile_id]

    monkeypatch.setattr(main_module, "get_dashboard_data", fake_dashboard_data)
    with TestClient(main_module.app) as client:
        live_export = client.post("/api/snapshots", json={"label": "live checkpoint"})
        first_id = live_export.json()["snapshot_id"]
        copied_export = client.post(
            "/api/snapshots",
            json={"label": "renamed checkpoint", "source_snapshot_id": first_id},
        )

    assert live_export.status_code == 201
    assert copied_export.status_code == 201
    source_id = copied_export.json()["snapshot_id"]
    copied, summary = load_snapshot(tmp_path, source_id)
    assert copied["label"] == "renamed checkpoint"
    assert copied["source_snapshot_id"] == first_id
    assert copied["views"]["by_profile"]["profile-b"]["indicators"]["qos_profile_id"] == "profile-b"
    assert summary["clients"] == []


def test_visualizer_remains_available_without_carbonshift(monkeypatch, tmp_path):
    monkeypatch.setattr(main_module.settings, "snapshots_dir", tmp_path)
    monkeypatch.setattr(
        data,
        "_cache",
        {
            "scheduler_requests": [],
            "capacity_tiers": [],
            "active_profiles": [],
            "profiles_unavailable_logged": False,
            "carbon_ci_list": [],
            "error_metrics_by_profile": {},
        },
    )
    monkeypatch.setattr(data, "fetch_json", lambda *_args, **_kwargs: None)

    with TestClient(main_module.app) as client:
        health = client.get("/health")
        live_data = client.get("/api/data")
        page = client.get("/")
        snapshots = client.get("/api/snapshots")

    assert health.status_code == 200
    assert live_data.status_code == 200
    assert live_data.json()["indicators"]["total_requests"] == 0
    assert page.status_code == 200
    assert snapshots.status_code == 200
    assert snapshots.json() == []
    assert "Choose snapshot" in page.text
    assert "syncRefreshTimer" in page.text


def test_snapshot_export_status_is_a_separate_header_row_and_clears_on_source_switch():
    with TestClient(main_module.app) as client:
        page = client.get("/").text

    header = page.split("<header>", 1)[1].split("</header>", 1)[0]
    assert header.index('<span id="snapshot-status"') > header.index('<div class="controls">')
    assert "flex-basis: 100%;" in page
    assert "dataSourceGeneration += 1;" in page
    assert "document.getElementById('snapshot-status').textContent = '';" in page
