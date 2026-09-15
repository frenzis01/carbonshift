"""Thin HTTP wrapper around carbonshift's `POST /v1/requests`."""
from __future__ import annotations

from typing import Any

import requests

from .config import settings


class CarbonshiftError(RuntimeError):
    pass


def submit(deadline_seconds: float, callback_url: str, payload: dict[str, Any],
           task_id: str | None = None) -> dict[str, Any]:
    headers = {}
    if settings.carbonshift_api_key:
        headers["X-API-Key"] = settings.carbonshift_api_key

    body: dict[str, Any] = {"deadline_seconds": deadline_seconds, "callback_url": callback_url, "payload": payload}
    if task_id is not None:
        body["task_id"] = task_id

    try:
        resp = requests.post(
            f"{settings.carbonshift_url}/v1/requests",
            json=body,
            headers=headers,
            timeout=settings.http_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise CarbonshiftError(f"cannot reach carbonshift at {settings.carbonshift_url}: {exc}") from exc

    if resp.status_code not in (200, 202):
        raise CarbonshiftError(f"carbonshift returned {resp.status_code}: {resp.text}")
    return resp.json()


def register_task(task_id: str, flavours: list[dict[str, Any]], max_error_threshold: float | None = None) -> None:
    """`POST /v1/tasks` — announces (or updates) a task's available
    flavours (`[{"name", "error", "duration"}, ...]`) on carbonshift, so
    requests submitted with this `task_id` are scheduled among them instead
    of carbonshift's built-in default flavours. `max_error_threshold` (%),
    if given, overrides carbonshift's single global default for this task's
    requests — see `push_flavours.py` for how it's chosen by default."""
    headers = {}
    if settings.carbonshift_api_key:
        headers["X-API-Key"] = settings.carbonshift_api_key

    body: dict[str, Any] = {"task_id": task_id, "flavours": flavours}
    if max_error_threshold is not None:
        body["max_error_threshold"] = max_error_threshold

    try:
        resp = requests.post(
            f"{settings.carbonshift_url}/v1/tasks",
            json=body,
            headers=headers,
            timeout=settings.http_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise CarbonshiftError(f"cannot reach carbonshift at {settings.carbonshift_url}: {exc}") from exc

    if resp.status_code != 204:
        raise CarbonshiftError(f"carbonshift returned {resp.status_code}: {resp.text}")


def get_status(request_id: str) -> dict[str, Any]:
    """`GET /v1/requests/{id}` — current status. Used to refresh a
    `TrackedRequest.ack` that was captured while still `pending` (the
    initial submit-time poll timed out before the DP solver committed an
    assignment), so fields like `flavour`/`carbon_cost` aren't stuck `null`
    forever once a result actually arrives (see `tracker.py::on_callback`)."""
    headers = {}
    if settings.carbonshift_api_key:
        headers["X-API-Key"] = settings.carbonshift_api_key

    try:
        resp = requests.get(
            f"{settings.carbonshift_url}/v1/requests/{request_id}",
            headers=headers,
            timeout=settings.http_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise CarbonshiftError(f"cannot reach carbonshift at {settings.carbonshift_url}: {exc}") from exc

    if resp.status_code != 200:
        raise CarbonshiftError(f"carbonshift returned {resp.status_code}: {resp.text}")
    return resp.json()


def get_stats() -> dict[str, Any]:
    """`GET /v1/stats` — request counts by status plus the scheduler's own
    (task-agnostic, by design) global error average/count."""
    headers = {}
    if settings.carbonshift_api_key:
        headers["X-API-Key"] = settings.carbonshift_api_key

    try:
        resp = requests.get(
            f"{settings.carbonshift_url}/v1/stats",
            headers=headers,
            timeout=settings.http_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise CarbonshiftError(f"cannot reach carbonshift at {settings.carbonshift_url}: {exc}") from exc

    if resp.status_code != 200:
        raise CarbonshiftError(f"carbonshift returned {resp.status_code}: {resp.text}")
    return resp.json()


def get_task_config(task_id: str) -> dict[str, Any]:
    """`GET /v1/tasks/{task_id}` — the task's currently effective flavours
    and `max_error_threshold` (registered override, or the global default)."""
    headers = {}
    if settings.carbonshift_api_key:
        headers["X-API-Key"] = settings.carbonshift_api_key

    try:
        resp = requests.get(
            f"{settings.carbonshift_url}/v1/tasks/{task_id}",
            headers=headers,
            timeout=settings.http_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise CarbonshiftError(f"cannot reach carbonshift at {settings.carbonshift_url}: {exc}") from exc

    if resp.status_code != 200:
        raise CarbonshiftError(f"carbonshift returned {resp.status_code}: {resp.text}")
    return resp.json()


def get_carbon_forecast() -> list[float]:
    """`GET /v1/carbon-forecast` — the forecast (index = slot) the DP solver
    is scheduling against, used to derive a plausible "actual" carbon
    intensity series to report back via `POST /v1/admin/advance-slot`."""
    headers = {}
    if settings.carbonshift_api_key:
        headers["X-API-Key"] = settings.carbonshift_api_key

    try:
        resp = requests.get(
            f"{settings.carbonshift_url}/v1/carbon-forecast",
            headers=headers,
            timeout=settings.http_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise CarbonshiftError(f"cannot reach carbonshift at {settings.carbonshift_url}: {exc}") from exc

    if resp.status_code != 200:
        raise CarbonshiftError(f"carbonshift returned {resp.status_code}: {resp.text}")
    return resp.json()["forecast"]
