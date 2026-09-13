"""`_cycled_sample`: synthetic examples must be drawn with (near-)equal
coverage, not `random.choice()` repeated (which can by chance over/under
represent specific items and skew any average computed over the result,
e.g. calibrate_models.py's per-flavour error_pct)."""
from __future__ import annotations

import random
from collections import Counter

from app.datasets import SYNTHETIC_NER_TEXTS, SYNTHETIC_PROMPTS, SYNTHETIC_QA, _cycled_sample, load_examples


def test_cycled_sample_gives_near_equal_coverage():
    pool = ["a", "b", "c"]
    out = _cycled_sample(pool, 9, random.Random(1))
    assert Counter(out) == {"a": 3, "b": 3, "c": 3}


def test_cycled_sample_handles_count_not_a_multiple_of_pool_size():
    pool = ["a", "b", "c"]
    out = _cycled_sample(pool, 7, random.Random(1))
    assert len(out) == 7
    counts = Counter(out)
    assert max(counts.values()) - min(counts.values()) <= 1


def test_cycled_sample_is_reproducible_for_a_fixed_seed():
    pool = list(range(5))
    a = _cycled_sample(pool, 20, random.Random(42))
    b = _cycled_sample(pool, 20, random.Random(42))
    assert a == b


def test_synthetic_text_generation_uses_equal_coverage():
    examples = load_examples("text_generation", 3 * len(SYNTHETIC_PROMPTS), seed=1, source="synthetic")
    prompts = [e["input"]["prompt"] for e in examples]
    assert Counter(prompts) == {p: 3 for p in SYNTHETIC_PROMPTS}


def test_synthetic_qa_and_ner_still_carry_ground_truth_reference():
    qa = load_examples("question_answering", len(SYNTHETIC_QA), seed=1, source="synthetic")
    assert all(e["input"].get("reference_answer") for e in qa)

    ner = load_examples("ner", len(SYNTHETIC_NER_TEXTS), seed=1, source="synthetic")
    assert all("text" in e["input"] for e in ner)
