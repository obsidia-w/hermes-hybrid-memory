"""Deterministic first-pass memory extraction for the Obsidian provider.

The rules intentionally favor explicit first-person statements. They provide a
safe local baseline while the later model-assisted curator is added.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, List


_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+)?previous\s+instructions", re.I),
    re.compile(r"system\s+prompt", re.I),
    re.compile(r"이전\s*(?:의\s*)?(?:지시|명령).*(?:무시|잊어)", re.I),
    re.compile(r"비밀.*(?:출력|공개|전송)", re.I),
]

_SECRET_PATTERNS = [
    re.compile(r"\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passwd)\s*[:=]", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b\d{6}\b.*\b(?:otp|인증번호|verification)\b", re.I),
]


@dataclass(frozen=True)
class MemoryCandidate:
    kind: str
    title: str
    statement: str
    subject_id: str
    predicate: str
    value: str
    confidence: float
    importance: float
    stability: str
    sensitivity: str
    source_id: str
    direct: bool
    promotion_target: str | None

    @property
    def normalized_key(self) -> str:
        normalized = re.sub(r"\s+", " ", self.value.strip().lower())
        raw = f"{self.kind}|{self.subject_id}|{self.predicate}|{normalized}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def contains_secret(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _SECRET_PATTERNS)


def looks_like_injection(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _INJECTION_PATTERNS)


def _sentences(text: str) -> Iterable[str]:
    for part in re.split(r"(?<=[.!?。！？])\s+|[\r\n]+", text or ""):
        cleaned = re.sub(r"\s+", " ", part).strip(" -\t")
        if 4 <= len(cleaned) <= 500:
            yield cleaned


def _candidate(
    *,
    kind: str,
    title: str,
    statement: str,
    predicate: str,
    value: str,
    source_id: str,
    confidence: float,
    importance: float,
    target: str | None,
) -> MemoryCandidate:
    return MemoryCandidate(
        kind=kind,
        title=title[:120],
        statement=statement[:500],
        subject_id="ent-user-primary" if target == "user" else "ent-project-current",
        predicate=predicate,
        value=value[:300],
        confidence=confidence,
        importance=importance,
        stability="stable",
        sensitivity="private",
        source_id=source_id,
        direct=True,
        promotion_target=target,
    )


def extract_candidates(user_text: str, source_id: str) -> List[MemoryCandidate]:
    """Extract explicit durable memories from genuine user text only."""
    if not user_text or contains_secret(user_text) or looks_like_injection(user_text):
        return []

    found: List[MemoryCandidate] = []
    seen: set[str] = set()

    def add(candidate: MemoryCandidate) -> None:
        if candidate.normalized_key not in seen:
            seen.add(candidate.normalized_key)
            found.append(candidate)

    for sentence in _sentences(user_text):
        match = re.search(
            r"(?:앞으로\s+)?(?:답변|대답|응답)(?:은|는|을|를)?\s*(.{2,140}?)\s*(?:로|으로)?\s*(?:해\s*줘|해주세요|작성해\s*줘|부탁해)",
            sentence,
            re.I,
        )
        if match:
            value = match.group(1).strip(" ,.")
            add(
                _candidate(
                    kind="preference",
                    title=f"응답 방식 선호: {value}",
                    statement=f"사용자는 응답을 {value} 방식으로 받기를 선호한다.",
                    predicate="prefers_response_style",
                    value=value,
                    source_id=source_id,
                    confidence=0.99,
                    importance=0.96,
                    target="user",
                )
            )

        match = re.search(
            r"(?:나는|저는|내가|제가)\s+(.{2,140}?)\s*(?:을|를)?\s*(?:선호해|선호합니다|좋아해|좋아합니다|원해|원합니다)(?:[.!?]|$)",
            sentence,
            re.I,
        )
        if match:
            value = match.group(1).strip(" ,.")
            add(
                _candidate(
                    kind="preference",
                    title=f"사용자 선호: {value}",
                    statement=f"사용자는 {value}을(를) 선호한다.",
                    predicate="prefers",
                    value=value,
                    source_id=source_id,
                    confidence=0.97,
                    importance=0.88,
                    target="user",
                )
            )

        match = re.search(r"\bI\s+(?:strongly\s+)?prefer\s+(.{2,180}?)(?:[.!?]|$)", sentence, re.I)
        if match:
            value = match.group(1).strip(" ,.")
            add(
                _candidate(
                    kind="preference",
                    title=f"User preference: {value}",
                    statement=f"The user prefers {value}.",
                    predicate="prefers",
                    value=value,
                    source_id=source_id,
                    confidence=0.97,
                    importance=0.88,
                    target="user",
                )
            )

        match = re.search(
            r"(?:이|우리)\s*프로젝트(?:는|에서는|에서)\s+(.{3,180}?)(?:[.!?]|$)",
            sentence,
            re.I,
        )
        if match:
            value = match.group(1).strip(" ,.")
            add(
                _candidate(
                    kind="project",
                    title=f"현재 프로젝트: {value[:80]}",
                    statement=f"현재 프로젝트는 {value}.",
                    predicate="project_fact",
                    value=value,
                    source_id=source_id,
                    confidence=0.95,
                    importance=0.86,
                    target="memory",
                )
            )

        match = re.search(
            r"(?:기억해\s*줘|기억해\s*둬|기억해|remember(?:\s+that)?)\s*[:,-]?\s*(.{3,220})",
            sentence,
            re.I,
        )
        if match:
            value = match.group(1).strip(" ,.")
            add(
                _candidate(
                    kind="profile",
                    title=f"명시적으로 요청된 기억: {value[:70]}",
                    statement=value,
                    predicate="explicit_memory",
                    value=value,
                    source_id=source_id,
                    confidence=0.99,
                    importance=0.96,
                    target="user",
                )
            )

    return found
