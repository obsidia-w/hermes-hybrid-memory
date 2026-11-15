"""Hermes Agent MemoryProvider adapter prototype for the rebuilt hybrid memory system.

Note: This module is a draft adapter interface and is NOT yet wired into
Hermes Agent's runtime plugin loader (``MemoryProvider`` ABC, ``plugin.yaml``,
``register()``, or ``sync_turn()`` contract).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import RebuildConfig, load_config
from .db import DatabaseManager
from .extractor import AsynchronousExtractor
from .indexer import UnifiedIndexer
from .retriever import HybridRetriever
from .store import MemoryStore


logger = logging.getLogger(__name__)


MEMORY_TOOL_SCHEMA = {
    "name": "obsidian_memory",
    "description": (
        "Search, inspect, record, or correct durable long-term memories in the local Obsidian vault. "
        "High-priority preferences and related context are already auto-injected before each turn. "
        "Use 'search' for deep lookups, 'remember' for durable facts/constraints, and 'correct' to supersede old memories."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "remember", "correct", "status"],
            },
            "query": {"type": "string", "description": "Search query for 'search'"},
            "memory_id": {"type": "string", "description": "Target mem-ID for 'correct'"},
            "statement": {"type": "string", "description": "Content for 'remember' or 'correct'"},
            "kind": {
                "type": "string",
                "enum": ["preference", "constraint", "decision", "fact", "insight", "plan", "status", "entity"],
                "description": "Category of memory for 'remember'",
            },
            "reason": {"type": "string", "description": "Audit reason for 'correct'"},
        },
        "required": ["action"],
    },
}


class RebuiltObsidianMemoryProvider:
    """Draft hybrid memory provider adapter (not yet auto-registered in Hermes Agent)."""

    def __init__(self, config: Optional[RebuildConfig] = None) -> None:
        self.config = config
        self.db: Optional[DatabaseManager] = None
        self.store: Optional[MemoryStore] = None
        self.indexer: Optional[UnifiedIndexer] = None
        self.retriever: Optional[HybridRetriever] = None
        self.extractor: Optional[AsynchronousExtractor] = None
        self.session_id: str = ""
        self.turn_number: int = 0

    @property
    def name(self) -> str:
        return "obsidian"

    def is_available(self) -> bool:
        if not self.config:
            return True
        return self.config.vault_path.is_dir()

    def initialize(self, session_id: str = "", **kwargs: Any) -> None:
        self.session_id = str(session_id or "")
        self.turn_number = 0

        if not self.config:
            # Look up standard config location
            hermes_home = Path(kwargs.get("hermes_home") or Path.home() / ".hermes")
            cfg_path = hermes_home / "obsidian-memory" / "config.yaml"
            if cfg_path.exists():
                self.config = load_config(cfg_path)
            else:
                env_vault = os.environ.get("HERMES_MEMORY_VAULT")
                vault_path = Path(env_vault).expanduser().resolve() if env_vault else (Path.home() / "Hermes-Memory-Vault")
                profile = os.environ.get("HERMES_MEMORY_PROFILE", "default")
                self.config = RebuildConfig(vault_path=vault_path, profile=profile)

        db_path = self.config.db_path or (self.config.vault_path / ".memory" / "index.sqlite3")
        self.db = DatabaseManager(db_path)
        self.store = MemoryStore(self.config, self.db)
        self.indexer = UnifiedIndexer(self.config, self.db)
        self.retriever = HybridRetriever(self.config, self.db)
        self.extractor = AsynchronousExtractor(self.config, self.db, self.store)

        # Quick startup reindex pass
        try:
            self.indexer.index_all()
        except Exception as e:
            logger.warning("Startup indexing encountered errors: %s", e)

    def prefetch(self, user_prompt: str) -> Optional[str]:
        """Auto-recall relevant context before generating the model response."""
        if not self.retriever or not user_prompt:
            return None

        result = self.retriever.retrieve(user_prompt)
        formatted = result.get("formatted_context", "").strip()
        if not formatted:
            return None

        return f"<recalled_memories>\n{formatted}\n</recalled_memories>"

    def capture_turn(
        self,
        user_content: str,
        assistant_content: str,
        tool_traces: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Capture completed turn asynchronously with 0-ms delay."""
        if not self.extractor:
            return

        self.turn_number += 1
        self.extractor.capture_turn(
            session_id=self.session_id,
            turn_number=self.turn_number,
            user_text=user_content,
            assistant_text=assistant_content,
        )

    def flush(self, session_id: Optional[str] = None) -> None:
        """Called at session end or idle to process queued extractions."""
        if self.extractor:
            logger.info("Flushing pending memory extraction jobs...")
            self.extractor.process_pending_jobs(limit=10)

    def shutdown(self) -> None:
        self.flush()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [MEMORY_TOOL_SCHEMA]

    def handle_tool_call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name != "obsidian_memory" or not self.store or not self.retriever:
            return {"error": f"Unknown tool: {name}"}

        action = arguments.get("action")
        if action == "search":
            query = arguments.get("query", "")
            return self.retriever.retrieve(query)
        elif action == "remember":
            stmt = arguments.get("statement", "")
            kind = arguments.get("kind", "fact")
            mem_id = self.store.remember(stmt, kind=kind)
            return {"status": "success", "memory_id": mem_id}
        elif action == "correct":
            old_id = arguments.get("memory_id", "")
            stmt = arguments.get("statement", "")
            reason = arguments.get("reason", "Agent correction")
            new_id = self.store.correct(old_id, stmt, reason=reason)
            return {"status": "success", "superseded_by": new_id}
        elif action == "status":
            return {"status": "active", "session_id": self.session_id, "profile": self.config.profile}

        return {"error": f"Unknown action: {action}"}
