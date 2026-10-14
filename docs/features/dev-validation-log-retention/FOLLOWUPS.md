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

- 상태: LG-D28의 명시 요구 변경으로 공식 offline 준비 기능 대기 조건을 해소했다. OS 격리안은 비채택이다. Private parent의 registry를 재사용하는 검증 전용 SDK 연결과 고정 writer 저장을 제품의 명시 `verify --scripted`에서도 확인했다. 관측하지 못한 observer/child I/O와 운영 MODEL은 unknown이며 전체 결과는 `inconclusive`다. 실행 capability가 없으면 `unavailable`로 끝난다.
- 현재 요구: 소유한 임시 HOME·설정·DB에서 OMP의 정상 시작·모델 목록 갱신 통신을 허용한다. 사용자 인증정보·실제 로그·유료 모델을 사용하지 않고, 테스트 모델 요청은 소유 localhost scripted provider로만 처리한다. 준비 I/O와 observer/child 관측은 분리한다.
- 관측: 고정 OMP 18.8.6의 [main.ts](https://github.com/can1357/oh-my-pi/blob/v18.8.6/packages/coding-agent/src/main.ts)는 CLI parent 준비에서 `refreshInBackground()`를 호출한다(2254·2482행). [CLI parser](https://github.com/can1357/oh-my-pi/blob/v18.8.6/packages/coding-agent/src/cli/args.ts)와 확인한 설정에는 discovery-off/전략 선택 option이 없다. SDK child에 private registry를 넘기는 공개 경로는 있지만 parent의 호출을 막지 못한다. 이는 source 판별이며 새 OMP 실행은 0회다.
- 비채택: 완전성이 확인되지 않은 provider blacklist, cache 내용 작성, 인증이 없으면 통신하지 않을 것이라는 기대, 부팅 타이밍·fetch 변조로 우회하지 않는다. 별도 Bun/SDK host route도 자동 채택하지 않는다.
- 이력: 위 source 판별은 OMP 자체에서 외부 연결 시도0를 만드는 공개 경로의 부재다. OS 또는 사용자 경계 변경까지 불가능하다는 결론은 철회한다. LG-D27 당시 결과와 source 근거는 보존하고 새 기준의 성공으로 재표시하지 않는다.
- owner: Root가 현재 설치 binary와 공개 SDK의 대응을 확인하고 기존 private parent의 registry를 사용하는 최소 검증 연결을 판별한다. 새로운 offline 옵션·OS 도구·blacklist의 확보를 재개 선행조건으로 두지 않는다.
- 후속 관측(2026-10-13): 설치 OMP 18.8.6의 검증 전용 SDK entry에서 POST 2건 모두 `db_summary_save`만 노출했고, 고정 writer가 중요 로그 entry와 receipt를 각 1건 저장했다. Child 종료와 parent 상태·registry·설정 불변을 확인했다. 검증 child만 registry 재사용과 isolated power off를 적용하며 운영 생성 옵션은 유지한다. [실행 근거](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261013/normal-startup01/smoke-private01/result.json)는 개발 capability이며 제품 8개 pass·운영 MODEL·전체 DVL-14 완료를 뜻하지 않는다. 미지원 관측 getter와 임시 DB 권한 오류로 실패한 이전 실행은 보존했다.
- 제품 연결 관측(2026-10-13): tools/tests/docs/.workflow가 없는 runtime-only 복사본에서 사용자 CLI로 새 소유 임시 환경을 실행했다. 수정본의 [새 실행 결과](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261013/product-partial01/root-owned02/result.json)는 localhost POST 2회·전용 tool 1회·중요 로그 1건·stored receipt 1건이며 요청/range/digest 결속, parent 상태·registry·설정 불변, child 종료와 임시 root 정리를 확인했다. 기존 agent·skill·SDK 및 제품 고정 writer를 사용한다. 미실행 priority/recovery는 `not_run`, I/O 귀속과 운영 MODEL은 unknown이며 pass record·보호 해제·raw 활성화를 만들지 않는다. 이전 실행 근거도 보존했다.
- 실패 이력: 최초 DB 관측 비교의 `schemaVersion` 누락과 소유 cleanup 판정은 수정·재검증했고 실패 기록을 보존했다. 긴 fixture data-dir의 Unix socket 경로 제한은 제품 실행 전에 거부됐다. 새 소유 0700 디렉터리의 짧은 절대경로만 사용해 재검증했으며 CLI 경로 검사나 제품 기준은 완화하지 않았다. 깊은 사용자 data-dir에도 같은 경로 길이 제한이 적용된다.
- 후속 리뷰에서 느린 HTTP 입력이 정리를 지연시키는 경로와 완료 JSON의 부분 쓰기 경합을 찾아 수정했다. 소유 서버가 받은 socket에 shutdown을 보내 handler 종료를 확인하고, 성공·오류 JSON은 별도 private 임시 파일에 쓴 뒤 rename으로 공개한다. 수정본의 새 제품 실행에서 남은 HTTP handler/socket은 0개였다. 새 격리 도구·상주 서비스·통신 정책은 추가하지 않았다.
- 재개 검증: private parent 준비 → native registry 주입 child → 소유 scripted provider의 최소 요청을 구간별로 관측한다. 사용자 credential/실제 로그 접근·유료 요청·child의 새 auth store open/설정 쓰기 또는 격리·귀속 불명은 통과시키지 않는다. 정상 시작·목록 갱신 통신을 observer/child가 유발한 요청으로 혼동하지 않으며 귀속 불명은 그대로 남긴다. Observer I/O0, 제한 tool·저장·no replay와 process/socket 정리를 독립 검증한다.
- 영향: G-LG-01·PRODUCT-evidence와 DVL-14 전체 완료, 이를 소비하는 DVL-13 최종46/INT-runtime은 미완료다. 실제 운영 MODEL-metadata unknown과 C2 hold는 별도 남은 조건이다. 이 기록은 Q01 통과·실제 raw 활성화·수치 또는 운영 route 변경이 아니다.

## DVL-14 소유 검증의 늦은 결과·중복 거부 (2026-10-13)

- 제품의 명시 `verify --scripted`에서 소유한 임시 journal의 요청을 첫 응답 전에 `unknown`으로 기록했다. 같은 입력·범위의 새 요청 예약은 거부되고 행도 추가되지 않았다. 실제 SDK tool과 고정 writer가 낸 늦은 유효 결과는 `stored`가 됐으며, 같은 receipt의 재기록은 거부됐다. [새 제품 실행](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261013/recovery-partial01/root-owned02/result.json)에서 상태 순서는 `prepared → sending → unknown → stored`, catalog·receipt는 각각 1개, duplicate drop은 1개였다.
- 이는 owner가 제어한 상태 전이의 직접 관측이다. 실제 연결 끊김 분류·Backend 재시작·pause의 제품 연결을 시험한 결과는 아니다. 같은 실행의 SDK child 1개·localhost POST 2회와 90초 상한을 유지했다. Parent·registry·설정 불변, child 종료와 소유 자원 정리도 확인했다.
- 첫 실행은 SDK가 붙인 별도 안내 text part와 prompt를 합쳐 JSON으로 읽으려다 실패했다. 그 [실패 기록](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261013/recovery-partial01/root-owned01/result.json)은 보존한다. 기존 prompt 바이트를 공유 순수 함수로 유지하고 SHA·byte 길이가 정확히 일치하는 part 하나만 읽도록 관측 경계를 수정했다. 일치하는 part가 없거나 두 개이면 journal 변경 전에 거부하며, substring·정규화·fallback은 사용하지 않는다. 다른 SDK part는 type·길이·SHA만 기록한다.
- Q01-recovery는 부분 관측으로 `inconclusive`다. 당시 Priority·pause·restart는 미실행이고 observer/child I/O와 운영 MODEL은 unknown이었다. 이전 current8이나 전체 Q01 pass로 합치지 않으며 보호 해제·raw 활성화도 만들지 않는다. DVL-14 전체와 DVL-13 최종46은 계속 미완료다.
- 후속 [제품 실행](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261013/pause-partial01/root-owned01/result.json)에서는 검증용 owner predicate를 false로 호출해 child 생성·prompt 시작·전송 0을 확인한 뒤 고정 helper로 `prepared → not_sent`를 기록했다. Predicate는 1회 호출됐고 해당 요청의 receipt·drop은 0개였다. 같은 metadata·입력·범위의 새 요청은 기존 정상 경로를 통과해 늦은 저장·중복 거부를 유지했다. Child 1개·POST 2회 상한과 소유 자원 정리도 유지했다.
- 이 원인 귀속은 owner predicate 호출과 정상 대조에 근거한다. SDK가 반환한 `model_metadata_unavailable` 사유는 변경하지 않았다. 실제 AutomationController의 pause·pause_store·restart·priority 연결은 미검증이고 I/O·운영 MODEL은 unknown이다. 따라서 이 후속 결과도 부분 관측이며 전체 pass·보호 해제·raw 활성화가 아니다.
- 후속 [제품 재시작 실행](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261013/restart-partial01/root-owned02/result.json)에서는 첫 POST의 요청·prompt 결속을 검증하고 응답 bytes 0인 상태에서 소유 PID·시작 identity를 대조해 OMP를 종료했다. 종료·자원 정리와 저장 0을 확인한 뒤 `prepared → sending → unknown`을 기록했다. 같은 journal을 다시 열어 동일 입력의 새 예약 거부·행 미생성을 확인하고, 같은 private 설정으로 OMP를 다시 시작했다. 같은 subject의 인접한 새 입력은 저장됐고, 기존 pause 보류·늦은 저장·중복 거부도 유지했다. 전체 90초 상한 안에서 OMP 2회·localhost POST 3회였으며 두 process group과 HTTP handler/socket의 잔여물은 0개였다.
- 종료된 OMP 안의 SDK child도 함께 종료되므로 해당 child의 dispose와 OMP 재시작 뒤 늦은 결과는 적용되지 않는 항목이다. 살아 있는 요청의 늦은 저장은 별도 C2 대조로 관측했다. 이는 소유 OMP 프로세스 교체의 검증이며 실제 Backend·scheduler 재시작이나 pause_store·priority 연결을 증명하지 않는다. Observer/child I/O와 운영 MODEL은 unknown으로 유지하고 전체 결과는 `inconclusive`다. 최초 [실행 환경 오류](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261013/restart-partial01/root-owned01/result.json)는 설치 OMP 경로 누락으로 native 실행 전에 끝났으며 보존했다. 첫 phase 산출물은 둘째 phase가 덮어쓰지 않도록 분리했고, 정상 정리 후에는 길이·SHA와 필요한 JSON 사실만 결과에 남긴다.
- 후속 독립 구성 시험(2026-10-14)은 실제 `AutomationController` 생성자·`PauseStore`·`Backend._automation_paused`·`SummaryScheduler`를 연결했다. 정상 pause 파일과 잘못된 `paused` 값의 파일에서 각각 controller와 scheduler를 재생성해 복원된 pause가 runtime·adapter 호출 및 새 요청을 모두 0으로 유지함을 확인했다. 명시적 resume 후에는 새 입력 없이 같은 pending 범위를 한 번 처리해 journal의 `stored`와 receipt를 확인했다. [시험 근거](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/actual-pause01/test01/result.json)는 `actual_core_components_fixture`이며 runtime metadata·adapter·compatibility·priority·dispatch readiness는 controlled seam이다. 제품 코드·기준은 바꾸지 않았고 실제 Backend 구동·bound run/OMP pause·bridge/TUI·priority·운영 MODEL/I/O 및 전체 Q01/DVL-14 완료는 계속 미검증이다.

## DVL-14 혼합 raw 용량 국소 검증 (2026-10-13)

- 독립 시험에서 소유한 임시 저장소에 run/command payload 합계 500,000,000 bytes를 기록했다. 실제 producer·Backend reader·collector를 사용하되 display drain과 policy admission은 fixture다. [실행 결과](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261013/mixed-volume01/green04/result.json)는 268.9초에 통과했다.
- Append 뒤 관측한 최대 raw 파일 합계는 497,421,608 bytes, 파일 수는 5개, 개별 최대는 100,000,000 bytes였다. Command의 recent/since/unseen buffer 최대는 각각 32,768 bytes였다. Eviction gap의 순서·범위·귀속과 영속 기록, retained tail의 bytes/digest, condition proof 및 기존 legacy 파일 불변을 확인했고 임시 저장소를 정리했다.
- 독립 리뷰에서 시험의 cleanup 실패 반환값을 놓칠 수 있는 경로를 찾아 정정했다. 정리 성공 반환과 임시 root 삭제를 단언하며, 본문·닫기·정리가 모두 끝난 뒤에만 passed를 기록한다. 수정본의 [최종 전체 재시험](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261013/mixed-volume01/green05/result.json)은 269.4초에 같은 500,000,000 bytes와 위 검사를 통과했다. 이전 성공 실행에서 정리가 실제 완료된 관측은 보존한다.
- 이전 범위 밖 fixture 출력, 240초 감독 제한 중단, gap 병합을 요구했던 시험 실패는 보존한다. Gap 기대만 반환 순서의 연속 coverage 검사로 바로잡았으며 제품 코드·용량·fsync·수락 기준은 바꾸지 않았다. 내부 600초/감독 660초는 시험 비용 상한이며 성능 수락 기준이 아니다.
- 이 시험에는 scheduler/model dispatch가 없으므로 긴급 추가 호출 수는 미검증이다. 전체 혼합 제품 실행·Q01·운영 MODEL·DVL-14 완료 근거로 확장하지 않는다. 제품 verifier의 I/O 근거와 DVL-13 최종46은 계속 미완료이며 실제 사용자 raw 활성화는 하지 않았다.

## DVL14-F02 — 제품 priority 부분 관측과 내부 daemon 종료 확인 (2026-10-14)

- tools/tests/docs/.workflow 없는 runtime-only 복사본의 [새 조합 실행](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/priority-core01/root-native04/result.json)에서 명시적으로 시작한 OMP 4회와 소유 localhost 모델 POST 9회를 관측했다. 기존 복구 관측을 유지하며, 실제 Core의 periodic QUESTION과 Manager end·Worker check 전달이 요약 child의 응답 보류 중 완료됐고 요약·receipt 각 1건이 저장됐다. Parent 상태·기존 entry view는 불변이었다. 주입 clock의 due 판별이며 실제 60초 대기·운영 모델 지연·전체 Backend/TaskFlow 실행 근거는 아니다.
- OMP 내부 text-predict의 종료 기록 writer가 등록된 process group 종료 이후에도 동작해 임시 root를 다시 만드는 [이전 정리 오판](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/priority-core01/root-native03/late-cleanup-disposition.json)을 보존했다. 수정본은 소유 root의 daemon metadata가 미종료·불명일 때 삭제하지 않고 cleanup false·경로 보존·inconclusive를 반환한다. 새 실행에서도 cleanup 시점의 `ready` 기록을 거부했으며 이를 정리 성공으로 표시하지 않았다.
- [DVL-FOLLOWUP-02](tickets/DVL-FOLLOWUP-02.md)에 종료 확인의 남은 범위를 기록한다. 부분 연결 관측과 종료 불명 거부의 근거이며, 전체 native I/O/process 부재·observer/child I/O·운영 MODEL·Q01/전체 DVL-14 완료를 뜻하지 않는다. C2 hold·no-replay·caps·운영 route와 실제 raw 비활성은 유지한다.
- 후속 수정의 [현재 runtime-only 실행](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/graceful-exit01/root-native01/result.json)은 두 OMP의 공개 `ctx.shutdown()`과 관측한 소유 registry의 `exited`를 공통 5초 종료 상한 안에서 확인했다. 실제 확인은 약 3.52초였고 fallback 없이 두 parent가 RC0으로 종료했다. 요약·receipt 각 1건, 독립 전달·부모 불변, 등록한 group·thread·socket 잔여물 0과 임시 root 삭제를 확인했다. 5초는 검증기 종료 비용의 상한이며 제품 latency·입출력 caps 변경이 아니다. 초기 3초 실험의 실패 기록은 보존했다.
- [독립 시험](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/priority-core01/graceful-exit01/test01/result.json)은 Python 11개·Node 4개가 통과했다. 시간 초과·metadata 읽기 실패·fallback은 늦은 종료 기록으로 지우지 않고 경로를 보존한다. Metadata가 없는 디렉터리와 미관측 writer의 부재는 입증하지 않으며 전체 결과는 계속 `inconclusive`다.

## Worker 중요 로그 조회 도구와 시작 격리 검사 연결 (2026-10-14)

- 실제 Backend 시작에서 양쪽 역할에 등록되는 승인된 조회 도구 `workbench_logs`를 Worker 격리 검사의 허용 목록이 빠뜨려 시작을 거부했다. [최초 실패](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/backend-composition-runtime01/final.json)를 보존하고 허용 목록에 정확히 이 이름 하나만 추가했다. 조회 action과 사용자 전용 변경 권한, 요약 child의 DB-only 경계는 바꾸지 않았다.
- [독립 시험](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/worker-logs-fix01/test01/result.json)에서 Python 17개와 기존 양쪽 역할의 read-only bridge 시험 1개가 통과했다. `bash`·`eval`·임의 `workbench_*`·MCP·browser·미승인 Task agent는 계속 거부되고 Manager 기준은 유지된다. 기존 skill 목록의 정확히 세 개 기대는 추가된 `important-log-summary`와 불일치해 별도 실패로 보존했다. 이 실패를 이번 수정의 성공 결과에 포함하지 않는다.
- [새 실제 Backend 실행](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/backend-composition-runtime02/final.json)에서 기본 격리 검사는 `ok:true`, 누출 0으로 통과했다. 다음 Task 생성은 fixture의 `spec.paths=["."]`가 기존 경로 규칙에 어긋나 거부됐다. Task·요약 child·모델 POST는 모두 0이며, 작업 종료에서 요약 저장까지의 연결은 이 실행으로 입증되지 않았다.
- Backend의 공개 종료 결과와 소유 Python 종료, 알려진 group·thread·socket 잔여물 0 및 임시 root 삭제를 확인했다. 두 TUI의 종료 코드는 143이므로 자연 종료로 표시하지 않는다. 전체 DVL-14·DVL-13 최종46·운영 MODEL/I/O·C2 hold는 남아 있고 실제 raw 활성화는 하지 않았다.

## 실제 Backend 작업 종료·요약 연결과 provider 오류 분류 (2026-10-14)

- [정상 연결 실행](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/backend-composition-runtime06/final.json)에서 실제 Backend·TaskFlow·terminal·기본 SummaryScheduler·bridge·설치 SDK child를 연결했다. Owner가 Core API로 승인된 Task를 시작하고 고정 명령 1개를 실행했으며, Worker 보고와 Task 종료 뒤 중요 로그와 receipt가 각각 1건 저장됐다. 전체 모델 POST는 4회이고 요약 child의 2회에는 `db_summary_save`만 노출됐다. 선택한 6개 입력 중 terminal 17 bytes는 실제 raw 범위·digest와 일치했다. 이는 수정 전 SDK 후보의 기능 연결 근거이며 아래 오류 수정 후보의 전체 정상 실행 근거로 재표시하지 않는다.
- Metadata·compatibility admission·entry의 공급은 private fixture이며 child 생성에는 소유 parent의 registry만 추가 주입했다. 운영 MODEL·실모델 소비량·observer/child I/O를 판별한 결과가 아니다. Parent의 기존 entry view와 model/thinking/tier는 유지됐다. Python 종료와 알려진 group·thread·socket 잔여물 0, 임시 root 삭제를 확인했지만 TUI 종료 코드는 143이므로 자연 종료·미관측 writer 부재를 주장하지 않는다.
- [최초 provider 오류 실행](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/provider-error-runtime01/final.json)에서는 실제 SDK가 assistant `message_end/stopReason:error`를 내고 결과 없이 종료했는데 `rejected/result_unavailable`로 기록돼 계약 streak가 1 증가했다. P5의 일시적 provider 오류 처리와 어긋나므로, 공개 error event가 있고 유효 결과·보호 사유가 없는 경우만 기존 `unknown/summary_outcome_unknown`으로 반환하도록 수정했다. 비허용 도구·binding 위반의 보호 우선순위, 정상 미완결 결과의 계약 거부, 이미 받은 유효 결과의 저장은 유지한다. [독립 시험](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/provider-error-fix01/test01/result.json)은 관련 41개를 통과했다.
- 수정 후 [새 실제 오류 실행](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/provider-error-runtime03/final.json)은 첫 child POST에 HTTP 400을 반환했다. 같은 요청이 `prepared → sending → unknown`과 `summary_outcome_unknown`으로 남았으며 control은 transient·streak 0·protection 없음이었다. Catalog·receipt·owner 재전송은 모두 0이었다. SDK prompt의 직접 resolve/reject 여부는 관측하지 않았고 공개 error event와 영속 결과를 근거로 한다. 종료 시 scheduler snapshot의 `busy:true`는 그대로 보존한다. 이전 실행의 closure 조회 lease-busy는 fixture 관측 실패로 보존했으며, 이번 실행에서는 해당 busy와 closure storage_unknown이 모두 0이었다.
- 위 실행들은 각 후보와 실패 이력을 분리한 부분 관측이다. 전체 결과는 계속 `inconclusive`이며 current8·Q01·전체 DVL-14·DVL-13 최종46의 pass로 합치지 않는다. 운영 MODEL/I/O, 실모델 계약, C2 hold와 실제 raw 비활성은 유지한다. 실패한 fixture의 Task 경로·조회 API·float JSON·PTY newline·lease 관측을 바로잡기 위해 제품 기준이나 입력을 축소하지 않았다.

## 실제 Backend의 종료 Task 요약 pause/resume 연결 (2026-10-14)

- 현재 SDK 수정이 포함된 [새 조합 실행](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/backend-pause-runtime01/final.json)에서 실제 Manager REPORT의 응답을 소유 provider가 잠시 보류한 동안 Task 종료를 확인하고 공개 `Backend.pause()`를 호출했다. 응답을 놓아 Manager가 idle로 돌아온 뒤 기본 scheduler의 완료 cycle은 `deferred/priority_pause_or_compatibility`, `busy:false`였다. 이때 journal 요청·receipt·요약 child POST는 모두 0이며 실제 `PauseStore`에도 paused=true가 저장됐다.
- 공개 `resume(reconciled=True)`의 완료와 paused=false 영속을 확인한 뒤, resume 직전의 미전송 6개 범위와 같은 입력이 최초 요청 하나로 저장됐다. 범위의 원래 순서·중복 수를 보존하고 비교용 복사본으로 multiset 동등을 확인했다. 실제 child POST는 2회이고 전용 도구만 노출됐으며 catalog·receipt는 각각 1건이었다. 전체 POST는 4회, 실제 경과 시간은 29.35초였다. Stock 300초 주기와 제품 caps는 유지됐고, 이 실행은 종료 이벤트로 due가 된 입력을 처리했으므로 300초 만료 검증으로 표시하지 않는다.
- Parent의 기존 entry view·model/thinking/tier와 제품 source는 불변이었다. 두 TUI는 RC0, 시작 격리 검사의 RPC는 RC143으로 종료했으며 알려진 group·thread·socket 잔여물 0과 임시 root 삭제를 확인했다. 소유 Core API·private entry·controlled metadata/admission·registry 주입의 부분 구성 근거다. 실행 중인 bound run의 중단·복구, 운영 MODEL/I/O, 미관측 writer 부재·전체 Q01/DVL-14 및 DVL-13 최종46은 계속 미검증이며 전체 결과는 `inconclusive`다.

## 승인된 skill asset 목록의 기존 시험 정합 (2026-10-14)

- 기존 격리 시험은 Workbench skill 디렉터리에 정확히 세 개만 있어야 한다고 기대했으나, LG-D22의 host 전용 `important-log-summary`가 추가돼 실패했다. 독립 시험의 inventory 이름·주석·정확한 네 개 literal 기대값만 정정했다. 일반 Manager의 `to-worker/workbench-recovery`와 Worker의 `to-manager` 허용 목록은 그대로이며 요약 skill을 일반 역할에 추가하지 않았다.
- [독립 결과](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/skill-inventory-alignment01/test01/result.json)에 원래 한 case의 실패를 보존하고 관련 세 case의 통과를 기록했다. 제품·skill·정책은 불변이다. SPEC의 후보 경로명 `workbench-important-log`와 실제 asset 명칭의 차이는 문서 정합 잔여 항목이며, 이번에 경로나 승인 bytes를 바꾸지 않는다.

## Backend 기본 runtime 공급 연결 (2026-10-14)

- 기존 Backend는 명시적 supplier가 없으면 runtime을 항상 None으로 넘겼다. 현재 Manager 연결의 session/generation에 묶인 읽기 전용 `summary_runtime` 조회로 metadata와 설치 SDK의 bundled task 본문을 공급하도록 연결했다. 명시적 callable은 반환값이 None이어도 우선하며, None이 아닌 비호출 객체는 기존처럼 차단한다. 조회는 scheduler 작업 경로에서만 실행하고 UI/status는 기존 관측 cache를 읽는다.
- [일반 entry.ts의 실제 RPC 실행](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/default-runtime-supplier01/feasibility01/result.json)에서 parent metadata와 820 bytes의 bundled task 본문을 읽었다. 모델 prompt·요약 child·모델 POST는 0이며 context/output/reasoning의 null과 unknown 세 항목을 보존했다. 기존 preflight는 `output_budget_unavailable`로 차단한다. 기본 공급 연결과 운영 예산 의미의 확보는 별개이며, 값이나 pass record를 만들어 채우지 않는다.
- 실행은 3.680초에 끝나 알려진 group·thread·socket 잔여물 0과 private root 삭제를 확인했다. 별도 outer90 supervisor는 실제 설치되지 않았으므로 선언된 전체 상한 보장으로 주장하지 않는다. 실제 RPC 조회 뒤 Backend 기본 callback 연결과 response 검사를 보강했으며, 최종 조합의 실제 Backend 실행은 이 근거에 포함되지 않는다.
- [독립 시험](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/default-runtime-supplier01/test01/result.json)의 새 9개 case가 통과했다. 실제 bridge handler와 observer를 controlled SDK getter에 연결하고 실제 Unix G3 transport·Backend 생성자 및 scheduler에 연결해 현재 peer 거부·snapshot/epoch 갱신·기존 supplier 우선·예산 미확인 시 예약/dispatch 0을 확인했다. Native/model 실행은 없다. 준비 통신·auth/settings I/O 귀속, 운영 MODEL, 제품 verifier/full14·DVL-13 최종46은 계속 미확인이다. 기존 정책 bytes·caps·C2 hold·no-replay·실제 raw 비활성은 유지한다.

## Backend 기본 runtime의 실제 보류 검증 (2026-10-14)

- [새 실행](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/default-backend-negative02/final.json)은 `summary_runtime`을 지정하지 않은 실제 Backend와 일반 shipping `entry.ts`를 사용했다. SDK·metadata wrapper나 별도 runtime supplier는 없으며, compatibility admission만 controlled fixture다. 실제 Manager의 공개 getter와 bundled task 본문을 읽고 context/output/reasoning의 null·unknown을 보존했다. 이는 기본 공급 연결의 실제 Backend 구성 근거이며 운영 예산 의미 확보나 compatibility pass가 아니다.
- 소유 Core API로 Task·짧은 명령·Worker REPORT·Task 종료를 실행했다. REPORT 응답을 잠시 보류한 동안 공개 pause를 호출한 뒤 완료된 scheduler cycle은 `priority_pause_or_compatibility`로 보류됐다. 공개 resume 완료와 `PauseStore`의 paused=false를 확인한 후 다음 완료 cycle은 `output_budget_unavailable`, requestId=null, busy=false였다. 미전송 입력 6개의 identity·revision·range·digest·순서·중복 수가 그대로 유지됐으며 journal 요청·receipt·누적 요약·요약 모델 POST는 모두 0, streak·protection도 불변이었다.
- 이 실행은 21.074초에 끝났고 시작 격리 검사용 RPC 2회와 Manager/Worker TUI 2회, parent 모델 POST 2회를 관측했다. 모델 요청은 소유 localhost scripted provider로만 처리했다. 알려진 process group의 reap 뒤 잔여물·HTTP handler/socket·Python thread는 0이며 임시 root 삭제를 확인했다. Python은 RC0, TUI/RPC는 RC143으로 종료했으므로 자연 종료 RC0으로 표시하지 않는다. 내부180초·정리60초·외부240초 감독이 실제 설치됐고 상한에는 도달하지 않았다.
- [최초 실패](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/default-backend-negative01/result.json)는 보존한다. 정상 producer의 늦은 REPORT까지 pending 전체 동일성으로 거부한 기대값과 아직 실행 중인 관측용 Python 자체를 잔여물로 센 cleanup 판정을 정정한 뒤, 독립 사전 리뷰를 거쳐 위 새 실행을 했다. 새 실행에는 추가 REPORT가 없었으므로 추가 REPORT의 POST·collector bytes·grant 결속을 검사하는 조건부 분기의 runtime coverage는 없다. 최초 실패와 새 실행을 합쳐 전체 통과로 만들지 않는다.
- 운영 MODEL의 output ceiling·reasoning·현재 context 의미는 계속 unknown이며 Q-S3-05에 연결한다. SDK child 생성 counter와 전체 native argv, observer/child I/O, 미관측 writer 부재, 실행 중 bound run의 priority/recovery는 이 실행으로 입증하지 않았다. 전체 결과는 `inconclusive`이고 DVL-14 전체·DVL-13 최종46·C2 hold·실제 raw 비활성 상태를 유지한다.

## DVL-02 외부 terminal·Bash/sh 부분 검증 (2026-10-14)

- LG-D29의 사용자 정정을 반영해 Bash와 sh(dash) 6개 셀·SEL1~3·대표 정책/장애 시나리오를 precise runner에 등록했다. `c9c8b58` 당시 등록은 14 Item·16개 legacy test ID·93개 step 참조이며, 독립 시험과 관련 fixture 57개·일반/보안 리뷰·변경분 secret scan을 거쳐 `c9c8b58`로 통합했다. Q01·신규 저장 정책·운영 MODEL 등 DVL-13의 후속 항목은 여전히 `not_run`으로 집계한다. 전체 legacy 회귀나 최종 precise를 통과한 등록으로 표시하지 않는다.
- [tmux의 실제 Bash/sh 실행](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/bash-matrix01/runtime01/checked.json)은 각각 C1~C7을 통과했다. `/usr/bin/bash`와 `/usr/bin/dash` 실행을 확인했고 두 셀 모두 65.8초 detach 동안 process identity·입력 owner·Task·provider 요청 수를 유지했다. 셀 소요 시간은 143.576초와 138.815초이며 소유 namespace 정리를 확인했다. 기존 tmux/dash C2 간헐 실패의 원인 규명이나 해결 근거는 아니다.
- [plain·Herdr 묶음 실행](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/bash-matrix01/runtime02/check.json)은 Root가 검증 도구의 기본300초 제한을 고려하지 않고 4개 셀을 묶어 중단됐다. plain Bash/sh는 item 수준에서 각각 126.351초·121.654초에 통과했으나 check 전체는 timeout 실패로 보존한다. 별도 process group의 driver가 parent 종료 뒤 잠시 남았고 이후 종료됐다. 정확한 driver identity·group 잔여0을 관측했으며, 복구한 Herdr Bash legacy 보고서는 nonce/control aggregation 결속이 없어 부분 관측으로만 보존한다. 이때 남은 namespace는 삭제하지 않고 운영 기록에 연결했다. 이는 제품 실패로 확정한 결과가 아니다.
- Claude의 좁은 자문 뒤 제품·helper source·시간 assertion을 바꾸지 않고 셀마다 명시300초 제한으로 다시 실행했다. 새 [Herdr Bash](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/bash-matrix01/runtime03-herdr-bash/checked.json)와 [Herdr sh](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/bash-matrix01/runtime04-herdr-sh/checked.json)는 C1~C7과 전용 session 삭제·소유 namespace 정리를 통과했다. 이전 복구 보고서는 새 입력으로 사용하지 않았다. 사용자 기본 Herdr server를 조회하거나 정리한 결과가 아니다.
- 위 결과는 각각의 실행 범위·후보에 묶인 부분 검증이며, 서로 다른 실행을 합쳐 최종 precise·G-DV-03 전체·46개 gate 통과를 만들지 않는다. 대표60초 review·pause/restart/fault runtime, 같은 범위의 전후 비용 비교와 10분 목표는 남아 있다. 기존 C2 hold·운영 MODEL/I/O unknown·DVL-14 및 DVL-13 미완료·실제 raw 비활성을 유지한다.

## DVL-02 정책 실행과 DVL-14 상태줄 회귀 (2026-10-14)

- [P1/P7 실행](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/bash-matrix01/runtime05-review-p1/checked.json)은 변경 전 `c971964` 후보에서 221.520초에 통과했다. 실제 첫 Worker 점검은 60.6초, busy 동안 보류·병합된 점검은 186.3초에 전달됐고 사용량 미확인과 종료·정리를 확인했다. 이후 상태줄 변경의 재검증이나 전체 precise 통과로 확대하지 않는다.
- [P2 최초 실패](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/bash-matrix01/runtime06-pause-p2/checked.json)는 Backend paused=true와 실제 화면의 `자동화: paused`에도 특정 문구를 기다리다 종료·재개 단계에 도달하지 못했다. 실제 정지 상태 표시를 확인하도록 시험만 정렬하고 요청 중 상태는 거부했다. [후속 실행](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/pause-ui-alignment01/runtime07-pause-p2/checked.json)에서는 지시 보류·로그 수집·재개와 no replay를 확인했지만, 긴 요약 호환 안내가 170열 상태줄의 `보고 대기`를 가렸다. 두 실패를 그대로 보존한다.
- DVL-14의 persistent 상태줄에만 간결한 호환 상태를 적용했다. 기본 상세 formatter/getter·CLI의 마지막 검사·사유·수동 verify 안내, 읽기 전용 관측과 기존 boot/store 오류·multiple-DB 경고는 유지한다. 독립 17개와 관련 fixture 104개가 통과했으며, uncropped 문자열에 상세 정보가 모두 있는 검사와 실제 170열에서 작업 상태가 보이는 검사를 구분했다. FL-01 Esc/slash menu·제품 정책·모델 설정은 바꾸지 않았다.
- [수정 후 새 P2 전체 실행](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/status-priority01/runtime08-pause-p2/checked.json)은 119.631초에 다섯 step을 모두 통과했다. 실제 85.8초 일시정지 중 주기 점검 0, 기존 실험·로그 수집 유지, 종료 뒤 0.5초 내 `보고 대기` 표시, 재개 후 중복 지시·실행 0과 run 1회를 확인했다. shutdown verified·소유 namespace 삭제·알려진 잔여물 0을 확인했다. scripted provider의 현재 설치 OMP 근거이며 실모델 적합성 근거는 아니다.
- 위 결과는 부분 검증이다. 전체 outer/precise·같은 범위 전후 비용 비교·10분 목표·DVL-13 최종46은 열려 있다. 기존 C2 hold·operating MODEL과 I/O unknown·DVL-14 전체 미완료·실제 raw 비활성을 유지한다.

## DVL-02 중단·재시작·작업 중 detach 등록 보강 (2026-10-14)

- 기존 P3 Manager 중단, P6 worker/host 재시작, X1 active/takeover/paused detach 시험을 precise 실행 목록에 연결했다. 현재 17 Item·19개 고유 unittest ID이며 기존 live 시험·assertion·실시간 대기는 변경하지 않았다. 전체 legacy 회귀·VM·유료 모델과 후행 제품 gate는 계속 미검증이다.
- [독립 fixture 28개](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/core-inventory01/test-output/result.json)와 관련 fixture 65개가 통과했다. 필수 step 누락·실패·미승인 n/a·cleanup 불명은 통과로 집계하지 않는다. X1의 515초는 기존 320초 실험과 세 번의 65초 detach를 보수적으로 합산한 watchdog 대기 허용량이며 측정된 소요 시간이나 비용 절감 근거가 아니다.
- [새 실제 P6 실행](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/core-inventory01/native-p6/report/result.json)은 57.330초에 다섯 step을 통과했다. 이전 worker 종료·새 worker 연결, Manager/host/Task 유지, unknown survivor 거부, 소유 host 강제 종료 후 새 shell 입력과 shutdown을 확인했다. 수집1·skip0, timeout 없음, 소유 namespace 삭제와 알려진 잔여물0을 관측했다. P3/X1 실제 실행·전체 precise 통과의 근거는 아니다. 선택 관측 `worker_restarted_notice_to_manager`는 false였고 기존 시험이 이를 assert하지 않으므로 Manager notice 전달 성공도 주장하지 않는다.
- 과거 비용 결과는 item key19 목록만 보존되고 원 report/workspace가 없어 과거 exact unittest ID의 byte 동일성이나 전후 비용을 입증할 수 없다. 같은 범위의 새 비교가 필요하며, 이번 등록·부분 실행으로 G-DV-05나 10분 목표를 통과시키지 않는다. C2 hold·operating MODEL/I/O unknown·DVL-14/DVL-13 미완료·실제 raw 비활성을 유지한다.

## DVL-02 실제 P3/X1과 병렬 process 경계 관측 (2026-10-14)

- 현재 `f0d2920` Source 후보에서 [P3](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/core-runtime01/native-p3/checked.json)는 38.923초에 네 step을 통과했다. Manager 스트리밍 중단 확인·실행 중 tool의 unknown 기록·재개 후 중단한 요청의 재전송0과 shutdown을 확인했다. 기존 100초 실험 명령·30초 stream/tool 조건은 변경하지 않았으며 시험은 실험 전체 완료를 기다리는 범위가 아니다.
- [X1](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/core-runtime01/native-x1/checked.json)은 344.465초에 일곱 step을 통과했다. 기존 320초 실험과 active/takeover/paused 각각 65초 이상 detach를 유지했고 실제 세 구간은 모두 67.2초였다. process·입력 owner·Task/전달 request를 유지하고 로그 수집·복귀 표시를 확인했다. active에서 Worker 점검1회, paused에서 점검·모델 호출0회였으며 재개 후 run1회·실험 성공을 확인했다. 두 시험 모두 수집1·skip0·timeout 없음, 소유 namespace 삭제·알려진 잔여물0이다.
- [tmux Bash/sh 동시 pilot의 원 실패](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/core-runtime01/parallel-pilot01/check.json)는 Root 집계기가 step 객체를 문자열과 비교한 오류로 보존한다. 개별 Native 셀은 C1~C7·각65.8초 detach·정리를 모두 통과했고 각각 152.416초와 149.039초였다. 원 wrapper를 통과 결과로 바꾸거나 원본을 덮어쓰지 않았다.
- [읽기 전용 artifact 판정](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/core-runtime01/pilot-adjudication01/result.json)에서 개별 report SHA·수집/step·Namespace/PID 결속·정리를 재확인했다. 두 Native 소요 시간 합301.455초가 같은 wrapper 구간152.678초보다 길어 동시 실행 겹침의 하한148.777초를 확인했다. Namespace·adopted PID 집합은 구분됐다. sentinel 실행 전 SHA는 메모리에만 있어 고정 literal의 재구성 기대값으로 표시하며, 실행 중 cross-sentinel 검사 성공으로 기록하지 않는다. 이는 재실행한 Native가 아닌 기존 관측의 정적 판정이다.
- Claude 자문 뒤 기존 `jobs1` 경로·전체 assertion·실시간 wait·보고 순서를 유지하는 process 분리 구현을 다음 작업으로 뒀다. status 객체 파싱과 sentinel before SHA 영속화는 다음 구현/시험에서 확인하며 그 후 실제 jobs2 측정이 새 검증을 겸한다. 당시 CLI는 jobs1만 지원했다. 기존 비용 원본 부재·전후 비교 미실행·10분 목표 미검증·전체 precise/46/full14 미완료와 C2 hold·operating MODEL/I/O unknown·실제 raw 비활성을 유지한다.

## DVL-02 process 분리와 동일 범위 비용 비교 후보 (2026-10-14)

- `--jobs 1` 기본값과 직접 직렬 실행을 유지하고 `--jobs 2` 코드 경로를 추가했다. fast 항목은 직렬이며, 실제 격리 관측이 있는 tmux Bash/sh 두 셀만 별도 Python process로 겹쳐 실행한다. Herdr와 나머지 precise 셀은 직렬이다. 기존 17개 항목·19개 exact test ID·assertion·실시간 wait와 보고 순서를 유지한다.
- worker는 allowlist 환경·전용 HOME/namespace를 쓰고 PID/start-ticks 근거로 소유 process만 정리한다. sentinel의 실제 전후 SHA와 worker identity/정리를 결과에 기록한다. 별도 worker의 parent-death/SIGTERM 정리는 기존 소유 자식 정리 경로로 연결하며, 공용 process의 ThreadPool에서 subreaper 귀속을 섞지 않는다.
- `--baseline-report <report.json>`은 같은 입력 전후 identity·도구 버전·OMP 경로·mode·cwd·환경 template·선정 항목/test ID/assertion/필수 step·수집 수와 정상 정리 근거를 확인한다. jobs 수와 전용 namespace 차이는 실행 전략으로 별도 기록한다. 실행한 core 범위의 wall time·항목별 시간·합을 비교하되, 필수 후행 항목 네 개가 미실행이면 전체 precise는 계속 inconclusive다. 없는 원본·실패·누락된 정리·다른 수집 수를 비용 통과로 바꾸지 않는다.
- [최초 자체 fixture 실패](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/parallel01/impl01/new-fixtures.log)는 정상 정리 결과의 `remaining` 필드 누락이었다. 이미 계산한 기본값을 기록하고 기존 stop 관측을 보존했다. [독립 시험 실패](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/parallel01/test01/result.json)는 수집 수 1→2 변경도 comparable로 판정한 결함이었다. 실행 범위의 수집 수 map 전체를 비교해 unknown으로 남기도록 보완했으며 두 원 실패와 시험 기대값은 유지한다.
- [보완 뒤 검사](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/parallel01/impl01/fix02-result.json)는 독립 8개와 관련 25개, 총 33개 fixture 통과다. 실제 두 셀 재검증은 이 후보의 별도 runtime 근거로 기록한다. 이 부분 후보·fixture 결과로 새 전체 baseline/after 비교·10분 목표·G-DV-05·전체 precise/46/full14 통과를 주장하지 않는다. C2 hold·operating MODEL/I/O unknown·실제 raw 비활성을 유지한다.
- 이후 독립 시험에서 중단 시 확정 실패를 덮는 분류, spawn 직후 정리 공백, 무관한 adopted process의 종료, 누락된 sentinel 근거의 비용 비교 허용을 [실제로 재현](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/parallel01/test02/result.json)했다. Claude 자문을 참고해 runner 한 파일에서 실패 보존·spawn 수명 정리·receipt 기반 소유권·전후 digest 형식을 보완했다. [변경하지 않은 독립 12개와 관련 25개 검사](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/parallel01/impl01/fix03/fixtures.log)는 총 37개 통과했으며, 원 실패 기록과 기존 시험 기대를 보존한다. 통합 검사와 실제 tmux Bash/sh 재검증은 별도 근거가 필요하다.
- 다음 리뷰의 종료 직전 정리 경계도 [독립 시험 1개로 재현](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/parallel01/test03/result.json)했다. 정상 driver 종료 뒤 실제 부모 종료가 일어나면 마지막 소유 자식 정리를 건너뛰던 순서를 바로잡고, 닫힌 FD/selector의 bookkeeping을 함께 보완했다. [기존 13개 독립 시험과 관련 25개 검사](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/parallel01/impl01/fix04/fixtures.log)는 총 38개 통과했으며, 원 실패를 보존한다. 이 보완도 전체 precise 비용·46개 gate·운영 MODEL/full14 통과를 뜻하지 않는다.

## 2026-10-14 Worker 요약 모델과 단순 예산 진행

- [LG-D30](BRIEF.md#lg-d30--worker-모델-재사용과-단순한-요약-예산-2026-10-14)에 따라 별도 checkout에서 Worker.default descriptor 선택·parent/child epoch 구분·단순 output 기본 요청·context 중심 선정 구현을 진행했다. Own fixture Python17/Node34 통과는 구현자의 부분 근거이며 독립 테스트/리뷰·통합 전 최종 pass가 아니다. 제품 compatibility record/admission 소비자와 실제 운영 model/provider 의미는 후속 정렬·관측 대상이다.
- 현재 core 비용 baseline의 Herdr/sh C2에서 Manager `esc_closed_menu=False`를 한 번 관측했다. [원 기록](../../../.workflow/dev-validation-log-retention/runs/dvl02-20261014/core-cost-3679155-01/baseline/08-compat_herdr_dash-compat-herdr-dash.json)을 보존하고 기존 tmux/dash의 같은 assertion과 연결하되 같은 원인·재현성을 단정하지 않는다. 이 baseline으로 전체 jobs2 비용 비교 pass를 만들거나 실패 셀을 제외하지 않는다. 기존 C2 hold와 실제 raw 비활성은 유지한다.
