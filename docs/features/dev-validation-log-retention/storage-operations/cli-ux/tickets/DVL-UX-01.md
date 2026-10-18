# DVL-UX-01 — 중요 로그 상태·본문 표시와 사용 안내

명세: [SPEC s1](../SPEC.md). 의존성과 쓰기/자원 범위는 [PLAN p1](../PLAN.json)의 이 ticket 항목을 따른다.
진행 상태와 실제 배정은 Root operational record만 유지한다.

## 전달할 동작

status의 불완전 조회에서 전용 경로 없음이 확인된 경우에만 조건부 첫 사용 안내를 추가한다.
기존 실패/부분 조회는 사유와 exit 1을 유지한다. 사람이 읽는 경고는 stderr에서 DB·reason을 표시한다.
show 기본 출력은 기존 entry의 ID·종류·시각·본문·출처·gap·DB UUID를 한국어로 구분한다.
`--json`의 전체 응답, page/limit/exit와 정상 list는 보존한다. USAGE에 기본 show 출력 변경을 안내한다.

## 수락 기준

UX-AC-01~06의 구현을 제공한다. worker quick check는 구현 가능 여부만 확인한다.
독립 수락 증거의 소유자는 02이며 01이 뒤 ticket의 결과를 자체 완료 기준으로 소비하지 않는다.

## 계약과 관련 근거

- 기준 commit `1a7f97d`; 기존 cli.py down alias·UI·사용자 workflow dirty 변경을 보존한다.
- 새 helper는 해석된 important 경로 하나의 metadata만 조회한다. JSON dict/reader/schema는 변경하지 않는다.
- 원본 body를 다시 모델로 보내거나 파싱·요약하지 않는다. 개행 정리는 기존 `_plain`을 줄별 재사용한다.
- [입력 계획](../../CLI-UX-PLAN.md), [USAGE](../../USAGE.md), SPEC의 채널/부분 결과 규칙을 따른다.

## 검증과 handoff

작은 parser/main 테스트와 관련 기존 status 테스트를 실행한다. 임시 root와 프로세스는 자기 소유만 정리한다.
Root에 candidate ID, 정확한 변경 경로, 실행 명령/결과/log, 실패·미확인, 남은 자원을 전달한다.
독립 test_designer/reviewer와 최종 결합 검증은 02에서 이 통합 후보를 대상으로 한다.

## 범위 밖·중단 조건

reader API/schema·새 reason·exit 변경·공유 `_plain` 의미 변경·설정/credential 접근이 필요하면 원인을 보고한다.
새 기능·모델 호출·보관/비용 정책·실제 삭제/활성화·과거 증거 재작성은 하지 않는다.
