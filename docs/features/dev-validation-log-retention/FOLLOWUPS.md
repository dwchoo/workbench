# 개발용 검증·로그 보관 후속 시험 계획

상태: 계획만. 이번 문서 보강에서 시험·모델 호출을 실행하지 않았다.
기존 간헐 실패 FL-01~04 및 실제 모델 RM-01~03 계획은 [core FOLLOWUPS](../core-workbench/FOLLOWUPS.md)를 참조한다.
기존 목록·완료/미확인 이력을 덮어쓰지 않고 비용·승인 경계를 유지한다.

## RM-input-budget — 실모델 입력·경합 확신 시험

- 선행: Q01 채택 경로와 adapter/token/context preflight의 scripted 통과,독립 시험/리뷰,별도 유료 모델 시험 승인/예산.
- 입력: 작은 일반 출력,다국어/paste·긴오류trace·고속출력,긴 시작/명세·결정/보고,최신누적요약 및 raw 누락.
  개인정보 없는 합성 입력만 사용하고 full 사용자 대화/실제 로그를 수집하지 않는다.
- 관측: 모델/version/context·token 추정과 actual input/output usage·cost·latency,기존 context 여유,
  선정/누락 range와 사실/해석/unknown,부작용 차단,주기 coalesce·종료·late/no replay.
- 경합: 입력 budget 상한의 요약 turn 중 user input/60초 점검/종료를 넣고 session/turn/lock 점유와
  최대 관측 차단 시간을 기록한다. scripted 수치를 실제 모델 상한으로 주장하지 않는다.
- 성공 기준: context 초과/조용한 token 잘림 없음,출력 token ceiling 전량 예약·실제 enforce와8KiB JSON cap 일치,요청별 usage 또는 미제공 이유 기록,누락/출처 정확,
  기존 승인·pause/no replay·도구 차단 유지. 공유경로 채택은 실제 측정/한계와 사용자 결정을 별도 기록한다.
- 실패/불명: budget을 기술 범위 안에서 축소 후 동일입력/모델 조건으로 한정 재시험하고 첫실패 보존.
  제품 행동·호출형태·예산 변경이 필요하면 사용자 결정을 요청한다. 반복 until-green·무제한 모델 호출 금지.
- 1MiB는 수집 한도이며 송신 목표가 아니다.32KiB/8192/10%·결과8KiB와 enforce output token ceiling의 실모델 적합성은 이 시험 전 미검증이다.

## 간헐 실패 연계

fast/precise 보고서의 최초실패·attempt chain·candidate/OMP 전후 버전·provider/환경/제품 분류를 기존 FL 조사에 전달한다.
동일 입력의 재현·bounded 반복·host 차이를 비교하되,재실행 성공이 최초 실패를 없애거나 feature 완료를 뜻하지 않는다.
VM/재부팅·실모델 검증은 별도 승인 작업이며 현재 계획 gate의 scripted/runtime 결과로 완료 처리하지 않는다.

## 실제 model metadata 관측과 후속 결정

DVL-14 MODEL-metadata는 실모델 호출 없이 실행 중 session의 현재 모델/thinking/tier/context/tokenizer·현재context·output ceiling과 reasoning 포함/예산
출처를 확인한다. modelRoles는 시작 기본값일 뿐 /model·/fast 뒤 실제 선택의 증거가 아니다. Observer의 네트워크0·auth store open0·설정쓰기0도 확인한다. Credential-free verify는 LG-D27/28의 준비·scripted 검증 구간 경계를 따른다. Scripted metadata는 실제 session 관측을 대신하지 않는다. 정보를 얻지 못하면
unknown preflight 차단을 유지하고 무엇이 없는지/가능한 공개조회와 보수대안의 근거를 보고한다.
Q-S3-05의 고정fallback 또는 새 설정은 그 관측 뒤 사용자 결정 전 도입하지 않는다. 수치의 안전성은 RM-input-budget에 연결한다.

## OMP 업데이트 운영과 실패 연계

OMP version-only는 기본적으로 기존 경로 사용을 유지하고 실제 검증 버전과 현재 버전을 분리한다.
매 업데이트 검증/반복verify 안내를 자동 실행하지 않는다. 확인된 호환성 문제나 관련 제품 경계 변경이면
사건·버전·첫실패·누락·영향범위를 기록하고 명시 수동verify/필요수정과 FL 계획에 연결한다.
일시적 provider/연결·요금 오류를 OMP 비호환으로 확정하지 않으며 재검증/수정 결과로 첫실패를 지우지 않는다.

## Guard·reasoning 후속 관측 (P1·P4·P7)

- Q01-tools에서 실제 요청에 묶인 enforcement 확인·유효 epoch/lease와 도구 복구를 먼저 판별한다.
  Hook 등록만 성공하거나 확인 불능이면 전송0으로 보호한다. 이때 DV-D03 예외를 지킬 수 없는 route의
  제한/대안만 사용자에게 보고하며 새 agent·별도 요약 route·안전 생략을 자동 채택하지 않는다.
- RM-input-budget는 visible JSON·reasoning·total output usage, 실제 thinking/tier 및 ceiling 의미·8KiB cap
  초과/미완결·latency/cost를 따로 측정한다. 최초 거부와 연속3회 계약 거부 보호·no replay도 확인한다.
  8192 상한을 목표 출력으로 쓰거나 bytes/token 고정 비율을 가정하지 않는다.
- Q-S3-06은 관측 뒤 요약 요청에만 thinking/tier를 낮출지의 조건부 사용자 결정이다. 지금 model/effort/tier
  pin을 바꾸지 않는다. 유료 시험·예산은 별도이며 fake provider로 실제 reasoning 소진을 통과 처리하지 않는다.

## Scripted 복구와 실모델 계약의 구분 (R2)

Scripted verify는 plumbing만 판별한다. 실모델 계약 거부 뒤의 명시 verify는 복구 시도로 기록하며 정상 정규
요청의 새입력만 처리하고 첫계약거부에 재보호한다. 실모델시험 승인으로 해석하지 않는다. 별도 승인된 RM에서
reasoning 소진/잘림·schema거부·최신stored결과/재발episode를 관측하고 처음 실패를 보존한다.
Claude의 luna max/8192 한계 예고는 아직 측정되지 않은 가능성이다. 현재 pin을 낮추거나 실패를 확정하지 않는다.

## DVL14-F01 — 검증용 OMP parent의 준비 경계 이력과 최소 연결

- 상태: LG-D28의 명시 요구 변경으로 공식 offline 준비 기능 대기 조건을 해소했다. OS 격리안은 비채택이다. Private parent의 registry를 재사용하는 검증 전용 SDK 연결과 고정 writer 저장은 개발 실행에서 확인했다. 제품 verifier의 완전한 observer/child I/O 근거와 운영 MODEL 근거는 미완료이며, 제품의 `verify --scripted`는 `unavailable`을 유지한다.
- 현재 요구: 소유한 임시 HOME·설정·DB에서 OMP의 정상 시작·모델 목록 갱신 통신을 허용한다. 사용자 인증정보·실제 로그·유료 모델을 사용하지 않고, 테스트 모델 요청은 소유 localhost scripted provider로만 처리한다. 준비 I/O와 observer/child 관측은 분리한다.
- 관측: 고정 OMP 18.8.6의 [main.ts](https://github.com/can1357/oh-my-pi/blob/v18.8.6/packages/coding-agent/src/main.ts)는 CLI parent 준비에서 `refreshInBackground()`를 호출한다(2254·2482행). [CLI parser](https://github.com/can1357/oh-my-pi/blob/v18.8.6/packages/coding-agent/src/cli/args.ts)와 확인한 설정에는 discovery-off/전략 선택 option이 없다. SDK child에 private registry를 넘기는 공개 경로는 있지만 parent의 호출을 막지 못한다. 이는 source 판별이며 새 OMP 실행은 0회다.
- 비채택: 완전성이 확인되지 않은 provider blacklist, cache 내용 작성, 인증이 없으면 통신하지 않을 것이라는 기대, 부팅 타이밍·fetch 변조로 우회하지 않는다. 별도 Bun/SDK host route도 자동 채택하지 않는다.
- 이력: 위 source 판별은 OMP 자체에서 외부 연결 시도0를 만드는 공개 경로의 부재다. OS 또는 사용자 경계 변경까지 불가능하다는 결론은 철회한다. LG-D27 당시 결과와 source 근거는 보존하고 새 기준의 성공으로 재표시하지 않는다.
- owner: Root가 현재 설치 binary와 공개 SDK의 대응을 확인하고 기존 private parent의 registry를 사용하는 최소 검증 연결을 판별한다. 새로운 offline 옵션·OS 도구·blacklist의 확보를 재개 선행조건으로 두지 않는다.
- 후속 관측(2026-10-13): 설치 OMP 18.8.6의 검증 전용 SDK entry에서 POST 2건 모두 `db_summary_save`만 노출했고, 고정 writer가 중요 로그 entry와 receipt를 각 1건 저장했다. Child 종료와 parent 상태·registry·설정 불변을 확인했다. 검증 child만 registry 재사용과 isolated power off를 적용하며 운영 생성 옵션은 유지한다. [실행 근거](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261013/normal-startup01/smoke-private01/result.json)는 개발 capability이며 제품 8개 pass·운영 MODEL·전체 DVL-14 완료를 뜻하지 않는다. 미지원 관측 getter와 임시 DB 권한 오류로 실패한 이전 실행은 보존했다.
- 재개 검증: private parent 준비 → native registry 주입 child → 소유 scripted provider의 최소 요청을 구간별로 관측한다. 사용자 credential/실제 로그 접근·유료 요청·child의 새 auth store open/설정 쓰기 또는 격리·귀속 불명은 통과시키지 않는다. 정상 시작·목록 갱신 통신을 observer/child가 유발한 요청으로 혼동하지 않으며 귀속 불명은 그대로 남긴다. Observer I/O0, 제한 tool·저장·no replay와 process/socket 정리를 독립 검증한다.
- 영향: G-LG-01·PRODUCT-evidence와 DVL-14 전체 완료, 이를 소비하는 DVL-13 최종46/INT-runtime은 미완료다. 실제 운영 MODEL-metadata unknown과 C2 hold는 별도 남은 조건이다. 이 기록은 Q01 통과·실제 raw 활성화·수치 또는 운영 route 변경이 아니다.
