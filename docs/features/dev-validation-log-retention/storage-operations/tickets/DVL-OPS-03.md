# DVL-OPS-03 — 관련 통합 검증과 운영 적용 준비

목적: 표시 변경과 기존 순환·보관 경로의 연결을 독립 검증하고 실제 적용 전 검토 가능한 결과를 만든다.
의존·resource·gate owner/supplier는 [PLAN](../PLAN.json), 검증 계약은 [SPEC](../SPEC.md)을 따른다.

## 제공할 결과

- OPS-01 재사용 판단과 OPS-02 최종 diff를 묶어 독립 test_designer와 frozen reviewer로 검증한다.
  필요 security trigger만 별도 검토한다. 변경 없는 전체 feature/outer 검사를 반복하지 않는다.
- 기존 실제 mixed producer/store/reader·proof·gap 근거를 재사용하고, 부족한 연결만 작은 임시 한도로
  실행한다. 임시 Backend/CLI 조회 경로를 포함해 legacy 불변·실제 raw 교체·정확한 gap,
  정상 요약/결과/근거 보존, 실패 표시·sent/unknown no replay·실행 지속을 확인한다.
  실제 SDK가 필요한 빈틈은 기존 scripted harness를 사용하고 새 fake success로 대신하지 않는다.
- 활성 DB의 120일/압박 정리·시작/최신 누적 보호·rollover/archive 보존과 명시 삭제 보호는
  기존 관련 근거/시험을 diff 영향에 맞춰 재사용한다. 실제 사용자 DB는 테스트 대상으로 쓰지 않는다.
- 활성화 안내에는 대상 data root, legacy/신규 적용 차이, 현재 로컬 product record의 gate/identity
  확인 결과와 가능/보류 사유를 포함한다. 명령이 준비돼도 실행하지 않는다.
- legacy 종료 확인 후보와 archive UUID별 크기/소실 정보를 제시한다. 기존 명시 삭제 도구로 가능한
  것과 별도 수동 정리 대상을 구분하고, active/orphan/실행 중/불명은 삭제 대상으로 확정하지 않는다.
- 실제 순환 삭제 비활성·C2 hold 유지, current source와 각 증거의 관계, 테스트/리뷰/미정리 자원,
  관련 사용법과 운영 승인 경계를 최종 보고한다. 상위 goal을 다시 열지 않는다.

## 수락·검증

LSO-AC-01~08 및 PLAN의 모든 로컬 gate. 상위 46 gate registry의 ID/level을 원본과 대조하고
관련 변경의 영향만 재검증한다. 이 후속의 통과를 Q01·실모델·C2 또는 최초 활성화의 자동 승인으로 쓰지 않는다.
새 격리 실행 전 용량 확인, 소유 process 종료/회수·socket/temp 정리와 종료 후 용량·캐시 유지보수를 수행한다.
재실행은 새 run 출력에 쓰며 이전 실패·증거를 덮지 않는다. cleanup 실패는 숨기지 않는다.

## 쓰기·중단 경계

시험·보고서·사용법만 수정하며 제품 추가 수정이 필요하면 OPS-02 범위로 원인을 돌려 교정·delta review한다.
무조건적인 500MB 재시험, 새 백그라운드 서비스, 무제한 원본 보관, 실제 데이터 삭제/활성화,
새 paid 모델 실행, 사용자 세션 중지, 기존 승인 bundle 변경은 하지 않는다.
운영 gate가 불충족이면 개발 검증 결과는 유지하고 운영만 보류한다. 관측되지 않은 것은 unknown으로 남긴다.
