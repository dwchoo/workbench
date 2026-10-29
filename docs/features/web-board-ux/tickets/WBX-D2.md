# WBX-D2: Board workspace 화면(격자 widget, panel, 브라우저별 기억, 좁은 화면)

명세: [../SPEC.md](../SPEC.md) s4 "트랙 D: Board workspace"의 "화면: workspace와 panel"과 WebSocket v2의 client 규칙. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-D2`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Sonnet(`model: "sonnet"`). D1이 정한 server 계약과 protocol(`board-state.js` v2) 위의 UI rendering이다.

## 제공할 동작

1. `/board`를 panel + 격자 workspace로(WBX-D42·D43): 기존 tab UI 제거, widget(제목 막대: 상태 점·이름·"다른 화면 N개 연결됨"·숨기기·닫기 ×, 본문 xterm), 멈춘 agent의 "중지됨 — 시작", `exited`·`failed`의 "재시작".
2. 손으로 쓴 끌기·크기 조절(WBX-D52): 제목 막대 pointer 끌기, 오른쪽 아래 손잡이, 칸 맞춤, 겹침 미리 보기와 되돌리기(WBX-D57). 새 library·외부 URL 없음, 위치는 CSSOM.
3. `static/board-layout.js`(순수): 칸 맞춤, 겹침, 자동 배치, 이동·크기 검사, 저장 값 parse/검사/serialize, 목록과 맞추기(저장 없음 → 전부 보임, 저장 있음 → 새 id 숨김, 없어진 id 버림), 좁은 화면 선택.
4. 브라우저 저장(WBX-D50·D49): `wb.board.layout`, `wb.board.panel`, `wb.board.narrow`(모두 try/catch, 실패 시 기본값).
5. Panel(WBX-D49): agent·shell 행의 동작, 새로 만들기(한도 표시), 이름 바꾸기(제자리 입력), 배치 초기화, 한도 설정, 확인 `<dialog>`(SPEC 문구). `GET /api/board` 5 s 갱신과 동작 뒤 갱신, `POST /api/board/open`(page 열 때 보이는 id).
6. 연결: 보이는 widget마다 `board-state.js` v2 하나, hello 전 동시 4개, 숨기면 `release`. 크기: 자기 layout 변화에서만 150 ms debounce `resize`, 서버 `size`면 xterm을 그 크기로 하고 letterbox/scroll(WBX-D54).
7. Focus(WBX-D53): 클릭한 widget에 강조 테두리와 `term.focus()`, 단축키 없음.
8. 좁은 화면(WBX-D51): 720 px 미만에서 격자 없이 panel이 고른 terminal 하나만 전체로 보이고 연결.
9. `pagehide`·`pageshow` 규칙(WBX-D34), 401 → `/login`, 이름은 `textContent`만.

## 수락 기준

WBX-AC-25, 26, 35, 36, 그리고 AC-30(panel·widget 제목에 같은 이름)·AC-32("다른 화면 N개 연결됨", 4429 안내)·AC-33(letterbox/scroll)의 화면 쪽.

## 계약과 맥락

선행: WBX-A8(IME, 같은 static 파일), WBX-D1(session API, WebSocket v2, `board-state.js` v2, terminal view). 현행 `static/board.js`(DOM·socket glue만), `board-state.js`(protocol), `style-nonce.js`(xterm `<style>` nonce), `pages/board.py`, `board.css`, `/board` CSP(2026-10-26 결정). Design token은 트랙 C(`base.css`·`app.css`, mockup A)를 따른다. BRIEF WBX-D42~D53, D56·D57.

## 검증

Seam(SPEC s4 "시험", 새 seam 없음): S8(`tests/hub/js/board_layout*.test.mjs` 등 새 node test 파일을 기존 `test_board_state_js.py`가 실행; 순수 함수 + stub `localStorage`·`matchMedia`·`fetch`), S1·S6(page 틀: panel·workspace·script 목록·CSP·JS 없음 안내), S5(외부 URL·inline script·vendored library 추가 없음). 실제 끌기 감각·두 기기·좁은 화면은 S7(사용자, WBX-O06).
독립 기대값: SPEC s4 "화면" 절의 규칙(칸 크기, 최소 크기, 겹침 되돌리기, 맞추기 규칙, 저장 형식, 720 px 경계, debounce, 확인 문구).
Behavior 순서: (1) 칸 맞춤 (2) 겹침 판정 (3) 자동 배치 (4) 이동·크기 검사와 되돌리기 (5) 저장 parse/검사(잘못된 값 버림)/serialize, 저장소 예외 (6) 목록과 맞추기 3규칙 (7) 좁은 화면 선택과 복귀 (8) panel 상태(한도 비활성, 이름 입력 Enter/Esc, 오류 문구) (9) 확인 dialog를 거쳐야 stop·delete·close·restart 호출 (10) resize debounce와 `size` 무반응 (11) "다른 화면 N개" 문구(N = clients − 1, 0이면 없음) (12) page 틀·CSP·script 목록(S1·S6) (13) 정적 검사(S5).
명령(공통 접두 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest`): `discover -s tests/hub -p 'test_board_state_js.py'`, `discover -s tests/hub -p 'test_web_pages_board*.py'`, `discover -s tests/hub -p 'test_web_board_csp*.py'`; 회귀 `discover -s tests/hub -p 'test_board_ws*.py'`, `discover -s tests/hub -p 'test_web_*.py'`.
독립 test_designer(Sonnet) 필수. 초점: 겹침 경계(맞닿음은 겹침 아님), 손상된 저장 값, 연결 수가 보이는 widget 수를 넘지 않음, 숨긴 widget의 연결 해제, 이름의 HTML 해석 없음.

## 범위 밖과 중단 조건

Server 계약 변경(필요하면 멈추고 D1 계약 차이로 보고), 단축키, 끌기 library, 배치의 Hub 저장(브라우저별, WBX-D50). xterm DOM renderer가 CSP 안에서 동적 크기 변경을 못 하면 멈추고 보고한다.

## 출력

변경 경로, 정한 격자 값(SPEC 값과 다르면 이유), behavior별 RED/GREEN, 화면 설명(사용자 S7 확인용 체크 목록: 끌기, 크기, 겹침, 새로 고침 뒤 유지, panel 접기 기억, 좁은 화면, 두 기기 "N개 연결됨"·크기 따름), 명령·결과.
