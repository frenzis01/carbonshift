"""Tests `ExtractiveQAPipeline.__call__`'s span-extraction logic with a fake
tokenizer/model (no real model download / torch model forward pass needed
beyond plain tensor math)."""
from __future__ import annotations

import torch

from app.qa_pipeline import ExtractiveQAPipeline


class _FakeEncoding(dict):
    def __init__(self, offsets, sequence_ids):
        super().__init__(input_ids=torch.zeros((1, len(sequence_ids)), dtype=torch.long))
        self._offsets = offsets
        self._sequence_ids = sequence_ids

    def pop(self, key):
        assert key == "offset_mapping"
        return torch.tensor([self._offsets])

    def sequence_ids(self, _batch_index):
        return self._sequence_ids


class _FakeOutputs:
    def __init__(self, start_logits, end_logits):
        self.start_logits = torch.tensor([start_logits], dtype=torch.float)
        self.end_logits = torch.tensor([end_logits], dtype=torch.float)


def _make_pipeline(offsets, sequence_ids, start_logits, end_logits):
    pipe = ExtractiveQAPipeline.__new__(ExtractiveQAPipeline)
    pipe.device = torch.device("cpu")
    pipe.tokenizer = lambda *a, **kw: _FakeEncoding(offsets, sequence_ids)
    pipe.model = lambda **kw: _FakeOutputs(start_logits, end_logits)
    return pipe


def test_extracts_answer_span_from_context_only():
    # tokens: [CLS] Who ? [SEP] Paris is the capital [SEP]
    #  seq:      0    0  0  0     1    1  1    1     1
    context = "Paris is the capital"
    offsets = [(0, 0), (0, 0), (0, 0), (0, 0), (0, 5), (6, 8), (9, 12), (13, 21), (0, 0)]
    sequence_ids = [None, 0, 0, None, 1, 1, 1, 1, None]
    # highest start/end logits both point at the "capital" token (index 7)
    start_logits = [0, 0, 0, 0, 0, 0, 0, 5, 0]
    end_logits = [0, 0, 0, 0, 0, 0, 0, 5, 0]

    pipe = _make_pipeline(offsets, sequence_ids, start_logits, end_logits)
    result = pipe("What is it?", context)

    assert result["answer"] == "capital"
    assert result["start"] == 13
    assert result["end"] == 21
    assert 0.0 < result["score"] <= 1.0


def test_never_picks_answer_from_question_tokens():
    # highest raw logit is on a question token (index 1), but it must be masked out
    context = "Paris is the capital"
    offsets = [(0, 0), (0, 0), (0, 0), (0, 0), (0, 5), (6, 8), (9, 12), (13, 21), (0, 0)]
    sequence_ids = [None, 0, 0, None, 1, 1, 1, 1, None]
    start_logits = [0, 100, 0, 0, 9, 0, 0, 0, 0]
    end_logits = [0, 100, 0, 0, 9, 0, 0, 0, 0]

    pipe = _make_pipeline(offsets, sequence_ids, start_logits, end_logits)
    result = pipe("Who?", context)

    assert result["answer"] == "Paris"
    assert result["start"] == 0
    assert result["end"] == 5
