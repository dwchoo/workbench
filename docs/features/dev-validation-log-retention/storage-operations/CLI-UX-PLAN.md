# 중요 로그 CLI 사용성 개선 계획

- 작성일: 2026-10-18
- 상태: claude-wb 자문 반영 계획안. 제품 코드·테스트 구현은 시작하지 않았다.
- 요청: 사용자가 실제 사용 시험의 두 관찰을 개선하는 계획을 claude-wb와 논의하도록 요청했다.
- 기준 구현: `1a7f97d354ee6244ec359645153d9a98ff613337`. 기존 dirty 변경은 별도 보존한다.
- 기존 승인 SPEC/PLAN과 완료된 DVL-OPS-01~03을 덮어쓰거나 다시 열지 않는 후속 제안이다.

## 1. 근거와 목적

claude-wb의 `storage-ops-hands-on-20261018-01` 재개 시험에서는 실제 Worker.default 요청 1회가
`stored`로 완료됐고, receipt digest·Task 출처 연결·기존 source 보존·CLI 조회·임시 자원 정리를
확인했다. 이것은 Task 경로의 실제 사용 근거이며 command 귀속·tool roster 직접 관측·실패 경로의
추가 통과를 뜻하지 않는다.

남은 개선 대상은 다음 두 가지다.

1. `logs important status`의 DB 미생성/조회 불가 안내를 명확히 한다.
2. `logs important show`의 기본 JSON lines를 사람이 읽기 쉬운 기록 표시로 정리한다.

앞선 “사람용 출력 첫줄에 JSON이 섞임”은 stdout/stderr 혼합 관찰로 정정됐다.
별도의 저장·요약 결함으로 취급하지 않는다.

## 2. 확인한 현재 코드

- `cli.py:_important_print`는 status에 한국어 안내를 제공하지만, 불완전 조회의 세부 사유는
  stderr에 JSON 배열로 출력한다. show는 `{entry, sourceDbUuids}` 행을 JSON lines로 출력한다.
- `ImportantLogReader.status()`는 실패를 `busy` 또는 `unavailable`로 줄인다. `unavailable`만으로
  DB 미생성·권한 문제·catalog 이상을 구분할 수 없다.
- `cmd_important_read`는 `complete`에 따라 exit 0/1을 결정한다. `--json`은 전체 reader 응답을
  직렬화하며 기존 기본 show의 JSON lines와도 형식이 다르다.
- show의 본문은 이미 복원된 `body.facts / interpretations / unknowns`이며 출처와 누락 범위는
  `sources / sourceGaps`에 있다. 새 요약이나 DB 재해석이 필요하지 않다.
- 기존 `_plain`은 제어문자를 제거하지만 개행도 합친다. 본문은 줄별로 적용하면 기존 개행을
  살리면서 같은 표시 규칙을 재사용할 수 있다.

## 3. 제안하는 변경 범위

### A. 상태 안내: 확인된 사실만 표시

사람용 status가 불완전할 때에만 중요 로그 전용 디렉터리의 존재를 생성 없는 단일 metadata 조회로
확인하는 작은 CLI helper를 둔다. 경로가 없다고 확인되면 다음처럼 안내한다.

```text
중요 로그 저장소 경로가 없습니다.
처음 사용하는 환경이라면 첫 작업 기록이 저장된 뒤 조회할 수 있습니다.
```

이 관측을 “과거 기록도 없었다”, “DB 0개/0B”, “정상 조회 완료”로 확대하지 않는다.
디렉터리가 존재하거나 존재 확인 자체가 실패하면 미생성이라고 단정하지 않는다.
기존 응답의 `busy` 등 확인된 사유를 표시하고, 더 좁힐 수 없는 경우는 조회 불가/원인 미확인으로 남긴다.
정상 생성된 빈 DB에서 `complete=True`인 경우는 정상 조회로 계속 표시한다.

사람용 불완전 조회의 stderr는 `DB <UUID 또는 미확인> · 사유 <기존 reason>` 형태로 바꾼다.
완료된 정상 결과를 실패 안내로 바꾸거나, 부분 결과의 경고를 숨기지 않는다.
`--json` 경로에는 이 metadata 조회나 사람용 보충 문구를 넣지 않는다.

중요: JSON key/type/value, stdout/stderr 구분, 기존 exit 0/1은 유지한다.
미생성 상태를 exit 0으로 바꾸는 것은 이번 제안에 포함하지 않는다.
안내에 “저장소를 확인하지 못해 종료 코드 1을 유지합니다”를 함께 적어 정상 종료로 오해하지 않게 한다.
reader·DB schema·catalog 판정·상태 API는 변경하지 않는 방향을 우선한다.

### B. show: 원문을 읽기 쉬운 구조로 표시

기본 출력은 entry마다 다음 기존 필드를 표시한다.

- Task/command 종류와 ID, 기록 종류·시각·entry ID
- 사실, 해석, 미확인 본문. 빈 구획은 생략하되 있는 내용은 임의로 재요약하거나 자르지 않는다.
- 출처 종류·식별자·입력 범위, source gap의 범위·사유, source DB UUID

본문 개행은 유지하고 각 줄의 제어문자는 기존 `_plain` 규칙으로 정리한다.
Task/command와 누락 범위가 생략돼 다른 작업이나 완전한 기록으로 오해되지 않게 한다.
순서, `--limit`, `--page`, `nextAfter`, 부분 결과 경고와 종료 코드는 그대로 둔다.
`--json`은 현재의 전체 응답 envelope를 그대로 유지한다.

기존 기본 show의 JSON lines를 파싱하던 스크립트에는 변경 영향이 있다.
USAGE에서 `--json`의 `entries[].entry`와 `sourceDbUuids`를 사용하는 방법을 안내한다.
`--json`을 기존 JSON lines와 동일한 형식이라고 설명하지 않는다.

## 4. 개발 순서와 최소 검증

한 구현 단위에서 A→B→USAGE 안내를 반영한 뒤 관련 변경을 묶어 독립 테스트·리뷰한다.
예상 수정 범위는 `src/workbench/backend/cli.py`, 관련 CLI/작은 integration 테스트와 USAGE다.
공유 helper 수정이나 reader 변경이 필요해지면 먼저 원인과 범위를 다시 확인한다.

최소 검증은 다음으로 한정한다.

1. 없는 저장소: 사람용 안내와 기존 exit 1, `--json` 응답 유지, 조회 후 경로/DB 미생성.
2. 정상 빈 DB·정상 기록: 정상 조회 유지, DB 수/bytes를 추정하지 않음.
3. 기존 조회 실패/부분 결과: `busy`·일반 unavailable을 미생성으로 오표시하지 않고 사유/exit 보존.
4. Task와 command의 작은 fixture: show의 ID·본문 세 구획·출처·gap·DB UUID 및 개행 표시.
5. show의 빈 결과·기존 page 경계·부분 결과와 `--json` 전체 구조/값의 기존 계약 보존.
6. 전용 임시 store를 사용한 실제 CLI smoke에서 조회에 따른 기존 허용 read 동작과 소유 자원 정리 확인.

관련 기존 테스트를 재사용하고 변경된 CLI 범위만 추가 확인한다. 기존 실제 모델 시험은
요약·저장 로직이 바뀌지 않는 범위에서 재사용하며 새 유료 모델 요청이나 500MB 검증을 필수로 삼지 않는다.
개발 시 새 후보에 대해 필요한 독립 테스트·리뷰와 최종 결합 확인을 완료한다.

## 5. 범위와 완료 조건

두 사용성 개선과 JSON/exit 호환성, 임시 환경 정리, 독립 리뷰의 유의미한 지적 해소가 완료 조건이다.
모델·skill·writer·작업 귀속·no-replay·실패 보호·보관 정책·예산은 바꾸지 않는다.
C2 보류와 실제 raw 순환 활성화·삭제 비활성도 유지한다.
이번 계획은 구현·실제 삭제·raw 활성화 승인이 아니다.

## 6. 자문 반영

claude-wb 자문 `storage-cli-ux-plan-20261018-01`을 Herdr의 동일 세션
`09935be2-0c32-4a1e-b4df-c5b15a829d53`에 inline으로 의뢰하고 완료 답변을 확인했다.
질문 packet이나 mirror 파일은 만들지 않았다. 반영한 내용은 다음과 같다.

- 기존 reader의 `unavailable`만으로는 미생성을 확정할 수 없다. 기존 디렉터리에서 active.json이
  없거나 busy인 경우를 미생성으로 오판하지 않는 검사가 필요하다.
- DB 수와 물리 크기의 “미확인” 표시는 유지한다. 미생성 안내가 0B 추정이나 성공 판정이 아니다.
- show는 기존 `{entry, sourceDbUuids}` 구조에서 필드만 꺼내 표시한다. JSON/exit와 page 계약은 보존한다.
- 두 변경을 한 구현 단위로 묶어 요구 기반 독립 테스트와 fresh review를 한다.
  저장 경로의 실제 모델 시험은 재사용하며 출력 개선 때문에 재호출하지 않는다.
- 기본 show 출력의 스크립트 호환성 안내가 필요하다. list/show의 미생성 상태 의미까지 바꾸는
  작업은 이번 status 안내 범위에 추가하지 않는다.

자문은 본문 fact의 단일 행 표시를 최소안으로 권했다. 이 계획은 사람용 본문 가독성을 위해
줄별 `_plain` 적용으로 개행을 보존하는 안을 제안한다. 별도 제어문자 정책이나 표시 라이브러리는 만들지 않는다.
자문 자체를 사용자 승인이나 gate 통과로 취급하지 않는다.

## 7. 읽기 전용 확인 manifest

아래 8개 source/test의 SHA-256을 자문 확인 전후 대조한다. 이 계획 작성은 이 파일들의 bytes를 바꾸지 않는다.

| 파일 | SHA-256 |
|---|---|
| `src/workbench/backend/cli.py` | `e88673712eaabc92c41357de48f47a7fc17b6c7393a53d569a715a69c3814b2a` |
| `src/workbench/storage/important_log/reader.py` | `64aa0d7ae11cdc5f13578522c1919c4b81b50e83eeee7ff77a1465a2feada2e4` |
| `src/workbench/storage/important_log/store.py` | `4a60b43b168769f01b83d2d6b8409ea2f5baf21dbca5204d93a77c081667e9ca` |
| `src/workbench/contracts/logs_v1.py` | `22c8baf98340774af3ea5679a8f5c8386789dc6a7e72e62a893276b07ebe8b25` |
| `src/workbench/ui/status_workbench/projection.py` | `c48987c2e01cb41f12463831f704e1d5b9bc9b9151196bf58d9bc5d9e43aaedf` |
| `tests/backend/test_storage_status_cli.py` | `dafc128b5b6d66fe5dac026abb072e13c7dbf12ee79edd9f1a7298e5f0817f04` |
| `tests/backend/test_storage_status_cli_independent.py` | `e4ff3ef650bd4a72b2a8c43f0b6803c814a30ba4bb1038584f20793de6391e56` |
| `tests/integration/test_storage_operations_independent.py` | `07e568eb1c8cc6f9f6e87136884b5448dffd846c77b863b234ee8b58923b74a0` |
