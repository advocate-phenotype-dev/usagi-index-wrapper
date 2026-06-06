"""
Core search engine: Python port of UsagiSearchEngine.java + UsagiAnalyzer.java.

Key design decisions and fidelity notes are annotated inline.
"""
import logging
import math
from typing import Any, Dict, List, Optional

import lucene  # PyLucene — must call lucene.initVM() before instantiating this class

from java.io import StringReader
from java.nio.file import Paths

from org.apache.lucene.analysis import Analyzer, PythonAnalyzer
from org.apache.lucene.analysis.core import LowerCaseFilter
from org.apache.lucene.analysis.ngram import NGramTokenizer
from org.apache.lucene.analysis.tokenattributes import CharTermAttribute
from org.apache.lucene.index import DirectoryReader, Term
from org.apache.lucene.queries.mlt import MoreLikeThis
from org.apache.lucene.queryparser.classic import QueryParser
from org.apache.lucene.search import (
    BooleanClause,
    BooleanQuery,
    IndexSearcher,
    TermQuery,
)
from org.apache.lucene.store import FSDirectory

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class UsagiAnalyzer(PythonAnalyzer):
    """
    Replicates UsagiAnalyzer.java exactly.

    The active pipeline is:
        NGramTokenizer(2, 3)  →  LowerCaseFilter

    The NGramTokenizer emits every contiguous substring of length 2 and 3 from
    the input text.  StandardFilter (which was chained in the Java source) is a
    no-op on ASCII text and was removed in Lucene 9.x, so it is omitted here
    without any practical difference.

    FIDELITY NOTE — the commented-out Java code shows an earlier design that used
    StandardTokenizer + PorterStemFilter + WordDelimiterFilter (word-level
    stemming).  That code was intentionally replaced with character n-grams.  Do
    NOT add stemming here.
    """

    def createComponents(self, field_name: str) -> Analyzer.TokenStreamComponents:
        tokenizer = NGramTokenizer(2, 3)
        stream = LowerCaseFilter(tokenizer)
        return Analyzer.TokenStreamComponents(tokenizer, stream)

    def initReader(self, field_name: str, reader):
        return reader


def analyze_to_tokens(analyzer: UsagiAnalyzer, text: str, field: str = "TERM") -> List[str]:
    """Tokenize *text* using *analyzer* and return the token list."""
    tokens: List[str] = []
    ts = analyzer.tokenStream(field, text)
    attr = ts.addAttribute(CharTermAttribute.class_)
    ts.reset()
    while ts.incrementToken():
        tokens.append(attr.toString())
    ts.end()
    ts.close()
    return tokens


# ---------------------------------------------------------------------------
# Search engine
# ---------------------------------------------------------------------------

class SearchEngine:
    """
    Wraps a Lucene index (mainIndex or derivedIndex) and replicates the full
    UsagiSearchEngine.search() pipeline including the custom TF-IDF cosine
    recomputation.
    """

    # TYPE field values
    CONCEPT_TYPE = "C"   # vocabulary concept document
    SOURCE_TYPE = "S"    # source-code document added to derivedIndex for IDF calibration

    # TERM_TYPE field values
    CONCEPT_TERM = "C"   # term is from concept_name / concept_synonym
    SOURCE_TERM = "S"    # term is from a non-standard concept that maps to a standard one

    def __init__(self, index_path: str):
        self.index_path = index_path
        self.reader: Optional[DirectoryReader] = None
        self.searcher: Optional[IndexSearcher] = None
        self.analyzer: Optional[UsagiAnalyzer] = None
        self.num_docs: int = 0
        self._concept_query: Optional[TermQuery] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        lucene.getVMEnv().attachCurrentThread()
        directory = FSDirectory.open(Paths.get(self.index_path))
        self.reader = DirectoryReader.open(directory)
        self.searcher = IndexSearcher(self.reader)
        self.analyzer = UsagiAnalyzer()
        self.num_docs = self.reader.numDocs()
        self._concept_query = TermQuery(Term("TYPE", self.CONCEPT_TYPE))
        logger.info(
            "Opened Lucene index at %s  (%d docs)", self.index_path, self.num_docs
        )

    def close(self) -> None:
        if self.reader:
            self.reader.close()
            self.reader = None

    # ------------------------------------------------------------------
    # Public search
    # ------------------------------------------------------------------

    def search(
        self,
        search_term: str,
        use_mlt: bool = True,
        domain_filter: Optional[List[str]] = None,
        vocabulary_filter: Optional[List[str]] = None,
        concept_class_filter: Optional[List[str]] = None,
        standard_only: bool = False,
        include_source_concepts: bool = False,
        top_n: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Replicates UsagiSearchEngine.search().

        Returns a list of dicts (without concept_name — caller must enrich from
        the concept store) sorted by similarity_score descending.
        """
        lucene.getVMEnv().attachCurrentThread()

        # --- build core query (MLT or keyword) --------------------------
        core_query = (
            self._build_mlt_query(search_term)
            if use_mlt
            else self._build_keyword_query(search_term)
        )
        if core_query is None:
            return []

        # --- wrap with TYPE=C filter + optional field filters -----------
        outer = BooleanQuery.Builder()
        outer.add(core_query, BooleanClause.Occur.SHOULD)
        outer.add(self._concept_query, BooleanClause.Occur.MUST)

        if domain_filter:
            outer.add(
                self._multi_value_filter("DOMAIN_ID", domain_filter),
                BooleanClause.Occur.MUST,
            )
        if vocabulary_filter:
            outer.add(
                self._multi_value_filter("VOCABULARY_ID", vocabulary_filter),
                BooleanClause.Occur.MUST,
            )
        if concept_class_filter:
            outer.add(
                self._multi_value_filter("CONCEPT_CLASS_ID", concept_class_filter),
                BooleanClause.Occur.MUST,
            )
        if standard_only:
            outer.add(TermQuery(Term("STANDARD_CONCEPT", "S")), BooleanClause.Occur.MUST)
        if not include_source_concepts:
            outer.add(
                TermQuery(Term("TERM_TYPE", self.CONCEPT_TERM)),
                BooleanClause.Occur.MUST,
            )

        filtered_query = outer.build()

        # --- retrieve top 100 candidates --------------------------------
        top_docs = self.searcher.search(filtered_query, 100)

        # --- recompute scores with custom TF-IDF cosine -----------------
        results = self._recompute_and_collect(top_docs.scoreDocs, core_query)

        # --- deduplicate by concept_id (keep highest score) -------------
        seen: Dict[int, bool] = {}
        deduped: List[Dict[str, Any]] = []
        for r in results:
            cid = r["concept_id"]
            if cid not in seen:
                seen[cid] = True
                deduped.append(r)

        return deduped[:top_n]

    # ------------------------------------------------------------------
    # Query builders
    # ------------------------------------------------------------------

    def _build_mlt_query(self, search_term: str):
        """
        MoreLikeThis query — Usagi's default mode.

        Configuration mirrors the Java source exactly: no min/max freq cutoffs,
        no stop words, all tokens parsed.  The n-gram nature of UsagiAnalyzer
        means every 2-3 char substring of the search term becomes a potential
        query term.
        """
        try:
            mlt = MoreLikeThis(self.reader)
            mlt.setMinTermFreq(1)
            mlt.setMinDocFreq(1)
            mlt.setMaxDocFreq(9999)
            mlt.setMinWordLen(1)
            mlt.setMaxWordLen(9999)
            mlt.setMaxDocFreqPct(100)
            mlt.setMaxNumTokensParsed(9999)
            mlt.setMaxQueryTerms(9999)
            mlt.setStopWords(None)
            mlt.setFieldNames(["TERM"])
            mlt.setAnalyzer(self.analyzer)
            return mlt.like("TERM", StringReader(search_term))
        except Exception as exc:
            logger.warning("MLT query failed (%s); falling back to keyword", exc)
            return self._build_keyword_query(search_term)

    def _build_keyword_query(self, search_term: str):
        """
        Standard QueryParser query.

        The Java code does NOT escape the input before parsing, so neither do we.
        ParseException is swallowed and returns None, replicating the Java behaviour
        of returning an empty result list.
        """
        try:
            parser = QueryParser("TERM", self.analyzer)
            return parser.parse(search_term)
        except Exception as exc:
            logger.warning("Keyword query parse failed: %s", exc)
            return None

    def _multi_value_filter(self, field: str, values: List[str]):
        """
        Build a SHOULD sub-query for a list of exact StringField values.

        DOMAIN_ID, VOCABULARY_ID, CONCEPT_CLASS_ID are all indexed as
        StringFields (not analysed), so a plain TermQuery matches exactly —
        equivalent to the Java KeywordAnalyzer + quoted-phrase approach.
        """
        sub = BooleanQuery.Builder()
        for val in values:
            sub.add(TermQuery(Term(field, val)), BooleanClause.Occur.SHOULD)
        return sub.build()

    # ------------------------------------------------------------------
    # Scoring — replicates UsagiSearchEngine.recomputeScores()
    # ------------------------------------------------------------------

    def _recompute_and_collect(
        self, score_docs, core_query
    ) -> List[Dict[str, Any]]:
        """
        Replicate Java recomputeScores() + result collection.

        Scoring:
          weight(term) = ln(numDocs / docFreq)   [IDF only — no TF]
          score        = cosine_similarity(query_vec, doc_vec)

        If the query contains non-TermQuery clauses (e.g. BoostQuery from MLT
        in certain Lucene versions), custom scoring is skipped and Lucene's
        native score is preserved — same fallback as the Java code.
        """
        query_vec = self._query_to_idf_vector(core_query)
        stored = self.reader.storedFields()

        raw: List[Dict[str, Any]] = []
        for sd in score_docs:
            doc_id = sd.doc

            if query_vec is not None:
                doc_vec = self._doc_to_idf_vector(doc_id)
                score = self._cosine(query_vec, doc_vec)
            else:
                score = float(sd.score)

            if score <= 0:
                continue

            doc = stored.document(doc_id)
            raw.append(
                {
                    "concept_id": int(doc.get("CONCEPT_ID") or 0),
                    "vocabulary_id": doc.get("VOCABULARY_ID") or "",
                    "domain_id": doc.get("DOMAIN_ID") or "",
                    "concept_class_id": doc.get("CONCEPT_CLASS_ID") or "",
                    "standard_concept": doc.get("STANDARD_CONCEPT") or "",
                    "match_term": doc.get("TERM") or "",
                    "similarity_score": round(score, 6),
                }
            )

        raw.sort(key=lambda r: r["similarity_score"], reverse=True)
        return raw

    def _query_to_idf_vector(self, query) -> Optional[Dict[str, float]]:
        """
        Walk the query tree and build an IDF-weighted term vector.
        Returns None if the query structure is not purely TermQuery-based
        (triggering the Lucene-native-score fallback).
        """
        terms: Dict[str, float] = {}
        if not self._extract_idf(query, terms):
            return None
        return terms if terms else None

    def _extract_idf(self, query, terms: Dict[str, float]) -> bool:
        """
        Recursively extract IDF weights.  Returns False (→ use native score) if
        any leaf is not a TermQuery on the TERM field.
        """
        if isinstance(query, TermQuery):
            t = query.getTerm()
            if t.field() == "TERM":
                df = self.reader.docFreq(t)
                if df > 0:
                    terms[t.text()] = math.log(self.num_docs / df)
            return True

        if isinstance(query, BooleanQuery):
            for clause in query.clauses():
                if not self._extract_idf(clause.getQuery(), terms):
                    return False
            return True

        # BoostQuery (MLT sometimes wraps terms with boosts in Lucene 9.x)
        try:
            from org.apache.lucene.search import BoostQuery  # type: ignore
            if isinstance(query, BoostQuery):
                return self._extract_idf(query.getQuery(), terms)
        except ImportError:
            pass

        return False  # unknown query type → fall back to native score

    def _doc_to_idf_vector(self, doc_id: int) -> Dict[str, float]:
        """
        Read the stored term vector for a document and build its IDF-weighted vector.

        Term vectors are stored with setStoreTermVectors(true) on the TERM field
        during index construction (textVectorField in UsagiSearchEngine.java).
        """
        result: Dict[str, float] = {}
        try:
            # Lucene 9.x prefers reader.termVectors().get(docId, field).
            # Fall back to the deprecated IndexReader.getTermVector() which
            # is still present but may be removed in a future release.
            try:
                terms = self.reader.termVectors().get(doc_id, "TERM")
            except AttributeError:
                terms = self.reader.getTermVector(doc_id, "TERM")

            if terms is None:
                return result

            te = terms.iterator()
            br = te.next()
            while br is not None:
                term_text = br.utf8ToString()
                df = self.reader.docFreq(Term("TERM", br))
                if df > 0:
                    result[term_text] = math.log(self.num_docs / df)
                br = te.next()
        except Exception as exc:
            logger.debug("Term vector read failed for doc %d: %s", doc_id, exc)
        return result

    @staticmethod
    def _cosine(v1: Dict[str, float], v2: Dict[str, float]) -> float:
        dot = sum(v1[t] * v2[t] for t in v1 if t in v2)
        n1 = math.sqrt(sum(x * x for x in v1.values()))
        n2 = math.sqrt(sum(x * x for x in v2.values()))
        if n1 == 0 or n2 == 0:
            return 0.0
        return dot / (n1 * n2)
