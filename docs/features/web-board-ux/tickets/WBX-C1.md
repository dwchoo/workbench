# WBX-C1: CSS design system과 전 화면 적용

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-C1`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Sonnet(`model: "sonnet"`). UI rendering과 CSS다.

## 제공할 동작

SPEC s2 "트랙 C 선택"대로 `docs/features/web-board-ux/mockups/a/style.css`를 출발점으로(token 이름 유지, mockup 전용 값은 정리하고 차이를 출력에 적음) 손으로 쓴 CSS: 색·간격·글자 token(CSS custom property), light·dark(`prefers-color-scheme`), table·card·badge·form·button 구성 요소. `base.css`·`app.css`·`board.css` 정리, 로그인 화면 포함 모든 화면의 markup class를 구성 요소에 맞춤. A1의 nav·A3의 배너 chip을 새 token으로 다시 그린다.

## 수락 기준

- WBX-AC-19: 모든 page가 같은 stylesheet 집합과 구성 요소 class를 쓴다(S1+S6 순회), dark 규칙 존재(S5), 외부 URL·`@import` 원격·font CDN 없음(S5), CSP 문자열 불변(S1). 시각은 사용자 browser `pending`.

## 계약과 맥락

선행: WBX-C0, WBX-A1, WBX-A2, WBX-A3(`render.py`·header 안정). CSP `style-src 'self'`(Board page nonce 예외 유지).

## 검증

Seam은 SPEC s2 "테스트 결정"에서 사용자가 확정한 것이다(2026-10-27, WBX-D20·D21).
Seam: S1+S6, S5. CSS 시각은 TDD 예외(S7).
명령: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_*.py'`.

## 범위 밖과 중단 조건

Dashboard 내용(C2). 새 JS. Mockup 선택 범위 밖 디자인 추가(WBX-D16).

## 출력

Token 목록, 변경 경로, 명령·결과, 화면 캡처는 사용자 확인 때.
