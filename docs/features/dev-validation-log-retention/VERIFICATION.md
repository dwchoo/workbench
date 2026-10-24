# 최종 통합 검증

상태: 구현 후보 `7e67fe2`에서 plain·tmux·Herdr의 Bash/sh 여섯 조합과 SEL1~3을 검증했다. 변경하지 않은 제품·실행 입력의 실제 Backend 요약·저장·조회와 Q01 근거를 재사용하고, 28개 AC·46개 required gate의 근거를 대조했다. 단일 전체 실행의 성공과 범위별 근거 재사용을 구분하며, 최종 gate 판정은 별도 검증 기록을 따른다.
이 문서는 이전 부분 결과·C2 hold·실패를 변경하지 않는다.

기준은 [DVL-13](tickets/DVL-13.md), [SPEC §11](SPEC.md#11-수락-조건-추적과-시험-결정),
[PLAN의 gate_items](PLAN.json)이다. 실행법은 [DEVELOPMENT](DEVELOPMENT.md)를 따른다.

## 최신 결과 (2026-10-16)

사용자가 채택한 LG-D30~33에 따라 다섯 안전 조건을 검증하고, 변경과 무관한 전체 검사를 반복하지 않았다.
[항목별 근거](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/required46-final-audit01.json)와
[독립 범위 대조](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/final-coverage-assessment01.json)를 함께 확인한다.
미실행 testcase와 operating model 의미의 unknown은 통과로 바꾸지 않는다.

| 범위 | 실제 관측과 근거 |
|---|---|
| 메뉴 검사 정정 | 시작 안내문의 `/hotkeys`를 메뉴로 잘못 인식한 test-only 결함이었다. 실제 메뉴 행으로 열림·닫힘을 판정하고, draft 부재만으로 통과시키던 조건을 제거했다. 독립 11개 테스트와 fresh review 뒤 `7e67fe2`로 커밋했다. 제품 입력·대기 시간·나머지 assertion은 유지했다. |
| 외부 terminal·shell | [plain/sh](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/c2-tip-native01-outcome.json)와 [나머지 조합](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/c2-matrix03-outcome.json)에서 C1~C7 및 SEL1~3이 통과했다. plain의 C6는 기존 계약대로 해당 없음이다. |
| Native 비용 비교 | 같은 후보·도구·환경의 tmux Bash/sh 두 항목은 순차 285.659초, 병렬 155.990초였다. 비교 identity·수집 수·assertion·정리 근거가 일치했다. 이 국소 비교를 전체 precise 성능으로 일반화하지 않는다. 원 Native02의 3055.069초는 600초 목표를 초과한 기록으로 유지한다. |
| 실제 통합과 재사용 | [Native02 원본](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/final-precise02/report.json)은 114개 수집, 24항목 중 23개 성공·C2 fixture 1개 실패로 보존한다. 실제 61.1초 점검·67.3초 detach, 314.706초 요약, Backend 24개 확인과 전용 raw 582,480,635B는 변경 영향 밖의 유효 근거다. 이를 새 단일 24항목 성공으로 재표시하지 않는다. |
| Q01·제품 근거 | [현재 입력 대조](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/current-runtime-applicability03.json)에서 제품 137개와 개발 7개 entry 및 설치 binary가 일치했다. Q01 원본 8개 판별과 hashed log를 보존한다. metadata getter 성공과 context·output enforce·reasoning 의미의 unknown을 구분한다. |
| 실제 모델 시험 | [이미 승인한 작은 실제 요약](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261015/real-summary01/actual-trial-outcome.json)은 Worker.default로 입력 → 요약 → DB 저장 → readonly 조회를 22.96초에 확인했다. 인증 파일은 내용을 표시하지 않고 임시 복사본만 사용·정리했으며 시험을 반복하지 않았다. |
| 정리 | 이번 matrix에서 기록한 PID/startTicks 185건과 namespace 10개가 남아 있지 않았고 임시 HOME·TMPDIR을 제거했다. 기존 Native02의 정리 근거도 별도로 보존한다. 과거 두 임시 경로 삭제 사건의 영향 unknown을 이번 성공으로 지우지 않는다. |

요약 표현·중요도·일부 누락의 품질 편차는 허용한다. provider의 output/reasoning 의미와 프로세스 내부 전체 I/O 귀속은 미검증 한계로 남긴다.
관련 행동이 별도 근거로 확인된 quoted-root launcher testcase와 과거 특정 batch/hash에 결속된 collector·measurement testcase는 미실행 상태로 유지한다.
과거 C2 실패 원인을 새 메뉴 검사 정정으로 소급해 확정하지 않는다. C2 hold와 실제 사용자 raw 순환 삭제 OFF는 그대로다.
(2026-10-24 후속: 실제 사용자 raw rotation은 [BRIEF LG-D34](BRIEF.md#lg-d34--raw-rotation-활성화-결정-2026-10-24)로 활성화 결정됨, 실행 대기다. 아직 어떤 사용자 data dir에서도 실행하지 않았다.)

## 이전 검증 (2026-10-15)

이하의 현재·남은 항목 표현은 당시 관측 시점의 기록이다. 최신 범위 대조는 위 결과를 따른다.

[원본 실행](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/fresh-feature4-05/report.json)과
[범위별 결과](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/fresh-feature4-outcome05.json)는
같은 입력에서 834.082초 동안 실행한 feature 4개 항목의 성공이다. 이전 core 20개를 합쳐 새 후보의 전체 통과로 표시하지 않는다.

| 범위 | 관측 결과와 한계 |
|---|---|
| 실제 Backend·Task·command | 24개 확인 성공. DB-only 도구, host 고정 writer, 귀속, 저장·조회, pause·late 결과·no-replay, 저장 장애와 복구를 확인했다. 실제 요약 간격은 314.627초였다. |
| 전용 raw·보관 DB | 582,480,635B의 전용 raw와 기본 eviction, legacy 불변, DB 회전·조회·삭제 경계를 확인했다. 사용자 raw 활성화는 하지 않았다. |
| Q01 | 별도 Backend origin과 기존 SDK controls를 판별해 8개 모두 통과했다. Scripted 실행 근거이며 실모델 의미의 전체 증명은 아니다. |
| metadata·제품 record | 실제 configured getter와 제품 record consumer 동작을 확인했다. Metadata 자체는 context·output enforce·reasoning 의미가 `inconclusive`이고, 내부 I/O 귀속 caveat도 유지한다. |
| 정리 | 이번 실행의 Backend·소유 process·namespace와 임시 HOME을 정리했다. 이전 정리 사건을 이 결과로 정당화하지 않는다. |

### 후속 검증과 현재 적용 범위

[현재 fast 원본](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/current-fast01/report.json)은
`fae0d04`에서 실제 등록된 3개 항목을 32.076초 동안 실행한 결과다. Harness 26개·launcher/skill filter 64개·설치된 OMP smoke 1개가 모두 통과했고 skip은 0이었다.
실제 plain Bash의 세 pane 입력·한글/paste·resize·pause 신규 호출 차단·shutdown을 확인했다. 입력과 도구 identity는 실행 전후 같았고,
[정리 확인](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/current-fast01-result.json)에서 기록된 소유 PID/startTicks·namespace의 부재와 Root 임시 HOME 삭제를 확인했다.
이는 fast 범위의 근거이며 precise·전체 core·46개 gate 통과가 아니다.

이와 별도로 현재 제품 입력에서 [A 106개](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/required46-local-a01-result.json)와
[B 143개](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/required46-local-b01-result.json)의 관련 fixture가 통과했다.
[C 원본](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/required46-local-c01-result.json)은 167개 중 158개 통과·2개 실패·7개 오류로 보존한다.
실패 원인은 긴 임시 socket 경로 7건, 기존 Backend fake의 raw-port lock 누락 1건, terminal source를 빠뜨린 기대값 1건이었다.
짧은 전용 TMPDIR과 테스트 2개 파일의 최소 정정 뒤 [관련 16개](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/required46-C-correction01-unit.json)가 통과했다.
이를 C 167개 전체의 단일 성공으로 바꾸지 않는다. Terminal source는 core source의 ID·범위·Task/run/revision과 일치하고 중복이 없는지도 확인했다.

별도의 [registry fixture 42개](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/required46-registry-alignment01-unit.json)는
실행 가능한 feature 4개를 포함한 항목 선택·집계와 비용 비교 거부 조건을 검증했다. Synthetic fixture이므로 실제 Native 비용 비교 근거는 아니다.
테스트 정정 4개 파일은 독립 테스트·fresh review·문법·diff·gitleaks 검증 뒤 `30ff1d8`·`fae0d04`로 커밋했다.

[현재 입력 대조](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/current-runtime-applicability02.json)에서
원래 Q01 projection 144개 entry와 제품 policy·설치 OMP binary가 일치했다. Native 입력 manifest의 차이는 위 테스트 4개 파일뿐이며 제품과 실행한 Native fixture 입력은 그대로다.
따라서 기존 실제 feature/Q01 결과의 해당 범위는 재사용하고, 모델 호출·300초 요약·500MB 시험은 반복하지 않았다.
[46개 항목별 감사](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/required46-applicability-audit03.json)의 `scoped_verified`는 해당 범위의 관측 근거라는 뜻이며 전체 최종 후보 gate 통과를 새로 발급한 표시가 아니다.

남은 필수 근거는 현재 최종 후보의 precise/core matrix와 실제 60초 점검·65초 detach, 동일 범위 Native baseline/after 비용 비교 및 최종 통합 판정이다.
과거 core 20개 중 C2 실패와 보류를 유지하며, 실패 셀을 빼서 전체 성공이나 비용 비교 성공을 만들지 않는다.
Metadata getter wrapper의 성공과 descriptor의 context·output enforce·reasoning 의미 `inconclusive`도 구분한다. LG-D30/32에 따른 이 unknown은 단독 차단 사유가 아니다.

관련 독립 검증은 [국소 단위 기록](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/final-fixture-alignment-unit01.json)을 따른다.
옛 fixture와 퇴역한 비용 기대를 현재 계약에 맞췄고 제품 guard·caps는 바꾸지 않았다. TypeScript 43개는 한 실행에서 통과했다.
Python은 98개 완료 후 600초 상한에 걸렸고, 같은 bytes의 나머지 14개만 별도 실행해 통과했다. 이를 단일 전체 실행의 성공으로 합치지 않는다.
Fresh review는 중대한 문제 없음이었고, 문법·diff·gitleaks 검사를 통과했다.

검사 정리 중 소유 생성 근거 없이 두 `/tmp` 경로를 삭제한 [범위 사건](../../../.workflow/dev-validation-log-retention/runs/dvl13-20261015/final-integration01/final-fixture-alignment-test01.json)이 있었다.
다른 동시 실행을 포함한 영향은 불명이며, 사용자에게 알리고 추가 탐색·삭제·추정 복원을 중단했다.
이후 14개 검사는 시작 전 전용 HOME·TMPDIR과 생성 reference를 고정하고, 소유 process 종료와 inode를 확인한 뒤 정리했다.

이전 `fresh-feature4-04`의 TimeoutError 원인은 여전히 불명이다. 이번 성공을 원인 규명으로 표시하지 않는다.
C2 hold와 실제 사용자 raw OFF를 유지한다. 기존 core의 C2 실패, 동일 범위 비용 비교와 최종 46개 근거의 적용 범위는 별도로 남아 있다.
이미 성공한 [작은 실제 요약 시험](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261015/real-summary01/actual-trial-outcome.json)은 재실행하지 않았다.

## precise 실행 항목

기존 fast 3개와 precise core 17개를 유지하며 다음 네 항목을 실제 실행 wrapper로 연결한다.

| 항목 | 필요한 실제 근거 |
|---|---|
| `q01_product_adapter_recheck` | frozen product-adapter recheck, 현재 입력·projection·8개 판별과 누락/실패 |
| `new_storage_summary_raw_product_gates` | 실제 Backend/Task/command 입력, real-clock 300초 summary, DB 보관·조회·정리·장애, owned activation/eviction |
| `model_metadata` | prompt 없는 실제 configured Manager getter와 Worker.default registry readback |
| `product_full_dvl14` | runtime-only copy의 실제 제품 verify record·hashed log·status·관련 경계 판정 |

wrapper의 수집 수나 exit 0만으로 전체 기능을 통과시키지 않는다. 기존 60초 점검·65초 detach는
core 항목에서, 새 300초 간격과 owned raw/DB 교차 동작은 feature runtime에서 실제로 관측해야 한다.
Fixture supplier는 fixture 수준으로만 기록하며 runtime이 필요한 gate를 대신하지 않는다.

## 이전 관측과 수정 배경

최소 credential-free 판별에서 기존 Sandbox와 실제 Backend의 두 OMP bridge가 ready에 도달했다.
하지만 `summary_runtime`의 실제 응답은 `deferred`, `sent:false`, `reason:model_metadata_unavailable`였다.
추가 `openai-codex` private model override를 제거한 비교 실행에서도 같은 응답을 관측했다.
이후 원래 product entry에 추가한 전용 read-only diagnostic extension은 session 시작과 3초 뒤에
동일한 session 등록, 동일한 registry, Worker.default 후보 1개를 관측했다. 두 시점 모두 부모의
`thinkingLevel`은 `undefined`였고, 뒤이은 readback도 같은 deferred 응답이었다.
이는 현재 metadata observer의 필수 string 검사와 맞지 않는 실제 필드 관측이며,
SDK 일반 결함이나 nonreasoning 모델의 지원 계약을 확정한 결과는 아니다.
Diagnostic generator의 앞선 syntax 실패도 별도로 보존하며 제품 판정 근거로 쓰지 않는다.
모든 판별은 prompt/summary 요청과 인증 접근 없이 소유 자원을 정리했다.
이 상태를 metadata·summary·INT-runtime 통과로 올리지 않는다.

전용 fixture의 부모·child 모델 선언에 `reasoning:true`를 지정한 다음 판별에서는 실제 parent thinking이
`high`였고, shipped Worker.default selector를 유지한 child descriptor가 전용 loopback의
`openai-completions`를 가리켰다. 이는 controlled descriptor plumbing 근거이며 operating MODEL pass가 아니다.
명시적 prompt/summary 제출은 0이고 provider counter는 `unknown:5`였다. Startup 내부 호출의 귀속은 미확인이므로
HTTP 0으로 표시하지 않는다. Nonreasoning 부모의 `undefined` thinking은 현재 metadata readback이 보류되는
알려진 한계로 남긴다.

다음 정적 연결 확인에서 기본 Backend runtime supplier가 `metadata`, `instructions`, `expected_peer`만 반환하지만
실제 SummaryScheduler가 `runtime["writer"]`를 요구하는 불일치를 발견했다.
Root가 승인한 추가 범위에서 Backend는 host의 lexical Python·package import root·data root를 공급하고,
scheduler는 count/예약/sending 전에 exact 3키 absolute/NUL-free 형태를 검사하도록 수정했다.
전용 fixture와 독립 시험에서 기본 공급 경로와 전송 전 거부를 확인하고 이 host writer 변경을 커밋했다. 이후 실제 Native 통합의 첫 결과는 아래와 같다.

새 runtime harness와 네 registry wrapper는 현재 후보에 포함돼 있다. Runtime-only 복사본의 실제 제품 verify와
명시 activation, 기본 Backend의 실제 TaskFlow·TerminalService·real-clock SummaryScheduler,
조회·DB 정리·기본 raw eviction을 연결한다. Q01 Composite는 기존 SDK raw 관측을 별도로 보존하고,
actual Backend origin trace의 사실별 판정을 공급한다. 이 조합의 실제 성공은 아직 미관측이다.
원래 core 20개 실행 항목과 precise exact test ID 19개는 유지한다.

위 연결의 후속 독립 테스트·리뷰와 실제 feature 실행 결과는 이 문서의 최신 결과를 따른다.
원래 실패·unknown·첫 시도와 이후 수정 근거를 함께 보존한다. 필수 runtime 근거가 없으면 완료하지 않는다.

## 운영과 시험의 구분

Owned scripted activation/eviction은 전용 데이터만 대상으로 한다. 실제 사용자 raw는 활성화 결정됨, 실행 대기다(2026-10-24, [BRIEF LG-D34](BRIEF.md#lg-d34--raw-rotation-활성화-결정-2026-10-24)). 실행 전까지는 기존 정책이며 C2 hold는 유지한다.
작은 실제 입력 → 요약 → DB → 조회 시험의 기존 성공은 별도 실모델 근거로 유지하고 재실행하지 않는다.
실모델 품질·provider output enforce·reasoning 의미·GUI fidelity·VM/재부팅의 미검증 범위를 확대 해석하지 않는다.
LG-D30/31/32의 승인된 한계는 unknown 또는 caveat로 남기며, unobserved 0이나 fixture pass로 대체하지 않는다.

## 첫 실제 feature 통합과 정리 보완

첫 registered feature 실행(`native-feature01`)은 133.324초에 `inconclusive`로 끝났다. 실제 Backend의 준비·metadata readback·기본 host writer·legacy source는 확인했지만, raw 활성화에 앞선 제품 검증에서 Q01-priority의 cleanup 확인이 미완료였다. 나머지 제품 판별 7개는 pass였으며 300초 요약과 529,527,850B raw 시험은 시작하지 않았다. 원본 report와 실패 결과를 보존한다.

바깥 runner는 종료 시 adopted zombie 3개를 실제 수거했고 남은 소유 프로세스와 임시 namespace는 없었다. 작은 stdlib PID 재현에서 수거 전 zombie가 남는 메커니즘을 확인했으나, 최초 실패의 inner 상세 근거가 보존되지 않아 같은 원인이라고 확정하지 않는다. 최소 보완은 runner가 실행 중에도 이미 귀속한 종료 프로세스를 수거하고, priority 검증기가 기존 5초 정리 기한 안에서 실제 소멸을 재확인하며, 실패한 제품 record와 그 log도 정리 전에 원본으로 보존하는 것이다. 이 보완의 독립 테스트·리뷰 및 후속 실제 실행 결과는 같은 run의 기록으로 판정한다. 전체 gate의 성공을 국소 근거에서 유추하지 않는다.

두 번째 실행(`native-feature02`)은 944.186초에 `failed`로 끝났다. 실제 Backend의 Task·command 귀속, DB-only 도구, 저장·조회, pause·중복 전송 방지와 316.637초 주기를 포함한 18개 확인은 성공했다. 전용 raw의 실제 관측량은 582,480,635B였고 기본 cap의 eviction 및 기존 legacy 불변을 확인했다. Runtime-only 제품 원본 8개 판별과 별도 Backend origin 사실 9개도 성공했지만, 뒤이은 보관 DB 시험에서 AssertionError가 발생했으므로 이 실행 전체를 통과로 표시하지 않는다. 소유 프로세스와 임시 namespace는 정리됐다. 이후 세 feature consumer는 이 실행에서 시작하지 않았다.

작은 실제 Catalog에서는 원래 closed subject와 새 pressure subject 모두 첫 append에 회전했다. 따라서 최초의 closed-prune 원인 가설은 철회하며 원 Native 원인은 미확정으로 남긴다. 최소 fixture 수정은 같은 Backend의 보관 DB 시험을 대량 raw 출력 앞으로 옮기고, 임시 target을 finally에서 복원하는 것이다. 별도 owned pressure subject는 실행 권한·작업 귀속 근거로 사용하지 않는다. Append 상태·prune·회전 사실과 제한된 AssertionError 원인을 남긴다. 작은 helper의 6개 확인은 성공했지만 최종 통합 실행을 대체하지 않는다. 121일 시간 주입에서 삭제가 없었던 관측은 만료 삭제 증거로 올리지 않는다.
