#!/usr/bin/env python3
"""Elizabeth Local-First Memory v1.

Indexes an Obsidian-compatible Markdown memory tree into local SQLite/FTS5,
then retrieves compact context packs for the agent.

Data stays local. No network calls. No secrets are intentionally collected.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Iterable, Sequence

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = DEFAULT_ROOT / "_system" / "memory.sqlite"
DEFAULT_PACK = DEFAULT_ROOT / "_system" / "latest-context-pack.md"
MAX_CHARS_DEFAULT = 12_000  # approx <= 3k tokens for Korean/English mixed text
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|authorization)\s*[:=]\s*\S+"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{20,}"),
]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def estimate_tokens(text: str) -> int:
    # Rough but deterministic. Korean often costs more than English; keep margin.
    return max(1, len(text) // 3)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def sanitize_text(text: str) -> str:
    sanitized = text
    for pat in SECRET_PATTERNS:
        sanitized = pat.sub(lambda m: m.group(0).split("=")[0].split(":")[0] + ": [REDACTED]", sanitized)
    return sanitized


def strip_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    meta: dict[str, object] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip()
        v = v.strip().strip('"\'')
        if k:
            meta[k] = v
    return meta, body


def extract_tags(text: str, meta: dict | None = None) -> list[str]:
    tags: set[str] = set()
    if meta:
        raw = meta.get("tags")
        if isinstance(raw, str):
            for part in re.split(r"[,\s\[\]]+", raw):
                part = part.strip("# '")
                if part:
                    tags.add(part)
    for m in re.finditer(r"(?<!\w)#([\w가-힣/_-]+)", text):
        tags.add(m.group(1))
    return sorted(tags)


def heading_level(line: str) -> int | None:
    m = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
    return len(m.group(1)) if m else None


def heading_text(line: str) -> str | None:
    m = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
    return m.group(2).strip() if m else None


def split_large_block(block: str, max_chars: int) -> list[str]:
    if len(block) <= max_chars:
        return [block]
    paragraphs = re.split(r"(\n\s*\n)", block)
    out: list[str] = []
    buf = ""
    for piece in paragraphs:
        if len(buf) + len(piece) <= max_chars:
            buf += piece
        else:
            if buf.strip():
                out.append(buf.strip())
            if len(piece) > max_chars:
                for i in range(0, len(piece), max_chars):
                    out.append(piece[i : i + max_chars].strip())
                buf = ""
            else:
                buf = piece
    if buf.strip():
        out.append(buf.strip())
    return [x for x in out if x.strip()]


def chunk_markdown(text: str, source_path: str, max_chars: int = MAX_CHARS_DEFAULT) -> list[dict]:
    meta, body = strip_frontmatter(text)
    body = sanitize_text(body)
    lines = body.splitlines()
    chunks: list[dict] = []
    current_heading = Path(source_path).stem
    current_level = 1
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        content = "\n".join(buf).strip()
        if not content:
            buf = []
            return
        for idx, part in enumerate(split_large_block(content, max_chars)):
            heading = current_heading if idx == 0 else f"{current_heading} / part {idx + 1}"
            chunks.append(
                {
                    "source_path": source_path,
                    "heading": heading,
                    "level": current_level,
                    "content": part,
                    "tokens_est": estimate_tokens(part),
                    "hash": sha256_text(source_path + "\n" + heading + "\n" + part),
                    "tags": extract_tags(part, meta),
                }
            )
        buf = []

    for line in lines:
        h = heading_text(line)
        lvl = heading_level(line)
        if h is not None:
            flush()
            current_heading = h
            current_level = lvl or 1
            buf = [line]
        else:
            buf.append(line)
    flush()
    return chunks


def iter_markdown_files(root: Path) -> Iterable[Path]:
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] == "_system":
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        yield p


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=WAL")
    con.execute("pragma foreign_keys=ON")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        create table if not exists documents (
            path text primary key,
            title text,
            mtime real not null,
            size integer not null,
            sha256 text not null,
            updated_at text not null
        );
        create table if not exists chunks (
            id integer primary key autoincrement,
            doc_path text not null references documents(path) on delete cascade,
            heading text not null,
            level integer not null,
            content text not null,
            tags text not null,
            tokens_est integer not null,
            hash text not null unique,
            importance real not null default 1.0,
            created_at text not null,
            updated_at text not null
        );
        create virtual table if not exists chunks_fts using fts5(
            heading, content, tags, doc_path unindexed, content='chunks', content_rowid='id', tokenize='unicode61'
        );
        create table if not exists runs (
            id integer primary key autoincrement,
            ran_at text not null,
            root text not null,
            documents integer not null,
            chunks integer not null
        );
        """
    )
    # Triggers are easier to reset than migrate during v1.
    con.executescript(
        """
        drop trigger if exists chunks_ai;
        drop trigger if exists chunks_ad;
        drop trigger if exists chunks_au;
        create trigger chunks_ai after insert on chunks begin
          insert into chunks_fts(rowid, heading, content, tags, doc_path)
          values (new.id, new.heading, new.content, new.tags, new.doc_path);
        end;
        create trigger chunks_ad after delete on chunks begin
          insert into chunks_fts(chunks_fts, rowid, heading, content, tags, doc_path)
          values('delete', old.id, old.heading, old.content, old.tags, old.doc_path);
        end;
        create trigger chunks_au after update on chunks begin
          insert into chunks_fts(chunks_fts, rowid, heading, content, tags, doc_path)
          values('delete', old.id, old.heading, old.content, old.tags, old.doc_path);
          insert into chunks_fts(rowid, heading, content, tags, doc_path)
          values (new.id, new.heading, new.content, new.tags, new.doc_path);
        end;
        """
    )
    con.commit()


def rebuild_index(root: Path | str = DEFAULT_ROOT, db_path: Path | str = DEFAULT_DB, max_chars: int = MAX_CHARS_DEFAULT) -> dict:
    root = Path(root)
    db_path = Path(db_path)
    con = connect(db_path)
    init_db(con)
    seen: set[str] = set()
    indexed_docs = 0
    indexed_chunks = 0
    stamp = now_iso()
    with con:
        for p in iter_markdown_files(root):
            rel = p.relative_to(root).as_posix()
            seen.add(rel)
            raw = p.read_text(encoding="utf-8", errors="replace")
            digest = sha256_text(raw)
            stat = p.stat()
            title = heading_text(next((line for line in raw.splitlines() if heading_text(line)), "")) or p.stem
            con.execute(
                """insert into documents(path,title,mtime,size,sha256,updated_at)
                   values(?,?,?,?,?,?)
                   on conflict(path) do update set
                     title=excluded.title, mtime=excluded.mtime, size=excluded.size,
                     sha256=excluded.sha256, updated_at=excluded.updated_at""",
                (rel, title, stat.st_mtime, stat.st_size, digest, stamp),
            )
            con.execute("delete from chunks where doc_path=?", (rel,))
            for ch in chunk_markdown(raw, rel, max_chars=max_chars):
                con.execute(
                    """insert or ignore into chunks
                       (doc_path, heading, level, content, tags, tokens_est, hash, importance, created_at, updated_at)
                       values(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        rel,
                        ch["heading"],
                        ch["level"],
                        ch["content"],
                        json.dumps(ch["tags"], ensure_ascii=False),
                        ch["tokens_est"],
                        ch["hash"],
                        score_importance(rel, ch["heading"], ch["content"]),
                        stamp,
                        stamp,
                    ),
                )
                indexed_chunks += 1
            indexed_docs += 1
        stale = [r[0] for r in con.execute("select path from documents").fetchall() if r[0] not in seen]
        for rel in stale:
            con.execute("delete from documents where path=?", (rel,))
        chunks_count = con.execute("select count(*) from chunks").fetchone()[0]
        docs_count = con.execute("select count(*) from documents").fetchone()[0]
        con.execute("insert into runs(ran_at,root,documents,chunks) values(?,?,?,?)", (stamp, str(root), docs_count, chunks_count))
        con.execute("insert into chunks_fts(chunks_fts) values('rebuild')")
    con.close()
    return {"documents": docs_count, "chunks": chunks_count, "indexed_documents": indexed_docs, "indexed_chunks": indexed_chunks, "db": str(db_path)}


def score_importance(path: str, heading: str, content: str) -> float:
    score = 1.0
    p = path.lower()
    h = heading.lower()
    if p.startswith("profile/"):
        score += 1.0
    if p.startswith("projects/"):
        score += 0.6
    if p.startswith("decisions/"):
        score += 0.8
    if p.startswith("daily/"):
        score += 0.2
    if any(word in h for word in ["선호", "경계", "보안", "환경", "결정", "원칙"]):
        score += 0.6
    if "[REDACTED]" in content:
        score -= 0.5
    return max(0.1, score)


def search(db_path: Path | str = DEFAULT_DB, query: str = "", limit: int = 10) -> list[dict]:
    db_path = Path(db_path)
    con = connect(db_path)
    init_db(con)
    rows: Sequence[sqlite3.Row]
    if query.strip():
        # FTS5 handles Korean substrings poorly in some tokenization cases, so combine FTS with LIKE fallback.
        fts_query = " OR ".join([escape_fts_token(t) for t in re.findall(r"[\w가-힣/_-]+", query) if t.strip()])
        if fts_query:
            rows = con.execute(
                """select c.*, d.title, bm25(chunks_fts) as bm25_score
                   from chunks_fts f join chunks c on c.id=f.rowid join documents d on d.path=c.doc_path
                   where chunks_fts match ?
                   order by bm25_score asc, c.importance desc
                   limit ?""",
                (fts_query, limit),
            ).fetchall()
        else:
            rows = []
        if len(rows) < limit:
            like = f"%{query}%"
            more = con.execute(
                """select c.*, d.title, 0.0 as bm25_score
                   from chunks c join documents d on d.path=c.doc_path
                   where c.content like ? or c.heading like ? or c.doc_path like ?
                   order by c.importance desc, c.updated_at desc
                   limit ?""",
                (like, like, like, limit - len(rows)),
            ).fetchall()
            seen_ids = {r["id"] for r in rows}
            rows = list(rows) + [r for r in more if r["id"] not in seen_ids]
    else:
        rows = con.execute(
            """select c.*, d.title, 0.0 as bm25_score
               from chunks c join documents d on d.path=c.doc_path
               order by c.importance desc, c.updated_at desc
               limit ?""",
            (limit,),
        ).fetchall()
    results = [row_to_result(r) for r in rows[:limit]]
    con.close()
    return results


def escape_fts_token(token: str) -> str:
    token = token.replace('"', '""')
    return f'"{token}"'


def row_to_result(r: sqlite3.Row) -> dict:
    content = r["content"]
    return {
        "id": r["id"],
        "source_path": r["doc_path"],
        "title": r["title"],
        "heading": r["heading"],
        "tokens_est": r["tokens_est"],
        "importance": r["importance"],
        "tags": json.loads(r["tags"] or "[]"),
        "snippet": summarize_chunk(content, max_chars=420),
        "content": content,
    }


def summarize_chunk(text: str, max_chars: int = 420) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= max_chars:
        return clean
    # Keep first sentence-ish and enough context.
    return clean[: max_chars - 1].rstrip() + "…"


def build_context_pack(db_path: Path | str = DEFAULT_DB, query: str = "", token_budget: int = 1800, limit: int = 12) -> str:
    results = search(db_path, query=query, limit=limit)
    used = 0
    lines = ["# Elizabeth Local Memory Context Pack", "", f"- query: {query or '(recent/high-importance)'}", f"- generated_at: {now_iso()}", ""]
    for r in results:
        if used + r["tokens_est"] > token_budget and used > 0:
            continue
        used += r["tokens_est"]
        lines.extend(
            [
                f"## {r['heading']}",
                f"- source: `{r['source_path']}`",
                f"- tokens_est: {r['tokens_est']}",
                "",
                r["content"].strip(),
                "",
            ]
        )
        if used >= token_budget:
            break
    lines.append(f"<!-- used_tokens_est: {used} -->")
    return "\n".join(lines).strip() + "\n"


def stats(db_path: Path | str = DEFAULT_DB) -> dict:
    con = connect(Path(db_path))
    init_db(con)
    out = {
        "documents": con.execute("select count(*) from documents").fetchone()[0],
        "chunks": con.execute("select count(*) from chunks").fetchone()[0],
        "db_path": str(db_path),
    }
    last = con.execute("select ran_at from runs order by id desc limit 1").fetchone()
    out["last_run"] = last[0] if last else None
    con.close()
    return out


def cmd_index(args: argparse.Namespace) -> int:
    print(json.dumps(rebuild_index(args.root, args.db, max_chars=args.max_chars), ensure_ascii=False, indent=2))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    rows = search(args.db, args.query, args.limit)
    for i, r in enumerate(rows, 1):
        print(f"{i}. {r['heading']} — {r['source_path']} ({r['tokens_est']} tok)")
        print(f"   {r['snippet']}")
    return 0


def cmd_pack(args: argparse.Namespace) -> int:
    pack = build_context_pack(args.db, args.query, args.token_budget, args.limit)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(pack, encoding="utf-8")
        print(args.output)
    else:
        print(pack)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    print(json.dumps(stats(args.db), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Elizabeth Local-First Memory v1")
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("index", help="rebuild SQLite/FTS index from Markdown tree")
    sp.add_argument("--max-chars", type=int, default=MAX_CHARS_DEFAULT)
    sp.set_defaults(func=cmd_index)
    sp = sub.add_parser("search", help="search indexed memory")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(func=cmd_search)
    sp = sub.add_parser("pack", help="build a compact context pack")
    sp.add_argument("query")
    sp.add_argument("--token-budget", type=int, default=1800)
    sp.add_argument("--limit", type=int, default=12)
    sp.add_argument("--output", default=str(DEFAULT_PACK))
    sp.set_defaults(func=cmd_pack)
    sp = sub.add_parser("stats", help="show index stats")
    sp.set_defaults(func=cmd_stats)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
