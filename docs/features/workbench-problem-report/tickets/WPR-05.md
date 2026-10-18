# WPR-05 — 최종 skill 동작·실제 OMP 통합 검증

계획 revision: p1; 명세: [SPEC s1](../SPEC.md); 원본: [BRIEF r1.0](../BRIEF.md).
상태: 제안. 구현·test 코드 작성 권한은 아직 없다.
의존성·gate owner/supplier·resource·write scope·worker 추천은 [PLAN.json](../PLAN.json)의 이 ticket 항목이 정본이다.

## 목적과 전달 동작

결합된 최종 후보가 사용자 호출부터 독립적으로 읽을 수 있는 보고서와 실패 fallback까지 동작함을 검증한다. 상태 flag나 scripted 문장만으로 완료 처리하지 않는다.

독립 test_designer가 요구사항에서 기대를 도출한 뒤 frozen skill을 실제 host agent로 실행하는 G-07을 관측한다. G-08은 실제 OMP와 소유 WB bridge/helper, synthetic status/log/raw/missing 자료, localhost scripted provider로 연결을 검증한다. saved artifact를 원자료 없는 다른 위치에서 읽고, 지정 경로·충돌·저장 실패도 확인한다. 일반 증상·notice·Worker에서는 skill이 시작되지 않는 trace를 보존한다.

## 수락과 계약

담당 AC: WPR-AC-01, WPR-AC-02, WPR-AC-03, WPR-AC-04, WPR-AC-05, WPR-AC-06, WPR-AC-07, WPR-AC-08, WPR-AC-09, WPR-AC-10, WPR-AC-11, WPR-AC-12, WPR-AC-13, WPR-AC-14.
SPEC의 공개 계약·불변식과 수락 추적을 따른다. 로컬 gate와 최종 G-07/G-08의 증거 범위를 구분한다.
frozen combined candidate with WPR-U1/U2 required local checks and independent review coverage; no product repair under verification-only scope.

## 검증 요구

G-07/08와 이전 gate의 현재 candidate coverage를 확인한다. actual skill reasoning과 scripted wiring evidence를 별개로 보고한다. fresh reviewer와 별도 security reviewer, deterministic secret scan, independent tests, integration delta review 및 Root final checks를 포함한다. 새 test harness는 python3 -m unittest tests.integration.live_workbench_report 경로로 실행 가능하게 한다. graphify update는 실제 제품 code 변경 후 implement에서 수행한다.

필수 independent test_designer와 fresh reviewer는 PLAN의 verification unit 경계를 따른다.
Root가 실제 candidate와 근거를 확인한 뒤 의존성 gate를 연다. 이 문서는 통과 증거가 아니다.

## 정지·보고 조건

이 ticket의 직접 write scope는 검증 코드·증거다. 제품 결함은 owner ticket의 필요한 correction으로 돌리고 freeze/test/review를 갱신한다. unavailable runtime/agent 평가를 pass로 바꾸지 않는다. 사용자의 auth·실제 로그·유료 모델 CLI를 자동 사용하지 않는다. 종료 후 프로세스와 cache 정리·용량을 보고한다.

결과에는 관측 사실, 미확인, 사용한 source candidate, 검증 명령·결과, 남은 문제를 구분한다.
commit·게시·배포는 하지 않는다.
