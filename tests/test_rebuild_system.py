"""Unit and integration tests for the Rebuilt Hybrid Memory System in an isolated environment."""

from __future__ import annotations

import tempfile
from pathlib import Path
import pytest

from src.config import RebuildConfig
from src.db import DatabaseManager
from src.extractor import AsynchronousExtractor
from src.indexer import UnifiedIndexer
from src.protection import is_user_protected, parse_frontmatter
from src.retriever import HybridRetriever
from src.store import MemoryStore


@pytest.fixture
def isolated_env():
    with tempfile.TemporaryDirectory() as tmp_dir:
        vault = Path(tmp_dir) / "test_vault"
        vault.mkdir(parents=True)
        db_path = vault / ".memory" / "index.sqlite3"

        config = RebuildConfig(
            vault_path=vault,
            profile="elizabeth",
            shared_profiles=["shared"],
            db_path=db_path,
        )
        db = DatabaseManager(db_path)
        store = MemoryStore(config, db)
        indexer = UnifiedIndexer(config, db)
        retriever = HybridRetriever(config, db)
        extractor = AsynchronousExtractor(config, db, store)

        yield {
            "config": config,
            "vault": vault,
            "db": db,
            "store": store,
            "indexer": indexer,
            "retriever": retriever,
            "extractor": extractor,
        }


def test_user_protection(isolated_env):
    vault = isolated_env["vault"]
    store = isolated_env["store"]

    # 1. Create a user-locked note
    mem_id = store.remember(
        statement="사용자가 직접 작성한 불변 규칙",
        kind="constraint",
        user_locked=True,
    )

    # 2. Attempting to correct/supersede a user-locked note must raise PermissionError
    with pytest.raises(PermissionError):
        store.correct(mem_id, "이 규칙을 임의로 수정하려 함")


def test_immutable_supersede_lifecycle(isolated_env):
    store = isolated_env["store"]
    vault = isolated_env["vault"]

    # 1. Initial decision memory
    mem_1 = store.remember(
        statement="DB는 PostgreSQL을 사용한다.",
        kind="decision",
        title="DB 선택",
    )

    # 2. Correction without destroying history
    mem_2 = store.correct(
        old_memory_id=mem_1,
        new_statement="DB는 SQLite FTS5로 최종 확정한다.",
        reason="로컬 우선 원칙 적용",
    )

    # Check status in DB
    with isolated_env["db"].get_connection() as con:
        cur = con.cursor()
        cur.execute("SELECT status, superseded_by FROM memories WHERE id = ?", (mem_1,))
        row1 = cur.fetchone()
        assert row1["status"] == "superseded"
        assert row1["superseded_by"] == mem_2

        cur.execute("SELECT status, statement FROM memories WHERE id = ?", (mem_2,))
        row2 = cur.fetchone()
        assert row2["status"] == "active"
        assert "SQLite FTS5" in row2["statement"]


def test_hybrid_indexing_and_retrieval(isolated_env):
    vault = isolated_env["vault"]
    store = isolated_env["store"]
    indexer = isolated_env["indexer"]
    retriever = isolated_env["retriever"]

    # 1. Add atomic memories
    store.remember(
        statement="코드 작성 시 불필요한 설명을 생략하고 완성형 코드로 응답할 것",
        kind="constraint",
    )
    store.remember(
        statement="프로젝트 루트 경로는 /home/hermes/Codex/Rebuild 이다.",
        kind="fact",
    )

    # 2. Add general project documentation markdown file
    proj_dir = vault / "Projects"
    proj_dir.mkdir(parents=True)
    doc_file = proj_dir / "Memory-Architecture.md"
    doc_file.write_text(
        """# 메모리 시스템 아키텍처
## 개요
이 프로젝트는 FTS5 검색과 비동기 큐를 사용합니다.

## 데이터베이스 설계
테이블은 memories와 chunks로 구성되며 unicode61 토크나이저를 사용합니다.
""",
        encoding="utf-8",
    )

    # 3. Run full indexing pass
    stats = indexer.index_all()
    assert stats["memories_indexed"] >= 2
    assert stats["chunks_indexed"] >= 2

    # 4. Perform hybrid search
    res = retriever.retrieve("데이터베이스 설계 FTS5")
    formatted = res["formatted_context"]

    # Verify that constraint is injected first
    assert "[규칙] 코드 작성 시 불필요한 설명을 생략" in formatted
    # Verify that project document chunk is retrieved
    assert "데이터베이스 설계" in formatted
    assert "unicode61" in formatted


def test_zero_latency_queue_extraction(isolated_env):
    extractor = isolated_env["extractor"]
    db = isolated_env["db"]

    # 1. Capture turn
    job_id = extractor.capture_turn(
        session_id="test-session-1",
        turn_number=1,
        user_text="앞으로는 테스트 코드 돌리기 전에는 commit 하지 말아줘. 그리고 SQLite를 선호해.",
        assistant_text="네, 알겠습니다. 테스트 확인 전 commit 금지 규칙과 SQLite 선호를 기억하겠습니다.",
    )
    assert job_id is not None

    # Verify job is recorded in DB
    jobs = db.fetch_pending_jobs()
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "extract_turn"

    # 2. Process background extraction
    results = extractor.process_pending_jobs()
    assert len(results) == 1
    assert results[0]["status"] == "done"

    # Check newly extracted memories in DB
    with db.get_connection() as con:
        cur = con.cursor()
        cur.execute("SELECT kind, statement FROM memories WHERE status = 'active'")
        rows = cur.fetchall()
        kinds = [r["kind"] for r in rows]
        # Should extract constraint and/or preference via fallback
        assert any(k in ("constraint", "preference") for k in kinds)


def test_secret_filtering(isolated_env):
    extractor = isolated_env["extractor"]
    db = isolated_env["db"]

    # Turn containing a sensitive API key
    job_id = extractor.capture_turn(
        session_id="test-session-2",
        turn_number=2,
        user_text="내 OPENAI_API_KEY=sk-abcdef1234567890abcdef1234567890 이거 저장해둬.",
        assistant_text="확인했습니다.",
    )
    # Must be blocked from being enqueued
    assert job_id is None
