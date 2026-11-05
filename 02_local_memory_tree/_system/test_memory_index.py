import sqlite3
from pathlib import Path

import memory_index


def test_chunk_markdown_splits_by_headings_and_keeps_paths():
    text = "# Root\nintro\n\n## A\nalpha\n\n## B\nbeta\n"
    chunks = memory_index.chunk_markdown(text, "notes/sample.md", max_chars=200)
    headings = [c["heading"] for c in chunks]
    assert "Root" in headings
    assert "A" in headings
    assert "B" in headings
    assert all(c["source_path"] == "notes/sample.md" for c in chunks)


def test_index_search_and_pack_roundtrip(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    (root / "profile").mkdir()
    (root / "profile" / "preferences.md").write_text(
        "# 사용자 선호\n\n사용자는 짧고 실행 가능한 답변을 선호한다.\n", encoding="utf-8"
    )
    db_path = root / "_system" / "memory.sqlite"
    stats = memory_index.rebuild_index(root, db_path)
    assert stats["documents"] == 1
    assert stats["chunks"] >= 1

    results = memory_index.search(db_path, "실행 가능한 답변", limit=3)
    assert results
    assert results[0]["source_path"] == "profile/preferences.md"

    pack = memory_index.build_context_pack(db_path, "실행 가능한 답변", token_budget=500)
    assert "실행 가능한 답변" in pack
    assert "profile/preferences.md" in pack


def test_rebuild_removes_stale_documents(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    note = root / "note.md"
    note.write_text("# Note\n\nfirst", encoding="utf-8")
    db_path = root / "_system" / "memory.sqlite"
    memory_index.rebuild_index(root, db_path)
    note.unlink()
    stats = memory_index.rebuild_index(root, db_path)
    assert stats["documents"] == 0
    con = sqlite3.connect(db_path)
    try:
        count = con.execute("select count(*) from chunks").fetchone()[0]
    finally:
        con.close()
    assert count == 0
