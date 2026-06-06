# usagi-search

Headless REST service that exposes the Usagi concept-matching search as a JSON API.  
Reads the existing Usagi Lucene index and Athena
`CONCEPT.csv` directly.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | |
| Java 11+ | `java -version` must work on PATH |
| PyLucene 9.x | See §"Installing PyLucene" below |
| Usagi index on disk | The `mainIndex/` (and optionally `derivedIndex/`) folder that Usagi builds |
| Athena `CONCEPT.csv` | From [athena.ohdsi.org](https://athena.ohdsi.org) — same download used when building the Usagi index |

---

## Quick start

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. (One-time) Upgrade the Lucene 4.9 index to Lucene 8.x format
#    so PyLucene 9.x can open it.  Downloads ~120 MB of JARs on first run.
python scripts/upgrade_lucene_index.py --index-dir /data/usagi/mainIndex

# 3. (One-time) Build the SQLite concept-name cache from Athena CONCEPT.csv
python scripts/build_concept_cache.py \
    --concept-csv /data/athena/CONCEPT.csv \
    --db-path     /data/usagi/concepts.db

# 4. Start the service
USAGI_USAGI_DIR=/data/usagi \
USAGI_CONCEPT_DB_PATH=/data/usagi/concepts.db \
uvicorn usagi_search.api:app --host 0.0.0.0 --port 8000

# 5. Run smoke tests (in a second terminal)
python test_service.py
```

The service auto-builds the concept cache on startup if `USAGI_CONCEPT_CSV` is set
and the cache file does not yet exist:

```bash
USAGI_USAGI_DIR=/data/usagi \
USAGI_CONCEPT_CSV=/data/athena/CONCEPT.csv \
uvicorn usagi_search.api:app --port 8000
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `USAGI_USAGI_DIR` | *(required)* | Root folder containing `mainIndex/` |
| `USAGI_CONCEPT_CSV` | `""` | Path to Athena `CONCEPT.csv`; triggers cache build if DB absent |
| `USAGI_CONCEPT_DB_PATH` | `<usagi_dir>/concepts.db` | SQLite cache path |
| `USAGI_USE_DERIVED_INDEX` | `false` | Use `derivedIndex/` when present (see note below) |
| `USAGI_DEFAULT_TOP_N` | `10` | Default result count |

### derivedIndex vs mainIndex

Usagi's *derived* index is a copy of `mainIndex` that additionally contains the
source-code names from the mapping file currently loaded in the GUI.  This shifts
the IDF weights so that tokens common in *your* source terminology are weighted lower.
For a general-purpose API without a specific source file, use `mainIndex`
(`USAGI_USE_DERIVED_INDEX=false`, the default).

---

## API

### `GET /health`

```json
{
  "status": "ok",
  "index_path": "/data/usagi/mainIndex",
  "index_docs": 8432105,
  "concept_db_available": true,
  "concept_db_path": "/data/usagi/concepts.db"
}
```

### `POST /search`

Request body:

```json
{
  "term": "myocardial infarction",
  "top_n": 5,
  "standard_only": true,
  "domain_filter": ["Condition"],
  "vocabulary_filter": ["SNOMED"],
  "concept_class_filter": null,
  "include_source_concepts": false,
  "use_mlt": true
}
```

Response:

```json
{
  "term": "myocardial infarction",
  "total_candidates": 24,
  "results": [
    {
      "concept_id": 4329847,
      "concept_name": "Myocardial infarction",
      "vocabulary_id": "SNOMED",
      "domain_id": "Condition",
      "concept_class_id": "Clinical Finding",
      "standard_concept": "S",
      "match_term": "Myocardial infarction",
      "similarity_score": 0.9982
    },
    ...
  ]
}
```

Interactive docs at **`/docs`** (Swagger UI) and **`/redoc`** after the service starts.

---

## Installing PyLucene

PyLucene is not on PyPI.  Three options, easiest first:

### Option A — conda-forge (pre-built wheel, recommended)

```bash
conda install -c conda-forge pylucene
```

### Option B — Docker (zero local setup)

```dockerfile
FROM coady/pylucene:latest
WORKDIR /app
COPY . .
RUN pip install fastapi uvicorn pydantic pydantic-settings httpx
CMD ["uvicorn", "usagi_search.api:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Option C — Build from source

```bash
# Requires: Java 11+, Ant ≥ 1.10, Python 3.10+, gcc/clang

wget https://dlcdn.apache.org/lucene/pylucene/pylucene-9.10.0-src.tar.gz
tar xf pylucene-9.10.0-src.tar.gz
cd pylucene-9.10.0/jcc
python setup.py build && python setup.py install
cd ..
# Edit Makefile: set PREFIX_PYTHON, PYTHON, JCC, NUM_FILES
make && make install
```

Full instructions: https://lucene.apache.org/pylucene/install.html

---

## Upgrading the Lucene index

Usagi writes its index with **Lucene 4.9**.  PyLucene 9.x cannot open it directly —
the format must be upgraded through intermediate Lucene versions.
`scripts/upgrade_lucene_index.py` automates this:

```
4.9 ──(lucene-core-5.5.5)──▶ 5.x
5.x ──(lucene-core-6.6.6)──▶ 6.x
6.x ──(lucene-core-7.7.3)──▶ 7.x
7.x ──(lucene-core-8.11.3)─▶ 8.x  ← PyLucene 9.x reads this
```

The script downloads JARs from Maven Central on first run (~120 MB total, cached in
`scripts/lib/`).  The index is upgraded **in place** — make a backup of
`mainIndex/` and `derivedIndex/` beforehand if needed.

```bash
python scripts/upgrade_lucene_index.py --index-dir /data/usagi/mainIndex
python scripts/upgrade_lucene_index.py --index-dir /data/usagi/derivedIndex  # if present
```

---

## Fidelity gaps vs the Java original

| Area | Gap | Practical impact |
|---|---|---|
| **NGramTokenizer version** | Usagi uses Lucene 4.9's `NGramTokenizer`; PyLucene 9.x ships Lucene 9.x's. Both emit all substrings of length 2–3. The only documented behavioural difference is Unicode code-point handling for surrogate pairs — irrelevant for OMOP vocabulary terms. | Negligible |
| **StandardFilter** | Removed in Lucene 9.x (was a no-op on ASCII in 7.x). Not applied in Python. | None |
| **derivedIndex IDF calibration** | The derived index includes the user's source-code names so IDF weights are calibrated for the specific mapping job. Without a source file we use `mainIndex`, shifting IDF slightly. | Minor — common vocabulary terms score marginally differently; rare terms unaffected |
| **Berkeley DB** | Python's `bsddb3` wraps C libdb and cannot read Berkeley DB JE (Java Edition) files. We substitute Athena `CONCEPT.csv` as the concept-metadata source. The data is identical — Usagi's `BerkeleyDbBuilder.java` populates its BDB from the same CSV. | None |
| **`filterConceptIds`** | Usagi's GUI passes a set of pre-selected concept IDs when the user is refining a mapping. This is not exposed by the REST API (add to `SearchRequest` if needed). | Feature gap, not a correctness issue |
| **Score normalisation** | Scores are TF-IDF cosine in [0, 1]. Lucene native BM25 scores (used as fallback when the query contains BoostQuery clauses) are not normalised. The fallback path is uncommon. | Rare edge case |

## Setup on Debian/Ubuntu VMs

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi "uvicorn[standard]" pydantic pydantic-settings
```
