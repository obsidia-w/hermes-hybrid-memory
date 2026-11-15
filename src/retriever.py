"""Hybrid Context Retriever combining high-priority constraints, atomic memories, and document chunks."""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional

from .config import RebuildConfig
from .db import DatabaseManager
from .indexer import estimate_tokens

KOREAN_PARTICLES = (
    "에서는", "에서도", "으로는", "으로도", "에게는", "에게도", "한테는", "한테도",
    "에서", "에게", "한테", "으로", "부터", "까지", "처럼", "보다", "이나", "이랑", "하고",
    "은", "는", "이", "가", "을", "를", "의", "에", "로", "와", "과", "도", "만", "랑", "나",
)


def extract_search_tokens(query: str) -> List[str]:
    """Extract normalized search tokens while preserving input order."""
    cleaned = re.sub(r"[^\w\s\uac00-\ud7a3]", " ", query or "")
    seen: set[str] = set()
    tokens: List[str] = []
    for raw in cleaned.split():
        tok = raw.strip()
        if tok and tok.lower() not in seen:
            seen.add(tok.lower())
            tokens.append(tok)
    return tokens


def strip_korean_particle(token: str) -> Optional[str]:
    """Strip a trailing Korean particle when the remaining stem has length >= 2."""
    for particle in KOREAN_PARTICLES:
        if token.endswith(particle) and len(token) - len(particle) >= 2:
            return token[: -len(particle)]
    return None


def expand_search_terms(query: str) -> List[str]:
    """Expand tokens with particle-stripped stems for recall."""
    seen: set[str] = set()
    terms: List[str] = []
    for tok in extract_search_tokens(query):
        for candidate in (tok, strip_korean_particle(tok)):
            if candidate and candidate.lower() not in seen:
                seen.add(candidate.lower())
                terms.append(candidate)
    return terms


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def escape_fts_query(query: str) -> str:
    """Build an FTS5 query with exact, prefix, and Korean particle-stripped terms."""
    terms = expand_search_terms(query)
    if not terms:
        return '""'
    clauses = []
    for t in terms:
        escaped = t.replace('"', '""')
        clauses.append(f'"{escaped}" OR "{escaped}"*')
    return " OR ".join(clauses)


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

        memory_limit = max(1, self.config.retrieval.max_results)
        chunk_limit = max(1, min(3, memory_limit))

        # 2. Atomic Memories via FTS5 + substring fallback
        memories = self._search_memories(query, allowed_profiles, limit=memory_limit)

        # 3. Knowledge Document Chunks via FTS5 + substring fallback
        chunks = self._search_chunks(query, allowed_profiles, limit=chunk_limit)

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
                results = [dict(r) for r in cur.fetchall()]
            except sqlite3.OperationalError:
                results = []

            if len(results) < limit:
                seen_ids = {r["id"] for r in results}
                fallback = self._like_fallback_query(
                    con=con,
                    query=query,
                    table="memories",
                    select_cols="id, kind, title, statement, importance, confidence, profile",
                    search_cols=("title", "statement", "kind"),
                    allowed_profiles=allowed_profiles,
                    exclude_ids=seen_ids,
                    limit=limit - len(results),
                    extra_where="status = 'active'",
                    has_confidence=True,
                )
                results.extend(fallback)

            # Update recall count for retrieved memories
            if results:
                ids = [r["id"] for r in results]
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

            return results

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
                    SELECT c.id, c.doc_path, c.heading, c.content, c.tags, c.importance,
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
                results = [dict(r) for r in cur.fetchall()]
            except sqlite3.OperationalError:
                results = []

            if len(results) < limit:
                seen_ids = {int(r["id"]) for r in results}
                fallback = self._like_fallback_query(
                    con=con,
                    query=query,
                    table="chunks",
                    select_cols="id, doc_path, heading, content, tags, importance",
                    search_cols=("heading", "content", "tags"),
                    allowed_profiles=allowed_profiles,
                    exclude_ids=seen_ids,
                    limit=limit - len(results),
                )
                results.extend(fallback)

            return results

    def _like_fallback_query(
        self,
        con: sqlite3.Connection,
        query: str,
        table: str,
        select_cols: str,
        search_cols: tuple[str, ...],
        allowed_profiles: tuple,
        exclude_ids: set[Any],
        limit: int,
        extra_where: str = "",
        has_confidence: bool = False,
    ) -> List[Dict[str, Any]]:
        tokens = extract_search_tokens(query)
        terms = expand_search_terms(query)[:20]
        if not terms or limit <= 0:
            return []

        profile_placeholders = ",".join("?" for _ in allowed_profiles)
        per_term_expr = "(" + " OR ".join(f"{col} LIKE ? ESCAPE '\\'" for col in search_cols) + ")"
        like_clauses = " OR ".join(per_term_expr for _ in terms)
        like_params: List[Any] = []
        for term in terms:
            pat = f"%{_escape_like(term)}%"
            like_params.extend([pat] * len(search_cols))

        where_parts: List[str] = []
        params: List[Any] = []
        if extra_where:
            where_parts.append(extra_where)

        where_parts.append(f"profile IN ({profile_placeholders})")
        params.extend(allowed_profiles)

        if exclude_ids:
            ex_placeholders = ",".join("?" for _ in exclude_ids)
            where_parts.append(f"id NOT IN ({ex_placeholders})")
            params.extend(exclude_ids)

        where_parts.append(f"({like_clauses})")
        params.extend(like_params)

        cur = con.cursor()
        cur.execute(
            f"SELECT {select_cols} FROM {table} WHERE {' AND '.join(where_parts)}",
            params,
        )
        candidates = [dict(r) for r in cur.fetchall()]
        tok_lowers = [t.lower() for t in tokens]
        term_lowers = [t.lower() for t in terms]

        def score(row: Dict[str, Any]) -> tuple:
            hay = " ".join(str(row[c]) for c in search_cols).lower()
            exact_hits = sum(1 for t in tok_lowers if t in hay)
            term_hits = sum(1 for t in term_lowers if t in hay)
            conf = row.get("confidence", 0.0) if has_confidence else 0.0
            return (-exact_hits, -term_hits, -row["importance"], -conf, row["id"])

        candidates.sort(key=score)
        selected = candidates[:limit]
        for row in selected:
            row["rank"] = 0.0
        return selected

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
