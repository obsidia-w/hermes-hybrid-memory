"""User Edit Protection Engine for the Obsidian Memory System.

Guarantees:
1. Notes marked with user_locked: true or source: manual are strictly read-only for AI.
2. User-authored sections inside notes (e.g., '## 사용자 수동 메모') are preserved during any re-renders.
3. Corrections and updates use immutable superseding instead of destructive overwrite.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple
import yaml


USER_SECTION_HEADER_PATTERN = re.compile(
    r"^(##+\s+(?:사용자\s*(?:수동\s*)?메모|User\s*Notes?|Manual\s*Notes?).*?)$",
    re.MULTILINE | re.IGNORECASE,
)

FRONTMATTER_PATTERN = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter and return (metadata_dict, remaining_body)."""
    match = FRONTMATTER_PATTERN.match(text or "")
    if not match:
        return {}, text or ""

    raw_yaml = match.group(1)
    body = text[match.end():]
    try:
        data = yaml.safe_load(raw_yaml) or {}
        if isinstance(data, dict):
            return data, body
    except Exception:
        pass
    return {}, body


def dump_frontmatter(data: Dict[str, Any], body: str) -> str:
    """Format metadata into YAML frontmatter and prepend to body."""
    yaml_str = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()
    clean_body = body.lstrip("\r\n")
    return f"---\n{yaml_str}\n---\n\n{clean_body}"


def is_user_protected(meta: Dict[str, Any], raw_text: str = "") -> bool:
    """Check if the note is strictly user-protected against AI modifications."""
    if meta.get("user_locked") is True:
        return True
    if str(meta.get("source", "")).strip().lower() in ("manual", "user"):
        return True

    tags = meta.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            tag_str = str(tag).strip().lower()
            if tag_str in ("manual", "user-locked", "user_locked", "immutable"):
                return True
    elif isinstance(tags, str):
        if any(t in tags.lower() for t in ("manual", "user-locked", "user_locked")):
            return True

    if "<!-- user_locked -->" in raw_text or "<!-- manual -->" in raw_text:
        return True

    return False


def extract_user_custom_sections(body: str) -> str:
    """Extract any custom user-added sections from the body to prevent loss."""
    matches = list(USER_SECTION_HEADER_PATTERN.finditer(body))
    if not matches:
        return ""

    user_sections: list[str] = []
    for match in matches:
        start_idx = match.start()
        # Find next header of equal or higher level (e.g. ## or #)
        header_text = match.group(1)
        level = len(match.group(1).split()[0])  # count '#'
        next_header_pattern = re.compile(rf"^#{{1,{level}}}\s+", re.MULTILINE)
        
        remaining = body[match.end():]
        next_match = next_header_pattern.search(remaining)
        if next_match:
            end_idx = match.end() + next_match.start()
        else:
            end_idx = len(body)
        user_sections.append(body[start_idx:end_idx].strip())

    return "\n\n".join(user_sections)


def mark_superseded(
    current_content: str,
    new_memory_id: str,
    reason: str = "corrected",
    timestamp: Optional[str] = None,
) -> str:
    """Update metadata to mark a note as superseded without altering its content."""
    meta, body = parse_frontmatter(current_content)
    meta["status"] = "superseded"
    meta["superseded_by"] = f"[[{new_memory_id}]]"
    meta["superseded_reason"] = reason
    if timestamp:
        meta["superseded_at"] = timestamp
    return dump_frontmatter(meta, body)
