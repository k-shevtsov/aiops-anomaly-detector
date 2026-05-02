"""
src/rag_hippo.py  —  HippoRAG backend for IncidentStore.

Adds multi-hop knowledge graph retrieval on top of the existing RAG architecture.
Activated via: VECTOR_STORE=hipporag

Dependencies:
  - embedding-server running in shared-infra namespace (or localhost:8001)
  - Ollama running with gemma2:9b model

Config env vars:
  HIPPO_SAVE_DIR       — where HippoRAG stores its graph (default: /data/hipporag)
  HIPPO_LLM_MODEL      — Ollama model for OpenIE (default: gemma2:9b)
  HIPPO_LLM_BASE_URL   — Ollama API URL (default: http://ollama:11434/v1)
  HIPPO_EMBED_BASE_URL — embedding server URL
  HIPPO_EMBED_MODEL    — model name (default: intfloat/e5-large-v2)
  RAG_TOP_K            — number of results to retrieve (inherited from rag.py)
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from rag import Incident, IncidentStore, RAG_TOP_K

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
HIPPO_SAVE_DIR     = os.getenv("HIPPO_SAVE_DIR",     "/data/hipporag")
HIPPO_LLM_MODEL    = os.getenv("HIPPO_LLM_MODEL",    "gemma2:9b")
HIPPO_LLM_BASE_URL = os.getenv("HIPPO_LLM_BASE_URL", "http://ollama:11434/v1")
HIPPO_EMBED_BASE_URL = os.getenv(
    "HIPPO_EMBED_BASE_URL",
    "http://embedding-server.shared-infra.svc.cluster.local:8001/v1"
)
HIPPO_EMBED_MODEL  = os.getenv("HIPPO_EMBED_MODEL",  "intfloat/e5-large-v2")


class HippoRagStore(IncidentStore):
    """
    HippoRAG-backed incident store.

    Stores incidents as documents, builds a knowledge graph of
    subject->relation->object triplets, and uses Personalized PageRank
    for multi-hop retrieval.

    This enables finding root causes that span multiple documents:
    e.g. 'high latency' -> 'postgres replica lag' -> 'disk I/O saturation'.

    Implements the same IncidentStore interface as SqliteVecStore.
    """

    def __init__(self):
        try:
            from hipporag import HippoRAG
        except ImportError as exc:
            raise ImportError(
                "HippoRAG backend requires: pip install hipporag"
            ) from exc

        Path(HIPPO_SAVE_DIR).mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._incidents: dict[str, Incident] = {}

        self._hippo = HippoRAG(
            save_dir             = HIPPO_SAVE_DIR,
            llm_model_name       = f"ollama/{HIPPO_LLM_MODEL}",
            llm_base_url         = HIPPO_LLM_BASE_URL,
            llm_api_key          = "ollama",
            embedding_model_name = HIPPO_EMBED_MODEL,
            embedding_base_url   = HIPPO_EMBED_BASE_URL,
            embedding_api_key    = "local",
        )

        log.info(
            "HippoRagStore initialised (save_dir=%s, llm=%s)",
            HIPPO_SAVE_DIR, HIPPO_LLM_MODEL,
        )

    # ── helpers ───────────────────────────────────────────────────────────────

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

    # ── public API ────────────────────────────────────────────────────────────

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
                retrieved_docs = results[0] if results else []
            except Exception as exc:
                log.error("HippoRagStore retrieve failed: %s", exc)
                return []

        incidents: list[Incident] = []
        for item in retrieved_docs[:k]:
            doc_text = item.get("text", "")
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
