"""FastAPI application — /health and /search endpoints.

Engine selection (in priority order):
  1. If USAGI_ENGINE=pylucene and PyLucene importable  → PyLucene engine
  2. If a native SQLite index exists at the db path    → NativeSearchEngine
  3. Startup fails with a clear error message
"""
import logging
import os
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException

from .concept_store import ConceptStore
from .config import get_settings
from .models import (
    ConceptResult,
    HealthResponse,
    SearchRequest,
    SearchResponse,
    ValidateRequest,
    ValidateResponse,
    CandidateVerdictResponse,
)
from .validator import ConceptValidator, ValidationResult

logger = logging.getLogger(__name__)

_engine = None
_store: ConceptStore = None   # type: ignore[assignment]
_validator: Optional[ConceptValidator] = None
_engine_type: str = "none"


# ---------------------------------------------------------------------------
# Engine selection
# ---------------------------------------------------------------------------

def _load_engine(settings):
    global _engine_type

    prefer_pylucene = os.environ.get("USAGI_ENGINE", "").lower() == "pylucene"

    if prefer_pylucene:
        try:
            import lucene
            from .engine import SearchEngine
            lucene.initVM(vmargs=["-Djava.awt.headless=true"])
            eng = SearchEngine(settings.index_path())
            eng.open()
            _engine_type = "pylucene"
            logger.info("Engine: PyLucene  index=%s", settings.index_path())
            return eng
        except Exception as exc:
            logger.warning("PyLucene engine failed (%s); falling back to native.", exc)

    # Native SQLite engine
    from .engine_native import NativeSearchEngine
    db = settings.db_path()
    if not os.path.exists(db):
        raise RuntimeError(
            f"No search index found.\n"
            f"  Native index expected at: {db}\n"
            f"  Run:  python scripts/build_demo_index.py --db-path {db}\n"
            f"  Or:   python scripts/build_native_index.py --concept-csv CONCEPT.csv "
            f"--concept-synonym-csv CONCEPT_SYNONYM.csv --db-path {db}"
        )
    eng = NativeSearchEngine(db)
    eng.open()
    _engine_type = "native"
    logger.info("Engine: native SQLite  db=%s", db)
    return eng


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine, _store, _validator
    settings = get_settings()

    _engine = _load_engine(settings)

    _store = ConceptStore(settings.db_path())
    _store.open()

    if not _store.is_available() and settings.concept_csv:
        logger.info("Building concept cache from %s…", settings.concept_csv)
        _store.build_from_csv(settings.concept_csv)
    elif not _store.is_available():
        logger.info(
            "No concept metadata cache found; concept_name will fall back to match_term."
        )

    # Validator is optional — only available when ANTHROPIC_API_KEY is set
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            _validator = ConceptValidator()
            logger.info("Clinical validator: ready (claude-opus-4-8)")
        except Exception as exc:
            logger.warning("Clinical validator unavailable: %s", exc)
    else:
        logger.info("Clinical validator: disabled (set ANTHROPIC_API_KEY to enable)")

    yield

    _engine.close()
    _store.close()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Usagi Search API",
    description=(
        "Headless REST wrapper that replicates Usagi's TF-IDF cosine n-gram "
        "search.  Supports a pure-Python SQLite backend (no Java required) and "
        "an optional PyLucene backend that reads an existing Usagi Lucene index."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse, tags=["ops"])
def health():
    settings = get_settings()
    num_docs = getattr(_engine, "num_docs", 0)
    return HealthResponse(
        status="ok",
        index_path=settings.db_path() if _engine_type == "native" else settings.index_path(),
        index_docs=num_docs,
        concept_db_available=_store.is_available(),
        concept_db_path=settings.db_path(),
    )


@app.post("/search", response_model=SearchResponse, tags=["search"])
def search(req: SearchRequest):
    if _engine is None:
        raise HTTPException(503, "Search engine not initialised")

    hits = _engine.search(
        search_term=req.term,
        use_mlt=req.use_mlt,
        domain_filter=req.domain_filter,
        vocabulary_filter=req.vocabulary_filter,
        concept_class_filter=req.concept_class_filter,
        standard_only=req.standard_only,
        include_source_concepts=req.include_source_concepts,
        top_n=req.top_n,
    )

    results: List[ConceptResult] = []
    hierarchy_available = _store.has_hierarchy()
    for h in hits:
        concept_name = _store.get_concept_name(h["concept_id"]) or h["match_term"]
        if hierarchy_available:
            parent_count, child_count = _store.get_hierarchy_counts(h["concept_id"])
            parents = _store.get_parents(h["concept_id"])
            breadcrumb = _store.get_breadcrumb(h["concept_id"], concept_name)
        else:
            parent_count, child_count, parents, breadcrumb = 0, 0, [], concept_name
        results.append(
            ConceptResult(
                concept_id=h["concept_id"],
                concept_name=concept_name,
                vocabulary_id=h["vocabulary_id"],
                domain_id=h["domain_id"],
                concept_class_id=h["concept_class_id"],
                standard_concept=h["standard_concept"],
                match_term=h["match_term"],
                similarity_score=h["similarity_score"],
                parent_count=parent_count,
                child_count=child_count,
                parents=parents,
                breadcrumb=breadcrumb,
            )
        )

    results = _reorder_ties(results)

    return SearchResponse(
        term=req.term,
        results=results[: req.top_n],
        total_candidates=len(results),
    )


def _reorder_ties(results: List[ConceptResult]) -> List[ConceptResult]:
    def key(r: ConceptResult):
        exact = r.match_term.lower() == r.concept_name.lower()
        return (-r.similarity_score, 0 if exact else 1)
    return sorted(results, key=key)


@app.post("/validate", response_model=ValidateResponse, tags=["search"])
def validate(req: ValidateRequest):
    """
    Validate concept mapping candidates using Claude (claude-opus-4-8).

    Sends the source term and top candidates to Claude with adaptive thinking
    enabled. Claude evaluates clinical accuracy and returns a structured
    verdict: correct / mismatch / ambiguous.

    Requires ANTHROPIC_API_KEY to be set on the server.
    The system prompt is cached after the first call (~90% cheaper thereafter).
    """
    if _validator is None:
        raise HTTPException(
            503,
            "Clinical validator is not available. "
            "Set ANTHROPIC_API_KEY on the server and restart.",
        )

    # Convert ConceptResult objects to plain dicts for the validator
    candidates = [c.model_dump() for c in req.candidates]

    result = _validator.validate(
        term=req.term,
        candidates=candidates,
        top_n=req.top_n,
    )

    return ValidateResponse(
        term=req.term,
        top_verdict=result.top_verdict,
        confidence=result.confidence,
        clinical_reasoning=result.clinical_reasoning,
        recommended_concept_name=result.recommended_concept_name,
        recommended_concept_notes=result.recommended_concept_notes,
        candidate_verdicts=[
            CandidateVerdictResponse(**v.model_dump())
            for v in result.candidate_verdicts
        ],
    )
