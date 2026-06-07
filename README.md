# usagi-search

Headless REST service for OMOP concept matching. Replicates Usagi's TF-IDF cosine n-gram search without the Java GUI or Excel dependency.

**No Java required.** Builds its own search index from Athena vocabulary files in ~10 minutes on a laptop.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | |
| Athena vocabulary files | `CONCEPT.csv`, `CONCEPT_SYNONYM.csv`, `CONCEPT_ANCESTOR.csv` from [athena.ohdsi.org](https://athena.ohdsi.org) |

---

## Quick start

```bash
# 1. Clone and create virtual environment
git clone https://github.com/advocate-phenotype-dev/usagi-index-wrapper.git
cd usagi-index-wrapper
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi "uvicorn[standard]" pydantic pydantic-settings

# 2. Build the search index (one-time, ~10 min on NVMe SSD)
python scripts/build_native_index.py \
  --concept-csv   /path/to/athena/CONCEPT.csv \
  --synonym-csv   /path/to/athena/CONCEPT_SYNONYM.csv \
  --ancestor-csv  /path/to/athena/CONCEPT_ANCESTOR.csv \
  --db-path       /data/search.db

# 3. Start the service
USAGI_CONCEPT_DB_PATH=/data/search.db \
  uvicorn usagi_search.api:app --host 0.0.0.0 --port 8000

# 4. Test
python test_service.py --term "cholecystectomy" --standard-only --top-n 5
```

---

## Docker / Podman

```bash
docker build -t usagi-search .

docker run -d -p 8000:8000 \
  -v /data/search.db:/data/search.db:ro \
  usagi-search:latest
```

Works with `podman` as a drop-in replacement. OCI-compliant for Singularity/Apptainer.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `USAGI_CONCEPT_DB_PATH` | `/data/search.db` | Path to the SQLite search + concept DB |
| `USAGI_USAGI_DIR` | `/data` | Used by PyLucene backend only |
| `USAGI_USE_DERIVED_INDEX` | `false` | PyLucene backend only |
| `USAGI_DEFAULT_TOP_N` | `10` | Default result count |
| `USAGI_ENGINE` | *(unset)* | Set to `pylucene` to use the PyLucene backend |

---

## API

### `GET /health`

```json
{
  "status": "ok",
  "index_path": "/data/search.db",
  "index_docs": 4809623,
  "concept_db_available": true,
  "concept_db_path": "/data/search.db"
}
```

### `POST /search`

Request:

```json
{
  "term": "cholecystectomy",
  "top_n": 5,
  "standard_only": true,
  "domain_filter": ["Procedure"],
  "vocabulary_filter": null,
  "concept_class_filter": null,
  "include_source_concepts": false,
  "use_mlt": true
}
```

Response:

```json
{
  "term": "cholecystectomy",
  "total_candidates": 5,
  "results": [
    {
      "concept_id": 4242997,
      "concept_name": "Cholecystectomy",
      "vocabulary_id": "SNOMED",
      "domain_id": "Procedure",
      "concept_class_id": "Procedure",
      "standard_concept": "S",
      "match_term": "Cholecystectomy",
      "similarity_score": 1.0,
      "parent_count": 2,
      "child_count": 10,
      "parents": [
        {"concept_id": 4001377, "concept_name": "Biliary tract excision"},
        {"concept_id": 4059308, "concept_name": "Operation on gallbladder"}
      ],
      "breadcrumb": "Procedure > Procedure by method > Removal > Surgical removal > Excision > Trunk excision > Abdomen excision > Biliary tract excision > Cholecystectomy"
    }
  ]
}
```

`match_term` is the indexed synonym that drove the match — may differ from `concept_name`.
`breadcrumb` is the ancestor path from vocabulary root to the concept.

Interactive docs at **`/docs`** after the service starts.

---

## Test script

`test_service.py` renders results as an indented ancestry tree:

```
4242997  Cholecystectomy  [SNOMED, Procedure]  score=1.000
  ← Biliary tract excision
    ← Abdomen excision
      ← Excision
        ← Removal
          ← Procedure
```

```bash
python test_service.py --term "Whipple Procedure" --standard-only --top-n 5
python test_service.py --term "HbA1c" --domain Measurement --top-n 3
```

---

## Hardware requirements

The search DB is ~6 GB. For acceptable query latency the host should have **≥ 8 GB RAM** so the OS page cache can hold the hot portions of the index. Typical query time is under 300 ms with domain filtering on a machine with sufficient RAM.

---

## PyLucene backend (optional)

If you have an existing Usagi Lucene index on disk and prefer to read it directly, set `USAGI_ENGINE=pylucene`. This requires Java 11+ and PyLucene (not on PyPI — install via conda-forge or build from source):

```bash
conda install -c conda-forge pylucene
```

Usagi's index is written with Lucene 4.9; PyLucene 9.x cannot read it directly. Upgrade it first:

```bash
python scripts/upgrade_lucene_index.py --index-dir /path/to/mainIndex
```

---

## Setup on Debian/Ubuntu VMs

```bash
sudo apt install python3.12-venv -y
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi "uvicorn[standard]" pydantic pydantic-settings
```
