# DVL-04 — 수집 중 완료 근거와 보수적 결과 판정

계획 revision p5 / SPEC s5 / BRIEF r0.9 (r0.8 승인 + 운영 원칙 개별 합의).
상태·의존성·resources·scope의 canonical source는 [PLAN.json](../PLAN.json)의 DVL-04이다.
이 ticket은 계획안이며 제품 구현/시험을 시작할 권한은 아직 없다.
기본 계약: [SPEC](../SPEC.md), [BRIEF](../BRIEF.md); source decision은 BRIEF 추적표를 따른다.

## 제공할 행동

승인 log_contains를 수집 때 exact match하고 운영 metadata에 보존해 raw 유실 후에도 올바르게 판단한다.

Raw writer 이전에 run/revision 고정 matcher를 연결한다. chunk split・긴 pattern・중복 byte・source gap을 추적한다.
matched proof는 최소 excerpt<=4KiB+전체 condition/match digest/offset과 source identity로 영속화하고 commit 성공 전 judge에 사용하지 않는다.
완전 관측 no-match만 failure, gap/collector restart no-match는 indeterminate다. Nonzero exit 등 독립 실패와 result provenance 조건은 유지한다.
조건 revision 바뀜/이미 완료된 run/다른 subject의 근거를 섞지 않는다. proof는 중요 DB 및120일 정리 밖에 둔다.
Legacy proof 없는 run은 기존 conservative read path로 판단하고 C-D46 저장 실패에서 새 자동 실행 hold/기존 실험 유지 경계를 지킨다.

## 수락·검증 계약

수락 ID: LG-AC-03.
G-LG-03 owner; local legacy quota/write-loss fixture로 독립 검증하고 최종 rotated-source scenario는 INT-runtime.
Gate supplier/required evidence level 및 체크 ID는 PLAN의 gate_items/required_checks를 따른다.
전체 trace는 SPEC §11이며 local 부분과 뒤 combined coverage를 바꾸거나 삭제하지 않는다.

검증: split every pattern boundary, false positive/negative, full no-match vs gap, raw cap/write loss, metadata commit fault, revision mismatch, reopen durable proof; result_file mutation/nonzero exit/legacy 판단 회귀.

후속 implement는 coherent candidate의 독립 test_designer 평가/시험과 frozen reviewer 검토를 포함한다.
다른 ticket 또는 실제 OMP 없이 관측 불가능한 항목을 fixture/문구 시험만으로 pass하지 않는다.
Required item unknown이면 실패 원인/한계와 evidence를 남기고 affected gate를 닫는다.

## 구현 경계·안전

Collector gap-free 또는 영속 commit을 증명할 수 없으면 success/no-match failure를 주장하지 않고 indeterminate/hold로 남긴다.
기존 dirty/new 사용자 파일과 legacy log/DB/core 승인 증거를 보존한다.
사용자/실제 Workbench·DB·socket·tmux/herdr를 대상으로 시험하거나 정리하지 않는다.
새 제품 agent 정의/상주 OMP/독립 Python 모델, Phase2, commit·게시·배포는 범위 밖이다.
권한이 필요한 대안은 Root에 사실/대안/영향을 전달하고 임의 확장하지 않는다.

## 작업·인계

추천 worker: worker_senior; 근거: SPEC.md §6; _drain/_analyse/C-D46.
추천은 실제 dispatch pin이 아니며 Root가 당시 code/host/권한을 재확인한다.
물리 작업 공간은 PLAN의 policy를 따르고 parallel writer는 별도 검증된 workspace/service namespace가 필요하다.
write_scope_hint는 계획 추정이며 dispatch 때 baseline·실제 경로를 검증한다. 이 Markdown에 별도 graph/status를 만들지 않는다.
중앙 PLAN/state/승인은 Root만 변경한다. Writer는 candidate delta/검사 evidence/남은 unknown을 반환한다.
현재 미검증: 원문 rotation과 product wiring은 DVL-11/12 공급 후 INT-runtime.

## p2 리뷰 보강

동결 logs 계약을 소비하므로 log-contract:v1 shared claim을 지킨다. 계약 수정이 필요하면 exclusive 재예약·버전/소비자 재검증 없이 바꾸지 않는다.
