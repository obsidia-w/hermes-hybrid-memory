"""Local Markdown source of truth and rebuildable SQLite index."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    import hermes_yaml as yaml
except ModuleNotFoundError as exc:
    if exc.name != "hermes_yaml":
        raise
    import yaml

from .config import ObsidianConfig
from .curator import (
    MemoryCandidate,
    contains_secret,
    extract_candidates,
    looks_like_injection,
)


_UUID_NAMESPACE = uuid.UUID("fb923d17-77d2-4fa7-89ef-6e97cca07b67")
_MANAGED_START = "<!-- hermes:managed:start -->"
_MANAGED_END = "<!-- hermes:managed:end -->"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str, limit: int = 64) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "-", text or "memory")
    value = re.sub(r"\s+", "-", value.strip()).strip("-.")
    return (value or "memory")[:limit]


def _frontmatter(data: Dict[str, Any]) -> str:
    return "---\n" + yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False
    ) + "---\n"


class ObsidianStore:
    """Owns Vault writes, the derived index, curation jobs and audit state."""

    REQUIRED_DIRS = (
        "00_Inbox/Imports",
        "10_Sources/Conversations",
        "10_Sources/Documents",
        "10_Sources/Artifacts",
        "20_Memories/Profile",
        "20_Memories/Preferences",
        "20_Memories/Projects",
        "20_Memories/Decisions",
        "20_Memories/Knowledge",
        "20_Memories/Episodes",
        "30_Entities/People",
        "30_Entities/Projects",
        "30_Entities/Tools",
        "30_Entities/Concepts",
        "40_Reflections/Daily",
        "40_Reflections/Weekly",
        "40_Reflections/Contradictions",
        "90_System/Audit",
        "90_System/Quarantine",
    )

    KIND_DIRS = {
        "profile": "20_Memories/Profile",
        "preference": "20_Memories/Preferences",
        "project": "20_Memories/Projects",
        "decision": "20_Memories/Decisions",
        "knowledge": "20_Memories/Knowledge",
        "episode": "20_Memories/Episodes",
        "inference": "20_Memories/Knowledge",
    }

    def __init__(self, config: ObsidianConfig, state_dir: Path):
        self.config = config
        self.vault = config.vault_path.expanduser().resolve()
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.db_path = self.state_dir / "index.sqlite3"
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------

    def initialize(self) -> None:
        self.vault.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.vault.is_symlink():
            raise RuntimeError("Vault root must not be a symbolic link")
        for relative in self.REQUIRED_DIRS:
            target = self.vault / relative
            if target.exists() and target.is_symlink():
                raise RuntimeError(f"Vault directory must not be a symbolic link: {target}")
            target.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.db_path, timeout=10.0, check_same_thread=False
        )
        if os.name != "nt":
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._create_schema()
        self._ensure_user_entity()
        self._ensure_project_entity()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Obsidian store is not initialized")
        return self._conn

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                turn_number INTEGER NOT NULL DEFAULT 0,
                captured_at TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                user_content TEXT NOT NULL DEFAULT '',
                assistant_content TEXT NOT NULL DEFAULT '',
                sensitivity TEXT NOT NULL DEFAULT 'private'
            );

            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                normalized_key TEXT NOT NULL UNIQUE,
                path TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT NOT NULL,
                statement TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                predicate TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                confidence REAL NOT NULL,
                importance REAL NOT NULL,
                stability TEXT NOT NULL,
                sensitivity TEXT NOT NULL,
                evidence_count INTEGER NOT NULL DEFAULT 1,
                source_ids TEXT NOT NULL,
                superseded_by TEXT,
                related_ids TEXT NOT NULL DEFAULT '[]',
                promotion_target TEXT,
                promotion_score REAL NOT NULL DEFAULT 0,
                direct INTEGER NOT NULL DEFAULT 0,
                recall_count INTEGER NOT NULL DEFAULT 0,
                last_recalled_at TEXT
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                source_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(kind, source_id)
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                object_id TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                memory_id UNINDEXED,
                title,
                statement,
                kind,
                tokenize='unicode61'
            );
            """
        )
        self.conn.commit()

    # -- safe filesystem ---------------------------------------------------

    def _validate_target(self, path: Path) -> Path:
        path = path.absolute()
        try:
            path.parent.resolve().relative_to(self.vault)
        except ValueError as exc:
            raise RuntimeError(f"Refusing write outside Vault: {path}") from exc
        current = self.vault
        for part in path.relative_to(self.vault).parts[:-1]:
            current = current / part
            if current.exists() and current.is_symlink():
                raise RuntimeError(f"Refusing write through symbolic link: {current}")
        if path.exists() and path.is_symlink():
            raise RuntimeError(f"Refusing to replace symbolic link: {path}")
        return path

    def _atomic_write(self, path: Path, content: str) -> None:
        path = self._validate_target(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".hermes-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _relative(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.vault)).replace(os.sep, "/")

    # -- source capture ----------------------------------------------------

    def capture_turn(
        self,
        *,
        session_id: str,
        turn_number: int,
        user_content: str,
        assistant_content: str,
        platform: str,
        tool_names: Optional[List[str]] = None,
    ) -> str:
        digest = hashlib.sha256(
            (user_content + "\x00" + assistant_content).encode("utf-8", "replace")
        ).hexdigest()
        key = f"turn|{session_id}|{turn_number}|{digest}"
        source_id = "src-" + str(uuid.uuid5(_UUID_NAMESPACE, key))
        with self._lock:
            existing = self.conn.execute(
                "SELECT id FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
            if existing:
                return source_id

            captured_at = _now()
            local_now = datetime.now().astimezone()
            path = (
                self.vault
                / "10_Sources/Conversations"
                / f"{local_now.year:04d}"
                / f"{local_now.month:02d}"
                / f"{source_id}--conversation-turn-{turn_number:04d}.md"
            )
            metadata = {
                "schema": "hermes.source/v1",
                "id": source_id,
                "kind": "conversation_turn",
                "session_id": session_id,
                "turn_number": turn_number,
                "platform": platform,
                "captured_at": captured_at,
                "content_sha256": digest,
                "sensitivity": "private",
                "immutable": True,
            }
            trace = "\n".join(f"- `{name}`" for name in (tool_names or [])) or "None recorded."
            note = (
                _frontmatter(metadata)
                + f"\n# Conversation turn {turn_number}\n\n"
                + "## User\n\n"
                + (user_content or "")
                + "\n\n## Assistant\n\n"
                + (assistant_content or "")
                + "\n\n## Tool trace\n\n"
                + trace
                + "\n"
            )
            self._atomic_write(path, note)
            self.conn.execute(
                """
                INSERT INTO sources
                    (id, path, kind, session_id, turn_number, captured_at,
                     content_sha256, user_content, assistant_content, sensitivity)
                VALUES (?, ?, 'conversation_turn', ?, ?, ?, ?, ?, ?, 'private')
                """,
                (
                    source_id,
                    self._relative(path),
                    session_id,
                    turn_number,
                    captured_at,
                    digest,
                    user_content,
                    assistant_content,
                ),
            )
            self.conn.execute(
                """
                INSERT OR IGNORE INTO jobs
                    (kind, source_id, status, created_at, updated_at)
                VALUES ('extract', ?, 'pending', ?, ?)
                """,
                (source_id, _utc_now(), _utc_now()),
            )
            self.conn.commit()
            self._event("captured", source_id, f"turn={turn_number}")
        return source_id

    def capture_manual_source(
        self, content: str, *, kind: str, session_id: str = "manual"
    ) -> str:
        digest = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
        source_id = "src-" + str(
            uuid.uuid5(_UUID_NAMESPACE, f"manual|{kind}|{session_id}|{digest}")
        )
        with self._lock:
            if self.conn.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                return source_id
            path = self.vault / "10_Sources/Documents" / f"{source_id}--{_slug(kind)}.md"
            captured_at = _now()
            note = _frontmatter(
                {
                    "schema": "hermes.source/v1",
                    "id": source_id,
                    "kind": kind,
                    "session_id": session_id,
                    "captured_at": captured_at,
                    "content_sha256": digest,
                    "sensitivity": "private",
                    "immutable": True,
                }
            ) + f"\n# {_slug(kind).replace('-', ' ').title()}\n\n{content}\n"
            self._atomic_write(path, note)
            self.conn.execute(
                """
                INSERT INTO sources
                    (id, path, kind, session_id, captured_at, content_sha256,
                     user_content, sensitivity)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'private')
                """,
                (source_id, self._relative(path), kind, session_id, captured_at, digest, content),
            )
            self.conn.commit()
            self._event("captured", source_id, kind)
        return source_id

    # -- curation ----------------------------------------------------------

    @staticmethod
    def _promotion_score(candidate: MemoryCandidate, evidence_count: int) -> float:
        recurrence = 1.0 if candidate.direct else min(1.0, evidence_count / 3.0)
        freshness = 1.0
        recall_usefulness = 0.0
        sensitivity_penalty = 0.25 if candidate.sensitivity == "sensitive" else 0.0
        ephemerality_penalty = 0.25 if candidate.stability == "ephemeral" else 0.0
        score = (
            0.30 * candidate.importance
            + 0.25 * candidate.confidence
            + 0.20 * recurrence
            + 0.15 * recall_usefulness
            + 0.10 * freshness
            - sensitivity_penalty
            - ephemerality_penalty
        )
        return round(min(1.0, max(0.0, score)), 4)

    def curate_pending(self, max_jobs: int = 20) -> Dict[str, Any]:
        with self._lock:
            jobs = self.conn.execute(
                """
                SELECT id, source_id FROM jobs
                WHERE kind='extract' AND status IN ('pending', 'retry')
                ORDER BY id LIMIT ?
                """,
                (max_jobs,),
            ).fetchall()
        created_or_updated = 0
        processed = 0
        errors: List[str] = []
        for job in jobs:
            job_id = int(job["id"])
            source_id = str(job["source_id"])
            try:
                with self._lock:
                    source = self.conn.execute(
                        "SELECT user_content FROM sources WHERE id=?", (source_id,)
                    ).fetchone()
                if not source:
                    raise RuntimeError(f"missing source {source_id}")
                for candidate in extract_candidates(str(source["user_content"]), source_id):
                    self.upsert_candidate(candidate)
                    created_or_updated += 1
                with self._lock:
                    self.conn.execute(
                        "UPDATE jobs SET status='done', attempts=attempts+1, updated_at=? WHERE id=?",
                        (_utc_now(), job_id),
                    )
                    self.conn.commit()
                processed += 1
            except Exception as exc:
                errors.append(f"{source_id}: {exc}")
                with self._lock:
                    self.conn.execute(
                        """
                        UPDATE jobs SET status='retry', attempts=attempts+1,
                            last_error=?, updated_at=? WHERE id=?
                        """,
                        (str(exc)[:500], _utc_now(), job_id),
                    )
                    self.conn.commit()
        return {
            "processed_jobs": processed,
            "memories_changed": created_or_updated,
            "errors": errors,
        }

    def upsert_candidate(self, candidate: MemoryCandidate) -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM memories WHERE normalized_key=?",
                (candidate.normalized_key,),
            ).fetchone()
            now = _now()
            if row:
                memory_id = str(row["id"])
                sources = list(json.loads(row["source_ids"] or "[]"))
                if candidate.source_id not in sources:
                    sources.append(candidate.source_id)
                evidence_count = max(int(row["evidence_count"]), len(sources))
                status = str(row["status"])
                if status not in {"promoted", "superseded", "quarantined"} and candidate.direct:
                    status = "active"
                confidence = max(float(row["confidence"]), candidate.confidence)
                importance = max(float(row["importance"]), candidate.importance)
                promotion_score = self._promotion_score(candidate, evidence_count)
                path = self.vault / str(row["path"])
                created_at = str(row["created_at"])
            else:
                memory_id = "mem-" + str(
                    uuid.uuid5(_UUID_NAMESPACE, candidate.normalized_key)
                )
                sources = [candidate.source_id]
                evidence_count = 1
                status = "active" if candidate.direct else "candidate"
                confidence = candidate.confidence
                importance = candidate.importance
                promotion_score = self._promotion_score(candidate, evidence_count)
                directory = self.KIND_DIRS.get(candidate.kind, "20_Memories/Knowledge")
                path = self.vault / directory / f"{memory_id}--{_slug(candidate.title)}.md"
                created_at = now

            note = self._render_memory_note(
                memory_id=memory_id,
                candidate=candidate,
                status=status,
                created_at=created_at,
                updated_at=now,
                confidence=confidence,
                importance=importance,
                evidence_count=evidence_count,
                source_ids=sources,
                promotion_score=promotion_score,
            )
            self._atomic_write(path, note)
            if row:
                self.conn.execute(
                    """
                    UPDATE memories SET status=?, title=?, statement=?, value=?,
                        updated_at=?, confidence=?, importance=?, evidence_count=?,
                        source_ids=?, promotion_target=?, promotion_score=?, direct=?
                    WHERE id=?
                    """,
                    (
                        status,
                        candidate.title,
                        candidate.statement,
                        candidate.value,
                        now,
                        confidence,
                        importance,
                        evidence_count,
                        json.dumps(sources, ensure_ascii=False),
                        candidate.promotion_target,
                        promotion_score,
                        int(candidate.direct),
                        memory_id,
                    ),
                )
            else:
                self.conn.execute(
                    """
                    INSERT INTO memories
                        (id, normalized_key, path, kind, status, title, statement,
                         subject_id, predicate, value, created_at, updated_at,
                         confidence, importance, stability, sensitivity,
                         evidence_count, source_ids, related_ids, promotion_target,
                         promotion_score, direct)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        candidate.normalized_key,
                        self._relative(path),
                        candidate.kind,
                        status,
                        candidate.title,
                        candidate.statement,
                        candidate.subject_id,
                        candidate.predicate,
                        candidate.value,
                        created_at,
                        now,
                        confidence,
                        importance,
                        candidate.stability,
                        candidate.sensitivity,
                        evidence_count,
                        json.dumps(sources, ensure_ascii=False),
                        json.dumps([candidate.subject_id], ensure_ascii=False),
                        candidate.promotion_target,
                        promotion_score,
                        int(candidate.direct),
                    ),
                )
            self._index_memory(memory_id, candidate.title, candidate.statement, candidate.kind)
            self.conn.commit()
            self._event("extracted", memory_id, f"source={candidate.source_id}")
        return memory_id

    def _render_memory_note(
        self,
        *,
        memory_id: str,
        candidate: MemoryCandidate,
        status: str,
        created_at: str,
        updated_at: str,
        confidence: float,
        importance: float,
        evidence_count: int,
        source_ids: List[str],
        promotion_score: float,
    ) -> str:
        metadata = {
            "schema": "hermes.memory/v1",
            "id": memory_id,
            "kind": candidate.kind,
            "status": status,
            "title": candidate.title,
            "subject_id": candidate.subject_id,
            "predicate": candidate.predicate,
            "value": candidate.value,
            "created_at": created_at,
            "updated_at": updated_at,
            "confidence": round(confidence, 4),
            "importance": round(importance, 4),
            "stability": candidate.stability,
            "sensitivity": candidate.sensitivity,
            "evidence_count": evidence_count,
            "source_ids": source_ids,
            "supersedes": [],
            "superseded_by": None,
            "related_ids": [candidate.subject_id],
            "promotion": {
                "target": candidate.promotion_target,
                "score": promotion_score,
                "promoted_at": None,
            },
            "recall": {"count": 0, "last_at": None},
            "direct": candidate.direct,
            "extractor": "hermes-obsidian-rules/v0.1",
        }
        evidence = "\n".join(
            f"- {self._source_wikilink(source_id)}" for source_id in source_ids
        )
        relation = self._entity_wikilink(candidate.subject_id)
        return (
            _frontmatter(metadata)
            + f"\n# {candidate.title}\n\n"
            + _MANAGED_START
            + "\n## 기억\n\n"
            + candidate.statement
            + "\n\n## 근거\n\n"
            + evidence
            + "\n\n## 관계\n\n"
            + f"- {relation}\n"
            + _MANAGED_END
            + "\n\n## 사용자 메모\n\n"
        )

    def _index_memory(self, memory_id: str, title: str, statement: str, kind: str) -> None:
        self.conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
        self.conn.execute(
            "INSERT INTO memory_fts(memory_id, title, statement, kind) VALUES (?, ?, ?, ?)",
            (memory_id, title, statement, kind),
        )

    # -- retrieval ---------------------------------------------------------

    def search_memories(
        self, query: str, *, limit: int = 5, min_confidence: float = 0.55
    ) -> List[Dict[str, Any]]:
        tokens = [t for t in re.findall(r"[\w가-힣]{2,}", query or "", flags=re.UNICODE) if t]
        if not tokens:
            return []
        fts_query = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens[:12])
        with self._lock:
            try:
                rows = self.conn.execute(
                    """
                    SELECT m.*, bm25(memory_fts) AS lexical_rank
                    FROM memory_fts
                    JOIN memories m ON m.id = memory_fts.memory_id
                    WHERE memory_fts MATCH ?
                      AND m.status IN ('active', 'promoted')
                      AND m.confidence >= ?
                      AND m.sensitivity != 'secret'
                    ORDER BY lexical_rank ASC, m.importance DESC
                    LIMIT ?
                    """,
                    (fts_query, min_confidence, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if not rows:
                like = f"%{query.strip()}%"
                rows = self.conn.execute(
                    """
                    SELECT m.*, 0.0 AS lexical_rank FROM memories m
                    WHERE (m.statement LIKE ? OR m.title LIKE ?)
                      AND m.status IN ('active', 'promoted')
                      AND m.confidence >= ? AND m.sensitivity != 'secret'
                    ORDER BY m.importance DESC LIMIT ?
                    """,
                    (like, like, min_confidence, limit),
                ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE memories SET recall_count=recall_count+1, last_recalled_at=? WHERE id IN ({placeholders})",
                    (_now(), *ids),
                )
                self.conn.commit()
                for memory_id in ids:
                    query_hash = hashlib.sha256(query.encode("utf-8", "replace")).hexdigest()[:16]
                    self._event("recalled", memory_id, f"query_sha256={query_hash}")
            return [self._row_to_memory(row) for row in rows]

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            return self._row_to_memory(row) if row else None

    def list_promotable(self, target: str, threshold: float) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT * FROM memories
                WHERE promotion_target=? AND promotion_score>=?
                  AND status IN ('active', 'promoted')
                  AND sensitivity NOT IN ('secret', 'sensitive')
                  AND direct=1 AND superseded_by IS NULL
                ORDER BY promotion_score DESC, importance DESC, updated_at DESC
                """,
                (target, threshold),
            ).fetchall()
            return [self._row_to_memory(row) for row in rows]

    def mark_promotion_state(self, promoted_ids: Iterable[str]) -> None:
        wanted = set(promoted_ids)
        changed: List[tuple[str, str]] = []
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, status FROM memories WHERE promotion_target IS NOT NULL"
            ).fetchall()
            for row in rows:
                memory_id = str(row["id"])
                current = str(row["status"])
                if memory_id in wanted and current == "active":
                    self.conn.execute("UPDATE memories SET status='promoted' WHERE id=?", (memory_id,))
                    self._event("promoted", memory_id, "active cache")
                    changed.append((memory_id, "promoted"))
                elif memory_id not in wanted and current == "promoted":
                    self.conn.execute("UPDATE memories SET status='active' WHERE id=?", (memory_id,))
                    self._event("demoted", memory_id, "active cache")
                    changed.append((memory_id, "active"))
            self.conn.commit()
        for memory_id, status in changed:
            self._rewrite_status(memory_id, status)

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        for key in ("source_ids", "related_ids"):
            try:
                data[key] = json.loads(data.get(key) or "[]")
            except Exception:
                data[key] = []
        data["direct"] = bool(data.get("direct"))
        return data

    # -- corrections -------------------------------------------------------

    def remember_manual(
        self, content: str, *, target: str = "memory", session_id: str = "manual"
    ) -> str:
        if contains_secret(content):
            raise ValueError("Secret-like content cannot be promoted into long-term memory")
        if looks_like_injection(content):
            raise ValueError("Instruction-like untrusted content cannot be promoted into memory")
        source_id = self.capture_manual_source(
            content, kind="explicit_memory_request", session_id=session_id
        )
        kind = "profile" if target == "user" else "knowledge"
        candidate = MemoryCandidate(
            kind=kind,
            title=("사용자 기억" if target == "user" else "Hermes 기억") + f": {content[:70]}",
            statement=content[:500],
            subject_id="ent-user-primary" if target == "user" else "ent-project-current",
            predicate="explicit_memory",
            value=content[:300],
            confidence=0.99,
            importance=0.96,
            stability="stable",
            sensitivity="private",
            source_id=source_id,
            direct=True,
            promotion_target=target,
        )
        return self.upsert_candidate(candidate)

    def correct_memory(self, memory_id: str, new_statement: str) -> str:
        if contains_secret(new_statement) or looks_like_injection(new_statement):
            raise ValueError("Unsafe correction content cannot enter active memory")
        old = self.get_memory(memory_id)
        if not old:
            raise KeyError(memory_id)
        source_id = self.capture_manual_source(
            f"Correction for {memory_id}: {new_statement}", kind="memory_correction"
        )
        candidate = MemoryCandidate(
            kind=str(old["kind"]),
            title=f"정정됨: {new_statement[:80]}",
            statement=new_statement[:500],
            subject_id=str(old["subject_id"]),
            predicate=str(old["predicate"]),
            value=new_statement[:300],
            confidence=0.99,
            importance=max(0.9, float(old["importance"])),
            stability=str(old["stability"]),
            sensitivity=str(old["sensitivity"]),
            source_id=source_id,
            direct=True,
            promotion_target=old.get("promotion_target"),
        )
        new_id = self.upsert_candidate(candidate)
        with self._lock:
            self.conn.execute(
                "UPDATE memories SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
                (new_id, _now(), memory_id),
            )
            self.conn.commit()
            self._event("superseded", memory_id, f"by={new_id}")
        self._rewrite_status(memory_id, "superseded", superseded_by=new_id)
        return new_id

    def deactivate_memory(self, memory_id: str, reason: str = "logical forget") -> bool:
        with self._lock:
            row = self.conn.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
            if not row:
                return False
            self.conn.execute(
                "UPDATE memories SET status='inactive', updated_at=? WHERE id=?",
                (_now(), memory_id),
            )
            self.conn.commit()
            self._event("corrected", memory_id, reason[:200])
        self._rewrite_status(memory_id, "inactive")
        return True

    def deactivate_matching(self, text: str) -> int:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM memories WHERE statement LIKE ? AND status IN ('active','promoted')",
                (f"%{text}%",),
            ).fetchall()
        count = 0
        for row in rows:
            if self.deactivate_memory(str(row["id"]), "built-in memory removal"):
                count += 1
        return count

    def _rewrite_status(
        self, memory_id: str, status: str, *, superseded_by: str | None = None
    ) -> None:
        memory = self.get_memory(memory_id)
        if not memory:
            return
        path = self.vault / str(memory["path"])
        try:
            raw = path.read_text(encoding="utf-8")
            metadata, body = self._parse_frontmatter(raw)
            metadata["status"] = status
            metadata["updated_at"] = _now()
            if superseded_by:
                metadata["superseded_by"] = superseded_by
            self._atomic_write(path, _frontmatter(metadata) + body.lstrip("\n"))
        except Exception:
            return

    # -- rebuild, diagnostics, reflection ---------------------------------

    def reindex(self) -> Dict[str, Any]:
        parsed: List[Dict[str, Any]] = []
        errors: List[str] = []
        for path in sorted((self.vault / "20_Memories").rglob("*.md")):
            try:
                raw = path.read_text(encoding="utf-8")
                metadata, body = self._parse_frontmatter(raw)
                if metadata.get("schema") != "hermes.memory/v1":
                    continue
                statement = self._extract_managed_statement(body)
                if not statement:
                    raise ValueError("missing managed memory statement")
                metadata["statement"] = statement
                metadata["path"] = self._relative(path)
                if contains_secret(statement):
                    metadata["sensitivity"] = "secret"
                    metadata["status"] = "quarantined"
                elif looks_like_injection(statement):
                    metadata["status"] = "quarantined"
                parsed.append(metadata)
            except Exception as exc:
                errors.append(f"{path}: {exc}")
        if errors:
            return {"success": False, "indexed": 0, "errors": errors}

        with self._lock:
            memory_ids = []
            for item in parsed:
                memory_id = str(item["id"])
                memory_ids.append(memory_id)
                promotion = item.get("promotion") if isinstance(item.get("promotion"), dict) else {}
                recall = item.get("recall") if isinstance(item.get("recall"), dict) else {}
                normalized_raw = "|".join(
                    [
                        str(item.get("kind", "knowledge")),
                        str(item.get("subject_id", "")),
                        str(item.get("predicate", "")),
                        re.sub(r"\s+", " ", str(item.get("value", "")).lower()),
                    ]
                )
                normalized_key = hashlib.sha256(normalized_raw.encode("utf-8")).hexdigest()
                self.conn.execute(
                    """
                    INSERT INTO memories
                        (id, normalized_key, path, kind, status, title, statement,
                         subject_id, predicate, value, created_at, updated_at,
                         confidence, importance, stability, sensitivity,
                         evidence_count, source_ids, superseded_by, related_ids,
                         promotion_target, promotion_score, direct, recall_count,
                         last_recalled_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        path=excluded.path, kind=excluded.kind, status=excluded.status,
                        title=excluded.title, statement=excluded.statement,
                        subject_id=excluded.subject_id, predicate=excluded.predicate,
                        value=excluded.value, updated_at=excluded.updated_at,
                        confidence=excluded.confidence, importance=excluded.importance,
                        stability=excluded.stability, sensitivity=excluded.sensitivity,
                        evidence_count=excluded.evidence_count,
                        source_ids=excluded.source_ids, superseded_by=excluded.superseded_by,
                        related_ids=excluded.related_ids,
                        promotion_target=excluded.promotion_target,
                        promotion_score=excluded.promotion_score, direct=excluded.direct
                    """,
                    (
                        memory_id,
                        normalized_key,
                        item["path"],
                        item.get("kind", "knowledge"),
                        item.get("status", "candidate"),
                        item.get("title", memory_id),
                        item["statement"],
                        item.get("subject_id", ""),
                        item.get("predicate", ""),
                        str(item.get("value", "")),
                        str(item.get("created_at", _now())),
                        str(item.get("updated_at", _now())),
                        float(item.get("confidence", 0.5)),
                        float(item.get("importance", 0.5)),
                        item.get("stability", "situational"),
                        item.get("sensitivity", "private"),
                        int(item.get("evidence_count", 1)),
                        json.dumps(item.get("source_ids") or [], ensure_ascii=False),
                        item.get("superseded_by"),
                        json.dumps(item.get("related_ids") or [], ensure_ascii=False),
                        promotion.get("target"),
                        float(promotion.get("score", 0.0)),
                        int(bool(item.get("direct", False))),
                        int(recall.get("count", 0)),
                        recall.get("last_at"),
                    ),
                )
                self._index_memory(
                    memory_id,
                    str(item.get("title", memory_id)),
                    item["statement"],
                    str(item.get("kind", "knowledge")),
                )
            if memory_ids:
                placeholders = ",".join("?" for _ in memory_ids)
                stale = self.conn.execute(
                    f"SELECT id FROM memories WHERE id NOT IN ({placeholders})",
                    tuple(memory_ids),
                ).fetchall()
            else:
                stale = self.conn.execute("SELECT id FROM memories").fetchall()
            for row in stale:
                self.conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (row["id"],))
                self.conn.execute("DELETE FROM memories WHERE id=?", (row["id"],))
            self.conn.commit()
        return {"success": True, "indexed": len(parsed), "errors": []}

    @staticmethod
    def _parse_frontmatter(raw: str) -> tuple[Dict[str, Any], str]:
        if not raw.startswith("---\n"):
            raise ValueError("missing YAML frontmatter")
        end = raw.find("\n---\n", 4)
        if end < 0:
            raise ValueError("unterminated YAML frontmatter")
        metadata = yaml.safe_load(raw[4:end]) or {}
        if not isinstance(metadata, dict):
            raise ValueError("frontmatter is not an object")
        return metadata, raw[end + 5 :]

    @staticmethod
    def _extract_managed_statement(body: str) -> str:
        if _MANAGED_START not in body or _MANAGED_END not in body:
            return ""
        managed = body.split(_MANAGED_START, 1)[1].split(_MANAGED_END, 1)[0]
        match = re.search(r"## 기억\s*\n+(.*?)(?=\n## |\Z)", managed, re.S)
        return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""

    def reflect(self, period: str = "daily") -> Path:
        period = "weekly" if period == "weekly" else "daily"
        now = datetime.now().astimezone()
        key = now.strftime("%G-W%V") if period == "weekly" else now.strftime("%Y-%m-%d")
        reflection_id = "ref-" + str(uuid.uuid5(_UUID_NAMESPACE, f"{period}|{key}"))
        folder = "Weekly" if period == "weekly" else "Daily"
        path = self.vault / "40_Reflections" / folder / f"{reflection_id}--{key}.md"
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, statement, confidence, importance FROM memories
                WHERE status IN ('active','promoted')
                ORDER BY importance DESC, updated_at DESC LIMIT 12
                """
            ).fetchall()
        evidence = "\n".join(
            f"- [[{row['id']}]] — {row['statement']}" for row in rows
        ) or "- 아직 활성 기억이 없다."
        metadata = {
            "schema": "hermes.reflection/v1",
            "id": reflection_id,
            "period": period,
            "status": "candidate",
            "title": f"{period.title()} reflection {key}",
            "created_at": _now(),
            "updated_at": _now(),
            "confidence": 0.5,
            "source_ids": [str(row["id"]) for row in rows],
            "promotion_eligible": False,
        }
        note = (
            _frontmatter(metadata)
            + f"\n# {period.title()} reflection — {key}\n\n"
            + _MANAGED_START
            + "\n## Observation\n\n"
            + "현재 활성 기억의 우선순위 스냅샷이다. 새로운 사실이 아니라 검토용 성찰 후보다.\n\n"
            + "## Evidence\n\n"
            + evidence
            + "\n\n## Possible implications\n\n"
            + "반복성·모순·프로젝트 변화를 다음 큐레이션에서 검토한다.\n"
            + _MANAGED_END
            + "\n\n## 사용자 메모\n\n"
        )
        with self._lock:
            self._atomic_write(path, note)
            self._event("reflected", reflection_id, period)
        return path

    def status(self) -> Dict[str, Any]:
        with self._lock:
            counts = {
                "sources": self.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
                "memories": self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
                "pending_jobs": self.conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','retry')"
                ).fetchone()[0],
                "active_memories": self.conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE status IN ('active','promoted')"
                ).fetchone()[0],
            }
        return {
            "vault": str(self.vault),
            "state_dir": str(self.state_dir),
            "index": str(self.db_path),
            **counts,
        }

    def doctor(self) -> Dict[str, Any]:
        checks = {
            "vault_exists": self.vault.is_dir(),
            "obsidian_opened": (self.vault / ".obsidian").is_dir(),
            "index_exists": self.db_path.is_file(),
            "vault_not_symlink": not self.vault.is_symlink(),
            "home_note_exists": (self.vault / "00 Home.md").is_file(),
        }
        try:
            with self._lock:
                checks["sqlite_integrity"] = (
                    self.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                )
        except Exception:
            checks["sqlite_integrity"] = False
        return {"ok": all(checks.values()), "checks": checks, **self.status()}

    # -- entities and audit ------------------------------------------------

    def _ensure_user_entity(self) -> None:
        path = self.vault / "30_Entities/People/ent-user-primary--user.md"
        if path.exists():
            return
        metadata = {
            "schema": "hermes.entity/v1",
            "id": "ent-user-primary",
            "entity_type": "person",
            "canonical_name": "User",
            "aliases": [],
            "status": "active",
            "created_at": _now(),
            "updated_at": _now(),
            "source_ids": [],
            "related_ids": [],
        }
        note = (
            _frontmatter(metadata)
            + "\n# User\n\n"
            + _MANAGED_START
            + "\n## Summary\n\nPrimary user of this Hermes profile.\n\n"
            + "## Relationships\n\n- None yet.\n"
            + _MANAGED_END
            + "\n\n## 사용자 메모\n\n"
        )
        self._atomic_write(path, note)

    def _ensure_project_entity(self) -> None:
        path = self.vault / "30_Entities/Projects/ent-project-current--current-project.md"
        if path.exists():
            return
        metadata = {
            "schema": "hermes.entity/v1",
            "id": "ent-project-current",
            "entity_type": "project",
            "canonical_name": "Current Project",
            "aliases": [],
            "status": "active",
            "created_at": _now(),
            "updated_at": _now(),
            "source_ids": [],
            "related_ids": [],
        }
        note = (
            _frontmatter(metadata)
            + "\n# Current Project\n\n"
            + _MANAGED_START
            + "\n## Summary\n\nProject-scoped memories without a more specific entity are linked here.\n\n"
            + "## Relationships\n\n- None yet.\n"
            + _MANAGED_END
            + "\n\n## 사용자 메모\n\n"
        )
        self._atomic_write(path, note)

    def _source_wikilink(self, source_id: str) -> str:
        row = self.conn.execute("SELECT path FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            return f"`{source_id}`"
        link_path = str(Path(str(row["path"])).with_suffix(""))
        return f"[[{link_path}|{source_id}]]"

    @staticmethod
    def _entity_wikilink(entity_id: str) -> str:
        if entity_id == "ent-user-primary":
            return "[[30_Entities/People/ent-user-primary--user|ent-user-primary]]"
        if entity_id == "ent-project-current":
            return "[[30_Entities/Projects/ent-project-current--current-project|ent-project-current]]"
        return f"`{entity_id}`"

    def _event(self, event: str, object_id: str, detail: str = "") -> None:
        created_at = _utc_now()
        self.conn.execute(
            "INSERT INTO events(event, object_id, detail, created_at) VALUES (?, ?, ?, ?)",
            (event, object_id, detail[:500], created_at),
        )
        self.conn.commit()
        audit_path = self.vault / "90_System/Audit" / f"{datetime.now().astimezone():%Y-%m}.md"
        if audit_path.exists():
            raw = audit_path.read_text(encoding="utf-8")
        else:
            raw = _frontmatter(
                {
                    "schema": "hermes.audit-log/v1",
                    "period": f"{datetime.now().astimezone():%Y-%m}",
                }
            ) + f"\n# Audit — {datetime.now().astimezone():%Y-%m}\n"
        line = f"\n- `{created_at}` · `{event}` · `{object_id}`\n"
        self._atomic_write(audit_path, raw.rstrip() + line)
