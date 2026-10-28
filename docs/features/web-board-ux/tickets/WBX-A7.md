# WBX-A7: 뒤로 가기로 다시 보인 화면을 세션 확인까지 가림

명세: [../SPEC.md](../SPEC.md) s3 "Dashboard 주소와 뒤로 가기"와 그 BRIEF r0.4 주석. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-A7`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Opus(`model: "opus"`). 세션 경계(로그아웃 뒤 화면)다.
기록 시점 상태: 완료(commit `1573ebc`, 2026-10-28). 이 파일은 이미 끝난 작업의 기록이다.

## 계기

WBX-A6 통합 뒤 사용자가 실제 Chrome에서 로그아웃 → 뒤로 가기를 하면 로그인 화면으로 가기 전에 이전 화면이 잠깐 보였다. 사용자가 고친 방식으로 Q39 a(세션 확인이 답할 때까지 화면을 가림)를 골랐다(BRIEF WBX-D36).

## 제공한 동작

1. `session-check.js`를 `<head>`에서 동기로 싣는다(`render.py`).
2. `back_forward` navigation과 bfcache 복원(`pageshow.persisted`)에서 `<html>`에 `wb-checking` class를 붙여 화면을 가린다(`base.css`).
3. `/api/badge` 답이 2xx이거나 그 밖의 오류이면, 또는 4 s timer가 끝나면 가림을 푼다. 401이면 가린 채 `location.replace("/login")`.
4. 일반 이동·새로 고침, 로그인 화면, JS가 없는 browser는 바뀌지 않는다.

## 수락 기준

- WBX-AC-03 보충 2(WBX-D36): 세션 없는 `back_forward`·persisted 복원에서 이전 내용이 보이지 않고 로그인 화면으로 간다. 세션 있음·오류·시간 초과에서는 화면이 보인다. 일반 navigation에는 가림이 없다.
- 실제 Chrome 재확인은 WBX-A5(사용자) `pending`.

## 검증

Seam: S1(`tests/hub/test_web_cache.py`, `test_web_cache_independent.py`: `<head>`의 동기 script, 화면별 포함), S8(`tests/hub/js/session_check.test.mjs`, `cache_independent.test.mjs`: 가림 class, 401·2xx·오류·timer, 중복 확인 없음).
명령: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_cache*.py'`, `… -p 'test_board_state_js.py'`.

## 범위 밖

`Clear-Site-Data`, 서버 쪽 로그아웃 동작, cache 정책 표 변경.

## 출력

변경 경로(`web/render.py`, `static/base.css`, `static/session-check.js`, 위 테스트), behavior별 RED/GREEN은 commit `1573ebc`의 작업 기록.
