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
출처를 확인한다. modelRoles는 시작 기본값일 뿐 /model·/fast 뒤 실제 선택의 증거가 아니다. Observer/credential-free verify의 네트워크0·auth store open0·설정쓰기0도 확인한다. Scripted metadata는 실제 session 관측을 대신하지 않는다. 정보를 얻지 못하면
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
