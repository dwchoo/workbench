# 중요 로그 CLI 사용성 — 구현 명세

Revision s1 · source: [BRIEF b1](BRIEF.md), [입력 계획](../CLI-UX-PLAN.md).
구현 승인은 별도 approval.json의 정확한 bundle digest와 실제 사용자 요청으로 판단한다.

## 문제와 해결

status의 일반 `unavailable`만으로는 최초 미생성과 조회 실패를 구별할 수 없다.
show는 이미 복원된 기록을 JSON lines로 출력하므로 작업 내용을 읽기 어렵다.
저장 엔진을 바꾸지 않고 CLI에 사실에 한정한 경로 안내와 기록 formatter를 추가한다.

사용자는 처음 조회했을 때 장애인지 단순 미생성 가능성인지 이해하고, Task/command의 본문과
출처를 직접 읽을 수 있어야 한다. 자동화 소비자는 기존 `--json` 응답을 계속 사용할 수 있어야 한다.

## 현재 경계와 기술 상세

- `cli.py:_important_print`의 status/show/불완전 조회 표시가 주 수정점이다.
- `cmd_important_read`의 `_layout(create=False)`와 기존 reader 호출/`complete` 기반 exit는 유지한다.
- `reader.status()`는 `unavailable:[{dbUuid,reason}]`와 `complete`를 반환한다. 실패 reason은 대체로
  busy/unavailable로 축약된다. 이 결과에 새 key/reason을 추가하지 않는다.
- `reader.show()`의 행은 `{entry, sourceDbUuids}`이며 `entry.body`는 facts/interpretations/unknowns다.
  새로운 DB decode나 결과 해석을 만들지 않는다.
- 공유 `_plain`은 한 줄 정리에 재사용한다. 원래 개행을 표시할 때는 CLI 내부에서 줄별로 적용한다.
  공유 helper의 의미를 바꾸지 않으며 별도 제어문자 정책/라이브러리는 추가하지 않는다.

## A. status 표시 계약

사람용 status가 `complete=False`일 때만 `layout.important_logs`의 metadata를 한 번 관측한다.
CLI 내부 optional 표시 인자/작은 helper로 `_important_print`에 전달하며 reader 응답 dict는 변경하지 않는다.
`Path.lstat()` 등 기존 표준 API를 사용하고, 대상은 이미 해석된 전용 경로 하나로 제한한다.

| 관측 | 사람용 안내 | JSON·종료 코드 |
|---|---|---|
| 경로 관측이 FileNotFoundError | 경로 없음 + 처음 사용하는 환경이라면 첫 기록 저장 후 조회 가능 | 기존 응답, exit 1 |
| 경로 존재(빈 디렉터리/active.json 없음 포함) | 조회 불가/부분 결과와 확인된 기존 reason | 기존 응답, exit 1 |
| metadata 관측이 권한/OSError 등으로 실패 | 존재 여부 미확인 + 조회 불가/기존 reason | 기존 응답, exit 1 |
| `complete=True` | 기존 정상 count/bytes/보관 안내 유지; 추가 metadata 조회 없음 | 기존 응답, exit 0 |

이 보충 관측은 현재 경로의 존재 사실뿐이다. 과거 기록 부재, 안전한 초기 상태, 0 DB/0B,
실패 원인의 확정으로 확대하지 않는다. 권한 오류를 false로 숨기는 exists() 판정에 기대지 않는다.
기존 `_layout`의 경로 검증/예외 처리를 약화하거나 조회 때문에 파일/디렉터리를 생성하지 않는다.

경로 없음 안내 예시는 다음과 같다. 단어와 줄 배치는 허용 범위 안에서 구현자가 정리할 수 있다.

```text
중요 로그 저장소 경로가 없습니다.
처음 사용하는 환경이라면 첫 작업 기록이 저장된 뒤 조회할 수 있습니다.
DB 수: 미확인 · 물리 크기: 미확인
저장소를 확인하지 못해 종료 코드 1을 유지합니다.
```

## B. show와 부분 결과 표시 계약

기본 show는 기존 순서대로 각 entry를 다음 구성으로 표시한다.

1. Task/command 구분·ID, entry kind·recordedAt·entryId.
2. 사실, 해석, 미확인 구획. 값이 있는 항목은 모두 표시하며 빈 구획은 생략할 수 있다.
   본문 개행은 줄 경계로 보존한다. 줄 안의 제어문자·공백 정리는 `_plain`의 기존 규칙을 따른다.
3. sourceKind와 존재하는 Task/revision/run/command/message/event 식별자, start/end 범위.
4. sourceGaps의 출처·범위·reason, sourceDbUuids. gap을 정상 source로 합치거나 숨기지 않는다.

사람용 표시에서 codec/byte count/hash 같은 저장 metadata를 전부 나열할 필요는 없다.
필요한 전체 원본 필드는 `--json`에 남는다. 본문을 새로 요약하거나 길이로 잘라내지는 않는다.

`complete=True`의 빈 show/list는 기존 “기록 없음”을 유지한다. 부분 결과가 있으면 정상 행은
보여 주고 실패 안내는 stderr로 보낸다. 실패 행은 `DB <UUID 또는 미확인> · 사유 <기존 reason>`으로
표시한다. 일반 unavailable을 PermissionError 등 더 구체적인 원인으로 추측하지 않는다.
이 공통 경고 formatter의 영향은 status/show/list의 기존 부분 결과 분기에 한정한다.
부분 결과인데 빈 목록이라는 이유로 “기록 없음”이라고 확정하지 않는다.

## 호환성과 불변 조건

- `--json`은 기존 전체 dict의 key/type/value를 그대로 직렬화한다. 새 metadata 관측과 사람용 문구는 없다.
  기존 정상/부분 응답의 stdout/stderr 및 exit 0/1을 유지하고 예외 경로도 별도 정상화하지 않는다.
- `--task`, `--command`, `--limit`, `--page`, `nextAfter` 및 reader의 entry 정렬/페이지 의미는 변경하지 않는다.
- 기본 show JSON lines 소비자에는 영향이 있다. USAGE에 `--json` 전체 envelope의 `entries[]`와
  `entries[].entry`, `sourceDbUuids` 위치를 안내한다. 기존 JSON lines와 동일 출력이라고 약속하지 않는다.
- raw-status의 완료된 표시, list의 정상 본문, summary/DB writer/schema/보관/권한 정책은 변경하지 않는다.
- 조회는 모델·verify·활성화·삭제·유지보수·설정 쓰기를 시작하지 않는다. 기존 readonly reader의
  정상적인 SHM reader mark/필요 빈 sidecar 허용 범위는 그대로이며 모든 I/O 0을 새 요구로 만들지 않는다.

## 수락 기준과 trace

| ID | 관측 가능한 기준 | 구현 | 최종 gate/소유자 |
|---|---|---|---|
| UX-AC-01 | 경로 없음이 확인될 때만 조건부 첫 사용 안내, count/bytes 미확인과 exit 1 유지 | DVL-UX-01 | G-UX-01 / DVL-UX-02 |
| UX-AC-02 | 존재/확인 실패/busy/일반 unavailable/부분 결과를 미생성으로 오판하지 않고 경고 보존 | DVL-UX-01 | G-UX-01 / DVL-UX-02 |
| UX-AC-03 | show의 Task/command·본문 세 구획·출처·gap·DB UUID·개행이 원래 행에 결속돼 표시 | DVL-UX-01 | G-UX-02 / DVL-UX-02 |
| UX-AC-04 | --json 전체 값, exit·채널, 빈 결과·순서·페이지 계약 보존 | DVL-UX-01 | G-UX-03 / DVL-UX-02 |
| UX-AC-05 | 전용 임시 store의 실제 CLI 경로에서 위 표시가 동작하고 조회/격리/정리 경계 유지 | DVL-UX-01 | G-UX-04 / DVL-UX-02 |
| UX-AC-06 | 기본 show 변경과 JSON envelope 전환 방법 안내, 기존 완료 기록/안전 경계 보존 | DVL-UX-01 | G-UX-05 / DVL-UX-02 |

모든 최종 gate는 01의 통합된 표시 기능과 02 자체 검증만 소비한다. 01은 worker quick check로
통합 가능 여부를 확인하며 그 상태를 독립 검증 완료로 표시하지 않는다. 두 티켓은 한 검증 단위다.

## 검증 방법과 재사용

- parser/main을 통한 작은 새 CLI 테스트로 A/B/JSON·exit·채널 계약을 검사한다.
- 독립 test_designer는 이 명세에서 먼저 기대를 정하고 필요한 테스트만 추가한다.
- 전용 임시 store에 Task/command와 gap을 기록하는 기존 fixture 패턴을 이용해 실제 reader와
  제품 parser/main을 거치는 smoke를 수행한다. HTTP Backend/OMP/model 성공으로 확대하지 않는다.
- 기존 `test_storage_status_cli`, `test_storage_status_cli_independent`,
  `test_storage_operations_independent` 및 필요한 direct-reader page 테스트를 이름으로 재사용한다.
- 기록된 실제 모델 Task 시험은 저장 로직이 그대로인 범위에서만 재사용한다.
  새 command 실모델 실행, 모든 고장 matrix, full46/500MB 재검증은 이번 필수 범위가 아니다.
- 새 metadata 경로 조회의 실제 diff는 보안 trigger로 검토한다. 파일 경로 접근 변경이 남으면
  별도 security reviewer와 diff secret scan을 수행한다. 기존 도구가 없으면 not_run/미확인으로 보고한다.
- 필요한 테스트 후 단일 frozen candidate를 독립 리뷰하고, 수정은 영향 범위만 재검증한다.
  마지막에 Root가 결합 후보와 소유 자원 정리를 확인한다. 실제 raw 활성화/삭제는 실행하지 않는다.

예정 명령(개발 승인 뒤, 환경에 맞는 Python 사용):

```sh
PYTHONPATH=src:. python3 -m unittest -v tests.backend.test_important_cli_ux tests.backend.test_important_cli_ux_independent tests.integration.test_important_cli_ux_independent
PYTHONPATH=src:. python3 -m unittest -v tests.backend.test_storage_status_cli tests.backend.test_storage_status_cli_independent tests.integration.test_storage_operations_independent
```

새 테스트 파일은 아직 없다. 실제 model/credential 없이 새 임시 HOME/data/TMPDIR로 검증한다.
AGENTS의 용량 확인·소유 프로세스 정리·종료 후 캐시 정리를 적용하며 보고서/실패 근거를 보존한다.

## 미결정·위임·graph

제품 방향의 미결정은 없다. 구체적인 문구·들여쓰기·private helper 이름은 구현자에게 위임한다.
추가 public API/reader/schema 변경이 필요하면 해당 부분을 멈추고 근거를 보고한다.
현재 모델/도구 실행은 하지 않았으므로 실행 적합성은 앞으로의 검증 대상이다.
[PLAN.json](PLAN.json)이 graph/자원/소유권의 유일한 기준이다. 기존 OPS와 parent graph는 유지한다.
