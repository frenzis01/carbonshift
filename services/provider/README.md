# Carbon-intensity provider

Serves carbon-intensity data to CarbonShift. In manual-clock emulation it
owns and distributes simulated time; in realtime, every peer derives the same
epoch-aligned slot from wall time.

* **[`INTERFACE.md`](INTERFACE.md)** — the HTTP contract. Peers depend on this,
  never on which adapter is behind it.
* **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — why it is shaped this way, which
  alternatives were rejected, the slot-alignment hazard, and the staged
  migration plan.

## Two roles, one contract

| `PROVIDER_ROLE` | Adapter | Status |
|---|---|---|
| `local` (default) | `SyntheticCarbonIntensitySource` — deterministic synthetic curve | ✅ working |
| `remote` | `RemoteCarbonIntensitySource` — `carbonintensity.org.uk` | 🚧 stub (see `ARCHITECTURE.md` §5) |

The role is selected in `app/source.py::build_source`. Nothing else in the
service — or in any peer — changes between roles.

## Run

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# Emulation: the virtual clock starts at today's local midnight and is driven explicitly.
PROVIDER_MANUAL_CLOCK=1 \
CARBONSHIFT_URL=http://localhost:8080 \
  .venv/bin/python -m uvicorn app.main:app --port 9100

# Realtime: /v1/slot is derived directly from the current wall-clock instant.
PROVIDER_MANUAL_CLOCK=0 .venv/bin/python -m uvicorn app.main:app --port 9100
```

Quick check:

```sh
curl localhost:9100/health
curl localhost:9100/v1/meta
curl 'localhost:9100/v1/forecast?count=5'
curl -X POST localhost:9100/v1/advance-slot -H 'Content-Type: application/json' -d '{}'
```

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `PROVIDER_ROLE` | `local` | `local` or `remote` |
| `PROVIDER_PORT` | `9100` | HTTP port |
| `PROVIDER_SLOT_MINUTES` | `60` | Slot length; **must match** carbonshift's `SLOT_DURATION_SECONDS` |
| `PROVIDER_EPOCH` | `2020-01-01T00:00:00+00:00` | Origin of global slot 0 |
| `PROVIDER_FORECAST_HORIZON_SLOTS` | `24` | Window length served and pushed in manual mode |
| `PROVIDER_GENERATOR_CYCLE_SLOTS` | `1440 / PROVIDER_SLOT_MINUTES` | Synthetic forecast cycle; defaults to one local day |
| `PROVIDER_MANUAL_CLOCK` | `0` | Freeze time for provider-driven emulation; never in production |
| `TZ` | `UTC` in Compose | Local timezone used to choose emulation midnight; keep identical across services |
| `CARBONSHIFT_URL` | `http://localhost:8080` | Primary peer to notify |
| `CLIENT_URL` | `http://localhost:8100` | Legacy single-client URL used when `PROVIDER_CLIENT_URLS` is unset or blank |
| `PROVIDER_CLIENT_URLS` | *(unset)* | Comma-separated client URLs to notify; each client is added as an ordered producer peer |
| `EXECUTOR_URL` | *(empty)* | Optional second peer to notify |
| `PROVIDER_NOTIFY_MAX_ATTEMPTS` | `3` | Retries per peer |
| `PROVIDER_GENERATOR_SEED` | `26` | Synthetic curve seed |

## Tests

```sh
.venv/bin/python -m pytest -q     # no network or peer services
```

`conftest.py` forces `PROVIDER_MANUAL_CLOCK=1` **before** `app.config` is
imported, because settings are read at import time. Tests that exercise the
fan-out inject a fake `post` rather than talking to a real peer.

## Status / limitations

* The remote adapter is intentionally unimplemented (it returns no data rather
  than raising, so the clock loop survives).
* Manual-clock emulation is wired through the provider: it ticks the client,
  CarbonShift, and (when configured) the executor. On the first call it
  synchronizes consumer clocks before the client's initial batch, so
  CarbonShift has the slot offset and UTC boundary before accepting those
  requests. See `INTERFACE.md` for the initial announce and rollover order.
  For multiple clients, set `PROVIDER_CLIENT_URLS` to their comma-separated
  base URLs; if it is unset, the existing `CLIENT_URL` single-client setting
  remains in effect.
* In realtime, the provider does not push periodic clock notifications.
  `GET /v1/slot` derives the current slot from wall time; the client ticks its
  plans locally, CarbonShift advances its local horizon at the same wall-clock
  edges, and the executor runs jobs when their UTC `execute_at` is reached.
* Manual emulation keeps the fixed global epoch/slot IDs but starts the virtual
  instant at today's local midnight. All peers receive the same UTC timestamp;
  `TZ` must be identical across services, and local midnight must lie on the
  configured slot grid. UTC is the Compose default.
* The upstream GB API is natively half-hourly, so a `remote` role only maps
  1:1 onto slots at `PROVIDER_SLOT_MINUTES=30`.
* There is **no `actual` for a future slot** and there never will be — a
  measurement is an event, not a property of a slot. See `ARCHITECTURE.md` §10
  for why the original `provides_actual_for_future_slots` flag was removed.