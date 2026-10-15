# 최종 통합 검증

상태: `814fb71`에서 관련 feature 통합 4개 항목과 Q01 scripted 판별 8개가 통과했다. 실제 Backend의 요약·저장·조회·장애 처리와 소유 자원 정리를 확인했다. 같은 최종 후보의 28개 AC·46개 required gate 전체는 아직 통과 판정하지 않았다.
이 문서는 이전 부분 결과·C2 hold·실패를 변경하지 않는다.

기준은 [DVL-13](tickets/DVL-13.md), [SPEC §11](SPEC.md#11-수락-조건-추적과-시험-결정),
[PLAN의 gate_items](PLAN.json)이다. 실행법은 [DEVELOPMENT](DEVELOPMENT.md)를 따른다.

## 최신 결과 (2026-10-15)

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

Owned scripted activation/eviction은 전용 데이터만 대상으로 한다. 실제 사용자 raw는 OFF이며 C2 hold는 유지한다.
작은 실제 입력 → 요약 → DB → 조회 시험의 기존 성공은 별도 실모델 근거로 유지하고 재실행하지 않는다.
실모델 품질·provider output enforce·reasoning 의미·GUI fidelity·VM/재부팅의 미검증 범위를 확대 해석하지 않는다.
LG-D30/31/32의 승인된 한계는 unknown 또는 caveat로 남기며, unobserved 0이나 fixture pass로 대체하지 않는다.

## 첫 실제 feature 통합과 정리 보완

첫 registered feature 실행(`native-feature01`)은 133.324초에 `inconclusive`로 끝났다. 실제 Backend의 준비·metadata readback·기본 host writer·legacy source는 확인했지만, raw 활성화에 앞선 제품 검증에서 Q01-priority의 cleanup 확인이 미완료였다. 나머지 제품 판별 7개는 pass였으며 300초 요약과 529,527,850B raw 시험은 시작하지 않았다. 원본 report와 실패 결과를 보존한다.

바깥 runner는 종료 시 adopted zombie 3개를 실제 수거했고 남은 소유 프로세스와 임시 namespace는 없었다. 작은 stdlib PID 재현에서 수거 전 zombie가 남는 메커니즘을 확인했으나, 최초 실패의 inner 상세 근거가 보존되지 않아 같은 원인이라고 확정하지 않는다. 최소 보완은 runner가 실행 중에도 이미 귀속한 종료 프로세스를 수거하고, priority 검증기가 기존 5초 정리 기한 안에서 실제 소멸을 재확인하며, 실패한 제품 record와 그 log도 정리 전에 원본으로 보존하는 것이다. 이 보완의 독립 테스트·리뷰 및 후속 실제 실행 결과는 같은 run의 기록으로 판정한다. 전체 gate의 성공을 국소 근거에서 유추하지 않는다.

두 번째 실행(`native-feature02`)은 944.186초에 `failed`로 끝났다. 실제 Backend의 Task·command 귀속, DB-only 도구, 저장·조회, pause·중복 전송 방지와 316.637초 주기를 포함한 18개 확인은 성공했다. 전용 raw의 실제 관측량은 582,480,635B였고 기본 cap의 eviction 및 기존 legacy 불변을 확인했다. Runtime-only 제품 원본 8개 판별과 별도 Backend origin 사실 9개도 성공했지만, 뒤이은 보관 DB 시험에서 AssertionError가 발생했으므로 이 실행 전체를 통과로 표시하지 않는다. 소유 프로세스와 임시 namespace는 정리됐다. 이후 세 feature consumer는 이 실행에서 시작하지 않았다.

작은 실제 Catalog에서는 원래 closed subject와 새 pressure subject 모두 첫 append에 회전했다. 따라서 최초의 closed-prune 원인 가설은 철회하며 원 Native 원인은 미확정으로 남긴다. 최소 fixture 수정은 같은 Backend의 보관 DB 시험을 대량 raw 출력 앞으로 옮기고, 임시 target을 finally에서 복원하는 것이다. 별도 owned pressure subject는 실행 권한·작업 귀속 근거로 사용하지 않는다. Append 상태·prune·회전 사실과 제한된 AssertionError 원인을 남긴다. 작은 helper의 6개 확인은 성공했지만 최종 통합 실행을 대체하지 않는다. 121일 시간 주입에서 삭제가 없었던 관측은 만료 삭제 증거로 올리지 않는다.
