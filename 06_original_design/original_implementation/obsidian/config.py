"""Configuration helpers for the Hermes Obsidian memory provider."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import yaml


DEFAULT_VAULT_PATH = Path("/home/hermes/Documents/Codex/Hermes-Memory-Vault")


def _hermes_home() -> Path:
    """Resolve the active profile-scoped Hermes home without caching it."""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()).expanduser().resolve()
    except Exception:
        configured = os.environ.get("HERMES_HOME")
        if configured:
            return Path(configured).expanduser().resolve()
        return Path.home() / ".hermes"


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ObsidianConfig:
    vault_path: Path = DEFAULT_VAULT_PATH
    capture_enabled: bool = True
    include_tool_traces: bool = True
    large_artifact_threshold_kb: int = 256
    curate_on_capture: bool = True
    max_curate_jobs_per_turn: int = 4
    retrieval_max_results: int = 5
    retrieval_max_tokens: int = 1200
    min_confidence: float = 0.55
    promotion_enabled: bool = True
    promotion_threshold: float = 0.78
    memory_cache_chars: int = 800
    user_cache_chars: int = 600

    @property
    def state_dir(self) -> Path:
        return _hermes_home() / "obsidian-memory"

    @property
    def config_path(self) -> Path:
        return self.state_dir / "config.yaml"

    @classmethod
    def from_mapping(cls, raw: Dict[str, Any] | None) -> "ObsidianConfig":
        raw = raw or {}
        capture = raw.get("capture") if isinstance(raw.get("capture"), dict) else {}
        curation = raw.get("curation") if isinstance(raw.get("curation"), dict) else {}
        retrieval = raw.get("retrieval") if isinstance(raw.get("retrieval"), dict) else {}
        promotion = raw.get("promotion") if isinstance(raw.get("promotion"), dict) else {}

        vault = Path(raw.get("vault_path") or DEFAULT_VAULT_PATH).expanduser()
        return cls(
            vault_path=vault.resolve(),
            capture_enabled=_as_bool(capture.get("enabled"), True),
            include_tool_traces=_as_bool(capture.get("include_tool_traces"), True),
            large_artifact_threshold_kb=max(
                16, int(capture.get("large_artifact_threshold_kb", 256))
            ),
            curate_on_capture=_as_bool(curation.get("on_capture"), True),
            max_curate_jobs_per_turn=max(
                1, int(curation.get("max_jobs_per_turn", 4))
            ),
            retrieval_max_results=max(1, int(retrieval.get("max_results", 5))),
            retrieval_max_tokens=max(200, int(retrieval.get("max_tokens", 1200))),
            min_confidence=min(1.0, max(0.0, float(retrieval.get("min_confidence", 0.55)))),
            promotion_enabled=_as_bool(promotion.get("enabled"), True),
            promotion_threshold=min(
                1.0, max(0.0, float(promotion.get("threshold", 0.78)))
            ),
            memory_cache_chars=max(0, int(promotion.get("memory_cache_chars", 800))),
            user_cache_chars=max(0, int(promotion.get("user_cache_chars", 600))),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "vault_path": str(self.vault_path),
            "capture": {
                "enabled": self.capture_enabled,
                "include_tool_traces": self.include_tool_traces,
                "large_artifact_threshold_kb": self.large_artifact_threshold_kb,
            },
            "curation": {
                "on_capture": self.curate_on_capture,
                "max_jobs_per_turn": self.max_curate_jobs_per_turn,
                "redact_secrets_for_remote_models": True,
            },
            "retrieval": {
                "mode": "lexical_graph",
                "max_results": self.retrieval_max_results,
                "max_tokens": self.retrieval_max_tokens,
                "min_confidence": self.min_confidence,
            },
            "promotion": {
                "enabled": self.promotion_enabled,
                "threshold": self.promotion_threshold,
                "memory_cache_chars": self.memory_cache_chars,
                "user_cache_chars": self.user_cache_chars,
            },
            "reflection": {"daily": True, "weekly": True},
        }


def load_plugin_config(hermes_home: str | Path | None = None) -> ObsidianConfig:
    home = Path(hermes_home).expanduser().resolve() if hermes_home else _hermes_home()
    path = home / "obsidian-memory" / "config.yaml"
    if not path.exists():
        return ObsidianConfig()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return ObsidianConfig.from_mapping(raw if isinstance(raw, dict) else {})
    except Exception:
        return ObsidianConfig()


def save_plugin_config(
    config: ObsidianConfig, hermes_home: str | Path | None = None
) -> Path:
    home = Path(hermes_home).expanduser().resolve() if hermes_home else _hermes_home()
    path = home / "obsidian-memory" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_dump(
        config.to_mapping(), allow_unicode=True, sort_keys=False
    )
    fd, temp_name = tempfile.mkstemp(prefix=".config-", suffix=".yaml", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return path
