---
schema: hermes.schema-document/v1
describes:
  - hermes.entity/v1
  - hermes.reflection/v1
---

# Entity and Reflection Schema

## Entity

개체는 `id`, `entity_type`, `canonical_name`, `aliases`, `status`, `source_ids`, `related_ids`를 가진다.

`entity_type`은 `person`, `project`, `tool`, `concept` 중 하나다.

## Reflection

성찰은 사실이 아니라 가설일 수 있으므로 다음을 반드시 가진다.

- `status: candidate`
- `confidence`
- `source_ids`
- `created_at`, `updated_at`
- 검증 전 자동 승격 금지 표시
