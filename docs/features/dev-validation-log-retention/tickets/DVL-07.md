# DVL-07 — 통합 조회·사용자 정리 CLI·지속 TUI 알림

계획 revision p5 / SPEC s5 / BRIEF r0.9 (r0.8 승인 + 운영 원칙 개별 합의).
상태·의존성·resources·scope의 canonical source는 [PLAN.json](../PLAN.json)의 DVL-07이다.
이 ticket은 계획안이며 제품 구현/시험을 시작할 권한은 아직 없다.
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
