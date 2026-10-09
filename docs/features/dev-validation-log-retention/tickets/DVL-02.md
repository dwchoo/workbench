# DVL-02 — precise matrix와 대기·중복 비용 단축

계획 revision p5 / SPEC s5 / BRIEF r0.9 (r0.8 승인 + 운영 원칙 개별 합의).
상태·의존성·resources·scope의 canonical source는 [PLAN.json](../PLAN.json)의 DVL-02이다.
이 ticket은 계획안이며 제품 구현/시험을 시작할 권한은 아직 없다.
기본 계약: [SPEC](../SPEC.md), [BRIEF](../BRIEF.md); source decision은 BRIEF 추적표를 따른다.

## 제공할 행동

precise의 설치 outer/shell·시간 의미·장애 경계를 제공하고 같은 항목의 전후 비용을 비교한다.

DVL-01 registry를 소비해 plain/설치된 tmux/herdr × bash/dash와 대표 장애·pause/restart/no replay 항목을 실행한다.
실제60초 review와65초 detach를 남기고 fast에는 제외 항목 미검증을 표시한다. scripted provider와 real OMP를 구분한다.
설치되어 있으나 격리 불가인 outer는 pass/skip으로 숨기지 말고 미검증 이유/종합 판정을 표시한다.
기존 assertion inventory를 대응시킨 상태에서 중복 시작·suite·비의미 sleep을 줄인다. 실제 latency/candidate/항목 일치 전후 비교를 남긴다.
셀 병렬은 전용 namespace와 실제 agent workspace/resource 검증 뒤만 허용하며 전체10분 달성을 추정하지 않는다.
FL-01~04/RM-01~03 계획은 기존 FOLLOWUPS를 보존·링크한다. 원인 수정/유료 모델/VM 실행은 이 ticket의 deliverable이 아니다.

## 수락·검증 계약

수락 ID: DV-AC-02, DV-AC-03, DV-AC-04, DV-AC-05.
G-DV-02/03/05 local owner; INT-runtime에서 최종 feature 변경 후 재실행.
Gate supplier/required evidence level 및 체크 ID는 PLAN의 gate_items/required_checks를 따른다.
전체 trace는 SPEC §11이며 local 부분과 뒤 combined coverage를 바꾸거나 삭제하지 않는다.

검증: 현재 호스트의 real OMP+scripted provider precise, outer sentinel/cleanup, bash/sh fallback, timeout 분류/첫 실패 연결; assertion 유지와 same-scope 비용 비교. 전체 회귀와 legacy 모듈도 별도로 계획.

후속 implement는 coherent candidate의 독립 test_designer 평가/시험과 frozen reviewer 검토를 포함한다.
다른 ticket 또는 실제 OMP 없이 관측 불가능한 항목을 fixture/문구 시험만으로 pass하지 않는다.
Required item unknown이면 실패 원인/한계와 evidence를 남기고 affected gate를 닫는다.

## 구현 경계·안전

시간 의미의 시험을 fake로 대체하거나 assertion을 낮춰야만 목표에 맞으면 중단·영향 보고. VM/유료 인증을 암묵적으로 사용하지 않는다.
기존 dirty/new 사용자 파일과 legacy log/DB/core 승인 증거를 보존한다.
사용자/실제 Workbench·DB·socket·tmux/herdr를 대상으로 시험하거나 정리하지 않는다.
새 제품 agent 정의/상주 OMP/독립 Python 모델, Phase2, commit·게시·배포는 범위 밖이다.
권한이 필요한 대안은 Root에 사실/대안/영향을 전달하고 임의 확장하지 않는다.

## 작업·인계

추천 worker: worker_senior; 근거: SPEC.md §4/11; C1~C7/SEL1~3 및 60s 점검.
추천은 실제 dispatch pin이 아니며 Root가 당시 code/host/권한을 재확인한다.
물리 작업 공간은 PLAN의 policy를 따르고 parallel writer는 별도 검증된 workspace/service namespace가 필요하다.
write_scope_hint는 계획 추정이며 dispatch 때 baseline·실제 경로를 검증한다. 이 Markdown에 별도 graph/status를 만들지 않는다.
중앙 PLAN/state/승인은 Root만 변경한다. Writer는 candidate delta/검사 evidence/남은 unknown을 반환한다.
현재 미검증: 현재 host의 herdr 격리와 2/10분은 pending.

## p2 리뷰 보강

core matrix만 이 단계에서 등록한다. 새 기능·Q01 재판정의 precise registry 등록은 DVL-13이며, 등록 전 해당 항목은 pending이다. 다른 harness shared 사용자와 exclusive 수정이 경합하면 직렬화한다.
