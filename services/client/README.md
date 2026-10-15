# CarbonShift Client

The CarbonShift client submits requests to **CarbonShift**, never directly to the executor. CarbonShift chooses when and how to execute each request, then returns the result to this client through a callback. The client collects end-to-end metrics such as submission and response times, deadline misses, and model quality and confidence.

```
client (this)    --POST /v1/requests-->  carbonshift  --POST /dispatch-->  executor
       ^                                       |                              |
       |                                  (chooses flavour                    |
       |                                   and slot based on                  |
       |                                   the carbon budget)                 |
       +--------------- POST /callback (result) <---- POST /v1/callback/{id} --+
```

**Important:** The client does not choose the flavour (`Accurate`, `Balanced`, or `Fast`); CarbonShift selects it through its carbon-aware optimization. The client chooses the executor's *task kind* (`text_generation`, `ner`, or `question_answering`), the input, the deadline, and optionally the QoS profile. A profile has a stable, shareable ID: clients that submit the same `qos_profile_id` share its budget. If the field is omitted, CarbonShift selects the default profile for that task kind.

## Setup

```sh
cd client
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # use requirements-dev.txt for tests
```

The Hugging Face `datasets` package is only needed when using `source: "dataset"` (see below). With the default `source: "synthetic"`, no downloads are required and the full pipeline can be tested immediately.

**To use real Hugging Face datasets:** The datasets used here require no account or login. Download them in advance with:

```sh
python scripts/download_datasets.py
```

This downloads `wikitext-2-raw-v1` (~4 MB, text-generation prompts), `squad_v2` (~40 MB, question-answering examples), and `tomaarsen/conll2003` (NER, a Parquet mirror of the same dataset).

## Start (recommended order)

1. **Executor** (directory `../executor`): `uvicorn app.main:app --port 9000`
2. **CarbonShift** (directory `../carbonshift/rust`), configured to use the executor:
   ```sh
   EXECUTOR_URL=http://localhost:9000/dispatch \
   CARBONSHIFT_ALLOW_PRIVATE_CALLBACKS=1 \
     ./target/release/carbonshift-service
   ```
`CARBONSHIFT_ALLOW_PRIVATE_CALLBACKS=1` is required locally because both the client's callback URL and CarbonShift's executor URL point to `localhost`; otherwise CarbonShift's SSRF guard rejects them. See `carbonshift/rust/PLAN_SERVICE.md`.
3. **Client** (this directory): `uvicorn app.main:app --port 8100`

## Usage

Start sending a request batch in the background; the HTTP response does not wait for the batch to finish:

```sh
curl -X POST localhost:8100/run/send-batch -H 'Content-Type: application/json' -d '{
  "task": "text_generation", "count": 5, "deadline_seconds": 30, "source": "synthetic"
}'
```

To select a shared budget, add `qos_profile_id` without replacing the task kind:

```json
{
  "task": "question_answering",
  "qos_profile_id": "qa-standard-v1",
  "count": 5,
  "deadline_seconds": 30
}
```

The profile must already be registered with CarbonShift using `POST /v1/profiles`. The client's `register_qos_profile` helper and `scripts/push_flavours.py` can register profiles; clients that share a budget must use the same ID and definition.

Alternatively, use the CLI script:

```sh
python scripts/send_batch.py --task text_generation --count 5 --deadline-seconds 30
```

Then inspect the request status:

```sh
curl localhost:8100/requests               # all tracked requests
curl localhost:8100/requests/<request_id>   # details for one request
curl localhost:8100/metrics/summary         # aggregated report
```

**Downloaded models:** If the executor only has the `text_generation` models downloaded, use `--task text_generation`. Models for `ner` and `question_answering` are downloaded on first use; alternatively, download them first with `executor/scripts/download_models.py`.

## Time slots and dated deadlines

In addition to the relative `deadline_seconds` used by `/run/send-batch`, `POST /run/send-plan` accepts **absolute timestamps** (including dates to avoid ambiguity) for `start_at` (when the request arrives) and `deadline_at` (when it must be completed). Both are discretized into `slot_minutes` time slots (default 60); for example, a `deadline_at` of 16:43 with 60-minute slots becomes a deadline of 17:00, the end of the 16:00–17:00 slot.

```sh
curl -X POST localhost:8100/run/send-plan -H 'Content-Type: application/json' -d '{
  "requests": [
    {"task": "text_generation", "qos_profile_id": "text_generation-calibrated-v1",
     "input": {"prompt": "Hello"},
     "start_at": "2026-08-26T19:00:00Z", "deadline_at": "2026-08-26T19:32:00Z"}
  ],
  "slot_minutes": 30,
  "mode": "realtime"
}'
```

With `mode: "realtime"`, the client submits the current slot's batch and then generates a tick at each boundary aligned to the shared epoch. The batch is submitted when its time slot begins, without waiting for a provider tick. With `mode: "emulated"`, the client instead consumes manual provider ticks; see below.

## Manual-clock emulation

To test a plan that spans hours or days of traffic without waiting for real time, use `mode: "emulated"`. The client stores the plan; the provider coordinates each slot. At slot entry, CarbonShift and the executor enter the slot before the provider ticks the client, allowing the scheduler to dispatch already-due assignments and the executor to process them while the client submits more requests for that slot. Before advancing, the provider waits for every client's `TickResponse` and asks CarbonShift to close the slot; CarbonShift flushes partial batches and waits for executor callbacks for all jobs due in that slot. Only after this barrier does the provider advance the clocks. The executor's advance endpoint does not wait for inference; its worker continues in the background.

The provider starts its virtual clock at **local midnight on the current day**. The first simulated time slot is therefore 00:00–01:00 (or the configured slot interval), even if the simulation starts later. The configured global epoch does not change: the provider preserves global slot IDs and sends all peers the same UTC timestamp for virtual midnight. Compose defaults to `TZ=UTC`; to use local midnight, set the same `TZ` (for example, `Europe/Rome`) for every service. The timezone and slot duration must align local midnight to a slot boundary. Run CarbonShift with `MANUAL_CLOCK=1` and the executor with `EXECUTOR_MANUAL_CLOCK=1`; it is also useful to lower `SUBMIT_WAIT_TIMEOUT_SECS` (for example, to `0.3`) because otherwise each request may wait for the default five-second submission timeout. See `carbonshift/rust/PLAN_SERVICE.md` for the full protocol rationale.

```sh
python scripts/run_emulation.py --slots 4 --per-slot 3 \
  --task text_generation --executor-url http://localhost:9000
```

`--slot-minutes` defaults to `PROVIDER_SLOT_MINUTES`. For 30-minute slots, configure `PROVIDER_SLOT_MINUTES=30`, `SLOT_DURATION_SECONDS=1800`, and `EXECUTOR_SLOT_MINUTES=30` for the stack; the client rejects plans whose duration does not match its slot clock.

By default, the script registers the calibrated profile with the stable ID `<task>-calibrated-v1` and includes it in every plan request. If a new calibration changes the definition, use `--profile-version v2` to register a new profile rather than overwriting the existing one. To have multiple clients submit requests under the same profile, one client can register it and the others can use `--no-register --qos-profile-id question_answering-calibrated-v1`. To use the task kind's default profile, pass `--no-register` without `--qos-profile-id`; the field is omitted.

The visualizer reads individual request status and callback data directly from CarbonShift (`GET /v1/requests`), so it does not require a client URL. It displays `completed`, `in progress`, `failed`, and `pending` according to the lifecycle maintained by the scheduler.

The capacity-tier ladder is global and does not belong to a profile. To set it explicitly for an emulation, add:

```sh
python scripts/run_emulation.py --slots 4 --per-slot 3 \
  --set-global-capacity-tiers
```

This reconstructs the ladder used before the QoS-profile migration (`per-slot` at multiplier 1.0, up to `1.5 × per-slot` at multiplier 1.5, followed by overflow at 5.0) and sends it **once before the plan**. This is a global override that affects requests from every profile and client on that CarbonShift instance. Without the flag, the client leaves the server's configured tiers unchanged. The API-key-protected `PUT /v1/admin/capacity-tiers` endpoint returns `204` when the global ladder has been replaced. An update waits for active batches to finish solving and committing; existing assignments and baselines retain their original costs, while subsequent calculations use the new ladder. Set the ladder before submitting a plan.

The script generates a synthetic plan (or a dataset-based plan with `--source dataset`) spanning N time slots and submits it. Follow the results with `curl localhost:8100/requests` or `/metrics/summary`; a plan representing hours or days can complete in a few seconds of real time.

If omitted, `--seed` is **random on every run** and printed to the console (for example, `seed=123456789 (random)`) so it can be reused later to reproduce the same plan. Pass `--seed N` to fix it. The previous default was always `42`, so every run produced identical requests; runs now vary unless you set the seed. With the default `source: "synthetic"`, examples are still selected with `random.choice` from a small pool of five variants per task. Use `--source dataset` for real variety across hundreds or thousands of examples.

### With Docker Compose

From the workspace root (not this directory), the one-off `emulation` service runs the script inside the Docker network and is already configured to reach `client`, `carbonshift`, and `executor` by hostname. Only workload options such as `--task` and `--slots` need to be supplied.

```sh
cd ..
export MANUAL_CLOCK=1 SUBMIT_WAIT_TIMEOUT_SECS=0.3 DISPATCHER_POLL_INTERVAL_MS=20
docker compose down   # if the stack was already running with other variables
docker compose up -d --build
docker compose run --rm emulation --slots 4 --per-slot 3 --task text_generation
```

For realtime operation, set `MANUAL_CLOCK=0`. CarbonShift, the client, and the executor follow the shared UTC clock boundaries; the client generates its own ticks without depending on periodic provider notifications.

For multi-client emulation, use the Compose `multi-client` profile, which adds a second client service with isolated callbacks and metrics. By default, the runner sends an NER plan to the client on port `8100` and a question-answering plan to the client on port `8101`; the provider and runner share the configured client endpoint list. Add more clients with additional `--client-url` and `--task` pairs. If `PROVIDER_CLIENT_URLS` is unset, the provider continues to use the single `CLIENT_URL`.

```sh
cd ..
export MANUAL_CLOCK=1 SUBMIT_WAIT_TIMEOUT_SECS=0.3
export PROVIDER_CLIENT_URLS=http://client:8100,http://client-2:8100
docker compose --profile multi-client up -d --build carbonshift executor client client-2 provider
docker compose --profile multi-client run --rm emulation-multi \
  --slots 4 --per-slot 3
```

`--per-slot` applies to each client; the global ladder is sized for the combined load. For a single client, pass one `--client-url` and `--task` pair. If using custom QoS profiles, provide one `--qos-profile-id` for each pair.

## Client API

| Endpoint | Purpose |
|---|---|
| `POST /run/send-batch` | Starts sending `count` requests for the selected task in the background; accepts an optional `qos_profile_id` and relative `deadline_seconds`. `source` can be `"synthetic"` (default) or `"dataset"` (Hugging Face). |
| `POST /run/send-plan` | Accepts a plan with absolute `start_at`/`deadline_at` values grouped by time slot; each request may include an optional `qos_profile_id`. |
| `POST /callback` | Receives the final result from CarbonShift (`CallerCallbackPayload`); do not call it manually. |
| `GET /requests` | Lists all tracked requests and their status. |
| `GET /requests/{id}` | Returns a request's details; the ID is assigned by CarbonShift. |
| `GET /metrics/summary` | Returns a detailed JSON report aggregated by `task/flavour`. |
| `GET /metrics/progress` | Returns a quick overview of the current test, including counts and key averages; designed for repeated polling with `curl`. |
| `GET /health` | Liveness. |

## Collected metrics

Metrics are stored per request in `data/metrics.jsonl` as soon as the result or timeout is recorded:
- `ack_latency_seconds`: time between submission and CarbonShift's synchronous response (**does not** include executor processing time).
- `execution_time_sec`: executor-only model execution time in seconds, when available in the result forwarded by CarbonShift.
- `end_to_end_seconds`: time between submission and the final callback, including scheduling delay and execution.
- `late`: `true` if `end_to_end_seconds` exceeds the request's `deadline_seconds`.
- `carbon_cost`, `flavour`, `scheduled_slot`, and `eta_seconds`: values returned by CarbonShift.
- `qos_profile_id`: the profile ID actually resolved by CarbonShift, including the default profile when the client did not select one.
- `scheduled_at`: ISO 8601 timestamp when CarbonShift's DP solver committed the request assignment; distinct from `callback_received_at`, when the executor result arrived. It is `null` if the request was never scheduled. If the synchronous submission response was still `pending` (`scheduled_slot`, `flavour`, `carbon_cost`, and `scheduled_at` are all `null`), the client fetches `GET /v1/requests/{id}` when the callback arrives to refresh them. A callback only arrives after the request has been scheduled and dispatched, so the values are available then.
- `baseline_carbon_cost`: CarbonShift's estimate of the cost to execute the same request immediately with the most accurate (and costly) flavour, without carbon-intensity optimization. It is computed at submission time and is the baseline for measuring savings.
- `carbon_saving_pct`: `(baseline_carbon_cost - carbon_cost) / baseline_carbon_cost * 100`, the savings from CarbonShift's flavour and slot selection compared with the **analytical** baseline (an estimate, not a measurement).
- `baseline_execution_time_sec`: **measured** execution time in seconds for a shadow run using the `Accurate` flavour on the same input. The executor runs it immediately after the assigned flavour unless `EXECUTOR_COMPUTE_QUALITY_BASELINE=0` (see `executor/README.md`). Unlike `baseline_carbon_cost`, this is an **empirical** value that can be used to estimate real time or energy savings.
- `energy_saving_pct`: `(baseline_execution_time_sec - execution_time_sec) / baseline_execution_time_sec * 100`, the empirical equivalent of `carbon_saving_pct`, based on measured execution times rather than the durations assumed in `config.rs`.
- `confidence` and `quality_score`: values from the executor result. `confidence` is populated whenever the model can produce it (the pipeline's native score for QA/NER, or an executor-calculated `exp(mean log-prob)` self-score over generated tokens for `text_generation`). `quality_score` uses, in order of priority: (1) a caller-provided `reference*` field, when present (ground truth); otherwise, (2) a comparison between the assigned flavour's output and the output of the `Accurate` shadow run (a ground-truth proxy, available unless `EXECUTOR_COMPUTE_QUALITY_BASELINE=0`). It remains `null` only when neither value is available.
- Status: `submitted` → `completed` | `failed` | `timed_out` (no callback received within `CLIENT_CALLBACK_TIMEOUT_SECONDS`, default 120 seconds).

`GET /metrics/summary` aggregates these values by `task/flavour`: counts, late-request rate (`late_rate`), minimum/average/maximum latency, measured and baseline execution time, carbon cost/baseline/savings (analytical), energy savings (empirical), confidence, and quality score. The same aggregation across **all** requests, regardless of task or flavour, is available in `overall`. The `scheduler` section is read **live from CarbonShift**, not derived from client-tracked requests, so it is `null` if CarbonShift is unreachable. `global_error_avg` and `global_error_count` are descriptive fleet-wide telemetry, not QoS guarantees across different error semantics. `profiles.<qos_profile_id>` exposes the task kind, error semantics, threshold, window, and cumulative constraint for profiles in use.

`GET /metrics/progress` provides a lightweight snapshot of the entire test, rather than a breakdown by task and flavour. It reports submitted, scheduled, completed, failed, and timed-out requests, along with average `quality_score`, `confidence`, `execution_time_sec`, `ack_latency_seconds`, `carbon_saving_pct`, and `energy_saving_pct`.

```sh
curl localhost:8100/metrics/progress
```

## Structured tests (battery runner)

For repeatable performance evaluation, whether with a few micro-tests (5–10 requests to check that the architecture works) or larger runs, use the script in `tests/battery/`. It follows the approach of `carbonshift/tests/battery/run_battery.py` (configuration-driven scenarios, timestamped output directory, CSV, and README) but uses a different output directory and format because it tests HTTP, async processing, and callbacks rather than the standalone Rust solver.

```sh
python tests/battery/run_battery.py --config tests/battery/battery_config_micro.json   # 6 requests, sanity check
python tests/battery/run_battery.py --config tests/battery/battery_config.json         # mixed scenarios, including one Hugging Face dataset
```

Each scenario in the configuration file can specify `task`, `count` (number of requests), `per_slot`/`slot_minutes`, `source` (`synthetic` or `dataset`, with the corresponding Hugging Face dataset selected in `app/datasets.py`), `seed`, and `mode` (`realtime` or `emulated`). The flavour cannot be set directly because CarbonShift selects it, but results are reported by flavour through `/metrics/summary`. The runner uses the provider's `/v1/slot` as its reference and, in `emulated` mode, advances the provider rather than CarbonShift or the executor directly so the client, scheduler, and executor receive the same slot and timestamp. Scenario slot durations must match `PROVIDER_SLOT_MINUTES`, `SLOT_DURATION_SECONDS / 60`, and `EXECUTOR_SLOT_MINUTES`; the default scenario files use the 60-minute Compose setting.

Each run writes to `tests/battery/results/<battery_id>_<timestamp>/`:
- `battery_config.json`: snapshot of the configuration used.
- `README.md`: per-scenario summary table (completed/failed/timed-out requests, average quality/confidence, average execution time, and average carbon savings).
- `results.csv`: the same metrics in tabular form.
- One subdirectory per scenario, containing `config.json`, `requests.jsonl` (raw per-request records), and `metrics.json`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CARBONSHIFT_URL` | `http://localhost:8080` | CarbonShift base URL. |
| `CARBONSHIFT_API_KEY` | *(unset)* | Set this if CarbonShift has `CARBONSHIFT_API_KEY` configured. |
| `CLIENT_SELF_BASE_URL` | `http://localhost:8100` | URL this client advertises to CarbonShift for callbacks. |
| `CLIENT_HOST` / `CLIENT_PORT` | `0.0.0.0` / `8100` | HTTP bind address (overridden by `uvicorn --host/--port`). |
| `CLIENT_HTTP_TIMEOUT_SECONDS` | `10` | Timeout for calls to CarbonShift. |
| `CLIENT_CALLBACK_TIMEOUT_SECONDS` | `120` | Time after which a request without a callback is marked `timed_out`. |
| `CLIENT_METRICS_PATH` | `data/metrics.jsonl` | Append-only metrics file. |
| `CLIENT_MANUAL_CLOCK` | `MANUAL_CLOCK`, otherwise `0` | In emulation, consume provider ticks; in realtime, enable the local epoch-aligned ticker. |
| `PROVIDER_URL` | `http://localhost:9100` | Provider used for slot and observation data. |
| `PROVIDER_EPOCH` | `2020-01-01T00:00:00+00:00` | Must match the epoch configured on the provider and CarbonShift. |
| `PROVIDER_SLOT_MINUTES` | `60` | Time-slot duration; must match CarbonShift and the executor. |
| `TZ` | `UTC` in Compose | Shared local timezone; determines virtual midnight in emulation. |
| `CLIENT_QOS_PROFILE_VERSION` | `v1` | Stable version used in calibrated profile IDs; increment it when the policy changes. |
| `CLIENT_QOS_PROFILE_THRESHOLD_POSITION` | `0.75` | Position between minimum and maximum error used to set calibrated profile thresholds. |
| `CLIENT_QOS_PROFILE_STATS_PATH` | `model_stats.json` in the client service | Calibration snapshot used to rebuild calibrated profiles at startup. |
| `CLIENT_QOS_PROFILE_DEFINITIONS_PATH` | *(unset)* | Optional path to a JSON array of complete QoS profiles to restore at startup. |
| `CLIENT_QOS_PROFILE_REGISTRATION_ATTEMPTS` | `10` | Initial registration attempts per profile, to handle service startup order. |
| `CLIENT_QOS_PROFILE_REGISTRATION_RETRY_SECONDS` | `2` | Delay between transient profile-registration retries. |

## Tests

`tests/test_tracker.py` contains unit tests that require no network. `tests/test_server.py` exercises HTTP through `TestClient` with `carbonshift_client.submit` replaced by a stub. Neither CarbonShift nor the executor needs to be running, and the `datasets` package is not required.

```sh
pip install -r requirements-dev.txt
pytest -q
```

## Docker

```sh
docker build -t carbonshift-client .
docker run --rm -p 8100:8100 -e CARBONSHIFT_URL=http://<host>:8080 carbonshift-client
```

To run the full stack (CarbonShift, executor, and client), use the workspace-root `docker-compose.yml`:

```sh
cd ..
docker compose up --build
```

## Task-specific calibrated flavours

CarbonShift does not inherently know about tasks. Its three default flavours (`Accurate`, `Balanced`, and `Fast`) form one global list with roughly estimated errors. To schedule using **measured error and cost for each task**, do the following:

1. **Calibrate once** in the **executor** environment, which requires `torch` and `transformers`. The calibration runs every configured model and measures actual error (from `quality_score`) and average execution time.
   ```sh
   cd ../executor
   python scripts/calibrate_models.py --samples 20
   ```
This writes or updates `client/model_stats.json`, a dictionary **indexed by model name** (not by task or flavour). You can change or add models in `executor/app/config.py` and recalibrate only those models without losing older measurements for models that are no longer configured.
2. **Register profiles with CarbonShift:** Group `model_stats.json` by task and register a versioned profile for each task kind.
   ```sh
   python scripts/push_flavours.py
   ```
Profiles use stable IDs (`<task-kind>-calibrated-v1`), measured flavours, an interpolated threshold, and the error semantics stored in the calibration data. The registration explicitly defines the window and cumulative constraint, so clients reusing the same ID must submit the same definition. If recalibration changes a policy, use `--profile-version v2` to create a different ID. Requests select the budget through `qos_profile_id`; the task kind still describes the executor operation. Different profiles do not share budgets and are scheduled in separate batches. Capacity tiers remain global and count requests from every profile.

The default threshold is 75% of the range between the minimum and maximum errors of the calibrated flavours; use `--threshold-position` to change it. Thresholds and cumulative constraints apply to the QoS profile, not to a hard aggregate average across different tasks. The `/v1/stats` endpoint retains a fleet-wide average only as descriptive telemetry.
3. **Correct with measured error** in live emulation or production, not in offline simulations that do not use CarbonShift. When the executor callback includes `result.quality_score`, CarbonShift replaces the assigned flavour's *predicted* error with the task-reported value in the associated profile's window and totals. See `carbonshift/rust/PLAN_SERVICE.md`.

## Restart and restore QoS profiles

CarbonShift stores custom QoS profiles in memory. At startup, the client rebuilds calibrated profiles from its `model_stats.json` and registers any complete profiles configured through `CLIENT_QOS_PROFILE_DEFINITIONS_PATH`. Identical registrations are idempotent; reusing an ID with a different definition is an error and does not overwrite the existing profile. The client retries connection and server errors up to configured limits; permanent definition errors prevent startup. When calibration changes, increment the version and keep `CLIENT_QOS_PROFILE_VERSION`, `push_flavours.py --profile-version`, and `run_emulation.py --profile-version` aligned. The new profiles receive distinct IDs and can be restored from the same configuration on each client.

If CarbonShift restarts while the client remains active, a request using a profile known to the local catalog triggers one idempotent registration attempt and is submitted once more. If the profile is not in the local catalog, submission fails explicitly; it is not silently replaced with a default profile.

For a custom profile, configure the file on every client that must be able to restore it:

```json
[
  {
    "profile_id": "qa-standard-v2",
    "task_kind": "question_answering",
    "flavours": [{"name": "Accurate", "error": 4.0, "duration_sec": 120.0}],
    "error_semantics": "word-overlap-f1-v1",
    "max_error_threshold": 10.0,
    "error_window": {"past_slots": 12, "future_slots": 14, "past_decay_slots": 12},
    "cumulative_error": {"enabled": true, "hard": true}
  }
]
```

`CLIENT_QOS_PROFILE_DEFINITIONS_PATH` must point to this file **inside** each client container. A process cannot restore a custom profile registered only by a script or another client unless it has the profile definition.

Restoration covers **definitions** and stable IDs, not scheduler-state persistence. A CarbonShift restart clears in-memory queues, assignments, error counters, and history; it does not reinterpret a profile definition under a different ID. Requests use `task_kind` to identify the operation and `qos_profile_id` to select the budget. Monitoring filters use the same profile ID.

## Known simplifications

- Requests are not retried when submission to CarbonShift fails; the request is logged and skipped, except for the single idempotent restoration attempt when CarbonShift reports that its in-memory profile registry was cleared.
- `source: "dataset"` for NER uses `tomaarsen/conll2003`, a Parquet mirror without loading scripts; recent `datasets` versions can no longer load the original `conll2003` dataset.
- Queues and request state are not persisted beyond the metrics JSONL file. A client restart loses tracking for in-flight requests; callbacks that arrive for unknown IDs are only logged.
