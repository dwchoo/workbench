# 개발용 검증과 로그 보관 — 구현 명세

Revision: s5; source: [BRIEF r0.9](BRIEF.md) — 승인된 r0.8 + 사용자 운영 원칙 개별 합의.
상태: s5/p5 승인 기준 + 2026-10-10 범위 합의에 따른 격리 구현·검증 재개.
현재 권한은 [추가 승인 기록](../../../.workflow/dev-validation-log-retention/runs/implement-p5-20261009/resume-decisions-20261010.json)을 따른다. 아래 승인 전 문구는 당시 이력이다.
기록일: 2026-10-09. 기존 [r0.8 승인 기록](../../../.workflow/dev-validation-log-retention/runs/approval-r0.8/approval.json)과
[추가 운영 결정](../../../.workflow/dev-validation-log-retention/runs/plan-p3-s3-20261009/decisions.json)을 함께 따른다.
PLAN revision p5, requirements revision r0.9-plan-p5-draft. AC 28개·ticket 14개·gate 46개 ID/증거 수준을 유지한다.
Claude `ticket-bundle-s4-p4-final-review-04`의 R1~R3을 승인 전 기술 보강으로 반영하고 R4~R5의 착수 검증을 연결한다.
사용자의 이번 “그렇게 해”는 문서·ticket·새 수신 기록의 변경 권한이며 전체 bundle 승인이나 구현 권한은 아니다.
§13의 측정 후 결정만 남기며 상세 변경 추적은 [계획 보강 기록](PLAN-REVIEW.md)을 따른다.
실제 모델·effort host metadata는 미확인이다. Skill은 모델을 전환하지 않는다.

## 1. 문제와 해결

호스트·OMP 업데이트 때 짧고 재현 가능한 개발 점검이 필요하다. 원문 저장 정지가 완료 판단을
불명으로 만들거나 초기 정보를 잃는 문제를 줄이되, 새 agent·사용자 대화 오염·거짓 완료·자동 replay를
추가하지 않는다. 개발 runner, 수집 중 완료 근거, 별도 중요 로그, 명시적으로 활성화하는 공용 raw rotation을 분리한다.

사용자 관점의 핵심 흐름은 다음과 같다.

- 개발자는 fast로 핵심 경로를 보고 precise로 설치된 outer/shell 및 장애 경계를 확인한다.
- 사용자는 실행을 유지하면서 남은 raw 구간, 결정·보고·누적 요약과 누락을 출처별로 조회한다.
- 중요한 기록은 활성 DB에서 보관·정리하고, 더 이상 정리할 수 없으면 보존 DB를 남기고 새 DB로 전환한다.
- 사용자는 복수 DB 경고를 보고 CLI로 선택한 보존 DB만 정리한다. 실행/승인/완료 근거는 이 정리로 삭제되지 않는다.

## 2. 범위와 권한 경계

BRIEF의 DV-AC-01~05와 LG-AC-01~23 전체를 유지한다. 내부 multiplexer 의존, Phase 2, Windows,
새 agent 정의/상주 OMP, 전체 사용자 대화 수집, 기존 로그 자동 이관/삭제, paid real-model 시험과 VM 재부팅은 제외한다.
개발용 runner는 repo 파일로만 제공하고 pyproject entrypoint나 전역 설치에는 추가하지 않는다.
중요 로그의 조회·보관·raw 정책은 제품 기능이며 개발 runner와 별개다.

Q-01 판별은 DVL-08의 선행 runtime gate다. 기존 허용 agent와 공개 API를 사용한 경로만 자동 채택할 수 있다.
별도 일회성 요약 OMP 또는 TS 직접 completion만 가능하면 그 대안을 보고하고 사용자 채택 전 구현을 확대하지 않는다.
계획 요청으로 probe를 실행하지 않는다. 후속 명시적 implement가 작은 scripted probe와 제품/시험 구현을 허가할 수 있다.
Paid model과 실제 사용자 로그의 raw 삭제 활성화는 그 요청에도 포함되지 않는다.

최초 신규 raw 삭제 활성화는 Q-01 통과 + §8.1의 현재 제품/OMP/host에 유효한 근거 + 사용자의 별도 활성화가 모두 필요하다. 활성화 이후 OMP 버전 변경만의 실행 유지 예외는 §8.1을 따른다.
기본은 legacy-stop이다. 구현 완료나 PLAN 승인, CLI 조회, Q-01 pass만으로 활성화하지 않는다.
사용자 요청에 따라 BRIEF의 운영 원칙을 r0.9로 반영하고 승인본 r0.8 bytes/기록은 보존한다. 기존 core bundle은 변경하지 않는다.

## 3. 관측한 기준선과 연결 지점

2026-10-09 읽기 조사: HEAD 2f21ec91990a409ea0d25f351343b50e34b48196,
원 승인 BRIEF r0.8 SHA-256 9b6632a9688a5c2ca0a3adb2094d4fe1813cb3ca93d4a87dbe4d55663bb9255a. 현재 r0.9 hash는 별도 bundle manifest에 기록한다.
AGENTS·CONTEXT·core BRIEF/FOLLOWUPS/OC 및 feature/operational 파일이 이미 dirty/new였다. 전부 보존한다.
별도 .venv는 없다. Python 3.12.3, OMP 18.8.6, tmux 3.4, herdr 0.9.3은 version-only 관측이며
pyte 가용성, herdr 격리, runtime 호환성과 소요시간은 미검증이다. graphify-out/graph.json은 없어 직접 소스를 대조했다.

| 현재 seam | 관측과 계획상 연결 |
|---|---|
| src/workbench/storage/log_raw/store.py | 소유 경로·0600·quota·fail-closed를 갖춘 append-only RawLogStore. legacy 구현/시험을 유지하고 별도 segmented store를 추가 |
| src/workbench/workflow/run.py | _drain에서 수집한 뒤 종료 시 원문을 읽어 _analyse. 수집 이전 matcher와 영속 근거를 연결; 현재 run의 revision을 고정 |
| src/workbench/backend/flow_terminal.py | _Command.write에 별도 LOG_MAX=64MiB, DONE_INSTRUCTION/log_path 소비. 활성화 후 reader descriptor로 전환하되 legacy 파일은 그대로 읽기 |
| src/workbench/backend/paths.py, service.py | DataLayout.tasks/raw_logs/workflow, Backend의 단일 instance 및 bridge/UI 서버. 새 별도 중요 DB 경로와 서비스 조합점 |
| src/workbench/tasks/repository.py, storage/metadata_sqlite/schema.py | append-only 운영 metadata schema v2. 근거·요약 요청/no-replay 제어 기록은 보존 대상 운영 metadata에 additive migration |
| src/workbench/backend/flow_tasks.py | task_closed는 flow journal, closed/finished/blocked/held가 구분됨. 기록 event에서 보관 종결 사실을 중요 DB에도 복사 |
| src/workbench/backend/automation.py | 프로젝트 paused + Task별 승인/실행 사실. 별도 전역 summary-on 설정을 만들지 않음 |
| src/workbench/backend/launcher.py, omp_bridge/g3/bridge.ts | role skill filter, notice의 idle/composer/pause 검사, main-session sendUserMessage, subagent bridge 거부. 현 경로 자체는 안전한 요약 경로라는 증거가 아님 |
| tests/integration/cw16_harness.py | isolated Sandbox, scripted local provider, owned PID/start-ticks, outer adapters를 재사용 |
| tests/integration/live_cw16_compat.py | plain/설치된 tmux/herdr × Bash/sh(dash), 기본 65초 detach, C1~C7/SEL1~3. LG-D29의 sh 필요 정정을 반영하며 historical 고정 버전 probe와 구분 |

기존 동작 기준은 Core BRIEF C-AC-07/12/21/24/28/29/30, OC §1/5/6 및
[ADR-0001](../../adr/0001-no-external-multiplexer.md)·[ADR-0002](../../adr/0002-managed-shell-control-wait.md)이다.
ADR의 과거 미검증 문구를 현재 runtime pass로 해석하지 않는다. 현 기능은 core 성공/승인 상태를 새로 정의하지 않는다.

## 4. 개발용 runner 계약

제안 인터페이스: PYTHONPATH=src <현재 Python> tools/dev_validate.py --mode fast|precise --report-dir <전용 경로>.
추가 --omp는 설치된 실행 파일을 선택하고, --jobs는 격리 검증을 통과한 셀 수만 제한한다.
생성 환경은 allowlist 방식이다. 사용자 HOME/XDG/PI/WORKBENCH/TMUX/HERDR context·인증을 상속하지 않고
전용 HOME·OMP home·data dir·socket·outer 설정을 만든다. 외부 네트워크를 사용하지 않는 local scripted provider다.
전용 root 밖의 Workbench/socket/DB/세션을 찾거나 shutdown/kill하지 않는다.
공통 harness 변경이 실제 사용자 경로로 새지 않는지 sentinel과 owned identity로 시험한다.

| 모드 | 필수 범위 | 제외/보고 |
|---|---|---|
| fast | runner/harness fixture, launcher/skill filter, 실제 설치 OMP의 isolation/bridge 시작, plain Bash의 세 pane 입력·한글/paste·resize·짧은 host 명령·pause 신규 호출 차단·shutdown | 실제 60초 점검·65초 detach, full outer/shell 장애 matrix는 미검증 |
| precise | fast 의미를 포함한 현 후보 점검, plain 및 설치된 tmux/herdr × Bash/sh(dash) C1~C7/SEL1~3, 대표 60초 Worker 점검·busy 병합·종료 처리, pause/restart/no replay, metadata/raw/중요 DB 장애 | VM 재부팅, 유료 모델, GUI terminal fidelity, 없는 outer/shell은 미검증 |

LG-D29의 최초 dash 제외는 사용자의 같은 날 sh 필요 정정으로 철회됐다. Bash와 sh 경로를 모두 필수 matrix에 포함하며, 이 호스트의 sh 경로는 기존 dash 셀로 판별한다. Bash 우선·`/bin/sh` fallback·shell 부재 진단(SEL1~3)도 유지한다. tmux를 우선하고 설치된 Herdr도 확인한다. 과거 tmux/dash C2 실패와 C2 hold는 그대로 남으며, 이 정정만으로 기존 실패나 미실행 셀을 pass로 표시하지 않는다.

DVL-02는 기존 core matrix를 등록한다. DVL-13이 새 중요 DB·요약·raw 정책·Q01 8개 재판정 항목을
registry에 추가해 precise에서 반복 실행할 수 있게 한다. INT-runtime 일회성 시험으로 등록을 대신하지 않는다.
DVL-08의 독립 개발 명령 `tools/q01_recheck.py`는 01/02 완료를 기다리지 않고 같은 8개 판별을 제공한다.
최종 precise는 이 명령의 product-adapter 모드와 제품 §8.1 검증 서비스를 호출하며 assertion ID·누락·stale를 보고한다.
MODEL-metadata의 실제 configured model 읽기 전용 확인도 등록한다. 이 명시적 개발 검증의 엄격한 pass와
OMP 업데이트 뒤 제품이 계속 실행할 수 있는 정책은 구분하며, 개발 runner를 자동 실행하지 않는다.
Q01 필수 항목을 fail/inconclusive/blocked_adoption인 채 precise success로 집계하지 않는다.

Precise가 같은 후보의 fast 항목을 다시 실행할 때 중복 비용을 기록한다. 같은 invocation에서 후보/환경이 동일한
검사 결과만 재사용할 수 있으며 다른 실행/호스트/버전 pass로 대체하지 않는다.
2분/10분은 목표이고 overall cutoff가 아니다. readiness 응답·UI predicate·종료 event를 기다려 준비되면 즉시 진행한다.
기본 watchdog은 readiness 30초, 일반 scenario 정체 120초, cleanup 20초의 개발 시험 한도다.
시간 자체가 조건인 wait는 별도 expected-wait로 계측한다. 65초 detach/60초 review를 이 watchdog이 잘라내거나
FakeClock으로 대체하지 않는다. 각 assertion의 진행 predicate/deadline을 기록하고 실제 비용에 따라 수치를 조정한다.

결과 schema v1: invocation/attempt ID, input manifest 및 전후 identity, cwd, mode, Python/OS/kernel/OMP 전후 버전,
도구 경로·버전, 환경 격리/자원 namespace, 항목·원래 assertion ID·수집된 시험 수·시간·목표 차이,
passed/failed/inconclusive/not_run/skip + 이유, stdout/stderr의 bounded evidence, 첫 실패 및 이후 attempt 연결.
0건 수집이나 required unknown을 success로 집계하지 않는다.
종합 exit 0=선택 범위 필수 항목 통과(미설치 조합 등 미검증 별도 표시), 1=확인된 실패,
2=필수 미검증/환경·provider/probe 오류 또는 inconclusive. Watchdog은 skip이 아니다.
원인 불명 timeout은 inconclusive, assertion mismatch는 failed. Scripted provider 장애와 제품/OMP 호환 실패를 분류한다.

DVL-01/02에서 항목별 baseline을 측정하고 같은 항목/후보로 전후 비용을 비교한다.
시간 의미 없는 settle sleep, 중복 backend/provider 시작과 중복 suite를 줄인다.
Assertion을 지우거나 retries-until-green으로 정상화하지 않는다. 전체 suite는 별도의 회귀 검사로 유지한다.
[기존 FOLLOWUPS](../core-workbench/FOLLOWUPS.md)의 FL-01~04/RM-01~03은 계획·예산·완료 기준만 연결하고 수행하지 않는다.

## 5. 공통 데이터 계약과 운영 metadata

DVL-03이 Python/TS 공통 JSON v1과 fixture contract를 먼저 제공한다. 이후 ticket은 이 통합 계약을 소비한다.

- SubjectRef: kind=task|command, id; Task 있는 command는 Task에만 귀속한다. SourceRef에 task_id,
  revision, run_id/command_id, source_kind, collector_epoch를 포함한다. ID/형식 검증·path-safe 제한은 기존 계약을 재사용한다.
- SourceRange: source별 단조 byte offset [start,end), collector epoch와 content digest.
  구조화 지시/보고는 message/event ID 및 revision으로 연결한다. 중복 input key는 subject·revision·source ranges/digests·skill digest다.
- RawReadResult: chunks + available ranges + missing/evicted ranges/reasons + source status.
  Rotation으로 사라진 구간을 연속인 것처럼 합치지 않는다. collector 공백과 저장 공백도 구분한다.
- ImportantEntry: entry_id, subject, kind=start|event|cumulative|intermediate|gap|closure,
  facts/interpretations/unknowns, source refs, time, body codec, decoded/stored byte 수 및 origin_db_uuid.
- HealthSnapshot: measured_at와 stale 여부, main/WAL/SHM/합계, logical live/freelist bytes,
  subject 통계·정리 후보, lock/write/maintenance 결과, disk free, 마지막 quick_check.
  운영 metadata/proof/request journal 사용량은 중요 DB 250MB·120일 예산 밖임을 별도 표시한다.
  페이지 배분을 확정할 수 없는 항목은 추정/unknown으로 표시하고 합산을 중복하지 않는다. 자동 정리를 추가하지 않는다.
- SummaryRequest/Result: request_id, subject/revision, authorization ref/epoch, input range key,
  skill name/version=SHA-256, source gaps, prior cumulative entry ref, DB health 및 schema_version.
  Result는 동일 request/ranges에 연결된 facts/interpretations/unknowns/usage를 가진 제한된 구조화 객체다.
  자유 대화 텍스트에서 heuristic으로 result를 긁어오지 않는다.

영속 no-replay/request state 및 원본 보존 의무가 있는 완료 근거는 운영 metadata에 둔다.
중요 DB rotation/정리/삭제로 이를 지우지 않는다. 운영 metadata에 전체 요약 원문·사용자 대화를 복제하지 않는다.
요약 제어 기록이 불능이면 신규 요약 전송을 막고 unknown을 보존한다. 기존 C-D46 hold를 임의 해제하지 않는다.
공통 contract 검증은 모델·raw rotation 구현 없이 fixture에서 가능해야 한다.

## 6. 수집 중 완료 근거

Matcher는 raw 저장 이전의 관측 byte에서 run 시작부터 추적한다. 승인 명세 revision·조건 digest를 고정한다.
KMP 등 incremental exact-byte matcher로 chunk 경계를 처리한다. 기존 log_contains 의미/허용 길이는 축소하지 않는다.
필요한 최소 원문은 최대 4KiB excerpt와 match offsets·full-match/pattern digest·source identity로 기록한다.
긴 패턴은 excerpt만으로 전체가 저장됐다고 표시하지 않으며 승인 명세의 원래 조건과 exact match 사실을 연결한다.

matched는 영속 근거 commit 뒤에만 사용한다. no-match 실패에는 시작부터 최종 drain까지 gap-free 증거가 필요하다.
collector 재시작/빠진 offset은 입증되지 않으면 gap이다. Raw 저장 실패 자체는 관측 공백과 다르다.
관측 불완전 no-match는 indeterminate, 독립 nonzero exit 등 확인된 실패는 failure가 가능하다.
result_file의 경로·변경/출처·내용 및 lifecycle 종료 조건 검사를 유지한다.
요약이나 retained tail만으로 성공을 만들지 않는다. 근거 persistence 실패는 C-D46이며 running 실험은 유지한다.
Legacy run에 새 근거가 없으면 기존 read-based 의미를 보수적으로 유지한다.

## 7. 중요 로그 SQLite·보관 상태

### 7.1 별도 store와 크기

새 private 중요 로그 directory에 DB UUID를 내부 meta에 저장한다. 운영 tasks.sqlite3와 구분한다.
Directory 0700, DB/sidecar/control file 0600 및 소유자·regular/single-link·symlink fail-closed를 적용한다.
SQLite stdlib, foreign keys, WAL, synchronous=FULL, auto_vacuum=INCREMENTAL을 새 DB 생성 시 설정한다.
이전 DB 자동 변환이나 특수 SQLite extension은 없다.

기술 상세의 MB는 1,000,000 bytes다. 목표 250,000,000 bytes, 정리 low-water 목표 225,000,000 bytes.
용량 판정은 committed snapshot의 (page_count - freelist_count) × page_size 논리 live allocation과
현재 저장의 증가량이다. WAL의 변경이 반영된 값으로 읽고 main 크기만 보고 가득 찼다고 하지 않는다.
Physical main+WAL+SHM 합계는 항상 별도 표시한다. WAL busy/reader 때문에 생긴 물리 증가를 정상 full로 오인해
반복 전환하지 않는다. 목표는 hard SQLite max_page_count가 아니다. 한 건/transaction의 제한된 초과를 보고하고 정리/전환한다.

본문은 decoded byte 수·codec version을 함께 저장한다. 4KiB 이상에서 zlib가 12.5% 이상 줄일 때만 압축한다.
읽기는 decoded bound·checksum·잘못된 codec을 검증한다. 압축으로 깨진 조회/통계/복구는 pass가 아니다.
요약 출력 JSON은 UTF-8 전체 envelope 포함 8KiB 이내다. 1MiB는 bounded 수집/후보 자료 상한이며 실모델 전송량으로 채택하지 않는다.
전송 envelope와 model/context/token 상한은 §8.2에서 더 작게 제한한다. 기존 승인 명세 원본은 operating metadata에 보존한다.

### 7.2 종결·정리·이월

Task closed event 시각과 모든 closed reason을 복사한다. done은 별도 Manager 성공 확정으로 바꾸지 않는다.
finished/blocked/held 및 unknown command는 보관상 열린 상태다. Command actual end, 또는 사용자 CLI archival-close
시각이 120일 기산점이다. 같은 대상의 합법적 재활동은 열린 보호로 복귀하고 다음 종결에서 기간을 새로 계산한다.
User archival-close는 core status/exit/실행/pause를 바꾸지 않으며 Agent에 제공하지 않는다.

활성 DB에만 정리 적용: 종결 후 120일 경과 → 용량 압력 시 오래된 종결 기록 →
그래도 필요하면 열린 대상의 오래된 중간 요약 순서다. 시작 정보·최신 cumulative를 우선 보존하고 gap/삭제 범위를 표시한다.
최근 120일 종결 기록도 capacity 압력에서 지워질 수 있다는 BRIEF 경계를 CLI 안내에 포함한다.
보존 DB에는 자동 DELETE·VACUUM 정리를 수행하지 않는다.

각 유지보수 transaction은 초기 최대 100 rows, busy timeout 100ms,
checkpoint PASSIVE 및 incremental_vacuum 최대 256 pages를 낮은 우선순위로 수행한다.
quick_check는 최초 open, 관련 오류 후, 마지막 완료 검사로부터 24시간 경과 시 주기 점검한다.
시간/row/page 한도는 개발 기본값이고 통계와 검사 지연을 표시한다. 미완료 검사나 busy를 ok로 표시하지 않는다.
매 저장은 가벼운 pre/post 크기·free disk·쓰기 결과를 점검하고 통계는 증분+캐시, 조회는 유지보수 trigger가 아니다.
긴 reader를 만들지 않도록 page 단위 응답을 복사한 뒤 connection/transaction을 닫는다.
전용 유지보수 connection의 SQLite progress handler로 중단 가능한 검사를 한정하고 미완료를 기록한다.
Foreground 경로에서 전체 VACUUM을 하지 않는다. SQLite가 DELETE만으로 파일을 줄이지 않는 점과
reader의 WAL checkpoint 제한을 고려한다([PRAGMA](https://www.sqlite.org/pragma.html), [WAL](https://www.sqlite.org/wal.html)).
INCREMENTAL은 작은 유지보수를 위한 설계 선택이다. 전체 재작성 VACUUM은 자동 경로에서 제외한다
([SQLite VACUUM](https://www.sqlite.org/lang_vacuum.html)).

### 7.3 전환·삭제·장애

정리 가능한 것을 소진한 뒤에도 logical live >= target이면 새 UUID DB를 만들고 활성 pointer를 원자 교체한다.
Catalog/pointer generation·fsync·단일 maintenance lease로 동시 쓰기/삭제를 직렬화한다.
생성 → schema/UUID/안전 검사 → 새 active publish → old archive 기록의 recovery marker를 갖춘다.
Crash의 orphan은 UUID/marker로 대조하고 임의 삭제하지 않는다. 불명 활성 identity이면 요약 저장을 보류하고 알린다.
전환 전 TRUNCATE checkpoint는 시도/완료를 기록하되 reader busy 때 WAL을 제거하지 않는다.
Archived connection은 read-only, WAL과 함께 완결성을 유지하며 본체만으로 완결됐다고 속이지 않는다.
Read-only open은 [SQLite WAL의 읽기 전용 조건](https://www.sqlite.org/wal.html#read_only_databases)을 따른다.
미checkpoint archive는 읽을 수 있는 WAL/SHM을 함께 사용하고, 조회로 sidecar 생성·checkpoint·복구 쓰기를 하지 않는다.
Sidecar/권한 조건이 불충족이면 해당 DB를 unavailable로 표시한다. 검증된 checkpoint 완료·writer 없음·보호 lease가
유지되는 sealed archive만 immutable 모드를 사용할 수 있다. Active 또는 미checkpoint DB에 immutable을 붙여
최신 WAL을 무시하지 않는다. DVL-07이 완료/미완료 checkpoint, 읽기 전용 directory, sidecar 없음/손상,
동시 이월·삭제를 시험하고 조회 전후 본체/sidecar hash·파일 목록 불변을 확인한다.

전환마다 모든 열린 대상의 시작/누적 본문을 반복 복사하지 않는다. source DB에 대한 보호 reference를 유지해
통합 조회로 찾고, 삭제 전에 필요한 시작/최신 요약을 남을 DB로 이월·검증한다.
한 신규 저장당 전환 한 번, copy/유지보수는 bounded work로 처리해 recursive rollover를 만들지 않는다.
필수 이월이 안전하게 완료되지 못하면 삭제를 중단하고 영향 대상/이유를 표시한다.
보관 종결 명시가 원본 기록이나 근거를 삭제할 권한은 아니다.

사용자 선택 DB 삭제는 DB UUID로 판정하고 경로/alias로 active refusal을 우회하지 못한다.
전용 backend lease에서 reader 종료·필수 이월 → 삭제 intent 영속 → 본체/sidecar 제거 → recount를 수행한다.
Crash 후 partial deletion을 숨기지 않고 intent를 대조하며 새/활성 DB를 잘못 삭제하지 않는다.
종료된 backend에서는 동일 exclusive instance/maintenance lock을 얻은 명시적 CLI만 삭제한다.

디스크 free < 16MB + bounded 최대 transaction 예산, EACCES/EROFS/ENOSPC/EDQUOT/IOERR,
손상/unsafe path는 저장 장애이며 normal250MB와 구분한다. 기존 실험을 멈추지 않는다.
busy는 짧게 재확인하고 contention/deferred로 표시한다. 안전한 저장을 확인 못 하면 신규 모델 호출은 보류한다.
도착한 결과는 무제한 메모리 보관하지 않는다. 실행 후 저장 실패의 input 범위는 no-replay gap이다.
복구 probe는 최대 30초마다 가벼운 안전/쓰기 transaction 및 필요한 quick_check 완료를 확인한다.
복구가 approval/pause를 풀지 않는다. 다음 5분 주기에 새 허용 input만 대상으로 재개한다.

## 8. 기존 agent·skill과 요약 요청 상태

### 8.1 Q-01 최소 판별

DVL-08이 제품 확대 전에 실제 설치 OMP와 scripted provider로 다음 항목을 각각 판별한다.

| Gate | 가장 작은 관측 | pass / 실패·불명 |
|---|---|---|
| Q01-route | 기존 허용 agent/parent의 격리 한 turn과 결과 회수 | 새 정의/상주 프로세스 없이 명명된 경로. LG-D22의 기존 OMP 프로세스 안 요청별 제한 SDK session은 채택됐으며 기존 agent 정의 prompt/skill 재사용을 검증한다. 그 밖의 일회성/직접 completion 필요는 blocked_adoption |
| Q01-tools | 시작 전 설정·검사 동작, 누락/무효·API 불일치·stale 거부와 한 번의 재생성·재검증, 실행 중 불일치·비허용 tool event | LG-D23: 검증 전 전송0; 실패 시 host가 새 private session으로 한 번 복구하고 다시 실패하면 요약만 보류. 실행 중 불일치는 예방 차단, 위반 결과는 거부·보호. 정상 시작 후 검사 코드의 고의 제거·무력화 내성은 완료 필수 조건에서 제외 |
| Q01-skill | 정확한 skill bytes/digest를 실제 context에 전달 | 확정 전달·role filter·isolation 기대값 일치. 모델 재량 로드만으로 pass 금지 |
| Q01-result | 정상·wrong ID/revision/range·중복 결과 각 한 번 | typed channel 검증, mismatch 거부, free text heuristic 금지 |
| Q01-context | 정상 3회 후 transcript/context delta와 cleanup | 전체 raw가 main transcript에 누적되지 않음; parent receipt 최대 4KiB/회, §8.2의 byte/token/context preflight·8KiB result·실제 enforce output token ceiling |
| Q01-priority | busy/composer/user 입력, due60s check/end와 동시 due 및 요약 실행 중 입력 | session/turn/delivery-lock 공유 여부와 최대 관측 차단 시간 기록. 공유 경로는 측정값의 사용자 채택 전 blocked_adoption; safety 위반은 failed |
| Q01-recovery | pause, disconnect/restart, late result 한 번씩 | 신규 호출 보류, valid late 저장 가능, unknown replay 금지·대조 가능 범위 기록 |
| Q01-origin | Task/command 작업 범위·관련 source/ranges·종료 처리 연결 | LG-D24: 작업 식별과 자료 귀속을 request에 묶음; 기존 Task 승인·종료 후처리·pause 유지. 개별 사용자 발화→command 일대일 증명은 요구하지 않으며 자동 notice/무관 Task/unpaused를 새 작업 권한으로 오인하지 않음 |

Q01-origin은 작업 내용을 이해하고 이어가기 위한 자료 연결을 검증한다(LG-D24).
Task가 있는 command는 해당 Task의 기존 승인·범위와 source/ranges에 연결하고 중복 요약하지 않는다.
Task 없는 command도 기존 core 실행 권한 아래 접수한 command ID·작업 맥락·범위와 관련 자료에 연결한다.
개별 사용자 발화 identity·원문·실행 의도와 각 command의 일대일 증명은 수집하거나 완료 조건으로 요구하지 않는다.
작업 범위 밖 자료나 연결 불명 자료를 임의 귀속하지 않으며 자동 notice·무관 Task 승인·unpaused만으로
새 작업 권한을 만들지 않는다. 기존 direct-instruction API 조사와 실패/미확인은 이력으로 보존한다.

Q01-tools의 현재 완료 경계는 LG-D23이다. 요청마다 private Settings/registry와 전용 DB tool 실제 목록,
요청 결속과 검사 동작을 전송 없이 확인한 뒤에만 prompt를 허용한다. 등록 여부·함수 존재만으로 검사 효력을
대신하지 않는다. 검증 실패 시 host가 기존 session을 폐기하고 신뢰하는 구성으로 한 번 재생성·재검증하며,
다시 실패하면 요약만 보류하고 메인 작업을 계속한다. 복구는 prompt 전으로 한정하고 모델 재호출·정책 수정·
무제한 재시도는 하지 않는다. 정상 시작 뒤 검사 코드를 고의로 제거하는 고장 시험은 한계 기록으로 유지한다.
실행 중 통상적인 roster/model/policy/request 불일치 차단과 비허용 tool event 보호는 계속 필수다.
이전 기준의 실패를 새 기준의 pass로 재표시하지 않고 새 관측으로 판별한다. 나머지 Q01 항목은 바꾸지 않는다.
종료 요약은 종료 event에 연결된 기존 승인 범위의 후처리다. Core Task를 다시 active로 바꾸지 않고
최신 승인/철회/pause를 재대조할 수 있는 종료 후처리 참조를 검증한다. 취소/권한 철회를 새 호출로 우회하지 않는다.
이벤트별 호출 정책과 측정 뒤 필요한 결정은 §8.3 및 §13에 명시한다. 보관 종결과 요약 trigger는 다르다.

핵심 safety/provenance 위반은 failed, 관측 부족/probe 이상/원인 불명 timeout은 inconclusive다.
한 항목이라도 failed/inconclusive/blocked_adoption이면 Q-01 전체 pass가 아니다.
부분 대안 결과는 보존하고 DVL-09/10의 affected expansion 및 DVL-14의 activation을 막는다.
DVL-01~07, disabled raw engine DVL-11, 비활성 주입·조회·reader DVL-12는 독립 진행할 수 있다.
대안 채택 후에는 route/채택 근거를 새로 고정하고 8개 전부 다시 판별한다. 이전 route의 부분 pass를 섞지 않는다.

#### 개발 gate와 제품 runtime 근거의 두 층

개발 check는 실제 전체 candidate의 before/after를 계속 기록한다. 기존 공용 validator의 정확한 candidate 검사를
완화하거나 과거 check의 candidate를 덮어쓰지 않는다. DVL-08이 Q01용 다음 별도 scoped 판정을 제공한다.

- 원 check의 candidate에서 공용 check schema/log hash/8개 required runtime item/requirements identity를 검증한다.
- Q01 projection은 versioned allowlist의 path·존재/부재·파일 mode·bytes digest와 OMP 실행 파일/version,
  host(OS/arch/주요 runtime), route/role/agent 정의 bytes, skill/contract, 모델/context 설정·provider/harness를 포함한다.
  최소 무효화 경계는 src/workbench/backend/**, src/workbench/ipc/**, src/workbench/contracts/**,
  omp_bridge/**, pyproject.toml과 probe/provider/harness/validator bytes다. service delivery lock,
  ipc/bridge_g3/mailbox.py의 전송·실행 판별, automation pause, flow_tasks/flow_terminal origin을 포함한다.
  Adapter/scheduler가 읽는 tasks/storage/workflow의 import closure·설정도 추가한다. 새 파일/동적 import/
  조건부 의존을 해명하지 못하면 동일하다고 추정하지 않고 invalidated다. 경로·closure 수집 정책은
  validator와 함께 독립 검토·version/digest 동결한다. 변경은 재동결 및 영향 근거 무효화를 요구한다.
- 현재 전체 candidate에서 같은 projection을 다시 수집하고 original projection과 비교한다. 관계 없는 UI·DB·문서
  변경은 동일 projection일 때만 재사용한다. allowlist/version 변경·빠진 입력·영향 불명은 invalidated다.
- 동일함이 확인되면 original check와 current candidate를 연결한 별도 revalidation record를 만든다.
  이는 dependency 진입 근거이지 공용 full-candidate gate의 pass로 위장하지 않는다. Final gate는 예외 없이
  DVL-13의 최종 frozen candidate에서 전체 required check를 다시 실행한다.

DVL-08 통합 직후 Root가 통합 후보에서 8개를 한 번 다시 실행한다. DVL-09/10/14 진입 시 projection을
재대조하고 변경·불명이면 8개를 재실행한다. DVL-09 adapter 통합 뒤에는 tools/q01_recheck.py의
--adapter product 모드로 실제 제품 adapter·backend delivery/pause/origin 경계를 사용해 8개를 재판정한다.
09 이전 probe-local은 제품 재판정으로 재사용하지 않는다. 제품 모드는 개발 harness가 제품 코드를 호출하며
제품이 tools/tests를 역으로 import하지 않는다. Golden/음성 계약 일치만으로 runtime 재판정을 대신하지 않는다.
관련 수정은 해당 ticket의 독립 테스트/리뷰에 포함한다. 이 절차의 코드 제공은08, 실행·기록은 후속 implement Root다.

현재 제품은 repo checkout 실행형이다. launcher가 checkout의 omp_bridge 자산을 읽으며 pyproject는 src만
패키징하므로 독립 설치형 배포를 가정하지 않는다. Packaging/자산 경로 이전은 범위 밖이다.
전체 제품 identity는 런타임 집합 src/workbench/**, omp_bridge/**, pyproject.toml의 경로·존재/부재·mode·bytes digest다.
tools/tests/docs/PLAN/.workflow/개발 manifest는 이 집합과 runtime 의존에서 제외한다.
DVL-14의 PRODUCT-evidence는 tools/tests/docs/.workflow 및 개발용 설정을 제거한 checkout 복사본에서
동일 제품 경로를 실행한다. 누락 자산이나 development import는 unavailable이며 원 checkout에서 몰래 읽어 pass하지 않는다.
의존 Python/OMP executable·환경 fingerprint는 별도 기록한다.

DVL-14가 제품 summary_compatibility와 좁은 사용자 CLI logs summary-compat verify --scripted를 제공한다.
일반 fast/precise runner의 제품화가 아니라 최초 활성화 또는 실제 문제 뒤 수동 판별 경로다.
제품에 포함한 local scripted provider를 사용하고 private temp home/project/data/socket의 일회성 검증용
OMP를 종료/정리한다. 기존 agent 정의·skill을 재사용하며 새 agent·상주 프로세스·유료 모델을 추가하지 않는다.
이 일회성 OMP는 검증 대상 환경일 뿐 운영 요약의 새 호출 route를 허가하지 않는다. 운영 요약은 Q01 채택
형태만 사용한다. LG-D22의 요청별 제한 SDK session 채택은 기존 OMP 프로세스 내부 경로에 한정하며,
별도 일회성 요약 OMP/TS direct completion 대안은 여전히 별도 사용자 채택이 필요하다.
Credential/실제 로그를 검증 자식에 전달하지 않는다. 격리를 이 runtime 집합만으로 재현 못 하면 unavailable다.
LG-D27/28에 따라 검증 환경 준비에서 소유한 임시 환경의 빈 private 저장소 생성·열기와 OMP의 정상
시작·모델 목록 갱신 통신을 허용한다. OS 격리·provider blacklist·새 상주 서비스를 추가하지 않는다.
Scripted 모델 요청은 소유 localhost provider로만 처리하고 결과는 임시 DB에 기록한다. 유료 모델 호출·
사용자 인증정보/실제 로그 접근은 준비와 검증 모두 금지한다. 시작·목록 갱신 I/O는 준비 동작으로 분리해
기록하며 observer/SDK child의 무부작용 근거로 합산하지 않는다. 금지 동작이나 격리/귀속 불명이 있으면
pass로 판정하지 않는다. 운영 요약 route와 실제 raw 활성화 권한은 바뀌지 않는다.

제품 record v1은 UUID, schema/검증기 version, 전체 checkout runtime 집합 digest와 별도 관련 closure/policy digest,
OMP binary/version 및 OMP 소유 bundled agent/tool/prompt identity, 안정 host fingerprint와 별도 변동 정보,
Workbench 소유 route/role/agent/skill/contract/model-config digest, 8개 관측+결과+unknowns, evidence/log digests,
adoption ref, 실제 model metadata 관측/출처, created_at를 가진다. 관련 입력·도구·환경의 전후 일치도 검사한다. record와 bounded 로그는
0700/0600 로컬 전용 store에 원자 기록하고 호출자가 같은 owner인지 검사한다. 제품 검증기는 실제 record와
로그·현재 identity·8개 결과·채택 근거를 확인하며 임의 외부 JSON/pass flag를 받지 않는다.
동일 사용자 권한의 고의적 파일 변조를 암호학적으로 방어한다고 주장하지 않는다.

개발 runner는 제품 서비스를 소비하지만 반대 의존은 없다. 개발 gate·최초 활성화는 exact current identity의
8개 pass를 요구한다. 제품 운영의 실행 허용과 검증 pass는 다음처럼 별도 상태로 기록한다.

| 운영 관측 | 신규 요약 / 새 source | 검증·사용자 안내 |
|---|---|---|
| 최초 통과·채택·identity 일치 | Task/command 기존 권한·preflight 아래 호출. Raw는 별도 활성화 없으면 legacy | verified_at/검증 버전 보존. 호환 근거는 요약 권한이나 raw 활성화가 아님 |
| OMP 소유 binary/version/bundled asset만 변경, 관련 non-OMP 경계 동일·문제 미관측 | 매 요청의 유효 guard·preflight 아래 기존 경로·권한으로 요약 유지. Raw는 기존 별도 활성화 결정이 있을 때만 v2 admission 유지 | version_drift_unverified. 기존 pass를 새 버전 pass로 재기록하지 않음. passive status에 검증/실행 버전만 표시, verify 요구 banner/반복 알림/자동 검증 없음 |
| 실제 호환성 문제 확인 | affected 신규 요약 보류, 신규 source legacy-stop, 진행 중 cohort 유지 | compatibility_failed. 사건당 알림1회와 문제·수동 verify 상태 표시; 재시작/화면 재연결로 같은 알림을 반복하지 않음 |
| 관련 closure/skill/route/안정 host/model-config 변경 등 재검증 필요 | affected 신규 요약 보류, 신규 source legacy-stop, 진행 중 cohort 유지 | revalidation_required. 위와 같은 수동 verify 안내 |
| 관련 closure는 동일하나 전체 identity의 무관 UI·문서/변동 host 정보만 변경 | 요청별 안전 검사 아래 계속 실행, cohort 유지 | unrelated_change_observed를 기록. 과거 exact pass를 현재 pass로 바꾸지 않고 요구 알림 없음 |
| guard 확인 불능 또는 반복 계약 결과 거부 | affected 신규 요약 보류, 신규 source legacy-stop, 진행 중 cohort 유지 | 보호 사유·미확인 원인 표시, 사건당 알림1회. OMP 원인 확정과 별개 |
| 사용자의 명시 verify pass | 최신 근거 저장; 기존 승인·unpaused·안전 preflight 아래 admission 회복 | 기존 raw 활성화 결정 없는 경우 계속 legacy. verify가 최초 활성화를 만들지 않음 |
| verify 실패/inconclusive | 영향 범위의 보호 상태 유지 | 원인·unknown 보존. 재시도/수정 요청 없이 자동 verify loop 없음 |

제품 운영 admission은 기존 원본 record의 schema/log hash·8개 pass·채택 근거·원 identity를 먼저 검증한다.
그 뒤 아래 동결 정책의 관련 non-OMP closure와 OMP 소유 변화만을 분리 판정한다. 원본 record가 없거나 불완전하면
version-drift 예외를 적용하지 않는다. Exact current identity 검증기를 완화하거나 새 pass record를 생성하지 않는다.
요약 권한과 raw 활성화 결정은 각각 별도 대조하고, 요청별 최신 모델/context·도구 guard도 유지한다.
Version drift 비교에서 OMP 변화와 함께 실제 모델 metadata·계약 등 다른 boundary 변경이 확인되면
해당 안전 검사를 건너뛰지 않는다. 이전부터 보호 상태였다면 추가 버전 변경으로 보호 상태를 해제하지 않는다.
호환성 실패는 확인된 공개 API/계약 불일치, 도구 차단 불능, provenance/결과 채널 등 필수 안전 경계 불능이다.
확인이 없다는 이유만으로 OMP가 원인이라고 확정하지 않는다. guard_confirmation_unavailable는 즉시 보호로 처리하고
확인된 API/도구 차단 불능은 compatibility_failed로 기록한다. 두 상태 모두 LG-D13의 신규 admission 보호를 적용한다.
일시적 연결/provider/요금 오류·일반 모델 지연·eviction만으로 이 상태를 만들지 않는다. 요청별 오류/unknown과
no replay는 계속 적용한다. 사건 key는 boundary identity+원인(불명이면 해당 코드)+incident episode로 묶는다.
같은 episode의 재시작·재접속은 중복 알림을 만들지 않으며 명시 verify 뒤 재발은 새 episode로 알림1회를 낸다.
수동 verify는 문제를 판별할 뿐 코드를 수정하거나 실행 승인을 갱신하지 않는다. 수정은 별도 요청 범위에서
검증하고 결과를 새 record에 연결한다. 새 agent/권한 확대·paid model 호출은 이 운영 원칙에 포함되지 않는다.

#### 요청별 guard와 운영 identity 정책 (P1~P3)

도구 차단은 LG-D23에 따라 모든 요약 요청의 시작 전에 fail-closed로 검증한다. Drift 상태만의 선택 검사가 아니다.
Host는 private Settings/registry·전용 DB 기록 tool 하나의 실제 read-back과 전송 없는 검사 동작 확인을
GuardReceipt에 결속한다. Hook 등록 성공·함수 존재·skill 약속만으로 효력을 대신하지 않으며 존재하지 않는
native enforcement ack를 주장하지 않는다. Receipt는 request ID·session/turn epoch·route·policy digest·
일회성 nonce에 묶고, 다른 요청의 receipt·누락·API 불일치를 거부한다. 시작 전 실패는 전송0으로 보류하며
실패 session을 폐기하고 신뢰하는 구성으로 한 번 재생성·재검증한다. 다시 실패하면 요약만 보류하고 메인 작업은 계속한다.
실행 중 현재 요청·tool·모델·정책 불일치는 실제 요청을 받는 검사점에서 전송 전에 차단한다. 비허용 event는
결과 거부·즉시 보호하며 종료/실패 때 요약 session을 폐기하고 상위 일반 agent 권한은 유지한다.
추가 모델 turn/canary 호출은 만들지 않는다. 시작 전 검사 누락·미작동·형식 변경·stale 결속의 거부와
복구 성공·반복 실패, 실행 중 불일치를 별도로 관측한다. 정상 시작 후 검사 코드를 고의로 제거·무력화하는
고장 시험은 완료 필수 조건에서 제외하며 기존 실패 기록은 유지한다. 실제 OMP의 정상 경로와 차단 관측은
계속 필요하고 fixture self-check만으로 실제 가용성을 pass하지 않는다. 요청별 시작 검증을 제공할 수 없는
route는 blocked_adoption이며 안전 없이 실행하거나 새 guard API가 있다고 가정하지 않는다.

R1 운영 감시: Guard 설정의 완료/실패를 await하고 같은 epoch의 read-back으로 확인한다. 기존
hideBridgeTools와 같은 fire-and-forget/오류 무시 패턴을 재사용하지 않는다. 이 절차는 앞의 실제효력·장벽
검증을 대체하지 않는다. 요약 request/turn에 귀속된 신뢰 가능한 OMP response/event에서 toolCall/tool_use
또는 비허용 tool 실행을 관측하면 guard 위반으로 즉시 해당 결과를 거부하고 새 요약을 보호한다.
요약용 허용 결과 전달 protocol의 typed result와 상위 agent의 일반 turn은 출처로 확정해 구분한다.
입력 raw에 있는 문자열이나 모델의 “도구를 사용했다”는 자기보고만으로 실행 사실을 확정하지 않는다.
확인된 비허용 tool 실행은 compatibility_failed,호출 시도만 확인됐거나 실행 여부가 불명이면
guard_violation_observed로 기록한다. 두 경우 모두 즉시 보호·LG-D13·사건당 알림1회를 적용하며 추가 모델 호출0.
마지막 정상 요약·가능한 관측/실험은 유지한다. 이미 발생한 부작용을 rollback했거나 예방했다고 보고하지 않는다.
Q01-tools/PRODUCT-evidence에는 receipt는 정상이지만 이후 비허용 tool event가 나오는 음성 대조와 결과거부/
즉시보호·상위 turn 오인0을 포함한다. 사후 감시만으로 예방적 guard의 불능 route를 채택하지 않는다.

운영 재검증에는 DVL-08이 동결한 관련 closure/policy를 제품이 자체 수집한다. 최소 backend/ipc/contracts/
omp_bridge/pyproject 및 실제 tasks/storage/workflow import closure를 유지하며 개발 tools/tests/harness는 제품
의존에서 제외한다. 전체 runtime digest는 provenance/최초 exact 검증용이며 무관 UI 변경을 운영 보류의 원인으로
쓰지 않는다. 경계 영향 불명·누락·관련 새 파일은 invalidated다. 개발 projection/exact candidate에는 DV-D03
완화를 적용하지 않는다. 최초 activation과 명시 verify는 현재 입력 관측을 계속 요구한다.

R3 단일 정책: canonical policy bytes는 제품 소유의 비실행 JSON
src/workbench/contracts/summary_compat_policy_v1.json에 한 번만 둔다(08 deliverable,현재 미작성).
버전·필수 runtime 경로·closure 규칙·OMP/Workbench 소유 구분·안정 host 필드를 담으며 08의 독립 동결 때
정확한 bytes SHA-256을 고정한다. 개발 tool과 제품 collector는 같은 파일을 읽고 각각의 상수를 재작성하지 않는다.
개발 전용 probe/provider/harness 입력은 별도 overlay이며 제품 core policy를 대체하거나 제외할 수 없다.
PRODUCT-evidence와 DV-registry는 제품이 읽은 core policy version/digest와08 동결 digest의 일치를 확인하고
누락/불일치는 invalidated다. backend/ipc/mailbox와 tasks/storage/workflow closure 변경의 동일 판정도 음성
fixture/runtime로 대조한다. 동일 digest만으로 collector가 규칙을 따랐다는 근거를 대신하지 않는다.
제품은 tools/tests/.workflow를 import하거나08의 개발 기록을 실행 시 읽지 않는다. 제품 local record에는 검증한
policy version/digest를 포함하고 현재 canonical bytes가 다르면 revalidation_required다. 변경은 Root의08
bounded follow-up 독립 재동결과 관련 재판정을 요구한다. Packaging 변경이나 새 전역 설정은 도입하지 않는다.
OMP가 소유한 bundled agent/tool/prompt bytes는 OMP identity에 포함해 version-only 예외로 분류하되 변경 사실을
기록한다. Workbench의 omp_bridge/agents·skills·omp-models.yml은 제품 경계다. 소유 불명은 예외를 적용하지 않는다.
안정 host key는 OS 계열·architecture·Python minor다. Kernel/OS patch/Python patch 등은 기록만 하며 단독
재검증 trigger가 아니다. OMP/Python 실행 환경의 실제 API 문제나 관련 경계 변화는 안전 검사로 별도 보호한다.

MODEL-metadata는 요청이 실제 사용할 session의 해석된 model/provider·thinking 수준·service tier를 읽는다.
R4 착수 체크: 공유 route는 현재 session을 조회한다. 요청별 격리 turn/subagent route는 Q01에서 채택한 기존
생성 경로로 대상 context를 준비한 뒤 모델 전송 전에 생성된 context의 해석 결과를 read-back한다.
부모 상속·role 해석의 출처/epoch를 함께 기록한다. 설정 표만으로 해석 결과를 추정하지 않으며 아직 session이
없다는 이유만으로 fallback을 도입하지 않는다. 생성만으로 모델이 실행되는 API라면 이 preflight를 충족한다고
가정하지 않고 Q01에서 제한을 보고한다. 별도 일회성 요약 프로세스의 채택 경계도 그대로다.
modelRoles 표는 시작 기본값의 출처이지 /model·/fast 뒤 실제 선택의 증거가 아니다. Context capacity·tokenizer/
검증 가능한 보수 estimator·현재 context 여유·enforce output ceiling·reasoning 포함 여부/예산 출처를 함께 관측한다.
공개 로컬 OMP 조회 또는 이미 보유한 검증 metadata만 사용한다. Metadata observer는 모델 호출·대화 입력·
네트워크 요청0·auth store open0·설정 쓰기0이며 provider /models나 로그인 경로로 보충하지 않는다.
Credential-free verify는 LG-D27/28의 구간 경계를 따른다. Metadata observer의 위 음성 조건은 그대로이며,
검증용 SDK child의 새 auth store open·설정 쓰기는 금지한다. 소유 localhost scripted provider 통신과
임시 DB 기록은 검증 동작으로 별도 기록한다. 준비의 빈 private 저장소 초기화와 정상 시작·목록 갱신은 observer/child의
무부작용 근거로 합산하지 않는다. 기존 운영 OMP의 정상 모델 호출 인증을 금지한다는 뜻은 아니다.
관측 snapshot은 요청/session/model epoch에 묶고 전송 전 변경이면 다시 확인한다. 비밀값은 기록하지 않으며
검증 자식에는 sanitized model descriptor만 넘긴다. 실제 session을 로컬에서 확인할 수 없거나 offline catalog가
없는 것은 예상 가능한 inconclusive이지 provider 실패/성공이 아니다. LG-D30의 단순 예산 기본값은 실제 model descriptor나 선택 출처의 부재를 대신하지 않는다.
요약 경로가 main context를 공유하면 기존 context 점유를 확인하고, 격리 context라면 그 경계를 입증한다.
얻을 수 없는 항목은 unknown/source unavailable로 product record에 기록한다. LG-D30에 따라 실제 선택 model/thinking/tier·context와 요청 결속을 확인할 수 없으면 affected preflight를 차단한다. Output ceiling enforce·reasoning 의미/예산·부모 context 점유량의 unknown만으로는 호출을 막지 않으며, host 기본 output 요청과 격리 child context fit을 사용한다. Scripted provider의 값만으로 실제 configured model 관측을 pass하지 않는다. 이 기본값은 provider 의미의 입증이 아니며 실제 품질/잘림/비용은 미검증으로 남긴다. 새 budget 설정이나 fallback chain은 만들지 않는다.
명시 verify의 Q01 8개 scripted 판별과 이 metadata 관측은 따로 기록하고 모두 필수 상태를 확인한다.
이때도 실제 모델 응답 품질·비용은 RM-input-budget 시험 전 미검증이다.

Priority 판별은 입력 상한의 실행 중에 user input/due60s/end를 주입해 enqueue→전달·점검 시작까지의 지연과
실제 session/turn/lock 점유 기간을 기록한다. 일반 제품 hard deadline을 추가하지 않는다. 비공유 경로도 새
전송 양보·실행 중 독립 전달이 관측돼야 pass다. 공유 경로는 scripted 측정과 실제 모델 미검증을 구분해
사용자에게 관측 최대값/입력·환경/한계를 보고하고 Q-S2-03 채택 전 blocked_adoption다.

Early gate는 아직 구현되지 않은 후속 동작을 요구하지 않는다. DVL-08이 개발 전용
tools/validate_feature_gate.py를 제공해 PLAN의 Q01 8개 item만 검사한다. 다른 dependency gate도
해당 owner의 선언된 local item으로 한정하며 전체 p5 requirements identity를 유지한다.
Shared check schema/log hash/원 candidate 및 위 projection 재대조/required evidence 수준 검증을 재사용하고 missing/stale/failed/unknown/fixture-only
runtime item을 pass로 만들 수 없게 한다. CLI가 임의로 item을 빼거나 pass flag를 넣을 수 없다.
최종 DVL-13은 shared 전체 gate validator로 p5의 모든 required gate를 요구한다. 이 scoped runner는 아직 없으며 enforcement는 pending이다.
Probe 결과 판정 전 validator/schema·음성 fixture를 독립 test_designer와 reviewer가 검증하고 bytes를 동결한다.
잘못된 hash/identity/누락 item/fixture-only/unknown/외부 pass flag를 거부하는 반례를 포함한다.
이후 validator 변경은 동결·독립 검토·그 validator로 판정한 결과를 무효화한다. 같은08 ticket 안의 별도 선행 phase다.
Freeze는 validator뿐 아니라 projection policy·q01_recheck의 probe-local/product-adapter 포트·판정/음성 fixture도
포함한다. Product 포트는08에서 완결하고09는 주입할 adapter만 작성한다.09에 판정 tool 쓰기 권한을 주지 않는다.
판정 tool 변경이 꼭 필요하면 Root가08의 bounded follow-up으로 분리하고 독립 시험/리뷰·새 digest 동결 후
변경된 tool이 판단한 결과를 새로 수집한다. Adapter와 판정 tool을 함께 바꾼 직후 self-pass를 만들지 않는다.
다른 ticket의 test_summary_*.py 같은 wildcard scope에도 동결 판정 fixture는 포함하지 않는다. 판정 tool/
fixture 쓰기 owner는08이며 최종 Root가 scoped reroute·독립 재동결 기록을 대조한다.
R5 착수 체크: audit helper는 제외 목록을 강제하지 않으므로10의 backend test hint는 명시 파일 목록으로 둔다.
Root는 dispatch의 실제 allowed 목록에 동결 판정 파일이 없음을 확인하고08 동결 digest를10 통합 전후에 대조한다.
제외를 서술한 것만으로 audit 통과를 주장하지 않는다. 새 시험 파일이 필요하면 Root가 명시 scope를 먼저 갱신한다.
08 이전의 독립 contract/fixture ticket은 Root가 선언된 local item의 실제 검사·독립 review를 대조해 통합한다.
이때는 scoped machine validation을 주장하지 않으며 08을 숨은 supplier로 삼지 않는다. 최종 검사에서 전체 근거를 다시 묶는다.

### 8.2 실제 adapter와 내부 skill

DVL-09는 Q-01에서 통과·채택된 경로만 구현한다. 새 agent 파일/이름은 만들지 않는다.
내부 skill은 omp_bridge/skills/workbench-important-log/SKILL.md 후보 경로, 영어로 작성한다.
선정 기준·사실/해석/미확인·출처·누적 갱신·DB health 해석·입력 지시 무시·구조화 schema만 담고
도구 차단은 실제 adapter guard가 담당한다. Role allowlist/isolation 테스트는 정확히 채택한 role만 확장한다.
기존 to-worker/to-manager/recovery 동작과 repo skill 크기 제약을 유지한다.

1MiB 수집 상한과 별개로 전송 body는 초기 최대32KiB이며 skill·system·기존 context를 포함한 token preflight를 한다.
LG-D30에 따라 최초 host 입력의 min(8192 tokens, model context의10%) 비용 quota는 적용하지 않는다. 선택한 child 모델의 context fit과 기존 32KiB 전송 경계를 최초·후속 SDK turn에서 확인한다. 부모 대화 context를 상속하지 않는 새 child이므로 부모의 미확인 context 점유량을 산술에 더하지 않는다.
최초 전송 전에 필수 후속 turn 증가분까지 예산에 맞는지 확인한다. 이후 매 전송도 최대 output 전량 예약,
기존 context/고정 instructions 여유, 전체 contextWindow와 전송 body 32KiB를 계속 검사한다.
Bytes 보수 estimator는 유효 결과의8KiB 상한과 실제 JSON framing·고정 tool 결과를 사용하며,
output token ceiling을 범용 token/byte 비율로 바꾸지 않는다. 필수 후속 turn이 맞지 않으면 전송 전에 보류한다.
후속 판별은 실제 SDK message 형식의 같은 projection에 기존 projected+8192 검사와 fittedCap 검사를 적용한다.
기존8192 framing allowance를 동적 추가분 몫으로 재사용하지 않는다. 판별 뒤 남은 wire 잔여분은
provider toolCallId·thinking·text·metadata의 예약 여유이며 무조건적인 byte 상한 보장이 아니다.
Context는 최초 입력·고정 결과/framing·기존 context와 직전 출력 전량 및 후속 출력 전량을 예약한다.
실제 후속 전송도 다시 검사하며 초과 시 전송하지 않는다. Provider HTTP body 적합성은 별도 미검증이다.
결과 전체 JSON은8KiB이며 독립 UTF-8 byte cap을 유지한다. LG-D30의 host 기본 outputCap은 min(8192, 선택 모델 maxTokens)이고 maxTokens option으로 요청한다. SDK context fitting과 전송 시 실제 입력 검사는 유지하되, provider enforce 여부·reasoning 포함/별도 ceiling·visible 결과 token 예산의 unknown은 호출 필수 차단 조건에서 제외한다. Unknown은 true/0 또는 관측값으로 위조하지 않는다.
요약 child는 configured Worker.default의 실제 registry descriptor와 selector thinking을 사용한다. 부모 model/thinking/tier 관측과 childSelection을 구분하여 epoch에 묶으며, child tier는 기존 부모 상속 출처를 명시한다. 부모 설정·session을 바꾸지 않는다.
잘림·미완결 또는 8KiB 초과 결과는 기존 계약 거부/no replay로 처리한다. 실제 provider 의미·품질·reasoning 소진·비용은 작은 실모델 시험 전 미검증이며, 사후 결과 거부를 비용 안전성 입증이라고 표현하지 않는다. 별도 비용 추적 정책이나 budget knobs는 추가하지 않는다.
Provider의 한도 초과 출력·잘린/미완결 JSON은 저장 거부/누락으로 기록하며 no replay를 우회하지 않는다.
32KiB/8192/10%와8KiB는 개발 기술 기본값이며 실모델 적합성은 미검증이다.
OMP가 제공하는 모델/tokenizer/context 정보를 먼저 사용한다. tokenizer가 없으면 bytes 기반 보수 추정을 쓰되
정확한 token 수라고 표시하지 않는다. model/context 또는 사용 가능한 기존 context를 확인 못 하면 preflight는
inconclusive로 차단한다. scripted fake context 성공으로 실모델 적합성을 주장하지 않는다.
초과 시 deterministic selection으로 최신 누적·시작/명세·확인 가능한 결정/보고를 우선하고 남은 terminal을
범위 단위로 줄인다. 생략 source/ranges/reason/추정량을 표시하고 잘림 text를 연속 원문으로 합치지 않는다.
필수 envelope도 예산에 안 맞으면 호출하지 않고 input_budget_unavailable를 표시한다. 긴급 추가 호출은 없다.
전송·수집·선정 byte/token 추정값, 모델/context 출처, 예약 output, usage/cost 또는 unknown을 request별로 기록한다.
FL/RM 후속은 feature의 FOLLOWUPS.md에 연결하고 RM-input-budget에서 실제 모델 context/비용/선정 누락을 확인한다.
Skill이 생략된 입력을 읽었다고 주장하거나 중요한 원본 근거를 대체하지 않는다.
DB 결과 검증은 schema·source refs·request digest·size·fact/interpretation/unknown 분리이고,
내용이 실제 모델에서 항상 올바르다는 보장은 별도 RM 시험 없이는 하지 않는다.

### 8.3 스케줄·승인·no replay

Backend 단일 scheduler: subject별 새 정보 high-water를 갖고 첫 dirty 이후 최소 300초 간격 + 종료 event로 coalesce.
Byte/event range 변화만 새 정보이며 동일 health snapshot·반복 상태만으로 호출하지 않는다.
Task 있는 command는 Task request 하나로 연결한다. 대기열은 body가 아닌 durable cursor/reference만 보관한다.
예약 선택은 oldest due 순, 한 요청 in-flight, 후보 page 최대32, 사용자가 직접 대화하는 동안 새 전송하지 않는다.

우선순위: 사용자 입력/기존 보호 → 기존 종료 처리·60초 점검 → 중요 요약/유지보수.
기존 점검에 새로운 hard 모델 완료 deadline은 만들지 않는다.
Task 요청은 해당 승인·활성 사실(또는 동일 종료 event의 승인된 후처리 참조)과 현재 unpaused를 확인한다.
종결 후처리 참조는 event 직전 승인·활성 사실과 event ID/사유/input cursor를 영속 복사하고 최신 철회를 재확인한다.
아래는 BRIEF LG-D14로 합의된 정책이다. 보관 종결과 새 종료 요약의 허용을 구분한다.

| Core event/상태 | 보관 종결 | 새 종료 요약 정책 |
|---|---|---|
| task_closed / done | closed 시각 | 기존 승인 후처리+새 정보+unpaused+미철회일 때1회 |
| task_closed / report_outcome_unknown, report_not_delivered | closed 시각·사유 | 같은 조건으로1회; report를 재전송하거나 성공을 확정하지 않음 |
| task_closed / cancelled | closed 시각 | 수집만, 신규 호출 없음 (LG-D14 합의) |
| task_closed / approval_retired_c_d66 | closed 시각 | 수집만, 신규 호출 없음 (LG-D14 합의) |
| task_closed / not_started_before_restart | closed 시각 | 미시작으로 수집만; 재시작이 종료 후처리 권한을 새로 만들지 않음 |
| finished / blocked / held, run_ended만 | 보관상 열린 상태 | 종료 trigger 아님. 주기는 기존 승인·활성·unpaused가 실제 유효할 때만 |
| 사용자 archival-close | CLI 지정 시각 | 호출 권한/종료 trigger 생성 아님 |
| Task 없는 command의 실제 end | 실제 확인 시각 | 같은 command의 작업 범위·관련 자료 연결, 새 정보·unpaused를 확인해1회 뒤 요약 범위 종료 |
| 새로운 종결 사유·event 중복/유실 | 사실만 보존 | 불명 권한으로 호출 금지; 오류/누락 표시 |

취소·철회·pause 전에 이미 전송된 유효 결과는 출처·중복·저장 안전 검사 뒤 저장할 수 있다.
이는 새 호출이나 자동화 재개가 아니다. 이벤트 중복·restart는 journal의 range/event key로 no replay를 유지한다.
Task 없는 command의 요약 범위는 해당 command의 5분/종료 요약 처리까지이며 다른 명령으로 승계하지 않는다.
개별 사용자 발화 연결을 위한 별도 grant는 요구하지 않는다. 종료 request가 끝나면 그 요약 범위도 끝난다.
Pause면 신규 호출을 보류하고 valid late result만 검증 후 저장한다. 기존 core 명령 실행 승인에는 영향을 주지 않는다.

request 상태: prepared → sending → executed_observed → stored.
확정 not_sent/미실행 실패는 다음 주기 새 정보와 병합 가능하다.
sending/실행 여부 unknown 또는 executed 후 결과 유실·저장 실패는 terminal gap이고 같은 input 범위를 자동 재호출하지 않는다.
새 ID·revision·skill 변경·누적/종료 요청도 이미 실행/불명 범위를 재포함하지 않는다.
이전 정상 누적 요약은 다음 입력으로 참고할 수 있지만 그 input의 원문을 다시 실행 구간으로 다루지는 않는다.
형식 거부된 응답도 실행이 관측됐다면 재호출하지 않는다.
마지막 좋은 cumulative를 failure로 덮지 않고 늦은 유효 결과는 첫 정상 저장 하나만 받아 duplicate/drop 이유를 남긴다.
동시 요청/late/restart 판단은 중요 DB가 아니라 durable request journal과 동일 range digest로 한다.
재시작은 stored range와 요청 event를 대조하며 새 권한·새 자동 전송을 만들지 않는다.
사용량은 기존 C-AC-21 표시와 연결하고 미제공 tokens/cost는 unknown으로 둔다.

반복 결과 거부의 circuit breaker (P5): 같은 채택 route/계약의 executed+rejected 중 schema 불일치·필수 skill
버전/출처 필드 누락·미완결/초과 결과가 연속3회면 summary_contract_failed 보호로 새 요청을 막는다.
3은 무기한 비용을 방지하는 기술 기본값이며 모델 원인 확정이나 실모델 적합성 판정이 아니다. Guard/provenance
안전 경계 불능은3회를 기다리지 않고 즉시 보호한다. 일시적 통신/provider/요금 오류·저장 장애는 계약 streak를
올리지도 초기화하지도 않는다. 정상 검증·저장된 새 범위 결과만 streak를0으로 초기화한다. request ID·OMP
버전 변경·restart로 초기화하지 않고 운영 journal에 마지막 사유와 함께 영속한다.
단일 in-flight의 요청 sequence로 streak를 대조하며 더 오래된 요청의 늦은 정상 결과는 최신 streak를 초기화하지 않는다.
보호 뒤에는 수집/실험·마지막 정상 요약을 유지하고14의 포트를 통해 신규 source만 legacy-stop·사건당 알림1회를
적용한다. 원인 불명은 그대로 표시하고 OMP 비호환 확정과 구분한다. 자동 verify·동일 거부 구간 재호출은 없고 LG-AC-18 유지.

R2 복구 시도: --scripted verify는 guard/skill/schema/budget의 plumbing과 metadata/preflight를 확인할 뿐,
실제 모델의 계약 준수·잘림 해소를 검증하지 않는다. Record에 plumbing_verified와
real_model_contract=unverified를 분리한다. summary_contract_failed 뒤 필요한 경계가 모두 pass하면 명시 verify
결과를 참조한 recovery_attempt를 영속하고 기존 권한 아래 복구를 시도할 수 있다. 모델 설정 변경을 요구하거나
paid real-model 시험을 자동 실행하지 않는다. 다음 정규 주기/허용 종료 요청의 새 미실행 범위만 대상으로 한다.
이 시도 중 executed+contract-rejected가1회면 즉시 재보호한다. 확인된 safety 위반은 언제나 즉시 보호한다.
Transient 오류·저장 장애는 recovery_attempt를 해제하지 않으며 원래 저장 장애/pause 경계도 유지한다.
최신 sequence의 정상 검증·저장 결과를 관측해야 시도를 마치고 통상3회 streak 감시로 돌아간다.
이 관측도 모델의 향후 품질 보장으로 기록하지 않는다. Recovery attempt/episode는 restart·OMP update로 reset하지
않고 이전 late 결과로 성공 처리하지 않는다. 재발은 새 incident episode라 알림1회이며 단순 재접속은 중복0.
Guard 불능·필수 metadata unknown·다른 보호 사유가 남으면 recovery_attempt만으로 이를 해제하지 않는다.
Raw admission 회복은 기존 명시 활성화·LG-D13과 현재 안전검사 범위 안에서만 가능하고,재보호 시 신규 source만
legacy-stop으로 받는다. 실패/불명 입력의 replay나 긴급 추가 호출은 이 경로에도 없다.

## 9. 공용 raw store와 조회 전환

DVL-11은 legacy store와 별도 raw-v2 segment 엔진을 구현한다. DVL-12는 비활성 주입·reader·조회,
DVL-14는 사용자 activation·제품 근거 서비스다. 12는 Q01·scheduler가 없어도 독립 구현/검증할 수 있다.
100,000,000 bytes/segment, 총5개(활성 포함), 헤더/framing도 file cap에 포함한다.
출력 record는 초기 payload 최대64KiB, bounded header에 source/epoch/offset/time/digest를 둔다.
한 source가 예산을 모두 쓸 수 있음을 숨기지 않고 source별 retained/evicted/missing bytes/ranges를 표시한다.
Payload가 segment 잔여보다 크면 쪼갠다. 안전한 rotate intent·sequence·partial record 복구와 전용 lock이 필요하다.
관측·UI·matcher는 storage보다 앞에 있고 storage fault/rotation은 host를 종료하지 않는다.
Source catalog/control metadata는 body를 복제하지 않는 작은 index이며 raw 파일 예산과 운영 metadata 사용량을 구분한다.
LG-D16의 fault 범위는 public segment 교체이며 control/quarantine은 transaction owner 전용이다. victim identity·durable intent·
nonce quarantine capture로 unknown replacement의 삭제/채택을 막고, collision·각 crash/restart 경계를 격리 검증한다.
quarantine/unknown raw 및 열린 anonymous raw inode도 총5개에 포함한다. victim FD를 닫기 전에 6번째 raw inode를 만들지 않는다.
동일 UID의 임의 control/quarantine 교체 보장은 별도 범위이며 0700/flock를 해당 보안 경계로 주장하지 않는다.

기존 파일의 log_path 의미는 legacy에서 유지한다. Segmented source는 opaque raw_log_ref + 전용 reader로 제공한다.
옛 log_path 필드가 필요한 응답은 bounded JSON source descriptor 파일(본문 복제 아님)을 가리키고
log_format=segmented-v1/read command/remaining ranges를 함께 제공한다.
실제 full text 파일이라고 표시하지 않는다. DONE_INSTRUCTION, Task command list, worker staged 분석과
flow_tasks의 출력 문구, agent skill·bridge schema, CLI 소비자를 함께 바꾼다.
활성화 전 기존 파일/필드/명령 한도와 DONE 설명은 그대로다. Legacy 조회는 원본 파일을 읽고 별도 용량을 표시한다.

제안 제품 CLI:
- omp-workbench logs raw --run <id> | --command <id>, --offset/--limit/--json
- omp-workbench logs raw-status --json
- omp-workbench logs summary-compat verify --scripted: 격리 판별만, 요약 활성화/실제 사용자 raw 삭제 없음
- omp-workbench logs raw-rotation activate --yes --evidence <로컬 product record UUID>
Activation은 사용자 CLI 전용(Agent 도구 없음), 제품 Q01 record identity 검증 + 명시 경고/요청 확인 후 durable policy 기록이다.
Fake path나 JSON의 pass=true만으로 gate를 우회하지 못하도록 제품 검증기로 검사한다. 개발 tool/tests 파일을 요구하지 않는다.
기존 진행 중 source는 끝날 때까지 legacy writer에 남기고 활성화 이후 시작한 source부터 새 정책이다.
기존 파일/legacy cohort는 v2 rotation으로 삭제하지 않는다. Restart는 저장된 활성화 결정과 cohort를 유지한다.
실험과 Task 있는/없는 terminal command를 같은 v2 singleton에 주입한다. Full-output consumers를 남겨서는 안 된다.
활성화 이후 보호/재검증 상태의 정책(LG-D13): 새 source만 legacy-stop, 진행 중 cohort는 끝까지 유지한다.
OMP 버전 변경만으로 보호 상태를 만들지 않는 DV-D03 예외와 §8.1 상태 표를 함께 적용한다.
원인/신규 cohort 정책·새 정책 적용 시각을 표시한다. 재검증 pass는 기존 활성화 아래 신규 admission만 회복하며
최초 사용자 activation 결정을 만들거나 기존 파일을 삭제하거나 진행 중 cohort를 강제 전환하지 않는다.
모델 지연·raw eviction만으로 추가 긴급 호출하지 않으며 bounded buffer와 호출0회 증가를 음성 assertion으로 둔다.

## 10. 조회·정리·UI 계약

제안 제품 CLI는 기존 --data-dir 규칙을 유지한다.

- logs important list, show --task <id> | --command <id>, status, --json; page/limit로 bounded 응답.
- logs important archival-close --task <id> | --command <id> --yes: 사용자만, 성공/실행 상태 변화 아님을 안내.
- logs important delete-db --db <UUID> --yes: 선택한 보존 DB의 영향/보호 이월을 보여주고 명시 삭제.
  --yes도 active UUID 거부·권한/alias 검사·이월 검사·lease를 생략하지 않는다.

기존 agent에는 workbench_logs 읽기 전용 bridge 도구로 list/show/status/raw 조회만 제공한다.
Create/summary/maintenance/activate/archival-close/delete는 이 도구로 호출할 수 없다.
정상 조회는 DB read-only transaction과 기존 안전성 검사만 한다. 모델 요청·정리·rotation·catalog/active DB 신규 할당을 시작하지 않는다.
LG-D15에 따라 private transient read snapshot은 허용한다. source 본체/WAL/SHM hash·파일 목록 불변과 최신 WAL 반영을 검증하며,
snapshot은 catalog count에 포함하지 않고 사용/오류 후 정리한다. copy bytes·scratch peak·I/O·lease/경합을 먼저 측정·보고한다.
snapshot 크기/시간 한도와 큰 정상 DB의 unavailable 정책은 별도 결정 전 확정하지 않는다.
Backend stopped일 때도 private path와 catalog가 확인되면 read-only 조회 가능, 불명은 error로 표시한다.
통합 조회는 active+archive DB UUID와 entry source를 표시하고 carried duplicate는 entry_id로 합치되 original/copy 출처를 남긴다.

TUI에는 summary error/delay/storage 장애와 항상 보이는 multiple-important-DB 경고를 기존 status 모델에 추가한다.
DB count >=2면 실제 수·physical 총량·CLI 정리 안내, 시작/reattach reminder를 표시한다.
Acknowledgment나 restart로 clear하지 않는다. 실물 DB catalog/recount로1개를 확인한 뒤만 해제한다.
Sidecar/metadata/intent/temp는 DB로 세지 않는다. Unknown count는 정상1개라고 표시하지 않는다.
과거 Task browsing 화면은 만들지 않는다. 요약 latency/new-info/usage와 last healthy 시간은 status에서 확인한다.

## 11. 수락 조건 추적과 시험 결정

requirements-r0.9-p5.json은 r0.8 승인 기준·r0.9 운영 합의의 28개 AC와 p5 검증 gate의 검토 기준이다.
Gate ID/증거 수준은 기술 분해이지 새 사용자 승인 주장이 아니다. §13 결정 대기를 pass로 간주하지 않는다.
PLAN.json만 dependency/resource/gate supplier의 canonical source다. 이 표는 행동 interface trace다.
최종 INT-runtime은 아래 전부를 통합 후보에서 다시 연결하되 유료 모델/VM은 pending 밖의 별도 TODO다.

| AC | 주 책임 ticket | 행동으로 관측할 interface / local gate |
|---|---|---|
| DV-AC-01 | DVL-01 | dev_validate mode 선택, 목표 초과해도 진행 / G-DV-01 |
| DV-AC-02 | DVL-02 (fast seam DVL-01) | readiness/watchdog, 실제 expected wait / G-DV-02 |
| DV-AC-03 | DVL-02 | 실제 설치 OMP plain/설치 outer/shell matrix와 미검증 / G-DV-03 |
| DV-AC-04 | DVL-01, DVL-02 | 보고서 identity/버전/첫 실패/attempt chain / G-DV-04 |
| DV-AC-05 | DVL-02 | 같은 항목 baseline→after cost, 안전 병렬·assertion inventory / G-DV-05 |
| LG-AC-01 | DVL-14 (disabled wiring DVL-12, engine DVL-11) | 제품 record·activate guard/legacy cap/v2 shared exact cap / G-LG-01 |
| LG-AC-02 | DVL-12 | host/display 유지·raw reader source/gaps / G-LG-02 |
| LG-AC-03 | DVL-04 | 수집 matcher→영속 proof→judge, missing indeterminate / G-LG-03 |
| LG-AC-04 | DVL-12 | legacy bytes/hash 불변·별도 사용량 / G-LG-04 |
| LG-AC-05 | DVL-10 (store DVL-03, guard DVL-09) | Task 입력·schema·출처·부작용 sentinel / G-LG-05 |
| LG-AC-06 | DVL-09 (Q01 DVL-08) | 기존 agent·skill digest·typed result/DB 검증 / G-LG-06 |
| LG-AC-07 | DVL-10 | scheduler dirty/300s/end/user priority/pause/health input / G-LG-07 |
| LG-AC-08 | DVL-10, DVL-12, DVL-14 | model lag/error·eviction에서도 host 유지/gaps, 긴급 추가 호출0·buffer bound / G-LG-08 |
| LG-AC-09 | DVL-06 | active-only120d/pressure cleanup, core DB 불변 / G-LG-09 |
| LG-AC-10 | DVL-06 | 열린 subject 시작/latest 보호·중간 제거/gap / G-LG-10 |
| LG-AC-11 | DVL-06 | HealthSnapshot pre/post sizes/stats/cache/write/locks/free / G-LG-11 |
| LG-AC-12 | DVL-06 | quick_check schedule·bounded maintenance·failure status / G-LG-12 |
| LG-AC-13 | DVL-07 | CLI/agent read-only·TUI delay/error·무모델/무정리 / G-LG-13 |
| LG-AC-14 | DVL-10 | direct command ID·300s/end·Task 중복 없음 / G-LG-14 |
| LG-AC-15 | DVL-06 | prune exhaustion→new active, old preserved/crash / G-LG-15 |
| LG-AC-16 | DVL-07 | 실제 count·persistent warning·start/reattach/restart / G-LG-16 |
| LG-AC-17 | DVL-06, DVL-07 | archive 불변·ack≠delete·recount / G-LG-17 |
| LG-AC-18 | DVL-10 | restart/late/unknown/lost/no replay/first good result / G-LG-18 |
| LG-AC-19 | DVL-10 | pause before dispatch vs valid late store / G-LG-19 |
| LG-AC-20 | DVL-07 | cross-DB provenance·UUID delete/active refusal/copy/recount / G-LG-20 |
| LG-AC-21 | DVL-07 (events DVL-05) | closed/actual end·CLI archival closure/reopen protection / G-LG-21 |
| LG-AC-22 | DVL-10 (origin DVL-05/08) | Task authority와 command 작업/자료 연결·종료 범위·무관 자료 거부 / G-LG-22 (LG-D24) |
| LG-AC-23 | DVL-10 (storage DVL-06) | normal full rollover vs unsafe storage call deferral/recovery / G-LG-23 |

필수 gate 추가: Q01-route/tools/skill/result/context/priority/recovery/origin (DVL-08 runtime),
RAW-engine (DVL-11 fixture), STORE-contract (DVL-03 fixture), LIFECYCLE (DVL-05 fixture),
INT-runtime (DVL-13 runtime, 전체 AC). Fixture pass를 runtime pass로 집계하지 않는다.
추가 required gate: Q01-validator(08 fixture·독립 검토 freeze), Q01-contract(09 fixture),
SUMMARY-budget(09 fixture), PRODUCT-evidence(14 runtime), MODEL-metadata(14 runtime·무모델호출), DV-registry(13 runtime).
13의 최종 full-candidate 검사는 모든 required gate를 재실행하며 앞 ticket 근거를 새 candidate로 relabel하지 않는다.
현재 모든 제품/runtime gate는 pending evidence다. 구조상 graph validator 통과는 기능 검증 완료가 아니다.

기존 회귀 seam: tests/storage/test_log_raw.py, tests/workflow/test_run*.py, tests/tasks/test_repository*.py,
tests/backend/test_flow_terminal*.py/test_task_flow*.py/test_workbench_skills*.py/test_paths_launcher.py,
tests/backend/test_omp_isolation*.py, tests/ui/status_workbench 및 tests/ui/test_product_state*,
tests/integration/test_cw16_harness.py/test_cw16_runtime_matrix.py.
새 seam: tests/dev_validation, tests/storage/important_log, tests/storage/test_raw_segments.py,
tests/workflow/test_condition_evidence.py, tests/backend/test_summary_*.py,
tests/backend/live_summary_feasibility.py, tests/integration/live_validation_retention.py.
이 이름들은 계획된 경로이고 아직 작성/실행하지 않았다.

후속 implement의 예시 명령(현재 실행하지 않음):
- PYTHONPATH=src <현재 Python> tools/dev_validate.py --mode fast --report-dir <isolated run>
- PYTHONPATH=src <현재 Python> tools/dev_validate.py --mode precise --report-dir <isolated run>
- 선택 ticket의 existing/new unittest file을 명시적으로 수집; 수집 수>0와 기대 assertion 목록 확인
- WB_LIVE_CW16=1 + 전용 HOME/OMP/data/report 환경에서 선택 live scenario, no real model
- 전체 test_*.py 회귀를 repo의 실제 collection 방식으로 실행하고 live/시간 시험은 분리 계측

Implement에는 독립 test_designer → 수정 → frozen reviewer → 필요한 delta review → 최종 combined 검사/범위 audit가 포함된다.
Root 내부 call 예산은 PLAN의 운영 추정이며 사용자 고정 상한이 아니다. 검증 근거는 후보/환경/요구 identity와 함께 기록한다.

## 12. 인계·graph·미확인

PLAN.json p5의 초기 frontier는 DVL-01, DVL-03, DVL-08이다.
다음은 읽기용 축약 graph다(전이적으로 중복인 edge는 생략). 정확한 edge/resource는 PLAN만 따른다.

```text
DVL-01 → DVL-02 ──────────────────────────────────┐
DVL-03 → DVL-04 → DVL-11 ───────────────┐          │
       → DVL-05 → DVL-06 → DVL-07 ───┐  │          │
DVL-03 + DVL-08(Q01 pass) → DVL-09 ──┼→ DVL-10    │
                                     │     │       │
DVL-07 + DVL-11 → DVL-12 (비활성 reader/조회)
DVL-10 + DVL-12 → DVL-14 (제품 근거·사용자 activation)
DVL-02 + DVL-04 + DVL-07 + DVL-10 + DVL-12 + DVL-14 → DVL-13
```

초기 병렬 후보는 03+08(추천) 또는03+01이고, 검증된 별도 workspace가 없으면 직렬이다.
01과08은 harness exclusive/shared가 충돌하므로 단순히 dependency가 없다는 이유로 동시 실행하지 않는다.
DVL-08 safety gate가 실패해도 DVL-01~07과 disabled DVL-11/12는 독립 수행할 수 있다.
03의 계약을 소비하는 ticket은 log-contract:v1 shared, 계약 파일 수정자는 exclusive를 claim한다.
계약 변경은 명시 version/digest+소비자 영향 재검증을 요구한다. 08 probe-local envelope와03 구현은09에서
같은 golden fixture·허용/거부 schema·range/authorization/usage 의미를 비교하고 Q01-contract gate로 기록한다.
이 문서는 기술 상세를 정하지만 actual test timings, pyte, OMP adapter/tool guard,
작업/자료 연결·종료 후처리의 실제 통합과 herdr 격리는 pending이다. 직접 발화→command 증명은 LG-D24로 완료 조건에서 제외했다. Q-04/05는 위 byte/size/reader 계약으로 상세화했다.
Q-01에서 제품 행동 변경이 필요한 대안은 재결정 대상이다. 새 agent로 우회하지 않는다.

Bundle 전체 완료에는 모든 required gate의 현재 runtime/fixture 근거가 필요하다.
설치되지 않은 outer는 BRIEF대로 미검증으로 표시하되 설치된 outer의 격리 실패를 pass로 숨기지 않는다.
Q-01 실패/불명으로 일부만 진행하면 완료로 보고하지 않고 달성 범위와 blocker를 분리한다.
Raw production 활성화는 최종 구현 gate의 임시 sandbox 활성화 시험과 별도 사용자 결정이다.

다음 명령은 이 정확한 bundle을 검토·승인하고 구현을 시작할 때 사용한다:
$implement docs/features/dev-validation-log-retention
다만 이 s5/p5 정확한 bundle 검토/승인과 필요한 조건부 §13 결정이 먼저다.
이번 계획 반영은 코드·시험·probe·worktree·agent 실행을 허가하지 않는다. 외부 리뷰의 실제 제출/수신은
별도 request·dispatch·received-review 기록으로 관리하며 publication 당시 상태를 현재 상태로 오인하지 않는다.

## 13. 사용자 합의와 측정 후 결정

| ID | 상태/결정 | 영향과 남은 경계 |
|---|---|---|
| Q-S2-01 (M1) | 합의: 보호/재검증 상태의 신규 source legacy-stop, 진행 중 유지 (LG-D13) | OMP 버전 변경만은 DV-D03 예외. 최초 활성화와 복구 admission 분리 |
| Q-S2-02 (M4) | 합의: cancelled·approval_retired는 수집만, 새 종료 요약 없음 (LG-D14) | 기존에 전송된 유효한 늦은 결과 저장은 별개 |
| Q-S2-04 (N4) | 합의 수정: OMP 업데이트 후 기본 실행 유지, 실제 문제·관련 제품 변경 때 수동 verify (DV-D03) | 매 업데이트 차단/반복 요구/자동 verify 제안은 채택하지 않음. 검증용 local provider/일회성 OMP는 운영 요약 route나 새 agent가 아님 |
| Q-S2-03 (M5) | 조건부: 공유 session/turn/lock의 측정·한계 보고 뒤 수용 결정 | 지금 차단 시간을 승인받지 않음. 비공유가 관측된 경로는 불필요한 사용자 대기 없음; 공유 route의 Q01-priority pass와09/10/14 확대는 측정값 채택 전 차단 |
| Q-S3-05 (N3) | LG-D30 사용자 결정: 단순 host 예산 기본값 사용 | 선택 model/context/출처 부재는 차단. Output/reasoning 의미의 unknown은 기록하고 기본 요청+context fit으로 진행; 실제 provider 의미 pass 아님 |
| Q-S3-06 (P4) | LG-D30 사용자 결정: 요약에 Worker.default 모델·selector thinking 재사용 | 부모 model/thinking/tier는 유지. Child tier는 기존 부모 상속, 새 모델 설정 없음. 개발 검증 뒤 작은 실제 요약 시험; 과도한 예산 정책 없음 |
| P1 후속 경로 채택 | 조건부: 요청별 유효 guard 확인이 불가능한 route인 경우만 대안 보고 | 안전 차단 없이 version drift 계속 실행하지 않음. 지금 대안/권한을 새로 확정하지 않음 |

추가 원칙은 사용자의 “1번 A / 2번 A / 3번은 … 기본은 그냥 새 버전에 대해서 동작해보고 문제가 생기거나
우리 프로그램의 업데이트가 필요하면 검증 + 수정” 및 “반영해줘.”로 합의했다.
이는 개별 운영 원칙과 문서 수정 권한이지 정확한 bundle 전체 승인·제품 구현·유료 모델 시험·실제 raw 삭제
활성화 승인이나 아직 측정하지 않은 한계의 수용이 아니다.


### 2026-10-10 추가 범위 결정

LG-D15/LG-D16은 [BRIEF의 재개 합의](BRIEF.md#2026-10-10-격리-수정검증-재개-합의)와 추가 승인 기록을 따른다.
AC 28개·gate 46개·PLAN p5 dependency/resource 및 Q01 8개 기준은 유지한다. snapshot 한도는 측정 후 보고/결정한다.
Q01 기준 변경·proxy 채택·FL-01 제품 수정·실제 raw 활성화는 이번 실행 범위에서 제외한다.

### 2026-10-10 쓰기 우선 첫 수정 경계

LG-D17의 쓰기 우선 결정과 [DVL-07 첫 수정 계획](tickets/DVL-07.md#2026-10-10-쓰기-우선-첫-수정-계획)을
따른다. writer급 catalog lease 취득 대기만 제한적으로 늘리는 최소 판별부터 시작한다.
reader 대기와 SQLite busy timeout은 별개이며, append 전체 재호출·commit 이후 재시도는 첫 수정에 넣지 않는다.
초기화·refresh·maintenance·recovery·health 경로의 같은 lease도 누락하지 않는다.
제품 수치 상한은 미채택이고 ticket의 실험 값은 격리 판별에만 사용한다.
동시 시험에서 정상 writer가 만드는 DB/WAL·catalog 변경과 조회의 원본 변경을 구분한다.
원본 불변은 통제된 조회 시험으로 확인하고, 동시 시험은 허용된 쓰기와 최종 기록을 대조한다.
원 SPEC s5·PLAN p5 전체를 재생성하거나 AC·gate를 줄이지 않는다. 후속 DVL-07 수락항목과 DVL-12 의존성은 유지한다.

### 2026-10-10 B안 실행 경계 (LG-D18)

[BRIEF LG-D18](BRIEF.md#2026-10-10-b안-승인-active-page-직접-조회)와
[DVL-07 B안](tickets/DVL-07.md#2026-10-10-b안-active-page-직접-조회)을 따른다.
이 절은 §10과 LG-D15의 active SHM/hash·namespace 불변 및 LG-D17의 고정 쓰기 우선 목표만 제한적으로 대체한다.
Active는 mode=ro/query_only 연결을 기존 catalog EX 아래에서 열고 page 응답을 얻고 닫은 뒤 lease를 해제한다.
Reader는 로그 내용·main/WAL 내용 쓰기, checkpoint·복구 데이터 쓰기·prune·rotation·삭제·새 DB/catalog 할당을 하지 않는다.
SQLite 표준 SHM read mark/wal-index 갱신과 필요한 sidecar 생성만 허용하며 private owner/권한/inode 검사는 유지한다.
불충족·복구 불가·busy는 읽기 실패로 표시하고 실제 내용 복구 쓰기나 active immutable로 우회하지 않는다.
Archive는 기존 snapshot과 §7.3 보호·삭제 순서를 유지한다. 읽기 도중 EX를 놓거나 unlink된 archive 조회를 허용하지 않는다.

Page cursor는 recordedAt/entryId keyset을 담아 cursor 찾기용 별도 snapshot을 제거한다.
List는 검증된 기존 subject_stats를 사용하고 entry 전체 집계를 lease 안에서 반복하지 않는다.
DB별 lease 사이 catalog generation 변화가 있으면 시작/종료 generation과 부분 결과를 표시한다.
경량 status·busy/기타 읽기 실패 표시를 유지한다. Snapshot cache·새 queue·archive lock 등은 추가하지 않는다.

Sidecar 없음/낡음과 생성 파일 권한을 먼저 격리 판별한다. 동일한 약250MB K=1/K=2, R=40, T=30초 조건에서
writer 기본100ms와 실험2초를 각각 측정한다. 누락·중복·writer lease 실패와 page lease/refresh/rollover/WAL 크기를 보고한다.
통제된 조회에서는 기존 main/WAL 내용 불변·최신 WAL 반영·checkpoint/새DB 할당0을 확인하며,
동시 시험의 정상 writer 변경은 최종 기록과 대조한다. Archive 불변·scratch 정리는 그대로 검증한다.
측정 성공은 해당 조건의 관측이며 strict priority·일반 무손실·제품 수치 상한 채택이나 전체 DVL-07 완료가 아니다.
Q01·proxy·FL-01·실제 raw 제외, 원 PLAN graph와 전체 gate는 유지한다.

### 2026-10-10 현실 호출 부하 판별 경계 (LG-D19)

[BRIEF LG-D19](BRIEF.md#2026-10-10-현실-호출-부하-판별-승인)과
[DVL-07 현실 부하 판별](tickets/DVL-07.md#2026-10-10-현실-호출-부하-판별)을 따른다.
LG-D18 구현·원본 보호와 원 feature 수락조건은 유지하고, 이번 B1 경합의 국소 완료 판별만 명확히 한다.
지속 append·lifecycle burst 중 사람 page 이동·초 단위 Agent 조회·경량 status·backend 점검을 모사한다.
Active만 있는 약250MB DB와 rollover 뒤 약250MB archive가 있는 DB에서 판별한다.
실험 빈도·window·watchdog은 종료 가능한 실험 값이며 제품 호출 제한이나 허용 지연 수치가 아니다.

전체 시도 marker/event_key를 active/archive 최종 store와 대조해 누락·중복 0,
경합 capture_unknown 0을 요구한다. busy/부분 결과·저장 실패 상태의 표시와 backend 지연,
writer 대기 분포·page EX 보유 시간을 기록한다. 실패나 미확인을 무음 성공으로 바꾸지 않는다.
연속 재진입 stress의 기존100ms 실패·실험2초 표본은 한계 기록으로 유지하고 이 국소 gate와 분리한다.
이전 문단과 첫 수정 이력의 연속 stress 누락0은 LG-D19 국소 판별의 완료 차단 기준이 아니다.

현실 부하의 실패는 원인·최소 대안과 함께 보고한다. Archive immutable page 읽기,
제품 writer 대기 수치·reader 진입 정책·새 잠금 프로토콜은 지금 변경하지 않는다.
기존 private fixture seam과 독립 시험·고정 입력 리뷰·영향권 검사를 적용한다.
DVL-07 전체 CLI/Agent/TUI/삭제 검증과 DVL-12, Q01/C2 차단은 별도로 남는다.

## 2026-10-10 LG-D20 sealed archive page 후속

BRIEF LG-D20와 [승인 기록](../../../.workflow/dev-validation-log-retention/runs/dvl07-sealed-20261010/authority.json)에 따라 현재 archive immutable page 보류를 검증된 sealed 분기에 한해 해제한다. Archive 등록과 `archiveChecks=complete`, WAL 없음/0바이트 조건을 기존 catalog EX 아래 확인하고, 원본에 `readonly=True, immutable=True`로 연결해 한 page 조회·close까지 EX를 유지한다. 기존 private path/UUID/sidecar 안전성 검사와 삭제·rollover 직렬화를 그대로 따른다. 미checkpoint archive는 기존 copy/re-hash snapshot과 최신 WAL 반영/불가 사유를 유지한다. Active에 immutable을 쓰지 않는다.

LG-D19의 현실 부하 누락0·중복0·경합 capture_unknown0·watchdog 기준과 stress 한계 기록은 유지한다. Monitor 지연을 판별에 포함하고, copy-start 관측은 새 sealed route에서 실제 archive page lease 진입으로 대체한다. 호출 일정·쓰기 분모·fixture·writer 기본 대기/polling·실험 watchdog은 바꾸지 않는다. Source 불변은 writer 없는 control과 보존 archive에서 검사하고 정상 concurrent writer 변경과 구분한다. 수치는 관측으로만 보고한다. 원 PLAN·전체 AC·Q01 기준을 변경하지 않는다.

## 2026-10-11 LG-D24 작업 내용 중심 방향 변경

[BRIEF LG-D24](BRIEF.md#lg-d24--작업-내용-중심-중요-로그와-작업-연결-기준-2026-10-11)와 [방향 승인 기록](../../../.workflow/dev-validation-log-retention/runs/work-log-boundary-20261011/decision.json)을 따른다. Q01-origin/G-LG-22와 LG-AC-22의 개별 발화→command 일대일 증명만 제외하며, 작업 식별·범위·관련 source/ranges·결과 연결과 기존 Task 승인·종료 후처리·철회·pause를 유지한다. Task 없는 command는 계속 command ID로 포함하고 전체 사용자 대화·시스템 활동 추적을 추가하지 않는다.

판정 facts·probe·작업 참조를 LG-D24에 맞춰 정렬하고 독립 시험·리뷰 및 192개 통합 회귀 검증을 마쳤다([국소 통합 기록](../../../.workflow/dev-validation-log-retention/runs/work-log-alignment-20261011/phase-integration01.json)). 현재 `command_start_bound`와 `work_source_ranges_bound` 등은 확인된 command 시작과 해당 작업 자료를 검증한다. 이전 `direct_command_bound`와 origin 조사·자문·미확인 기록은 보존하되 새 기준의 차단 선행조건으로 사용하지 않는다. 문서 변경이나 국소 검증만으로 Q01 전체 pass를 내지 않는다. 현재 수집기는 원 격리 runtime의 변경 없는 8개 stage와 우선순위 재측정 1개 stage를 구분해 재판독한다. 현재 판정은 7개 통과, Q01-priority의 내부 lock 최대 점유 시간 미확인이다. 전송 body 전체 byte 검사와 측정 누락을 미확인으로 남기는 독립 시험 7개·관련 회귀 34개는 통과했다. 이 결과는 Q01 전체 통과나 제품 연결 완료를 뜻하지 않는다([판정·측정 기록](../../../.workflow/dev-validation-log-retention/runs/work-log-alignment-20261011/current-eight-writer05/result.json)). 나머지 Q01·예산·SDK 도구제한·no replay·C2 hold·조회결정·의존성·제외 범위는 유지한다.

## 2026-10-11 LG-D25 기존 Task를 이용한 원인 수정·복구

[BRIEF LG-D25](BRIEF.md#lg-d25--원인-수정복구는-기존-승인-task-방식-사용-2026-10-11)를 따른다. 원인 분석·영구 수정·회귀 검증은 기존 승인 Task 범위에서 수행하고 범위 밖 대상은 작은 수정 Task로 별도 승인한다. 실패 알림 자체는 수정 권한이 아니다. 운영 명시 verify와 recovery_attempt 연결, 미전송 범위만 재시도하고 전송된 unknown/contract-rejected 범위는 replay하지 않는 계약을 유지한다. 제품 전체의 상시 쓰기 권한이나 자동 verify loop는 채택하지 않는다. 현재 개발 수정·독립 검증 권한은 유지하며 실제 제품 연결은 DVL-09/10/14의 수락 대상이다.

## 2026-10-12 LG-D26 최초 입력 quota 적용 범위

[BRIEF LG-D26](BRIEF.md#lg-d26--최초-host-입력-quota와-후속-turn-예약-2026-10-12)와 [사용자 승인 기록](../../../.workflow/dev-validation-log-retention/runs/dvl10-20261012/u1-host-input-20261012/decision.json)에 따라 §8.2의 최초 host 입력과 필수 후속 turn 판별을 구분한다. 한도 수치·no replay·운영 metadata 요구·PLAN 의존성과 나머지 Q01 기준은 유지한다. 기존 stream별 quota 아래의 실패·측정 기록은 당시 결과로 보존하고 새 해석의 회귀·runtime을 별도로 검증한다.

## 2026-10-14 LG-D30 요약 모델·예산 후속 승인

[BRIEF LG-D30](BRIEF.md#lg-d30--worker-모델-재사용과-단순한-요약-예산-2026-10-14)은 부모 모델 복제·최초 비용 quota·출력 의미 unknown 차단에 대한 현재 승인이다. Gate ID/증거 수준, DB-only·결속·no replay·C2 hold와 실제 raw 비활성은 유지하며 기존 실패/관측을 새 pass로 재표시하지 않는다. 해당 모델/예산 소비자는 이 승인에 맞춰 독립 검증하고, scripted fixture와 실제 운영 관측을 구분한다.

## 2026-10-15 LG-D31 구현·검증 집중 범위

[BRIEF LG-D31](BRIEF.md#lg-d31--실용-기능-완성과-다섯-안전-조건-2026-10-15)에 따라 사용 가능한 작업 기록·요약·조회 기능의 완성을 우선한다. 표현·중요도 판단·일부 내용 누락의 품질 편차를 허용하며, 요약의 완벽함이나 반복 품질 평가·자동 교정을 완료 조건으로 추가하지 않는다. DB 기록 형식·권한·작업 귀속·실패 표시 같은 기존 구조적 계약과 품질 편차를 구분한다.

검증은 실제 경로의 DB-only 및 host 고정 writer 경계, Task·command·입력 범위 결속, sent/unknown과 확정 not_sent의 구분 및 no replay, 실패 상태·최소 원인과 정상 기록 보존, 전용 임시 환경·소유 자원 정리에 집중한다. 가능한 모든 내부 고장 증명이나 실제 결함과 직접 연결되지 않는 견고화·성능 개선은 필수 작업으로 확대하지 않고 후속으로 남긴다.

기존 수락 ID·필수 gate ID·증거 수준·의존 graph와 미검증 상태는 유지한다. 영향 범위가 맞는 유효 근거를 재사용하고 관련 변경을 묶어 독립 검증한다. 필요한 최종 통합 검증은 유지하되 무관한 전체 검사를 변경마다 반복하지 않는다. LG-D30의 Worker.default·단순 기본값과 이미 승인한 작은 실제 입력 → 요약 → DB 저장 → 조회 시험, C2 hold·실제 raw 순환 삭제 비활성을 유지한다. 이 방향 변경만으로 gate 통과나 실모델 품질을 선언하지 않는다.

## LG-D32 검증 귀속 범위 (2026-10-15)

[BRIEF LG-D32](BRIEF.md#lg-d32--검증-io-귀속의-완료-경계-조정-2026-10-15)에 따라 §8.2의 observer/SDK child 음성 조건을 검증할 때, 같은 프로세스 안의 전체 native I/O 귀속 불명은 명시적 미검증 한계로 남기며 단독 완료·정상 요약 차단 사유로 쓰지 않는다. 관측되지 않은 값은 0/pass로 올리지 않고 실제 금지 동작은 실패로 처리한다. LG-D31의 다섯 안전 조건을 실제 경로에서 확인하고 Q01 8개·나머지 필수 gate·전용 임시 환경·사용자 자원 불변·소유 자원 정리·C2 hold·실제 raw 비활성을 유지한다. 부분 supplier 결과를 전체 통과로 바꾸지 않는다.

## LG-D33 실제 시험 인증 경계 (2026-10-15)

[BRIEF LG-D33](BRIEF.md#lg-d33--작은-실제-요약-시험의-인증-복사-2026-10-15)의 작은 실제 요약 시험에 한해, 필요한 인증 파일을 내용 열람·출력 없이 전용 임시 환경으로 기계적으로 복사하고 기존 OMP가 복사본을 사용하게 할 수 있다. 원본 쓰기·인증 내용의 prompt/대화/로그 노출은 허용하지 않는다. 입력·세션·DB 격리와 복사본을 포함한 owned cleanup을 확인한다. Credential-free scripted verify의 인증정보 제외 조건과 나머지 안전 조건은 유지한다.
