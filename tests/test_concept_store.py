"""
Tests for ConceptStore — concept metadata lookups and hierarchy queries.
"""
import sqlite3
import tempfile
import os
import pytest

from usagi_search.concept_store import ConceptStore


@pytest.fixture
def store_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE concepts (
            concept_id INTEGER PRIMARY KEY,
            concept_name TEXT NOT NULL,
            domain_id TEXT, vocabulary_id TEXT, concept_class_id TEXT,
            standard_concept TEXT, concept_code TEXT,
            valid_start_date TEXT, valid_end_date TEXT, invalid_reason TEXT
        );
        CREATE TABLE concept_hierarchy (
            concept_id INTEGER NOT NULL,
            parent_concept_id INTEGER NOT NULL,
            PRIMARY KEY (concept_id, parent_concept_id)
        );
        CREATE INDEX idx_hierarchy_parent ON concept_hierarchy(parent_concept_id);
    """)
    conn.executemany(
        "INSERT INTO concepts VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (100, "Root",           "Procedure", "SNOMED", "Procedure", "S", "R", "", "", ""),
            (200, "Parent",         "Procedure", "SNOMED", "Procedure", "S", "P", "", "", ""),
            (300, "Child",          "Procedure", "SNOMED", "Procedure", "S", "C", "", "", ""),
            (400, "Grandchild",     "Procedure", "SNOMED", "Procedure", "S", "G", "", "", ""),
            (500, "Sibling",        "Procedure", "SNOMED", "Procedure", "S", "S", "", "", ""),
        ],
    )
    conn.executemany(
        "INSERT INTO concept_hierarchy VALUES (?,?)",
        [
            (200, 100),  # Parent → Root
            (300, 200),  # Child → Parent
            (400, 300),  # Grandchild → Child
            (500, 200),  # Sibling → Parent
        ],
    )
    conn.commit()
    conn.close()

    store = ConceptStore(db_path)
    store.open()
    yield store
    store.close()
    os.unlink(db_path)


# ---------------------------------------------------------------------------
# Basic lookups
# ---------------------------------------------------------------------------

def test_get_concept_name(store_db):
    assert store_db.get_concept_name(100) == "Root"
    assert store_db.get_concept_name(300) == "Child"


def test_get_concept_name_missing(store_db):
    assert store_db.get_concept_name(9999) is None


def test_is_available(store_db):
    assert store_db.is_available()


# ---------------------------------------------------------------------------
# Hierarchy counts
# ---------------------------------------------------------------------------

def test_parent_count_root_has_none(store_db):
    pc, cc = store_db.get_hierarchy_counts(100)
    assert pc == 0


def test_child_count_root(store_db):
    pc, cc = store_db.get_hierarchy_counts(100)
    assert cc == 1  # only Parent is a direct child of Root


def test_parent_count_child(store_db):
    pc, cc = store_db.get_hierarchy_counts(300)
    assert pc == 1


def test_child_count_parent(store_db):
    pc, cc = store_db.get_hierarchy_counts(200)
    assert cc == 2  # Child and Sibling


def test_leaf_has_no_children(store_db):
    pc, cc = store_db.get_hierarchy_counts(400)
    assert cc == 0


# ---------------------------------------------------------------------------
# Parents list
# ---------------------------------------------------------------------------

def test_get_parents_returns_immediate_parents(store_db):
    parents = store_db.get_parents(300)
    assert len(parents) == 1
    assert parents[0]["concept_id"] == 200
    assert parents[0]["concept_name"] == "Parent"


def test_get_parents_root_returns_empty(store_db):
    assert store_db.get_parents(100) == []


# ---------------------------------------------------------------------------
# Breadcrumb
# ---------------------------------------------------------------------------

def test_breadcrumb_leaf(store_db):
    bc = store_db.get_breadcrumb(400, "Grandchild")
    assert bc == "Root > Parent > Child > Grandchild"


def test_breadcrumb_root(store_db):
    bc = store_db.get_breadcrumb(100, "Root")
    assert bc == "Root"


def test_breadcrumb_one_level(store_db):
    bc = store_db.get_breadcrumb(200, "Parent")
    assert bc == "Root > Parent"


def test_breadcrumb_sibling(store_db):
    bc = store_db.get_breadcrumb(500, "Sibling")
    assert bc == "Root > Parent > Sibling"
