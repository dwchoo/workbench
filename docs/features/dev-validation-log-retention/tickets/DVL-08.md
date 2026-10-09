# DVL-08 — Q-01 기존 agent 요약 경로 최소 판별

계획 revision p5 / SPEC s5 / BRIEF r0.9 (r0.8 승인 + 운영 원칙 개별 합의).
상태·의존성·resources·scope의 canonical source는 [PLAN.json](../PLAN.json)의 DVL-08이다.
이 ticket은 계획안이며 제품 구현/시험을 시작할 권한은 아직 없다.
기본 계약: [SPEC](../SPEC.md), [BRIEF](../BRIEF.md); source decision은 BRIEF 추적표를 따른다.

## 제공할 행동

실제 설치 OMP의 기존 agent/skill/tools/typed result/priority/provenance 가능성을 작은 격리 실험으로 판별한다.

SPEC의 Q01 8개 item별 pass/fail/inconclusive/blocked_adoption를 먼저 고정하고 작은 runtime probe만 구현한다.
기존 agent/parent 공개 API를 조사하고 isolated OMP+scripted provider로 한 turn/tool request를 판별한다. probe용 scoped fixture는 production agent 정의를 만들지 않는다.
tools 악성 요청/sentinel, 확정 skill context/digest, typed result wrong ID/range,3회 context 증가, busy/check/end 우선순위,
pause/restart/late/no replay 및 직접 user 실행/종료 후처리 origin을 각각 최소 사례로 시험한다.
full chat 저장/heuristic scraping/skill-only safety/별도 OMP 실행을 기존 agent라고 자동 인정하지 않는다.
08은 SPEC의 동결 preflight/선정 상한을 probe-local로 판별하며09 제품 adapter를 선행 요구하지 않는다.
03/09와 local envelope/budget 의미가 같다는 계약 시험과 배포 후 재판정은09의 별도 gate다.
stdout/PTY를 drain하며 bounded watchdog·owned process group cleanup과 candidate/version/requirements evidence를 남긴다.
공통 runtime harness를 import할 수 있지만 변경하지 않는다. probe-local envelope는 SPEC의 동결 schema를 사용하므로 DVL-03 구현을 prerequisite로 삼지 않는다.
tools/validate_feature_gate.py도 제공한다. PLAN의 Q01 8개 scope와 full p5 requirements identity에 묶어
check/log hash·원 candidate 및 current projection/version·runtime 수준·unknown 없음을 검증한다. 아직 pending인 후속46개 전체를
early gate에 요구하지 않으며 arbitrary subset/pass flag를 거부한다. Missing/stale/fixture-only/failed negative fixture를 포함한다.

## 수락·검증 계약

수락 ID: LG-AC-05, LG-AC-06, LG-AC-07, LG-AC-18, LG-AC-19, LG-AC-22.
Q01-route/tools/skill/result/context/priority/recovery/origin만 owner. 전체 pass가 실제 제품 skill 배포 완료는 아니다.
Gate supplier/required evidence level 및 체크 ID는 PLAN의 gate_items/required_checks를 따른다.
전체 trace는 SPEC §11이며 local 부분과 뒤 combined coverage를 바꾸거나 삭제하지 않는다.

검증: 실제 OMP+scripted provider, positive+intentional negative item, proof of preventive tool guard, wrong/late/duplicate ranges, source/authority binding, transcript delta, cleanup; timeout 원인/probe validity를 별도 기록.

후속 implement는 coherent candidate의 독립 test_designer 평가/시험과 frozen reviewer 검토를 포함한다.
다른 ticket 또는 실제 OMP 없이 관측 불가능한 항목을 fixture/문구 시험만으로 pass하지 않는다.
Required item unknown이면 실패 원인/한계와 evidence를 남기고 affected gate를 닫는다.

## 구현 경계·안전

Safety/provenance 위반은 failed. 관측 부족/원인불명 timeout은 inconclusive. 대안 일회성/TS completion 채택 필요는 blocked_adoption. 어느 경우도 DVL-09/10 확대 및 DVL-14 activation나 actual raw 삭제를 시작하지 않는다.
기존 dirty/new 사용자 파일과 legacy log/DB/core 승인 증거를 보존한다.
사용자/실제 Workbench·DB·socket·tmux/herdr를 대상으로 시험하거나 정리하지 않는다.
새 제품 agent 정의/상주 OMP/독립 Python 모델, Phase2, commit·게시·배포는 범위 밖이다.
권한이 필요한 대안은 Root에 사실/대안/영향을 전달하고 임의 확장하지 않는다.

## 작업·인계

추천 worker: worker_senior; 근거: SPEC.md §8.1; BRIEF Q-01/bridge.ts safety boundaries.
추천은 실제 dispatch pin이 아니며 Root가 당시 code/host/권한을 재확인한다.
물리 작업 공간은 PLAN의 policy를 따르고 parallel writer는 별도 검증된 workspace/service namespace가 필요하다.
write_scope_hint는 계획 추정이며 dispatch 때 baseline·실제 경로를 검증한다. 이 Markdown에 별도 graph/status를 만들지 않는다.
중앙 PLAN/state/승인은 Root만 변경한다. Writer는 candidate delta/검사 evidence/남은 unknown을 반환한다.
현재 미검증: 8개 runtime item 모두 현재 pending; direct-origin API도 미검증.

## p2 리뷰 보강

SPEC §8.1의 원 full-candidate check와 current projection 재대조를 별도로 기록한다. Q01-validator의 음성 fixture·독립 test_designer/reviewer·validator digest freeze를 probe 판정 전에 완료한다. validator 변경은 관련 판정을 무효화한다. tools/q01_recheck.py를 제공하며 Root는08 통합 직후 통합 후보에서8개를 재실행한다.09/10/14 진입 시 projection 변경/불명은8개 재실행,final은 전체 exact candidate 재실행이다. 공유 session/turn/lock이면 입력 상한 실행 중 사용자/due60s/end 최대 차단 시간·실모델 미검증을 보고하고 사용자 채택 전 Q01-priority는 blocked_adoption이다. 대안 채택 뒤에도 채택 route8개 전체 재판정한다.

## p3 무효화·재판정 보완 (N2)

Validator freeze 전에 SPEC §8.1의 backend/ipc/contracts/omp_bridge/pyproject directory와 import closure 정책을 고정한다.
Delivery lock/mailbox 실행 관측·pause·command origin 파일 변경, 관련 새 파일·조건부 import·allowlist 변경을
음성 fixture로 검증한다. 영향 불명은 invalidated다. Product-adapter 모드의 포트를 제공하고09 통합 이후
--adapter product로 실제 backend/adapter 8개를 재실행한다. Probe-local만 다시 돌린 결과는 제품 근거가 아니다.
Output8KiB와 실제 enforce output token ceiling/동일량 예약을 작은 runtime probe에서 확인한다.

## p4 요청별 guard와 판정 독립성 (P1·P8)

Q01-tools는 공개 API의 실제 enforcement를 request/session/turn epoch/policy/one-use nonce에 결속해 확인한다.
등록 ack나 모델 자기보고로 효력을 대신하지 않는다. Hook 누락·등록되나 미작동·API 변형·stale ack·check/send
교체의 wrapper 음성은 전송0·즉시보호다. 실제 OMP 정상효력 관측도 필요하며 추가 모델 canary는 만들지 않는다.
확인/장벽이 불가능한 route는 blocked_adoption으로 제한/대안을 보고한다. DV-D03 안전 예외를 임의 만들지 않는다.
validator/projection/recheck 두 모드·product 포트/판정 음성 fixture를 모두 runtime 판정 전에 독립 검토·동결한다.
09는 adapter만 주입한다. 판정 tool 변경은 Root의08 follow-up으로 독립 재동결 후8개를 새로 판정한다.

## p5 운영 감시와 단일 정책 (R1·R3)

최소 probe에 receipt 정상 뒤 비허용 tool event가 나오는 음성 대조를 포함한다. Await/read-back·예방 차단과
request별 실제 response/event 감시를 함께 확인하며 결과거부/즉시보호·상위turn 오인0·추가모델call0을 검증한다.
src/workbench/contracts/summary_compat_policy_v1.json의 비실행 canonical core bytes를 제공하고 validator/
recheck와 함께 독립 검토·version/SHA-256 동결한다. 개발 probe/harness overlay는 별도이며 core를 약화하지 않는다.
제품의 실수집기는14에서 이 동일 파일을 읽는다.08의 freeze는 제품 policy digest 대조의 원 근거이며
제품이 개발 tools/기록을 실행 시 import/read하게 만들지 않는다. 경로 변경은 Root08 follow-up 재동결 대상이다.
