"""HTTP-level tests for `POST /v1/tick` — the provider's entry point.

`carbonshift_client.submit` is monkeypatched, so no real carbonshift is needed.
These tests pin the contract the provider depends on:

* a tick for a slot inside the plan submits exactly that slot's requests;
* a tick outside the plan's range is a 200 with `submitted: 0` (normal, not an
  error — the provider ticks on its own schedule);
* a retried tick is a no-op, so the provider's retries cannot double-submit.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import plan_runner, state
from app.main import app
from app.timeslots import floor_to_slot, slot_of

SLOT_MINUTES = 60.0
_counter = itertools.count(1)


def fake_submit(deadline_seconds, callback_url, payload, task_id=None):
    return {
        "request_id": next(_counter),
        "status": "scheduled",
        "scheduled_slot": 3,
        "eta_seconds": 5.0,
        "flavour": "Balanced",
        "carbon_cost": 1.2,
        "baseline_carbon_cost": 2.4,
        "scheduled_at": 1700000000.0,
        "error": None,
    }


@pytest.fixture(autouse=True)
def _patch_submit(monkeypatch):
    monkeypatch.setattr(plan_runner, "submit", fake_submit)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset both the plan registry and the shared tracker.

    `app.main.tracker` is module-level, so without this the `/requests` counts
    would accumulate across tests and the assertions would depend on test
    order.
    """
    from app import main as client_main

    state.reset()
    client_main.tracker.reset()
    yield
    state.reset()
    client_main.tracker.reset()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _register_plan(client, reference: datetime, offsets_minutes: list[float]) -> int:
    requests = [
        {
            "task": "text_generation",
            "input": {"prompt": str(i)},
            "start_at": (reference + timedelta(minutes=off)).isoformat(),
            "deadline_at": (reference + timedelta(minutes=off + SLOT_MINUTES)).isoformat(),
        }
        for i, off in enumerate(offsets_minutes)
    ]
    resp = client.post("/run/send-plan", json={
        "requests": requests, "slot_minutes": SLOT_MINUTES, "mode": "emulated",
    })
    assert resp.status_code == 202
    return resp.json()["plan_id"]


def _tick(client, slot: int):
    return client.post("/v1/tick", json={
        "current_slot": slot,
        "slot_start_utc": datetime.now(timezone.utc).isoformat(),
        "source": "local",
        "observed": None,
        "forecast": [],
    })


def _boundary(dt: datetime) -> datetime:
    return floor_to_slot(dt, SLOT_MINUTES)


# ─── the happy path ──────────────────────────────────────────────────────────


def test_tick_submits_the_slots_requests(client):
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = _register_plan(client, ref, [0, 5, 10])
    start = state.get_plan(plan_id)["plan_start_slot"]

    body = _tick(client, start).json()

    assert body["submitted"] == 3
    assert body["plan_index"] == [[plan_id, 0]]
    assert len(client.get("/requests").json()) == 3


def test_tick_only_submits_the_slot_it_names(client):
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = _register_plan(client, ref, [0, 5, 60, 65])
    start = state.get_plan(plan_id)["plan_start_slot"]

    first = _tick(client, start).json()
    assert first["submitted"] == 2

    second = _tick(client, start + 1).json()
    assert second["submitted"] == 2
    assert second["plan_index"] == [[plan_id, 1]]

    assert len(client.get("/requests").json()) == 4


def test_tick_accepts_the_providers_real_payload_shape(client):
    """The provider sends current_slot/slot_start_utc/observed/forecast — the
    client's model must mirror it, not invent its own field names."""
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = _register_plan(client, ref, [0])
    start = state.get_plan(plan_id)["plan_start_slot"]

    resp = client.post("/v1/tick", json={
        "source": "local",
        "current_slot": start,
        "slot_start_utc": "2026-01-01T12:00:00+00:00",
        "observed": {"slot": start, "actual": 73.3, "observed_at_slot": start},
        "forecast": [{"slot": start, "forecast": 74.5}],
    })
    assert resp.status_code == 200
    assert resp.json()["submitted"] == 1


# ─── slots outside the plan ──────────────────────────────────────────────────


def test_tick_before_the_plan_starts_is_a_no_op_not_an_error(client):
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = _register_plan(client, ref, [0])
    start = state.get_plan(plan_id)["plan_start_slot"]

    resp = _tick(client, start - 5)
    assert resp.status_code == 200
    assert resp.json()["submitted"] == 0
    assert resp.json()["plan_index"] == []


def test_tick_after_the_plan_ends_is_a_no_op_not_an_error(client):
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = _register_plan(client, ref, [0])
    start = state.get_plan(plan_id)["plan_start_slot"]

    resp = _tick(client, start + 99)
    assert resp.status_code == 200
    assert resp.json()["submitted"] == 0


def test_tick_with_no_plan_registered_is_a_no_op(client):
    resp = _tick(client, 59000)
    assert resp.status_code == 200
    assert resp.json()["submitted"] == 0


# ─── idempotency ─────────────────────────────────────────────────────────────


def test_a_retried_tick_does_not_double_submit(client):
    """The provider retries on failure, so a repeated tick must be a no-op."""
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = _register_plan(client, ref, [0, 5])
    start = state.get_plan(plan_id)["plan_start_slot"]

    first = _tick(client, start).json()
    assert first["submitted"] == 2
    assert first["duplicate"] is False

    second = _tick(client, start).json()
    assert second["submitted"] == 0
    assert second["duplicate"] is True

    assert len(client.get("/requests").json()) == 2


def test_a_retried_older_tick_is_also_a_no_op(client):
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = _register_plan(client, ref, [0, 60])
    start = state.get_plan(plan_id)["plan_start_slot"]

    _tick(client, start)
    _tick(client, start + 1)

    # A late retry of the first tick must not resubmit slot 0.
    assert _tick(client, start).json()["submitted"] == 0
    assert len(client.get("/requests").json()) == 2


# ─── the invariant ───────────────────────────────────────────────────────────


def test_tick_does_not_require_the_client_to_track_its_own_slot(client):
    """A gap in the ticks must not shift the mapping.

    The provider advances one slot at a time, so ticks are monotonic — but a
    tick can be *missed* (the provider's fan-out failed and it retried later,
    or the client was briefly down). If the client kept its own counter it
    would treat the next tick as "the next slot" and submit the wrong slot's
    work. Deriving the index from the slot it is told about makes a gap
    harmless: slot 2's tick still submits slot 2's request.
    """
    ref = _boundary(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    plan_id = _register_plan(client, ref, [0, 60, 120])
    start = state.get_plan(plan_id)["plan_start_slot"]

    # Record what actually reaches carbonshift, so we can prove *which*
    # requests went out and not merely how many.
    sent: list[dict] = []
    original = plan_runner.submit

    def recording_submit(deadline_seconds, callback_url, payload, task_id=None):
        sent.append(payload)
        return original(deadline_seconds, callback_url, payload, task_id=task_id)

    plan_runner.submit = recording_submit
    try:
        # Slot 0's tick never arrives. Slots 1 and 2 must still submit their
        # own work, not "the first and second unsubmitted requests".
        assert _tick(client, start + 1).json()["submitted"] == 1
        assert _tick(client, start + 2).json()["submitted"] == 1
    finally:
        plan_runner.submit = original

    # `_register_plan` labels each request with its offset index, so the
    # prompts tell us exactly which slots were submitted.
    assert [p["input"]["prompt"] for p in sent] == ["1", "2"]
