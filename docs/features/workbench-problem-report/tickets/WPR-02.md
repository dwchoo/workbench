# WPR-02 — 안전한 Markdown 준비·저장 helper

계획 revision: p1; 명세: [SPEC s1](../SPEC.md); 원본: [BRIEF r1.0](../BRIEF.md).
상태: 제안. 구현·test 코드 작성 권한은 아직 없다.
의존성·gate owner/supplier·resource·write scope·worker 추천은 [PLAN.json](../PLAN.json)의 이 ticket 항목이 정본이다.

## 목적과 전달 동작

보고서 저장 실패에도 가림 처리된 동일 전문을 제공하고 기존 파일을 보호하는 작은 Python helper를 제공한다. 새 수집기나 모델 기능을 만들지 않는다.

SPEC §4/5의 stdin Markdown→JSON 인터페이스, template 기반 heading 검사, secret 패턴 가림, UTF-8 크기 검사, 기본/지정 경로, create-only 충돌 처리와 부분 쓰기 오류를 구현한다. reference 구현을 기다리지 않고 테스트별 임시 canonical heading fixture를 사용한다. reference 탐색은 helper의 정해진 상대 자산 경로를 기준으로 하며 cwd 추측에 의존하지 않는다.

## 수락과 계약

담당 AC: WPR-AC-02, WPR-AC-08, WPR-AC-09, WPR-AC-11, WPR-AC-12, WPR-AC-14.
SPEC의 공개 계약·불변식과 수락 추적을 따른다. 로컬 gate와 최종 G-07/G-08의 증거 범위를 구분한다.
integrated WPR-01 pass; SPEC s1 helper CLI/JSON contract; tests may supply a temporary canonical reference fixture without WPR-03.

## 검증 요구

G-02/03/04. CLI black-box로 한글/공백 경로, unknown 섹션, bytes 경계, secret이 본문/오류/fallback에 섞인 경우, 충돌·symlink·동시 생성, 권한·ENOSPC·부분 쓰기를 확인한다. syscall fault는 소유 fixture와 독립 seam으로 주입하며 운영 경로를 손대지 않는다. known secret이 로그나 임시 raw 파일에 남으면 실패다.

필수 independent test_designer와 fresh reviewer는 PLAN의 verification unit 경계를 따른다.
Root가 실제 candidate와 근거를 확인한 뒤 의존성 gate를 연다. 이 문서는 통과 증거가 아니다.

## 정지·보고 조건

현재 디렉터리 전체 수집, DB 접속, subprocess/network 의존성, 출처 없는 원문 복제, raw 초안 임시 저장은 하지 않는다. 공개 CLI/JSON/heading 계약을 변경할 필요가 있으면 Root에 근거를 반환한다. 권한 없는 경로는 우회하지 않고 not_saved로 처리한다.

결과에는 관측 사실, 미확인, 사용한 source candidate, 검증 명령·결과, 남은 문제를 구분한다.
commit·게시·배포는 하지 않는다.
