"""Tests for the ordered, retrying rollover fan-out.

The ordering and retry behaviour is the concrete answer to "could something be
lost because someone is behind the clock?", so it is worth pinning down.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.clock import ProviderClock
from app.notifications import (
    Peer,
    Role,
    build_rollover_payload,
    notify_peers,
    peers_from_settings,
)
from app.source import SyntheticCarbonIntensitySource

EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)
START = datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)

PEERS = [
    Peer(name="carbonshift", base_url="http://cs:8080", advance_path="/v1/admin/advance-slot", order=10),
    Peer(name="executor", base_url="http://ex:9000", advance_path="/admin/advance-slot", order=20),
]


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = ""


class _FakeSettings:
    """Minimal stand-in so `peers_from_settings` can be tested without env."""

    def __init__(self, carbonshift_url="http://cs:8080", executor_url="",
                 client_url="http://localhost:8100") -> None:
        self.carbonshift_url = carbonshift_url
        self.executor_url = executor_url
        self.client_url = client_url


def _source() -> SyntheticCarbonIntensitySource:
    return SyntheticCarbonIntensitySource(
        seed=26, slot_minutes=30, epoch=EPOCH, night_max=160.0, day_min=70.0,
        cycle_slots=12, noise_std=2.0, actual_jitter_std=0.05,
    )


def _clock() -> ProviderClock:
    return ProviderClock(manual=True, slot_minutes=30, epoch=EPOCH, start_at=START)


# ─── payload ─────────────────────────────────────────────────────────────────


def test_payload_carries_the_whole_window_not_just_the_current_value():
    """Lets a peer that missed a notification recover from the next one."""
    payload = build_rollover_payload(_clock(), _source(), horizon_slots=24)
    assert len(payload["forecast"]) == 24
    assert [p["slot"] for p in payload["forecast"]] == list(
        range(payload["current_slot"], payload["current_slot"] + 24)
    )


def test_payload_contains_no_actual_inside_the_forecast_window():
    """The forecast is a prediction; measurements travel separately."""
    payload = build_rollover_payload(
        _clock(), _source(), horizon_slots=4, reading=_source().observe(_clock().current_slot())
    )
    assert all("actual" not in p for p in payload["forecast"])


def test_payload_carries_the_single_reading_for_the_current_slot():
    clock = _clock()
    reading = _source().observe(clock.current_slot())
    payload = build_rollover_payload(clock, _source(), horizon_slots=4, reading=reading)
    assert payload["observed"] is not None
    assert payload["observed"]["slot"] == payload["current_slot"]
    assert payload["observed"]["actual"] is not None
    assert payload["observed"]["observed_at_slot"] == payload["current_slot"]


def test_payload_reports_null_observed_when_no_reading_was_taken():
    """Rendering an absent measurement as null, rather than substituting the
    forecast, is what keeps a carbon-saving metric from becoming fiction."""
    payload = build_rollover_payload(_clock(), _source(), horizon_slots=4, reading=None)
    assert payload["observed"] is None


def test_payload_reports_null_observed_for_the_unimplemented_remote_source():
    from app.source import RemoteCarbonIntensitySource

    remote = RemoteCarbonIntensitySource(base_url="https://x.invalid", slot_minutes=30, epoch=EPOCH)
    assert build_rollover_payload(_clock(), remote, horizon_slots=4, reading=None)["observed"] is None


def test_payload_identifies_the_active_source():
    assert build_rollover_payload(_clock(), _source(), horizon_slots=2)["source"] == "local"


# ─── ordering ────────────────────────────────────────────────────────────────


def test_peers_are_notified_in_policy_order_not_list_order():
    """carbonshift must move before the executor: it owns dispatch, so if the
    executor moved first a job could be handed to a slot it already left."""
    seen: list[str] = []

    def post(url, json=None, timeout=None):
        seen.append(url)
        return _Resp(200)

    notify_peers(
        list(reversed(PEERS)), {"x": 1},
        timeout_seconds=1, max_attempts=1, backoff_seconds=0.0,
        post=post, sleep=lambda _: None,
    )
    assert seen == [PEERS[0].url, PEERS[1].url]


def test_fan_out_aborts_after_the_first_failing_peer():
    """Continuing to a later peer while an earlier one is behind is exactly
    the desynchronization hazard, so it must not happen."""
    seen: list[str] = []

    def post(url, json=None, timeout=None):
        seen.append(url)
        return _Resp(500)

    results = notify_peers(
        PEERS, {"x": 1},
        timeout_seconds=1, max_attempts=1, backoff_seconds=0.0,
        post=post, sleep=lambda _: None,
    )
    assert seen == [PEERS[0].url]
    assert len(results) == 1
    assert results[0].ok is False


# ─── retries ─────────────────────────────────────────────────────────────────


def test_retries_until_success():
    calls = {"n": 0}

    def post(url, json=None, timeout=None):
        calls["n"] += 1
        return _Resp(200 if calls["n"] >= 3 else 500)

    results = notify_peers(
        [PEERS[0]], {"x": 1},
        timeout_seconds=1, max_attempts=3, backoff_seconds=0.0,
        post=post, sleep=lambda _: None,
    )
    assert results[0].ok is True
    assert results[0].attempts == 3


def test_gives_up_after_max_attempts_and_reports_why():
    def post(url, json=None, timeout=None):
        return _Resp(500)

    results = notify_peers(
        [PEERS[0]], {"x": 1},
        timeout_seconds=1, max_attempts=2, backoff_seconds=0.0,
        post=post, sleep=lambda _: None,
    )
    assert results[0].ok is False
    assert results[0].attempts == 2
    assert "500" in (results[0].error or "")


def test_missing_manual_clock_is_reported_without_pointless_retries():
    """409 is a configuration error, not a transient one."""
    calls = {"n": 0}

    def post(url, json=None, timeout=None):
        calls["n"] += 1
        return _Resp(409)

    results = notify_peers(
        [PEERS[0]], {"x": 1},
        timeout_seconds=1, max_attempts=5, backoff_seconds=0.0,
        post=post, sleep=lambda _: None,
    )
    assert calls["n"] == 1
    assert results[0].ok is False
    assert "manual clock" in (results[0].error or "")


def test_network_exception_is_retried_then_reported():
    import requests

    calls = {"n": 0}

    def post(url, json=None, timeout=None):
        calls["n"] += 1
        raise requests.RequestException("connection refused")

    results = notify_peers(
        [PEERS[0]], {"x": 1},
        timeout_seconds=1, max_attempts=3, backoff_seconds=0.0,
        post=post, sleep=lambda _: None,
    )
    assert calls["n"] == 3
    assert results[0].ok is False
    assert "connection refused" in (results[0].error or "")


def test_backoff_is_applied_between_attempts():
    sleeps: list[float] = []

    def post(url, json=None, timeout=None):
        return _Resp(500)

    notify_peers(
        [PEERS[0]], {"x": 1},
        timeout_seconds=1, max_attempts=3, backoff_seconds=0.5,
        post=post, sleep=sleeps.append,
    )
    # Linear backoff: 0.5 then 1.0, never sleeping after the final attempt.
    assert sleeps == [0.5, 1.0]


def test_all_ok_is_true_only_when_every_peer_acked():
    def ok(url, json=None, timeout=None):
        return _Resp(200)

    results = notify_peers(
        PEERS, {"x": 1}, timeout_seconds=1, max_attempts=1, backoff_seconds=0.0,
        post=ok, sleep=lambda _: None,
    )
    assert all(r.ok for r in results)


def test_rollover_report_distinguishes_ok_from_failed():
    """`all_ok` and `any_failed` must not be confusable: an empty fan-out is
    neither a success nor a failure, and a partial failure is not `all_ok`."""
    from app.notifications import RolloverReport

    empty = RolloverReport(tick=None, deliveries=[])
    assert empty.all_ok is False
    assert empty.any_failed is False

    def ok(url, json=None, timeout=None):
        return _Resp(200)

    good = notify_peers(
        PEERS, {"x": 1}, timeout_seconds=1, max_attempts=1, backoff_seconds=0.0,
        post=ok, sleep=lambda _: None,
    )
    report = RolloverReport(tick=None, deliveries=good)
    assert report.all_ok is True and report.any_failed is False

    def bad(url, json=None, timeout=None):
        return _Resp(500)

    mixed = RolloverReport(
        tick=None,
        deliveries=notify_peers(
            [PEERS[0]], {"x": 1}, timeout_seconds=1, max_attempts=1,
            backoff_seconds=0.0, post=bad, sleep=lambda _: None,
        ),
    )
    assert mixed.all_ok is False and mixed.any_failed is True


# ─── peer construction ───────────────────────────────────────────────────────


def test_client_is_the_first_peer_because_it_produces_the_slot_work():
    """Ordering is a policy, not an accident: the client supplies slot N's
    requests, so it must be notified before the scheduler processes slot N."""
    peers = sorted(peers_from_settings(_FakeSettings()), key=lambda p: p.order)
    assert [p.name for p in peers] == ["client", "carbonshift"]
    assert peers[0].url == "http://localhost:8100/v1/tick"
    assert peers[0].role is Role.PRODUCER


def test_carbonshift_is_a_consumer_and_follows_the_client():
    peers = sorted(peers_from_settings(_FakeSettings()), key=lambda p: p.order)
    carbonshift = next(p for p in peers if p.name == "carbonshift")
    assert carbonshift.role is Role.CONSUMER
    assert carbonshift.url == "http://cs:8080/v1/admin/advance-slot"


def test_executor_joins_last_when_configured():
    peers = sorted(peers_from_settings(_FakeSettings(executor_url="http://ex:9000")), key=lambda p: p.order)
    assert [p.name for p in peers] == ["client", "carbonshift", "executor"]
    assert peers[2].url == "http://ex:9000/admin/advance-slot"
    assert peers[2].role is Role.CONSUMER


def test_blank_executor_url_is_treated_as_not_configured():
    names = [p.name for p in peers_from_settings(_FakeSettings(executor_url="   "))]
    assert names == ["client", "carbonshift"]


def test_trailing_slash_does_not_double_up_in_urls():
    peers = peers_from_settings(_FakeSettings(executor_url="http://ex:9000/"))
    assert all("//" not in p.url.replace("http://", "") for p in peers)


def test_peer_role_defaults_to_consumer():
    """Producers opt in explicitly; the common case needs no ceremony."""
    assert Peer(name="x", base_url="http://x", advance_path="/p", order=1).role is Role.CONSUMER


def test_delivery_result_serialises_for_the_http_response():
    def ok(url, json=None, timeout=None):
        return _Resp(200)

    results = notify_peers(
        [PEERS[0]], {"x": 1}, timeout_seconds=1, max_attempts=1, backoff_seconds=0.0,
        post=ok, sleep=lambda _: None,
    )
    assert results[0].to_dict()["peer"] == "carbonshift"
