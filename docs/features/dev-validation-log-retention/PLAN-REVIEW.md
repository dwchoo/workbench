# 계획 리뷰와 보강 — s5 / p5

상태: 운영 원칙 개별 합의 및 문서 반영 완료. 정확한 새 bundle 승인·구현 권한 없음.
원본: 승인 BRIEF r0.8·SPEC s4/PLAN p4 및 Claude `ticket-bundle-s4-p4-final-review-04`. 현재 BRIEF r0.9는 승인 기준+추가 개별 합의다.
이번 변경 전 bytes는 `.workflow/dev-validation-log-retention/runs/plan-p5-s5-20261009/before-checkpoint.json`에 보존한다. 승인 BRIEF r0.8 원문은 이전 p3 run의 source/BRIEF-r0.8.md를 그대로 참조한다.
기존 p1~p4 manifest/구조 검증/리뷰 요청·제출 기록은 역사로 유지하며 s5/p5의 현재 pass나 승인으로 재사용하지 않는다.

## s4/p4 마지막 리뷰 R1~R6 처리

리뷰 판정은 조건부 진행(승인 아님),새 사용자 결정 없음이었다. 사용자의 이번 “그렇게 해”로
R1~R3의 작은 기술 보강과 R4~R5 착수 검증·R6 새 수신 기록을 반영한다. 정확한 s5/p5 승인은 별도다.

| 항목 | 처리 | 소유·관측 |
|---|---|---|
| R1 | Guard설정 await/read-back·실행중trusted toolCall/tool_use/비허용실행 감시. 위반결과거부·즉시보호,확인실행compatibility_failed/시도·불명별도. 등록/오류무시/사후감시만으로안전채택금지 | 08 Q01-tools 최소음성,09 adapter,14 PRODUCT-evidence,13 final. 정상receipt뒤위반/상위turn오인0/추가call0 |
| R2 | Scripted plumbing/실모델계약unverified 분리. 명시verify뒤새입력정규요청 recovery_attempt,첫계약거부1회즉시재보호·재발새episode알림1회. Restart/oldlate reset0/no replay | 10 G-LG-18 local 포트,14 runtime 실제알림/admission,13통합. 유료시험/모델pin변경없음 |
| R3 | 08소유 비실행제품JSON canonicalcorebytes/version/digest 하나. 개발overlay별도,제품도동일파일읽기.14/13은08freeze와대조·closure변경판정동일성/누락거부 | 08 Q01-validator freeze,14 PRODUCT-evidence,13 DV-registry. 제품개발tools의존없음 |
| R4 | 착수 체크를명세/09·14포트에연결: 공유현재session 또는채택격리생성context해석을전송전read-back/role상속출처기록 | 09 SUMMARY-budget fixture,14 MODEL-metadata runtime. API가용성미검증;모델call로보충안함 |
| R5 | 10backendtest hint를명시목록으로변경. Rootdispatch actualallowed에08frozenfiles없음+통합전후freeze digest대조 | Root/10. Helper가exclusions를강제한다고주장하지않음 |
| R6 | p4prepared request·publication status는당시기록으로보존. review-dispatch의working관측유지,final회신은p5새receipt/현재state에연결. SPEC의휘발성외부리뷰상태를운영기록참조로대체 | Root운영기록. 새Claude의뢰/승인/구현없음 |

이번 변경은 계획·ticket·운영기록뿐이다. 14 ticket·46 gate·28 AC 및dependency graph/증거수준은 유지한다.
R4~R5는 착수 때 실제관측/범위감사를 요구하며 지금 완료로처리하지 않는다. Guard/API/modelbudget 등은
미검증이다. Luna max/8192 우려도 관측예고일뿐 설정변경이나실패확정의근거가아니다.

## s3/p3 재리뷰 P1~P9 반영

사용자의 “그렇게 해”는 아래 기술 보강·AGENTS 포인터·리뷰 수신 기록의 변경 권한이다.
제품 결정/AC는 유지한다. BRIEF r0.9 인계 포인터만 갱신했고 전체 r0.9/s4/p4 승인·구현은 아직 없다.

| 항목 | s4/p4 보완 | 소유·검증 |
|---|---|---|
| P1 | 매 요청의 실제 enforcement GuardReceipt·session/turn/policy/nonce·check/send 장벽;등록/자기보고만으로 불충분. 확인없으면 전송0·즉시보호. 실제API가용성 미검증 | 08 Q01-tools,09 adapter,14 PRODUCT-evidence,13 final. Hook미작동/stale ack/경합 음성. 불가route는관측후대안채택 |
| P2 | OMP bundled agent/tool/prompt는OMP identity/버전예외;Workbench-owned assets는제품. 안정host OS계열/arch/Pythonminor;volatile patch는기록만 | 14 제품 policy fixture/runtime |
| P3 | 전체 runtime identity는provenance/최초exact,운영재검증은08동결관련closure. 무관UI계속실행·불명invalidated. 개발exact gate엄격유지 | 08 policy freeze,14 운영,13 final |
| P4 | 현재session thinking/reasoning 포함·예약·tier출처와예산관측;사용자pin유지 | 09 budget fixture,14 MODEL-metadata,RM 별도. 필요변경은Q-S3-06후속 |
| P5 | 연속3회 executed+contract-rejected 영속streak/보호·즉시safetyfault와구분·일시오류제외·복구/no replay | 10 local주입포트,14 실제admission/알림,13 cross-ticket. 원인불명≠OMP확정 |
| P6 | 현재session model epoch read-only;observer/credential-free verify 네트워크0·auth store open0·설정쓰기0. offline catalog없음 예상inconclusive | 09 포트fixture,14 실제관측·Q-S3-05,13 registry |
| P7 | visible JSON8KiB/모델별token예산+reasoning으로실제 ceiling 산출·전량예약,8192는상한. byte cap독립·여유/초과/usage기록 | 09 SUMMARY-budget,08 context,14 metadata,RM |
| P8 | 08이validator/projection/recheck두모드포트·fixtures 선행동결.09에서tools/q01_recheck.py·test_summary_probe.py쓰기제거 | 08 독립test/review freeze. 필요변경Root08 follow-up 재동결·결과무효화 |
| P9 | p3미요청/실행중기록을역사로보존하고새수신record;AGENTS의r0.8승인label을baseline+합의문구로정정 | Root 운영record와단일AGENTS포인터. 사용자가이번반영권한부여 |

구조 gate 46개·ticket 14개·dependency graph는 유지한다. Runtime feasibility는 전부 pending이며 이번에 시험/
probe/모델 호출·Claude 재의뢰·agent 생성은 하지 않는다. 구조 검증은 기능 pass나 승인으로 읽지 않는다.

## 이전 s1/p1 리뷰의 s2/p2 처리와 현재 연결

| 항목 | s2/p2 보강 | 상태/소유 |
|---|---|---|
| H1 | full candidate 개발 check와 Q01 projection 재대조를 구분.08 통합 직후/adapter 변경 뒤 재실행,consumer 진입 시 invalidation 검사,final 전체 exact candidate. 제품 별도 record/validator/생성 경로 | 08·09·14·13 기술 보강. 제품이 개발 tools/tests/manifest를 읽지 않음 |
| M1 | 재검증 개발 명령08·최종 precise 등록13·checkout 제품 verify14 | LG-D13 합의;OMP version-only 기본실행 예외는DV-D03 |
| M2 | 13 write scope에 tools/dev_validate.py·tests/dev_validation, harness exclusive; DV-registry gate | 기술 보강 |
| M3 | 12는07/11만 소비하는 비활성 조회/reader;14가10/12 뒤 activation | 기존 ID01~13 유지,14 추가 |
| M4 | 보관 종결/요약 trigger 분리,종결 사유별 권한표 및이벤트중복/late/restart | LG-D14로 수집만·새호출없음 합의 |
| M5 | 요약 실행 중 user/due60s/end 주입,session/turn/lock 공유·최대 관측 차단 측정 | 공유 경로는 측정/실모델 한계 보고 및 Q-S2-03 채택 전 blocked_adoption |
| M6 | 1MiB 수집과32KiB/모델 기반 token/context 전송 budget 분리;선정/누락/usage·미확인 차단 | 09 SUMMARY-budget;실모델은 RM-input-budget 미착수 |
| M7 | 같은08 내 validator/schema·음성 fixtures 독립 시험/리뷰·bytes freeze가 runtime 판정 선행 | Q01-validator gate,변경 무효화 |
| L1 | harness shared/exclusive·log-contract 소비/수정·important-log:schema claims 보강;계약 version/digest 변경시 소비자 재검증 | PLAN canonical |
| L2 | eviction 임박 추가모델호출0·bounded buffer/cursor 음성 assertion | 10 local/14·13 live |
| L3 | core FOLLOWUPS 쓰기 scope 제거,feature FOLLOWUPS에서 core목록 링크+추가RM 계획 | 역사적 core bundle 미수정 |
| L4 | probe-local/03/배포 adapter golden/음성 계약 비교,adapter 변경 뒤8개 재판정 | 09 Q01-contract |
| L5 | 운영 metadata/proof/journal 별도 사용량·추정/unknown 표시,정리 정책 확장 없음 | 06/07 status |
| L6 | archive checkpoint·권한·WAL/SHM·immutable 조건 및 조회파일 불변 시험 | 07/13 |

## s2/p2 재리뷰 N1~N7 반영

| 항목 | s3/p3 반영 | 소유·남은 증거 |
|---|---|---|
| N1 | 현재 제품은 repo checkout 실행형. runtime identity=src/workbench/**·omp_bridge/**·pyproject.toml. tools/tests/docs/.workflow 없는 checkout 복사본에서 실행. packaging 변경 제외 | 14 PRODUCT-evidence; 실제 독립 실행 미검증 |
| N2 | backend/ipc/contracts/omp_bridge/pyproject directory + 실제 import closure projection. lock·mailbox 실행판별·pause·origin·관련 새 파일/불명 무효화. validator와 정책을08에서 선행 동결.09 이후 product-adapter 모드 8개 재판정 | 08 Q01-validator,09 Q01-contract,후속 Root 재대조. 동결/관측 아직 미실행 |
| N3 | MODEL-metadata runtime gate 추가. 실제 modelRoles/선택agent context·tokenizer/estimator·현재context/output ceiling 읽기 전용 확인,모델호출/설정쓰기/credential노출0. 필수unknown은 inconclusive/preflight 차단 | 14 관측/제품record,13 precise등록. fallback은Q-S3-05 조건부 후속 결정 |
| N4 | 사용자 DV-D03은 원 추천과 다름: OMP update-only 기본실행·미검증버전구분,매번차단/반복verify요구/자동verify없음. 확인된문제/제품경계변경때수동verify·신규source보호·사건당알림1회. 일회성검증OMP는운영요약route아님 | 14 제품 운영,10 주입포트,13 통합. 최초검증/activation 유지 |
| N5 | result JSON 전체8KiB,출력은 실제enforce token ceiling(초기8192이하) 별도 제한. preflight는그ceiling전체예약·불명차단. 64KiB결과/2048예약 불일치 제거 | 08 Q01-context,09 SUMMARY-budget; 실제API enforce/실모델적합성 미검증 |
| N6 | p1~p4 requirements는 superseded history이며 원 bytes/hash를 보존한다 | 현재 validator/manifest 입력은 requirements-r0.9-p5.json(46) |
| N7 | 새 bundle manifest에 published-manifest의 경로+실제sha를 기록. candidate와bundle identity를 구분하고 이전p2 manifest는 수정하지 않음 | Root 운영 기록. 파일hash/candidate는 도구관측으로 확인 |

## 사용자 결정과 권한

- 승인된 기준: r0.8의28개 AC. r0.8 원문/hash/승인 기록은 보존한다.
- 추가 개별 합의: 1A=LG-D13,2A=LG-D14,OMP 업데이트 후 계속사용=DV-D03. “반영해줘.”는 문서 수정 권한이다.
- 정확한 r0.9/s5/p5 bundle 전체 승인·제품 구현·유료모델/OMP probe·raw 삭제 활성화·commit/게시 권한은 없음.
- 측정 후 결정: 공유 route일 때 Q-S2-03,metadata 불능 때 Q-S3-05,thinking/tier 변경 필요 때 Q-S3-06,요청별guard 확인 불가route의대안. 지금 새사용자선택을임의확정하지 않는다.
- local provider/일회성검증OMP는 새agent·상주프로세스가 아닌 명시verify 수단이다. 일회성운영요약route의별도채택경계는 유지.
- 미검증: Q01 8개,실제modelmetadata·outputenforce,directorigin,verify격리,herdr/pyte,2분/10분,실모델budget·공유차단시간.
- 계획 구조 validator는 AC/ID/DAG/gate supplier 연결만 확인한다. 제품 기능/runtime 통과의 근거가 아니다.

현재14 ticket·46 gate,초기03+08 추천·01/08 harness 충돌·최대2 verified writer 정책은 유지한다.
기존Claude 회신은 사용자에게 전달된 읽기전용 결과로 수신 기록만 보강한다. 이번 반영 요청으로 새 리뷰를 발송하거나
agent를 생성하지 않는다. 다음은 정확한 새 bundle 검토/승인이며 조건부 관측은 승인된 구현 범위 안에서만 수행한다.
