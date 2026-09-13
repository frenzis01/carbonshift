"""Builds task inputs for the executor either from a handful of hardcoded
examples (`source="synthetic"`, no download needed — good for a first test)
or from ready-made HuggingFace datasets (`source="dataset"`, requires the
`datasets` package and a one-off download, see README.md).
"""
from __future__ import annotations

import random
from typing import Any, Optional

SYNTHETIC_PROMPTS = [
    "The future of renewable energy is",
    "In a small village nestled in the mountains,",
    "Artificial intelligence has changed the way we",
    "The most important lesson I learned from failure was",
    "Climate change is affecting global agriculture because",
]

SYNTHETIC_QA = [
    {"question": "Where is the Eiffel Tower located?",
     "context": "The Eiffel Tower is located in Paris, France, and was completed in 1889.",
     "reference_answer": "Paris"},
    {"question": "Who wrote Romeo and Juliet?",
     "context": "Romeo and Juliet is a tragedy written by William Shakespeare early in his career.",
     "reference_answer": "William Shakespeare"},
    {"question": "What is the tallest mountain in the world?",
     "context": "Mount Everest, located in the Himalayas, is the tallest mountain in the world at 8,849 meters.",
     "reference_answer": "Mount Everest"},
    {"question": "Who developed the theory of relativity?",
     "context": "Albert Einstein developed the theory of relativity, one of the two pillars of modern physics.",
     "reference_answer": "Albert Einstein"},
    {"question": "What year did the first man land on the Moon?",
     "context": "Apollo 11 was the spaceflight that first landed humans on the Moon in 1969.",
     "reference_answer": "1969"},
]

SYNTHETIC_NER_TEXTS = [
    "Barack Obama was born in Hawaii and became President of the United States.",
    "Apple was founded by Steve Jobs in Cupertino, California.",
    "Marie Curie won the Nobel Prize in Physics and later the Nobel Prize in Chemistry.",
    "Amazon was founded by Jeff Bezos in Seattle in 1994.",
    "The United Nations headquarters is located in New York City.",
]


def _cycled_sample(pool: list, count: int, rng: random.Random) -> list:
    """Repeats `pool` in shuffled blocks until `count` items are collected,
    so every item gets (near-)equal representation — unlike `rng.choice()`
    called `count` times, which (especially for `count >> len(pool)`) can by
    chance over/under-represent specific items and skew any average computed
    over the result (e.g. `calibrate_models.py`'s per-flavour error_pct)."""
    out: list = []
    while len(out) < count:
        block = pool[:]
        rng.shuffle(block)
        out.extend(block)
    return out[:count]


def _synthetic_examples(task: str, count: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    if task == "text_generation":
        return [{"input": {"prompt": p, "max_new_tokens": 40}}
                for p in _cycled_sample(SYNTHETIC_PROMPTS, count, rng)]
    if task == "question_answering":
        return [{"input": dict(ex)} for ex in _cycled_sample(SYNTHETIC_QA, count, rng)]
    if task == "ner":
        return [{"input": {"text": t}} for t in _cycled_sample(SYNTHETIC_NER_TEXTS, count, rng)]
    raise ValueError(f"unknown task {task!r}")


def _conll_entities(tokens: list[str], tags: list[str]) -> list[dict[str, str]]:
    """Turns CoNLL-2003 BIO tags into `[{"text", "label"}, ...]` spans."""
    entities: list[dict[str, str]] = []
    current: list[str] = []
    label: Optional[str] = None
    for token, tag in zip(tokens, tags):
        if tag.startswith("B-"):
            if current and label:
                entities.append({"text": " ".join(current), "label": label})
            current, label = [token], tag[2:]
        elif tag.startswith("I-") and label == tag[2:]:
            current.append(token)
        else:
            if current and label:
                entities.append({"text": " ".join(current), "label": label})
            current, label = [], None
    if current and label:
        entities.append({"text": " ".join(current), "label": label})
    return entities


def _dataset_examples(task: str, count: int, seed: int) -> list[dict[str, Any]]:
    from datasets import load_dataset  # heavy import, only needed for source="dataset"

    if task == "text_generation":
        # No `reference` field: unlike QA/NER, open-ended generation has no
        # single correct continuation — comparing word-overlap F1 against
        # the *literal* rest of the source article rejects even a "perfect"
        # model for paraphrasing instead of reproducing it verbatim (this
        # was tried and made calibrated error_pct *worse*, not better: even
        # Accurate scored ~85%). `quality_score` here always uses the
        # same-input shadow-vs-Accurate comparison instead (see inference.py);
        # `calibrate_models.py` uses `confidence` (self-supervised, i.e. how
        # "unsurprised" the model itself is by its own generation) instead of
        # `quality_score` for this task specifically, for the same reason.
        ds = load_dataset("salesforce/wikitext", "wikitext-2-raw-v1", split="train")
        ds = ds.filter(lambda r: len(r["text"].split()) >= 12)
        ds = ds.shuffle(seed=seed).select(range(min(count, len(ds))))
        return [
            {"input": {"prompt": " ".join(row["text"].split()[:12]), "max_new_tokens": 40}}
            for row in ds
        ]

    if task == "question_answering":
        ds = load_dataset("rajpurkar/squad_v2", split="validation")
        ds = ds.shuffle(seed=seed).select(range(min(count, len(ds))))
        examples = []
        for row in ds:
            answers = row["answers"]["text"]
            examples.append({"input": {
                "question": row["question"],
                "context": row["context"],
                "reference_answer": answers[0] if answers else None,
            }})
        return examples

    if task == "ner":
        # `conll2003` still uses a loading script (rejected by newer `datasets`
        # versions); this is a script-free Parquet mirror with the same fields.
        ds = load_dataset("tomaarsen/conll2003", split="validation")
        label_names = ds.features["ner_tags"].feature.names
        ds = ds.shuffle(seed=seed).select(range(min(count, len(ds))))
        examples = []
        for row in ds:
            tags = [label_names[i] for i in row["ner_tags"]]
            examples.append({"input": {
                "text": " ".join(row["tokens"]),
                "reference_entities": _conll_entities(row["tokens"], tags),
            }})
        return examples

    raise ValueError(f"unknown task {task!r}")


def load_examples(task: str, count: int, seed: int = 42, source: str = "synthetic") -> list[dict[str, Any]]:
    if source == "synthetic":
        return _synthetic_examples(task, count, seed)
    if source == "dataset":
        return _dataset_examples(task, count, seed)
    raise ValueError(f"unknown source {source!r}")
