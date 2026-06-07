#!/usr/bin/env python3
"""
Build the production SQLite search index from Athena vocabulary files.

This replaces Usagi's full index-building pipeline (which can take 8+ hours)
with a pure-Python process that typically completes in 10-30 minutes depending
on the vocabulary size and disk speed.

Required Athena files (download from athena.ohdsi.org):
    CONCEPT.csv           — all concepts
    CONCEPT_SYNONYM.csv   — concept synonyms  (optional but recommended)

Usage:
    python scripts/build_native_index.py \\
        --concept-csv      /data/athena/CONCEPT.csv \\
        --synonym-csv      /data/athena/CONCEPT_SYNONYM.csv \\
        --db-path          /data/usagi/search.db

What it indexes (mirrors LuceneIndexBuilder.java):
    • Standard concepts (standard_concept = 'S' or 'C') → TERM_TYPE 'C'
    • Their synonyms → TERM_TYPE 'C'
    • Non-standard concept names that have a 'Maps to' relationship to a
      standard concept → TERM_TYPE 'S' (for include_source_concepts queries)
"""
import argparse
import csv
import logging
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from usagi_search.engine_native import NativeIndexBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s  %(message)s")
log = logging.getLogger(__name__)


def load_concepts(concept_csv: str) -> tuple[dict, dict]:
    """Return (valid_concepts, maps_to) dicts."""
    valid_concepts: dict[int, dict] = {}
    log.info("Loading CONCEPT.csv…")
    with open(concept_csv, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            if row.get("invalid_reason", "").strip():
                continue  # skip invalid concepts
            cid = int(row["concept_id"])
            valid_concepts[cid] = {
                "name":             row["concept_name"],
                "domain_id":        row["domain_id"],
                "vocabulary_id":    row["vocabulary_id"],
                "concept_class_id": row["concept_class_id"],
                "standard_concept": row.get("standard_concept", ""),
                "concept_code":     row["concept_code"],
                "valid_start_date": row.get("valid_start_date", ""),
                "valid_end_date":   row.get("valid_end_date", ""),
                "invalid_reason":   row.get("invalid_reason", ""),
            }
    log.info("  %d valid concepts loaded.", len(valid_concepts))
    return valid_concepts


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--concept-csv",   required=True)
    p.add_argument("--synonym-csv",   default=None)
    p.add_argument("--ancestor-csv",  default=None,
                   help="Path to CONCEPT_ANCESTOR.csv for parent-child hierarchy")
    p.add_argument("--db-path",       required=True)
    args = p.parse_args()

    t0 = time.time()
    concepts = load_concepts(args.concept_csv)

    builder = NativeIndexBuilder(args.db_path)
    builder.open()

    # 1 ── Concept names
    log.info("Indexing concept names…")
    count = 0
    for cid, c in concepts.items():
        std = c["standard_concept"]
        if std in ("S", "C"):
            builder.add_term(
                c["name"], cid,
                c["domain_id"], c["vocabulary_id"],
                c["concept_class_id"], std, "C",
            )
            count += 1
            if count % 100_000 == 0:
                builder.commit()
                log.info("  %d concept names indexed…", count)
    builder.commit()
    log.info("  %d concept names indexed.", count)

    # 2 ── Synonyms
    if args.synonym_csv and Path(args.synonym_csv).exists():
        log.info("Indexing synonyms from %s…", args.synonym_csv)
        count = 0
        with open(args.synonym_csv, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            for row in reader:
                cid = int(row["concept_id"])
                c = concepts.get(cid)
                if c is None:
                    continue
                std = c["standard_concept"]
                if std not in ("S", "C"):
                    continue
                raw = row.get("concept_synonym_name", "").strip()
                # LOINC stores multiple synonyms as one semicolon-separated
                # string per row.  Split so each token is indexed separately,
                # preventing unrelated n-gram overlap across the full string.
                parts = [p.strip() for p in raw.split(";") if p.strip()] if raw else []
                for syn in parts:
                    if syn.lower() == c["name"].lower():
                        continue
                    builder.add_term(
                        syn, cid,
                        c["domain_id"], c["vocabulary_id"],
                        c["concept_class_id"], std, "C",
                    )
                    count += 1
                    if count % 100_000 == 0:
                        builder.commit()
                        log.info("  %d synonyms indexed…", count)
        builder.commit()
        log.info("  %d synonyms indexed.", count)

    builder.close()

    # 3 ── Concept metadata table (for concept_name lookups)
    log.info("Writing concept metadata…")
    conn = sqlite3.connect(args.db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS concepts (
            concept_id       INTEGER PRIMARY KEY,
            concept_name     TEXT NOT NULL,
            domain_id        TEXT,
            vocabulary_id    TEXT,
            concept_class_id TEXT,
            standard_concept TEXT,
            concept_code     TEXT,
            valid_start_date TEXT,
            valid_end_date   TEXT,
            invalid_reason   TEXT
        )
    """)
    batch = [
        (cid, c["name"], c["domain_id"], c["vocabulary_id"],
         c["concept_class_id"], c["standard_concept"], c["concept_code"],
         c["valid_start_date"], c["valid_end_date"], c["invalid_reason"])
        for cid, c in concepts.items()
    ]
    conn.executemany("INSERT OR REPLACE INTO concepts VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
    conn.commit()
    conn.close()

    # 4 ── Hierarchy (optional)
    if args.ancestor_csv:
        from usagi_search.concept_store import ConceptStore
        log.info("Loading parent-child hierarchy from %s…", args.ancestor_csv)
        store = ConceptStore(args.db_path)
        store.open()
        valid_ids = set(concepts.keys())
        n = store.build_hierarchy(args.ancestor_csv, valid_ids)
        store.close()
        log.info("  %d relationships written.", n)
    else:
        log.info("Skipping hierarchy (no --ancestor-csv provided).")

    elapsed = time.time() - t0
    log.info("Done in %.1f s.  Index at: %s", elapsed, args.db_path)


if __name__ == "__main__":
    main()
