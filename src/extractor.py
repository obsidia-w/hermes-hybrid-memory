"""Asynchronous Memory Extractor supporting the 8 Core Categories with GPT-4o-mini and Local Fallback."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error

from .config import RebuildConfig, SUPPORTED_CATEGORIES
from .db import DatabaseManager
from .indexer import sha256_text
from .store import MemoryStore


logger = logging.getLogger(__name__)

SECRET_PATTERNS = [
    re.compile(r"\b(?:api[_ -]?key|access[_ -]?token|password|passwd)\s*[:=]\s*\S+", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{16,}\b"),
]

INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+)?previous\s+instructions", re.I),
    re.compile(r"이전\s*(?:의\s*)?(?:지시|명령).*(?:무시|잊어)", re.I),
]


def contains_secret(text: str) -> bool:
    return any(p.search(text or "") for p in SECRET_PATTERNS)


def looks_like_injection(text: str) -> bool:
    return any(p.search(text or "") for p in INJECTION_PATTERNS)


EXTRACTION_SYSTEM_PROMPT = """당신은 대화에서 장기적으로 기억해야 할 핵심 정보를 8대 카테고리로 정밀하게 추출하는 메모리 큐레이터입니다.
대화에서 사용자의 지속적인 성향, 규칙, 프로젝트 사실, 중요한 결정 사항을 JSON 형식으로 추출하세요.
단순한 인사나 일회성 질문은 무시하세요.

8대 카테고리:
1. preference: 사용자의 선호 스타일 (예: "간결한 코드 선호")
2. constraint: 금기 사항, 절대적인 규칙, Don'ts (예: "테스트 없이 commit 금지")
3. decision: 아키텍처/방향성 확정 사항 (예: "DB는 SQLite FTS5로 확정")
4. fact: 환경/프로젝트 사실 (예: "OS는 Ubuntu 24.04")
5. insight: 트러블슈팅 노하우 및 배운 점 (예: "FTS5는 unicode61 토크나이저 필수")
6. plan: 앞으로의 계획 및 마일스톤 (예: "다음 주 캘린더 연동 예정")
7. status: 현재 작업의 중단점/진행 상태 (예: "DB 리팩토링 진행 중")
8. entity: 주요 도구, 인물, 프로젝트 개체

반드시 다음과 같은 JSON 형식으로만 응답하세요:
{
  "memories": [
    {
      "kind": "preference | constraint | decision | fact | insight | plan | status | entity",
      "title": "요약 제목 (30자 이내)",
      "statement": "명확하고 완전한 서술형 기억 문장",
      "importance": 0.5 ~ 1.0,
      "confidence": 0.5 ~ 1.0
    }
  ]
}
만약 추출할 만한 장기 기억이 없다면 {"memories": []} 로 답하세요.
"""


class AsynchronousExtractor:
    """Captures dialogue turns with zero latency and extracts memories in the background."""

    def __init__(
        self,
        config: RebuildConfig,
        db_manager: DatabaseManager,
        store: MemoryStore,
    ) -> None:
        self.config = config
        self.db = db_manager
        self.store = store

    def capture_turn(
        self,
        session_id: str,
        turn_number: int,
        user_text: str,
        assistant_text: str,
        profile: Optional[str] = None,
    ) -> Optional[int]:
        """Capture turn with 0-latency and enqueue background extraction."""
        target_profile = profile or self.config.profile
        now_str = datetime.now(timezone.utc).isoformat()

        # Sanitize / Guard against secrets
        if contains_secret(user_text) or looks_like_injection(user_text):
            logger.warning("Turn contains secret or injection pattern; skipping extraction.")
            return None

        # Store in sources table
        source_id = f"src-{sha256_text(user_text + assistant_text)[:12]}"
        rel_path = f"sources/{source_id}.md"

        with self.db.get_connection() as con:
            con.execute(
                """
                INSERT OR IGNORE INTO sources (
                    id, profile, path, session_id, turn_number, captured_at,
                    content_sha256, user_content, assistant_content
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id, target_profile, rel_path, session_id, turn_number,
                    now_str, sha256_text(user_text), user_text, assistant_text,
                ),
            )
            con.commit()

        # Enqueue background job (0-latency to user)
        job_payload = {
            "source_id": source_id,
            "session_id": session_id,
            "turn_number": turn_number,
            "user_text": user_text,
            "assistant_text": assistant_text,
        }
        return self.db.enqueue_job(
            kind="extract_turn",
            profile=target_profile,
            payload=job_payload,
            created_at=now_str,
        )

    def process_pending_jobs(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Process pending background jobs via GPT-4o-mini or local fallback."""
        jobs = self.db.fetch_pending_jobs(limit=limit)
        results = []

        for job in jobs:
            job_id = job["id"]
            payload = job["payload"]
            profile = job["profile"]
            user_text = payload.get("user_text", "")
            source_id = payload.get("source_id")

            now_str = datetime.now(timezone.utc).isoformat()
            try:
                extracted = self._extract_memories(user_text)
                created_ids = []
                for item in extracted:
                    kind = item.get("kind", "fact")
                    if kind not in SUPPORTED_CATEGORIES:
                        kind = "fact"

                    mem_id = self.store.remember(
                        statement=item["statement"],
                        kind=kind,
                        title=item.get("title"),
                        profile=profile,
                        importance=float(item.get("importance", 0.9)),
                        confidence=float(item.get("confidence", 0.9)),
                        source_id=source_id,
                    )
                    created_ids.append(mem_id)

                self.db.mark_job_done(job_id, now_str)
                results.append({"job_id": job_id, "status": "done", "created_ids": created_ids})
            except Exception as e:
                self.db.mark_job_failed(job_id, str(e), now_str)
                results.append({"job_id": job_id, "status": "failed", "error": str(e)})

        return results

    def _extract_memories(self, text: str) -> List[Dict[str, Any]]:
        """Try LLM extraction first (GPT-4o-mini), then fall back to local rule-based extractor."""
        api_key = os.environ.get(self.config.models.api_key_env)
        if api_key:
            try:
                return self._call_openai_extractor(text, api_key)
            except Exception as e:
                logger.warning("LLM extraction failed, falling back to local extractor: %s", e)

        return self._local_fallback_extract(text)

    def _call_openai_extractor(self, text: str, api_key: str) -> List[Dict[str, Any]]:
        """Call OpenAI chat completion API for structured memory extraction."""
        url = (self.config.models.api_base or "https://api.openai.com/v1") + "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        body = {
            "model": self.config.models.extractor_model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": f"대화 턴:\n{text}"},
            ],
            "temperature": 0.1,
        }

        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            return parsed.get("memories", [])

    def _local_fallback_extract(self, text: str) -> List[Dict[str, Any]]:
        """Deterministic rule-based extractor for when offline or no API key is provided."""
        memories = []

        # 1. Constraint (금기 사항, 절대 규칙)
        for m in re.finditer(r"(?:절대|하지\s*마|금지|말아\s*줘|말것)(?:[.!?]|$)", text):
            stmt = text.strip()
            memories.append({
                "kind": "constraint",
                "title": f"금기 사항: {stmt[:25]}",
                "statement": stmt,
                "importance": 1.0,
                "confidence": 0.95,
            })
            break

        # 2. Preference (선호)
        match = re.search(r"(?:선호해|좋아해|방식으로\s*해줘|스타일로)", text)
        if match:
            memories.append({
                "kind": "preference",
                "title": f"사용자 선호: {text[:25]}",
                "statement": text.strip(),
                "importance": 0.9,
                "confidence": 0.9,
            })

        # 3. Decision (결정)
        match = re.search(r"(?:결정했어|확정|하기로\s*하자|선택했어)", text)
        if match:
            memories.append({
                "kind": "decision",
                "title": f"결정 사항: {text[:25]}",
                "statement": text.strip(),
                "importance": 0.9,
                "confidence": 0.9,
            })

        return memories
