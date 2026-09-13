"""`_word_overlap_f1`'s SQuAD-style normalization: trailing punctuation or
articles must not zero out an otherwise-correct answer."""
from __future__ import annotations

from app.inference import _word_overlap_f1


def test_trailing_punctuation_does_not_zero_the_score():
    # "Paris," (comma attached) vs reference "Paris" is a partial, not a
    # zero, match: predicted includes an extra correct token ("France").
    assert _word_overlap_f1("Paris, France", "Paris") > 0.0


def test_exact_match_after_normalization_is_perfect():
    assert _word_overlap_f1("The Paris.", "paris") == 1.0


def test_no_overlap_is_still_zero():
    assert _word_overlap_f1("London", "Paris") == 0.0
