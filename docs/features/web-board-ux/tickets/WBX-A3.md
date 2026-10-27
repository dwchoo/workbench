# WBX-A3: 평문 HTTP 배너 접기

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-A3`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Sonnet(`model: "sonnet"`). 정형 UI·정적 JS다.

## 제공할 동작

SPEC "평문 HTTP 배너 접기": 배너 + header chip 동시 렌더, `<head>`의 `/static/insecure-banner.js`(localStorage `wb.insecureBanner`, `<html>` class `wb-insecure-collapsed`), 닫기·다시 펼치기, CSS로 "항상 하나는 보임". 로그인 화면 포함. `wb hub status` 경고 줄 불변.

## 수락 기준

- WBX-AC-05: 경고 조건이면 모든 page(로그인 포함)에 배너(닫기 버튼 `hidden`)와 chip(`title` = 경고 전문)이 있고, 조건이 없으면 둘 다 없다. CSS 규칙이 class 유무에 따라 정확히 하나를 보이게 한다(정적). 저장·복원 logic은 S8, 실제 browser의 새로 고침·저장소 삭제는 사용자 browser `pending`(I1).
- `wb hub status` 경고 문자열 불변(기존 테스트).

## 계약과 맥락

`web/render.py:layout`, `config.INSECURE_WARNING_TEXT`, `static/{insecure-banner.js,base.css}`. CSP 불변(inline 없음).

## 검증

Seam은 SPEC s2 "테스트 결정"에서 사용자가 확정한 것이다(2026-10-27, WBX-D20·D21).
Seam: S1+S6(markup), S8(`tests/hub/js/insecure_banner.test.mjs`: 저장 값 → class, 저장소 예외 시 펼침, 닫기·다시 펼치기), S5(CSS 정적). 독립 기대값: SPEC 절, BRIEF AC-05.
Behavior 순서: (1) 조건 참일 때 배너+chip (2) 조건 거짓일 때 없음 (3) chip `title` 전문 (4) script가 `<head>`에 동기로 실림·inline 없음 (5) CSS 상호 배타 규칙.
명령: node seam: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_board_state_js.py'`(새 `tests/hub/js/<name>.test.mjs`는 자동으로 포함, node 없으면 skip); `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_banner*.py'`; 회귀 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_*.py'`.

## 범위 밖과 중단 조건

경고 조건·문구 변경 금지. `render.py` 자원 claim.

## 출력

변경 경로, behavior별 RED/GREEN, 명령·결과.
