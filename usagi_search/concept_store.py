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
