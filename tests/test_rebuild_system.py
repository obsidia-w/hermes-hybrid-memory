"""Unit and integration tests for the Rebuilt Hybrid Memory System in an isolated environment."""

from __future__ import annotations

import tempfile
from pathlib import Path
import pytest

from src.cli import main as cli_main
from src.config import RebuildConfig, load_config
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
        statement="프로젝트 루트 경로는 /example/workspace/hermes-hybrid-memory 이다.",
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


def test_korean_particle_and_partial_match_retrieval(isolated_env):
    vault = isolated_env["vault"]
    store = isolated_env["store"]
    indexer = isolated_env["indexer"]
    retriever = isolated_env["retriever"]

    # Stored with Korean particles and compound words
    store.remember(
        statement="로컬 저장소로 SQLite를 사용한다.",
        kind="decision",
        title="저장소 선택",
    )
    store.remember(
        statement="비동기큐설계 원칙에 따라 백그라운드 처리를 분리한다.",
        kind="insight",
        title="큐 처리 원칙",
    )
    store.remember(
        statement="Python 환경에서 테스트를 실행한다.",
        kind="fact",
        title="실행 환경",
    )

    docs_dir = vault / "Projects"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "Search-Notes.md").write_text(
        "# 검색 노트\n## 한국어검색\nSQLite 기반 데이터베이스설계를 문서화한다.\n",
        encoding="utf-8",
    )
    indexer.index_all()

    # 1. Base keyword matches stored token with attached particle ("SQLite를") without duplicating chunks
    res_base = retriever.retrieve("SQLite")
    assert res_base["memories_count"] == 1
    assert res_base["chunks_count"] == 1
    assert "SQLite를" in res_base["formatted_context"]
    assert res_base["formatted_context"].count("Projects/Search-Notes.md > 한국어검색") == 1

    # 2. Query with attached particle ("Python으로") matches stored base token ("Python")
    res_particle_query = retriever.retrieve("Python으로")
    assert "Python 환경" in res_particle_query["formatted_context"]

    # 3. Partial substring inside Korean compound word ("비동기큐설계" -> "큐", "설계")
    res_partial_mem = retriever.retrieve("비동기큐")
    assert "비동기큐설계" in res_partial_mem["formatted_context"]

    res_partial_chunk = retriever.retrieve("설계")
    assert "데이터베이스설계를" in res_partial_chunk["formatted_context"]


def test_user_manual_section_preserved_on_correct(isolated_env):
    vault = isolated_env["vault"]
    store = isolated_env["store"]

    mem_1 = store.remember(
        statement="초기 배포 방식은 단일 스크립트 실행이다.",
        kind="decision",
        title="배포 방식",
    )
    with isolated_env["db"].get_connection() as con:
        row = con.execute("SELECT path FROM memories WHERE id = ?", (mem_1,)).fetchone()
    old_file = vault / row["path"]
    old_file.write_text(
        old_file.read_text(encoding="utf-8") + "\n## 사용자 수동 메모\n- 운영 서버 점검 시간은 매주 일요일 03:00\n",
        encoding="utf-8",
    )

    mem_2 = store.correct(mem_1, "배포 방식은 CLI와 테스트 통과 후 수동 배포로 변경한다.", reason="정책 변경")
    with isolated_env["db"].get_connection() as con:
        row2 = con.execute("SELECT path FROM memories WHERE id = ?", (mem_2,)).fetchone()
    new_text = (vault / row2["path"]).read_text(encoding="utf-8")
    assert "운영 서버 점검 시간은 매주 일요일 03:00" in new_text


def test_config_override_and_cli_roundtrip(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "schema_version: 2\nvault_path: ./ignored_default\nprofile: default\n",
        encoding="utf-8",
    )
    custom_vault = tmp_path / "custom_vault"
    monkeypatch.setenv("HERMES_MEMORY_VAULT", str(custom_vault))
    monkeypatch.setenv("HERMES_MEMORY_PROFILE", "sandbox")

    cfg = load_config(cfg_file)
    assert cfg.vault_path == custom_vault.resolve()
    assert cfg.profile == "sandbox"

    # Explicit missing config file must fail instead of falling back silently
    with pytest.raises(FileNotFoundError):
        cli_main(["--config", str(tmp_path / "missing.yaml"), "status"])

    # CLI end-to-end smoke flow on an isolated temporary vault
    cli_vault = tmp_path / "cli_vault"
    assert cli_main(["--vault", str(cli_vault), "--profile", "demo", "status"]) == 0
    assert cli_main([
        "--vault", str(cli_vault), "--profile", "demo",
        "remember", "로컬 환경에서 SQLite를 기본 저장소로 사용한다.", "--kind", "decision",
    ]) == 0
    assert cli_main([
        "--vault", str(cli_vault), "--profile", "demo",
        "capture", "--user", "앞으로는 테스트 없이 commit 하지 말아줘.", "--assistant", "알겠습니다.",
    ]) == 0
    assert cli_main(["--vault", str(cli_vault), "--profile", "demo", "process-jobs"]) == 0
    assert cli_main(["--vault", str(cli_vault), "--profile", "demo", "search", "SQLite"]) == 0
