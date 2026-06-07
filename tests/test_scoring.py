"""
Tests for TF-IDF cosine scoring.

Usagi recomputeScores() uses IDF-only weights (no TF) and standard cosine
similarity.  These tests verify the Python port is correct.
"""
import math
import pytest
from usagi_search.engine_native import _cosine


def test_identical_vectors_score_one():
    v = {"ab": 1.5, "bc": 2.0, "abc": 0.8}
    assert _cosine(v, v) == pytest.approx(1.0)


def test_orthogonal_vectors_score_zero():
    v1 = {"ab": 1.0}
    v2 = {"xy": 1.0}
    assert _cosine(v1, v2) == pytest.approx(0.0)


def test_partial_overlap():
    v1 = {"ab": 1.0, "bc": 1.0}
    v2 = {"ab": 1.0, "cd": 1.0}
    score = _cosine(v1, v2)
    assert 0.0 < score < 1.0
    # dot = 1*1 = 1; |v1| = |v2| = sqrt(2); cosine = 1/2 = 0.5
    assert score == pytest.approx(0.5)


def test_empty_vector_returns_zero():
    assert _cosine({}, {"ab": 1.0}) == pytest.approx(0.0)
    assert _cosine({"ab": 1.0}, {}) == pytest.approx(0.0)
    assert _cosine({}, {}) == pytest.approx(0.0)


def test_scaling_does_not_change_score():
    v1 = {"ab": 1.0, "bc": 2.0}
    v2 = {"ab": 2.0, "bc": 4.0}  # v2 = 2 * v1
    assert _cosine(v1, v2) == pytest.approx(1.0)


def test_symmetry():
    v1 = {"ab": 1.0, "bc": 2.0, "cd": 0.5}
    v2 = {"ab": 0.5, "bc": 1.0, "xy": 3.0}
    assert _cosine(v1, v2) == pytest.approx(_cosine(v2, v1))


def test_score_bounded_zero_to_one():
    v1 = {"ab": 1.0, "bc": 2.0, "cd": 3.0}
    v2 = {"ab": 0.5, "de": 1.0, "fg": 2.0}
    score = _cosine(v1, v2)
    assert 0.0 <= score <= 1.0
