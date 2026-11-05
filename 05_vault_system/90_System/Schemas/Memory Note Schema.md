---
schema: hermes.schema-document/v1
describes: hermes.memory/v1
---

# Memory Note Schema

## Required frontmatter

| Field | Description |
|---|---|
| `schema` | 항상 `hermes.memory/v1` |
| `id` | `mem-<uuid>` 형식의 안정적인 ID |
| `kind` | `profile`, `preference`, `project`, `decision`, `knowledge`, `episode`, `inference` |
| `status` | `candidate`, `active`, `promoted`, `disputed`, `superseded`, `inactive`, `quarantined` |
| `title` | 사람이 읽을 수 있는 짧은 제목 |
| `created_at` | ISO 8601 시각 |
| `updated_at` | ISO 8601 시각 |
| `confidence` | 0~1 |
| `importance` | 0~1 |
| `stability` | `ephemeral`, `situational`, `stable` |
| `sensitivity` | `public`, `private`, `sensitive`, `secret` |
| `source_ids` | 하나 이상의 원본 ID |

## Managed content

Hermes는 다음 두 마커 사이만 자동 수정한다.

```text
<!-- hermes:managed:start -->
<!-- hermes:managed:end -->
```

마커 밖의 사용자 작성 내용은 보존한다.
