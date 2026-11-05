# Memory System Rebuild — 작업 자료

기존 B/C 메모리 시스템을 재설계하기 위한 코드/문서 스냅샷.

## 읽는 순서
1. REBUILD_BRIEF.md — 사용자 목표와 현재 문제
2. 06_original_design/Obsidian-Memory-Architecture-v0.1.md — 원래 설계(구현 완료 증거 아님)
3. 01_current_provider/obsidian/README.md — 현재 C 구현
4. 02_local_memory_tree/_system/로컬 메모리 시스템 v1.md — B 운영 문서(일부 설명은 과거 상태)
5. 04_configuration/ — 현재 프로필의 메모리 관련 설정만 추출
6. 03_automation/ — 스크립트/선별 cron/Calendar 헬퍼
7. 05_vault_system/ — 현재 정책/스키마/템플릿
8. 07_hermes_core_reference/ — Hermes 접점 코드·문서·테스트 참고본
9. 08_database_schemas/ — 실제 DB의 스키마만, 데이터 없음

## 구분
- 01: 현재 설치된 provider 원본
- 06/original_implementation: 최초 개발본 및 테스트, 현재 코드와 다를 수 있음
- 07: 현재 소스 체크아웃 참고본, 별도 설치 의존성/완전 실행환경 아님

## 제외
대화 원문, 개인 메모리/공부/운동 기록, 캘린더 이벤트,
실제 SQLite DB/WAL/SHM, 인증 토큰, .env 전체, config.yaml 전체,
감사 로그, 캐시, 바이너리, 백업 아카이브는 넣지 않았다.
기존 절대 경로는 소스에 남아 있으므로 테스트 실행 전 격리해야 한다.

MANIFEST.json은 원본 위치/복사 방식/크기/SHA-256을 기록한다.
이 묶음은 바로 설치되는 새 프로그램이 아니라 재개발에 필요한 자료다.
