# WBX-E1: Backend ui_v1 공유 attach, 화면 fan-out, 크기·입력 규칙

명세: [../SPEC.md](../SPEC.md) s5 "트랙 E"의 "ui_v1 덧붙이기: 공유 attach"와 "시험". 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-E1`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Opus(`model: "opus"`). Backend 연결 수명(attach 자리와 공유 집합), 신원(Hub process 확인), 전달 순서(fan-out·replay·state), 보안 경계(같은 uid socket 위의 Hub 전용 권한)가 한 ticket에 걸리고, 기존 TUI를 하나도 바꾸지 않아야 한다. 독립 test_designer(Sonnet) 검증 필수.

## 제공할 동작

1. `contracts/ui_v1.py`: request type `share_attach`·`share_detach`(field 없음, 모르는 field 거절), reason `resize_owned_by_ui`. `VERSION`은 1 그대로.
2. `backend/ui_server.py`: primary `attached`와 따로 있는 공유 attach 집합. 받아들이는 조건은 `workbench-hub` 이름 + 같은 peer uid + `hub_peer` 확인(`_may_control_unattached` + `_is_hub_process`). 이미 primary·이미 공유인 연결의 거절, `MAX_SHARED_ATTACHMENTS = 4`(넘으면 `queue_full`), 공유 연결의 `attach` 거절.
3. 공유 attach가 보낼 수 있는 request(input, paste, resize, takeover_request/confirm, handoff, restart_pane, kill_pane, stop_command, share_detach)와 거절하는 것(`focus`, `detach` → `not_attached`). 처리는 primary와 같은 handler.
4. Fan-out: `broadcast`·`push_state`를 primary와 모든 공유 attach에. 연결별 `outbound` 한도 초과는 그 연결만 `slow_client`로 닫고 공유 집합에서 뺀다(primary·pane 무영향). `share_attach` 응답 = `result ok` + snapshot, 그 뒤 모든 pane replay, 그 뒤 live(primary attach와 같은 순서).
5. 크기: 공유 attach의 `resize`는 `pane` 필수, primary가 있으면 적용하지 않고 `resize_owned_by_ui`, 없으면 그 pane에 적용. 공유 attach는 attach 때 크기를 바꾸지 않는다. Primary의 `on_attach(size)`·`resize`는 그대로.
6. `backend/service.py`: 주기 state push를 primary나 공유 attach가 있을 때 보냄, snapshot에 `shared_attachments`(기존 `attached` 뜻 그대로).
7. 입력·host shell 주인: 공유 attach 입력이 같은 `admit` 경로·거절 이유를 쓰고, lone Ctrl+C 예외와 `command_interrupt` journal이 같다. 입력 주인은 backend에 하나(`takeover_*`/`handoff`는 누가 보내든 같은 상태를 바꾸고 모든 attach에 state push).
8. TUI 불변 확인: TUI(`workbench-ui`)가 snapshot의 새 key를 무시하고, 공유 attach가 있을 때 TUI attach·화면·입력·`Ctrl-]`·detach·다시 attach가 같다. 두 번째 primary는 `attached_elsewhere` 그대로.

## 수락 기준

WBX-AC-39(backend 공존 부분), 40, 41(backend 규칙), 42(admit 경로). 최종 통합은 WBX-E4.

## 계약과 맥락

현행: `contracts/ui_v1.py`(`ClientType`, `Reason`, `NON_ATTACHED_CONTROL_CLIENTS`, `parse_client_frame`), `backend/ui_server.py`(`UiServer.attached`, `_dispatch`, `_require_attached`, `_may_control_unattached`, `_is_hub_process`, `_send`의 `slow_client`, `broadcast`, `push_state`, `_deliver_stop_finals`, `close`), `backend/service.py`(`snapshot`의 `attached`, `on_attach`, `resize`, state push loop, `_command_interrupted`), `backend/panes.py`(`OmpPane.admit`, `ShellPane._admit_locked`·`_accepts_manual_input`·`_interrupt_run`, `info`의 `rows`·`cols`), `ui/product/model.py`(snapshot 읽기, backend focus 따라가기). BRIEF WBX-D59·D60·D61·D65, ADR-0002, command-stop SPEC, core DECISIONS C-D69(6)(b). Hub 쪽(중계·WebSocket)은 이 ticket 밖이다.

## 검증

Seam(SPEC s5 "시험", 새 seam 없음): 계약 seam(`tests/contracts/test_ui_v1*.py`), backend ui_server seam = Hub SPEC S5(`tests/backend/test_ui_server*.py`: 실제 `UiServer` + `FakeController` + 실제 `UiClient`, peer는 `peer_identity` 하위 클래스로 주입), `tests/backend/test_panes.py`, Hub SPEC S6(실제 backend + fake OMP, driver subprocess, `tests/hub/test_project_control.py`의 `ProjectSandbox`), Hub SPEC S8·기존 TUI 시험(TUI 불변).
독립 기대값: ui_v1 계약 문구와 SPEC s5 표, 받은 frame bytes, backend journal(`command_interrupt`), snapshot 값, `/proc`.
Behavior 순서(한 behavior씩 RED → GREEN):
(1) `share_attach`·`share_detach` parse·모르는 field 거절·`resize_owned_by_ui` (2) 조건: 이름·uid·Hub pid 각각 틀리면 `not_attached`, attach 자리 불변 (3) primary와 공존, 두 번째 primary `attached_elsewhere` 불변, 공유 연결의 `attach`·primary의 `share_attach` 거절, 4개 한도 (4) 응답 순서: result+snapshot → replay → live, 빠짐·중복 없음 (5) display·state fan-out(같은 bytes) (6) 공유 연결 overflow → 그 연결만 `slow_client`, primary 계속 받음 (7) `focus`·`detach` 거절, 허용 request가 primary와 같은 결과 (8) 크기: primary 있으면 `resize_owned_by_ui`·pane 크기 불변, 없으면 적용, `pane` 없음 거절 (9) `share_detach` 뒤 전송 없음 (10) state push 조건과 `shared_attachments` (11) 실제 pane: 공유 attach 입력의 host 거절 이유·lone Ctrl+C journal·OMP pane 입력 (12) `takeover_*`/`handoff`가 primary·공유 어느 쪽이든 같은 주인 상태와 모든 attach의 state (13) TUI가 새 snapshot key를 무시, TUI 동작 불변.
명령(공통 접두 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest`): `discover -s tests/contracts -p 'test_ui_v1*.py'`, `discover -s tests/backend -p 'test_ui_server*.py'`, `discover -s tests/backend -p 'test_panes.py'`, `discover -s tests/hub -p 'test_project_control*.py'`, `discover -s tests/ui -p 'test_product_model*.py'`, `discover -s tests/ui -p 'test_product_pty*.py'`; 회귀 `discover -s tests/backend -p 'test_command_stop*.py'`, `discover -s tests/hub -p 'test_hub_list*.py'`.
독립 test_designer(Sonnet) 필수. 초점: 이름만 맞는 같은 uid process, 다른 uid, Hub 아닌 pid의 공유 attach; 공유 attach가 primary 자리를 차지하거나 primary의 detach를 일으키는 경로; 공유 연결 overflow가 primary·pane을 끊는 경로; TUI attach 뒤 웹 resize가 이기는 경로; replay와 live 사이 빠짐·중복; lone Ctrl+C·거절 이유가 primary와 다른 경로; `focus`가 공유 attach에서 통하는 경로.
격리 실행 주의: Hub SPEC S6은 실제 backend·fake OMP를 띄운다. CLAUDE.md "테스트 실행"의 sandbox 규칙(짧은 private `XDG_RUNTIME_DIR`, temp HOME/XDG_STATE_HOME, `WORKBENCH_HUB_AUTOSTART=0`, 프로젝트의 OMP namespace dir만 계산해 사전 부재 확인·사후 삭제, 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l` 동일). Checkpoint·hash manifest exclude: `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시. 전후 `du -sh .workflow graphify-out .worktrees`를 보고한다.

## 범위 밖과 중단 조건

Hub 중계·WebSocket·동작 API·감사(WBX-E2), 화면(WBX-E3), 문서(WBX-E4), TUI 변경(하지 않는다), ui_v1 `VERSION` 올리기, backend journal에 client 구분 더하기. TUI가 snapshot의 새 key를 무시하지 않거나, 공유 attach를 위해 TUI·기존 `attach` 동작을 바꿔야 한다면 멈추고 보고한다. 기존 seam으로 잡을 수 없는 behavior가 나오면 새 seam을 만들지 말고 반환한다.

## 출력

변경 경로, behavior별 RED 실패 메시지와 GREEN 결과, 추가 type·reason·snapshot field 대조표, 공유 attach 조건별 결과 표, TUI 불변 증거, 명령·결과, 전후 `du`와 `/tmp/omp-workbench-<uid>` 개수.
