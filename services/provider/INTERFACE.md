# Carbon-intensity provider — interface contract

The provider is a **replaceable component**: the same HTTP contract is served
whether it is backed by a synthetic generator (`PROVIDER_ROLE=local`) or a real
upstream (`PROVIDER_ROLE=remote`). Peers must depend on this document, never on
who is behind it.

Base URL convention: `http://provider:9100` (see `PROVIDER_PORT`).

---

## Read endpoints

### `GET /health`

Liveness **and** adapter health in one call.

```json
{
  "status": "ok",                      // "degraded" when the adapter is not usable
  "source": { "source": "local", "ok": true, "seed": 26, "cycle_slots": 12,
              "detail": "synthetic/deterministic — emulation and offline use" },
  "current_slot": 118015,
  "manual_clock": true,
  "drift_slots": 0
}
```

* Returns **503** when `source.ok` is false (e.g. the remote upstream is
  unreachable), so an orchestrator sees it without parsing the body.
* `drift_slots` = how far the *simulated* instant has drifted from real wall
  time. Non-zero is normal in a fast manual run; it stops being normal if it
  goes **negative**, which would mean the simulation fell behind slots it had
  already published.

### `GET /v1/meta`

The configuration actually in effect, so an operator can tell an emulation
setup from a real one without inspecting env vars.

```json
{
  "role": "local",
  "source": "local",
  "slot_minutes": 60.0,
  "epoch_utc": "2020-01-01T00:00:00+00:00",
  "forecast_horizon_slots": 24,
  "manual_clock": true,
  "auto_advance_clock": false,
  "peers": [ { "name": "carbonshift",
               "url": "http://carbonshift:8080/v1/admin/advance-slot",
               "order": 10 } ]
}
```

### `GET /v1/slot`

Cheap and safe to poll.

```json
{ "current_slot": 118015,
  "slot_start_utc": "2026-09-24T16:00:00+00:00",
  "slot_minutes": 60.0,
  "manual_clock": true,
  "local_step": 0,
  "source": "local" }
```

### `GET /v1/forecast`

| Query | Type | Default | Meaning |
|---|---|---|---|
| `from_slot` | int | current slot | First slot of the window |
| `count` | int (1..288) | `PROVIDER_FORECAST_HORIZON_SLOTS` | Window length |

```json
{
  "source": "local",
  "slot_minutes": 60.0,
  "current_slot": 118015,
  "points": [
    { "slot": 118015, "forecast": 74.548932 },
    { "slot": 118016, "forecast": 87.661767 }
  ]
}
```

**A forecast point has no `actual` field, by design.** A forecast is a
prediction spanning many slots; a measurement concerns exactly one slot and only
exists once that slot has been reached. Keeping them in one row made
"the actual of a future slot" expressible — which is not a thing that exists.
Measurements come from `GET /v1/observed` instead.

### `GET /v1/observed?slot=`

The **measurement taken** for a slot, if one was taken.

```json
{ "known": true, "slot": 118015, "actual": 73.303066, "observed_at_slot": 118015 }
```

```json
{ "known": false, "slot": 118020, "actual": null, "observed_at_slot": null }
```

* `observed_at_slot` is the slot during which the reading was taken. It equals
  `slot` in every case that can physically happen (you measure the slot you are
  standing in); both are carried so that a hypothetical "measurement of the
  future" is representable and therefore refusable, rather than silently
  readable as a plausible number.
* `known: false` is the **normal** answer for any slot not yet reached, and for
  a source that has no reading (e.g. the unimplemented remote adapter). It is
  never substituted with the forecast value.

---

## `POST /v1/advance-slot` — the rollover

**Manual clock only** (409 otherwise, matching carbonshift's and the executor's
convention).

Request body (all fields optional):

```json
{
  "expect_slot": 118015,   // idempotency interlock: 409 on mismatch
  "notify_peers": true
}
```

Response:

```json
{
  "current_slot": 118016,
  "slot_start_utc": "2026-09-24T16:30:00+00:00",
  "local_step": 1,
  "source": "local",
  "notified": true,
  "all_ok": true,
  "deliveries": [
    { "peer": "carbonshift", "url": "http://carbonshift:8080/v1/admin/advance-slot",
      "ok": true, "attempts": 1, "status_code": 200, "error": null }
  ]
}
```

### Semantics

1. The clock moves **exactly one slot**. There is no multi-slot jump: a jump
   would leave peers holding a slot they never processed.
2. **A reading is taken for the slot just entered**, and it is taken *after*
   the clock moves and *before* the fan-out. Ordering matters: measuring before
   advancing would send peers a reading describing the slot just left, so every
   correction they apply would be off by one slot — a bug that yields plausible
   numbers and is very hard to notice.
3. The peers listed in `GET /v1/meta` are notified **in `order`**, each with
   bounded retries, and the fan-out **stops at the first peer that fails** —
   advancing a later peer while an earlier one is behind is precisely the
   desynchronization hazard this protocol exists to prevent.
4. Each notification body carries the full forecast window (not just the current
   value), so a peer that missed one can recover from the next.

### Notification body (pushed to each peer)

```json
{
  "source": "local",
  "current_slot": 118016,
  "slot_start_utc": "2026-09-24T16:30:00+00:00",
  "observed": {
    "slot": 118016,
    "actual": 91.976173,
    "observed_at_slot": 118016
  },
  "forecast": [
    { "slot": 118016, "forecast": 87.661767 },
    { "slot": 118017, "forecast": 115.230974 }
  ]
}
```

`observed` is `null` when no reading could be taken. Peers must treat that as
"no correction available" and **never** substitute the forecast value — doing so
would silently turn a measured carbon saving into an assumed one.

Note the two fields are structurally different, not two views of one list:
`forecast` is a window of predictions, `observed` is a single event.

### Status codes

| Code | Meaning |
|---|---|
| 200 | Slot advanced (check `all_ok` for whether peers acked) |
| 409 | Clock is not manual, **or** `expect_slot` did not match |
| 422 | Malformed body |

---

## Slot identity — read this before consuming any slot number

Two different numbers are both called "slot". They are **not** interchangeable.

| | Definition | Example |
|---|---|---|
| **Global slot** (this service) | `floor((now - epoch) / slot_duration)` | 118015 |
| **Engine slot** (carbonshift today) | `floor(virtual_elapsed_ms / slot_duration)`, i.e. an **uptime counter** from 0 | 0 |

The provider publishes **global slots**. carbonshift's internal `current_slot`
is an uptime counter that restarts at 0 with the process. Any consumer of a
provider-published slot must convert:

```
engine_slot = global_slot - Config::slot_epoch_offset
```

`Config::slot_epoch_offset` is `0` by default, which preserves today's
behaviour for simulations and tests. **Until it is set correctly, a pushed
forecast will be indexed off-by-~118k slots in carbonshift** — i.e. silently
ignored rather than obviously wrong. See `ARCHITECTURE.md` §"Allineamento slot"
for the chosen fix and its alternatives.
