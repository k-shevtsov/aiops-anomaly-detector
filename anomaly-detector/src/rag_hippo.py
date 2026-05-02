"""
src/rag_hippo.py  —  HippoRAG backend for IncidentStore.

Adds multi-hop knowledge graph retrieval on top of the existing RAG architecture.
Activated via: VECTOR_STORE=hipporag

Dependencies:
  - OPENAI_API_KEY env var (used by HippoRAG OpenIE for triplet extraction)
  - facebook/contriever downloaded on first run (~400MB, cached locally)

Config env vars:
  HIPPO_SAVE_DIR     — graph storage directory (default: /data/hipporag)
  HIPPO_LLM_MODEL    — OpenAI model for OpenIE (default: gpt-4o-mini)
  HIPPO_EMBED_MODEL  — embedding model (default: facebook/contriever, local)
  RAG_TOP_K          — number of results to retrieve (inherited from rag.py)
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from rag import Incident, IncidentStore, RAG_TOP_K

log = logging.getLogger(__name__)

# ── Defaults (re-read at instantiation time via os.getenv in __init__) ────────
_DEFAULT_SAVE_DIR    = "/data/hipporag"
_DEFAULT_LLM_MODEL   = "gpt-4o-mini"
_DEFAULT_EMBED_MODEL = "facebook/contriever"


class HippoRagStore(IncidentStore):
    """
    HippoRAG-backed incident store.

    Stores incidents as documents, builds a knowledge graph of
    subject->relation->object triplets, and uses Personalized PageRank
    for multi-hop retrieval.

    OpenIE (triplet extraction) uses OpenAI API (gpt-4o-mini by default).
    Results are cached in save_dir/openie_cache — no repeated API calls.

    Embedding uses facebook/contriever — runs fully locally, no API needed.
    """

    def __init__(self):
        try:
            from hipporag import HippoRAG
        except ImportError as exc:
            raise ImportError(
                "HippoRAG backend requires: pip install hipporag"
            ) from exc

        # Read env vars at instantiation — allows test overrides via os.environ
        save_dir    = os.getenv("HIPPO_SAVE_DIR",    _DEFAULT_SAVE_DIR)
        llm_model   = os.getenv("HIPPO_LLM_MODEL",   _DEFAULT_LLM_MODEL)
        embed_model = os.getenv("HIPPO_EMBED_MODEL",  _DEFAULT_EMBED_MODEL)

        Path(save_dir).mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._incidents: dict[str, Incident] = {}

        # No llm_base_url — uses OpenAI API directly
        self._hippo = HippoRAG(
            save_dir             = save_dir,
            llm_model_name       = llm_model,
            embedding_model_name = embed_model,
        )

        log.info(
            "HippoRagStore initialised (save_dir=%s, llm=%s, embed=%s)",
            save_dir, llm_model, embed_model,
        )

    def _incident_to_doc(self, incident: Incident) -> str:
        """Convert an Incident to a rich text document for indexing."""
        metrics = ", ".join(
            f"{k}={v:.3f}" for k, v in incident.metrics_snapshot.items()
        )
        return (
            f"Incident {incident.incident_id} occurred at {incident.timestamp}. "
            f"Severity: {incident.severity}. "
            f"Root cause: {incident.root_cause}. "
            f"Metrics at time of incident: {metrics}. "
            f"Explanation: {incident.explanation}. "
            f"Recommended action: {incident.recommended_action}. "
            f"Healing performed: {'yes' if incident.healing_performed else 'no'}."
            + (f" Resolution: {incident.resolution}." if incident.resolution else "")
        )

    def store(self, incident: Incident) -> None:
        """
        Add incident to the knowledge graph.
        openie_cache ensures triplets are not re-extracted for existing docs.
        """
        with self._lock:
            self._incidents[incident.incident_id] = incident
            docs = [
                self._incident_to_doc(inc)
                for inc in self._incidents.values()
            ]
            try:
                self._hippo.index(docs=docs)
                log.info(
                    "HippoRagStore indexed incident %s (total docs=%d)",
                    incident.incident_id, len(docs),
                )
            except Exception as exc:
                log.error(
                    "HippoRagStore index failed for %s: %s",
                    incident.incident_id, exc,
                )

    def retrieve(self, query_text: str, k: int = RAG_TOP_K) -> list[Incident]:
        """
        Multi-hop retrieval via Personalized PageRank over the knowledge graph.
        Returns up to k Incident objects ordered by relevance.
        """
        with self._lock:
            if not self._incidents:
                return []

            try:
                results = self._hippo.retrieve(
                    queries=[query_text],
                    num_to_retrieve=k,
                )
                retrieved_docs = results[0] if results else None
            except AssertionError as exc:
                # Graph has no matching phrases — fall back to most recent incidents
                log.warning("HippoRagStore graph lookup failed (%s) — using recency fallback", exc)
                retrieved_docs = None
            except Exception as exc:
                log.error("HippoRagStore retrieve failed: %s", exc)
                return []

            if retrieved_docs is None:
                # Fallback: return most recently stored incidents
                recent = list(self._incidents.values())[-k:]
                log.info("HippoRagStore fallback: returning %d recent incidents", len(recent))
                return recent

        # retrieved_docs is a QuerySolution object with .docs (list[str]) and .doc_scores
        incidents: list[Incident] = []
        docs_list = retrieved_docs.docs if hasattr(retrieved_docs, "docs") else []
        for doc_text in docs_list[:k]:
            for inc_id, inc in self._incidents.items():
                if inc_id in doc_text:
                    incidents.append(inc)
                    break

        log.info(
            "HippoRagStore retrieved %d/%d results for query: %s",
            len(incidents), k, query_text[:60],
        )
        return incidents

    def count(self) -> int:
        with self._lock:
            return len(self._incidents)
