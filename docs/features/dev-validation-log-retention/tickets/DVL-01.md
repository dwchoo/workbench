# DVL-01 — fast runner와 비용 계측

계획 revision p5 / SPEC s5 / BRIEF r0.9 (r0.8 승인 + 운영 원칙 개별 합의).
상태·의존성·resources·scope의 canonical source는 [PLAN.json](../PLAN.json)의 DVL-01이다.
이 ticket은 계획안이며 제품 구현/시험을 시작할 권한은 아직 없다.
기본 계약: [SPEC](../SPEC.md), [BRIEF](../BRIEF.md); source decision은 BRIEF 추적표를 따른다.

## 제공할 행동

저장소 전용 두 모드 entrypoint와 fast smoke, 격리·readiness·watchdog·first-failure 보고 계약을 제공한다.

tools/dev_validate.py를 repo 전용으로 추가한다. fast/precise 선택과 목표 시간 표시를 구현하되 precise는 DVL-02에서 확장할 수 있는 item registry를 둔다.
fast의 실제 설치 OMP isolation/bridge 및 plain Bash 최소 product smoke를 scripted provider로 실행한다. 전체 출시 검증이라고 표시하지 않는다.
전용 환경·socket·PID/start-ticks·bounded capture/cleanup과 0건 수집 거부를 구현한다. fixed settle 대신 predicate/event readiness를 사용한다.
item/expected-wait 비용을 계측하고 동일 후보·환경의 baseline을 남긴다. 목표 초과는 overall cutoff가 아니다.
다른 호스트의 통과, 재시도 통과, provider 오류가 최초 실패를 지우거나 product compatibility pass로 바뀌지 않게 한다.

## 수락·검증 계약

수락 ID: DV-AC-01, DV-AC-02, DV-AC-04, DV-AC-05.
G-DV-01/G-DV-04의 local owner; fast 부분 DV-AC-02/05는 DVL-02 및 INT-runtime으로 결합.
Gate supplier/required evidence level 및 체크 ID는 PLAN의 gate_items/required_checks를 따른다.
전체 trace는 SPEC §11이며 local 부분과 뒤 combined coverage를 바꾸거나 삭제하지 않는다.

검증: CLI mode/target-overrun fixture와 real installed OMP plain smoke; stale identity, watchdog, empty collection, hostile inherited environment, child cleanup 반례. 기존 test_cw16_harness/test_paths_launcher를 회귀로 연결한다.

후속 implement는 coherent candidate의 독립 test_designer 평가/시험과 frozen reviewer 검토를 포함한다.
다른 ticket 또는 실제 OMP 없이 관측 불가능한 항목을 fixture/문구 시험만으로 pass하지 않는다.
Required item unknown이면 실패 원인/한계와 evidence를 남기고 affected gate를 닫는다.

## 구현 경계·안전

사용자 HOME/Workbench/multiplexer와 격리하지 못하거나 local provider 밖 모델 요청이 발생하면 중단. 타이밍 baseline이 없으면 시간 단축 수치는 pending으로 보고.
기존 dirty/new 사용자 파일과 legacy log/DB/core 승인 증거를 보존한다.
사용자/실제 Workbench·DB·socket·tmux/herdr를 대상으로 시험하거나 정리하지 않는다.
새 제품 agent 정의/상주 OMP/독립 Python 모델, Phase2, commit·게시·배포는 범위 밖이다.
권한이 필요한 대안은 Root에 사실/대안/영향을 전달하고 임의 확장하지 않는다.

## 작업·인계

추천 worker: worker_senior; 근거: SPEC.md §4; 기존 cw16_harness의 isolation/owned cleanup.
추천은 실제 dispatch pin이 아니며 Root가 당시 code/host/권한을 재확인한다.
물리 작업 공간은 PLAN의 policy를 따르고 parallel writer는 별도 검증된 workspace/service namespace가 필요하다.
write_scope_hint는 계획 추정이며 dispatch 때 baseline·실제 경로를 검증한다. 이 Markdown에 별도 graph/status를 만들지 않는다.
중앙 PLAN/state/승인은 Root만 변경한다. Writer는 candidate delta/검사 evidence/남은 unknown을 반환한다.
현재 미검증: readiness/실제 child isolation/cleanup은 fixture만으로 입증 불가.

## p2 리뷰 보강

후속 새 기능 registry 등록은 DVL-13이다. 이 ticket에서 Q01/DB/요약의 미래 runtime을 이미 검증했다고 표시하지 않는다.
