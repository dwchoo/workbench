# DVL-11 — 비활성 공용 segmented raw engine

계획 revision p5 / SPEC s5 / BRIEF r0.9 (r0.8 승인 + 운영 원칙 개별 합의).
상태·의존성·resources·scope의 canonical source는 [PLAN.json](../PLAN.json)의 DVL-11이다.
이 ticket은 계획안이며 제품 구현/시험을 시작할 권한은 아직 없다.
기본 계약: [SPEC](../SPEC.md), [BRIEF](../BRIEF.md); source decision은 BRIEF 추적표를 따른다.

## 제공할 행동

legacy와 별도인 source-framed 100MB×총5 segment store/reader를 기본 비활성 상태로 제공한다.

DVL-03 SourceRange/RawReadResult와 DVL-04 proof-first seam을 사용한 새 엔진을 구현한다.
100,000,000 byte/file(헤더포함), active 포함 총5, 최대64KiB record payload를 split하고 exact cap을 유지한다.
source offset/epoch/digest/time framing, retained/evicted/missing index, source별 사용량·lost range, partial write 및 rotate crash recovery를 구현한다.
원문관측/display/proof는 엔진 앞에 있으므로 write error/rotate가 execution stop 신호가 되지 않는다.
Legacy 디렉터리/파일을 migration/delete하지 않는다. production injection/activation은 DVL-12에만 둔다.
fixtures의 작은 cap으로 explicit isolated engine rotation을 시험하되 실제 user activation이나 Q01 pass로 기록하지 않는다.

## 수락·검증 계약

수락 ID: LG-AC-01, LG-AC-02, LG-AC-03, LG-AC-04.
RAW-engine fixture owner; full LG-AC-01/02/04는 DVL-12, rotated judge final은 INT-runtime.
Gate supplier/required evidence level 및 체크 ID는 PLAN의 gate_items/required_checks를 따른다.
전체 trace는 SPEC §11이며 local 부분과 뒤 combined coverage를 바꾸거나 삭제하지 않는다.

검증: exact cap incl frame,5개 incl active, mixed run/command starvation visibility, multi-chunk bytes/offset reader/gap, partial/crash/concurrent opens, unsafe path/mode/lock, legacy hash unchanged, proof survives evicted fixture.

후속 implement는 coherent candidate의 독립 test_designer 평가/시험과 frozen reviewer 검토를 포함한다.
다른 ticket 또는 실제 OMP 없이 관측 불가능한 항목을 fixture/문구 시험만으로 pass하지 않는다.
Required item unknown이면 실패 원인/한계와 evidence를 남기고 affected gate를 닫는다.

## 구현 경계·안전

legacy 사용자 파일이나 전용 root 밖을 삭제하거나 source gaps를 full output으로 보여야 한다면 중단. Q01 실패여도 이 ticket은 disabled engine/fixture 범위만 가능.
기존 dirty/new 사용자 파일과 legacy log/DB/core 승인 증거를 보존한다.
사용자/실제 Workbench·DB·socket·tmux/herdr를 대상으로 시험하거나 정리하지 않는다.
새 제품 agent 정의/상주 OMP/독립 Python 모델, Phase2, commit·게시·배포는 범위 밖이다.
권한이 필요한 대안은 Root에 사실/대안/영향을 전달하고 임의 확장하지 않는다.

## 작업·인계

추천 worker: worker_senior; 근거: SPEC.md §9; owned raw store and proof interface.
추천은 실제 dispatch pin이 아니며 Root가 당시 code/host/권한을 재확인한다.
물리 작업 공간은 PLAN의 policy를 따르고 parallel writer는 별도 검증된 workspace/service namespace가 필요하다.
write_scope_hint는 계획 추정이며 dispatch 때 baseline·실제 경로를 검증한다. 이 Markdown에 별도 graph/status를 만들지 않는다.
중앙 PLAN/state/승인은 Root만 변경한다. Writer는 candidate delta/검사 evidence/남은 unknown을 반환한다.
현재 미검증: product legacy readers/DONE 문구/activation은 DVL-12.

## p2 리뷰 보강

log-contract:v1 shared로 동결 계약을 소비한다. 모델 지연/rotation 임박에 긴급 모델 호출이 없고 bounded payload/buffer만 유지함을 injectable port로 확인한다.
