# CarbonShift Executor

The executor is an external service in the CarbonShift architecture. It receives HTTP jobs (task type, flavour, and execution time), queues them by time slot, runs **one job at a time** using a Hugging Face model, and returns each result through an HTTP callback.

The executor lives in its own directory beside `carbonshift/` because it is an independent service. It can be tested by itself with `curl` or reached by CarbonShift by setting `EXECUTOR_URL` (see [Integration with CarbonShift](#integration-with-carbonshift)).

## Tasks and models

The executor supports three task types and three flavours each, following the same `Accurate`/`Balanced`/`Fast` concept as CarbonShift. The models are selected to run on common hardware (tested on an i7-7700 with 16 GB RAM and a 3 GB GTX 1060):

| Task | Fast | Balanced | Accurate |
|---|---|---|---|
| `text_generation` | `distilgpt2` (~330MB) | `gpt2` (~500MB) | `gpt2-medium` (~1.4GB) |
| `ner` | `dslim/distilbert-NER` (~260MB) | `dslim/bert-base-NER` (~430MB) | `dslim/bert-large-NER` (~1.3GB) |
| `question_answering` | `distilbert-base-cased-distilled-squad` (~260MB) | `deepset/bert-base-uncased-squad2` (~430MB) | `deepset/bert-large-uncased-whole-word-masking-squad2` (~1.3GB) |

Approximate disk sizes use fp32 weights; on a GPU, fp16 inference uses about half that memory. A 3 GB GTX 1060 cannot fit all nine models in VRAM simultaneously, so the executor keeps a small LRU cache (`EXECUTOR_MAX_LOADED_MODELS`, default 2 concurrently loaded models) and loads other models on demand, evicting the least recently used one. This is not a concern on CPU, though inference is slower.

## Setup

```sh
cd executor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # use requirements-dev.txt for tests
```

**Hugging Face account:** These models do not require an account or login; none are gated. The weights for each `(task, flavour)` are downloaded automatically on first use to `~/.cache/huggingface` (or `$HF_HOME`, if set). To download all models in advance, for example before a demo:

```sh
python scripts/download_models.py
```

For GPU use, `pip install -r requirements.txt` installs `torch` from PyPI, which includes CUDA support on Linux for common GPUs, including the Pascal GTX 1060. Check availability with:

```sh
python -c "import torch; print(torch.cuda.is_available())"
```

If this prints `False` despite an installed NVIDIA driver, follow the instructions for the correct CUDA build at https://pytorch.org/get-started/locally/. The executor also works on CPU alone (`EXECUTOR_DEVICE=cpu`), but more slowly.

## Start

```sh
uvicorn app.main:app --host 0.0.0.0 --port 9000
```

All environment variables are optional:

| Variable | Default | Purpose |
|---|---|---|
| `EXECUTOR_HOST` / `EXECUTOR_PORT` | `0.0.0.0` / `9000` | HTTP bind address (used only when started with `python -m app`; with `uvicorn`, pass `--host` and `--port`). |
| `EXECUTOR_DEVICE` | `auto` | `auto`, `cpu`, or `cuda`. |
| `EXECUTOR_MAX_LOADED_MODELS` | `2` | Number of model pipelines to keep in RAM/VRAM at once. |
| `EXECUTOR_POLL_INTERVAL_SECONDS` | `0.5` | Queue polling interval when empty or when jobs are not yet due. |
| `EXECUTOR_METRICS_PATH` | `data/metrics.jsonl` | Append-only JSONL metrics file. |
| `EXECUTOR_CALLBACK_TIMEOUT_SECONDS` | `10` | HTTP timeout for sending callbacks. |
| `EXECUTOR_MANUAL_CLOCK` | `0` | Freeze the queue clock except when advanced through `POST /admin/advance-slot` (testing/emulation only). |
| `TZ` | `UTC` in Compose | Shared timezone for display; `execute_at` instants are always sent in UTC. |
| `EXECUTOR_SLOT_MINUTES` | `60` | Manual-clock time-slot duration; must match `PROVIDER_SLOT_MINUTES` and `SLOT_DURATION_SECONDS / 60` in CarbonShift. |
| `EXECUTOR_COMPUTE_QUALITY_BASELINE` | `1` | When enabled, requests using a flavour other than `accurate` also run an Accurate-flavour shadow pass on the same input. This supplies measured `baseline_execution_time_sec`/`baseline_model` and a comparison `quality_score` when no `reference*` is provided. It roughly doubles inference work per request; disable it (`0`) for large-scale tests that do not need quality or timing comparisons. |

Flavour durations and executor measurements are expressed in seconds: definitions use `duration_sec`, and results use `execution_time_sec` and `baseline_execution_time_sec`. Inference may take fractions of a second; CarbonShift stores those values without converting them to milliseconds.

## API

### `POST /jobs` — Native submission (standalone testing, without CarbonShift)

```json
{
  "task": "question_answering",
  "flavour": "fast",
  "input": {"question": "Where is the Eiffel Tower?", "context": "The Eiffel Tower is in Paris, France."},
  "execute_at": "2026-08-26T18:00:00Z",
  "callback_url": "http://localhost:8123/my-callback"
}
```
`execute_at` is optional (default: immediately). `callback_url` is optional; if omitted, retrieve the result with `GET /jobs/{request_id}`.

Risposta `202`: `{"request_id", "status": "queued", "execute_at", "queue_position"}`.

`input` formats by task:
- `text_generation`: `{"prompt": str, "max_new_tokens"?: int, "reference"?: str}`
- `ner`: `{"text": str, "reference_entities"?: [{"text": str, "label": str}, ...]}`
- `question_answering`: `{"question": str, "context": str, "reference_answer"?: str}`

The `reference*` fields are optional. When present, the executor also calculates a `quality_score` (token-overlap F1) by comparing the output with the expected value. This is useful when the client uses Hugging Face datasets with known answers.

### `POST /dispatch` — CarbonShift adapter

This uses the exact payload schema sent by CarbonShift's Rust dispatcher (`ExecutorDispatchPayload`; see `carbonshift/rust/src/service/models.rs`): `{request_id, scheduled_slot, execute_at, flavour, carbon_cost, callback_url, payload}`. `execute_at` is the UTC boundary of the assigned slot, serialized as a timezone-aware RFC 3339 timestamp; the executor keeps the job queued until that instant. In realtime mode it uses the system UTC clock and has no independent slot counter. In emulation, the initial announcement sets the exact provider virtual timestamp and each rollover advances by one slot duration. The executor reads `task` and `input` from inside `payload`; see [Integration with CarbonShift](#integration-with-carbonshift). `request_id` is also an idempotency key: repeating an identical dispatch returns the existing queued or completed job without running inference again, while reusing the ID with different data returns `409 Conflict`.

### Other endpoints

- `GET /jobs/{request_id}` — job status and result.
- `GET /queue` — queue snapshot grouped by time slot.
- `GET /models` — configured task → flavour → model registry.
- `GET /metrics/summary` — aggregated JSON report (count, execution time, confidence, and quality score for each task/flavour pair).
- `GET /metrics/raw?limit=100` — most recent raw records.
- `GET /health` — liveness.

Every executed job is also appended to `data/metrics.jsonl` (configurable path), independently of HTTP report requests.

## Esempi curl

```sh
# Text generation
curl -X POST localhost:9000/jobs -H 'Content-Type: application/json' -d '{
  "task": "text_generation", "flavour": "fast",
  "input": {"prompt": "The future of renewable energy is", "max_new_tokens": 30}
}'

# NER
curl -X POST localhost:9000/jobs -H 'Content-Type: application/json' -d '{
  "task": "ner", "flavour": "balanced",
  "input": {"text": "Barack Obama was born in Hawaii and became President of the United States."}
}'

# Question answering
curl -X POST localhost:9000/jobs -H 'Content-Type: application/json' -d '{
  "task": "question_answering", "flavour": "accurate",
  "input": {"question": "Where was Barack Obama born?",
            "context": "Barack Obama was born in Hawaii and became President of the United States."}
}'

# Result (replace <id> with the received request_id)
curl localhost:9000/jobs/<id>

# Metrics report
curl localhost:9000/metrics/summary
```

## Tests

The test suite (`tests/test_api.py`) replaces real model execution (`run_task`) with a fast stub. It verifies HTTP routing, time-slot queues, the `/dispatch` adapter, and metrics **without** downloading or running any model, so neither `torch`/`transformers` nor a GPU is required:

```sh
pip install -r requirements-dev.txt
pytest -q
```

### Manual tests with real models

After installing `torch` and `transformers` from the full `requirements.txt` and starting `uvicorn app.main:app`, use the curl examples above. The first request for each `(task, flavour)` downloads its model if it has not already been fetched with `scripts/download_models.py`; subsequent requests start immediately.

### Model calibration (measured error and cost for CarbonShift)

```sh
pip install datasets  # or use requirements-dev.txt, which already includes it
python scripts/calibrate_models.py --source dataset --samples 30
```

This runs every `(task, flavour)` configured in `app/config.py` on real examples (`--source dataset` is recommended: hundreds or thousands of examples with ground truth, rather than about five synthetic examples per task that can make small-sample averages unstable). It measures actual error (from `quality_score`) and average execution time, then updates `../client/model_stats.json`. The file is indexed by model name, so changing or adding models does not discard older measurements. Each record also includes the versioned ID of the error semantics used for measurement. `client/scripts/push_flavours.py` combines the most recent records by task/flavour and registers immutable QoS profiles with CarbonShift; see the client guide's [task-specific calibrated flavours](../client/README.md#task-specific-calibrated-flavours) section. When calibration changes, register a new profile version instead of reusing an ID with different contents.

For QA/NER, `actual_error_pct` follows the measured `quality_score` even for the Accurate flavour when a ground-truth reference is supplied; it is not forced to zero merely because Accurate is the comparison baseline. Without a reference, Accurate's quality convention is 1.0, so the derived error is zero. Text generation uses relative confidence degradation instead, making Accurate's error zero against itself.

## Manual-clock emulation

For multi-slot tests without waiting for real time, set `EXECUTOR_MANUAL_CLOCK=1`; see `carbonshift/rust/PLAN_SERVICE.md` for the complete protocol shared with CarbonShift. The queue clock is frozen and advances only through `POST /admin/advance-slot`, which moves it to the next slot, wakes the worker, and returns immediately without waiting for inference. The provider is the time master: after CarbonShift closes the previous slot, it advances CarbonShift and the executor, then starts client ticks. CarbonShift does not close a slot until all clients have submitted their requests and callbacks have arrived for all jobs due in that slot. The worker can therefore process jobs while clients are still submitting requests for the same slot. If the slot duration sent by the provider does not match `EXECUTOR_SLOT_MINUTES`, the endpoint returns `409`.

```sh
EXECUTOR_MANUAL_CLOCK=1 EXECUTOR_SLOT_MINUTES=30 uvicorn app.main:app --port 9000
curl -X POST localhost:9000/admin/advance-slot \
  -H 'Content-Type: application/json' \
  -d '{"kind":"announce","slot_start_utc":"2026-08-26T18:00:00Z","slot_minutes":30}'
```

## Integration with CarbonShift and the client

The client (in `../client/`; see its README) sends requests to CarbonShift, not directly to the executor. This service only receives CarbonShift dispatches on `/dispatch`. To connect them:

```sh
EXECUTOR_URL=http://<host-executor>:9000/dispatch \
  ./carbonshift/rust/target/release/carbonshift-service
```

The opaque `payload` field is forwarded unchanged by CarbonShift and must contain `{"task": ..., "input": {...}}` in the format expected by this executor. CarbonShift handles only the slot, flavour, and timing; the job input is carried in `payload`.

## Docker

```sh
docker build -t carbonshift-executor .
docker run --rm -p 9000:9000 -v hf-cache:/root/.cache/huggingface carbonshift-executor
```

The `hf-cache` volume avoids downloading models again after container restarts. See the workspace-root `docker-compose.yml` for the complete stack. The image is CPU-only; GPU support requires an `nvidia/cuda` base image and `--gpus all`, which are not included here for simplicity (see the comments in the `Dockerfile`).

## Known simplifications

- Callbacks are not retried; after one attempt, failures are only logged. A retry policy like CarbonShift's backoff could be added if greater robustness is needed.
- The queue is not persisted. Restarting the service loses jobs that have not run, but metrics already written to disk remain.
- `quality_score` is a simple token-overlap F1 score, not a full NLP metric such as BLEU, ROUGE, or SQuAD exact match. It is sufficient for relative flavour comparisons, not for academic evaluation.
- The Docker image does not include GPU support by default; see the Docker section.
