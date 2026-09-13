#!/usr/bin/env python3
"""Pre-downloads every model in the registry, so the executor doesn't hit
the network on the first request for each (task, flavour). Optional: models
are otherwise downloaded lazily (and cached) on first use anyway.

Usage:
    python scripts/download_models.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import MODEL_REGISTRY  # noqa: E402

# Task HF per cui la pipeline() nativa e' stata rimossa (transformers >= 5.3)
# e va quindi caricata via model+tokenizer diretti.
_MANUAL_LOAD_TASKS = {"question-answering"}


def _download_manual(hf_task: str, model_id: str) -> None:
    if hf_task == "question-answering":
        from app.qa_pipeline import load_qa_pipeline

        load_qa_pipeline(model_id)  # istanzia e quindi scarica/cachea i pesi
    else:
        raise ValueError(f"No manual loader defined for task {hf_task!r}")


def main() -> None:
    from transformers import pipeline

    for task, flavours in MODEL_REGISTRY.items():
        for flavour, (hf_task, model_id) in flavours.items():
            print(f"Downloading {task}/{flavour}: {model_id} ...")
            if hf_task in _MANUAL_LOAD_TASKS:
                _download_manual(hf_task, model_id)
            else:
                pipeline(hf_task, model=model_id)
    print("All models downloaded and cached (see HF_HOME / ~/.cache/huggingface).")


if __name__ == "__main__":
    main()