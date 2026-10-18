# CLI UX 후속 계획 검토

- 작성일: 2026-10-18. Root의 정적 계획 검토이며 독립 코드 리뷰나 제품 gate pass가 아니다.
- 입력: ../CLI-UX-PLAN.md 및 ../BRIEF.md. 기존 glossary를 재사용했고 ADR/제품 결정은 추가하지 않았다.
- bundle: `310e99dbed84189d7779084c5f2c3e8501e7e45fbd6b1ae28ac24711a6f46b21` (approval.json이 나열한6개 파일; approval/review는 hash 바깥).
- 요구사항 identity: `a4a96a2dfd9afc9d0e78926205c107d669dc3571e961b9fd44431d3a4f3dd756`.

## 검토 결과

- DVL-UX-01 → DVL-UX-02의 두 노드, 최초 frontier01. 공통 cli.py 때문에 두 출력 구현을 병렬 분리하지 않았다.
- 각 ticket ID는 유일하고 의존성이 존재한다. 모든 final gate의 owner02와 supplier01/02는 owner 또는 선행 ticket이다.
- 요구사항6개와 gate5개가 SPEC/PLAN/ticket에 연결된다. 변경되지 않는 parent28AC/46gate 및 OPS8AC/7gate는 외부 ref+hash로 보존한다.
- worker 추천은 기존 formatter/parser와 고정 경로 metadata의 bounded_pattern 근거다. 실제 배정/모델은 implement 때 Root가 관측한다.
- Root 소유 final02에서 독립 test_designer, frozen reviewer, 실제 diff trigger에 따른 별도 security review/diff scan을 포함한다.
- 새 helper는 경로 없음의 관측만 표시한다. empty=정상종료/0B, busy=미생성, 일반 unavailable=특정권한오류 같은 추정을 금지했다.
- 기본 show 변경은 의도한 interface 변화다. --json 전체 envelope와 page/exit/채널을 보존하고 migration을 USAGE에 안내한다.
- 실제 모델 Task 사용 시험은 저장 경로의 이전 범위만 재사용한다. 새로운 CLI/static/runtime gate는 구현 후 검증하며 현재 not_run이다.
- 사용자 DB/auth/세션/실제 raw/삭제에 접근하지 않았다. 새 제품/테스트 source, worktree, agent 또는 운영 ledger를 생성하지 않았다.
- 새 계획은 approval 대기 초안이다. 기존 PLAN의 완료 여부나 종전 승인으로 새 구현 권한을 추정하지 않는다.

## 검증 기록

validate_plan.py가 계산한 requirements identity를 PLAN에 결속한 뒤 최종 검증에서 valid=true/errors=[]를 확인한다.
최초 identity 미결속 scaffold의 mismatch는 결속으로 해소했으며 수락 기준을 축소하지 않았다.
공통 bundle-digest helper로 계산하고 approval.json에 기록한 digest를 다시 검증한다.
실제 코드/테스트/모델 실행은 이번 단계의 대상이 아니다.

## 기존 입력 보존 manifest

기준 HEAD: `1a7f97d354ee6244ec359645153d9a98ff613337`. 아래20개 파일은 작성 전후 같은 SHA-256인지 검사한다.
기존 dirty 변경은 유지하며 이 manifest는 전체 repository clean을 주장하지 않는다.

| 파일 | SHA-256 |
|---|---|
| `CONTEXT.md` | `5538ec38de2ceb508974c096ddf8e1d0113a9a14a04eaeaea8974d432934dfbe` |
| `src/workbench/backend/cli.py` | `e88673712eaabc92c41357de48f47a7fc17b6c7393a53d569a715a69c3814b2a` |
| `src/workbench/storage/important_log/reader.py` | `64aa0d7ae11cdc5f13578522c1919c4b81b50e83eeee7ff77a1465a2feada2e4` |
| `src/workbench/storage/important_log/store.py` | `4a60b43b168769f01b83d2d6b8409ea2f5baf21dbca5204d93a77c081667e9ca` |
| `src/workbench/contracts/logs_v1.py` | `22c8baf98340774af3ea5679a8f5c8386789dc6a7e72e62a893276b07ebe8b25` |
| `src/workbench/ui/status_workbench/projection.py` | `c48987c2e01cb41f12463831f704e1d5b9bc9b9151196bf58d9bc5d9e43aaedf` |
| `tests/backend/test_storage_status_cli.py` | `dafc128b5b6d66fe5dac026abb072e13c7dbf12ee79edd9f1a7298e5f0817f04` |
| `tests/backend/test_storage_status_cli_independent.py` | `e4ff3ef650bd4a72b2a8c43f0b6803c814a30ba4bb1038584f20793de6391e56` |
| `tests/integration/test_storage_operations_independent.py` | `07e568eb1c8cc6f9f6e87136884b5448dffd846c77b863b234ee8b58923b74a0` |
| `docs/features/dev-validation-log-retention/storage-operations/USAGE.md` | `a048751803fc09a512b936c1d5929d7ae0429d38a7f75a5b6f1ff1d9ef3e2062` |
| `docs/features/dev-validation-log-retention/storage-operations/PLAN-REVIEW.md` | `0eb3f3c7addde5fc8ae8c211705ca57314f2a05cd1014d2e150699744f5d5483` |
| `docs/features/dev-validation-log-retention/storage-operations/BRIEF.md` | `6c92cb991d06abb2dcf38901b167be6ce5b16abb4fcbcd0dba909f8966aca1b9` |
| `docs/features/dev-validation-log-retention/storage-operations/CLI-UX-PLAN.md` | `bc7720255cd70f255c6e9998ccdc0c7aa1c347055f2b9546fe285c3d075e5623` |
| `docs/features/dev-validation-log-retention/storage-operations/SPEC.md` | `42f19f380a2af83e2a87a8ec24a0b557e3082fab9bf81e707ff4977e1c454fb3` |
| `docs/features/dev-validation-log-retention/storage-operations/PLAN.json` | `3c1143c9155009457e4a67c53b8adee2094611e68caf042c2a1f066508f266a9` |
| `docs/features/dev-validation-log-retention/storage-operations/approval.json` | `59cc1fdd2d51cd1e73a02a2b8f9e00f2e56b8ea6222e4b76e97194713f0a9412` |
| `docs/features/dev-validation-log-retention/storage-operations/requirements.json` | `4e353acf244a9356ff1e78b972a2d3a561daa65c368d36bd2ed6b213125c89a2` |
| `docs/features/dev-validation-log-retention/storage-operations/tickets/DVL-OPS-03.md` | `68ace5506dd907a3a3d1d875717527a904a9e7d8d669aed11d24ed415d6d37ed` |
| `docs/features/dev-validation-log-retention/storage-operations/tickets/DVL-OPS-02.md` | `213fe87831d276f052663948789fc6c959d237c11dcdff384ce9616e7d45aa39` |
| `docs/features/dev-validation-log-retention/storage-operations/tickets/DVL-OPS-01.md` | `2168bdd82f264c6dd75ea26d58d534184ad5364add6774a3347372d5977cbe60` |
