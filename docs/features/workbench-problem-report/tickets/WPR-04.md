# WPR-04 — Manager 전용 연결과 기존 격리 계약 보존

계획 revision: p1; 명세: [SPEC s1](../SPEC.md); 원본: [BRIEF r1.0](../BRIEF.md).
상태: 제안. 구현·test 코드 작성 권한은 아직 없다.
의존성·gate owner/supplier·resource·write scope·worker 추천은 [PLAN.json](../PLAN.json)의 이 ticket 항목이 정본이다.

## 목적과 전달 동작

실제 작성된 skill/reference/helper를 Manager에만 연결한다. 기존 isolation과 Worker 제한을 유지한다.

ROLE_SKILL_PATTERNS의 Manager 목록에만 workbench-report를 추가하고 README에 사용자 전용 사용법과 측정 OMP 버전을 설명한다. model-visible skills와 user skill commands를 혼동하는 기존 assertion이 있으면 의미를 보존해 갱신한다. role filters, ambient-source isolation, Worker tools/agents, 다른 skill 동작은 유지한다. 필요 이상으로 isolation checker를 재설계하지 않는다.

## 수락과 계약

담당 AC: WPR-AC-01, WPR-AC-03, WPR-AC-11.
SPEC의 공개 계약·불변식과 수락 추적을 따른다. 로컬 gate와 최종 G-07/G-08의 증거 범위를 구분한다.
WPR-02/03 integrated and local gates passed on a common source snapshot; preserve existing role tools/agents and unrelated skills.

## 검증 요구

G-06. 실제 shipped assets로 role_skill_allowlist/observe_isolation의 분리된 목록, reference/helper 경로, 기존 focus 회귀를 확인한다. 명령: PYTHONPATH=src:. python3 -m unittest tests.backend.test_paths_launcher tests.backend.test_omp_isolation tests.backend.test_workbench_skills_independent_p27cd69c 와 새 local test discovery. runtime 확인은 G-08의 final owner가 수행한다.

필수 independent test_designer와 fresh reviewer는 PLAN의 verification unit 경계를 따른다.
Root가 실제 candidate와 근거를 확인한 뒤 의존성 gate를 연다. 이 문서는 통과 증거가 아니다.

## 정지·보고 조건

기존 allowlist/보안 경계를 넓히거나 모든 ambient skill을 켜서 해결하지 않는다. WPR-01 실측과 다르면 affected gate를 다시 확인한다. 승인된 SPEC·PLAN 내용 변경은 Root의 scope/authority 검토를 거친다.

결과에는 관측 사실, 미확인, 사용한 source candidate, 검증 명령·결과, 남은 문제를 구분한다.
commit·게시·배포는 하지 않는다.
