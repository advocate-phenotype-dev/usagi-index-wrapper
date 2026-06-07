"""Pydantic models for API request/response."""
from typing import List, Optional
from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    term: str = Field(..., description="Source term to match against the vocabulary")
    domain_filter: Optional[List[str]] = Field(
        None, description="Restrict to domain IDs, e.g. ['Condition', 'Drug']"
    )
    vocabulary_filter: Optional[List[str]] = Field(
        None, description="Restrict to vocabulary IDs, e.g. ['SNOMED', 'RxNorm']"
    )
    concept_class_filter: Optional[List[str]] = Field(
        None, description="Restrict to concept class IDs"
    )
    standard_only: bool = Field(
        False, description="Only return standard concepts (standard_concept = 'S')"
    )
    include_source_concepts: bool = Field(
        False,
        description=(
            "Include source-coded terms (non-standard concepts whose names were "
            "added to the index as alternate terms for standard concepts)"
        ),
    )
    top_n: int = Field(10, ge=1, le=200, description="Maximum number of results to return")
    use_mlt: bool = Field(
        True,
        description=(
            "True → MoreLikeThis query (Usagi default, better for multi-word terms). "
            "False → keyword QueryParser query."
        ),
    )


class ParentConcept(BaseModel):
    concept_id: int
    concept_name: str


class ConceptResult(BaseModel):
    concept_id: int
    concept_name: str = Field(..., description="Canonical concept name from vocabulary")
    vocabulary_id: str
    domain_id: str
    concept_class_id: str
    standard_concept: str = Field(..., description="'S'=standard, 'C'=classification, ''=non-standard")
    match_term: str = Field(..., description="Indexed term that drove the match (may be a synonym)")
    similarity_score: float = Field(..., description="TF-IDF cosine similarity [0, 1]")
    parent_count: int = Field(0, description="Number of immediate parent concepts")
    child_count: int = Field(0, description="Number of immediate child concepts")
    parents: List[ParentConcept] = Field(
        default_factory=list,
        description="Immediate parent concepts (up to 10)",
    )


class SearchResponse(BaseModel):
    term: str
    results: List[ConceptResult]
    total_candidates: int = Field(..., description="Total candidates before top_n truncation")


class HealthResponse(BaseModel):
    status: str
    index_path: str
    index_docs: int
    concept_db_available: bool
    concept_db_path: str
