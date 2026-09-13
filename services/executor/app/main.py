"""FastAPI application: the executor's HTTP surface.

Two submission entry points:
- `POST /jobs`: native API, standalone-testable right now (explicit task,
  flavour, input, execute_at, callback_url).
- `POST /dispatch`: adapter matching carbonshift's `ExecutorDispatchPayload`
  verbatim, so `EXECUTOR_URL` can point straight here once the carbonshift
  client exists (see README.md "Integrazione con carbonshift").
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException

from .config import ALL_TASKS, MODEL_REGISTRY, settings
from .metrics import metrics_store
from .models import CarbonshiftDispatchPayload, JobSubmitRequest, JobSubmitResponse
from .queue_worker import job_queue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("executor.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    job_queue.start()
    logger.info("executor worker started (device=%s, max_loaded_models=%d)",
                settings.device, settings.max_loaded_models)
    yield
    job_queue.stop()


app = FastAPI(title="CarbonShift Executor", lifespan=lifespan)


@app.get("/health")
async def health() -> str:
    return "ok"


@app.get("/models")
async def models() -> dict[str, dict[str, str]]:
    return {
        task: {flavour: model_id for flavour, (_, model_id) in flavours.items()}
        for task, flavours in MODEL_REGISTRY.items()
    }


@app.post("/jobs", status_code=202)
async def submit_job(body: JobSubmitRequest) -> JobSubmitResponse:
    job = job_queue.submit(
        task=body.task,
        flavour=body.flavour,
        task_input=body.input,
        execute_at=body.execute_at,
        callback_url=body.callback_url,
        request_id=body.request_id,
    )
    return JobSubmitResponse(
        request_id=job.request_id,
        status=job.status,
        execute_at=job.execute_at,
        queue_position=job_queue.queue_position(job),
    )


@app.post("/dispatch", status_code=202)
async def dispatch_from_carbonshift(body: CarbonshiftDispatchPayload) -> JobSubmitResponse:
    task = body.payload.get("task")
    if task not in ALL_TASKS:
        raise HTTPException(422, f"payload.task must be one of {ALL_TASKS}, got {task!r}")
    task_input = body.payload.get("input", {})

    execute_at = None
    raw_execute_at = body.payload.get("execute_at")
    if raw_execute_at:
        execute_at = datetime.fromisoformat(raw_execute_at)

    job = job_queue.submit(
        task=task,
        flavour=body.flavour.lower(),
        task_input=task_input,
        execute_at=execute_at,
        callback_url=body.callback_url,
        request_id=str(body.request_id),
        context={"scheduled_slot": body.scheduled_slot, "carbon_cost": body.carbon_cost},
    )
    return JobSubmitResponse(
        request_id=job.request_id,
        status=job.status,
        execute_at=job.execute_at,
        queue_position=job_queue.queue_position(job),
    )


@app.get("/jobs/{request_id}")
async def get_job(request_id: str) -> dict:
    job = job_queue.get(request_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown request_id")
    return job.to_status_dict()


@app.get("/queue")
async def get_queue() -> dict[str, list[str]]:
    return job_queue.snapshot()


@app.post("/admin/advance-slot")
async def advance_slot() -> dict:
    if not settings.manual_clock:
        raise HTTPException(status_code=409, detail="EXECUTOR_MANUAL_CLOCK is not enabled")
    return job_queue.advance_slot()


@app.get("/metrics/summary")
async def metrics_summary() -> dict:
    return metrics_store.summary()


@app.get("/metrics/raw")
async def metrics_raw(limit: int = 100) -> list[dict]:
    return metrics_store.raw(limit=limit)
