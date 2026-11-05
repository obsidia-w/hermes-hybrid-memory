#!/usr/bin/env python3
"""Collect Google Calendar events into Elizabeth's local-first Obsidian memory tree.

Calendar-only collector:
- Reads Google Calendar through the existing Hermes google-workspace wrapper.
- Writes one Obsidian-compatible Markdown chunk per event.
- Regenerates month index notes for quick inspection.
- Runs the local memory SQLite/FTS indexer after writing.

No Gmail/Docs/Drive data is collected here.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Seoul")
PROFILE_HOME = Path(os.environ.get("HERMES_HOME", "/home/hermes/.hermes/profiles/elizabeth"))
VAULT = Path("/home/hermes/Obsidian/Macidian")
MEMORY_ROOT = VAULT / "hermes저장소" / "엘리자베스" / "로컬 메모리 트리"
CAL_ROOT = MEMORY_ROOT / "sources" / "google-calendar"
EVENTS_DIR = CAL_ROOT / "events"
MONTHS_DIR = CAL_ROOT / "months"
SYSTEM_DIR = MEMORY_ROOT / "_system"
STATE_PATH = SYSTEM_DIR / "calendar_collector_state.json"
GAPI = PROFILE_HOME / "skills" / "productivity" / "productivity-platform-operations" / "references" / "google-workspace" / "scripts" / "google_api.py"
INDEXER = SYSTEM_DIR / "memory_index.py"
CONTEXT_PACK_SCRIPT = PROFILE_HOME / "scripts" / "elizabeth_calendar_context_pack.py"
DAILY_NOTE_SCRIPT = PROFILE_HOME / "scripts" / "elizabeth_daily_note_ensure.py"

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|authorization)\s*[:=]\s*\S+"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{20,}"),
]


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def iso_zoned(d: dt.datetime) -> str:
    return d.astimezone(TZ).isoformat(timespec="seconds")


def parse_event_dt(value: str) -> dt.datetime:
    if not value:
        return now()
    # google_api currently returns ISO strings. All-day events may still include T00:00.
    if value.endswith("Z"):
        return dt.datetime.fromisoformat(value[:-1] + "+00:00").astimezone(TZ)
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        parsed_date = dt.date.fromisoformat(value[:10])
        return dt.datetime.combine(parsed_date, dt.time.min, TZ)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def sanitize(text: str) -> str:
    text = text or ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    for pat in SECRET_PATTERNS:
        text = pat.sub(lambda m: m.group(0).split("=")[0].split(":")[0] + ": [REDACTED]", text)
    # Keep Markdown chunks compact; long descriptions are still local but should not explode context packs.
    if len(text) > 6000:
        text = text[:6000].rstrip() + "\n… [truncated locally for memory chunk size]"
    return text


def yaml_quote(s: str) -> str:
    return json.dumps(s or "", ensure_ascii=False)


def event_key(event: dict[str, Any]) -> str:
    raw = str(event.get("id") or event.get("summary") or json.dumps(event, sort_keys=True, ensure_ascii=False))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def event_importance(event: dict[str, Any]) -> float:
    text = " ".join(str(event.get(k, "")) for k in ("summary", "description", "location")).lower()
    score = 1.0
    keywords = [
        "마감", "제출", "시험", "면접", "수강료", "납부", "접수", "과제", "회의", "발표",
        "deadline", "exam", "interview", "submit", "payment", "meeting",
    ]
    if any(k in text for k in keywords):
        score += 0.7
    start = parse_event_dt(str(event.get("start", "")))
    days = (start.date() - now().date()).days
    if -1 <= days <= 14:
        score += 0.4
    if event.get("description"):
        score += 0.2
    return round(min(score, 3.0), 2)


def fetch_calendar(start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
    # google_api defaults to a small max result count; request enough events so a broad
    # collection window does not silently stop before upcoming schedules.
    # Network/DNS can be briefly unavailable on this machine while Wi-Fi/extender setup
    # is changing, so retry before failing the scheduled job.
    cmd = [sys.executable, str(GAPI), "calendar", "list", "--start", iso_zoned(start), "--end", iso_zoned(end), "--max", "250"]
    last_error = ""
    for attempt in range(1, 4):
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=120)
        if proc.returncode == 0:
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"calendar list returned non-JSON: {proc.stdout[:500]}") from e
            if not isinstance(data, list):
                raise RuntimeError(f"calendar list returned unexpected payload: {type(data).__name__}")
            return data
        last_error = proc.stderr or proc.stdout
        if attempt < 3:
            time.sleep(10 * attempt)
    raise RuntimeError(f"calendar list failed after retries: {last_error}")


def write_event_note(event: dict[str, Any], collected_at: str) -> tuple[Path, bool, dict[str, Any]]:
    key = event_key(event)
    start = parse_event_dt(str(event.get("start", "")))
    end = parse_event_dt(str(event.get("end", "")))
    summary = sanitize(str(event.get("summary") or "제목 없음"))
    location = sanitize(str(event.get("location") or ""))
    description = sanitize(str(event.get("description") or ""))
    status = sanitize(str(event.get("status") or ""))
    importance = event_importance(event)
    month = start.strftime("%Y-%m")
    path = EVENTS_DIR / f"{key}.md"
    tags = ["source/google-calendar", "calendar-event", f"month/{month}"]
    if importance >= 1.7:
        tags.append("important")
    body_lines = [
        "---",
        "type: calendar_event",
        "source: google-calendar",
        f"event_id_hash: {yaml_quote(key)}",
        f"summary: {yaml_quote(summary)}",
        f"start: {yaml_quote(iso_zoned(start))}",
        f"end: {yaml_quote(iso_zoned(end))}",
        f"status: {yaml_quote(status)}",
        f"importance: {importance}",
        "tags: [" + ", ".join(yaml_quote(t) for t in tags) + "]",
        f"collected_at: {yaml_quote(collected_at)}",
        "---",
        "",
        f"# {summary}",
        "",
        "## 일정 정보",
        f"- 시작: {iso_zoned(start)}",
        f"- 종료: {iso_zoned(end)}",
        f"- 상태: {status or 'unknown'}",
        f"- 중요도: {importance}",
    ]
    if location:
        body_lines.append(f"- 위치: {location}")
    body_lines.extend([
        "",
        "## 로컬 메모리 요약",
        f"- {start.strftime('%Y-%m-%d %H:%M')} — {summary}",
    ])
    if description:
        body_lines.extend(["", "## 설명", description])
    body_lines.extend([
        "",
        "## 수집 원칙",
        "- Google Calendar만 수집한 로컬 우선 메모리 청크입니다.",
        "- 메일/문서/드라이브 내용은 포함하지 않습니다.",
    ])
    content = "\n".join(body_lines).rstrip() + "\n"
    existed = path.exists()
    old = path.read_text(encoding="utf-8") if existed else None
    path.parent.mkdir(parents=True, exist_ok=True)
    if old != content:
        path.write_text(content, encoding="utf-8")
        changed = True
    else:
        changed = False
    meta = {
        "key": key,
        "summary": summary,
        "start": iso_zoned(start),
        "end": iso_zoned(end),
        "month": month,
        "path": str(path),
        "importance": importance,
        "changed": changed,
    }
    return path, changed, meta


def write_month_indexes(items: list[dict[str, Any]], collected_at: str) -> list[Path]:
    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_month[item["month"]].append(item)
    written: list[Path] = []
    MONTHS_DIR.mkdir(parents=True, exist_ok=True)
    for month, events in sorted(by_month.items()):
        events.sort(key=lambda e: e["start"])
        path = MONTHS_DIR / f"{month}.md"
        lines = [
            "---",
            "type: calendar_month_index",
            "source: google-calendar",
            f"month: {yaml_quote(month)}",
            "tags: [source/google-calendar, calendar-month-index]",
            f"collected_at: {yaml_quote(collected_at)}",
            "---",
            "",
            f"# Google Calendar {month}",
            "",
            f"- 수집 시각: {collected_at}",
            f"- 이벤트 수: {len(events)}",
            "",
            "## 일정 목록",
        ]
        for e in events:
            rel = Path(e["path"]).relative_to(MEMORY_ROOT).with_suffix("")
            lines.append(f"- {e['start']} → {e['end']} | 중요도 {e['importance']} | [[{rel.as_posix()}|{e['summary']}]]")
        content = "\n".join(lines).rstrip() + "\n"
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            written.append(path)
    return written


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_indexer() -> str:
    proc = subprocess.run([sys.executable, str(INDEXER), "index"], text=True, capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"indexer failed: {proc.stderr or proc.stdout}")
    return (proc.stdout or "").strip()


def run_script(path: Path, label: str) -> str:
    proc = subprocess.run([sys.executable, str(path)], text=True, capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"{label} failed: {proc.stderr or proc.stdout}")
    return (proc.stdout or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--past-days", type=int, default=30)
    ap.add_argument("--future-days", type=int, default=120)
    ap.add_argument("--skip-index", action="store_true")
    ap.add_argument("--skip-context-pack", action="store_true")
    ap.add_argument("--skip-daily", action="store_true")
    args = ap.parse_args()

    collected = now()
    collected_at = iso_zoned(collected)
    start = collected - dt.timedelta(days=args.past_days)
    end = collected + dt.timedelta(days=args.future_days)

    events = fetch_calendar(start, end)
    items: list[dict[str, Any]] = []
    changed_files: list[str] = []
    for event in events:
        path, changed, meta = write_event_note(event, collected_at)
        items.append(meta)
        if changed:
            changed_files.append(str(path))
    changed_months = write_month_indexes(items, collected_at)

    state = load_state()
    state.update({
        "last_run_at": collected_at,
        "window_start": iso_zoned(start),
        "window_end": iso_zoned(end),
        "event_count": len(events),
        "event_files_changed": len(changed_files),
        "month_indexes_changed": len(changed_months),
    })
    save_state(state)

    context_pack_output = "skipped"
    if not args.skip_context_pack:
        context_pack_output = run_script(CONTEXT_PACK_SCRIPT, "calendar context pack")

    daily_note_output = "skipped"
    if not args.skip_daily:
        daily_note_output = run_script(DAILY_NOTE_SCRIPT, "daily note ensure")

    index_output = "skipped"
    if not args.skip_index:
        index_output = run_indexer()

    print(json.dumps({
        "ok": True,
        "source": "google-calendar",
        "window_start": iso_zoned(start),
        "window_end": iso_zoned(end),
        "events": len(events),
        "event_files_changed": len(changed_files),
        "month_indexes_changed": len(changed_months),
        "calendar_root": str(CAL_ROOT),
        "state": str(STATE_PATH),
        "context_pack": context_pack_output,
        "daily_note": daily_note_output,
        "index": index_output,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
