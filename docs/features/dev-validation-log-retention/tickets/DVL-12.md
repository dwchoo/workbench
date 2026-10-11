# DVL-12 — 비활성 raw 주입·reader와 출처별 조회

계획 revision p5 / SPEC s5 / BRIEF r0.9 (r0.8 승인 + 운영 원칙 개별 합의).
상태·의존성·resources·scope의 canonical source는 [PLAN.json](../PLAN.json)의 DVL-12이다.
원 s5/p5 승인과 [2026-10-10 두 결정·격리 재개 승인](../../../../.workflow/dev-validation-log-retention/runs/implement-p5-20261009/resume-decisions-20261010.json)을 함께 따른다.
Q01 기준 변경·proxy 채택·FL-01 제품 수정·실제 raw 활성화는 제외한다.
기본 계약: [SPEC](../SPEC.md) §5/9/10, [BRIEF](../BRIEF.md).

## 제공할 행동

DVL-07의 통합 조회와 DVL-11의 disabled engine을 소비해 비활성 주입 port·reader/descriptor·출처별 raw 조회·legacy 별도 용량을 연결한다.
Q01과 DVL-10이 pending/failed여도 이 행동은 구현·검증할 수 있다. activation CLI/정책 전환은 DVL-14의 별도 scope다.
production 기본 legacy-stop과 기존 실험64MiB/run·512MiB/project 및 command64MiB를 그대로 유지한다.
source writer/reader 선택 port는 v2 비활성이고 unknown policy도 삭제를 켜지 않는다. fixture에만 disabled segmented source를 주입한다.
opaque raw_log_ref/descriptor와 read command·remaining/evicted/gap를 제공한다. DONE_INSTRUCTION/task command/report/skill 소비자의 full-output 가정을 함께 제거하되 legacy 설명/파일은 보존한다.
Raw CLI/Agent 조회는 모델 호출·정리·activation 없이 남은 범위만 반환한다. 기존/신규 사용량은 별도 표시한다.
summary는 injectable disabled port이며 실제 adapter·요약 결과를 완료 조건으로 요구하지 않는다.

## 수락·검증 계약

수락 ID: LG-AC-02, LG-AC-03, LG-AC-04, LG-AC-13.
G-LG-02/04 owner. 실제 activation/mixed v2는14, proof/summary/readonly 교차 수락은 INT-runtime이다.
계약: PLAN gate_items/required_checks의 supplier·증거 수준과 SPEC §11 추적을 유지한다.
검증: Q01 pending/failed 상태에서도 legacy 기본/cap/hash·별도 크기, injected disabled descriptor/reader/DONE/Agent 조회, source별 eviction/gap, 조회 model/cleanup/activation counter0.

## 구현 경계·작업 인계

Q01 pass를 조회 전제조건으로 삼지 않는다. 반대로 이 ticket 완료로 production v2 삭제·요약·activation이 검증됐다고 하지 않는다.
기존 사용자 Workbench·로그·DB·socket·tmux/herdr를 건드리지 않고 전용 namespace만 사용한다.
coherent candidate의 독립 test_designer·frozen reviewer 및 필요한 수정/검사를 포함한다.
추천 worker: worker_senior. PLAN의 write scope와 shared/exclusive claim은 dispatch 시 재확인한다.
계약 수정은 log-contract:v1 exclusive·version/digest·영향 소비자 재검증을 요구한다.
새 제품 agent/상주 OMP, paid model, 실제 raw 활성화, core 승인 bundle 수정, commit·배포는 범위 밖이다.
중앙 PLAN/state/승인은 Root만 변경한다. candidate delta·검사 근거·미확인을 반환한다.
