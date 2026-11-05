"""Hybrid Context Retriever combining high-priority constraints, atomic memories, and document chunks."""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional

from .config import RebuildConfig
from .db import DatabaseManager
from .indexer import estimate_tokens


def escape_fts_query(query: str) -> str:
    """Escapes punctuation and operators for SQLite FTS5 syntax."""
    cleaned = re.sub(r'[^\w\s\uac00-\ud7a3]', " ", query)
    tokens = [t.strip() for t in cleaned.split() if t.strip()]
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)


class HybridRetriever:
    """Retrieves and packs relevant context within strict token budgets."""

    def __init__(self, config: RebuildConfig, db_manager: DatabaseManager) -> None:
        self.config = config
        self.db = db_manager

    def retrieve(
        self,
        query: str,
        profile: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        target_profile = profile or self.config.profile
        token_budget = max_tokens or self.config.retrieval.max_tokens
        shared_profiles = self.config.shared_profiles if self.config.retrieval.include_shared else []
        allowed_profiles = tuple([target_profile] + shared_profiles)

        # 1. High-Priority Constraints & Active Preferences (Must-Have context)
        constraints = self._get_high_priority_rules(allowed_profiles)
        
        # 2. Atomic Memories via FTS5
        memories = self._search_memories(query, allowed_profiles, limit=5)
        
        # 3. Knowledge Document Chunks via FTS5
        chunks = self._search_chunks(query, allowed_profiles, limit=3)

        # 4. Pack into formatted context packet obeying token budget
        context_packet = self._pack_context(
            constraints=constraints,
            memories=memories,
            chunks=chunks,
            token_budget=token_budget,
        )

        return {
            "query": query,
            "profile": target_profile,
            "tokens_est": estimate_tokens(context_packet),
            "constraints_count": len(constraints),
            "memories_count": len(memories),
            "chunks_count": len(chunks),
            "formatted_context": context_packet,
        }

    def _get_high_priority_rules(self, allowed_profiles: tuple) -> List[Dict[str, Any]]:
        """Fetch active constraints and top preferences that guide the agent's behavior."""
        placeholders = ",".join("?" for _ in allowed_profiles)
        with self.db.get_connection() as con:
            cur = con.cursor()
            cur.execute(
                f"""
                SELECT id, kind, title, statement, importance, confidence
                FROM memories
                WHERE status = 'active'
                  AND kind IN ('constraint', 'preference')
                  AND profile IN ({placeholders})
                ORDER BY (kind = 'constraint') DESC, importance DESC, confidence DESC
                LIMIT 6
                """,
                allowed_profiles,
            )
            return [dict(r) for r in cur.fetchall()]

    def _search_memories(
        self, query: str, allowed_profiles: tuple, limit: int = 5
    ) -> List[Dict[str, Any]]:
        fts_query = escape_fts_query(query)
        if fts_query == '""':
            return []

        placeholders = ",".join("?" for _ in allowed_profiles)
        with self.db.get_connection() as con:
            cur = con.cursor()
            try:
                cur.execute(
                    f"""
                    SELECT m.id, m.kind, m.title, m.statement, m.importance, m.confidence, m.profile,
                           bm25(memory_fts) as rank
                    FROM memory_fts f
                    JOIN memories m ON m.id = f.memory_id
                    WHERE memory_fts MATCH ?
                      AND m.status = 'active'
                      AND m.profile IN ({placeholders})
                    ORDER BY rank ASC
                    LIMIT ?
                    """,
                    (fts_query, *allowed_profiles, limit),
                )
                rows = cur.fetchall()
            except sqlite3.OperationalError:
                rows = []

            # Update recall count for retrieved memories
            if rows:
                ids = [r["id"] for r in rows]
                id_placeholders = ",".join("?" for _ in ids)
                con.execute(
                    f"""
                    UPDATE memories
                    SET recall_count = recall_count + 1, last_recalled_at = datetime('now')
                    WHERE id IN ({id_placeholders})
                    """,
                    ids,
                )
                con.commit()

            return [dict(r) for r in rows]

    def _search_chunks(
        self, query: str, allowed_profiles: tuple, limit: int = 3
    ) -> List[Dict[str, Any]]:
        fts_query = escape_fts_query(query)
        if fts_query == '""':
            return []

        placeholders = ",".join("?" for _ in allowed_profiles)
        with self.db.get_connection() as con:
            cur = con.cursor()
            try:
                cur.execute(
                    f"""
                    SELECT c.id, c.doc_path, c.heading, c.content, c.tags,
                           bm25(chunks_fts) as rank
                    FROM chunks_fts f
                    JOIN chunks c ON c.id = f.chunk_id
                    WHERE chunks_fts MATCH ?
                      AND c.profile IN ({placeholders})
                    ORDER BY rank ASC
                    LIMIT ?
                    """,
                    (fts_query, *allowed_profiles, limit),
                )
                return [dict(r) for r in cur.fetchall()]
            except sqlite3.OperationalError:
                return []

    def _pack_context(
        self,
        constraints: List[Dict[str, Any]],
        memories: List[Dict[str, Any]],
        chunks: List[Dict[str, Any]],
        token_budget: int,
    ) -> str:
        parts: List[str] = []
        used_tokens = 0

        # Part 1: Rules & Constraints
        rule_lines = []
        for r in constraints:
            prefix = "규칙" if r["kind"] == "constraint" else "선호"
            line = f"- [{prefix}] {r['statement']}"
            t = estimate_tokens(line)
            if used_tokens + t <= token_budget * 0.35:
                rule_lines.append(line)
                used_tokens += t

        if rule_lines:
            parts.append("### [핵심 가이드 및 제약]\n" + "\n".join(rule_lines))

        # Part 2: Related Atomic Memories (excluding duplicates already in rules)
        seen_statements = {r["statement"] for r in constraints}
        mem_lines = []
        for m in memories:
            if m["statement"] in seen_statements:
                continue
            line = f"- [{m['kind']}] {m['statement']}"
            t = estimate_tokens(line)
            if used_tokens + t <= token_budget * 0.70:
                mem_lines.append(line)
                used_tokens += t
                seen_statements.add(m["statement"])

        if mem_lines:
            parts.append("### [관련 기억]\n" + "\n".join(mem_lines))

        # Part 3: Knowledge Document Chunks
        chunk_blocks = []
        for c in chunks:
            block = f"#### [{c['doc_path']} > {c['heading']}]\n{c['content'].strip()}"
            t = estimate_tokens(block)
            if used_tokens + t <= token_budget:
                chunk_blocks.append(block)
                used_tokens += t

        if chunk_blocks:
            parts.append("### [관련 문서 발췌]\n" + "\n\n".join(chunk_blocks))

        return "\n\n".join(parts)
