"""Profile selection belongs to each generated request, not its executor task."""
from __future__ import annotations

from datetime import datetime, timezone

from app import plan_builder


def test_build_requests_carries_optional_profile_without_replacing_task(monkeypatch):
    monkeypatch.setattr(
        plan_builder,
        "load_examples",
        lambda task, count, seed, source: [
            {"input": {"prompt": str(index)}} for index in range(count)
        ],
    )
    reference = datetime(2026, 1, 1, tzinfo=timezone.utc)

    profiled = plan_builder.build_requests(
        "text_generation",
        total_slots=1,
        per_slot_avg=2,
        slot_minutes=30,
        source="synthetic",
        seed=4,
        pattern="flat",
        reference=reference,
        qos_profile_id="text-generation-calibrated-v1",
    )
    defaulted = plan_builder.build_requests(
        "text_generation",
        total_slots=1,
        per_slot_avg=1,
        slot_minutes=30,
        source="synthetic",
        seed=4,
        pattern="flat",
        reference=reference,
    )

    assert len(profiled) == 2
    assert all(item["task"] == "text_generation" for item in profiled)
    assert all(item["qos_profile_id"] == "text-generation-calibrated-v1" for item in profiled)
    assert "qos_profile_id" not in defaulted[0]
