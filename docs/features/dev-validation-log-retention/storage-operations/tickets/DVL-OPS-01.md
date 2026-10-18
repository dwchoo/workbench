# DVL-OPS-01 — 저장 현황과 기존 검증 근거의 범위 확인

목적: 새 저장 기능을 만들기 전에 실제 관리 범위와 남은 표시 공백을 확정한다.
의존·resource·기계 수락 정의는 [PLAN](../PLAN.json), [SPEC](../SPEC.md)을 따른다.

## 제공할 결과

- 현재 checkout/dirty 입력과 기존 완료 근거를 읽고 관련 gate의 재사용/영향/미확인을 대응시킨다.
  상위 28 AC/46 gate 목록을 보존하며 flag만으로 current pass를 주장하지 않는다.
- 지정 데모 `~/wb-demo/data`의 기존 readonly 상태 조회와 metadata 크기 조사만 수행한다.
  legacy run/terminal, segmented raw/control, 활성/보존 DB별 UUID·role·물리 크기·부분 상태를 시점과 기록한다.
  root가 없으면 unavailable로 남기며 만들거나 시작하지 않는다. 로그 본문·인증정보는 읽지 않는다.
- 확정 종료된 기존 legacy 후보와 수동 선택 대상 archive를 구분한다. 미확인/실행 중은 보류한다.
  새 legacy 삭제 API·script는 만들지 않고 실제 삭제도 하지 않는다.
- OPS-02의 출력 표기·기존 JSON key 대응을 확정한다. 이미 충족한 부분은 다시 구현하지 않는다.
  운영 적용을 막는 실제 gate/record 문제는 후속 개발과 구분해 기록한다.

## 수락·검증

LSO-AC-01/02/05/06. 실제 관측값·관측 시각·unknown을 포함한 작은 현황 보고와 재사용 대응표를 남긴다.
문서/metadata 조사 ticket이므로 새 executable test는 필요 없다. 독립 검토는 OPS-03의 한 단위로 묶는다.
현재 사용자 환경은 변할 수 있으므로 live 값의 불변을 검증 통과 조건으로 삼지 않는다.
기존 gate의 로그 hash·후보·source 관련성을 검사하고, 인용 없는 오래된 pass는 unknown으로 둔다.

## 쓰기·중단 경계

제품/테스트·실제 data root는 쓰지 않는다. 작은 보고서는 이번 후속 `.workflow` run에만 둔다.
기존 root 재사용·삭제·중지/재시작·DB 복구·새 credential 복사·모델 요청은 하지 않는다.
권한 있는 readonly 조사로 범위를 확인할 수 없으면 그 항목만 partial로 보고한다.
판별이 새 API/schema 또는 raw 활성화 기준 변경을 요구하면 원인과 재개 조건을 Root에 보고한다.
