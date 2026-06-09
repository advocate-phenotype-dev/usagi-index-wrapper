"""
Clinical AI validation of Usagi concept mappings using Claude.

Calls claude-opus-4-8 with adaptive thinking to evaluate whether the
top-ranked candidates from usagi_search() are clinically accurate mappings.

Prompt caching is applied to the system prompt — it never changes across
requests, so after the first call it costs ~0.1x for all subsequent validations.
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional

import anthropic
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt — cached; never changes
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an expert clinical informaticist specializing in \
OMOP CDM vocabulary and clinical terminology mapping. Your task is to \
evaluate whether automated concept mappings produced by a character n-gram \
TF-IDF search engine (Usagi) are clinically accurate.

For each source term you receive the top-ranked OMOP standard concept \
candidates along with their vocabulary, domain, concept class, similarity \
score, and hierarchical breadcrumb path from the vocabulary root.

Your evaluation must consider:
- Scope and anatomical extent (e.g. excision of a single node ≠ en-bloc \
dissection of a nodal basin)
- Procedure intent (diagnostic biopsy ≠ therapeutic resection)
- Specificity and granularity (parent concept ≠ child concept)
- Clinical implications: morbidity documentation, staging accuracy, quality \
metrics, and clinical decision support rule firing
- Whether an abbreviation or colloquial term maps to the right formal concept

Return a JSON object conforming exactly to the schema provided. Do not add \
commentary outside the JSON."""

# ---------------------------------------------------------------------------
# Response schema (Pydantic + JSON Schema)
# ---------------------------------------------------------------------------

class CandidateVerdict(BaseModel):
    concept_id: int
    concept_name: str
    verdict: str          # "correct" | "mismatch" | "ambiguous"
    notes: str


class ValidationResult(BaseModel):
    top_verdict: str                              # "correct" | "mismatch" | "ambiguous"
    confidence: str                               # "high" | "medium" | "low"
    clinical_reasoning: str
    recommended_concept_name: Optional[str] = None
    recommended_concept_notes: Optional[str] = None
    candidate_verdicts: List[CandidateVerdict]


_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "top_verdict": {
            "type": "string",
            "enum": ["correct", "mismatch", "ambiguous"],
            "description": "Verdict on the top-ranked candidate",
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "Confidence in the verdict",
        },
        "clinical_reasoning": {
            "type": "string",
            "description": "Clinical explanation of the key distinction and why it matters",
        },
        "recommended_concept_name": {
            "type": "string",
            "description": "If verdict is mismatch or ambiguous: name of a better OMOP concept",
        },
        "recommended_concept_notes": {
            "type": "string",
            "description": "Why the recommended concept is more appropriate",
        },
        "candidate_verdicts": {
            "type": "array",
            "description": "Per-candidate verdicts",
            "items": {
                "type": "object",
                "properties": {
                    "concept_id":   {"type": "integer"},
                    "concept_name": {"type": "string"},
                    "verdict":      {"type": "string", "enum": ["correct", "mismatch", "ambiguous"]},
                    "notes":        {"type": "string"},
                },
                "required": ["concept_id", "concept_name", "verdict", "notes"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["top_verdict", "confidence", "clinical_reasoning", "candidate_verdicts"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class ConceptValidator:
    def __init__(self):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY environment variable is not set. "
                "The /validate endpoint requires it."
            )
        self._client = anthropic.Anthropic(api_key=api_key)

    def validate(
        self,
        term: str,
        candidates: List[Dict[str, Any]],
        top_n: int = 3,
    ) -> ValidationResult:
        """
        Call Claude to evaluate whether the top candidates are clinically
        accurate mappings for the given source term.
        """
        subset = candidates[:top_n]
        user_prompt = _build_prompt(term, subset)

        response = self._client.messages.create(
            model="claude-opus-4-8",
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    # Cache the system prompt — identical across every validate call
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": _OUTPUT_SCHEMA,
                }
            },
            messages=[{"role": "user", "content": user_prompt}],
        )

        result_text = next(
            (b.text for b in response.content if b.type == "text"), "{}"
        )
        cache_read = getattr(response.usage, "cache_read_input_tokens", 0)
        logger.info(
            "Validate '%s': verdict=%s  cache_read=%d tokens",
            term,
            json.loads(result_text).get("top_verdict", "?"),
            cache_read,
        )
        return ValidationResult.model_validate(json.loads(result_text))


def _build_prompt(term: str, candidates: List[Dict[str, Any]]) -> str:
    lines = [
        f'Source term to map: "{term}"',
        "",
        "Top candidates from OMOP vocabulary search:",
    ]
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"\n#{i}  concept_id={c['concept_id']}  score={c.get('similarity_score', '?'):.3f}"
        )
        lines.append(f"    Name:        {c['concept_name']}")
        lines.append(f"    Vocabulary:  {c.get('vocabulary_id', '')}  |  Domain: {c.get('domain_id', '')}")
        lines.append(f"    Class:       {c.get('concept_class_id', '')}  |  Standard: {c.get('standard_concept', '')}")
        bc = c.get("breadcrumb", "")
        if bc:
            lines.append(f"    Breadcrumb:  {bc}")
        matched = c.get("match_term", "")
        if matched and matched.lower() != c["concept_name"].lower():
            lines.append(f"    Matched via: {matched}")

    lines.append(
        "\nEvaluate each candidate and return the JSON verdict object."
    )
    return "\n".join(lines)
