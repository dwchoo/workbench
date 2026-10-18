# DVL-UX-02 — 독립 검증·리뷰와 최종 결합 확인

명세: [SPEC s1](../SPEC.md). 실행 graph와 gate 공급자는 [PLAN p1](../PLAN.json)을 따른다.
Root가 소유하며 테스트/리뷰 역할은 각자 독립 범위로 배정한다.

## 전달할 결과

01의 통합된 CLI/USAGE 변경을 한 검증 단위로 평가하고, 모든 UX-AC-01~06의 현재 증거를 연결한다.
기존 완료 범위와 실제 모델 Task 시험은 유효한 부분만 재사용한다. 새 full46/model 통과를 주장하지 않는다.

## 수락·검증

- G-UX-01: 경로 없음, 정상 빈 DB, 기존 경로/active.json 없음, busy/일반 실패/metadata 실패를 구분한다.
- G-UX-02: Task/command의 작은 기록으로 body 세 구획·개행·출처·gap·DB UUID가 바뀌지 않고 표시된다.
- G-UX-03: JSON 전체 dict/exit/채널/순서/빈 결과/page 계약을 기존 동작과 대조한다.
- G-UX-04: 전용 임시 store에서 실제 reader와 parser/main을 거치는 결합 smoke 및 자원 정리를 확인한다.
- G-UX-05: 기본 show 호환성 안내·변경 범위·기존 안전 경계와 fresh review 결과를 확인한다.

test_designer는 요구 기준을 먼저 읽고 필요한 테스트만 작성·실행한다. Root는 검증된 후보를 동결해
fresh reviewer에게 제공한다. metadata 경로 조회가 보안 trigger이면 별도 security review와 diff secret scan을 한다.
실패가 있으면 코드/시험/환경/계약을 구분하고 필요한 수정 뒤 해당 delta와 영향을 재검증한다.
수리가 product write이면 Root가 같은 단일 writer 규칙으로 01 범위를 재배정한다. test writer가 제품을 고치지 않는다.

## 완료와 보고

Root가 최종 후보·허용 변경·gate별 증거 수준/한계·독립 리뷰·소유 자원 정리를 확인한다.
필수 실패/미검증을 pass로 표시하지 않는다. status flag나 worker 완료만으로 gate를 통과시키지 않는다.
원래 실패 기록은 보존하며 기존 OPS 완료 record와 parent 28AC/46gate의 의미를 변경하지 않는다.

## 범위 밖·중단 조건

사용자 DB/세션/auth를 사용하는 운영 시험, 새로운 모델 요청, 500MB 반복 검증, 실제 raw 삭제/활성화는 하지 않는다.
새 표면/API나 미합의 호환성 변경이 필요하면 affected scope를 멈추고 명시한다.
