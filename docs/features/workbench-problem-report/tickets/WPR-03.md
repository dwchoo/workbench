# WPR-03 — 사용자 인터뷰 skill과 reference 양식

계획 revision: p1; 명세: [SPEC s1](../SPEC.md); 원본: [BRIEF r1.0](../BRIEF.md).
상태: 제안. 구현·test 코드 작성 권한은 아직 없다.
의존성·gate owner/supplier·resource·write scope·worker 추천은 [PLAN.json](../PLAN.json)의 이 ticket 항목이 정본이다.

## 목적과 전달 동작

사용자 직접 호출 뒤 기존 자료를 조사해 일관된 보고서를 만드는 skill과 reference를 작성한다. 정해진 양식이 있으면서 원인 미확정을 허용하는 것이 핵심이다.

실측 frontmatter로 SKILL.md를 작성하고 매 호출 references/report-template.md를 읽게 한다. 영어 지시문과 한국어 열 가지 heading, WB 관련성 판별, 선행 자료 확인·필요 질문 묶음, 기존 status/logs 읽기, segmented/missing 구간, 가림·발췌 한도, helper 호출·결과 처리와 저장 실패 전문 제공을 명시한다. host terminal 제어·Worker 위임·재현·수정·자동 복구를 시작하지 않는다.

## 수락과 계약

담당 AC: WPR-AC-02, WPR-AC-04, WPR-AC-05, WPR-AC-06, WPR-AC-07, WPR-AC-08, WPR-AC-09, WPR-AC-10, WPR-AC-12, WPR-AC-13, WPR-AC-14.
SPEC의 공개 계약·불변식과 수락 추적을 따른다. 로컬 gate와 최종 G-07/G-08의 증거 범위를 구분한다.
integrated WPR-01 pass and measured metadata; SPEC s1 frozen heading/helper contract; helper implementation is not needed for local asset checks.

## 검증 요구

G-05의 로컬 자산 계약을 검증한다. static 문구 검사는 G-07의 실제 skill_behavior 통과가 아니다. helper가 아직 없어도 SPEC의 고정 CLI/JSON 계약으로 지침을 작성하고 로컬 reference 검사를 할 수 있다. 독립 테스트가 요구사항에서 검증해야 할 normal/partial/unknown/unrelated/save-failure fixture 목록을 남긴다.

필수 independent test_designer와 fresh reviewer는 PLAN의 verification unit 경계를 따른다.
Root가 실제 candidate와 근거를 확인한 뒤 의존성 gate를 연다. 이 문서는 통과 증거가 아니다.

## 정지·보고 조건

예시를 사실로 채우거나 모르는 버전·Task ID·시각을 꾸미지 않는다. 새로운 bridge tool·DB 정책·전체 environment 수집·자동화 상태 변경이 필요해지면 구현하지 말고 Root에 gap을 보고한다. helper와 계약 불일치는 해당 파일을 몰래 수정하지 말고 통합 시 조정한다.

결과에는 관측 사실, 미확인, 사용한 source candidate, 검증 명령·결과, 남은 문제를 구분한다.
commit·게시·배포는 하지 않는다.
