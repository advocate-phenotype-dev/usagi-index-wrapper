"""
SQLite-backed concept metadata store built from Athena's CONCEPT.csv.

Berkeley DB Java Edition (used by Usagi's sleepyCat/ folder) stores data in a
proprietary binary format that Python's bsddb3 (which wraps C libdb) cannot read.
The data is equivalent: BerkeleyDbBuilder.java populates it directly from the same
CONCEPT.csv that Athena distributes.  We replicate that by building a lightweight
SQLite cache from the same file.
"""
import csv
import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS concepts (
    concept_id      INTEGER PRIMARY KEY,
    concept_name    TEXT    NOT NULL,
    domain_id       TEXT,
    vocabulary_id   TEXT,
    concept_class_id TEXT,
    standard_concept TEXT,
    concept_code    TEXT,
    valid_start_date TEXT,
    valid_end_date   TEXT,
    invalid_reason   TEXT
);
CREATE INDEX IF NOT EXISTS idx_concepts_vocab ON concepts(vocabulary_id);
CREATE INDEX IF NOT EXISTS idx_concepts_domain ON concepts(domain_id);

-- Immediate parent-child relationships (min_levels_of_separation=1 from CONCEPT_ANCESTOR.csv).
-- Mirrors Usagi's ParentChildRelationShip BDB store.
CREATE TABLE IF NOT EXISTS concept_hierarchy (
    concept_id        INTEGER NOT NULL,
    parent_concept_id INTEGER NOT NULL,
    PRIMARY KEY (concept_id, parent_concept_id)
);
CREATE INDEX IF NOT EXISTS idx_hierarchy_parent ON concept_hierarchy(parent_concept_id);
"""


class ConceptStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def is_available(self) -> bool:
        if self._conn is None:
            return False
        try:
            cur = self._conn.execute("SELECT COUNT(*) FROM concepts")
            return cur.fetchone()[0] > 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Build cache from CONCEPT.csv
    # ------------------------------------------------------------------

    def build_from_csv(self, csv_path: str) -> int:
        """
        Parse Athena CONCEPT.csv (tab-separated) and populate the SQLite table.
        Mirrors BerkeleyDbBuilder.loadConcepts() filtering: only rows where
        invalid_reason is empty are stored (valid concepts only).
        Returns the number of rows inserted.
        """
        if not Path(csv_path).exists():
            raise FileNotFoundError(f"CONCEPT.csv not found: {csv_path}")

        self._conn.execute("DROP TABLE IF EXISTS concepts")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

        inserted = 0
        with open(csv_path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            batch = []
            for row in reader:
                if row.get("invalid_reason", "").strip():
                    continue  # skip invalid concepts (same filter as Usagi BDB)
                batch.append((
                    int(row["concept_id"]),
                    row["concept_name"],
                    row["domain_id"],
                    row["vocabulary_id"],
                    row["concept_class_id"],
                    row.get("standard_concept", ""),
                    row["concept_code"],
                    row.get("valid_start_date", ""),
                    row.get("valid_end_date", ""),
                    row.get("invalid_reason", ""),
                ))
                if len(batch) >= 10_000:
                    self._flush(batch)
                    inserted += len(batch)
                    batch = []
                    if inserted % 100_000 == 0:
                        logger.info(f"  {inserted:,} concepts loaded…")
            if batch:
                self._flush(batch)
                inserted += len(batch)

        self._conn.commit()
        logger.info(f"Concept cache built: {inserted:,} valid concepts")
        return inserted

    def _flush(self, batch: list) -> None:
        self._conn.executemany(
            "INSERT OR REPLACE INTO concepts VALUES (?,?,?,?,?,?,?,?,?,?)", batch
        )

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def get_concept_name(self, concept_id: int) -> Optional[str]:
        if self._conn is None:
            return None
        try:
            row = self._conn.execute(
                "SELECT concept_name FROM concepts WHERE concept_id = ?", (concept_id,)
            ).fetchone()
            return row["concept_name"] if row else None
        except Exception:
            return None

    def get_concept(self, concept_id: int) -> Optional[dict]:
        if self._conn is None:
            return None
        try:
            row = self._conn.execute(
                "SELECT * FROM concepts WHERE concept_id = ?", (concept_id,)
            ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Hierarchy
    # ------------------------------------------------------------------

    def has_hierarchy(self) -> bool:
        if self._conn is None:
            return False
        try:
            return self._conn.execute(
                "SELECT COUNT(*) FROM concept_hierarchy"
            ).fetchone()[0] > 0
        except Exception:
            return False

    def get_parents(self, concept_id: int) -> list:
        """Return immediate parent concepts (up to 10)."""
        if self._conn is None:
            return []
        try:
            rows = self._conn.execute(
                """
                SELECT c.concept_id, c.concept_name
                FROM concept_hierarchy h
                JOIN concepts c ON c.concept_id = h.parent_concept_id
                WHERE h.concept_id = ?
                LIMIT 10
                """,
                (concept_id,),
            ).fetchall()
            return [{"concept_id": r["concept_id"], "concept_name": r["concept_name"]}
                    for r in rows]
        except Exception:
            return []

    def get_breadcrumb(self, concept_id: int, concept_name: str) -> str:
        """
        Walk up the hierarchy from concept_id to the root and return a
        ' > '-separated path string, e.g.:
            'Procedure on bone marrow > Bone marrow sampling'
        (The concept itself is not included — the caller prepends it.)
        Always picks the first parent at each level; OMOP is a DAG so paths
        are not unique, but one representative path is enough for display.
        """
        if self._conn is None:
            return concept_name
        path = [concept_name]
        current_id = concept_id
        for _ in range(8):
            row = self._conn.execute(
                """SELECT c.concept_id, c.concept_name
                   FROM concept_hierarchy h
                   JOIN concepts c ON c.concept_id = h.parent_concept_id
                   WHERE h.concept_id = ?
                   ORDER BY c.concept_id
                   LIMIT 1""",
                (current_id,),
            ).fetchone()
            if row is None:
                break
            path.append(row["concept_name"])
            current_id = row["concept_id"]
        path.reverse()
        return " > ".join(path)

    def get_hierarchy_counts(self, concept_id: int) -> tuple:
        """Return (parent_count, child_count) for a concept."""
        if self._conn is None:
            return 0, 0
        try:
            pc = self._conn.execute(
                "SELECT COUNT(*) FROM concept_hierarchy WHERE concept_id = ?",
                (concept_id,),
            ).fetchone()[0]
            cc = self._conn.execute(
                "SELECT COUNT(*) FROM concept_hierarchy WHERE parent_concept_id = ?",
                (concept_id,),
            ).fetchone()[0]
            return pc, cc
        except Exception:
            return 0, 0

    def build_hierarchy(self, ancestor_csv: str, valid_concept_ids: set) -> int:
        """
        Populate concept_hierarchy from CONCEPT_ANCESTOR.csv.
        Only stores rows with min_levels_of_separation=1 (immediate parent-child),
        mirroring BerkeleyDbBuilder.loadAncestors().
        """
        if not Path(ancestor_csv).exists():
            raise FileNotFoundError(f"CONCEPT_ANCESTOR.csv not found: {ancestor_csv}")

        self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS concept_hierarchy ("
            "  concept_id INTEGER NOT NULL,"
            "  parent_concept_id INTEGER NOT NULL,"
            "  PRIMARY KEY (concept_id, parent_concept_id)"
            ");"
            "CREATE INDEX IF NOT EXISTS idx_hierarchy_parent "
            "  ON concept_hierarchy(parent_concept_id);"
        )

        count = 0
        batch = []
        with open(ancestor_csv, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            for row in reader:
                if row.get("min_levels_of_separation", "") != "1":
                    continue
                anc = int(row["ancestor_concept_id"])
                desc = int(row["descendant_concept_id"])
                if anc == desc:
                    continue
                if anc not in valid_concept_ids or desc not in valid_concept_ids:
                    continue
                batch.append((desc, anc))  # child → parent
                if len(batch) >= 10_000:
                    self._conn.executemany(
                        "INSERT OR IGNORE INTO concept_hierarchy VALUES (?,?)", batch
                    )
                    self._conn.commit()
                    count += len(batch)
                    batch = []
                    if count % 100_000 == 0:
                        logger.info("  %d hierarchy rows loaded…", count)
        if batch:
            self._conn.executemany(
                "INSERT OR IGNORE INTO concept_hierarchy VALUES (?,?)", batch
            )
            self._conn.commit()
            count += len(batch)

        logger.info("Hierarchy built: %d parent-child relationships.", count)
        return count
