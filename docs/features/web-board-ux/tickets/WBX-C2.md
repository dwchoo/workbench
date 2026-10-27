# WBX-C2: Dashboard `/`와 JSON 갱신

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-C2`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Sonnet(`model: "sonnet"`). 세션 JSON route와 page·정적 JS다.

## 제공할 동작

SPEC s2 "트랙 C 선택"과 `mockups/a/dashboard.html` 배치대로. `/`를 dashboard로: 요약 card(실행 중·꺼진 프로젝트 수; NEW·REVIEWING·NEEDS_INFO·RUNNING 수; "내 답이 필요한 요청" = NEEDS_INFO + 재시도 한도 소진 뒤 소유자 결정 대기(P2-AC-27), WBX-D18), 프로젝트 표(상태, 대기 사유, 검토 방식, 바로가기; HB-D09 항목 유지), 최근 요청 10개, Board agent 상태 card와 "열기". 10초 meta refresh 제거. 세션 JSON endpoint `GET /api/dashboard`(`api` 표시, `no-store`)와 `static/dashboard.js`가 숫자·표를 갱신(주기 10 s, 401이면 `/login`). Route audit pin.

## 수락 기준

- WBX-AC-20: `/` HTML에 네 구역, meta refresh 없음, JSON endpoint 값이 inbox·Hub 목록과 일치(독립 계산), 세션 없으면 `/`는 `303 /login`·JSON은 401. JS 갱신 logic은 S8(`tests/hub/js/dashboard.test.mjs`), 실제 browser는 `pending`.

## 계약과 맥락

선행: WBX-C1. `pages/board_status.py`, `pages/requests.py`(집계), `presentation`, `web/app.py`, route audit.

## 검증

Seam은 SPEC s2 "테스트 결정"에서 사용자가 확정한 것이다(2026-10-27, WBX-D20·D21).
Seam: S1+S6, S5, S8. "내 답이 필요한 요청"의 독립 기대값은 테스트가 store에 직접 만든 요청 상태.
명령: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_dashboard*.py'`, route audit, `-p 'test_web_*.py'`.

## 범위 밖과 중단 조건

"내 답이 필요한 요청" 알림(WBX-D16 제안, 미승인).

## 출력

변경 경로, JSON 예, behavior별 RED/GREEN, 명령·결과.
