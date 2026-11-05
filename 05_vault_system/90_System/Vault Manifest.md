---
schema: hermes.vault-manifest/v1
vault_id: hermes-memory-primary
created_at: 2026-08-14T00:00:00+09:00
provider: obsidian
provider_status: installed_pending_gateway_restart
source_of_truth: markdown
index_status: ready
---

# Vault Manifest

## Paths

- Vault: `/home/hermes/Documents/Codex/Hermes-Memory-Vault`
- 예정된 파생 인덱스: `$HERMES_HOME/obsidian-memory/index.sqlite3`
- 예정된 Provider 설정: `$HERMES_HOME/obsidian-memory/config.yaml`

## Invariants

1. `10_Sources`의 기존 원본은 자동으로 덮어쓰지 않는다.
2. 기억은 하나 이상의 `source_id`를 가져야 한다.
3. SQLite 인덱스는 파생 데이터이며 Markdown에서 재생성할 수 있어야 한다.
4. Hermes는 원자 노트의 관리 마커 내부만 자동 수정한다.
5. 캐시에서 강등된 기억도 Vault에는 남는다.
6. `secret` 자료는 자동 프롬프트 주입과 원격 임베딩에서 제외한다.

## Integration status

- [x] Vault 디렉터리 생성
- [x] 기본 스키마와 템플릿 생성
- [ ] Obsidian에서 폴더를 Vault로 열기
- [x] Hermes Obsidian Memory Provider v0.1 수직 경로 구현
- [x] 파생 인덱스 생성 및 무결성 검사
- [ ] 자동 큐레이션과 성찰 예약
- [x] 격리 단위·E2E 플러그인 로드 검증
- [ ] 실행 중인 Telegram 게이트웨이 재시작 후 실제 대화 검증
