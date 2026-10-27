# WBX-A5: 사용자 Chrome 확인(WBX-O01)

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-A5`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: 사용자(관찰) + orchestrator 기록. Agent가 대신할 수 없는 실제 browser 확인이다.

## 제공할 동작

사용자가 실제 Chrome(평문 HTTP, LAN 주소)에서 Workbench의 자동 갱신 없는 화면 사이를 오가며 뒤로 가기 경고가 매번 뜨는지 확인한다. Orchestrator가 결과를 `docs/features/web-board-ux/CHECK-CHROME.md`(새 파일)에 기록한다: 날짜, Chrome 버전, "Always use secure connections" 설정, 주소 형태, 오간 화면 순서, 경고 횟수, `/`(meta refresh가 남아 있으면) 따로 표시. 같은 기회에 배너 접힘 기억(WBX-AC-05), 복사 버튼(WBX-AC-06), 좁은 화면 nav(WBX-AC-01), 로그아웃 뒤 뒤로 가기(WBX-AC-03)를 함께 본다.

## 수락 기준

- WBX-AC-04: 효과가 있으면 그 기록, 없으면 알려진 한계로 기록하고 USAGE(WBX-A2가 쓴 항목)가 TLS를 근본 해결로 안내함을 확인.

## 계약과 맥락

선행: WBX-A1·A2·A3 통합. 시점은 트랙 A 뒤(WBX-D22). `/`의 10초 refresh는 트랙 C까지 남으므로 뒤로 가기 확인은 자동 갱신이 없는 화면(요청 목록 `/requests`, 요청 상세, 설정·key 목록, 프로젝트 상세 등) 사이에서 하고, `/`는 기록에서 따로 표시만 한다.

## 검증

증거 수준 runtime(사용자 관찰). 자동 테스트 없음(TDD 대상 아님).

## 범위 밖과 중단 조건

결과에 따라 새 기능을 더하지 않는다(WBX-D16). 효과가 없으면 사용자에게 다음 선택을 묻는다.

## 출력

`CHECK-CHROME.md`, 관찰 요약.
