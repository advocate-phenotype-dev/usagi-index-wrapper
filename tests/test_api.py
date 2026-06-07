"""
Tests for FastAPI endpoints using TestClient with a mocked engine and store.
"""
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Minimal stubs so the app can be imported without a real DB
# ---------------------------------------------------------------------------

MOCK_HIT = {
    "concept_id": 4329847,
    "vocabulary_id": "SNOMED",
    "domain_id": "Condition",
    "concept_class_id": "Disorder",
    "standard_concept": "S",
    "match_term": "Myocardial infarction",
    "similarity_score": 1.0,
}


@pytest.fixture
def client():
    mock_engine = MagicMock()
    mock_engine.num_docs = 1000
    mock_engine.reader = True
    mock_engine.search.return_value = [MOCK_HIT]

    mock_store = MagicMock()
    mock_store.is_available.return_value = True
    mock_store.has_hierarchy.return_value = False
    mock_store.get_concept_name.return_value = "Myocardial infarction"
    mock_store.get_hierarchy_counts.return_value = (0, 0)
    mock_store.get_parents.return_value = []
    mock_store.get_breadcrumb.return_value = "Myocardial infarction"

    # Patch _load_engine so the lifespan never touches the filesystem,
    # and patch ConceptStore so the store is never opened.
    with patch("usagi_search.api._load_engine", return_value=mock_engine), \
         patch("usagi_search.api.ConceptStore", return_value=mock_store):
        from usagi_search.api import app
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c, mock_engine, mock_store


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

def test_health_ok(client):
    c, _, _ = client
    resp = c.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["index_docs"] == 1000
    assert data["concept_db_available"] is True


# ---------------------------------------------------------------------------
# /search — happy path
# ---------------------------------------------------------------------------

def test_search_returns_results(client):
    c, engine, _ = client
    resp = c.post("/search", json={"term": "myocardial infarction"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["term"] == "myocardial infarction"
    assert len(data["results"]) == 1
    assert data["results"][0]["concept_id"] == 4329847
    assert data["results"][0]["similarity_score"] == 1.0


def test_search_calls_engine_with_correct_args(client):
    c, engine, _ = client
    c.post("/search", json={
        "term": "diabetes",
        "domain_filter": ["Condition"],
        "standard_only": True,
        "top_n": 3,
    })
    engine.search.assert_called_once()
    call_kwargs = engine.search.call_args.kwargs
    assert call_kwargs["search_term"] == "diabetes"
    assert call_kwargs["domain_filter"] == ["Condition"]
    assert call_kwargs["standard_only"] is True
    assert call_kwargs["top_n"] == 3


def test_search_concept_name_enriched(client):
    c, _, store = client
    store.get_concept_name.return_value = "Myocardial infarction"
    resp = c.post("/search", json={"term": "MI"})
    assert resp.json()["results"][0]["concept_name"] == "Myocardial infarction"


def test_search_empty_results(client):
    c, engine, _ = client
    engine.search.return_value = []
    resp = c.post("/search", json={"term": "xyzzy"})
    assert resp.status_code == 200
    assert resp.json()["results"] == []
    assert resp.json()["total_candidates"] == 0


# ---------------------------------------------------------------------------
# /search — validation
# ---------------------------------------------------------------------------

def test_search_missing_term_is_422(client):
    c, _, _ = client
    resp = c.post("/search", json={"top_n": 5})
    assert resp.status_code == 422


def test_search_top_n_below_minimum_is_422(client):
    c, _, _ = client
    resp = c.post("/search", json={"term": "test", "top_n": 0})
    assert resp.status_code == 422


def test_search_top_n_above_maximum_is_422(client):
    c, _, _ = client
    resp = c.post("/search", json={"term": "test", "top_n": 201})
    assert resp.status_code == 422


def test_search_top_n_boundary_values_accepted(client):
    c, _, _ = client
    assert c.post("/search", json={"term": "t", "top_n": 1}).status_code == 200
    assert c.post("/search", json={"term": "t", "top_n": 200}).status_code == 200
