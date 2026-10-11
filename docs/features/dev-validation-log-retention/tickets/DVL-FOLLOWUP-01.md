# DVL-FOLLOWUP-01 — 비차단 후속 backlog

[2026-10-10 진행 방향 동의](../../../../.workflow/dev-validation-log-retention/runs/blockers-20261010/authority.json)에 따라
현재 완료를 막지 않는 개선을 모았다. 이 문서는 새 기능·정책·수치의 구현 승인이 아니다.

| 항목 | 유지할 경계 | 착수 조건 |
|---|---|---|
| 조회 cold cache·느린 저장소 추가 측정 | 기존 warm/synthetic 측정을 일반 보장으로 확대하지 않는다 | 실제 측정 환경과 필요성 확보 |
| 미checkpoint archive snapshot 비용·scratch 한도 | LG-D15 사본 보호·최신 WAL과 기존 실패 사유를 유지한다 | 큰 archive의 실제 비용 문제 또는 수치 채택 요청 |
| 추가 경합·조회 최적화 | 현실 부하 판별과 연속 stress 한계 기록을 보존한다 | 현실 호출에서 실패·지연이 관측될 때 |
| FL-02/03/04와 기타 Core 개선 | [Core 후속 목록](../../core-workbench/FOLLOWUPS.md)의 원 기록을 유지한다 | 별도 우선순위·범위 합의 |
| 실모델 품질·비용 확신 시험 | scripted/fixture와 구분한다 | 별도 모델·유료 예산 승인 |

Q01, C2, 저장·출처·권한 위반 등 필수 gate 실패는 이 backlog로 옮겨 완료 처리하지 않는다.
실제 raw 활성화, Phase 2와 truecolor는 별도 사용자 결정 대상이다.
