"""
Pure-Python search engine — no Java, no PyLucene required.

Replicates UsagiSearchEngine + UsagiAnalyzer exactly using SQLite as the
backing store:

  Index layout
  ────────────
  docs        — one row per indexed term (concept name or synonym)
  ngram_docs  — inverted index: (ngram, doc_id)  for candidate retrieval
  ngram_df    — document frequency per n-gram     for IDF computation
  meta        — num_docs and other global stats

  Scoring
  ───────
  Same as Java recomputeScores():
    weight(t) = ln(N / df_t)   [IDF only — TF intentionally ignored]
    score     = cosine(query_vec, doc_vec)
"""
import logging
import math
import sqlite3
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# N-gram tokenizer (replicates UsagiAnalyzer: NGramTokenizer(2,3) + lowercase)
# ---------------------------------------------------------------------------

def ngrams(text: str, min_n: int = 2, max_n: int = 3) -> Set[str]:
    """
    All contiguous substrings of length min_n..max_n from lowercased text.

    UsagiAnalyzer.createComponents() chains:
        NGramTokenizer(2, 3)  →  StandardFilter (no-op on ASCII)  →  LowerCaseFilter
    """
    t = text.lower()
    result: Set[str] = set()
    for n in range(min_n, max_n + 1):
        for i in range(len(t) - n + 1):
            result.add(t[i : i + n])
    return result


# ---------------------------------------------------------------------------
# Search engine
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    doc_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id       INTEGER NOT NULL,
    term             TEXT    NOT NULL,
    domain_id        TEXT    DEFAULT '',
    vocabulary_id    TEXT    DEFAULT '',
    concept_class_id TEXT    DEFAULT '',
    standard_concept TEXT    DEFAULT '',
    term_type        TEXT    DEFAULT 'C'
);
CREATE TABLE IF NOT EXISTS ngram_docs (
    ngram  TEXT    NOT NULL,
    doc_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ngram_df (
    ngram    TEXT PRIMARY KEY,
    doc_freq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Applied by NativeSearchEngine.open() after the index is fully built.
_READ_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_ngram_docs_ngram ON ngram_docs(ngram);
"""


class NativeSearchEngine:
    """
    SQLite-backed search engine with identical tokenisation and scoring to Usagi.
    """

    CONCEPT_TERM = "C"
    SOURCE_TERM  = "S"

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self.num_docs: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA query_only=ON")
        # Ensure the search index exists (idempotent on already-built DBs).
        self._conn.executescript(_READ_INDEXES)
        try:
            self.num_docs = int(
                self._conn.execute(
                    "SELECT value FROM meta WHERE key='num_docs'"
                ).fetchone()["value"]
            )
        except Exception:
            self.num_docs = self._conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        logger.info("Opened native index at %s  (%d docs)", self.db_path, self.num_docs)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Public search
    # ------------------------------------------------------------------

    def search(
        self,
        search_term: str,
        domain_filter: Optional[List[str]] = None,
        vocabulary_filter: Optional[List[str]] = None,
        concept_class_filter: Optional[List[str]] = None,
        standard_only: bool = False,
        include_source_concepts: bool = False,
        top_n: int = 10,
        # use_mlt is accepted but ignored — MLT/keyword distinction collapses
        # to the same n-gram query when using a pre-built inverted index.
        use_mlt: bool = True,
    ) -> List[Dict[str, Any]]:
        if not self._conn:
            return []

        query_grams = ngrams(search_term)
        if not query_grams:
            return []

        # --- IDF weights for query n-grams ------------------------------
        query_vec = self._idf_vector(query_grams)
        if not query_vec:
            return []

        # --- candidate retrieval: single JOIN avoids large IN (doc_ids) -
        # Passing only query_grams (≤ ~50 values for typical terms) stays
        # well within SQLite's variable limit.  We rank candidates by n-gram
        # hit count and cap at 500 before scoring, which also limits the
        # amount of cosine computation on very common n-grams.
        grams_ph = ",".join("?" * len(query_grams))
        params: List[Any] = list(query_grams)

        where_clauses = [f"nd.ngram IN ({grams_ph})"]
        if not include_source_concepts:
            where_clauses.append("d.term_type = 'C'")
        if standard_only:
            where_clauses.append("d.standard_concept = 'S'")
        if domain_filter:
            dp = ",".join("?" * len(domain_filter))
            where_clauses.append(f"d.domain_id IN ({dp})")
            params.extend(domain_filter)
        if vocabulary_filter:
            vp = ",".join("?" * len(vocabulary_filter))
            where_clauses.append(f"d.vocabulary_id IN ({vp})")
            params.extend(vocabulary_filter)
        if concept_class_filter:
            cp = ",".join("?" * len(concept_class_filter))
            where_clauses.append(f"d.concept_class_id IN ({cp})")
            params.extend(concept_class_filter)

        where_sql = " AND ".join(where_clauses)
        sql = f"""
            SELECT d.doc_id, d.concept_id, d.term, d.domain_id, d.vocabulary_id,
                   d.concept_class_id, d.standard_concept, d.term_type,
                   COUNT(*) AS ngram_hits
            FROM ngram_docs nd
            JOIN docs d ON d.doc_id = nd.doc_id
            WHERE {where_sql}
            GROUP BY d.doc_id
            ORDER BY ngram_hits DESC
            LIMIT 500
        """
        docs = self._conn.execute(sql, params).fetchall()

        if not docs:
            return []

        # --- score, deduplicate, sort ------------------------------------
        scored: List[Dict[str, Any]] = []
        for doc in docs:
            doc_grams = ngrams(doc["term"])
            doc_vec = self._idf_vector(doc_grams)
            score = _cosine(query_vec, doc_vec)
            if score <= 0:
                continue
            scored.append(
                {
                    "concept_id": doc["concept_id"],
                    "vocabulary_id": doc["vocabulary_id"],
                    "domain_id": doc["domain_id"],
                    "concept_class_id": doc["concept_class_id"],
                    "standard_concept": doc["standard_concept"],
                    "match_term": doc["term"],
                    "similarity_score": round(score, 6),
                }
            )

        scored.sort(key=lambda r: r["similarity_score"], reverse=True)

        # deduplicate concept_ids, keep best score
        seen: Dict[int, bool] = {}
        deduped: List[Dict[str, Any]] = []
        for r in scored:
            cid = r["concept_id"]
            if cid not in seen:
                seen[cid] = True
                deduped.append(r)

        return deduped[:top_n]

    # ------------------------------------------------------------------
    # IDF helpers
    # ------------------------------------------------------------------

    def _idf_vector(self, grams: Set[str]) -> Dict[str, float]:
        """Build IDF-weighted vector for a set of n-grams."""
        if not grams or self.num_docs == 0:
            return {}
        placeholders = ",".join("?" * len(grams))
        rows = self._conn.execute(
            f"SELECT ngram, doc_freq FROM ngram_df WHERE ngram IN ({placeholders})",
            list(grams),
        ).fetchall()
        result: Dict[str, float] = {}
        for row in rows:
            df = row["doc_freq"]
            if df > 0:
                result[row["ngram"]] = math.log(self.num_docs / df)
        return result


# ---------------------------------------------------------------------------
# Index builder (used by build scripts)
# ---------------------------------------------------------------------------

class NativeIndexBuilder:
    """Builds the SQLite n-gram index from concept records."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        self._conn = sqlite3.connect(self.db_path)
        self._conn.executescript(_SCHEMA)
        # Bulk-load pragmas: no fsync, memory journal, large cache.
        # Safe to use here because the builder always runs to completion
        # before the DB is read by the service.
        self._conn.execute("PRAGMA synchronous=OFF")
        self._conn.execute("PRAGMA journal_mode=MEMORY")
        self._conn.execute("PRAGMA cache_size=-524288")  # 512 MB

    def close(self) -> None:
        if self._conn:
            self._finalise()
            self._conn.close()
            self._conn = None

    def add_term(
        self,
        term: str,
        concept_id: int,
        domain_id: str = "",
        vocabulary_id: str = "",
        concept_class_id: str = "",
        standard_concept: str = "",
        term_type: str = "C",
    ) -> int:
        """Insert one document row and return its doc_id."""
        cur = self._conn.execute(
            "INSERT INTO docs (concept_id, term, domain_id, vocabulary_id, "
            "concept_class_id, standard_concept, term_type) VALUES (?,?,?,?,?,?,?)",
            (concept_id, term, domain_id, vocabulary_id,
             concept_class_id, standard_concept, term_type),
        )
        doc_id = cur.lastrowid
        grams = ngrams(term)
        # Plain INSERT — no duplicates possible because ngrams() returns a Set,
        # so each (ngram, doc_id) pair is unique within a single add_term call.
        self._conn.executemany(
            "INSERT INTO ngram_docs (ngram, doc_id) VALUES (?,?)",
            [(g, doc_id) for g in grams],
        )
        return doc_id

    def commit(self) -> None:
        self._conn.commit()

    def _finalise(self) -> None:
        """Build the ngram search index, compute ngram_df and num_docs."""
        logger.info("Building ngram_docs index…")
        # Create the index now, after all rows are inserted, so SQLite builds
        # the B-tree in one pass rather than maintaining it on every insert.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ngram_docs_ngram ON ngram_docs(ngram)"
        )
        logger.info("Computing n-gram document frequencies…")
        self._conn.execute("DELETE FROM ngram_df")
        self._conn.execute(
            "INSERT INTO ngram_df (ngram, doc_freq) "
            "SELECT ngram, COUNT(DISTINCT doc_id) FROM ngram_docs GROUP BY ngram"
        )
        num_docs = self._conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('num_docs', ?)",
            (str(num_docs),),
        )
        self._conn.commit()
        logger.info("Index finalised: %d docs, ngram_df computed.", num_docs)


# ---------------------------------------------------------------------------
# Scoring (module-level — shared with engine)
# ---------------------------------------------------------------------------

def _cosine(v1: Dict[str, float], v2: Dict[str, float]) -> float:
    dot = sum(v1[t] * v2[t] for t in v1 if t in v2)
    n1 = math.sqrt(sum(x * x for x in v1.values()))
    n2 = math.sqrt(sum(x * x for x in v2.values()))
    if n1 == 0 or n2 == 0:
        return 0.0
    return dot / (n1 * n2)
