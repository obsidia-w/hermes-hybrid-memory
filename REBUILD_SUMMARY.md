# Rebuilt Hybrid Memory System — 구현 요약 및 검증 범위 보고서

## 1. 구현 요약 (구현 완료 vs. 미완료 구분)

기존에 분리되어 있던 **시스템 B (문서 청크 FTS 인덱서)**와 **시스템 C (원자 기억 저장소)**의 핵심 저장·색인·검색 로직을 `src/` 패키지와 독립 실행형 CLI로 재구현했습니다. 다만 **실제 Hermes Agent 런타임 자동 연동**과 **야간 성찰 크론**은 아직 후속 통합 작업이 필요한 프로토타입 상태입니다.

### 구현 및 테스트 완료 항목 (`src/` + CLI)
1. **단일 볼트 & 프로필 분리 (`src/config.py`)**:
   - `20_Memories/{profile}/`와 공통 프로필(`shared`, `global`)을 분리 조회합니다.
   - `04_configuration/rebuild-memory.config.yaml` 예제 설정을 기본으로 제공하며, `--vault` / `--profile` CLI 플래그 및 `HERMES_MEMORY_VAULT` / `HERMES_MEMORY_PROFILE` 환경변수로 재정의할 수 있습니다.
2. **3중 사용자 편집 보호 (`src/protection.py`, `src/store.py`)**:
   - `user_locked: true` 또는 `source: manual` 노트는 `correct()` 시 `PermissionError`로 수정을 차단합니다.
   - `## 사용자 수동 메모` / `User Notes` / `Manual Notes` 섹션은 `correct()`로 새 노트를 생성할 때 승계됩니다. *(잠금되지 않은 노트의 임의 외부 편집 자동 병합은 범위 밖입니다.)*
   - 정정 시 기존 노트를 덮어쓰지 않고 `status: superseded`, `superseded_by` 메타데이터를 남기는 비파괴 이력 관리를 적용했습니다.
3. **8대 카테고리 체계**:
   - `preference`, `constraint`, `decision`, `fact`, `insight`, `plan`, `status`, `entity`
4. **비동기 작업 큐 기반 추출 파이프라인 (`src/extractor.py`, `src/db.py`)**:
   - `capture_turn()` 호출 시 대화 응답 경로에서 외부 LLM 호출을 기다리지 않고 SQLite `sources`·`jobs` 테이블에 작업을 등록합니다. *(디스크 SQLite 기록 자체는 동기 실행됩니다.)*
   - `process-jobs` 실행 시 API 키가 있으면 `extractor_model`(기본 `gpt-4o-mini`)을 호출하고, 없으면 결정적 규칙 기반 fallback 추출기로 동작합니다.
   - 비밀키(`API_KEY`, `Bearer`, `sk-` 등) 및 프롬프트 인젝션 패턴 감지 시 큐 등록을 사전 차단합니다.
5. **하이브리드 인출 & 검색 보완 (`src/retriever.py`)**:
   - 토큰 예산 내에서 `[핵심 제약 및 선호]` + `[관련 원자 기억]` + `[문서 청크]`를 조립합니다.
   - SQLite FTS5(`unicode61`) 정확/접두사 매칭(`"token"*`) + 기본 한국어 조사 제거 + 미충족 시 `LIKE` 부분 문자열 fallback으로 한국어 조사(`SQLite를`) 및 복합어 부분 일치(`비동기큐설계`) 검색을 보완했습니다. *(형태소 분석기 및 벡터 검색은 미구현)*
6. **관리 CLI (`src/cli.py`)**:
   - `status`, `reindex`, `search`, `remember`, `correct`, `capture`, `process-jobs` 서브커맨드를 제공합니다.

### 미완료 / 프로토타입 항목
- **Hermes Agent 자동 연동 (`src/provider.py`)**: `RebuiltObsidianMemoryProvider`는 초안 어댑터 클래스이며, Hermes Agent의 `MemoryProvider` ABC 상속, `plugin.yaml`/`register()` 등록, `sync_turn()` 계약 및 실제 데몬 런타임 통합은 아직 완료되지 않았습니다.
- **야간 성찰 / 모순 감지 워커 (`reflection_model`)**: 설정 스키마에 필드만 존재하며 실행 코드는 미구현입니다.
- **시맨틱(벡터)/그래프 검색**: 현재 키워드·조사 완화·부분 일치 기반 검색만 구현되어 있습니다.

---

## 2. 테스트 및 검증 범위

macOS 로컬 환경(Python 3.9.6 및 Python 3.11.15)의 격리된 임시 볼트에서 8개 단위/통합 테스트를 실행해 모두 통과했습니다. (Windows/Linux 환경 및 실제 Hermes 데몬 연동은 미검증)

```bash
$ PYTHONPATH=. uv run --with pytest --with pyyaml pytest -v tests/test_rebuild_system.py

tests/test_rebuild_system.py::test_user_protection PASSED                          [ 12%]
tests/test_rebuild_system.py::test_immutable_supersede_lifecycle PASSED            [ 25%]
tests/test_rebuild_system.py::test_hybrid_indexing_and_retrieval PASSED            [ 37%]
tests/test_rebuild_system.py::test_zero_latency_queue_extraction PASSED            [ 50%]
tests/test_rebuild_system.py::test_secret_filtering PASSED                         [ 62%]
tests/test_rebuild_system.py::test_korean_particle_and_partial_match_retrieval PASSED [ 75%]
tests/test_rebuild_system.py::test_user_manual_section_preserved_on_correct PASSED [ 87%]
tests/test_rebuild_system.py::test_config_override_and_cli_roundtrip PASSED        [100%]
```

---

## 3. CLI 주요 사용법 (임시 볼트 재현 예시)

```bash
DEMO_VAULT=/tmp/hermes-memory-demo

# 상태 확인
PYTHONPATH=. uv run --with pyyaml python3 -m src.cli --config 04_configuration/rebuild-memory.config.yaml --vault "$DEMO_VAULT" --profile demo status

# 수동 불변 규칙 추가
PYTHONPATH=. uv run --with pyyaml python3 -m src.cli --vault "$DEMO_VAULT" --profile demo remember "테스트 통과 전 커밋 금지" --kind constraint --lock

# 턴 캡처 및 백그라운드 큐 처리
PYTHONPATH=. uv run --with pyyaml python3 -m src.cli --vault "$DEMO_VAULT" --profile demo capture --user "앞으로는 SQLite를 선호해." --assistant "알겠습니다."
PYTHONPATH=. uv run --with pyyaml python3 -m src.cli --vault "$DEMO_VAULT" --profile demo process-jobs

# 전체 볼트(원자 기억 + 문서 청크) SQLite FTS5 인덱싱 및 검색
PYTHONPATH=. uv run --with pyyaml python3 -m src.cli --vault "$DEMO_VAULT" --profile demo reindex
PYTHONPATH=. uv run --with pyyaml python3 -m src.cli --vault "$DEMO_VAULT" --profile demo search "SQLite" --max-tokens 1000
```
