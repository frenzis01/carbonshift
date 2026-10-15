from __future__ import annotations

import sys

import pytest

from scripts import run_emulation_multi
from scripts.run_emulation_multi import (
    DEFAULT_CLIENT_URLS,
    DEFAULT_TASKS,
    _resolve_targets,
    build_capacity_tiers,
)


def test_default_targets_pair_ner_and_qa_with_two_clients():
    targets = _resolve_targets(None, None, None)

    assert [(target.client_url, target.task) for target in targets] == list(
        zip(DEFAULT_CLIENT_URLS, DEFAULT_TASKS)
    )
    assert [target.qos_profile_id for target in targets] == [None, None]


def test_additional_clients_are_configuration_not_new_runner_branches():
    targets = _resolve_targets(
        ["http://client-a:8100", "http://client-b:8100", "http://client-c:8100"],
        ["ner", "question_answering", "text_generation"],
        ["ner-profile", "qa-profile", "text-profile"],
    )

    assert [target.task for target in targets] == [
        "ner",
        "question_answering",
        "text_generation",
    ]
    assert [target.qos_profile_id for target in targets] == [
        "ner-profile",
        "qa-profile",
        "text-profile",
    ]


def test_single_client_can_be_selected_without_explicit_task_url_pairing():
    targets = _resolve_targets(["http://localhost:8100/"], None, None)

    assert [(target.client_url, target.task) for target in targets] == [
        (DEFAULT_CLIENT_URLS[0], DEFAULT_TASKS[0])
    ]


@pytest.mark.parametrize(
    ("client_urls", "tasks", "profile_ids", "message"),
    [
        (["http://a:8100", "http://b:8100"], ["ner"], None, "one --task"),
        (["http://a:8100"], ["ner"], ["p1", "p2"], "one --qos-profile-id"),
        (["http://a:8100", "http://a:8100/"], ["ner", "question_answering"], None, "unique"),
    ],
)
def test_invalid_client_task_configuration_is_rejected(
    client_urls, tasks, profile_ids, message
):
    with pytest.raises(ValueError, match=message):
        _resolve_targets(client_urls, tasks, profile_ids)


def test_runner_submits_a_separate_task_plan_to_each_client(monkeypatch):
    submissions = []
    generated_tasks = []
    tier_updates = []
    exported_snapshots = []

    class Response:
        status_code = 200

        def __init__(self, body=None):
            self.body = body or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    def fake_build_requests(task, *args, **kwargs):
        generated_tasks.append((task, args[4], kwargs["qos_profile_id"]))
        return [{"task": task}]

    def fake_post(url, json=None, timeout=None):
        submissions.append((url, json))
        return Response({"current_slot": 1, "plan_id": len(submissions)})

    def fake_get(url, timeout=None, params=None):
        if url.endswith("/v1/slot"):
            return Response(
                {
                    "current_slot": 0,
                    "slot_start_utc": "2025-01-01T00:00:00+00:00",
                }
            )
        return Response({"task": url})

    monkeypatch.setattr(sys, "argv", [
        "run_emulation_multi.py",
        "--client-url", "http://client-a:8100",
        "--client-url", "http://client-b:8100",
        "--task", "ner",
        "--task", "question_answering",
        "--slots", "1",
        "--per-slot", "1",
        "--seed", "10",
        "--timeout", "0",
        "--no-register",
    ])
    monkeypatch.setattr(run_emulation_multi, "build_requests", fake_build_requests)
    monkeypatch.setattr(
        run_emulation_multi,
        "force_set_global_capacity_tiers",
        tier_updates.append,
    )
    monkeypatch.setattr(run_emulation_multi.requests, "post", fake_post)
    monkeypatch.setattr(run_emulation_multi.requests, "get", fake_get)
    monkeypatch.setattr(
        run_emulation_multi,
        "export_emulation_snapshot",
        lambda **kwargs: exported_snapshots.append(kwargs) or {"snapshot_filename": "snapshot.json"},
    )
    monkeypatch.setattr(run_emulation_multi.time, "sleep", lambda _: None)

    run_emulation_multi.main()

    plan_submissions = [
        (url, body)
        for url, body in submissions
        if url.endswith("/run/send-plan")
    ]
    assert [url for url, _ in plan_submissions] == [
        "http://client-a:8100/run/send-plan",
        "http://client-b:8100/run/send-plan",
    ]
    assert [body["requests"][0]["task"] for _, body in plan_submissions] == [
        "ner",
        "question_answering",
    ]
    assert generated_tasks == [("ner", 10, None), ("question_answering", 11, None)]
    assert tier_updates == [build_capacity_tiers(2)]
    assert [client["label"] for client in exported_snapshots[0]["clients"]] == ["_1", "_2"]
    assert [client["task"] for client in exported_snapshots[0]["clients"]] == [
        "ner",
        "question_answering",
    ]
