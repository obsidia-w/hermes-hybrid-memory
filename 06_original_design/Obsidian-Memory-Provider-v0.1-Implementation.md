# Obsidian Memory Provider v0.1 — Implementation Report

작성일: 2026-08-14

## 설치 상태

- 활성 Hermes 프로필: `elizabeth`
- 활성 제공자 설정: `memory.provider: obsidian`
- 플러그인: `$HERMES_HOME/plugins/obsidian/`
- Provider 설정: `$HERMES_HOME/obsidian-memory/config.yaml`
- 파생 인덱스: `$HERMES_HOME/obsidian-memory/index.sqlite3`
- Vault: `/home/hermes/Documents/Codex/Hermes-Memory-Vault`
- Hermes CLI 인식: installed, available, active
- 실행 중인 Telegram 게이트웨이: 재시작 승인 전이므로 아직 이전 프로세스 상태

## 구현된 수직 경로

1. 완료된 대화 턴을 append-only Source Markdown으로 저장
2. 안정적인 ID, SHA-256, 세션·턴 출처 기록
3. SQLite 작업 큐와 FTS5 파생 인덱스
4. 명시적 사용자 선호·프로젝트 사실의 보수적 로컬 추출
5. 원자 기억 노트, 개체 링크, 근거 링크 생성
6. 중복 병합, 정정 시 `superseded`, 논리적 망각 시 `inactive`
7. 질문 전 자동 FTS 인출과 제한된 기억 패킷 생성
8. 고신뢰 기억을 Provider 소유 `obs:` 항목으로 `MEMORY.md`·`USER.md`에 자동 승격
9. 내장 memory 도구 쓰기를 Vault에 미러링
10. `obsidian_memory` 단일 도구: search, remember, inspect, correct, forget
11. 상태·진단·재색인·큐레이션·성찰 CLI
12. 원자적 파일 쓰기, 심볼릭 링크 차단, 비밀·인젝션 추출 차단

## 검증 결과

- Python `unittest`: 7/7 통과
- 플러그인 탐색기 E2E: `obsidian` 발견·로드·도구 등록 통과
- `hermes memory status`: installed / available / active
- `hermes obsidian doctor`: 모든 검사 통과
- SQLite `PRAGMA integrity_check`: OK
- 설치본과 검증 소스 SHA-256 일치 확인

## 아직 남은 v0.1 작업

- 실제 Hermes 대화 한 턴을 통한 운영 환경 캡처 검증
- Telegram 게이트웨이 재시작 후 제공자 로딩 확인
- Hermes cron을 통한 일일·주간 성찰 예약
- 규칙 기반 추출을 보강할 모델 보조 큐레이터
- 선택적 로컬 임베딩 의미 검색
- 큰 도구 결과를 Artifact로 분리하는 경로

재시작 전에도 설정과 파일은 보존되어 있으며, 다음 자연스러운 게이트웨이 시작 시 자동 적용된다.
