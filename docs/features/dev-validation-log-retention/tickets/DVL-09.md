# DVL-09 — 채택된 adapter와 내부 important-log skill

계획 revision p5 / SPEC s5 / BRIEF r0.9 (r0.8 승인 + 운영 원칙 개별 합의).
상태·의존성·resources·scope의 canonical source는 [PLAN.json](../PLAN.json)의 DVL-09이다.
이 ticket은 계획안이며 제품 구현/시험을 시작할 권한은 아직 없다.
기본 계약: [SPEC](../SPEC.md), [BRIEF](../BRIEF.md); source decision은 BRIEF 추적표를 따른다.

## 제공할 행동

Q-01 통과 경로를 제품 typed adapter로 연결하고 기존 agent에 내부 skill을 확정 전달한다.

DVL-08의 채택된 existing-agent path와 DVL-03 contract만 구현한다. guard mechanism/structured channel을 다른 가정으로 갈아끼우지 않는다.
내부 영어 skill에 선정/사실·해석·unknown/출처/누적/DB health/입력 지시 무시/출력 schema를 넣고 digest를 입력과 결과에 묶는다.
요약 request 동안 실제 수정/terminal/side-effect 도구를 enforce 차단하고 기존 일반 Manager/Worker 도구 권한은 그 밖에서 유지한다.
role skill allowlist/isolation 기대값을 실제 채택 role만 변경하고 기존 skill 크기/동작을 회귀로 검증한다.
SPEC §8.2의 수집/전송/token/context budget·8KiB result·parent receipt bound, request/source/origin/authorization refs/duplicate 검증 및 usage unknown shape를 제공한다.
Python direct model 판단이나 새 agent 정의/상주 프로세스를 추가하지 않는다.

## 수락·검증 계약

수락 ID: LG-AC-05, LG-AC-06, LG-AC-18, LG-AC-19, LG-AC-22.
G-LG-06, Q01-contract, SUMMARY-budget owner; LG-AC-05/18/19/22 전체는 DVL-10과 INT-runtime.
Gate supplier/required evidence level 및 체크 ID는 PLAN의 gate_items/required_checks를 따른다.
전체 trace는 SPEC §11이며 local 부분과 뒤 combined coverage를 바꾸거나 삭제하지 않는다.

검증: 실제 runtime Q01 경로에서 skill load/digest/filter, adversarial tool call guard, normal role restore, wrong source/result/drop·size bound/usage; existing skills/isolation/bridge 회귀.

후속 implement는 coherent candidate의 독립 test_designer 평가/시험과 frozen reviewer 검토를 포함한다.
다른 ticket 또는 실제 OMP 없이 관측 불가능한 항목을 fixture/문구 시험만으로 pass하지 않는다.
Required item unknown이면 실패 원인/한계와 evidence를 남기고 affected gate를 닫는다.

## 구현 경계·안전

원 runtime 근거와 현재 projection 대조/필요 재실행 및 해당 경로 채택 근거가 없으면 시작하지 않는다. 기존 agent 활용 의미가 바뀌면 Root가 사용자 대안을 요청.
기존 dirty/new 사용자 파일과 legacy log/DB/core 승인 증거를 보존한다.
사용자/실제 Workbench·DB·socket·tmux/herdr를 대상으로 시험하거나 정리하지 않는다.
새 제품 agent 정의/상주 OMP/독립 Python 모델, Phase2, commit·게시·배포는 범위 밖이다.
권한이 필요한 대안은 Root에 사실/대안/영향을 전달하고 임의 확장하지 않는다.

## 작업·인계

추천 worker: worker_senior; 근거: SPEC.md §8.1/8.2; current ROLE_SKILL_PATTERNS and isolation.
추천은 실제 dispatch pin이 아니며 Root가 당시 code/host/권한을 재확인한다.
물리 작업 공간은 PLAN의 policy를 따르고 parallel writer는 별도 검증된 workspace/service namespace가 필요하다.
write_scope_hint는 계획 추정이며 dispatch 때 baseline·실제 경로를 검증한다. 이 Markdown에 별도 graph/status를 만들지 않는다.
중앙 PLAN/state/승인은 Root만 변경한다. Writer는 candidate delta/검사 evidence/남은 unknown을 반환한다.
현재 미검증: 실제 배포 adapter로 Q01 pass를 재확인해야 함.

## p2 리뷰 보강

Q01-contract gate: 08 probe-local/03 Python·TS/배포 adapter envelope를 같은 golden 및 음성 fixture로 비교한다. ID/range/authority/usage 의미까지 확인하고 adapter 변경 후8개 runtime 재판정한다. SUMMARY-budget gate는1MiB 수집과 실제32KiB/token/context 전송 budget을 분리하고 미확인 preflight 차단·결정/보고 우선 선정·누락 range·요청별 추정/actual usage/unknown을 검증한다. 수치의 실모델 적합성은 RM-input-budget TODO다.

## p3 입력·출력과 제품 재판정 (N2·N3·N5)

Adapter 통합 뒤 q01_recheck --adapter product로8개를 재판정하고 closure/validator 영향 변경도 다시 검토한다.
Result 전체8KiB와 실제 enforce output token ceiling(초기8192 이하)을 별도 제한하며 preflight는 그 token ceiling
전체를 예약한다. 출력 한도/필수context 불명은 차단, 초과·미완결 JSON은 거부한다. 실제 configured model의
metadata discoverability 관측은14 MODEL-metadata에서 수행하므로09의 fake fixture를 그 runtime pass로 쓰지 않는다.

## p4 guard·session budget·동결 도구 소비 (P1·P4·P6~P8)

SPEC §8.1의 GuardReceipt와 epoch/lease를 확인한 요청만 전송한다. 확인 불능은 전송0이며 일반 role 권한 복구도
검증한다. Epoch 교체/stale ack를 음성 fixture에 포함한다. q01_recheck/판정 fixture는08의 동결본을 읽기만 한다.
09 worker가 자기 판정 tool을 수정하지 않으며 필요 변경은 Root가08 follow-up 독립 검토/동결로 분리한다.
실제 session model/thinking/tier와 reasoning이 ceiling에 포함되는 의미/예산을 request preflight port에 결속한다.
Observer의 네트워크0/auth-store open0/설정쓰기0,모델 교체 뒤 재확인/send0를 fixture로 검증한다.
8KiB visible JSON budget+reasoning 예약에서 enforce ceiling을 산출/전량예약하고 불명은 차단한다.8192는상한이지
목표출력량이 아니다. 사용자 model/effort/tier를 낮추지 않는다. 실모델 관측은14/RM이고 fake fixture는그pass가아니다.

## p5 실행 중 guard 감시·route metadata (R1·R4)

Guard 설정은 await 후 같은 epoch의 read-back으로 확인한다. hideBridgeTools의 fire-and-forget/오류 무시를
재사용하지 않는다. 요약 request/turn에 귀속된 trusted response/event의 toolCall/tool_use·비허용 실행을 감시해
위반 결과를 거부하고14 보호 포트에 연결한다. 확인된 비허용 실행은 compatibility_failed,시도/불명은
guard_violation_observed로 구분한다. Raw 문자열·자기보고·상위agent 일반turn을 실행근거로 오인하지 않는다.
허용 결과 전달 protocol의 typed result는 출처로 구분하고 사후감시만으로 예방 guard를 대신하지 않는다.
R4 착수 포트는 공유현재session 또는 채택격리route의생성context 해석 read-back을 모델전송 전에 반환한다.
role/부모상속·model epoch출처와 불명/변경 음성을 포함하며 직접설정표추정/모델call로metadata보충하지 않는다.
