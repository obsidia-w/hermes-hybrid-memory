"""CLI commands for the active Obsidian memory provider."""

from __future__ import annotations

import json
from pathlib import Path

from .config import load_plugin_config
from .store import ObsidianStore


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()).expanduser().resolve()
    except Exception:
        return Path.home() / ".hermes"


def _open_store() -> ObsidianStore:
    home = _home()
    store = ObsidianStore(load_plugin_config(home), home / "obsidian-memory")
    store.initialize()
    return store


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def obsidian_command(args) -> None:
    command = getattr(args, "obsidian_command", None)
    store = _open_store()
    try:
        if command == "status":
            _print(store.status())
        elif command == "doctor":
            result = store.doctor()
            _print(result)
            if not result.get("ok"):
                raise SystemExit(1)
        elif command == "reindex":
            result = store.reindex()
            _print(result)
            if not result.get("success"):
                raise SystemExit(1)
        elif command == "curate":
            _print(store.curate_pending(max(1, int(getattr(args, "limit", 100)))))
        elif command == "reflect":
            path = store.reflect(getattr(args, "period", "daily"))
            _print({"success": True, "path": str(path)})
        else:
            print("Usage: hermes obsidian <status|doctor|reindex|curate|reflect>")
    finally:
        store.close()


def register_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="obsidian_command")
    subs.add_parser("status", help="Show Vault, index and queue counts")
    subs.add_parser("doctor", help="Verify Vault and SQLite integrity")
    subs.add_parser("reindex", help="Rebuild the memory index from Markdown")
    curate = subs.add_parser("curate", help="Process pending extraction jobs")
    curate.add_argument("--limit", type=int, default=100)
    reflect = subs.add_parser("reflect", help="Write a reviewable reflection snapshot")
    reflect.add_argument("--period", choices=["daily", "weekly"], default="daily")
    subparser.set_defaults(func=obsidian_command)
