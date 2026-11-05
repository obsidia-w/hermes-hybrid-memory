---
schema: hermes.schema-document/v1
describes: hermes.source/v1
---

# Source Note Schema

## Required frontmatter

| Field | Description |
|---|---|
| `schema` | 항상 `hermes.source/v1` |
| `id` | `src-<uuid>` 형식의 안정적인 ID |
| `kind` | `conversation_turn`, `document`, `artifact` |
| `captured_at` | ISO 8601 시각 |
| `content_sha256` | 원본 무결성 해시 |
| `sensitivity` | `public`, `private`, `sensitive`, `secret` |
| `immutable` | 자동 덮어쓰기 금지 여부 |

대화 원본에는 가능할 때 `session_id`, `turn_number`, `platform`도 기록한다.

## Rules

- 기존 Source 노트는 자동으로 수정하지 않는다.
- 매우 큰 도구 출력은 `10_Sources/Artifacts`에 분리하고 상대 링크와 해시를 기록한다.
- 같은 멱등성 키 또는 동일 해시가 이미 있으면 중복 원본을 만들지 않는다.
