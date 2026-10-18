# WPR-01 — 사용자 전용 호출 가능성 판별

계획 revision: p1; 명세: [SPEC s1](../SPEC.md); 원본: [BRIEF r1.0](../BRIEF.md).
상태: 제안. 구현·test 코드 작성 권한은 아직 없다.
의존성·gate owner/supplier·resource·write scope·worker 추천은 [PLAN.json](../PLAN.json)의 이 ticket 항목이 정본이다.

## 목적과 전달 동작

새 제품 기능의 선행 조건은 현재 OMP에서 사용자 slash 노출과 model discovery 제외를 동시에 만족하는 것이다. 기존 README의 18.7.0 기록을 18.11.1 runtime 증거로 대체하지 않는다.

기존 live_omp_isolation_probe/check_isolation 패턴으로 소유한 임시 HOME·project·canary skill 하나와 localhost scripted provider를 준비한다. Manager/Worker 실제 실행의 command와 prompt를 관측하고 직접 slash 확장/인수 전달, 일반 입력·notice·peer 입력의 비확장을 판별한다. provider 요청과 출력 marker를 비교하고 유료 모델·사용자 auth를 사용하지 않는다. 제품 allowlist나 실제 skill은 아직 추가하지 않는다.

## 수락과 계약

담당 AC: WPR-AC-01, WPR-AC-03.
SPEC의 공개 계약·불변식과 수락 추적을 따른다. 로컬 gate와 최종 G-07/G-08의 증거 범위를 구분한다.
BRIEF r1.0 + SPEC s1; current repository snapshot; no product skill or feature assumed present.

## 검증 요구

G-01. 두 pipe/PTY를 계속 drain하고 각 probe deadline을 명시하며 process group 종료·회수를 확인한다. secret 없는 version/metadata/request-shape/marker 증거만 보존한다. 새 격리 실행 전 10 GiB 경계를 확인하고 종료 후 재생성 cache를 정리한다.

필수 independent test_designer와 fresh reviewer는 PLAN의 verification unit 경계를 따른다.
Root가 실제 candidate와 근거를 확인한 뒤 의존성 gate를 연다. 이 문서는 통과 증거가 아니다.

## 정지·보고 조건

실제 기능이 없거나 probe가 불명확하면 원인·시도·passed/failed/inconclusive를 기록하고 확장을 보류한다. probe 결함은 bounded repair로 분류한다. OMP fork·전용 daemon·자동 호출 허용 등으로 요구를 바꾸지 않는다. 통과한 정확한 frontmatter와 command 동작이 후속 ticket의 입력이다.

결과에는 관측 사실, 미확인, 사용한 source candidate, 검증 명령·결과, 남은 문제를 구분한다.
commit·게시·배포는 하지 않는다.
