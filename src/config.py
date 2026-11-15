"""Configuration schema and loader for the rebuilt hybrid memory system."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml


SUPPORTED_CATEGORIES = [
    "preference",   # User style and preference
    "constraint",   # Strict rules, prohibitions, "don'ts"
    "decision",     # Architectural/project decisions
    "fact",         # Environmental/project facts
    "insight",      # Lessons learned, troubleshooting tips
    "plan",         # Milestones, future plans
    "status",       # Active working context, pause/resume state
    "entity",       # Agents, persons, tools, vault items
]


@dataclass
class RetrievalConfig:
    max_results: int = 6
    max_tokens: int = 1200
    chunk_tokens: int = 400
    min_confidence: float = 0.5
    include_shared: bool = True


@dataclass
class ModelConfig:
    extractor_model: str = "gpt-4o-mini"
    reflection_model: str = "luna"
    api_key_env: str = "OPENAI_API_KEY"
    api_base: Optional[str] = None


@dataclass
class RebuildConfig:
    vault_path: Path
    profile: str = "default"
    shared_profiles: List[str] = field(default_factory=lambda: ["shared", "global"])
    models: ModelConfig = field(default_factory=ModelConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    db_path: Optional[Path] = None  # Derived index sqlite path

    def __post_init__(self) -> None:
        if isinstance(self.vault_path, str):
            self.vault_path = Path(self.vault_path).expanduser().resolve()
        if self.db_path and isinstance(self.db_path, str):
            self.db_path = Path(self.db_path).expanduser().resolve()

    def profile_memory_dir(self, profile_name: Optional[str] = None) -> Path:
        target = profile_name or self.profile
        return self.vault_path / "20_Memories" / target

    def shared_memory_dirs(self) -> List[Path]:
        return [self.vault_path / "20_Memories" / p for p in self.shared_profiles]


def load_config(
    config_path: Path | str,
    vault_override: Optional[Path | str] = None,
    profile_override: Optional[str] = None,
) -> RebuildConfig:
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw: Dict[str, Any] = yaml.safe_load(f) or {}

    vault_val = vault_override or os.environ.get("HERMES_MEMORY_VAULT") or raw.get("vault_path")
    if not vault_val:
        raise ValueError("vault_path must be configured in config file, parameter, or HERMES_MEMORY_VAULT env")

    profile_val = str(
        profile_override or os.environ.get("HERMES_MEMORY_PROFILE") or raw.get("profile", "default")
    )

    models_raw = raw.get("models", {})
    models = ModelConfig(
        extractor_model=models_raw.get("extractor_model", "gpt-4o-mini"),
        reflection_model=models_raw.get("reflection_model", "luna"),
        api_key_env=models_raw.get("api_key_env", "OPENAI_API_KEY"),
        api_base=models_raw.get("api_base"),
    )

    retrieval_raw = raw.get("retrieval", {})
    retrieval = RetrievalConfig(
        max_results=retrieval_raw.get("max_results", 6),
        max_tokens=retrieval_raw.get("max_tokens", 1200),
        chunk_tokens=retrieval_raw.get("chunk_tokens", 400),
        min_confidence=retrieval_raw.get("min_confidence", 0.5),
        include_shared=retrieval_raw.get("include_shared", True),
    )

    db_path_val = os.environ.get("HERMES_MEMORY_DB") or raw.get("db_path")
    db_path = Path(db_path_val).expanduser().resolve() if db_path_val else None

    return RebuildConfig(
        vault_path=Path(vault_val).expanduser().resolve(),
        profile=profile_val,
        shared_profiles=list(raw.get("shared_profiles", ["shared", "global"])),
        models=models,
        retrieval=retrieval,
        db_path=db_path,
    )
