# WBX-E3: Pane widget, 버튼, panel "프로젝트" 묶음, 프로젝트 화면 링크

명세: [../SPEC.md](../SPEC.md) s5 "트랙 E"의 "화면", WebSocket endpoint의 client message, pane view의 `actions`. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-E3`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Sonnet(`model: "sonnet"`). 트랙 D workspace 화면의 기존 패턴(widget, panel, `<dialog>`, 순수 logic + S8, CSP)을 따르는 화면 작업이다.

## 제공할 동작

1. Panel "프로젝트" 묶음: `GET /api/board`의 `projects`마다 이름(`textContent`)·켜짐 점·"manager"·"worker"·"host" 추가 버튼. 이미 보이는 widget이면 focus. 추가는 이 브라우저 배치에만.
2. Pane widget: 제목 "<프로젝트 이름> · manager"(worker, host), 상태 점, "다른 화면 N개 연결됨", TUI가 붙어 있으면 "TUI가 크기를 정함", 숨기기·×(감추기만, 확인 없음), `off`·`unreachable`·`too_old`·`unavailable`의 `message`와 `/projects/<project_id>` 링크.
3. 버튼(`actions`에 있을 때만 활성): host 인수 요청·인수 확인·넘기기·명령 중지(`<dialog>`, 명령 이름·owner)·강제 종료(`<dialog>`), 끝난 pane 재시작. CSRF header, 결과·거절 이유·`notice`(명령 중지 최종 결과)를 상태 줄에.
4. 입력: `static/board-pane.js`(새, 순수 logic) — manager·worker의 Ctrl-d 두 번 누르기(TUI `_send_input`와 같은 규칙과 안내 문장, 2 s, `ESC \x04`도 한 key, 다른 key·붙여넣기·focus 이동은 잡아 둔 것을 버림), host는 그대로, `input_refused` 문장 표시. `Ctrl-]` prefix 없음.
5. 크기: `size`·`ui_attached`에 따라 xterm을 서버 크기로 맞추고 letterbox/scroll, `resize`는 자기 layout 변화에서만(트랙 D client 규칙). `reset` → xterm 지우기.
6. 배치: `wb.board.layout`에 pane id, 맞추기(없어진 프로젝트 항목 버림, 자동 배치에 pane 넣지 않음), 좁은 화면 선택 목록에 pane widget.
7. 프로젝트 화면 `GET /projects/{project_id}`에 "작업 공간에서 열기" 링크 `/board#open=<project_id>`, `/board` JS의 fragment 처리(검사 → 세 widget 더하기·보이기 → manager focus → fragment 지우기).

## 수락 기준

WBX-AC-41(letterbox), 42(Ctrl-d·거절 표시), 43(버튼·dialog), 45, 46(표시). 최종 통합은 WBX-E4.

## 계약과 맥락

선행: WBX-E2(pane view·WebSocket message·동작 API), 트랙 D 화면(D2의 `board.js`·`board-layout.js`·`board-panel.js`·`board.css`·`pages/board.py`). 현행 패턴: 트랙 D "화면: workspace와 panel", `static/board-state.js`(v2 상태 기계), `ui/product/model.py`(`CTRL_D_CONFIRM_SECONDS`, `CTRL_D_NOTICE`, `_send_input`, `stop_confirm_lines`). BRIEF WBX-D60~D62·D64. CSP `script-src 'self'`, inline 없음, 새 library 없음.

## 검증

Seam(새 seam 없음): S8(`tests/hub/test_board_state_js.py` → `tests/hub/js/*.test.mjs`: `board-pane.js`, panel 프로젝트 묶음 상태, 배치 맞추기, `#open=` parse), S1 + S6(`/board` 틀, 프로젝트 화면 링크, 이름 escape), S5(외부 URL·inline script·library 없음). 실제 browser는 S7(WBX-O09, E4에서 `pending`).
독립 기대값: SPEC s5 화면 절, pane view `actions` 표, TUI의 Ctrl-d 규칙·안내 문장.
Behavior 순서(한 behavior씩 RED → GREEN):
(1) Ctrl-d 규칙(첫 번째 보류·안내, 2 s 안 두 번째 전달, 다른 key·붙여넣기·focus 이동이 버림, `ESC \x04` 한 key, host는 그대로) (2) `actions` → 버튼 활성·비활성 (3) 동작 결과·`backend_refused`·504·`notice` 표시 (4) `size`·`ui_attached` → letterbox 판정, `size`에 반응해 `resize` 안 보냄 (5) `reset` 처리 (6) panel 프로젝트 묶음(이름·켜짐·추가, 있으면 focus) (7) 배치 맞추기(pane id, 없어진 프로젝트, 자동 배치 제외) (8) `#open=` parse·검사·세 widget·fragment 지우기 (9) 프로젝트 화면 링크(S1·S6) (10) × 는 감추기만(동작 API 호출 없음) (11) CSP·외부 URL 없음(S5).
명령(공통 접두 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest`): `discover -s tests/hub -p 'test_board_state_js.py'`, `discover -s tests/hub -p 'test_web_pages_board*.py'`, `discover -s tests/hub -p 'test_web_pages_projects*.py'`, `discover -s tests/hub -p 'test_web_board_csp*.py'`.

## 범위 밖과 중단 조건

Hub·backend 동작(차이를 찾으면 E2·E1 계약 차이로 보고), USAGE(WBX-E4), 단축키, backend focus. 기존 seam으로 잡을 수 없는 behavior가 나오면 새 seam을 만들지 말고 반환한다.

## 출력

변경 경로, behavior별 RED 실패 메시지와 GREEN 결과, 버튼·dialog 문구 표, Ctrl-d 규칙 대조(TUI와), 명령·결과, S7에서 사용자가 볼 목록.
