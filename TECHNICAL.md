# usagi-search: Technical Reference

## Background

[OHDSI Usagi](https://github.com/OHDSI/Usagi) is the standard tool for mapping local EHR source codes to OMOP standard concepts. Its search engine uses Apache Lucene with a custom character n-gram analyzer to find candidate concept matches by TF-IDF cosine similarity.

In practice, Usagi requires a clinician or analyst to:
- Run a Java GUI application
- Import a source code file
- Wait for Usagi to build its Lucene index and Berkeley DB (8+ hours on typical academic computing hardware)
- Manually review and accept mappings

**This service replaces Usagi's search pipeline** with a headless REST API that:
- Runs as a container with no Java dependency
- Builds its index in ~10 minutes from standard Athena vocabulary files
- Exposes concept matching over HTTP for programmatic use

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    FastAPI service                        │
│                                                          │
│  POST /search ──► NativeSearchEngine ──► SQLite DB       │
│  GET  /health                           (search.db)      │
└─────────────────────────────────────────────────────────┘
         ▲
         │ built once from
┌─────────────────────────┐
│  Athena vocabulary CSVs  │
│  CONCEPT.csv             │
│  CONCEPT_SYNONYM.csv     │
└─────────────────────────┘
```

Two search engine backends are provided:

| Backend | File | Requires | Use when |
|---|---|---|---|
| **Native** (default) | `engine_native.py` | Python only | New deployments, no existing Usagi index |
| **PyLucene** | `engine.py` | Java + PyLucene | Reading an existing Usagi Lucene index |

The native backend is the primary path. It replicates Usagi's search behavior in pure Python using SQLite as the backing store.

---

## Replication of Usagi's Search Logic

### UsagiAnalyzer (tokenization)

Usagi's `UsagiAnalyzer.java` uses:

```java
final Tokenizer source = new NGramTokenizer(matchVersion, reader, 2, 3);
TokenStream result = new StandardFilter(matchVersion, source);
result = new LowerCaseFilter(matchVersion, result);
```

This generates all contiguous substrings of length 2 and 3 from lowercased input. For example, `"diabetes"` produces:

```
di, ia, ab, be, et, te, es  (bigrams)
dia, iab, abe, bet, ete, tes  (trigrams)
```

This is **character n-gram tokenization, not word stemming**. The commented-out code in `UsagiAnalyzer.java` shows that Porter stemming and word delimiter splitting were tried and intentionally replaced with this approach.

`StandardFilter` is a no-op on ASCII text and was removed in Lucene 9.x; it is omitted here without any practical effect.

Python equivalent in `engine_native.py`:

```python
def ngrams(text: str, min_n: int = 2, max_n: int = 3) -> Set[str]:
    t = text.lower()
    result: Set[str] = set()
    for n in range(min_n, max_n + 1):
        for i in range(len(t) - n + 1):
            result.add(t[i : i + n])
    return result
```

### Scoring (recomputeScores)

Usagi discards Lucene's native BM25 score and recomputes a custom TF-IDF cosine similarity in `UsagiSearchEngine.recomputeScores()`:

```java
double tfidf = idf(reader.docFreq(new Term(field, termsEnum.term())), numDocs);

private double idf(int docFreq, int d) {
    return Math.log(d / (double) docFreq);   // natural log
}
```

Key points:
- **TF is intentionally ignored** — term frequency within a document does not affect the score
- **IDF uses natural log**: `ln(N / df)` where N is total document count
- **Cosine similarity** is computed manually over IDF-weighted term vectors

This means a term that appears once in a document is weighted identically to one that appears ten times. Only how rare the term is across the entire vocabulary matters.

Python equivalent:

```python
weight(term) = ln(num_docs / doc_freq)
score = dot(query_vec, doc_vec) / (|query_vec| × |doc_vec|)
```

### Index Schema

Usagi's Lucene index stores one document per indexed term (concept names and synonyms are separate documents). The stored fields are:

| Field | Type | Contents |
|---|---|---|
| `TYPE` | StringField | `"C"` = vocabulary concept; `"S"` = source code added to derivedIndex |
| `TERM` | TextField (with term vectors) | The indexed term text |
| `CONCEPT_ID` | StringField | OMOP concept ID |
| `DOMAIN_ID` | StringField | e.g. `"Condition"`, `"Drug"` |
| `VOCABULARY_ID` | StringField | e.g. `"SNOMED"`, `"RxNorm"` |
| `CONCEPT_CLASS_ID` | StringField | e.g. `"Disorder"`, `"Ingredient"` |
| `STANDARD_CONCEPT` | StringField | `"S"` = standard, `"C"` = classification, `""` = non-standard |
| `TERM_TYPE` | StringField | `"C"` = concept term; `"S"` = source synonym term |

The native SQLite backend uses an equivalent schema in the `docs` table.

### What Gets Indexed

Mirroring `LuceneIndexBuilder.java`:

1. **Standard and classification concepts** (`standard_concept IN ('S', 'C')`): concept name indexed as `TERM_TYPE='C'`
2. **Concept synonyms** from `CONCEPT_SYNONYM.csv` for standard/classification concepts: each synonym indexed as `TERM_TYPE='C'`
3. **Non-standard concept names** that have a `Maps to` relationship to a standard concept: indexed as `TERM_TYPE='S'` under the target standard concept (used only when `include_source_concepts=true`)

### Berkeley DB

Usagi stores concept metadata in Berkeley DB Java Edition (`sleepyCat/` directory). The Java Edition format is incompatible with Python's `bsddb3` library, which wraps the C-based Berkeley DB.

This service reads the same concept metadata directly from Athena's `CONCEPT.csv` and caches it in the same SQLite database. The data is identical — Usagi's `BerkeleyDbBuilder.java` populates its BDB from the same CSV files.

### derivedIndex vs mainIndex

Usagi maintains two Lucene indexes:

- **mainIndex**: vocabulary concepts and synonyms only
- **derivedIndex**: mainIndex + the source codes currently loaded in the GUI

The derived index shifts IDF weights so that n-grams common in *your specific source terminology* are down-weighted. This improves ranking for mapping jobs but makes the index specific to a single source file.

This service uses `mainIndex` semantics by default (general-purpose, not job-specific). Setting `USAGI_USE_DERIVED_INDEX=true` will prefer `derivedIndex` if it exists when using the PyLucene backend.

---

## Index Build

### Process

`scripts/build_native_index.py` replaces Usagi's full indexing pipeline:

1. Load all valid concepts from `CONCEPT.csv` into memory (~1M rows, ~5 seconds)
2. For each standard/classification concept, tokenize the name into n-grams and insert into `docs` + `ngram_docs`
3. For each synonym in `CONCEPT_SYNONYM.csv`, split on `;` (LOINC stores multiple synonyms per row as a semicolon-separated string), tokenize each part, and insert
4. After all inserts: `CREATE INDEX idx_ngram_docs_ngram ON ngram_docs(ngram)` in one B-tree pass
5. Compute `ngram_df` (document frequency per n-gram) via `GROUP BY`
6. Write concept metadata to the `concepts` table

### Why the Index Deferral Matters

The naive approach — creating the `ngram_docs` index before inserts — requires SQLite to maintain the B-tree on every row. With ~200M+ rows, this causes the index to grow incrementally on disk, producing constant random I/O.

Deferring index creation until after all inserts allows SQLite to build the B-tree in a single sequential pass, which is dramatically faster. In practice this reduced synonym indexing from ~38 minutes to ~2 minutes on the test VM.

### LOINC Synonym Handling

LOINC's `CONCEPT_SYNONYM.csv` rows concatenate multiple synonym keywords into a single semicolon-separated string:

```
"ABS; Aby; Antby; Anti; Antibodies; breast cancer; Neuro; ..."
```

Indexing this as a single document creates spurious n-gram overlap between unrelated search terms (the string contains "breast cancer" and "negative" as separate keywords, causing it to match "triple negative breast cancer"). The build script splits on `;` and indexes each token as a separate document.

### Timing

| Phase | 3.3 GB VM (shared storage) | MacBook Pro (NVMe) |
|---|---|---|
| Load CONCEPT.csv | ~5 s | ~3 s |
| Index ~917K concept names | ~9 min | ~18 s |
| Index ~3.9M synonyms | ~2 min | ~55 s |
| BUILD ngram_docs index | ~5 hours | ~2.5 min |
| Compute ngram_df | ~6 min | ~4 min |
| Load hierarchy (~1.76M rows) | ~2 min | ~12 s |
| **Total** | **~5.5 hours** | **~8 min** |

The `CREATE INDEX` step dominates on slow storage. The final DB is approximately 6 GB.

---

## SQLite Schema

```sql
-- One row per indexed term (concept name or synonym)
CREATE TABLE docs (
    doc_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id       INTEGER NOT NULL,
    term             TEXT    NOT NULL,
    domain_id        TEXT,
    vocabulary_id    TEXT,
    concept_class_id TEXT,
    standard_concept TEXT,
    term_type        TEXT    -- 'C' or 'S'
);

-- Inverted index: which documents contain each n-gram
CREATE TABLE ngram_docs (
    ngram  TEXT    NOT NULL,
    doc_id INTEGER NOT NULL
);
CREATE INDEX idx_ngram_docs_ngram ON ngram_docs(ngram);

-- Document frequency per n-gram (for IDF computation)
CREATE TABLE ngram_df (
    ngram    TEXT PRIMARY KEY,
    doc_freq INTEGER NOT NULL
);

-- Global stats (num_docs used in IDF formula)
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Concept metadata (concept_name lookups after search)
CREATE TABLE concepts (
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
);

-- Immediate parent-child relationships (min_levels_of_separation=1 from CONCEPT_ANCESTOR.csv).
-- Mirrors Usagi's ParentChildRelationShip BDB store.
-- concept_id is the child; parent_concept_id is the immediate parent.
CREATE TABLE concept_hierarchy (
    concept_id        INTEGER NOT NULL,
    parent_concept_id INTEGER NOT NULL,
    PRIMARY KEY (concept_id, parent_concept_id)
);
CREATE INDEX idx_hierarchy_parent ON concept_hierarchy(parent_concept_id);
```

---

## Search Query Flow

1. Tokenize search term into bigrams and trigrams
2. Compute IDF-weighted query vector using `ngram_df`
3. Select the 5 highest-IDF trigrams for candidate retrieval (limits JOIN size on common terms)
4. Execute JOIN: `ngram_docs ⋈ docs WHERE ngram IN (top_k_trigrams)` with filters applied, `GROUP BY doc_id ORDER BY ngram_hits DESC LIMIT 500`
5. For each candidate document, tokenize its term and compute IDF-weighted doc vector
6. Compute cosine similarity between query vector and each doc vector
7. Filter scores ≤ 0, deduplicate by `concept_id` (keep highest score), sort descending
8. Enrich with `concept_name` from the `concepts` table
9. Tie-break: within equal scores, prefer rows where `match_term == concept_name`
10. Enrich with `parent_count`, `child_count`, `parents[]`, and `breadcrumb` from `concept_hierarchy`

---

## API Reference

### `GET /health`

```json
{
  "status": "ok",
  "index_path": "/data/search.db",
  "index_docs": 2642392,
  "concept_db_available": true,
  "concept_db_path": "/data/search.db"
}
```

### `POST /search`

**Request:**

```json
{
  "term": "triple negative breast cancer",
  "domain_filter": ["Condition"],
  "vocabulary_filter": null,
  "concept_class_filter": null,
  "standard_only": true,
  "include_source_concepts": false,
  "top_n": 10,
  "use_mlt": true
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `term` | string | required | Source term to match |
| `domain_filter` | string[] | null | Restrict to OMOP domain IDs |
| `vocabulary_filter` | string[] | null | Restrict to vocabulary IDs |
| `concept_class_filter` | string[] | null | Restrict to concept class IDs |
| `standard_only` | bool | false | Only return standard concepts (`standard_concept='S'`) |
| `include_source_concepts` | bool | false | Include non-standard concept names indexed as source terms |
| `top_n` | int | 10 | Maximum results (1–200) |
| `use_mlt` | bool | true | Accepted for API compatibility; native engine uses the same n-gram retrieval regardless |

**Response:**

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

| Field | Description |
|---|---|
| `match_term` | The indexed synonym that drove the match — may differ from `concept_name` |
| `parent_count` | Number of immediate parent concepts in the hierarchy |
| `child_count` | Number of immediate child concepts |
| `parents` | Immediate parent concepts (up to 10) with concept_id and concept_name |
| `breadcrumb` | Ancestor path from vocabulary root to this concept, `' > '`-separated, walking one representative parent chain (OMOP is a DAG) |

---

## Deployment

### Docker / Podman

```bash
# Build
docker build -t usagi-search .

# Run (mount the pre-built search.db as a read-only volume)
docker run -d \
  -p 8000:8000 \
  -v /data/search.db:/data/search.db:ro \
  usagi-search:latest
```

The same command works with `podman` in place of `docker`. The image is OCI-compliant and can be converted for Singularity/Apptainer with `singularity pull docker://...`.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `USAGI_USAGI_DIR` | `/data` | Directory searched for `mainIndex/` (PyLucene backend only) |
| `USAGI_CONCEPT_DB_PATH` | `/data/search.db` | Path to the SQLite search + concept DB |
| `USAGI_USE_DERIVED_INDEX` | `false` | Prefer `derivedIndex/` when present (PyLucene backend only) |
| `USAGI_DEFAULT_TOP_N` | `10` | Default result count |
| `USAGI_ENGINE` | *(unset)* | Set to `pylucene` to force the PyLucene backend |

---

## Performance Characteristics

### Hardware requirements

The search DB is approximately 6 GB. For acceptable query latency, the host should have **at least 8 GB RAM** so the OS page cache can hold the hot portions of `ngram_docs`. On a 3.3 GB VM the index does not fit in cache and queries take 2–4 seconds with domain filtering; without domain filtering, common n-gram posting lists overwhelm available RAM.

### Query latency (observations)

| Query | Filters | VM (3.3 GB RAM) | Expected (8+ GB RAM) |
|---|---|---|---|
| "myocardial infarction" | domain=Condition | ~2s | <200ms |
| "triple negative breast cancer" | standard_only | ~2–4min (cold) | <500ms |
| "triple negative breast cancer" | domain=Condition | ~2s | <200ms |

Domain and vocabulary filters substantially reduce the JOIN result set and are strongly recommended for production use.

### Index build time

The bottleneck is `CREATE INDEX` on the `ngram_docs` table (~200M rows). On shared VM storage with 3.3 GB RAM this took approximately 5 hours. On a server with local NVMe and sufficient RAM, expect under 30 minutes.

---

## Fidelity Gaps vs Usagi

| Area | Gap | Impact |
|---|---|---|
| **IDF corpus** | Usagi's derivedIndex adds source codes to the corpus, shifting IDF for tokens common in the source file. The native engine uses vocabulary-only IDF. | Minor — affects ranking of common medical terms slightly |
| **LOINC synonyms** | Usagi indexes the full semicolon-concatenated synonym string as one term. This service splits on `;` and indexes each token separately, which improves precision but changes IDF weights for LOINC concepts. | Improved behavior vs Usagi (less noise) |
| **Short terms (< 3 chars)** | Candidate retrieval falls back to bigrams for terms shorter than 3 characters. Usagi uses bigrams throughout. | Negligible for typical medical terms |
| **Berkeley DB fields** | Usagi's BDB stores `parentCount`, `childCount`, and LOINC `additionalInformation`. These are not returned by this service. | Feature gap — add to response if needed |
| **`filterConceptIds`** | Usagi's GUI can constrain results to a pre-selected concept ID set. Not exposed by the REST API. | Feature gap |

---

## Future Work

- **Batch endpoint** (`POST /batch`): accept thousands of source terms in one request, process asynchronously, prioritize by source frequency, return results via `GET /batch/{job_id}/results`
- **Index size reduction**: rebuild `ngram_docs` with trigrams only (~half the current size), reducing RAM requirements
- **FTS5 backend**: SQLite's built-in trigram FTS5 tokenizer (available in SQLite ≥ 3.34) could replace the custom `ngram_docs` table with a C-optimized implementation, improving query speed on low-RAM hosts
