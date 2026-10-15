# DVL-13 — 최종 통합·독립 검증과 개발 인계

계획 revision p5 / SPEC s5 / BRIEF r0.9 (r0.8 승인 + 운영 원칙 개별 합의).
상태·의존성·resources·scope의 canonical source는 [PLAN.json](../PLAN.json)의 DVL-13이다.
이 ticket은 계획안이며 제품 구현/시험을 시작할 권한은 아직 없다.
기본 계약: [SPEC](../SPEC.md), [BRIEF](../BRIEF.md); source decision은 BRIEF 추적표를 따른다.

## p4 최종 교차 검증 추가

SPEC §8.1~8.3의 요청별guard 확인·check/send교체 전송0,OMPbundled/무관UI/host변동의운영예외와개발exact엄격성,
실제session offline metadata·reasoning budget·연속3계약거부보호를precise registry에등록하고46개전체gate를
최종candidate에서재실행한다.사용자pin변경/유료모델시험/실제사용자rawactivation은포함하지않는다.

## p5 최종 연결 (R1~R5)

DV-registry에서 제품 canonical core policy version/digest와08freeze/14record의일치·closure 변경판정 동일성을
검사하고 불일치/누락은 invalidated로 집계한다. Guard 실행중위반/결과거부·복구시도 첫재거부보호/재발episode
알림·route별metadata read-back도최종runtime에서연결한다.08frozenfiles변경은재동결/독립검토 없으면통과하지 않는다.

## 제공할 행동

같은 최종 후보에서 개발 runner·proof·중요 로그·raw 정책 전체 수락 및 회귀/범위 audit를 연결한다.

Root가 통합 후보를 freeze하고 독립 test_designer와 fresh reviewer를 배치한다. 새 테스트/회귀는 승인 행동으로 도출한다.
precise registry에 Q01 8개 재판정·제품 verify·새 중요DB/요약/raw 장애를 실제 등록한다. tools/dev_validate.py와 tests/dev_validation 및 harness exclusive claim은 PLAN에 포함한다.
real installed OMP+scripted provider에서 fast/precise와 full feature scenario, 모든46개 required gate를 같은 최종 candidate/requirements identity로 다시 실행한다. 다른 candidate의 과거 check를 relabel하지 않는다.
legacy default와 isolated activation, marker eviction proof, initial/latest importance preservation, model lag/pause/restart/lost no replay,
Task/directcommand/expiry,120일/cap/rollover/multiDB-warning/explicit-delete, unsafe storage/late failure 및 readonly query를 교차 확인한다.
첫 실패·수정·delta review를 보존하고 required unknown/missing runtime evidence면 전체 done_verified를 거부한다.
core 역사 기록·FOLLOWUPS를 수정하지 않는다. feature FOLLOWUPS.md에서 기존 FL/RM 링크와 RM-input-budget 계획/미착수 상태를 기록한다.
 actual user raw 삭제 활성화/paid model/VM/commit/publish/deploy 없이 검증된 범위와 pending을 인계한다.

## 수락·검증 계약

수락 ID: DV-AC-01, DV-AC-02, DV-AC-03, DV-AC-04, DV-AC-05, LG-AC-01, LG-AC-02, LG-AC-03, LG-AC-04, LG-AC-05, LG-AC-06, LG-AC-07, LG-AC-08, LG-AC-09, LG-AC-10, LG-AC-11, LG-AC-12, LG-AC-13, LG-AC-14, LG-AC-15, LG-AC-16, LG-AC-17, LG-AC-18, LG-AC-19, LG-AC-20, LG-AC-21, LG-AC-22, LG-AC-23.
INT-runtime의 전체 AC 및 DV-registry owner. Status flag나 validator exit0만으로 기능 통과를 선언하지 않는다.
Gate supplier/required evidence level 및 체크 ID는 PLAN의 gate_items/required_checks를 따른다.
전체 trace는 SPEC §11이며 local 부분과 뒤 combined coverage를 바꾸거나 삭제하지 않는다.

검증: 전후 frozen candidate, 모든 PLAN gate와 공급ticket evidence current, 독립 테스트/리뷰/필요한 correction, full regression nonempty collection, fast/precise timings, owned resource residue/scope audit.

후속 implement는 coherent candidate의 독립 test_designer 평가/시험과 frozen reviewer 검토를 포함한다.
다른 ticket 또는 실제 OMP 없이 관측 불가능한 항목을 fixture/문구 시험만으로 pass하지 않는다.
Required item unknown이면 실패 원인/한계와 evidence를 남기고 affected gate를 닫는다.

## 구현 경계·안전

Q01 실패/불명/미채택 또는 required 제품/runtime 항목 미검증이면 전체 완료하지 않는다. 부분 진행 결과와 blocker를 구분한다.
기존 dirty/new 사용자 파일과 legacy log/DB/core 승인 증거를 보존한다.
사용자/실제 Workbench·DB·socket·tmux/herdr를 대상으로 시험하거나 정리하지 않는다.
새 제품 agent 정의/상주 OMP/독립 Python 모델, Phase2, commit·게시·배포는 범위 밖이다.
권한이 필요한 대안은 Root에 사실/대안/영향을 전달하고 임의 확장하지 않는다.

## 작업·인계

추천 worker: worker_senior; 근거: SPEC.md §11/12; 전체28 AC와 INT-runtime.
추천은 실제 dispatch pin이 아니며 Root가 당시 code/host/권한을 재확인한다.
물리 작업 공간은 PLAN의 policy를 따르고 parallel writer는 별도 검증된 workspace/service namespace가 필요하다.
write_scope_hint는 계획 추정이며 dispatch 때 baseline·실제 경로를 검증한다. 이 Markdown에 별도 graph/status를 만들지 않는다.
중앙 PLAN/state/승인은 Root만 변경한다. Writer는 candidate delta/검사 evidence/남은 unknown을 반환한다.
현재 미검증: 유료 실모델/VM/실제 GUI fidelity/미설치 outer는 별도 pending TODO.

## p3 반복 검증 등록

MODEL-metadata와 product-adapter Q01 재판정을 precise registry에 등록한다. 실제 모델 호출 없는 metadata 관측과
scripted provider의 Q01 결과를 구분하고 필수unknown을 성공으로 집계하지 않는다. Version-only 실행 유지,
확인된 문제 보호·수동verify 복구·취소/철회 신규호출0·사건중복알림0을 통합에서 확인한다.
실제 모델 품질/비용 또는 공유경로 차단시간을 이 scripted 근거로 승인했다고 주장하지 않는다.

## LG-D31 최종 검증 실행 원칙 (2026-10-15)

[BRIEF LG-D31](../BRIEF.md#lg-d31--실용-기능-완성과-다섯-안전-조건-2026-10-15)에 따라 다섯 안전 조건과 사용 가능한 입력 → 요약 → DB 저장 → 조회 흐름을 중심으로 최종 통합한다. 내용의 완벽함·반복 품질 평가·자동 교정이나 가능한 모든 내부 고장 증명은 필수 검증으로 추가하지 않는다. 실제 결함과 무관한 견고화·성능 개선은 후속으로 남긴다.

기존 필수 46개 gate·28개 AC·증거 수준·의존 graph와 최종 후보 검증 계약은 유지한다. 영향 범위가 맞는 유효 근거는 재사용하고 관련 변경은 묶어 독립 테스트·리뷰한다. 최종 통합에 필요한 전체 검사는 수행하되 무관한 전체 검사를 각 국소 변경마다 반복하지 않는다. 미검증·실패·C2 hold를 pass로 재표시하지 않고, 실제 raw 순환 삭제는 활성화하지 않는다. LG-D30에서 후속 승인한 작은 실제 요약 시험은 관련 개발 검증 뒤 수행하며 기존 scripted 결과와 구분한다.
