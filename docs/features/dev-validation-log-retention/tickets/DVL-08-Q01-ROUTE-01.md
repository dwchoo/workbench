# DVL-08-Q01-ROUTE-01 — 요약 경로 차단점 판별

상위 ticket: [DVL-08](DVL-08.md). 기존 SPEC s5/PLAN p5와 Q01 8개 기준을 유지하는 하위 작업이다.
권한: [2026-10-10 진행 방향 동의](../../../../.workflow/dev-validation-log-retention/runs/blockers-20261010/authority.json).
기록 구분: 아래 2026-10-10 source 판별의 `blocked_adoption`과 실패 근거는 보존한다. 현재 재개 권한과 경로 결정은 문서 끝의 LG-D22 기록을 따른다. 경로 채택은 Q01 통과·후속 구현 완료가 아니다.

## 문제와 기존 근거

native/callback 경로는 guard 누락·미작동·확인 후 정책 교체 음성 조건에서 요청 전송을 막지 못했다.
private owner 후보는 내부 tool/settings/model 변경과 reentrant 변경을 통제한다는 근거가 부족하다.
[기존 runtime10](../../../../.workflow/dev-validation-log-retention/runs/implement-p5-20261009/resume-result-dvl08-runtime10.json),
[runtime11](../../../../.workflow/dev-validation-log-retention/runs/implement-p5-20261009/resume-result-dvl08-runtime11.json),
[ownership 자문](../../../../.workflow/dev-validation-log-retention/runs/implement-p5-20261009/resume-result-dvl08-oracle02.json)을 보존한다.

## 이번 작업

1. 고정된 source 근거에서 tool policy를 바꾸는 실제 경로와 요청 전 최종 확인 경계를 좁게 대조한다.
2. 후보별로 지원 API, 소유·변경 통제 방법, 아직 증명하지 못한 조건을 적는다. 기존 hook 검색·실패한 실행을 그대로 반복하지 않는다.
3. 구체적인 후보와 새로운 판별 항목이 있을 때만 작은 credential-free 격리 probe로 정상·누락/noop/stale·실제 정책 교체를 비교한다.
4. 후보가 없으면 정확한 차단 조건과 재개에 필요한 새 근거를 반환한다. 더 큰 adapter/scheduler를 먼저 만들지 않는다.

## 판별과 종료

- 구현 가능한 후보의 지원 근거와 실제 시험 결과, 또는 후보 부재·증거 부족의 구체적인 원인을 남긴다.
- 음성 조건에서 실제 요청 전송이 있으면 실패다. proxy 전달 0을 원래 전송 0으로 바꾸지 않는다.
- 부분 성공은 상위 DVL-08 완료가 아니다. 경로 채택에는 기존 8개 전체 판별과 필요한 사용자 채택 결정이 남는다.
- DVL-09/10/14와 DVL-13 전체 완료의 차단은 상위 gate가 통과하기 전 유지한다.

Q01 기준 변경, proxy 채택, 새 제품 agent/상주 OMP, 유료 모델, 사용자 설정·credential 접근, 실제 raw 활성화는 제외한다.

## 2026-10-10 판별 결과와 재개 조건

Claude의 읽기 전용 자문은 parent extension의 `before_provider_request`가 최종 payload를 검사하고
throw로 전송을 막는 후보 하나를 제안했다. Root가 공개 v18.8.6 `ExtensionRunner` source를 확보해 확인한 결과,
payload handler의 예외는 공통 실행기에서 잡히고, 오류 알림 뒤 변경 전 payload가 반환된다.
따라서 자문의 P2 조건(throw/reject 시 dispatch 중단)이 이 source에서 부정이며, 중단 조건에 따라 추가 runtime probe는 실행하지 않았다.
[source hash·위치·판별 기록](../../../../.workflow/dev-validation-log-retention/runs/blockers-20261010/q01-source01/result.json)을 보존한다.
공개 source와 설치 binary의 빌드 대응은 미확정이므로 이를 설치 binary의 새 runtime 실패라고 표시하지 않는다.

재개에는 guard 누락/noop에도 전송을 막고 최종 request/policy에 결속하는 지원된 필수 dispatch capability,
또는 기존 실패를 뒤집는 구체적인 별도 경로 근거가 필요하다. 같은 hook 실험을 반복하거나
adapter/scheduler를 먼저 구현하지 않는다. Q01 8개 전체 판별·필요한 사용자 채택 결정과 상위 gate는 그대로 남는다.


## 2026-10-11 전용 DB 기록 방향 채택 후 재개

사용자는 전용 DB 기록 script/tool 방향을 승인하고 개발 재개를 요청했다([LG-D21](../BRIEF.md#lg-d21--전용-db-기록-기능-채택과-개발-재개-2026-10-11)).
[격리 OMP 시험](../../../../.workflow/dev-validation-log-retention/runs/q01-script-only-20261011/REPORT.md)에서는 일반 tools 없이 시작한 session의 고정 기록 tool만 실행 가능했고, 정상 1행 저장·일반 bash/write/edit 거부·추가 command/SQL/path 속성 거부를 확인했다.
기존 callback 후보와 다른 근거이므로 이번 적용 판별을 재개한다. 기존 agent/Task 경로에 동일 실행 제한을 적용할 수 있는 공개 API와 운영 route를 확인하고, 가능한 경로의 Q01 8개를 다시 판별한다.
원 실패와 source 판별은 보존한다. 새 격리 top-level 시험을 기존 Task 검증이나 전체 Q01 pass로 바꾸지 않는다. 후속 ticket의 진입 조건은 유지한다.

## 2026-10-11 제한 SDK session 운영 경로 채택

사용자는 “문서 요약도 sdk session 경로로 채택해.”로 [LG-D22](../BRIEF.md#lg-d22--운영-요약의-요청별-제한-sdk-session-경로-채택-2026-10-11)를 승인했다. 기존 Manager/Worker OMP 안의 요청별 제한 SDK session, 기존 agent 정의 prompt/skill 재사용, 전용 DB tool 1개, 종료 뒤 dispose가 채택 범위다. Task 경로·새 agent 정의·상주 프로세스·별도 운영 OMP·직접 HTTP completion은 포함하지 않는다.

[앞선 SDK 국소 검증](../../../../.workflow/dev-validation-log-retention/runs/script-only-adoption-20261011/REPORT.md)의 실제 OMP 21개 검사·독립 4개 테스트·Root 결합 9개 검사는 그대로 보존한다. 해당 fixture prompt 시험을 실제 agent prompt/skill 또는 Q01 8개 통과로 바꾸지 않는다. 경로 채택 질문은 해소됐으며 현재 남은 작업은 이 경로의 요청별 전송 경계와 기존 prompt/skill 전달을 먼저 검증하고 Q01 8개를 재판별하는 것이다. 확인 전 전송0 등 기존 기준과 PLAN 의존성을 유지한다.
