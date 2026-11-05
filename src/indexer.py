"""Unified Indexer for both Atomic Memories and General Knowledge Markdown Documents."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import RebuildConfig
from .db import DatabaseManager
from .protection import is_user_protected, parse_frontmatter


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """Rough estimation: Korean ~1.5 tokens/word, English/Code ~1 token/4 chars."""
    if not text:
        return 0
    words = len(text.split())
    chars = len(text)
    return max(1, int((words * 1.3) + (chars / 8)))


def extract_tags(text: str, meta: Optional[Dict[str, Any]] = None) -> List[str]:
    tags: set[str] = set()
    if meta and "tags" in meta:
        raw = meta["tags"]
        if isinstance(raw, list):
            tags.update(str(t).lstrip("#").strip() for t in raw if t)
        elif isinstance(raw, str):
            tags.update(t.lstrip("#").strip() for t in re.split(r"[,\s]+", raw) if t)

    for m in re.finditer(r"(?:^|\s)#([a-zA-Z0-9_\-\uac00-\ud7a3]+)", text):
        tags.add(m.group(1))

    return sorted(t for t in tags if t)


def split_heading_blocks(text: str, max_chars: int = 1500) -> List[Dict[str, Any]]:
    """Splits markdown into logical chunks based on headings (#, ##, ###)."""
    lines = text.splitlines(keepends=True)
    chunks: List[Dict[str, Any]] = []

    current_heading = "Top"
    current_level = 1
    current_lines: List[str] = []

    def flush_chunk():
        nonlocal current_lines
        content = "".join(current_lines).strip()
        if content:
            chunks.append({
                "heading": current_heading,
                "level": current_level,
                "content": content,
            })
        current_lines = []

    for line in lines:
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        if heading_match:
            flush_chunk()
            current_level = len(heading_match.group(1))
            current_heading = heading_match.group(2).strip()
            current_lines.append(line)
        else:
            current_lines.append(line)
            if sum(len(l) for l in current_lines) >= max_chars and line.strip() == "":
                flush_chunk()

    flush_chunk()
    return chunks


class UnifiedIndexer:
    """Indexes both atomic memories and knowledge chunks into SQLite FTS5."""

    def __init__(self, config: RebuildConfig, db_manager: DatabaseManager) -> None:
        self.config = config
        self.db = db_manager

    def index_all(self, target_profile: Optional[str] = None) -> Dict[str, Any]:
        """Performs a full index pass over atomic memories and markdown documents."""
        profile = target_profile or self.config.profile
        now_str = utc_now_iso()
        stats = {
            "profile": profile,
            "memories_indexed": 0,
            "chunks_indexed": 0,
            "files_scanned": 0,
            "errors": [],
        }

        with self.db.get_connection() as con:
            # 1. Index Atomic Memories (20_Memories/{profile} and 20_Memories/shared)
            memory_dirs = [self.config.profile_memory_dir(profile)]
            if self.config.retrieval.include_shared:
                memory_dirs.extend(self.config.shared_memory_dirs())

            seen_memory_ids = set()
            for mem_dir in memory_dirs:
                if not mem_dir.exists():
                    continue
                for path in mem_dir.rglob("*.md"):
                    if path.name.startswith("."):
                        continue
                    stats["files_scanned"] += 1
                    try:
                        mem_id = self._index_memory_file(con, path, profile, now_str)
                        if mem_id:
                            seen_memory_ids.add(mem_id)
                            stats["memories_indexed"] += 1
                    except Exception as e:
                        stats["errors"].append(f"Memory {path}: {str(e)}")

            # 2. Index General Knowledge Chunks (excluding 20_Memories and system hidden folders)
            seen_chunk_hashes = set()
            vault = self.config.vault_path
            for path in vault.rglob("*.md"):
                if path.name.startswith("."):
                    continue
                rel_parts = path.relative_to(vault).parts
                if "20_Memories" in rel_parts or "90_System" in rel_parts:
                    continue  # skip atomic memories and raw templates

                stats["files_scanned"] += 1
                try:
                    hashes = self._index_document_chunks(con, path, profile, now_str)
                    seen_chunk_hashes.update(hashes)
                    stats["chunks_indexed"] += len(hashes)
                except Exception as e:
                    stats["errors"].append(f"Chunk {path}: {str(e)}")

            con.commit()

        return stats

    def _index_memory_file(
        self, con: Any, path: Path, default_profile: str, now_str: str
    ) -> Optional[str]:
        raw_text = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(raw_text)

        mem_id = meta.get("id") or path.stem
        profile = meta.get("profile", default_profile)
        kind = meta.get("kind", "fact")
        status = meta.get("status", "active")
        title = meta.get("title") or path.stem
        statement = meta.get("statement") or body.strip()
        confidence = float(meta.get("confidence", 1.0))
        importance = float(meta.get("importance", 1.0))
        user_locked = 1 if is_user_protected(meta, raw_text) else 0
        superseded_by = meta.get("superseded_by")
        source_ids = json.dumps(meta.get("source_ids", []), ensure_ascii=False)
        normalized_key = meta.get("normalized_key") or sha256_text(f"{kind}|{statement}")

        rel_path = str(path.relative_to(self.config.vault_path))

        con.execute(
            """
            INSERT INTO memories (
                id, profile, normalized_key, path, kind, status, title, statement,
                subject_id, predicate, value, created_at, updated_at, confidence,
                importance, stability, sensitivity, source_ids, superseded_by,
                user_locked, recall_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                profile=excluded.profile,
                normalized_key=excluded.normalized_key,
                path=excluded.path,
                kind=excluded.kind,
                status=excluded.status,
                title=excluded.title,
                statement=excluded.statement,
                updated_at=excluded.updated_at,
                confidence=excluded.confidence,
                importance=excluded.importance,
                superseded_by=excluded.superseded_by,
                user_locked=excluded.user_locked
            """,
            (
                mem_id, profile, normalized_key, rel_path, kind, status, title, statement,
                meta.get("subject_id", ""), meta.get("predicate", ""), meta.get("value", ""),
                meta.get("created_at", now_str), now_str, confidence,
                importance, meta.get("stability", "stable"), meta.get("sensitivity", "private"),
                source_ids, superseded_by, user_locked,
            ),
        )
        return mem_id

    def _index_document_chunks(
        self, con: Any, path: Path, profile: str, now_str: str
    ) -> List[str]:
        raw_text = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(raw_text)
        tags = ",".join(extract_tags(raw_text, meta))
        rel_path = str(path.relative_to(self.config.vault_path))

        blocks = split_heading_blocks(body)
        hashes = []

        for b in blocks:
            content = b["content"]
            chunk_hash = sha256_text(f"{rel_path}|{b['heading']}|{content}")
            hashes.append(chunk_hash)
            tokens_est = estimate_tokens(content)

            con.execute(
                """
                INSERT INTO chunks (
                    profile, doc_path, heading, level, content, tags,
                    tokens_est, hash, importance, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?)
                ON CONFLICT(hash) DO UPDATE SET
                    heading=excluded.heading,
                    level=excluded.level,
                    content=excluded.content,
                    tags=excluded.tags,
                    tokens_est=excluded.tokens_est,
                    updated_at=excluded.updated_at
                """,
                (
                    profile, rel_path, b["heading"], b["level"], content, tags,
                    tokens_est, chunk_hash, now_str, now_str,
                ),
            )

        return hashes
