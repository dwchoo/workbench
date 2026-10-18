# 중요 로그 CLI 안내·본문 표시 개선

Revision b1 · 2026-10-18 · 방향을 정리한 후속 계획 입력. 정확한 구현 bundle은 승인 전 초안이다.

## 출처와 권한

- 직접 입력: [CLI-UX-PLAN](../CLI-UX-PLAN.md). 이 문서의 두 개선과 최소 검증 범위를 이어받는다.
- U-UX-01: 사용자가 실제 사용 시험에 근거한 개선을 구체적으로 요청했다.
- U-UX-02: “좋아 이거에 대한 개선 계획을 claude-wb랑 논의해서 계획 세워봐.”
- U-UX-03: `$to-tickets docs/features/dev-validation-log-retention/storage-operations/CLI-UX-PLAN.md`.
  SPEC·티켓 작성 권한이다. 제품/테스트 구현, 모델 실행, 실제 삭제·활성화 권한은 아니다.
- [상위 후속 BRIEF](../BRIEF.md), [기존 SPEC](../SPEC.md)의 저장 정책·권한·JSON 계약은 유지한다.
  완료된 DVL-OPS-01~03과 원래 기능의 승인·완료·실패 기록을 변경하지 않는다.

## 관측 사실

claude-wb가 보고한 `storage-ops-hands-on-20261018-01` 재개 시험에서는 Task 입력 → 실제
Worker.default 요약 → stored/receipt digest 일치 → CLI 조회와 임시 자원 정리를 확인했다.
이 보고는 Task 경로의 근거이며 command 귀속·tool roster 직접 관측·모든 실패 경로까지 확장하지 않는다.
이번 계획에서는 인증·실제 DB·모델에 접근하거나 원래 시험을 다시 실행하지 않았다.

남은 관찰은 미생성/조회 불가 안내가 모호하다는 점과 기본 show가 JSON lines라는 점이다.
“사람용 출력 첫줄 JSON”은 stdout/stderr를 합쳐 본 관찰로 정정됐으며 별도 저장 결함이 아니다.

## 목표와 제안 범위

1. `logs important status`: 저장소 경로가 없다고 확인된 경우에만 그 사실과 첫 사용 안내를 표시한다.
   존재 확인 실패·catalog 이상·busy를 미생성이라고 단정하지 않는다. DB 수/용량 unknown과 exit 1은 유지한다.
2. 사람용 부분 조회 경고의 DB UUID와 기존 reason을 JSON 배열 대신 읽기 쉬운 문구로 표시한다.
3. `logs important show`: 기본 출력을 작업 ID·기록 종류/시각·본문·출처·gap·DB UUID로 정리한다.
   사실/해석/미확인 내용을 재요약·절단하지 않고 본문 개행을 보존한다.
4. `--json` 전체 응답과 종료 코드·순서·limit/page를 유지한다. 기본 show를 파싱하던 스크립트에는
   기존 `--json` envelope로 전환해야 함을 안내한다. JSON lines와 같은 형식이라고 설명하지 않는다.

새 용어는 추가하지 않는다. [CONTEXT](../../../../../CONTEXT.md)의 중요 로그·활성 DB·보존 DB를 사용한다.
CLI 내부의 metadata 관측과 formatter만 추가하는 기술 상세는 SPEC에 구분한다. 새 ADR은 필요하지 않다.

## 비목표·유지 조건

- reader API/schema/상태 의미, 저장·요약·모델/skill/writer, 작업 귀속·no-replay·실패 보호를 바꾸지 않는다.
- C2 보류와 실제 raw 순환 활성화·삭제 비활성을 유지한다. 보관/예산/fallback 정책도 변경하지 않는다.
- list/show의 미생성 판정을 새로 설계하거나 모든 오류를 세분화하지 않는다.
- 새 라이브러리·서비스·queue·전역 설정·자동 교정·전체 고장 matrix는 추가하지 않는다.
- 기존 유효 근거를 재사용하고 CLI 변경에 필요한 검증만 한다. 유료 모델·500MB 검증은 반복하지 않는다.

## 수락 예시

- 전용 important 경로가 없으면 경로 없음/첫 사용 안내, DB 수·bytes 미확인, 기존 exit 1을 함께 표시한다.
- 디렉터리가 있지만 active.json이 없거나 catalog가 busy이면 조회 불가/기존 사유를 표시한다.
- show에 서로 다른 Task/command fixture를 주면 각 ID·본문·출처·누락 범위가 섞이지 않고 표시된다.
- `--json`에 사람용 안내나 새 metadata를 섞지 않는다. 조회 때문에 저장소를 생성하지 않는다.

## 미결정과 승인 상태

일반 요구사항 인터뷰를 다시 할 필요는 없다. 단, 기본 show 출력의 호환성 변경을 포함한 정확한
SPEC/PLAN/ticket bundle은 사용자의 구현 요청 또는 별도 승인 전까지 초안이다.
기존 parent 28 AC/46 gate 및 OPS 8 AC/7 gate는 삭제·재판정하지 않는다.
