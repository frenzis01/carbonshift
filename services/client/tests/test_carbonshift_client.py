"""HTTP contract tests for QoS profile selection and registration."""
from __future__ import annotations

from app import carbonshift_client
from app.config import settings


class FakeResponse:
    def __init__(self, status_code: int, body=None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        return self._body


def test_submit_sends_task_kind_and_profile_as_distinct_fields(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return FakeResponse(202, {"request_id": 7, "qos_profile_id": "qa-calibrated-v1"})

    monkeypatch.setattr(carbonshift_client.requests, "post", fake_post)
    monkeypatch.setattr(settings, "carbonshift_api_key", "caller-key")

    response = carbonshift_client.submit(
        60.0,
        "http://client/callback",
        {"task": "question_answering", "input": {"question": "q"}},
        qos_profile_id="qa-calibrated-v1",
        task_kind="question_answering",
    )

    body = captured["json"]
    assert response["qos_profile_id"] == "qa-calibrated-v1"
    assert body["qos_profile_id"] == "qa-calibrated-v1"
    assert body["task_kind"] == "question_answering"
    assert "task_id" not in body
    assert captured["headers"] == {"X-API-Key": "caller-key"}


def test_submit_restores_known_profile_once_after_scheduler_restart(monkeypatch):
    profile = {
        "profile_id": "restart-recovery-v1",
        "task_kind": "question_answering",
        "flavours": [{"name": "Accurate", "error": 4.0, "duration": 120}],
        "error_semantics": "word-overlap-f1-v1",
        "max_error_threshold": 10.0,
        "error_window": {"past_slots": 12, "future_slots": 14, "past_decay_slots": 12},
        "cumulative_error": {"enabled": True, "hard": True},
    }
    calls = []
    request_responses = iter([
        FakeResponse(404, text='{"error":"unknown QoS profile"}'),
        FakeResponse(202, {"request_id": 88, "qos_profile_id": "restart-recovery-v1"}),
    ])

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/v1/profiles"):
            return FakeResponse(204)
        return next(request_responses)

    monkeypatch.setattr(carbonshift_client.requests, "post", fake_post)
    carbonshift_client.register_qos_profile(profile)
    calls.clear()

    ack = carbonshift_client.submit(
        30.0,
        "http://client/callback",
        {"task": "question_answering", "input": {"question": "q"}},
        qos_profile_id="restart-recovery-v1",
        task_kind="question_answering",
    )

    assert ack["request_id"] == 88
    assert [url.rsplit("/", 1)[-1] for url, _ in calls] == [
        "requests",
        "profiles",
        "requests",
    ]
    assert calls[0][1]["json"] == calls[2][1]["json"]


def test_submit_does_not_guess_a_definition_for_an_unknown_profile(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return FakeResponse(404, text='{"error":"unknown QoS profile"}')

    monkeypatch.setattr(carbonshift_client.requests, "post", fake_post)

    try:
        carbonshift_client.submit(
            30.0,
            "http://client/callback",
            {"task": "question_answering", "input": {"question": "q"}},
            qos_profile_id="not-in-this-client-catalog-v1",
            task_kind="question_answering",
        )
    except carbonshift_client.CarbonshiftError as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("unknown profiles must be reported, not replaced with defaults")

    assert len(calls) == 1


def test_profile_helpers_use_the_protected_profile_endpoints(monkeypatch):
    profile = {"profile_id": "qa-calibrated-v1", "task_kind": "question_answering"}
    calls = []

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        return FakeResponse(204)

    def fake_get(url, **kwargs):
        calls.append(("GET", url, kwargs))
        return FakeResponse(200, [profile] if url.endswith("/v1/profiles") else profile)

    monkeypatch.setattr(carbonshift_client.requests, "post", fake_post)
    monkeypatch.setattr(carbonshift_client.requests, "get", fake_get)
    monkeypatch.setattr(settings, "carbonshift_api_key", "caller-key")

    carbonshift_client.register_qos_profile(profile)
    assert carbonshift_client.list_qos_profiles() == [profile]
    assert carbonshift_client.get_qos_profile("qa-calibrated-v1") == profile

    assert [call[1] for call in calls] == [
        f"{settings.carbonshift_url}/v1/profiles",
        f"{settings.carbonshift_url}/v1/profiles",
        f"{settings.carbonshift_url}/v1/profiles/qa-calibrated-v1",
    ]
    assert all(call[2]["headers"] == {"X-API-Key": "caller-key"} for call in calls)


def test_force_set_capacity_tiers_uses_protected_global_endpoint(monkeypatch):
    captured = {}

    def fake_put(url, **kwargs):
        captured.update(url=url, **kwargs)
        return FakeResponse(204)

    monkeypatch.setattr(carbonshift_client.requests, "put", fake_put)
    monkeypatch.setattr(settings, "carbonshift_api_key", "caller-key")
    tiers = [
        {"max_requests": 4, "multiplier": 1.0},
        {"max_requests": 6, "multiplier": 1.5},
        {"max_requests": None, "multiplier": 5.0},
    ]

    carbonshift_client.force_set_global_capacity_tiers(tiers)

    assert captured["url"] == f"{settings.carbonshift_url}/v1/admin/capacity-tiers"
    assert captured["json"] == {"capacity_tiers": tiers}
    assert captured["headers"] == {"X-API-Key": "caller-key"}
