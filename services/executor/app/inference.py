"""Runs the configured HuggingFace model for a task and scores the result.

`torch`/`transformers` are imported lazily inside functions (not at module
import time) so the rest of the app — queueing, HTTP contract, metrics — can
be imported and unit-tested without those (large) dependencies installed.
"""
from __future__ import annotations

import logging
import re
import string
import time
from collections import OrderedDict
from typing import Any

from .config import Flavour, Task, resolve_model, settings

logger = logging.getLogger("executor.inference")


def _resolve_device() -> int:
    """transformers pipeline `device` arg: -1 = CPU, >=0 = CUDA device index."""
    import torch

    if settings.device == "cpu":
        return -1
    if settings.device == "cuda":
        return 0 if torch.cuda.is_available() else -1
    return 0 if torch.cuda.is_available() else -1  # auto


class PipelineCache:
    """LRU cache of loaded pipelines, bounded so a small GPU (e.g. a 3GB
    GTX 1060) isn't asked to hold all 9 configured models resident at once."""

    def __init__(self, max_size: int):
        self.max_size = max_size
        self._cache: "OrderedDict[tuple[str, str], Any]" = OrderedDict()

    def get(self, task: str, flavour: str):
        key = (task, flavour)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        from transformers import pipeline
        import transformers
        transformers.utils.logging.set_verbosity_error()

        hf_task, model_id = resolve_model(task, flavour)
        device = _resolve_device()
        logger.info("loading model task=%s flavour=%s model=%s device=%s", task, flavour, model_id, device)
        if hf_task == "question-answering":
            from app.qa_pipeline import load_qa_pipeline
            pipe = load_qa_pipeline(model_id)
        elif hf_task == "ner":
            # Without this, entities come back as raw per-token BIO labels
            # (e.g. "B-PER"/"I-PER", one entity per (sub)token instead of a
            # merged span) — never matching CoNLL-style bare category labels
            # ("PER") used by reference_entities, so ground-truth quality_score
            # was always computing 0 overlap (100% error) for every example.
            pipe = pipeline(hf_task, model=model_id, device=device, aggregation_strategy="simple")
        else:
            pipe = pipeline(hf_task, model=model_id, device=device)
        self._cache[key] = pipe
        self._cache.move_to_end(key)
        self._evict_if_needed()
        return pipe

    def _evict_if_needed(self):
        while len(self._cache) > self.max_size:
            evicted_key, _evicted_pipe = self._cache.popitem(last=False)
            logger.info("evicting cached model %s to bound memory usage", evicted_key)
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass


_cache = PipelineCache(settings.max_loaded_models)


def _normalize_answer_text(s: str) -> str:
    """SQuAD-style normalization (lowercase, strip punctuation/articles) so
    e.g. "Paris," and "Paris" count as the same token instead of silently
    zeroing out the F1 overlap below."""
    s = re.sub(r"\b(a|an|the)\b", " ", s.lower())
    s = "".join(ch for ch in s if ch not in string.punctuation)
    return " ".join(s.split())


def _word_overlap_f1(predicted: str, reference: str) -> float:
    """Dependency-free SQuAD-style token-overlap F1, used both for
    `reference*`-based `quality_score` and for comparing two model outputs
    (see `_compare_outputs`)."""
    pred_tokens = _normalize_answer_text(predicted).split()
    ref_tokens = _normalize_answer_text(reference).split()
    if not pred_tokens or not ref_tokens:
        return 0.0

    ref_counts: dict[str, int] = {}
    for t in ref_tokens:
        ref_counts[t] = ref_counts.get(t, 0) + 1
    pred_counts: dict[str, int] = {}
    for t in pred_tokens:
        pred_counts[t] = pred_counts.get(t, 0) + 1

    overlap = sum(min(c, ref_counts.get(t, 0)) for t, c in pred_counts.items())
    if overlap == 0:
        return 0.0
    precision = overlap / len(pred_tokens)
    recall = overlap / len(ref_tokens)
    return 2 * precision * recall / (precision + recall)


def _entity_set_f1(pred_entities: list[dict[str, Any]], ref_entities: list[dict[str, Any]]) -> float:
    """F1 over (text, label) entity sets — used both for `reference_entities`
    and for comparing two NER outputs (see `_compare_outputs`)."""
    pred_set = {(e["text"].lower(), e["label"]) for e in pred_entities}
    ref_set = {(e["text"].lower(), e["label"]) for e in ref_entities}
    overlap = len(pred_set & ref_set)
    precision = overlap / len(pred_set) if pred_set else 0.0
    recall = overlap / len(ref_set) if ref_set else 0.0
    return 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0


def _text_generation_confidence(pipe, prompt: str, generated_text: str) -> float | None:
    """Self-supervised confidence for text generation: `exp(mean log-prob)`
    the model itself assigns to the tokens it generated, computed via a
    single teacher-forced forward pass over its own output (no external
    reference needed, unlike `quality_score`).

    Returns `None` on any tokenization/shape issue (e.g. nothing was
    actually generated) rather than raising, since a scoring problem must
    never break the actual generation result.
    """
    try:
        import torch

        tokenizer = pipe.tokenizer
        model = pipe.model
        device = model.device

        prompt_ids = tokenizer(prompt, return_tensors="pt").input_ids.to(device)
        full_ids = tokenizer(generated_text, return_tensors="pt").input_ids.to(device)
        prompt_len = prompt_ids.shape[1]
        if full_ids.shape[1] <= prompt_len:
            return None

        with torch.no_grad():
            logits = model(full_ids).logits

        log_probs = torch.log_softmax(logits, dim=-1)
        continuation_ids = full_ids[0, prompt_len:]
        # logits at position i predict the token at position i+1.
        shifted_log_probs = log_probs[0, prompt_len - 1 : full_ids.shape[1] - 1, :]
        token_log_probs = shifted_log_probs.gather(1, continuation_ids.unsqueeze(-1)).squeeze(-1)
        mean_log_prob = token_log_probs.mean().item()
        return float(torch.exp(torch.tensor(mean_log_prob)))
    except Exception:
        logger.warning("failed to compute text_generation confidence", exc_info=True)
        return None


def _run_single(task: str, flavour: str, task_input: dict[str, Any]) -> dict[str, Any]:
    """Runs exactly one `(task, flavour)` pipeline once.

    Returns `{output, model, confidence, execution_time_seconds}` — no
    `quality_score`, since that requires a reference to compare against
    (either caller-supplied or another flavour's output; see `run_task`,
    which may call this twice).
    """
    _, model_id = resolve_model(task, flavour)
    pipe = _cache.get(task, flavour)

    t0 = time.perf_counter()

    if task == Task.TEXT_GENERATION:
        prompt = task_input["prompt"]
        max_new_tokens = int(task_input.get("max_new_tokens", 50))
        out = pipe(prompt, max_new_tokens=max_new_tokens, num_return_sequences=1)
        generated = out[0]["generated_text"]
        confidence = _text_generation_confidence(pipe, prompt, generated)
        output = {"generated_text": generated}

    elif task == Task.NER:
        text = task_input["text"]
        out = pipe(text)
        entities = [
            {
                "text": e["word"],
                "label": e.get("entity_group", e.get("entity")),
                "score": float(e["score"]),
                "start": e.get("start"),
                "end": e.get("end"),
            }
            for e in out
        ]
        confidence = sum(e["score"] for e in entities) / len(entities) if entities else None
        output = {"entities": entities}

    elif task == Task.QUESTION_ANSWERING:
        question = task_input["question"]
        context = task_input["context"]
        out = pipe(question=question, context=context)
        confidence = float(out["score"])
        output = {"answer": out["answer"], "start": out.get("start"), "end": out.get("end")}

    else:
        raise ValueError(f"unsupported task {task!r}")

    elapsed = time.perf_counter() - t0
    return {"output": output, "model": model_id, "confidence": confidence, "execution_time_seconds": elapsed}


def _reference_quality_score(task: str, task_input: dict[str, Any], output: dict[str, Any]) -> float | None:
    """`quality_score` against a caller-supplied ground-truth `reference*`
    field, when present. Takes priority over `_compare_outputs` (an actual
    ground truth beats a same-model-family proxy)."""
    if task == Task.TEXT_GENERATION:
        reference = task_input.get("reference")
        if not reference:
            return None
        prompt = task_input.get("prompt", "")
        return _word_overlap_f1(output["generated_text"][len(prompt):], reference)
    if task == Task.QUESTION_ANSWERING:
        reference = task_input.get("reference_answer")
        return _word_overlap_f1(output["answer"], reference) if reference else None
    if task == Task.NER:
        reference_entities = task_input.get("reference_entities")
        return _entity_set_f1(output["entities"], reference_entities) if reference_entities else None
    return None


def _compare_outputs(task: str, task_input: dict[str, Any], primary_output: dict[str, Any],
                      accurate_output: dict[str, Any]) -> float | None:
    """`quality_score` proxy when the caller supplied no `reference*` field:
    compares the assigned flavour's output against the Accurate flavour's
    own output on the same input (see `run_task`'s "quality baseline" pass),
    so every request gets a quality signal, not just ones with ground truth.
    """
    if task == Task.TEXT_GENERATION:
        prompt = task_input.get("prompt", "")
        pred = primary_output["generated_text"][len(prompt):]
        ref = accurate_output["generated_text"][len(prompt):]
        return _word_overlap_f1(pred, ref)
    if task == Task.QUESTION_ANSWERING:
        return _word_overlap_f1(primary_output["answer"], accurate_output["answer"])
    if task == Task.NER:
        return _entity_set_f1(primary_output["entities"], accurate_output["entities"])
    return None


def run_task(task: str, flavour: str, task_input: dict[str, Any]) -> dict[str, Any]:
    """Runs the model configured for `(task, flavour)`.

    Returns `{output, model, confidence, quality_score, actual_error_pct,
    execution_time_seconds, baseline_execution_time_seconds, baseline_model}`.

    `confidence` is always populated when the model can produce one: the
    pipeline's own score for QA/NER, or a self-supervised exp(mean log-prob)
    score for text_generation (see `_text_generation_confidence`).

    `quality_score`: a caller-supplied `reference*` field wins if present
    (real ground truth); otherwise, unless `flavour` is already "accurate"
    and `settings.compute_quality_baseline` is enabled, it's computed by
    comparing this flavour's output against a same-input "shadow" run of
    the Accurate flavour (see `_compare_outputs`) — so quality differences
    between flavours are measurable without needing an external dataset
    with ground-truth references. For the Accurate flavour itself there is
    no comparison target, so it defaults to `1.0` (perfect) instead of `null`
    when no `reference*` was supplied either — consistent with `error_pct`
    always being 0% for Accurate elsewhere in this project.

    `actual_error_pct`: what carbonshift's `POST /v1/callback/{id}` handler
    uses to correct its scheduler's predicted-error average with the real
    outcome (see `service::handlers::executor_callback` /
    `SharedState::correct_assignment_error`). For QA/NER this is
    `(1 - quality_score) * 100` (word-overlap F1 is a reasonable proxy
    there). For text_generation it is instead the same *relative confidence
    degradation vs Accurate* used by `calibrate_models.py` — word-overlap F1
    is not meaningful for open-ended generation (even Accurate would score
    low against any single reference/shadow continuation), and naively using
    `1 - quality_score` there previously corrected the scheduler's global
    error average up to ~80-90% for perfectly normal generations. `None`
    (no correction applied) if nothing usable is available for either.

    `baseline_execution_time_seconds`/`baseline_model`: the Accurate
    flavour's own measured execution time/model on this same input — an
    *empirical* reference point for actual energy/time savings, independent
    of carbonshift's analytical `baseline_carbon_cost` estimate. `None` when
    `settings.compute_quality_baseline` is disabled (set
    `EXECUTOR_COMPUTE_QUALITY_BASELINE=0` for large-scale runs where the
    extra Accurate-flavour pass per request isn't worth the added compute).
    """
    primary = _run_single(task, flavour, task_input)
    quality_score = _reference_quality_score(task, task_input, primary["output"])

    baseline_execution_time_seconds: float | None = None
    baseline_model: str | None = None
    accurate_confidence: float | None = None
    if flavour == Flavour.ACCURATE:
        baseline_execution_time_seconds = primary["execution_time_seconds"]
        baseline_model = primary["model"]
    elif settings.compute_quality_baseline:
        accurate = _run_single(task, Flavour.ACCURATE, task_input)
        baseline_execution_time_seconds = accurate["execution_time_seconds"]
        baseline_model = accurate["model"]
        accurate_confidence = accurate["confidence"]
        if quality_score is None:
            quality_score = _compare_outputs(task, task_input, primary["output"], accurate["output"])

    if flavour == Flavour.ACCURATE:
        # For the Accurate flavour, we could consider the error percentage to be zero
        # since it is considered the reference output.
        actual_error_pct = 0.0
        if quality_score is None:
            quality_score = 1.0
    elif task == Task.TEXT_GENERATION:
        confidence = primary["confidence"]
        actual_error_pct = (
            max(0.0, (accurate_confidence - confidence) / accurate_confidence) * 100.0
            if accurate_confidence and confidence is not None
            else None
        )
    elif quality_score is not None:
        actual_error_pct = (1.0 - quality_score) * 100.0
    else:
        actual_error_pct = None

    return {
        "output": primary["output"],
        "model": primary["model"],
        "confidence": primary["confidence"],
        "quality_score": quality_score,
        "actual_error_pct": actual_error_pct,
        "execution_time_seconds": primary["execution_time_seconds"],
        "baseline_execution_time_seconds": baseline_execution_time_seconds,
        "baseline_model": baseline_model,
    }

