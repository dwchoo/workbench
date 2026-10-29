# WBX-A8: Board terminal 한글 IME 입력

명세: [../SPEC.md](../SPEC.md) s4 "Board terminal 한글 IME". 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-A8`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: orchestrator가 배정(2026-10-29 진행 중). 결정: BRIEF WBX-D41.
기록 시점 상태: 진행 중(2026-10-29). 작업 트리에 commit 전 후보 수정이 있다(`static/board.css`, `tests/hub/test_web_pages_board.py`).

## 증상

사용자 관찰(2026-10-29, 실제 Chrome, `/board`): terminal에서 한글 IME로 "안녕하세요"를 치면 "녕녕녕녕녕안."이 들어간다.

## 제공할 동작

한글 IME 입력이 친 그대로 terminal의 program에 도착한다. 영문 입력·붙여넣기·`onBinary` 경로와 `/board` CSP는 바뀌지 않는다. 원인과 고치는 곳은 이 ticket이 정한다. 후보 진단(작업 트리): `base.css`의 `textarea { max-width: 100% }`가 xterm의 helper textarea를 폭 0으로 묶어 Chrome이 조합 중인 음절을 caret 0에서 확정한다. 후보 수정은 `board.css`의 `.xterm .xterm-helper-textarea { max-width: none; }`.

## 수락 기준

- WBX-AC-24.

## 검증

Seam: S5(정적 CSS 검사, `tests/hub/test_web_pages_board.py`), JS를 바꾸면 S8(`tests/hub/js/*.test.mjs`). 진단에 headless Chrome CDP(`Input.imeSetComposition`·`insertText`)를 쓸 수 있으나 진단일 뿐 seam이 아니다(WBX-D34 선례).
명령: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_pages_board*.py'`, JS를 바꾸면 `… -p 'test_board_state_js.py'`.
마지막 확인: 사용자의 실제 Chrome·실제 한글 IME로 agent·shell terminal에 "안녕하세요"(와 받침·겹받침이 있는 문장)를 입력해 본다(S7). 사용자 확인 전에는 AC-24를 `pending`으로 둔다.

## 범위 밖과 중단 조건

트랙 D 화면 구조, xterm 판 올리기, 새 library. CSS로 고쳐지지 않고 xterm 내부 수정이 필요하면 멈추고 보고한다.

## 출력

원인(관측 근거), 변경 경로, behavior별 RED/GREEN, 사용자 IME 확인 결과(사용자가 기록).
