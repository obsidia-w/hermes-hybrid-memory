"""CLI Interface for Managing the Rebuilt Memory System."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .config import RebuildConfig, load_config
from .db import DatabaseManager
from .extractor import AsynchronousExtractor
from .indexer import UnifiedIndexer
from .retriever import HybridRetriever
from .store import MemoryStore


def get_system(config_path: Optional[str] = None, vault_path: Optional[str] = None):
    # Auto-find config or create default
    default_cfg = Path("04_configuration/rebuild-memory.config.yaml")
    if not config_path and default_cfg.exists():
        cfg_file = default_cfg
    elif config_path:
        cfg_file = Path(config_path)
    else:
        # Default fallback
        cfg_file = None

    if cfg_file and cfg_file.exists():
        config = load_config(cfg_file, vault_override=vault_path)
    else:
        vault = Path(vault_path or "./test_vault").resolve()
        config = RebuildConfig(vault_path=vault, db_path=vault / ".memory" / "index.sqlite3")

    db_path = config.db_path or (config.vault_path / ".memory" / "index.sqlite3")
    db = DatabaseManager(db_path)
    store = MemoryStore(config, db)
    indexer = UnifiedIndexer(config, db)
    retriever = HybridRetriever(config, db)
    extractor = AsynchronousExtractor(config, db, store)

    return config, db, store, indexer, retriever, extractor


def cmd_reindex(args: argparse.Namespace) -> int:
    config, db, store, indexer, retriever, extractor = get_system(args.config, args.vault)
    print(f"[*] Reindexing vault: {config.vault_path} (Profile: {config.profile})")
    stats = indexer.index_all()
    print("[+] Reindex complete:")
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    config, db, store, indexer, retriever, extractor = get_system(args.config, args.vault)
    print(f"[*] Searching for '{args.query}'...")
    res = retriever.retrieve(args.query, max_tokens=args.max_tokens)
    print(f"[+] Found {res['memories_count']} memories, {res['chunks_count']} chunks ({res['tokens_est']} tokens)")
    print("\n--- [Retrieved Context Packet] ---")
    print(res["formatted_context"])
    return 0


def cmd_remember(args: argparse.Namespace) -> int:
    config, db, store, indexer, retriever, extractor = get_system(args.config, args.vault)
    mem_id = store.remember(
        statement=args.statement,
        kind=args.kind,
        title=args.title,
        user_locked=args.lock,
    )
    print(f"[+] Memory remembered successfully! ID: {mem_id}")
    return 0


def cmd_correct(args: argparse.Namespace) -> int:
    config, db, store, indexer, retriever, extractor = get_system(args.config, args.vault)
    new_id = store.correct(
        old_memory_id=args.old_id,
        new_statement=args.new_statement,
        reason=args.reason or "Manual CLI correction",
    )
    print(f"[+] Memory '{args.old_id}' superseded by '{new_id}'")
    return 0


def cmd_process_jobs(args: argparse.Namespace) -> int:
    config, db, store, indexer, retriever, extractor = get_system(args.config, args.vault)
    print("[*] Processing background extraction jobs...")
    results = extractor.process_pending_jobs(limit=args.limit)
    print(f"[+] Processed {len(results)} jobs:")
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config, db, store, indexer, retriever, extractor = get_system(args.config, args.vault)
    with db.get_connection() as con:
        cur = con.cursor()
        cur.execute("SELECT count(*) as c FROM memories WHERE status = 'active'")
        active_mems = cur.fetchone()["c"]
        cur.execute("SELECT count(*) as c FROM chunks")
        total_chunks = cur.fetchone()["c"]
        cur.execute("SELECT count(*) as c FROM jobs WHERE status = 'pending'")
        pending_jobs = cur.fetchone()["c"]

    print(f"=== Memory System Status ===")
    print(f"Vault: {config.vault_path}")
    print(f"Profile: {config.profile} (Shared: {config.shared_profiles})")
    print(f"Extractor Model: {config.models.extractor_model}")
    print(f"Reflection Model: {config.models.reflection_model}")
    print(f"Active Memories: {active_mems}")
    print(f"Document Chunks: {total_chunks}")
    print(f"Pending Jobs in Queue: {pending_jobs}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuilt Hybrid Memory System CLI")
    parser.add_argument("--config", help="Path to config.yaml")
    parser.add_argument("--vault", help="Override vault path")

    sub = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = sub.add_parser("status", help="Show system status")
    p_status.set_defaults(func=cmd_status)

    # reindex
    p_reindex = sub.add_parser("reindex", help="Reindex entire vault into SQLite FTS5")
    p_reindex.set_defaults(func=cmd_reindex)

    # search
    p_search = sub.add_parser("search", help="Search memories and documents")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--max-tokens", type=int, default=1200, help="Max tokens budget")
    p_search.set_defaults(func=cmd_search)

    # remember
    p_rem = sub.add_parser("remember", help="Manually add a memory note")
    p_rem.add_argument("statement", help="Memory statement")
    p_rem.add_argument("--kind", default="fact", choices=["preference", "constraint", "decision", "fact", "insight", "plan", "status", "entity"])
    p_rem.add_argument("--title", help="Optional title")
    p_rem.add_argument("--lock", action="store_true", help="Lock against AI editing")
    p_rem.set_defaults(func=cmd_remember)

    # correct
    p_corr = sub.add_parser("correct", help="Supersede an existing memory with a correction")
    p_corr.add_argument("old_id", help="Memory ID to supersede")
    p_corr.add_argument("new_statement", help="New corrected statement")
    p_corr.add_argument("--reason", help="Audit reason")
    p_corr.set_defaults(func=cmd_correct)

    # process-jobs
    p_jobs = sub.add_parser("process-jobs", help="Process queued extraction jobs")
    p_jobs.add_argument("--limit", type=int, default=10, help="Max jobs to process")
    p_jobs.set_defaults(func=cmd_process_jobs)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
