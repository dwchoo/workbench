# DVL-07 — 통합 조회·사용자 정리 CLI·지속 TUI 알림

계획 revision p5 / SPEC s5 / BRIEF r0.9 (r0.8 승인 + 운영 원칙 개별 합의).
상태·의존성·resources·scope의 canonical source는 [PLAN.json](../PLAN.json)의 DVL-07이다.
원 s5/p5 승인과 [2026-10-10 두 결정·격리 재개 승인](../../../../.workflow/dev-validation-log-retention/runs/implement-p5-20261009/resume-decisions-20261010.json)을 함께 따른다.
Q01 기준 변경·proxy 채택·FL-01 제품 수정·실제 raw 활성화는 제외한다.
기본 계약: [SPEC](../SPEC.md), [BRIEF](../BRIEF.md); source decision은 BRIEF 추적표를 따른다.

## 제공할 행동

active/archive readonly 조회와 사용자-only 관리, 실제 복수 DB 경고/재접속 안내를 제공한다.

logs important list/show/status와 workbench_logs의 read-only important action을 구현한다. Task/command 분리와 DB UUID provenance/page bound를 제공한다.
조회는 summary/maintenance/new DB/delete를 시작하지 않고 stopped backend도 읽을 수 있는 안전 범위를 제공한다.
사용자 CLI archival-close와 delete-db --db UUID를 추가한다. Agent에는 두 mutation을 노출하지 않는다.
삭제 전 exact selected archive·active refusal·open handles·ongoing start/latest의 이월/검증·intent를 확인하며 오류/crash면 영향과 실제 잔존을 표시한다.
2개 이상 경고는 실제 수/총 물리 용량과 CLI 안내로 상시 표시하고 시작/재접속 때 알린다. ack/restart는 clear하지 않는다.
core status 변경/실행 중단/새 과거 탐색 TUI는 만들지 않는다. 실제1개 확인 때만 해제하고 unknown count는 unknown으로 둔다.

## 수락·검증 계약

수락 ID: LG-AC-13, LG-AC-16, LG-AC-17, LG-AC-20, LG-AC-21.
G-LG-13/16/20/21 owner; archive 자동 삭제 없음은 DVL-06과 결합.
Gate supplier/required evidence level 및 체크 ID는 PLAN의 gate_items/required_checks를 따른다.
전체 trace는 SPEC §11이며 local 부분과 뒤 combined coverage를 바꾸거나 삭제하지 않는다.

검증: readonly CLI/agent calls에서 model/cleanup counters=0, active/archive provenance, empty/notfound/corrupt/partial data; UUID/alias/active deletion refusal, safety carry failure/crash, archival-close/re-activity; TUI ack/restart/reattach/recount.

후속 implement는 coherent candidate의 독립 test_designer 평가/시험과 frozen reviewer 검토를 포함한다.
다른 ticket 또는 실제 OMP 없이 관측 불가능한 항목을 fixture/문구 시험만으로 pass하지 않는다.
Required item unknown이면 실패 원인/한계와 evidence를 남기고 affected gate를 닫는다.

## 구현 경계·안전

Agent에 삭제/종결 도구를 노출하거나 protected 정보 이월 실패 후 파일을 지워야 하면 중단. 기존 user DB 대상으로 시험하지 않는다.
기존 dirty/new 사용자 파일과 legacy log/DB/core 승인 증거를 보존한다.
사용자/실제 Workbench·DB·socket·tmux/herdr를 대상으로 시험하거나 정리하지 않는다.
새 제품 agent 정의/상주 OMP/독립 Python 모델, Phase2, commit·게시·배포는 범위 밖이다.
권한이 필요한 대안은 Root에 사실/대안/영향을 전달하고 임의 확장하지 않는다.

## 작업·인계

추천 worker: worker_senior; 근거: SPEC.md §7.3/10; existing CLI/UI/bridge.
추천은 실제 dispatch pin이 아니며 Root가 당시 code/host/권한을 재확인한다.
물리 작업 공간은 PLAN의 policy를 따르고 parallel writer는 별도 검증된 workspace/service namespace가 필요하다.
write_scope_hint는 계획 추정이며 dispatch 때 baseline·실제 경로를 검증한다. 이 Markdown에 별도 graph/status를 만들지 않는다.
중앙 PLAN/state/승인은 Root만 변경한다. Writer는 candidate delta/검사 evidence/남은 unknown을 반환한다.
현재 미검증: 실제 summary error/lag 공급은 DVL-10과 INT-runtime.

## p2 리뷰 보강

archive 조회는 checkpoint 완료/미완료, 읽기 전용 directory, readable WAL/SHM·sidecar 없음/손상, 동시 이월/삭제를 시험한다. 조회 전후 본체/sidecar hash·파일 목록이 불변이어야 한다. active/미checkpoint DB의 immutable 사용은 거부하고 불충족은 unavailable로 표시한다.

## 2026-10-10 조회 해석

LG-D15: catalog/active DB 신규 할당 없이 private transient read snapshot 허용. source 본체/WAL/SHM hash·목록 불변,
최신 WAL 반영·cleanup·scratch/copy/I/O/lease 비용을 격리 측정한다. 크기/시간 한도·큰 정상 DB unavailable 정책은 측정 후 별도 결정이다.

## 2026-10-10 쓰기 우선 첫 수정 계획

근거: LG-D17 사용자 결정과
[claude-wb 경계 리뷰](../../../../.workflow/dev-validation-log-retention/runs/snapshot-boundary-review-20261010-01/received-review.json).
전체 ticket의 일부를 판별하는 bounded continuation이며 PLAN p5의 graph·resources·수락기준은 바꾸지 않는다.
이 첫 수정 계획과 측정 실패는 이력으로 보존한다. 현재 실행 범위는 아래 LG-D18 B안이 대체한다.

## 2026-10-10 B안: active page 직접 조회

근거: 사용자 “b안과 네 추천대로 해줘”, BRIEF LG-D18 및
[실행 승인](../../../../.workflow/dev-validation-log-retention/runs/dvl07-direct-20261010/authority.json).
이 절만 재개하며 원 PLAN dependency/resource·전체 수락항목은 변경하지 않는다.

1. Sidecar 없음/낡음의 표준 ro open, 기존 main/WAL 내용 불변, 생성 sidecar의 owner/0600/nlink/inode 조건을
   작은 private fixture로 먼저 판별한다. 복구 데이터 쓰기나 권한 완화로 통과시키지 않는다.
2. Active는 catalog EX → mode=ro open → page 한 번 조회 → close → EX 해제한다.
   SHM reader 협조 갱신·필요한 sidecar 생성만 허용한다. Archive는 기존 snapshot/보호 조건을 유지한다.
3. Show keyset token(recordedAt, entryId)으로 cursor 탐색용 snapshot을 제거한다.
   List는 기존 subject_stats를 사용한다. DB 사이 generation 변화는 부분 결과로 표시한다.
   경량 status·busy/기타 실패 사유·출처/중복 처리는 유지한다.
4. 동일한 약250MB K=1/K=2, R=40, T=30초·backend watchdog5초 조건에서 writer100ms와 실험2초를 각각 측정한다.
   최종 알려진 기록 누락/중복·writer lease 실패, page EX 보유 시간, refresh/rollover/checkpoint/WAL 비용을 보고한다.
   통제된 main/WAL 불변·최신 WAL·새 DB/checkpoint0, archive 불변·scratch 정리,
   paging100건 초과·복수 DB·generation 변화 및 rollover/삭제 직렬화를 시험한다.
5. 독립 test_designer·frozen reviewer와 영향권 최종 회귀·범위 감사를 수행한다.
   실패 결과를 보존하며 fixture 판별을 전체 runtime 완료로 확대하지 않는다.

제품 대기·snapshot 상한은 미채택이다. Intent/abort·전역 멱등화/commitUnknown 개편·queue/cache·
archive별 lock/immutable fast path·삭제 규약 변경·append 전체 재시도는 제외한다.
Q01 기준 변경·proxy 채택·FL-01 제품 수정·실제 raw 활성화·사용자 DB·유료 모델 제외를 유지한다.

### 이전 첫 수정 계획의 상세 (이력)

### 포함과 순서

1. 기존 maintenance lock과 원본 전체 hash 검증을 유지한다. writer급 lease 취득 대기를 매개변수화하고
   제한된 추가 대기를 적용한다. append·ActiveSubjectLog뿐 아니라 catalog 생성/열기·초기화·backend refresh·
   maintenance·recovery·health의 같은 lease 경로를 포함한다. reader는 짧은 대기를 유지한다.
   대기는 mutation body 진입 전에만 적용한다. SQLite busy timeout, append 전체 재호출,
   commit 이후 재시도·성공 판정 개편은 포함하지 않는다.
2. 기본 status의 DB별 full snapshot·quick_check를 제거한다. catalog와 이미 확인된 last-checked 정보만
   표시하고, 캐시된 근거가 없거나 backend가 중지돼 확인할 수 없으면 unknown으로 둔다.
   새 지속 cache를 만들거나 조회로 검사를 시작하지 않는다.
3. DB당 100건 이후가 누락되는 show paging을 수정한다. cursor·정렬·복수 DB provenance와 완료 여부를 확인한다.
   최소한 busy와 기타 실패를 구분하고, 부분 결과에는 빠진 DB와 이유를 표시한다.
4. 아래 격리 판별을 수행한다. 관련 독립 시험과 frozen review를 거쳐 첫 수정 결과를 보고한다.

### 최소 판별과 완료 기준

- 유효한 약 250MB fixture와 reader K=1/K=2, lifecycle·append를 섞은 알려진 쓰기를 사용한다.
  생성/초기화와 refresh 경합도 포함한다. 실제 사용자 DB·서비스는 사용하지 않는다.
- 초기 실험 값은 writer lease 대기 2초, reader 대기와 SQLite busy timeout은 기존 100ms다.
  K별 쓰기 R=40, 관측 창 T=30초, backend 무응답 watchdog 5초를 시작 값으로 둔다.
  이는 실험의 재현·종료 경계이며 제품 허용 지연·용량 정책이 아니다. harness 결함 때문에 조정하면
  이유와 전후 결과를 보존하며, 실패를 통과로 만들기 위해 값을 조용히 늘리지 않는다.
- 알려진 event_key·entry가 최종 active/archive store 전체에 정확히 한 번 존재해야 한다.
  조회 경합에 따른 최종 누락·중복·capture_unknown·writer 대기 소진은 0이어야 한다.
- 통제된 read-only/오류 시험에서는 원본 DB/WAL/SHM hash·파일 목록 불변, 최신 WAL 반영,
  사용 후·오류 후 scratch 정리를 확인한다. 동시 시험에서는 정상 writer의 변경과 rollover는 허용된
  실행 기록으로 대조한다. 쓰는 동안 전체 DB hash가 불변이라고 잘못 판정하지 않는다.
- paging은 DB당 100건 초과와 복수 DB에서 누락·중복 없이 이어져야 한다. 기본 status가 전체 복사·검사를
  시작하지 않고 미확인을 정상으로 표시하지 않는지도 확인한다.
- writer 대기 분포/최대값, backend 응답·루프 정체, reader 완료/busy 수, 호출/page 전체 비용을 보고한다.
  watchdog 초과나 안전성 실패는 원인과 증거를 남기고 판별 실패/불충족으로 처리한다.
  성공은 측정한 조건에 한정하며 모든 host의 strict priority나 제품 지연 상한을 승인하지 않는다.

### 보류와 확대 경계

writer-intent/chunk abort, 전역 멱등화·commit-unknown 개편, archive별 lock/immutable fast path,
scratch owner lock·fallocate·강제 종료 sweep, 새 queue·지속 snapshot cache는 첫 수정에서 보류한다.
기존 source preservation과 사용 후·오류 후 cleanup은 유지하며, 나머지 원 수락조건을 면제하지 않는다.

누락·대기 소진, 과도한 backend 정체, reader 압력에 의한 writer starvation이 실제로 관측될 때만
원인에 맞는 최소 후속 대책을 검토한다. 실패가 곧 전체 잠금 재설계 권한은 아니다.
멈춘 reader·느린 I/O·강제 종료 잔존은 관측 한계로 기록하고 새 보장 예외를 임의 채택하지 않는다.

### owner와 후속

DVL-06 catalog lease/storage fault, DVL-05 lifecycle/subject, backend init/refresh composition,
DVL-07 reader 변경의 영향 범위를 묶어 독립 검증한다. 변경된 후보에는 기존 통합 pass를 그대로 재사용하지 않는다.
CLI/Agent 전체 조회·사용자 정리·TUI 경고·archive 조건 등 DVL-07의 나머지 수락항목은 유지한다.
DVL-12는 전체 DVL-07 완료 뒤에 진행한다. Q01·C2 차단과 원 제외 범위는 유지한다.
제품 writer 대기·backend 허용 지연·snapshot/scratch 한도는 관측 결과 뒤 보고/결정한다.

## 2026-10-10 현실 호출 부하 판별

현재 경합 국소 판별은 BRIEF LG-D19와 사용자의 “그렇게 진행해 줘.” 승인을 따른다.
[승인 기록](../../../../.workflow/dev-validation-log-retention/runs/dvl07-usage-20261010/authority.json)과
[claude 자문 종합](../../../../.workflow/dev-validation-log-retention/runs/snapshot-usage-boundary-review-20261010-01/REVIEW-SYNTHESIS.md)을 참조한다.
위 첫 수정·B안 연속 stress와 실패는 이력이며, stress 누락0을 이번 국소 완료 차단으로 요구하지 않는다.
PLAN p5의 graph·resources와 전체 AC·Q01은 변경하지 않는다.

1. 기존 B1과 writer100ms/10ms polling을 유지한 합성 fixture에서 현실 부하 harness를 작성한다.
   지속 append·lifecycle burst와 드문 사람 page 이동·초 단위 Agent 조회·경량 status·backend 점검을
   모사한다. 실제 CLI/Agent 전체 통합이나 호출 빈도 정책의 검증으로 확대하지 않는다.
2. Active만 있는 약250MB DB와 실제 rollover 뒤 약250MB archive를 포함한 show/page 이동을 각각 측정한다.
   격리 fixture 생성·rollover는 허용하지만 reader가 이를 유발해서는 안 된다.
   source 내용 불변은 writer가 통제된 별도 시험에서 확인하고, concurrent writer의 정상 변경과 구분한다.
3. 전체 시도별 marker/event_key를 최종 active/archive와 대조한다. 누락·중복 0,
   경합 capture_unknown 0, backend watchdog 이내를 요구하고 writer 대기·page EX·지연을 기록한다.
   오류·부분 결과·busy/capture_unknown 표시와 archive 불변·scratch cleanup을 확인한다.
4. 독립 test_designer가 harness와 판별 분모·실패 표시를 검증한다. 필수 실패가 있으면 final acceptance
   review 대신 읽기 전용 실패 분석을 수행하고 결과·최소 대안을 보고한다. 성공한 고정 후보는 독립 리뷰와
   영향권 최종 검사·범위 감사로 닫는다. 원 측정·실패 파일을 덮어쓰지 않는다.

현재 권한은 문서/승인 반영과 이 측정·검증이다. Archive immutable page 읽기,
writer 대기 수치·reader 재진입 정책·새 프로토콜은 지금 채택하지 않는다.
무음 누락·원본 보호 위반·scratch 잔여·watchdog 초과나 현실 부하 실패는 숨기지 않고 보고한다.
통과도 B1 국소 판별이며 전체 DVL-07/DVL-12와 feature 완료가 아니다. 원 제외 범위를 유지한다.

## 2026-10-10 승인된 sealed archive 최소 후속

LG-D20 “응 일단 이어가자.”가 [다음 범위 제안](../../../../.workflow/dev-validation-log-retention/runs/dvl07-usage-20261010/NEXT-SCOPE.md)을 승인했다. [승인 기록](../../../../.workflow/dev-validation-log-retention/runs/dvl07-sealed-20261010/authority.json)으로 현재 archive page 분기 보류를 좁게 해제한다.

1. 검증된 sealed archive(`archiveChecks=complete`, WAL 없음/0바이트)만 기존 catalog EX를 open→page query→close 전체에 유지한 ro immutable 읽기를 적용한다. 미checkpoint fallback·active·delete/rollover 규약은 유지한다.
2. Harness는 backend monitor 지연도 기존 watchdog과 대조한다. 새 route는 copy를 시작하지 않으므로 같은 호출 일정에서 실제 archive page EX 진입을 동기화 관측한다. Writer 기본100ms/10ms·SQLite100ms·기존 fixture와 시도 쓰기40·창35s를 유지한다. 새 출력 파일을 사용한다.
3. Independent test_designer는 원본 불변·sealed 조건·미checkpoint 최신 WAL/fallback·EX 전체 수명·삭제/rollover 직렬화·generation 부분 결과·monitor watchdog 판별을 검증한다. 코드+tests를 동결한 뒤 최종 후보에서 같은 250MB 두 시나리오를 한 번 측정한다. 모든 시도 기준 누락0·중복0·경합 capture_unknown0과 watchdog을 판별한다.
4. Required pass 후 fresh reviewer와 영향권 Root 회귀·scope audit로 국소 완료를 판단한다. 실패 시 기존 기준을 바꾸지 않고 원인·다음 최소 대안을 보고한다. 전체 DVL-07·DVL-12는 여전히 별도 범위다.

새 protocol/locks/queue/cache/전역 멱등화, writer 대기 수치 채택, Q01/proxy/FL-01/실제 raw/사용자 DB는 제외한다. 이전 LG-D19 실패 자료와 원 PLAN/전체 AC를 보존한다.
