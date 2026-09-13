#!/usr/bin/env python3
"""CLI convenience wrapper: triggers a batch send on a running client server
(the client must already be up, e.g. `uvicorn app.main:app --port 8100`).

Usage:
    python scripts/send_batch.py --task text_generation --count 5 --deadline-seconds 30
"""
from __future__ import annotations

import argparse
import json

import requests


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-url", default="http://localhost:8100")
    parser.add_argument("--task", default="text_generation",
                         choices=["text_generation", "ner", "question_answering"])
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--deadline-seconds", type=float, default=30.0)
    parser.add_argument("--source", default="synthetic", choices=["synthetic", "dataset"])
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    resp = requests.post(f"{args.client_url}/run/send-batch", json={
        "task": args.task,
        "count": args.count,
        "deadline_seconds": args.deadline_seconds,
        "source": args.source,
        "seed": args.seed,
    })
    resp.raise_for_status()
    print(json.dumps(resp.json(), indent=2))


if __name__ == "__main__":
    main()
