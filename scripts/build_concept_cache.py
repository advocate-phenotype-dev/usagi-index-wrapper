#!/usr/bin/env python3
"""
Build (or rebuild) the SQLite concept-name cache from Athena's CONCEPT.csv.

Usage:
    python scripts/build_concept_cache.py \\
        --concept-csv /path/to/CONCEPT.csv \\
        --db-path     /path/to/usagi_data/concepts.db

The resulting SQLite file is the substitute for Usagi's Berkeley DB (sleepyCat/)
folder, which stores data in the Berkeley DB Java Edition binary format that
Python's bsddb3 cannot read.
"""
import argparse
import logging
import sqlite3
import sys
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from usagi_search.concept_store import ConceptStore

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--concept-csv", required=True, help="Path to Athena CONCEPT.csv")
    p.add_argument(
        "--db-path",
        required=True,
        help="Output SQLite path (e.g. /data/usagi/concepts.db)",
    )
    args = p.parse_args()

    store = ConceptStore(args.db_path)
    store.open()
    n = store.build_from_csv(args.concept_csv)
    store.close()
    print(f"Done. {n:,} concepts written to {args.db_path}")


if __name__ == "__main__":
    main()
