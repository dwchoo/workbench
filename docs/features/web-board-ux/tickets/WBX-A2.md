# WBX-A2: Cache 정책과 bfcache 복원 세션 확인

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-A2`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Opus(`model: "opus"`). Key 원문·원문 log 화면의 `no-store` 보장과 세션 검사에 닿는 보안 header 변경이다.

## 제공할 동작

SPEC "Cache 정책과 bfcache" 표대로 `middleware.security_headers` 규칙을 바꾼다(세션 있는 page의 HTML은 `private, no-cache`, 예외 `no-store` 유지). `/static/session-check.js`(로그인 화면 전부, `pageshow.persisted` → `/api/badge` → 401이면 `/login`), `/static/login-restore.js`(로그인 화면, persisted면 reload). Task-inbox USAGE §10에 "뒤로 가기마다 Chrome 경고" 항목(TLS가 근본 해결, "Always use secure connections" 영향).

## 수락 기준

- WBX-AC-02: 표의 모든 행을 route별로 확인(세션 page 200·4xx·stale form 403 → `private, no-cache`; `POST /settings/keys` 응답·`/raw`·`/full`·`/artifact` → `no-store`; login·redirect·JSON·REST·bare 오류 → `no-store`; static·favicon 불변).
- WBX-AC-03: 로그아웃·만료 뒤 복원 화면의 POST는 `303 /login` 또는 `stale_form_page` 403이고 아무것도 바뀌지 않음(서버 쪽). JS logic은 S8, 실제 bfcache 동작은 사용자 browser(S7) `pending`.
- WBX-AC-04(문서 부분): USAGE 항목 존재. 실제 Chrome 확인은 WBX-A5.

## 계약과 맥락

`web/middleware.py:security_headers`, `pages/login.py`(favicon cache 순서 유지), `render.layout`(script 포함), `docs/features/task-inbox/USAGE.md` §10. `/api/badge` 응답 형태 불변.

## 검증

Seam은 SPEC s2 "테스트 결정"에서 사용자가 확정한 것이다(2026-10-27, WBX-D20·D21).
Seam: S1(header), S8(`tests/hub/js/session_check.test.mjs`, `login_restore.test.mjs`: persisted일 때만 확인, 401 → `/login`, 오류 무시, login reload). 독립 기대값: SPEC cache 표.
Behavior 순서: (1) 세션 page `private, no-cache` (2) key 발급 응답 `no-store` (3) raw/full/artifact `no-store` (4) login·redirect·JSON `no-store` 유지 (5) static·favicon 유지 (6) session-check/login-restore script가 로그인·로그인 화면에 실림.
명령: node seam: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_board_state_js.py'`(새 `tests/hub/js/<name>.test.mjs`는 자동으로 포함, node 없으면 skip); `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_cache*.py'`; 회귀 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_*.py'`, `-p 'test_hub_web_*.py'`.
독립 test_designer 필수. 초점: key 원문이 든 응답이 하나라도 `no-store`가 아닌 경로(오류 page 포함), 세션 없는 응답이 `private` cache로 새지 않음.

## 범위 밖과 중단 조건

Meta refresh 제거(트랙 C까지 유지, WBX-D22). HSTS·TLS 동작 변경. `render.py` 자원 claim.

## 출력

변경 경로, route별 header 표(관측값), behavior별 RED/GREEN, 명령·결과.
