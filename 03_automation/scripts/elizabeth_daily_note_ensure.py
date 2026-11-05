#!/usr/bin/env python3
"""Create/update Elizabeth local memory daily note in Obsidian.

Silent on success. Prints errors only.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
import re
import sys

KST = timezone(timedelta(hours=9))
ROOT = Path('/home/hermes/Obsidian/Macidian/hermes저장소/엘리자베스/로컬 메모리 트리')
DAILY_DIR = ROOT / 'daily'
CALENDAR_CONTEXT = ROOT / 'sources/google-calendar/오늘-이번주-일정.md'


def today_kst() -> tuple[str, str]:
    now = datetime.now(KST)
    return now.strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d %H:%M KST')


def extract_today_calendar(date_s: str) -> list[str]:
    if not CALENDAR_CONTEXT.exists():
        return []
    text = CALENDAR_CONTEXT.read_text(encoding='utf-8')
    # Prefer the dedicated "오늘 일정" section. The context pack often uses MM/DD
    # ranges, so matching the full YYYY-MM-DD would miss real events and catch
    # metadata such as generated_at/window.
    m = re.search(r'^## 오늘 일정\n(.*?)(?=\n## |\Z)', text, flags=re.M | re.S)
    section = m.group(1) if m else text
    lines: list[str] = []
    for raw in section.splitlines():
        line = raw.strip()
        if not line.startswith('- '):
            continue
        # Exclude context-pack metadata if a fallback ever parses whole text.
        if any(line.startswith(prefix) for prefix in ('- generated_at:', '- source:', '- window:', '- events_in_window:')):
            continue
        lines.append(line)
    # De-duplicate while preserving order.
    seen = set()
    out = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out[:20]


def default_content(date_s: str, updated: str, events: list[str]) -> str:
    event_block = '\n'.join(events) if events else '- 등록된 일정 없음 또는 아직 수집 전'
    return f"""# {date_s}

> 엘리자베스 로컬 메모리 daily 자동 생성 파일입니다. 개인 일일 기록은 별도 개인 일일 노트에 둡니다.

- 생성/갱신: {updated}

## 오늘 일정
{event_block}

## 오늘 기록

## 진행 / 완료

## 공부 / 배움

## 결정

## 내일로 넘길 것

## 연결
- [[_system/calendar-context-pack]]
- [[sources/google-calendar/오늘-이번주-일정]]
"""


def replace_section(text: str, heading: str, body_lines: list[str]) -> str:
    body = '\n'.join(body_lines) if body_lines else '- 등록된 일정 없음 또는 아직 수집 전'
    pattern = rf'(^## {re.escape(heading)}\n)(.*?)(?=\n## |\Z)'
    repl = rf'\1{body}\n'
    if re.search(pattern, text, flags=re.M | re.S):
        return re.sub(pattern, repl, text, flags=re.M | re.S)
    return text.rstrip() + f"\n\n## {heading}\n{body}\n"


def ensure_heading(text: str, heading: str) -> str:
    if re.search(rf'^## {re.escape(heading)}\s*$', text, flags=re.M):
        return text
    return text.rstrip() + f"\n\n## {heading}\n"


def update_existing(text: str, updated: str, events: list[str]) -> str:
    if re.search(r'^- 생성/갱신: .*$', text, flags=re.M):
        text = re.sub(r'^- 생성/갱신: .*$', f'- 생성/갱신: {updated}', text, flags=re.M)
    else:
        text = text.replace('\n', f'\n\n- 생성/갱신: {updated}\n', 1)
    text = replace_section(text, '오늘 일정', events)
    for h in ['오늘 기록', '진행 / 완료', '공부 / 배움', '결정', '내일로 넘길 것', '연결']:
        text = ensure_heading(text, h)
    if '[[sources/google-calendar/오늘-이번주-일정]]' not in text:
        text = text.rstrip() + '\n- [[sources/google-calendar/오늘-이번주-일정]]\n'
    return text.rstrip() + '\n'


def main() -> int:
    date_s, updated = today_kst()
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    path = DAILY_DIR / f'{date_s}.md'
    events = extract_today_calendar(date_s)

    if path.exists():
        old = path.read_text(encoding='utf-8')
        new = update_existing(old, updated, events)
    else:
        new = default_content(date_s, updated, events)

    if not path.exists() or path.read_text(encoding='utf-8') != new:
        path.write_text(new, encoding='utf-8')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f'elizabeth_daily_note_ensure failed: {exc}', file=sys.stderr)
        raise
