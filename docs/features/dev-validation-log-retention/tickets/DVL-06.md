# DVL-06 — SQLite 상태·retention·rollover와 저장 장애

계획 revision p5 / SPEC s5 / BRIEF r0.9 (r0.8 승인 + 운영 원칙 개별 합의).
상태·의존성·resources·scope의 canonical source는 [PLAN.json](../PLAN.json)의 DVL-06이다.
이 ticket은 계획안이며 제품 구현/시험을 시작할 권한은 아직 없다.
기본 계약: [SPEC](../SPEC.md), [BRIEF](../BRIEF.md); source decision은 BRIEF 추적표를 따른다.

## 제공할 행동

논리/물리 DB 상태, 활성-only 보존 정리와 crash-safe 전환·archive 보호를 제공한다.

250MB decimal live allocation과225MB low-water, main/WAL/SHM physical/freelist를 구분하고 증분 통계를 pre/post 제공한다.
active DB만120일/압력 종결 기록→열린 중간 요약 순으로 정리하고 start/latest 우선 및 gap을 남긴다. archive와 core metadata는 정리하지 않는다.
bounded quick_check/maintenance/checkpoint/reader lifetime 및 busy/stale 상태를 구현한다. 삭제 후 main 크기가 그대로여도 즉시 full로 오판하지 않는다.
정리 소진 시 UUID DB 생성·검증·원자 active pointer·recovery intent로 전환하고 이전 파일/WAL을 보존한다.
반복 anchor 복사/recursive rollover는 피하고 삭제 전 보호 이월 API를 제공한다.
normal target와 physical free/error/corruption/unsafe path를 구분하고 lightweight recovery state를 서비스에 제공한다. 여기서는 모델 호출 정책을 연결하지 않는다.

## 수락·검증 계약

수락 ID: LG-AC-09, LG-AC-10, LG-AC-11, LG-AC-12, LG-AC-15, LG-AC-17, LG-AC-23.
G-LG-09/10/11/12/15/17 owner; G-LG-23은 DVL-10이 storage 공급을 결합.
Gate supplier/required evidence level 및 체크 ID는 PLAN의 gate_items/required_checks를 따른다.
전체 trace는 SPEC §11이며 local 부분과 뒤 combined coverage를 바꾸거나 삭제하지 않는다.

검증: 작은 cap/clock으로120일 경계·capacity 조기 정리·ongoing anchor·archive 불변, busy reader/WAL growth/reusable pages, crash every publish boundary, concurrent writes, ENOSPC/EACCES/corruption, cached stats/maintenance bound.

후속 implement는 coherent candidate의 독립 test_designer 평가/시험과 frozen reviewer 검토를 포함한다.
다른 ticket 또는 실제 OMP 없이 관측 불가능한 항목을 fixture/문구 시험만으로 pass하지 않는다.
Required item unknown이면 실패 원인/한계와 evidence를 남기고 affected gate를 닫는다.

## 구현 경계·안전

active UUID가 불명확하거나 checkpoint 미완료인데 WAL을 버려야 한다면 중단. 보존 DB 자동 삭제/대량 foreground VACUUM은 하지 않는다.
기존 dirty/new 사용자 파일과 legacy log/DB/core 승인 증거를 보존한다.
사용자/실제 Workbench·DB·socket·tmux/herdr를 대상으로 시험하거나 정리하지 않는다.
새 제품 agent 정의/상주 OMP/독립 Python 모델, Phase2, commit·게시·배포는 범위 밖이다.
권한이 필요한 대안은 Root에 사실/대안/영향을 전달하고 임의 확장하지 않는다.

## 작업·인계

추천 worker: worker_senior; 근거: SPEC.md §7; LG-D08/09/11.
추천은 실제 dispatch pin이 아니며 Root가 당시 code/host/권한을 재확인한다.
물리 작업 공간은 PLAN의 policy를 따르고 parallel writer는 별도 검증된 workspace/service namespace가 필요하다.
write_scope_hint는 계획 추정이며 dispatch 때 baseline·실제 경로를 검증한다. 이 Markdown에 별도 graph/status를 만들지 않는다.
중앙 PLAN/state/승인은 Root만 변경한다. Writer는 candidate delta/검사 evidence/남은 unknown을 반환한다.
현재 미검증: 실제 scheduler의 저장불능 호출 차단은 DVL-10; TUI multi-DB는 DVL-07.

## p2 리뷰 보강

운영 metadata/proof/request journal의 별도 사용량·추정/unknown을 HealthSnapshot에 제공한다. 중요 DB 250MB/120일 정책을 운영 metadata에 적용하지 않는다.
