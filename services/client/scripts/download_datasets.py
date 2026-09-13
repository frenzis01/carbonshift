#!/usr/bin/env python3
"""Pre-downloads the HuggingFace datasets used by `source="dataset"`
(not needed for `source="synthetic"`, the default)."""
from __future__ import annotations


def main() -> None:
    from datasets import load_dataset

    print("Downloading wikitext-2-raw-v1 (text_generation prompts)...")
    load_dataset("salesforce/wikitext", "wikitext-2-raw-v1", split="train")

    print("Downloading squad_v2 (question_answering)...")
    load_dataset("rajpurkar/squad_v2", split="validation")

    print("Downloading tomaarsen/conll2003 (ner) -- script-free mirror of conll2003...")
    load_dataset("tomaarsen/conll2003", split="validation")

    print("Done.")


if __name__ == "__main__":
    main()
