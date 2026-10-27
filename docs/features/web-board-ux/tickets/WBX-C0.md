# WBX-C0: Mockup 선택 gate(WBX-D14)

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-C0`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: 사용자 결정 + orchestrator 기록. 구현 ticket이 아니다.

## 제공할 동작

다른 agent가 `docs/features/web-board-ux/mockups/`에 만드는 dashboard·로그인 화면 정적 HTML mockup 2~3개(이 ticket은 그 폴더를 만들거나 고치지 않는다) 중 사용자가 하나를 고른다(부분 조합 가능). 선택은 SPEC s2의 "트랙 C 선택" 절에 기록했고, C1·C2의 token 이름·구성 요소 목록·dashboard 배치·JSON endpoint·갱신 주기를 그 개정에서 확정한다.

## 수락 기준

- WBX-AC-18: mockup 2~3개가 있고 사용자의 선택이 기록된 뒤에만 WBX-C1이 시작된다.

## 계약과 맥락

BRIEF WBX-D12~D14·D18. **충족(2026-10-27): 사용자가 방향 A(차분한 중립 admin console, `mockups/a/`)를 골랐다. 기록: BRIEF WBX-D14, SPEC s2 "트랙 C 선택".** Mockup은 CDN·framework 없이 CSP를 지킬 수 있는 형태여야 한다(선택 때 확인).

## 검증

증거 수준 static(선택 기록). TDD 대상 아님.

## 범위 밖과 중단 조건

Mockup 제작. 선택 전 C1·C2 착수 금지.

## 출력

SPEC s2 "트랙 C 선택" 절.
