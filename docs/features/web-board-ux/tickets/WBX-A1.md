# WBX-A1: 전역 nav와 Board 상태 점

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-A1`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Sonnet(`model: "sonnet"`). 기존 render 패턴을 따르는 UI rendering이다.

## 제공할 동작

SPEC "전역 nav" 그대로: 로그인한 모든 화면의 header에 대시보드·요청·새 요청 버튼·Board agent(상태 점)·설정 메뉴(`<details>`: 비밀번호/API key/감사 기록), 오른쪽 badge·로그아웃. `aria-current="page"` 강조, `max-width: 45rem`에서 checkbox toggle로 접힘(JS 없음). 로그인 화면·세션 없는 오류 page에는 nav 없음. `WebServices.board_state`(기본 `"off"`)를 더하고 Hub가 `BoardSet.status()`로 채운다. `render.layout(..., nav=NavContext)`와 각 page frame helper.

## 수락 기준

- WBX-AC-01: 모든 로그인 화면(`/`, `/projects/<id>`, `/requests*`, `/board`, `/settings*`)에 같은 nav 링크 집합. 현재 화면 링크에만 `aria-current`. `/board`·`/settings/keys`·`/settings/audit`·`/settings`는 어느 화면에서든 링크 1개 또는 summary+링크 2개 안. `/login`에는 nav 없음. 좁은 화면 접힘은 CSS·markup 정적 확인(시각은 I1·사용자 browser `pending`).
- 상태 점 값 `on`/`off`/`error`가 Board state 매핑대로.

## 계약과 맥락

`web/render.py:layout`, `pages/{login,settings,requests,projects,board,board_status,control}.py`의 frame helper, `web/app.py:WebServices`, `hub/server.py`(board_state 연결), `static/base.css`(nav 최소 스타일; 트랙 C가 나중에 다시 쓴다). 기존 `settings.settings_nav()`는 유지.

## 검증

Seam은 SPEC s2 "테스트 결정"에서 사용자가 확정한 것이다(2026-10-27, WBX-D20·D21).
Seam: S1 + S6(html.parser DOM). 독립 기대값: SPEC "전역 nav" 표와 BRIEF WBX-AC-01.
Behavior 순서: (1) 로그인 page에 nav 링크 집합 (2) 경로별 `aria-current` (3) 설정 `<details>` 하위 링크 (4) 세션 없는 화면에 nav 없음 (5) Board 상태 점 3값 (6) 좁은 화면 toggle markup.
명령: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_nav*.py'`; 회귀 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_*.py'`.
독립 test_designer(Sonnet) 필수. 초점: 모든 page route에서 nav 누락 없음(route 목록 기준 순회), 사용자 텍스트가 nav에 섞이지 않음.

## 범위 밖과 중단 조건

디자인 token·색(트랙 C), dashboard. `render.py`는 자원 `file:web/render.py`를 잡은 동안만 고친다. Page 응답 형태 외 동작 변경이 필요하면 멈추고 보고.

## 출력

변경 경로, behavior별 RED/GREEN, 명령·결과, nav markup 예.
