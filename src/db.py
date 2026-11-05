"""Unified SQLite Database Manager with FTS5 search and asynchronous job queue."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 1. Atomic Memories
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    profile TEXT NOT NULL DEFAULT 'default',
    normalized_key TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    title TEXT NOT NULL,
    statement TEXT NOT NULL,
    subject_id TEXT NOT NULL DEFAULT '',
    predicate TEXT NOT NULL DEFAULT '',
    value TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    importance REAL NOT NULL DEFAULT 1.0,
    stability TEXT NOT NULL DEFAULT 'stable',
    sensitivity TEXT NOT NULL DEFAULT 'private',
    source_ids TEXT NOT NULL DEFAULT '[]',
    superseded_by TEXT,
    user_locked INTEGER NOT NULL DEFAULT 0,
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_recalled_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_memories_profile ON memories(profile);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);

-- FTS5 table for atomic memories
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    memory_id UNINDEXED,
    title,
    statement,
    kind,
    profile UNINDEXED,
    tokenize='unicode61'
);

-- Triggers for memory_fts synchronization
DROP TRIGGER IF EXISTS trg_memories_ai;
CREATE TRIGGER trg_memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memory_fts(memory_id, title, statement, kind, profile)
    VALUES (new.id, new.title, new.statement, new.kind, new.profile);
END;

DROP TRIGGER IF EXISTS trg_memories_ad;
CREATE TRIGGER trg_memories_ad AFTER DELETE ON memories BEGIN
    DELETE FROM memory_fts WHERE memory_id = old.id;
END;

DROP TRIGGER IF EXISTS trg_memories_au;
CREATE TRIGGER trg_memories_au AFTER UPDATE ON memories BEGIN
    DELETE FROM memory_fts WHERE memory_id = old.id;
    INSERT INTO memory_fts(memory_id, title, statement, kind, profile)
    VALUES (new.id, new.title, new.statement, new.kind, new.profile);
END;


-- 2. General Document Chunks (Projects, Decisions, Daily, Manual notes)
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile TEXT NOT NULL DEFAULT 'default',
    doc_path TEXT NOT NULL,
    heading TEXT NOT NULL,
    level INTEGER NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    tokens_est INTEGER NOT NULL,
    hash TEXT NOT NULL UNIQUE,
    importance REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_profile ON chunks(profile);
CREATE INDEX IF NOT EXISTS idx_chunks_doc_path ON chunks(doc_path);

-- FTS5 table for document chunks
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    heading,
    content,
    tags,
    doc_path UNINDEXED,
    profile UNINDEXED,
    tokenize='unicode61'
);

DROP TRIGGER IF EXISTS trg_chunks_ai;
CREATE TRIGGER trg_chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(chunk_id, heading, content, tags, doc_path, profile)
    VALUES (new.id, new.heading, new.content, new.tags, new.doc_path, new.profile);
END;

DROP TRIGGER IF EXISTS trg_chunks_ad;
CREATE TRIGGER trg_chunks_ad AFTER DELETE ON chunks BEGIN
    DELETE FROM chunks_fts WHERE chunk_id = old.id;
END;

DROP TRIGGER IF EXISTS trg_chunks_au;
CREATE TRIGGER trg_chunks_au AFTER UPDATE ON chunks BEGIN
    DELETE FROM chunks_fts WHERE chunk_id = old.id;
    INSERT INTO chunks_fts(chunk_id, heading, content, tags, doc_path, profile)
    VALUES (new.id, new.heading, new.content, new.tags, new.doc_path, new.profile);
END;


-- 3. Asynchronous Job Queue for Zero-Latency Extraction and Reflection
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT 'default',
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);


-- 4. Captured Raw Sources
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    profile TEXT NOT NULL DEFAULT 'default',
    path TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL DEFAULT '',
    turn_number INTEGER NOT NULL DEFAULT 0,
    captured_at TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    user_content TEXT NOT NULL DEFAULT '',
    assistant_content TEXT NOT NULL DEFAULT '',
    sensitivity TEXT NOT NULL DEFAULT 'private'
);


-- 5. Audit Events
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile TEXT NOT NULL DEFAULT 'default',
    event TEXT NOT NULL,
    object_id TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


class DatabaseManager:
    """Manages SQLite connections, schema initialization, and transactional queries."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def get_connection(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=30.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON;")
        return con

    def _init_db(self) -> None:
        with self.get_connection() as con:
            con.executescript(SCHEMA_SQL)
            con.commit()

    def enqueue_job(self, kind: str, profile: str, payload: Dict[str, Any], created_at: str) -> int:
        """Insert a background task into the jobs queue."""
        with self.get_connection() as con:
            cur = con.cursor()
            cur.execute(
                """
                INSERT INTO jobs (kind, profile, payload, status, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (kind, profile, json.dumps(payload, ensure_ascii=False), created_at, created_at),
            )
            con.commit()
            return int(cur.lastrowid)

    def fetch_pending_jobs(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self.get_connection() as con:
            cur = con.cursor()
            cur.execute(
                """
                SELECT id, kind, profile, payload, status, attempts, created_at
                FROM jobs
                WHERE status = 'pending'
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cur.fetchall()
            results = []
            for r in rows:
                results.append({
                    "id": r["id"],
                    "kind": r["kind"],
                    "profile": r["profile"],
                    "payload": json.loads(r["payload"]),
                    "status": r["status"],
                    "attempts": r["attempts"],
                    "created_at": r["created_at"],
                })
            return results

    def mark_job_done(self, job_id: int, updated_at: str) -> None:
        with self.get_connection() as con:
            con.execute(
                "UPDATE jobs SET status = 'done', updated_at = ? WHERE id = ?",
                (updated_at, job_id),
            )
            con.commit()

    def mark_job_failed(self, job_id: int, error_msg: str, updated_at: str) -> None:
        with self.get_connection() as con:
            con.execute(
                """
                UPDATE jobs
                SET status = 'failed', attempts = attempts + 1, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error_msg, updated_at, job_id),
            )
            con.commit()
