"""Thin HTTP wrapper around the provider's API (see provider/INTERFACE.md).

Mirrors `carbonshift_client.py`: build the URL, send the request, check the
status, raise a typed error, return JSON. Nothing else.

## Why this module exists

The client now talks to **two** services: carbonshift (submit requests) and
the provider (which owns the clock). Without this module, every call site
would carry its own `requests.post(...)` plus its own idea of the provider's
URL and error handling — the "how do I talk to the provider" knowledge
duplicated and free to drift.

This is the same ports-and-adapters idea as the provider's own
`CarbonIntensitySource`: the client's plan logic should not know *how* the
clock is driven, only that it can ask. This module is the adapter.

Note the direction: this is the client calling *out* to the provider. It is
not the provider's `notifications.py`, which is the provider calling out to
its peers.
"""
from __future__ import annotations

from typing import Any, Optional

import requests

from .config import settings


class ProviderError(RuntimeError):
    """Raised for any failure talking to the provider.

    Deliberately not swallowed anywhere: the provider is the clock, so if it
    is unreachable the run cannot proceed meaningfully. See
    `provider/ARCHITECTURE.md` §4 — a silent fallback here is exactly the
    class of bug the design exists to prevent.
    """


def _base_url() -> str:
    return settings.provider_url.rstrip("/")


def get_observed(slot: Optional[int] = None) -> dict[str, Any]:
    """`GET /v1/observed` — the reading taken for a slot, if one was taken.

    `slot=None` yields the current slot's reading. `known: false` is the
    normal answer for a slot that has not been reached: a measurement of a
    future slot does not exist.
    """
    params: dict[str, Any] = {"slot": slot}
    try:
        resp = requests.get(f"{_base_url()}/v1/observed", params=params,
                            timeout=settings.http_timeout_seconds)
    except requests.RequestException as exc:
        raise ProviderError(f"cannot reach provider at {_base_url()}: {exc}") from exc
    if resp.status_code != 200:
        raise ProviderError(f"provider returned {resp.status_code}: {resp.text}")
    return resp.json()
