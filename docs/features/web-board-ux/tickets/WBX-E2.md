# WBX-E2: Hub pane 중계, WebSocket endpoint, pane 동작 API, 감사

명세: [../SPEC.md](../SPEC.md) s5 "트랙 E"의 "Hub 중계: PaneRelay"(수명, 화면 중계, pane view, 입력, 크기 주인, pane 동작 API, WebSocket endpoint, `GET /api/board`의 `projects`, 감사, 보안, 실패와 경계 동작)와 "시험". 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-E2`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Opus(`model: "opus"`). 프로젝트당 연결 하나를 여러 웹 연결이 나누는 중계(전달 순서, replay·reset, backpressure), backend 신원 확인, 크기 주인 판정, 다시 붙기 수명, 세션·CSRF 경계가 한 ticket에 걸린다. 독립 test_designer(Sonnet) 검증 필수.

## 제공할 동작

1. `hub/board/panes.py`(새) `PaneRelay`: 프로젝트당 하나, asyncio Unix socket ui_v1 client(event loop를 막지 않음). 열기(신원 확인 → hello `workbench-hub` → welcome `backend_pid` 확인 → `share_attach`), 준비(snapshot, pane별 ring 256 KiB), 닫기(마지막 웹 연결 뒤 30 s, Hub 정지 때 즉시).
2. 화면 중계: pane 분배, `(session_id, generation)`·`sequence` 판정과 `reset`, 새 `share_attach`마다 `reset` + replay, 트랙 D `_attach`처럼 한 단계 attach(replay → live 빠짐·중복 없음), 연결별 송신 queue 4 MiB(4003).
3. Pane view(SPEC 표)와 `state` message, `GET /api/board`의 `projects`(backend에 연결하지 않음, `power`는 dashboard 값).
4. 입력: binary frame 하나 = `input` 하나(합치지·나누지 않음), 도착 순서 pipelining, `id` → 연결 map, `input_refused`(reason별 고정 문장 표), 미응답 상한(64 request / 1 MiB)과 socket 읽기 멈춤, `running`이 아니면 보내지 않음.
5. 크기 주인: `ui_attached`면 `resize`를 보내지 않고 `size`만 알림, 아니면 트랙 D 크기 주인 규칙을 pane마다, `resize_owned_by_ui` 버림.
6. Pane 동작 API `POST /api/board/panes/{id}/{takeover_request,takeover_confirm,handoff,stop,restart,kill}`: SPEC 표의 body·pane 제한·성공·오류 code, `stop`의 202와 WebSocket `notice` 최종 결과, 10 s 대기와 504, relay가 닫혀 있으면 열기.
7. `GET /ws/board/{id}`의 pane id 분기: id 판정·registry 확인(upgrade 전 404), pane당 8 연결(4429), `reset`·`input_refused`·`notice` message, 등록 지움 → `gone` 4404. 트랙 D의 hello·heartbeat·세션·queue·close code를 그대로 쓴다.
8. 감사 event 표(`pane_attach`·`pane_release`·`pane_timeout`·`pane_closed`·`pane_denied`·`pane_takeover_request`·`pane_takeover_confirm`·`pane_handoff`·`pane_stop`·`pane_restart`·`pane_kill`). 입력 내용·byte 수·입력 거절·resize는 남기지 않는다.
9. 실패: `off`, `unreachable`과 backoff(≤ 15 s, 공유 attach 거절은 ≤ 60 s), `slow_client`로 닫힘 → 다시 붙기, `too_old`(다시 시도 안 함, 새 hello·60 s에 한 번 확인, backend 재시작 안 함), Hub 재시작, 등록 지움.
10. Route audit pin(`/api/board/panes/*`, pane id `/ws/board/*`가 세션 전용, `/api/v1/`·내부 listener에 pane 동작 없음).

## 수락 기준

WBX-AC-39(중계·WebSocket), 41(Hub 크기 주인), 43(API·결과 전달), 44, 46, 47. 화면 표시는 WBX-E3, 최종 통합은 WBX-E4.

## 계약과 맥락

선행: WBX-E1(공유 attach 계약), 트랙 D 통합(D1의 `hub/board/ws.py` v2·session API·감사 helper, D2의 화면). 현행: `hub/board/ws.py`(v2 연결, `_attach`, `_paced`, 크기 주인, close code), `web/pages/board_api.py`(session API 모양·오류 code), `web/app.py`(route), `hub/inbox/backend_port.py`(신원 확인·`workbench-hub`), `backend/process_identity.py`(`inspect_backend`), `hub/registry.py`, dashboard의 프로젝트 상태 값, 감사 helper. BRIEF WBX-D60~D63·D65~D67. Hub는 실행 중 backend를 재시작하지 않는다.

## 검증

Seam(SPEC s5 "시험", 새 seam 없음): task-inbox T6(`ProjectSandbox`: 실제 Hub + 실제 backend + fake OMP) + `tests/hub/support/ws_client.py`의 `Tab` 여러 개(TUI 대역은 test가 연 실제 `UiClient(name="workbench-ui")` primary), Hub SPEC S4 계약 fixture backend(`tests/hub/fixtures/contract_backend.py`: 이전 판 `unsupported_type`, 신원 불일치), T5 형태의 in-process relay 시험(필요하면 같은 fixture), S1 + S6(route·CSRF·Bearer·route audit·id 404·이름 escape).
독립 기대값: SPEC s5 표와 close code, `sqlite3`로 읽은 `audit`, fake OMP가 기록한 입력 bytes와 순서, backend journal, snapshot, `/proc`.
Behavior 순서(한 behavior씩 RED → GREEN):
(1) pane id 판정·registry 확인·upgrade 전 404, Bearer 401, CSRF (2) relay 열기: 신원 확인·welcome pid·`share_attach`, 실패 시 `unreachable` (3) 두 웹 연결 같은 replay·live, replay와 live 사이 빠짐·중복 없음 (4) pane 재시작·새 backend의 `reset`, 새 `share_attach`의 `reset` + replay (5) 입력: frame 하나 = request 하나, 도착 순서, lone `\x03` 유지 (6) `input_refused` 전달(host 거절 이유), 꺼짐·`running` 아님 (7) 미응답 상한과 읽기 멈춤 (8) 크기: TUI 대역이 붙어 있으면 `resize` 없음·`size` 알림, 없으면 크기 주인 규칙, `resize_owned_by_ui` 버림 (9) 동작 API: takeover_request/confirm/handoff(host만, `wrong_pane`), restart, kill(`confirm_required`), stop(202 + `notice` 최종), `backend_refused` + reason, `terminal_busy`, 504 (10) 감사 행과 detail, 입력 내용 없음 (11) pane당 9번째 4429, 느린 웹 연결 4003이 다른 연결·relay를 끊지 않음 (12) 마지막 연결 뒤 30 s 닫기·그 안 재연결은 재attach 없음 (13) backend 재시작 → backoff 다시 붙기 (14) 이전 판 backend `too_old`·다시 시도 규칙 (15) 등록 지움 → `gone` 4404 (16) `GET /api/board`의 `projects` (17) route audit pin.
명령(공통 접두 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest`): `discover -s tests/hub -p 'test_board_pane*.py'`, `discover -s tests/hub -p 'test_board_ws*.py'`, `discover -s tests/hub -p 'test_board_api*.py'`(있으면), `discover -s tests/hub -p 'test_web_route_audit.py'`; 회귀 `discover -s tests/hub -p 'test_board_*.py'`, `discover -s tests/hub -p 'test_hub_list*.py'`, `discover -s tests/hub -p 'test_inbox_flow*.py'`.
독립 test_designer(Sonnet) 필수. 초점: replay와 live 사이 빠짐·중복, `reset` 없이 다른 session bytes가 이어지는 경로, 입력 순서 뒤바뀜·합쳐짐(lone Ctrl+C), 한 웹 연결의 overflow가 relay나 다른 연결을 끊는 경로, TUI가 있을 때 Hub가 `resize`를 보내는 경로, 감사에 입력이 남는 경로, Bearer·cross-origin·다른 세션 없는 연결로 pane에 닿는 경로, 등록되지 않은 id로 relay가 열리는 경로, Hub 정지 뒤 남는 연결.
격리 실행 주의: T6은 실제 Hub·backend·fake OMP를 띄운다. CLAUDE.md sandbox 규칙(짧은 private `XDG_RUNTIME_DIR`, temp HOME/XDG_STATE_HOME, `WORKBENCH_HUB_AUTOSTART=0`, 프로젝트의 OMP namespace dir만 계산·사후 삭제, 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l` 동일). Checkpoint·hash manifest exclude: `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시. 전후 `du -sh .workflow graphify-out .worktrees`를 보고한다.

## 범위 밖과 중단 조건

Backend 계약 변경(WBX-E1의 계약을 바꿔야 하면 멈추고 보고), 화면·버튼·panel(WBX-E3), USAGE(WBX-E4), backend 자동 재시작, keystroke 감사. 실제 curses TUI와 웹 WebSocket을 한 시험에서 함께 써야 한다고 판단하면 쓰기 전에 반환한다(BRIEF WBX-O10, 사용자 확인 대상). 기존 seam으로 잡을 수 없는 behavior가 나오면 새 seam을 만들지 말고 반환한다.

## 출력

변경 경로, behavior별 RED 실패 메시지와 GREEN 결과, pane view 예, API·close code 대조표, `input_refused` 문장 표, 감사 행 예(입력 내용 없음 확인), 실패 시나리오별 widget 상태, 명령·결과, 전후 `du`와 `/tmp/omp-workbench-<uid>` 개수.
