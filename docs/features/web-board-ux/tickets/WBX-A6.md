# WBX-A6: `/dashboard` 이동과 로그아웃 뒤 뒤로 가기

명세: [../SPEC.md](../SPEC.md) s3. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-A6`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Opus(`model: "opus"`). 세션 경계(로그아웃 뒤 화면)와 cache·bfcache·redirect가 여러 module에 걸친다.

## 제공할 동작

SPEC "Dashboard 주소와 뒤로 가기"(WBX-D29·D34):
1. Dashboard handler를 `GET /dashboard`로 옮긴다(`render.HOME_PATH`). `GET /`는 세션이 있으면 `303 /dashboard`, 없으면 기존 `303 /login`.
2. 로그인 성공 뒤 이동, s2까지 `/`로 보내던 서버 redirect, 돌아갈 곳 fallback(`middleware`), brand·nav 대시보드 링크, 프로젝트 화면의 "프로젝트 상태" 링크를 `/dashboard`로. `aria-current`는 `/dashboard`(정확히)·`/projects/…`.
3. Route audit에 `/dashboard`(page)와 `/`(redirect) pin. Cache: `/`는 redirect `no-store`, `/dashboard`는 `private, no-cache`.
4. `session-check.js`: `pageshow.persisted` 또는 navigation type `back_forward`에서 `/api/badge` 확인, 401이면 `location.replace("/login")`. `Clear-Site-Data`는 쓰지 않는다.
5. bfcache 적격성: `dashboard.js`·`badge.js`(와 timer·fetch가 있는 화면 JS)가 `pagehide`에서 진행 중인 fetch를 abort하고 timer를 멈추며 persisted `pageshow`에서 다시 시작한다. `AbortError`는 오류로 보이지 않는다.
6. 진단(seam 아님): headless Chrome DevTools로 `/dashboard`·`/requests`의 bfcache 미복원 이유(`Page.backForwardCacheNotUsed` 또는 `notRestoredReasons`)를 수정 전후로 기록한다. Chrome이 없으면 생략하고 적는다.

## 수락 기준

- WBX-AC-21: 로그인한 `GET /` → 303 `Location: /dashboard`; 세션 없는 `/` → 303 `/login`; `POST /login` 성공 → 303 `/dashboard`; `GET /dashboard`가 dashboard(요약 card 등, WBX-AC-20 그대로); brand·nav 대시보드 링크 `href="/dashboard"`; `/dashboard`·`/projects/<id>`에서만 대시보드 `aria-current`; Referer 없는 fallback이 `/dashboard`; `GET /api/dashboard` 응답 불변.
- WBX-AC-03(보충): `back_forward` navigation에서 세션 없음 → `/login`, 세션 있음·네트워크 오류 → 아무것도 하지 않음, 일반 navigation(`navigate`·`reload`)에서는 확인하지 않음, 같은 문서에서 중복 확인 없음. 기존 persisted 동작 유지.
- bfcache 정리: `pagehide` 뒤 진행 중 fetch abort·timer 없음, persisted `pageshow` 뒤 다시 갱신 시작, abort가 오류 표시를 만들지 않음.
- 실제 Chrome의 로그아웃 → 뒤로 가기, `/dashboard` 뒤로 가기 경고는 WBX-A5 재확인(사용자) `pending`.

## 계약과 맥락

`web/render.py`(`HOME_PATH`, nav), `web/pages/login.py`(`/`·`/dashboard`·로그인 redirect), `web/middleware.py`(돌아갈 곳 fallback), `pages/{dashboard,projects,board_status}.py`, `static/{session-check,dashboard,badge}.js`, `tests/hub/test_web_route_audit.py`. 기존 계약: SPEC "전역 nav", "Cache 정책과 bfcache"(s3 보충), 트랙 C dashboard(WBX-C2, `GET /api/dashboard` 10 s). `/api/badge` 응답 형태 불변. 작업 트리에 이 ticket 범위의 미commit 변경이 있을 수 있다: 시작 전 `git status`로 baseline을 대조하고 다른 변경을 덮지 않는다.

## 검증

Seam은 SPEC s2·s3 "테스트 결정"의 확정 seam이다(2026-10-27, WBX-D20·D21; s3에서 새 seam 없음).
Seam: S1(route·redirect·cache header·route audit), S6(nav DOM: brand·링크 `href`·`aria-current`), S8(`tests/hub/js/session_check.test.mjs`, `dashboard.test.mjs`, badge용 `.test.mjs`: stub `performance.getEntriesByType`, `AbortController`, timer). 실제 browser는 S7(WBX-A5). DevTools 진단은 seam이 아니다.
독립 기대값: SPEC "Dashboard 주소와 뒤로 가기" 절, BRIEF WBX-AC-21·AC-03 보충.
Behavior 순서: (1) `GET /dashboard`가 dashboard (2) 로그인한 `/` → 303 `/dashboard` (3) 세션 없는 `/` → `/login` (4) 로그인 성공 → `/dashboard` (5) fallback·프로젝트 링크 (6) nav·brand `href`와 `aria-current` (7) cache header(`/` no-store, `/dashboard` private no-cache) (8) route audit pin (9) session-check `back_forward` 401 → `/login` (10) `navigate`·`reload`에서는 확인 없음, 중복 없음 (11) dashboard.js pagehide abort·timer 정지·persisted 재시작 (12) badge.js 같은 정리 (13) AbortError 무표시.
명령: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_dashboard*.py'`, `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_nav*.py'`, `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_cache*.py'`, `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_route_audit.py'`; node seam `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_board_state_js.py'`(node 없으면 skip); 회귀 `-p 'test_web_*.py'`, `-p 'test_hub_web_*.py'`.
독립 test_designer(Sonnet) 필수. 초점: 세션 없는 응답이 `/dashboard`·`private` cache로 새지 않음, `/`가 page로 남은 경로 없음, 로그아웃 뒤 `back_forward`로 그린 화면의 POST는 여전히 `303 /login`·`stale_form_page`, 다른 origin `Referer`가 fallback을 바꾸지 않음.

## 범위 밖과 중단 조건

HTTPS-First 경고 자체(서버가 막을 수 없음, AC-04는 A5·TLS 안내). `Clear-Site-Data`. Dashboard 내용·JSON 형태 변경(WBX-C2 계약). 새 세션 endpoint. `render.py`는 자원 claim 동안만 고친다. 진단이 서버 header 변경 없이는 bfcache 적격이 될 수 없다고 보이면(예: `Cache-Control` 표 변경 필요) 고치지 말고 근거와 함께 멈춘다.

## 출력

변경 경로, behavior별 RED/GREEN, route·header 관측 표, DevTools 진단 전후 기록(또는 생략 사유), 명령·결과.
