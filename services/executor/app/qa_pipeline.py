"""Extractive QA pipeline replacement.

transformers >= 5.3 ha rimosso "question-answering" dal registro di
`pipeline()`. Questo wrapper ricrea lo stesso comportamento (span extraction
da un contesto) usando direttamente model + tokenizer, così il resto del
codice che si aspetta un oggetto "pipeline-like" callable non deve cambiare.
"""
from __future__ import annotations

import torch
from transformers import AutoModelForQuestionAnswering, AutoTokenizer

class ExtractiveQAPipeline:
    def __init__(self, model_id: str, device: int | str | None = None):
        self.tokenizer = AutoTokenizer.from_pretrained(model_id)
        self.model = AutoModelForQuestionAnswering.from_pretrained(model_id)
        self.model.eval()

        # Normalizza il device come fa transformers.pipeline():
        # None/-1/"cpu" -> CPU, altrimenti sposta il modello sul device indicato.
        if device is not None and device != -1 and device != "cpu":
            self.device = torch.device(device if isinstance(device, str) else f"cuda:{device}")
            self.model.to(self.device)
        else:
            self.device = torch.device("cpu")

    def __call__(self, question: str, context: str) -> dict:
        encoding = self.tokenizer(
            question, context, return_tensors="pt", truncation=True, return_offsets_mapping=True
        )
        offsets = encoding.pop("offset_mapping")[0]
        # la risposta deve provenire dal contesto (sequence 1), mai dalla domanda (sequence 0)
        context_mask = torch.tensor([sid == 1 for sid in encoding.sequence_ids(0)])
        inputs = {k: v.to(self.device) for k, v in encoding.items()}
        with torch.no_grad():
            outputs = self.model(**inputs)

        start_logits = outputs.start_logits[0].masked_fill(~context_mask, float("-inf"))
        end_logits = outputs.end_logits[0].masked_fill(~context_mask, float("-inf"))
        start_probs = torch.softmax(start_logits, dim=-1)
        end_probs = torch.softmax(end_logits, dim=-1)

        start_idx = int(torch.argmax(start_probs))
        end_idx = start_idx + int(torch.argmax(end_probs[start_idx:]))
        start_char = int(offsets[start_idx][0])
        end_char = int(offsets[end_idx][1])

        return {
            "answer": context[start_char:end_char],
            "score": float(start_probs[start_idx] * end_probs[end_idx]),
            "start": start_char,
            "end": end_char,
        }


def load_qa_pipeline(model_id: str, device: int | str | None = None) -> ExtractiveQAPipeline:
    return ExtractiveQAPipeline(model_id, device=device)