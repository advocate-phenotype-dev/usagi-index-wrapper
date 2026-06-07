"""
Tests for the n-gram tokenizer — the foundation of all search fidelity.

UsagiAnalyzer uses NGramTokenizer(2, 3) + LowerCaseFilter.  Any change to
ngrams() that breaks these tests changes ranking for the entire vocabulary.
"""
import pytest
from usagi_search.engine_native import ngrams


def test_bigrams_and_trigrams_present():
    result = ngrams("abc")
    assert "ab" in result   # bigram
    assert "bc" in result   # bigram
    assert "abc" in result  # trigram


def test_lowercased():
    assert ngrams("ABC") == ngrams("abc")


def test_single_char_returns_empty():
    assert ngrams("a") == set()


def test_two_chars_returns_only_bigram():
    result = ngrams("ab")
    assert result == {"ab"}


def test_three_chars_returns_bigrams_and_trigram():
    assert ngrams("abc") == {"ab", "bc", "abc"}


def test_known_medical_term():
    result = ngrams("diabetes")
    # spot-check a few expected n-grams
    assert "di" in result
    assert "dia" in result
    assert "abe" in result
    assert "tes" in result
    # should NOT contain the full word
    assert "diabetes" not in result


def test_returns_set_no_duplicates():
    # "aaa" → bigrams: "aa", "aa" → should be deduplicated
    result = ngrams("aaa")
    assert result == {"aa", "aaa"}


def test_space_included():
    # spaces are part of the token stream in the Java NGramTokenizer
    result = ngrams("type 2")
    assert "e " in result
    assert " 2" in result
    assert "e 2" in result


def test_custom_min_max():
    result = ngrams("abcd", min_n=3, max_n=3)
    assert "ab" not in result   # bigram excluded
    assert "abc" in result
    assert "bcd" in result


def test_empty_string():
    assert ngrams("") == set()


def test_count_for_known_term():
    # "abc" → 2 bigrams + 1 trigram = 3
    assert len(ngrams("abc")) == 3
    # "abcd" → 3 bigrams + 2 trigrams = 5
    assert len(ngrams("abcd")) == 5
