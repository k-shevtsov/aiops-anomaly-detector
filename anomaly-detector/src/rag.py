"""
src/rag.py  —  RAG store for past incidents (Tier-2 Step-1).

Architecture:
  ┌─────────────────────────────────────────────────────────────┐
  │  IncidentStore  (abstract interface)                        │
  │    .store(incident)  →  persists + indexes embedding        │
  │    .retrieve(query, k)  →  top-k similar past incidents     │
  └──────────────────┬──────────────────────────────────────────┘
                     │
        ┌────────────┴────────────────────┐
        ▼                                 ▼
  SqliteVecStore  (default)        PgVectorStore  (prod upgrade)
  sqlite + sqlite-vec              PostgreSQL + pgvector
  zero infra, runs in-pod          swap: set VECTOR_STORE=pgvector
                                   + PGVECTOR_DSN=postgresql://...

Embedding backend (swap via EMBEDDING_BACKEND env):
  "hashing"  — HashingVectorizer, dim=128, zero deps (default)
  "voyage"   — voyage-3-lite via voyageai SDK, dim=1024 (production)
  "openai"   — text-embedding-3-small, dim=1536 (alternative)

Usage in agent.py:
    from rag import get_store, Incident
    store = get_store()
    similar = store.retrieve(query_text, k=3)
    # inject similar as few-shot examples into system prompt
    store.store(Incident(...))   # after resolution
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import struct
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
VECTOR_STORE       = os.getenv("VECTOR_STORE",       "sqlite")          # sqlite | pgvector
EMBEDDING_BACKEND  = os.getenv("EMBEDDING_BACKEND",  "hashing")         # hashing | voyage | openai
EMBED_DIM          = int(os.getenv("EMBED_DIM",       "128"))            # must match backend
SQLITE_DB_PATH     = os.getenv("SQLITE_DB_PATH",      "/data/incidents.db")
RAG_TOP_K          = int(os.getenv("RAG_TOP_K",       "3"))              # few-shot examples
RAG_MIN_SIMILARITY = float(os.getenv("RAG_MIN_SIMILARITY", "0.3"))       # distance threshold
PGVECTOR_DSN       = os.getenv("PGVECTOR_DSN",        "")               # for prod upgrade


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Incident:
    """
    One resolved incident — the unit stored and retrieved.
    All fields are plain Python types so the dataclass serialises to JSON.
    """
    incident_id:        str
    timestamp:          str                        # ISO-8601
    score:              float
    severity:           str                        # low|medium|high|critical
    root_cause:         str
    metrics_snapshot:   dict[str, float]           # error_rate, p95_latency, …
    explanation:        str                        # agent's summary
    recommended_action: str
    healing_performed:  bool
    resolution:         str = ""                   # filled in later if known
    # ── computed on store() ───────────────────────────────────────────────────
    embed_text: str = field(default="", repr=False)  # text that was embedded

    def to_few_shot_text(self) -> str:
        """
        Returns a compact string suitable for injection into a Claude prompt
        as a few-shot example.
        """
        metrics = ", ".join(
            f"{k}={v:.3f}" for k, v in self.metrics_snapshot.items()
            if k in ("error_rate", "p95_latency", "request_rate")
        )
        return (
            f"[Past incident {self.incident_id[:8]} | {self.severity.upper()} | "
            f"{self.timestamp[:16]}]\n"
            f"Metrics: {metrics}\n"
            f"Root cause: {self.root_cause}\n"
            f"Action: {self.recommended_action}\n"
            f"Healed: {'yes' if self.healing_performed else 'no'}"
            + (f"\nResolution: {self.resolution}" if self.resolution else "")
        )


# ── Embedding backends ────────────────────────────────────────────────────────

def _make_embed_text(incident: Incident) -> str:
    """Canonical text representation used for embedding."""
    return (
        f"severity={incident.severity} "
        f"root_cause={incident.root_cause} "
        f"error_rate={incident.metrics_snapshot.get('error_rate', 0.0):.3f} "
        f"p95_latency={incident.metrics_snapshot.get('p95_latency', 0.0):.3f} "
        f"healing={incident.healing_performed} "
        f"explanation={incident.explanation[:200]}"
    )


def _embed_hashing(text: str) -> list[float]:
    """
    HashingVectorizer — fixed DIM=128, zero downloads, works offline.
    Good for development and demo. Semantic quality is lower than dense models.
    """
    from sklearn.feature_extraction.text import HashingVectorizer
    hvec = HashingVectorizer(n_features=EMBED_DIM, norm="l2", alternate_sign=False)
    return hvec.transform([text]).toarray()[0].tolist()


def _embed_voyage(text: str) -> list[float]:
    """
    Voyage AI voyage-3-lite — 1024-dim dense embeddings, production quality.
    Requires: pip install voyageai + VOYAGE_API_KEY env var.
    Set EMBED_DIM=1024 when switching to this backend.
    """
    import voyageai  # type: ignore
    client = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
    result = client.embed([text], model="voyage-3-lite", input_type="document")
    return result.embeddings[0]


def _embed_openai(text: str) -> list[float]:
    """
    OpenAI text-embedding-3-small — 1536-dim.
    Requires: pip install openai + OPENAI_API_KEY env var.
    Set EMBED_DIM=1536 when switching.
    """
    from openai import OpenAI  # type: ignore
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.embeddings.create(model="text-embedding-3-small", input=text)
    return resp.data[0].embedding


def embed(text: str) -> list[float]:
    """
    Route to the configured embedding backend.
    Swap backend by setting EMBEDDING_BACKEND env var — no code changes needed.
    """
    backend = EMBEDDING_BACKEND
    if backend == "hashing":
        return _embed_hashing(text)
    elif backend == "voyage":
        return _embed_voyage(text)
    elif backend == "openai":
        return _embed_openai(text)
    else:
        raise ValueError(f"Unknown EMBEDDING_BACKEND: {backend!r}")


def _pack_vector(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def _unpack_vector(b: bytes) -> list[float]:
    n = len(b) // 4
    return list(struct.unpack(f"{n}f", b))


# ── Abstract store interface ───────────────────────────────────────────────────

class IncidentStore(ABC):
    """
    Abstract interface — swap implementations without touching agent.py.

    To upgrade to pgvector:
      1. Set VECTOR_STORE=pgvector + PGVECTOR_DSN=postgresql://...
      2. Set EMBEDDING_BACKEND=voyage + EMBED_DIM=1024
      3. No other code changes needed.
    """

    @abstractmethod
    def store(self, incident: Incident) -> None:
        """Persist an incident and index its embedding."""

    @abstractmethod
    def retrieve(self, query_text: str, k: int = RAG_TOP_K) -> list[Incident]:
        """Return up to k most similar past incidents."""

    @abstractmethod
    def count(self) -> int:
        """Return total number of stored incidents."""

    def retrieve_as_few_shot(self, query_text: str, k: int = RAG_TOP_K) -> str:
        """
        Convenience wrapper — returns a formatted string ready for prompt injection.
        Returns empty string when store is empty.
        """
        incidents = self.retrieve(query_text, k=k)
        if not incidents:
            return ""
        header = f"## {len(incidents)} similar past incident(s) for context:\n\n"
        body   = "\n\n".join(inc.to_few_shot_text() for inc in incidents)
        return header + body


# ── SQLite + sqlite-vec implementation ───────────────────────────────────────

class SqliteVecStore(IncidentStore):
    """
    Local SQLite store with sqlite-vec for ANN search.

    Schema:
      incidents   — full incident JSON (source of truth)
      vec_index   — virtual vec0 table, rowid FK → incidents.rowid
    """

    def __init__(self, db_path: str = SQLITE_DB_PATH):
        self._db_path = db_path
        self._lock    = threading.Lock()
        self._conn    = self._init_db(db_path)
        log.info("SqliteVecStore initialised at %s (dim=%d)", db_path, EMBED_DIM)

    # ── internal ─────────────────────────────────────────────────────────────

    @staticmethod
    def _init_db(db_path: str) -> sqlite3.Connection:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        # Load sqlite-vec extension
        try:
            import sqlite_vec  # type: ignore
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception as exc:
            log.error("sqlite-vec load failed: %s — vector search disabled", exc)

        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS incidents (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT    UNIQUE NOT NULL,
                timestamp   TEXT    NOT NULL,
                severity    TEXT    NOT NULL,
                root_cause  TEXT    NOT NULL,
                payload     TEXT    NOT NULL,   -- full Incident JSON
                embed_text  TEXT    NOT NULL,
                created_at  REAL    NOT NULL    -- monotonic for ordering
            );

            CREATE TABLE IF NOT EXISTS schema_version (version INTEGER);
            INSERT OR IGNORE INTO schema_version VALUES (1);
        """)

        # Create vec0 virtual table (may already exist)
        try:
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_index "
                f"USING vec0(embedding float[{EMBED_DIM}])"
            )
        except Exception as exc:
            log.warning("vec0 table creation: %s", exc)

        conn.commit()
        return conn

    # ── public API ────────────────────────────────────────────────────────────

    def store(self, incident: Incident) -> None:
        embed_text = _make_embed_text(incident)
        incident.embed_text = embed_text

        try:
            vec = embed(embed_text)
        except Exception as exc:
            log.error("Embedding failed for %s: %s — skipping store", incident.incident_id, exc)
            return

        payload = json.dumps(asdict(incident))
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT OR REPLACE INTO incidents
                        (incident_id, timestamp, severity, root_cause, payload, embed_text, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        incident.incident_id,
                        incident.timestamp,
                        incident.severity,
                        incident.root_cause,
                        payload,
                        embed_text,
                        time.monotonic(),
                    ),
                )
                rowid = cur.lastrowid

                # Upsert into vec_index — delete first to handle OR REPLACE
                self._conn.execute("DELETE FROM vec_index WHERE rowid = ?", [rowid])
                self._conn.execute(
                    "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
                    [rowid, _pack_vector(vec)],
                )
                self._conn.commit()
                log.info(
                    "RAG stored incident %s (severity=%s, rowid=%d)",
                    incident.incident_id, incident.severity, rowid,
                )
            except Exception as exc:
                log.error("RAG store failed for %s: %s", incident.incident_id, exc)
                self._conn.rollback()

    def retrieve(self, query_text: str, k: int = RAG_TOP_K) -> list[Incident]:
        try:
            q_vec  = embed(query_text)
            q_bytes = _pack_vector(q_vec)
        except Exception as exc:
            log.error("RAG embed for retrieval failed: %s", exc)
            return []

        with self._lock:
            try:
                rows = self._conn.execute(
                    """
                    SELECT i.payload, v.distance
                    FROM vec_index v
                    JOIN incidents i ON i.id = v.rowid
                    WHERE v.embedding MATCH ?
                      AND k = ?
                    ORDER BY v.distance
                    """,
                    [q_bytes, k],
                ).fetchall()
            except Exception as exc:
                log.error("RAG retrieve failed: %s", exc)
                return []

        incidents: list[Incident] = []
        for row in rows:
            distance = row["distance"]
            # Skip results that are too dissimilar
            # sqlite-vec returns L2 distance; lower = more similar
            # RAG_MIN_SIMILARITY is reused as max_distance here
            if distance > (2.0 - RAG_MIN_SIMILARITY):
                continue
            try:
                data = json.loads(row["payload"])
                incidents.append(Incident(**data))
            except Exception as exc:
                log.warning("RAG deserialise failed: %s", exc)

        log.info("RAG retrieved %d/%d candidates for query", len(incidents), k)
        return incidents

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]


# ── pgvector stub (production upgrade path) ───────────────────────────────────

class PgVectorStore(IncidentStore):
    """
    PostgreSQL + pgvector — production-grade vector store.

    To activate:
      export VECTOR_STORE=pgvector
      export PGVECTOR_DSN=postgresql://user:pass@host:5432/dbname
      export EMBEDDING_BACKEND=voyage
      export EMBED_DIM=1024

    Requires: pip install psycopg2-binary pgvector
    The interface is identical to SqliteVecStore — drop-in replacement.
    """

    def __init__(self):
        try:
            import psycopg2  # type: ignore
            from pgvector.psycopg2 import register_vector  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "pgvector backend requires: pip install psycopg2-binary pgvector"
            ) from exc

        self._dsn  = PGVECTOR_DSN
        self._lock = threading.Lock()
        self._init_schema()
        log.info("PgVectorStore initialised (dsn masked), dim=%d", EMBED_DIM)

    def _conn(self):
        import psycopg2
        from pgvector.psycopg2 import register_vector
        conn = psycopg2.connect(self._dsn)
        register_vector(conn)
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS incidents (
                    id          SERIAL PRIMARY KEY,
                    incident_id TEXT UNIQUE NOT NULL,
                    timestamp   TEXT NOT NULL,
                    severity    TEXT NOT NULL,
                    root_cause  TEXT NOT NULL,
                    payload     JSONB NOT NULL,
                    embed_text  TEXT NOT NULL,
                    embedding   vector({EMBED_DIM}),
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS incidents_embedding_idx "
                f"ON incidents USING ivfflat (embedding vector_l2_ops) WITH (lists=100)"
            )
        conn.commit()

    def store(self, incident: Incident) -> None:
        embed_text = _make_embed_text(incident)
        incident.embed_text = embed_text
        vec = embed(embed_text)
        import numpy as np
        payload = json.dumps(asdict(incident))
        with self._lock, self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO incidents (incident_id, timestamp, severity, root_cause,
                                       payload, embed_text, embedding)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (incident_id) DO UPDATE
                    SET payload=EXCLUDED.payload, embedding=EXCLUDED.embedding
                """,
                (incident.incident_id, incident.timestamp, incident.severity,
                 incident.root_cause, payload, embed_text, np.array(vec)),
            )
        conn.commit()
        log.info("PgVectorStore stored %s", incident.incident_id)

    def retrieve(self, query_text: str, k: int = RAG_TOP_K) -> list[Incident]:
        import numpy as np
        vec = embed(query_text)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload, embedding <-> %s AS distance
                FROM incidents
                ORDER BY distance
                LIMIT %s
                """,
                (np.array(vec), k),
            )
            rows = cur.fetchall()
        incidents = []
        for payload, distance in rows:
            if distance > (2.0 - RAG_MIN_SIMILARITY):
                continue
            try:
                incidents.append(Incident(**payload))
            except Exception as exc:
                log.warning("PgVectorStore deserialise: %s", exc)
        return incidents

    def count(self) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM incidents")
            return cur.fetchone()[0]


# ── Factory ───────────────────────────────────────────────────────────────────

_store_instance: Optional[IncidentStore] = None
_store_lock = threading.Lock()


def get_store() -> IncidentStore:
    """
    Module-level singleton factory.
    Returns SqliteVecStore or PgVectorStore based on VECTOR_STORE env.
    Thread-safe — safe to call from multiple async tasks.
    """
    global _store_instance
    if _store_instance is not None:
        return _store_instance

    with _store_lock:
        if _store_instance is not None:   # double-checked locking
            return _store_instance

        backend = VECTOR_STORE
        if backend == "sqlite":
            _store_instance = SqliteVecStore()
        elif backend == "pgvector":
            _store_instance = PgVectorStore()
        elif backend == "hipporag":
            from rag_hippo import HippoRagStore
            _store_instance = HippoRagStore()
        else:
            raise ValueError(f"Unknown VECTOR_STORE: {backend!r}")

        log.info("RAG store initialised: %s (embedding=%s dim=%d)",
                 type(_store_instance).__name__, EMBEDDING_BACKEND, EMBED_DIM)
        return _store_instance


def store_resolved_incident(result, score: float, metrics: dict, incident_id: str) -> None:
    """
    Convenience function called from agent.py after run_agent() completes.
    Converts AgentResult → Incident and persists it.
    """
    try:
        store = get_store()
        incident = Incident(
            incident_id        = incident_id,
            timestamp          = datetime.now(timezone.utc).isoformat(),
            score              = score,
            severity           = result.severity,
            root_cause         = result.root_cause,
            metrics_snapshot   = metrics,
            explanation        = result.explanation,
            recommended_action = result.recommended_action,
            healing_performed  = result.healing_performed,
        )
        store.store(incident)
    except Exception as exc:
        log.error("store_resolved_incident failed: %s", exc)
