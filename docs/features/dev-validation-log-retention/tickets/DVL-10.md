# DVL-10 — 5분·종료 요약 scheduler와 재시작/no replay

계획 revision p5 / SPEC s5 / BRIEF r0.9 (r0.8 승인 + 운영 원칙 개별 합의).
상태·의존성·resources·scope의 canonical source는 [PLAN.json](../PLAN.json)의 DVL-10이다.
이 ticket은 계획안이며 제품 구현/시험을 시작할 권한은 아직 없다.
기본 계약: [SPEC](../SPEC.md), [BRIEF](../BRIEF.md); source decision은 BRIEF 추적표를 따른다.

## 제공할 행동

새 정보 coalesce·승인·pause·storage 장애·late/no replay를 기존 실행보다 낮은 우선순위로 연결한다.

DVL-05의 Subject/authority와 DVL-06 health/storage 상태, DVL-07 status, DVL-09 adapter를 조합한다.
dirty high-water 기준300초+종료 event, new-info 없음 skip, Task command dedup, cursor-only coalesce, 한 inflight 및 bounded 후보 page를 구현한다.
사용자/기존 종료·60초 check가 우선이며 요약은 기존 delivery lock 앞에서 대기한다. 의미 없는60초 hard completion limit을 추가하지 않는다.
Task 최신승인/활성·종료 후처리 참조와 직접 command grant의 수명·pause/revoke를 재대조한다. 허용 불명은 수집만 한다.
prepared/sending/executed/stored/not_sent/unknown/lost range를 영속 대조한다. 확정 미실행만 retry; 실행/불명은 자동 reinclude 금지.
late first valid만 저장, duplicate 폐기 기록, 실패로 last-good overwrite 금지. 저장불능이면 신규 호출 보류/상태 표시/가벼운 복구 후 다음주기.
model 오류/lag/raw 누락은 기존 host와 가능한 수집을 유지하고 usage/unknown/delay를 TUI/status에 제공한다.

## 수락·검증 계약

수락 ID: LG-AC-05, LG-AC-07, LG-AC-08, LG-AC-14, LG-AC-18, LG-AC-19, LG-AC-22, LG-AC-23.
G-LG-05/07/08/14/18/19/22/23 owner. v2/live UI combined 관측은 INT-runtime.
Gate supplier/required evidence level 및 체크 ID는 PLAN의 gate_items/required_checks를 따른다.
전체 trace는 SPEC §11이며 local 부분과 뒤 combined coverage를 바꾸거나 삭제하지 않는다.

검증: FakeClock의299/300/종료와 real scripted OMP priority/skill/result, pause race/permission expiry/direct command, restart every request state, output rejected/lost/storage fault, late duplicate, raw missing adapter fixture, healthy recovery/no automatic resume.

후속 implement는 coherent candidate의 독립 test_designer 평가/시험과 frozen reviewer 검토를 포함한다.
다른 ticket 또는 실제 OMP 없이 관측 불가능한 항목을 fixture/문구 시험만으로 pass하지 않는다.
Required item unknown이면 실패 원인/한계와 evidence를 남기고 affected gate를 닫는다.

## 구현 경계·안전

개발 Q01 근거 stale/failed 또는 command 작업 범위·관련 자료 연결/종료 후처리가 불명이면 해당 구현 확대를 중단한다. LG-D24로 개별 사용자 발화→command 일대일 증명은 요구하지 않는다. 제품 운영의 OMP version-only 실행 예외는 SPEC §8.1이며 개발 gate를 완화하지 않는다. 입력/요청 ID/skill 변경으로 no replay를 우회하지 않는다.
기존 dirty/new 사용자 파일과 legacy log/DB/core 승인 증거를 보존한다.
사용자/실제 Workbench·DB·socket·tmux/herdr를 대상으로 시험하거나 정리하지 않는다.
새 제품 agent 정의/상주 OMP/독립 Python 모델, Phase2, commit·게시·배포는 범위 밖이다.
권한이 필요한 대안은 Root에 사실/대안/영향을 전달하고 임의 확장하지 않는다.

## 작업·인계

추천 worker: worker_senior; 근거: SPEC.md §8.3; LG-AC-18/19/22/23.
추천은 실제 dispatch pin이 아니며 Root가 당시 code/host/권한을 재확인한다.
물리 작업 공간은 PLAN의 policy를 따르고 parallel writer는 별도 검증된 workspace/service namespace가 필요하다.
write_scope_hint는 계획 추정이며 dispatch 때 baseline·실제 경로를 검증한다. 이 Markdown에 별도 graph/status를 만들지 않는다.
중앙 PLAN/state/승인은 Root만 변경한다. Writer는 candidate delta/검사 evidence/남은 unknown을 반환한다.
현재 미검증: 실제 v2 eviction integration은 DVL-14/13; paid model behavior는 RM TODO.

## p2 리뷰 보강

SPEC §8.3의 event/종결 사유 표를 그대로 소비한다. 취소/철회는 LG-D14로 합의되어 수집만 하고 새 종료 요약을 호출하지 않는다. finished/blocked/held/run end는 Task 종료 trigger가 아니며 주기 호출도 기존 승인·활성·unpaused가 유효할 때만이다. 기존 전송된 유효 late 결과 저장과 새 호출을 구분한다. 빠른 eviction/lag에서 긴급 추가 호출0 및 buffer/cursor 상한을 직접 assertion으로 검증한다. v2 활성 mixed는14/13에서 검증한다.

## p3 운영 상태 연계

14의 호환 보호 서비스에 연결할 주입 포트를 제공한다. Local gate는 보호/정상/version-drift 상태를 계약 fixture로
검증하고 후행14를 supplier로 요구하지 않는다. 실제 제품 drift·사건 알림·신규 source 정책 교차 수락은14/13이다.
일시적 provider/연결 오류만으로 비호환을 확정하지 않고 기존 no replay·last-good 보존을 유지한다.

## p4 반복 계약 거부·보호 (P5)

SPEC §8.3의 executed+contract-rejected 연속3회 영속 streak/circuit breaker를 구현한다. 새 ID·OMP 업데이트·
restart로 reset하지 않고 provider/통신/요금/저장 장애는 올리거나 reset하지 않는다. 정상 검증·저장 결과만 reset.
Guard/provenance 안전불능은 즉시 보호,결과 계약 거부는 summary_contract_failed로 원인불명을 구분한다.
14의 보호/admission 서비스를 주입할 포트만 local fixture로 확인한다. 실제 신규 source legacy-stop·cohort/알림
교차 시험은14/13이며 후행14를 local gate supplier로 만들지 않는다. 명시 verify 복구 전 자동 loop/no replay없음.

## p5 복구 시도와 착수 scope (R2·R5)

Scripted verify의 plumbing pass를 실모델 계약 복구 pass로 읽지 않는다. 보호 뒤 명시 verify의 확인된 경계
복구 참조로 recovery_attempt를 영속하고 다음 정규 호출의 새 미실행 입력만 시도한다. 계약 거부1회면 즉시
재보호,최신 정상 검증·stored 결과 뒤에만 통상3회 감시로 복귀한다. Transient/storage/pause는 별도 경계다.
Attempt/episode는restart·OMP update·old late로 reset하지 않는다. 재발은새episode알림1회 포트이며 실제알림은14/13.
추가모델시험/실패범위replay는 없다. Local gate는 recovery/event 포트 fixture로 관측해14를 supplier로 요구하지 않는다.
PLAN의 backend test scope는 명시파일 목록이다. Root는 audit allowed에08동결판정 파일이 없음과10통합전후
freeze digest불변을 확인한다. Helper가 write_scope_exclusions를 강제한다고 가정하지 않는다. 추가시험경로는
Root가 dispatch scope를 먼저 갱신한다.
