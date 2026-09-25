# Carbon-intensity provider

Serves carbon-intensity data to CarbonShift, and (in manual-clock mode) owns
the shared clock.

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

# Emulation: the provider owns the clock and is driven explicitly.
PROVIDER_MANUAL_CLOCK=1 PROVIDER_AUTO_ADVANCE_CLOCK=0 \
CARBONSHIFT_URL=http://localhost:8080 \
  .venv/bin/python -m uvicorn app.main:app --port 9100

# Realtime: the provider advances its own slot every PROVIDER_SLOT_MINUTES.
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
| `PROVIDER_FORECAST_HORIZON_SLOTS` | `24` | Window length pushed/served |
| `PROVIDER_MANUAL_CLOCK` | `0` | Owner-of-time mode; never in production |
| `PROVIDER_AUTO_ADVANCE_CLOCK` | `1` | Realtime self-advance (ignored when manual) |
| `CARBONSHIFT_URL` | `http://localhost:8080` | Primary peer to notify |
| `EXECUTOR_URL` | *(empty)* | Optional second peer to notify |
| `PROVIDER_NOTIFY_MAX_ATTEMPTS` | `3` | Retries per peer |
| `PROVIDER_GENERATOR_SEED` | `26` | Synthetic curve seed |

## Tests

```sh
.venv/bin/python -m pytest -q     # 114 tests, no network, no peer services
```

`conftest.py` forces `PROVIDER_MANUAL_CLOCK=1` and disables the auto-advance
thread **before** `app.config` is imported, because settings are read at import
time. Tests that exercise the fan-out inject a fake `post` rather than talking
to a real peer.

## Status / limitations

* The remote adapter is intentionally unimplemented (it returns no data rather
  than raising, so the clock loop survives).
* The provider is **not yet wired into the running stack** — carbonshift,
  client and executor still behave exactly as before. See `ARCHITECTURE.md` §8
  for the migration stages; **the pushed forecast is inert until
  `Config::slot_epoch_offset` is set** (§7).
* The upstream GB API is natively half-hourly, so a `remote` role only maps
  1:1 onto slots at `PROVIDER_SLOT_MINUTES=30`.
* There is **no `actual` for a future slot** and there never will be — a
  measurement is an event, not a property of a slot. See `ARCHITECTURE.md` §10
  for why the original `provides_actual_for_future_slots` flag was removed.