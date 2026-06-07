"""
Integration tests for NativeSearchEngine using an in-memory SQLite index
built from a small set of known OMOP-style concepts.

These tests verify search logic, filters, deduplication, and scoring
without requiring the full 6 GB production index.
"""
import math
import sqlite3
import tempfile
import os
import pytest

from usagi_search.engine_native import NativeIndexBuilder, NativeSearchEngine


# ---------------------------------------------------------------------------
# Fixture: small in-memory index
# ---------------------------------------------------------------------------

CONCEPTS = [
    # (concept_id, term, domain_id, vocabulary_id, concept_class_id, standard_concept, term_type)
    (4329847, "Myocardial infarction",       "Condition", "SNOMED", "Disorder", "S", "C"),
    (4329847, "Heart attack",                "Condition", "SNOMED", "Disorder", "S", "C"),
    (312327,  "Acute myocardial infarction", "Condition", "SNOMED", "Disorder", "S", "C"),
    (201826,  "Type 2 diabetes mellitus",    "Condition", "SNOMED", "Disorder", "S", "C"),
    (201826,  "T2DM",                        "Condition", "SNOMED", "Disorder", "S", "C"),
    (316866,  "Hypertension",                "Condition", "SNOMED", "Disorder", "S", "C"),
    (1310149, "Metformin",                   "Drug",      "RxNorm", "Ingredient", "S", "C"),
    (1310149, "Glucophage",                  "Drug",      "RxNorm", "Ingredient", "S", "C"),
    (3004249, "Systolic blood pressure",     "Measurement", "LOINC", "Clinical Observation", "S", "C"),
    (99999,   "Non-standard concept",        "Condition", "SNOMED", "Disorder", "",  "C"),
    (88888,   "Source term only",            "Condition", "SNOMED", "Disorder", "S", "S"),
]


@pytest.fixture(scope="module")
def index_db():
    """Build a temp SQLite index and yield its path."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    builder = NativeIndexBuilder(db_path)
    builder.open()
    for cid, term, domain, vocab, cls, std, ttype in CONCEPTS:
        builder.add_term(term, cid, domain, vocab, cls, std, ttype)
    builder.commit()
    builder.close()

    # Add concept metadata table (normally written by build_native_index.py)
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS concepts (
        concept_id INTEGER PRIMARY KEY, concept_name TEXT NOT NULL,
        domain_id TEXT, vocabulary_id TEXT, concept_class_id TEXT,
        standard_concept TEXT, concept_code TEXT,
        valid_start_date TEXT, valid_end_date TEXT, invalid_reason TEXT
    )""")
    unique = {cid: (term, domain, vocab, cls, std)
              for cid, term, domain, vocab, cls, std, _ in CONCEPTS}
    conn.executemany(
        "INSERT OR REPLACE INTO concepts VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(cid, t, d, v, c, s, "", "", "", "") for cid, (t, d, v, c, s) in unique.items()]
    )
    conn.commit()
    conn.close()

    yield db_path
    os.unlink(db_path)


@pytest.fixture(scope="module")
def engine(index_db):
    eng = NativeSearchEngine(index_db)
    eng.open()
    yield eng
    eng.close()


# ---------------------------------------------------------------------------
# Basic search
# ---------------------------------------------------------------------------

def test_exact_match_scores_one(engine):
    results = engine.search("Myocardial infarction")
    assert results, "expected at least one result"
    top = results[0]
    assert top["concept_id"] == 4329847
    assert top["similarity_score"] == pytest.approx(1.0)


def test_synonym_finds_correct_concept(engine):
    results = engine.search("Heart attack")
    assert results[0]["concept_id"] == 4329847


def test_partial_match_returns_results(engine):
    results = engine.search("diabetes")
    ids = [r["concept_id"] for r in results]
    assert 201826 in ids


def test_no_match_returns_empty(engine):
    results = engine.search("xyzzy irrelevant zzz")
    assert results == []


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def test_deduplication_returns_each_concept_once(engine):
    # concept 4329847 has two indexed terms; should appear only once
    results = engine.search("Myocardial infarction")
    ids = [r["concept_id"] for r in results]
    assert ids.count(4329847) == 1


def test_deduplication_keeps_best_score(engine):
    results = engine.search("Myocardial infarction")
    mi = next(r for r in results if r["concept_id"] == 4329847)
    # Best score should be 1.0 (exact match), not the lower synonym score
    assert mi["similarity_score"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def test_domain_filter(engine):
    results = engine.search("Metformin", domain_filter=["Drug"])
    assert all(r["domain_id"] == "Drug" for r in results)
    ids = [r["concept_id"] for r in results]
    assert 1310149 in ids


def test_domain_filter_excludes_other_domains(engine):
    results = engine.search("blood pressure", domain_filter=["Drug"])
    ids = [r["concept_id"] for r in results]
    assert 3004249 not in ids


def test_vocabulary_filter(engine):
    results = engine.search("Metformin", vocabulary_filter=["RxNorm"])
    assert all(r["vocabulary_id"] == "RxNorm" for r in results)


def test_standard_only_excludes_non_standard(engine):
    results = engine.search("Non-standard concept", standard_only=True)
    ids = [r["concept_id"] for r in results]
    assert 99999 not in ids


def test_standard_only_false_includes_non_standard(engine):
    results = engine.search("Non-standard concept", standard_only=False)
    ids = [r["concept_id"] for r in results]
    assert 99999 in ids


def test_include_source_concepts_false_excludes_source_terms(engine):
    results = engine.search("Source term only", include_source_concepts=False)
    ids = [r["concept_id"] for r in results]
    assert 88888 not in ids


def test_include_source_concepts_true_includes_source_terms(engine):
    results = engine.search("Source term only", include_source_concepts=True)
    ids = [r["concept_id"] for r in results]
    assert 88888 in ids


def test_top_n_limits_results(engine):
    results = engine.search("infarction", top_n=1)
    assert len(results) <= 1


# ---------------------------------------------------------------------------
# Scoring properties
# ---------------------------------------------------------------------------

def test_results_sorted_descending(engine):
    results = engine.search("diabetes")
    scores = [r["similarity_score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_scores_between_zero_and_one(engine):
    results = engine.search("Myocardial infarction")
    for r in results:
        assert 0.0 < r["similarity_score"] <= 1.0


def test_exact_match_beats_partial(engine):
    results = engine.search("Myocardial infarction")
    ids = [r["concept_id"] for r in results]
    if 312327 in ids:  # "Acute myocardial infarction" — partial match
        exact_score = next(r["similarity_score"] for r in results if r["concept_id"] == 4329847)
        partial_score = next(r["similarity_score"] for r in results if r["concept_id"] == 312327)
        assert exact_score > partial_score
