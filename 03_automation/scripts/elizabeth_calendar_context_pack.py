#!/usr/bin/env python3
"""Build a compact Calendar-only context pack for Elizabeth local memory."""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Seoul")
MEMORY_ROOT = Path("/home/hermes/Obsidian/Macidian/hermes저장소/엘리자베스/로컬 메모리 트리")
CAL_ROOT = MEMORY_ROOT / "sources" / "google-calendar"
EVENTS_DIR = CAL_ROOT / "events"
OUT_SYSTEM = MEMORY_ROOT / "_system" / "calendar-context-pack.md"
OUT_DASHBOARD = CAL_ROOT / "오늘-이번주-일정.md"


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def parse_dt(value: str) -> dt.datetime:
    if value.endswith("Z"):
        return dt.datetime.fromisoformat(value[:-1] + "+00:00").astimezone(TZ)
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def read_meta(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    meta: dict[str, str] = {"path": str(path), "body": text}
    if not m:
        return meta
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip().strip('"')
        meta[k.strip()] = v
    return meta


def load_events() -> list[dict]:
    events = []
    for path in sorted(EVENTS_DIR.glob("*.md")):
        meta = read_meta(path)
        if meta.get("type") != "calendar_event":
            continue
        try:
            start = parse_dt(meta.get("start", ""))
            end = parse_dt(meta.get("end", ""))
        except Exception:
            continue
        meta["start_dt"] = start
        meta["end_dt"] = end
        try:
            meta["importance_float"] = float(meta.get("importance", "1"))
        except ValueError:
            meta["importance_float"] = 1.0
        events.append(meta)
    return sorted(events, key=lambda e: (e["start_dt"], -e["importance_float"]))


def bullet(e: dict, root: Path = MEMORY_ROOT) -> str:
    start = e["start_dt"]
    end = e["end_dt"]
    rel = Path(e["path"]).relative_to(root).with_suffix("").as_posix()
    time = start.strftime("%m/%d %H:%M")
    if start.date() == end.date():
        end_s = end.strftime("%H:%M")
    else:
        end_s = end.strftime("%m/%d %H:%M")
    summary = e.get("summary", "제목 없음")
    imp = e.get("importance", "1.0")
    return f"- {time}–{end_s} | 중요도 {imp} | [[{rel}|{summary}]]"


def main() -> int:
    today = now().date()
    horizon = today + dt.timedelta(days=7)
    generated_at = now().isoformat(timespec="seconds")
    events = load_events()
    active_or_upcoming = [e for e in events if e["end_dt"].date() >= today and e["start_dt"].date() <= horizon]
    today_events = [e for e in active_or_upcoming if e["start_dt"].date() <= today <= e["end_dt"].date()]
    week_events = [e for e in active_or_upcoming if e not in today_events]
    important = [e for e in active_or_upcoming if e["importance_float"] >= 1.7]

    lines = [
        "# Calendar Context Pack",
        "",
        f"- generated_at: {generated_at}",
        "- source: Google Calendar only",
        f"- window: {today.isoformat()} ~ {horizon.isoformat()}",
        f"- events_in_window: {len(active_or_upcoming)}",
        "",
        "## 오늘 일정",
    ]
    lines.extend([bullet(e) for e in today_events] or ["- 없음"])
    lines.extend(["", "## 이번 주/다가오는 일정"])
    lines.extend([bullet(e) for e in week_events[:20]] or ["- 없음"])
    lines.extend(["", "## 중요 일정 후보"])
    lines.extend([bullet(e) for e in important[:12]] or ["- 없음"])
    lines.extend([
        "",
        "## 사용 규칙",
        "- 이 context pack은 Calendar만 반영한다.",
        "- 메일/문서/드라이브/외부 서비스 내용은 포함하지 않는다.",
        "- 아침 브리핑이나 일정 질문에서 우선 참고한다.",
    ])
    content = "\n".join(lines).rstrip() + "\n"
    OUT_SYSTEM.parent.mkdir(parents=True, exist_ok=True)
    OUT_SYSTEM.write_text(content, encoding="utf-8")
    OUT_DASHBOARD.parent.mkdir(parents=True, exist_ok=True)
    OUT_DASHBOARD.write_text(content, encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "generated_at": generated_at,
        "today_events": len(today_events),
        "week_events": len(week_events),
        "important_events": len(important),
        "system_pack": str(OUT_SYSTEM),
        "obsidian_note": str(OUT_DASHBOARD),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
