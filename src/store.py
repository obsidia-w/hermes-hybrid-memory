"""Markdown Store and Lifecycle Manager for Atomic Memories."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import RebuildConfig
from .db import DatabaseManager
from .protection import (
    dump_frontmatter,
    extract_user_custom_sections,
    is_user_protected,
    mark_superseded,
    parse_frontmatter,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^\w\uac00-\ud7a3]+", "-", text).strip("-")
    return cleaned[:limit] or "memory"


class MemoryStore:
    """Manages file storage, supersede tracking, and user protection in the Obsidian Vault."""

    def __init__(self, config: RebuildConfig, db_manager: DatabaseManager) -> None:
        self.config = config
        self.db = db_manager

    def atomic_write(self, path: Path, content: str) -> None:
        """Write content to a file atomically via temporary file rename."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile("w", dir=str(path.parent), delete=False, encoding="utf-8")
        try:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, path)
        except Exception:
            if os.path.exists(tmp.name):
                os.remove(tmp.name)
            raise

    def remember(
        self,
        statement: str,
        kind: str = "fact",
        title: Optional[str] = None,
        profile: Optional[str] = None,
        confidence: float = 1.0,
        importance: float = 1.0,
        source_id: Optional[str] = None,
        user_locked: bool = False,
    ) -> str:
        """Create a new atomic memory note safely."""
        target_profile = profile or self.config.profile
        now_str = utc_now_iso()
        mem_id = f"mem-{uuid.uuid4().hex[:10]}"
        note_title = title or f"[{kind.upper()}] {statement[:30]}"
        filename = f"{slugify(note_title)}_{mem_id}.md"

        target_dir = self.config.profile_memory_dir(target_profile)
        file_path = target_dir / filename

        meta: Dict[str, Any] = {
            "id": mem_id,
            "profile": target_profile,
            "kind": kind,
            "status": "active",
            "title": note_title,
            "statement": statement,
            "confidence": confidence,
            "importance": importance,
            "user_locked": user_locked,
            "created_at": now_str,
            "updated_at": now_str,
            "source_ids": [source_id] if source_id else [],
        }

        body = f"# {note_title}\n\n{statement}\n"
        content = dump_frontmatter(meta, body)
        self.atomic_write(file_path, content)

        # Record in DB
        with self.db.get_connection() as con:
            rel_path = str(file_path.relative_to(self.config.vault_path))
            con.execute(
                """
                INSERT INTO memories (
                    id, profile, normalized_key, path, kind, status, title, statement,
                    created_at, updated_at, confidence, importance, source_ids, user_locked
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mem_id, target_profile, mem_id, rel_path, kind, note_title, statement,
                    now_str, now_str, confidence, importance,
                    json.dumps(meta["source_ids"], ensure_ascii=False), 1 if user_locked else 0,
                ),
            )
            con.commit()

        return mem_id

    def correct(
        self,
        old_memory_id: str,
        new_statement: str,
        reason: str = "User correction",
        profile: Optional[str] = None,
    ) -> str:
        """Supersede an existing memory without destructive overwrite."""
        target_profile = profile or self.config.profile

        with self.db.get_connection() as con:
            cur = con.cursor()
            cur.execute("SELECT path, kind, title FROM memories WHERE id = ?", (old_memory_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Memory with id '{old_memory_id}' not found.")

            old_rel_path = row["path"]
            old_kind = row["kind"]
            old_title = row["title"]

        old_file_path = self.config.vault_path / old_rel_path
        if not old_file_path.exists():
            raise FileNotFoundError(f"Underlying file missing: {old_file_path}")

        old_content = old_file_path.read_text(encoding="utf-8")
        old_meta, old_body = parse_frontmatter(old_content)

        # Check if protected by user
        if is_user_protected(old_meta, old_content):
            raise PermissionError(f"Memory note '{old_memory_id}' is protected by user and cannot be altered.")

        # Extract any user-written custom notes to preserve in the new note
        custom_user_notes = extract_user_custom_sections(old_body)

        # 1. Create the new superseded memory
        new_mem_id = self.remember(
            statement=new_statement,
            kind=old_kind,
            title=f"정정: {old_title}",
            profile=target_profile,
            importance=1.0,
            confidence=1.0,
        )

        # If there were user custom sections, append them to the new file
        if custom_user_notes:
            with self.db.get_connection() as con:
                cur = con.cursor()
                cur.execute("SELECT path FROM memories WHERE id = ?", (new_mem_id,))
                new_row = cur.fetchone()
                if new_row:
                    new_file_path = self.config.vault_path / new_row["path"]
                    new_content = new_file_path.read_text(encoding="utf-8")
                    new_content += f"\n\n{custom_user_notes}\n"
                    self.atomic_write(new_file_path, new_content)

        # 2. Mark old note as superseded (immutable update)
        now_str = utc_now_iso()
        superseded_content = mark_superseded(old_content, new_mem_id, reason=reason, timestamp=now_str)
        self.atomic_write(old_file_path, superseded_content)

        with self.db.get_connection() as con:
            con.execute(
                """
                UPDATE memories
                SET status = 'superseded', superseded_by = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_mem_id, now_str, old_memory_id),
            )
            con.commit()

        return new_mem_id
