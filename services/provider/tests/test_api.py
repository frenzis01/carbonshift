"""HTTP contract tests for the provider, via in-process TestClient.

Mirrors the executor/client approach: no listening socket, no external
service. `requests.post` is monkeypatched so the fan-out never touches the
network.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture()
def client(monkeypatch):
    # The provider's module-level clock is shared across tests; re-create it
    # per test so slot assertions are independent.
    from app.clock import ProviderClock
    from app.slots import parse_epoch

    main.clock = ProviderClock(
        manual=True,
        slot_minutes=30,
        epoch=parse_epoch(main.settings.epoch_iso),
    )
    # The desync latch is module-level too: reset it, or one test's induced
    # failure would make every later test see a permanently desynced provider.
    main._desynced = None
    main.observed_readings.clear()
    yield TestClient(main.app)


class _Resp:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = ""


def test_health_reports_the_active_source(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["source"]["source"] == "local"
    assert body["manual_clock"] is True


def test_meta_describes_the_effective_configuration(client):
    body = client.get("/v1/meta").json()
    assert body["role"] == "local"
    assert body["source"] == "local"
    assert body["slot_minutes"] == 30
    assert body["forecast_horizon_slots"] == 24
    assert body["manual_clock"] is True
    assert body["auto_advance_clock"] is False  # disabled by conftest


def test_meta_lists_the_peer_order(client):
    """The client is first because it produces the slot's work; the consumers
    follow. See notifications.Role."""
    peers = client.get("/v1/meta").json()["peers"]
    assert [p["name"] for p in peers] == ["client", "carbonshift"]
    assert peers[0]["order"] < peers[1]["order"]


def test_slot_endpoint_returns_a_consistent_view(client):
    body = client.get("/v1/slot").json()
    assert body["manual_clock"] is True
    assert body["slot_minutes"] == 30
    assert body["local_step"] == 0


def test_forecast_defaults_to_the_configured_horizon(client):
    body = client.get("/v1/forecast").json()
    assert len(body["points"]) == 24


def test_forecast_starts_at_the_current_slot_by_default(client):
    current = client.get("/v1/slot").json()["current_slot"]
    body = client.get("/v1/forecast").json()
    assert body["points"][0]["slot"] == current


def test_forecast_honours_an_explicit_window(client):
    body = client.get("/v1/forecast", params={"from_slot": 500, "count": 5}).json()
    assert [p["slot"] for p in body["points"]] == [500, 501, 502, 503, 504]


def test_forecast_never_exposes_an_actual(client):
    """A forecast window is a prediction, not a measurement."""
    body = client.get("/v1/forecast", params={"count": 5}).json()
    assert all(set(p) == {"slot", "forecast"} for p in body["points"])
    assert "provides_actual_for_future_slots" not in body


def test_forecast_rejects_an_absurd_count(client):
    assert client.get("/v1/forecast", params={"count": 100000}).status_code == 422


def test_observed_is_unknown_before_any_slot_is_reached(client):
    # The provider starts frozen at the current slot without having observed.
    body = client.get("/v1/observed").json()
    assert body["known"] is False


def test_observed_is_unknown_for_a_future_slot(client):
    current = client.get("/v1/slot").json()["current_slot"]
    body = client.get("/v1/observed", params={"slot": current + 5}).json()
    assert body["known"] is False


def test_advance_records_a_reading_for_the_new_slot(client, monkeypatch):
    monkeypatch.setattr(main.notifications.requests, "post", lambda *a, **k: _Resp(200))
    client.post("/v1/advance-slot", json={})

    current = client.get("/v1/slot").json()["current_slot"]
    body = client.get("/v1/observed").json()
    assert body["known"] is True
    assert body["slot"] == current
    assert body["observed_at_slot"] == current


def test_observed_reading_deviates_from_the_forecast(client, monkeypatch):
    """The emulated measurement must drift from the prediction, otherwise the
    correction path in carbonshift would never be exercised."""
    monkeypatch.setattr(main.notifications.requests, "post", lambda *a, **k: _Resp(200))
    client.post("/v1/advance-slot", json={})

    current = client.get("/v1/slot").json()["current_slot"]
    observed = client.get("/v1/observed").json()["actual"]
    forecast = client.get("/v1/forecast", params={"from_slot": current, "count": 1}).json()["points"][0]["forecast"]
    assert observed != forecast


def test_reading_describes_the_slot_entered_not_the_one_left(client, monkeypatch):
    """Ordering guard: the observation must be taken AFTER the clock moves.

    If it were taken before, the peers would receive a reading describing the
    slot just left, and every correction they apply would be off by one slot —
    a bug that produces plausible numbers and would be very hard to spot.
    """
    monkeypatch.setattr(main.notifications.requests, "post", lambda *a, **k: _Resp(200))
    before = client.get("/v1/slot").json()["current_slot"]
    client.post("/v1/advance-slot", json={})

    reading = client.get("/v1/observed").json()
    assert reading["slot"] == before + 1
    assert reading["slot"] != before


def test_reading_is_recorded_once_per_advanced_slot(client, monkeypatch):
    monkeypatch.setattr(main.notifications.requests, "post", lambda *a, **k: _Resp(200))
    first = client.get("/v1/slot").json()["current_slot"]
    for _ in range(3):
        client.post("/v1/advance-slot", json={})

    for slot in range(first + 1, first + 4):
        assert client.get("/v1/observed", params={"slot": slot}).json()["known"] is True


def test_advance_slot_moves_one_slot_and_reports_deliveries(client, monkeypatch):
    calls: list[str] = []

    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        return _Resp(200)

    monkeypatch.setattr(main.notifications.requests, "post", fake_post)

    before = client.get("/v1/slot").json()["current_slot"]
    body = client.post("/v1/advance-slot", json={}).json()

    assert body["current_slot"] == before + 1
    assert body["local_step"] == 1
    assert body["all_ok"] is True
    # Client first (producer), then carbonshift (consumer).
    assert calls == [
        "http://localhost:8100/v1/tick",
        "http://localhost:8080/v1/admin/advance-slot",
    ]


def test_advance_slot_pushes_the_forecast_window_to_peers(client, monkeypatch):
    captured: dict = {}

    def fake_post(url, json=None, timeout=None):
        captured.update(json)
        return _Resp(200)

    monkeypatch.setattr(main.notifications.requests, "post", fake_post)
    client.post("/v1/advance-slot", json={})

    assert len(captured["forecast"]) == 24
    assert captured["current_slot"] is not None
    assert "observed" in captured
    # The reading belongs to the slot the peers have just entered.
    assert captured["observed"]["slot"] == captured["current_slot"]
    assert all("actual" not in p for p in captured["forecast"])


def test_advance_slot_accepts_expect_slot_when_it_matches(client, monkeypatch):
    monkeypatch.setattr(main.notifications.requests, "post", lambda *a, **k: _Resp(200))
    current = client.get("/v1/slot").json()["current_slot"]
    assert client.post("/v1/advance-slot", json={"expect_slot": current}).status_code == 200


def test_advance_slot_rejects_a_stale_expect_slot(client):
    """Idempotency interlock: a retried notification must not double-advance."""
    current = client.get("/v1/slot").json()["current_slot"]
    resp = client.post("/v1/advance-slot", json={"expect_slot": current + 99})
    assert resp.status_code == 409
    assert "slot mismatch" in resp.json()["detail"]


def test_advance_slot_can_skip_notifying_peers(client):
    body = client.post("/v1/advance-slot", json={"notify_peers": False}).json()
    assert body["notified"] is False
    assert body["deliveries"] == []
    assert body["all_ok"] is False  # nothing was notified, so not "all ok"


def test_advance_slot_fails_hard_when_a_peer_does_not_ack(client, monkeypatch):
    """A partial fan-out must not return 200: the clock moved but not every
    peer followed, so the run is unrecoverable and must be reported as such."""
    monkeypatch.setattr(main.notifications.requests, "post", lambda *a, **k: _Resp(500))
    resp = client.post("/v1/advance-slot", json={})
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["desynced"] is True
    assert detail["deliveries"][0]["ok"] is False


def test_a_desynced_provider_refuses_all_further_advances(client, monkeypatch):
    """The latch is what stops a desynced run from quietly continuing."""
    monkeypatch.setattr(main.notifications.requests, "post", lambda *a, **k: _Resp(500))
    assert client.post("/v1/advance-slot", json={}).status_code == 503

    # Even with a healthy peer now, the run stays refused.
    monkeypatch.setattr(main.notifications.requests, "post", lambda *a, **k: _Resp(200))
    resp = client.post("/v1/advance-slot", json={})
    assert resp.status_code == 503
    assert "restart the stack" in resp.json()["detail"]["reason"]


def test_health_reports_degraded_once_desynced(client, monkeypatch):
    monkeypatch.setattr(main.notifications.requests, "post", lambda *a, **k: _Resp(500))
    client.post("/v1/advance-slot", json={})

    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"
    assert resp.json()["desynced"] is not None


def test_advance_slot_refuses_when_the_clock_is_not_manual(client, monkeypatch):
    from app.clock import ProviderClock
    from app.slots import parse_epoch

    main.clock = ProviderClock(manual=False, slot_minutes=30, epoch=parse_epoch(main.settings.epoch_iso))
    resp = client.post("/v1/advance-slot", json={})
    assert resp.status_code == 409
    assert "MANUAL_CLOCK" in resp.json()["detail"]
