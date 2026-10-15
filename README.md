# CarbonShift

CarbonShift is a carbon-aware workload scheduler. It assigns incoming requests to execution strategies (flavours) and time slots using carbon-intensity forecasts, while respecting deadlines and QoS error budgets. The repository contains the Rust scheduler and four supporting Python services: a client, an executor, a carbon-intensity provider, and a visualizer.

## Request flow

```text
Client --POST /v1/requests--> CarbonShift --POST /dispatch--> Executor
   ^                              ^                              |
   |                              |                              |
   +-- POST /callback <---- POST /v1/callback/{id} <--------------+
                                  ^
                                  |
                         Carbon-intensity provider
```

The client submits work to CarbonShift, never directly to the executor. CarbonShift selects the slot and flavour, dispatches due work, and receives the executor's measured result through a callback. It corrects the recorded cost and error, then forwards the result to the client. The provider supplies forecasts and observed carbon intensity; the visualizer can display live operational data or browse saved snapshots without CarbonShift running.

## Components

| Component | Path | Runtime | Purpose |
|---|---|---|---|
| Scheduler and REST service | [`carbonshift/rust/`](carbonshift/rust/) | Rust, edition 2024 | QoS-aware scheduling, carbon-cost optimization, dispatch, and callback handling |
| Client | [`carbonshift/services/client/`](carbonshift/services/client/) | Python 3.11, FastAPI | Generates or accepts request plans, submits work, and collects end-to-end metrics |
| Executor | [`carbonshift/services/executor/`](carbonshift/services/executor/) | Python 3.11, FastAPI | Runs configured Hugging Face models and reports measured execution outcomes |
| Provider | [`carbonshift/services/provider/`](carbonshift/services/provider/) | Python 3.11, FastAPI | Supplies carbon forecasts and observations and drives manual-clock emulation |
| Visualizer | [`carbonshift/services/visualizer/`](carbonshift/services/visualizer/) | Python 3.11, FastAPI | Displays live or snapshotted assignments, error tracking, costs, and carbon intensity |

The services communicate over HTTP/JSON with callback URLs. They do not use a message broker or shared database. Python services append metrics to local JSONL files; CarbonShift's queues, assignments, and QoS profile definitions are held in memory.

## Domain overview

- **Time slots:** Requests have arrival and deadline slots; assignments target a scheduled slot. All services use the same epoch and slot duration.
- **Flavours:** `Accurate`, `Balanced`, and `Fast` are execution strategies with different expected error and duration trade-offs. CarbonShift chooses a flavour; the executor runs it.
- **QoS profiles:** A stable `qos_profile_id` selects the immutable flavour and error-budget policy used for scheduling. Requests using the same profile share its budget.
- **Capacity tiers:** A global tier ladder applies nonlinear carbon-cost multipliers as the number of assignments in a slot grows. It is not a hard admission limit.
- **Carbon estimates and measurements:** Scheduling uses forecast intensity and expected flavour cost. Executor outcomes and provider observations can correct stored assignment metrics after execution.

## Quick start with Docker Compose

From the repository root, start the five services:

```sh
cd carbonshift
MANUAL_CLOCK=0 docker compose up --build
```

The default ports are CarbonShift `8080`, executor `9000`, client `8100`, provider `9100`, and visualizer `8500`. The executor downloads Hugging Face model weights on first use and stores them in the `hf-cache` Docker volume.

Submit a small request batch and inspect the client's metrics:

```sh
curl -X POST localhost:8100/run/send-batch \
  -H 'Content-Type: application/json' \
  -d '{"task":"text_generation","count":3,"deadline_seconds":30}'

curl localhost:8100/metrics/summary
```

## Manual-clock emulation

Manual-clock mode advances simulated slots without waiting for wall time. Docker Compose enables the manual clocks by default; use the one-off `emulation` service to run a plan:

```sh
cd carbonshift
docker compose down
export MANUAL_CLOCK=1 SUBMIT_WAIT_TIMEOUT_SECS=0.3
docker compose up -d --build
docker compose run --rm emulation --slots 4 --per-slot 3 --task text_generation
```

If the stack is already running in another clock mode, run `docker compose down` before restarting it with the new environment. The provider is the sole clock driver. It synchronizes CarbonShift and the executor, then ticks clients for the active slot. While clients submit requests, CarbonShift schedules work and dispatches assignments due in that slot, and the executor can process them concurrently. Before advancing, the provider waits for every client tick response and calls CarbonShift's `POST /v1/admin/close-slot`; CarbonShift flushes pending batches and waits for callbacks for assignments due in the closing slot. Only after that barrier succeeds does the provider enter the next slot, notifying CarbonShift, the executor, and then the clients. The executor's clock advance wakes its worker without waiting for inference to complete. See the provider [interface contract](carbonshift/services/provider/INTERFACE.md) for the full protocol.

For 30-minute slots, keep the service settings aligned:

```sh
MANUAL_CLOCK=1 \
PROVIDER_SLOT_MINUTES=30 \
EXECUTOR_SLOT_MINUTES=30 \
SLOT_DURATION_SECONDS=1800 \
docker compose up --build
```

For multi-client emulation, use the `multi-client` Compose profile and the `emulation-multi` runner defined in [`carbonshift/docker-compose.yml`](carbonshift/docker-compose.yml).

Both emulation runners save a chart-ready snapshot and a matching client-summary JSON file under `carbonshift/snapshots/YYYYMMDD/`. The multi-client summary retains labels such as `_1` and `_2`. The visualizer mounts this directory, offers `Live data` and `Choose snapshot` modes, and stops polling while a saved snapshot is selected. The visualizer can also export the currently displayed live or saved data under a custom name; snapshots remain available after CarbonShift stops. The shared `snapshots/` directory is mounted read/write because both the runners and the visualizer's export action can create paired files.

## Development and tests

Rust tests are in `carbonshift/rust/`; Python service tests are located under each service's `tests/` directory. The Rust crate can be checked with:

```sh
cd carbonshift/rust
cargo check --offline --all-targets
cargo test --offline --test service_api
```

Run a Python service's tests from its directory after installing `requirements-dev.txt`:

```sh
python -m pytest -q
```

The executor and client defer heavy optional imports so their services and unit tests can run without the full ML or dataset packages when those features are not being exercised.

## Documentation

- [Project documentation](carbonshift/notes/PROJECT_DOCUMENTATION.md) — system overview, responsibilities, metrics, and HTTP surfaces.
- [Component interactions](carbonshift/notes/PROJECT_INTERACTIONS.md) — request lifecycle and manual-clock sequence diagrams.
- [Scheduler structure](carbonshift/rust/SCHEDULER_STRUCTURE.md) — Rust scheduler modules, batch flow, and strategy extension point.
- [Client guide](carbonshift/services/client/README.md) — request plans, emulation, QoS profiles, metrics, and tests.
- [Executor guide](carbonshift/services/executor/README.md) — model execution, API, calibration, and tests.
- [Provider guide](carbonshift/services/provider/README.md) — configuration and run instructions.
- [Provider interface](carbonshift/services/provider/INTERFACE.md) — clock notification and slot-close contract.

Legacy Python scheduling prototypes are kept in [`carbonshift/ignore/`](carbonshift/ignore/) for reference only and are not used by the services.
