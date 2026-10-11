# DVL-05 — Subject 수집·보관 종결·호출 권한 provenance

계획 revision p5 / SPEC s5 / BRIEF r0.9 (r0.8 승인 + 운영 원칙 개별 합의).
상태·의존성·resources·scope의 canonical source는 [PLAN.json](../PLAN.json)의 DVL-05이다.
이 ticket은 계획안이며 제품 구현/시험을 시작할 권한은 아직 없다.
기본 계약: [SPEC](../SPEC.md), [BRIEF](../BRIEF.md); source decision은 BRIEF 추적표를 따른다.

## 제공할 행동

기존 이벤트를 Task/command 중요 기록에 연결하고 core 상태와 별개인 보관·승인 사실을 영속화한다.

전달된 spec/instruction/report/확인 decision과 terminal SourceRange만 기록한다. 전체 사용자 대화 수집/인위적 Task 생성은 없다.
Task 있는 command는 Task subject 하나로 연결하고 Task 없는 것은 command ID다. core closed 시각/reason과 command actual end를 별도 중요 DB에 복사한다.
finished/blocked/held/unknown command는 보호한다. archival closure/re-activity API는 core success/exit/active 상태를 변경하지 않는다.
Task의 approval/proceed/현재 scope·pause·철회와 command ID·작업 범위·관련 source 참조를 기록하는 bounded authority port를 만든다.
LG-D24에 따라 개별 사용자 발화→command 증명은 요구하지 않는다. 기존 injectable fixture port를 유지하고 실제 작업/자료 연결은 DVL-08 검증 후 DVL-09/10이 제공한다. 새 권한 서비스나 전체 대화 수집을 추가하지 않는다.
Flow journal만 남아 있고 중요 DB 기록이 실패했다면 미확인을 표시하고 재시작 때 확인 가능한 사실만 복사한다. 모델 호출/재실행을 trigger하지 않는다.

## 수락·검증 계약

수락 ID: LG-AC-05, LG-AC-14, LG-AC-21, LG-AC-22.
LIFECYCLE local owner; LG-AC-21 CLI는 DVL-07, 실제 LG-AC-14/22는 DVL-10.
Gate supplier/required evidence level 및 체크 ID는 PLAN의 gate_items/required_checks를 따른다.
전체 trace는 SPEC §11이며 local 부분과 뒤 combined coverage를 바꾸거나 삭제하지 않는다.

검증: 모든 closed reason/time vs finished/blocked/held, actual end vs unknown, re-activity 보호, Task command dedup, no-Task, unrelated grant/pause/revoke 및 late event; 원문 대화 비수집 확인.

후속 implement는 coherent candidate의 독립 test_designer 평가/시험과 frozen reviewer 검토를 포함한다.
다른 ticket 또는 실제 OMP 없이 관측 불가능한 항목을 fixture/문구 시험만으로 pass하지 않는다.
Required item unknown이면 실패 원인/한계와 evidence를 남기고 affected gate를 닫는다.

## 구현 경계·안전

기존 Manager 완료 확정 기록이나 작업 연결을 invent하면 중단. 기존 core 실행 승인·작업 범위를 유지하며 연결 불명 자료는 수집 사실과 미확인만 보존한다.
기존 dirty/new 사용자 파일과 legacy log/DB/core 승인 증거를 보존한다.
사용자/실제 Workbench·DB·socket·tmux/herdr를 대상으로 시험하거나 정리하지 않는다.
새 제품 agent 정의/상주 OMP/독립 Python 모델, Phase2, commit·게시·배포는 범위 밖이다.
권한이 필요한 대안은 Root에 사실/대안/영향을 전달하고 임의 확장하지 않는다.

## 작업·인계

추천 worker: worker_senior; 근거: SPEC.md §5/7.2/8.3; task_closed/finished/blocked/held.
추천은 실제 dispatch pin이 아니며 Root가 당시 code/host/권한을 재확인한다.
물리 작업 공간은 PLAN의 policy를 따르고 parallel writer는 별도 검증된 workspace/service namespace가 필요하다.
write_scope_hint는 계획 추정이며 dispatch 때 baseline·실제 경로를 검증한다. 이 Markdown에 별도 graph/status를 만들지 않는다.
중앙 PLAN/state/승인은 Root만 변경한다. Writer는 candidate delta/검사 evidence/남은 unknown을 반환한다.
현재 미검증: LG-D24 작업/자료 연결과 종료 후처리의 실제 통합은 Q01-origin pending. 이전 direct-instruction API 미확인은 새 기준의 선행조건이 아니다.

## p2 리뷰 보강

task_closed 사유/직전 승인·활성 fact와 event/input cursor를 복사하되 새 종료 모델 호출은 이 ticket에서 만들지 않는다. SPEC §8.3의 보관 종결과 요약 trigger를 분리한다. 취소/철회는 LG-D14로 수집만·새 호출 없음이 합의되었으며 이 ticket에서는 injectable consumer로 종결/권한 fact 수집을 검증한다.
