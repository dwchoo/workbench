# WBX-D1: Board terminal registry, 여러 agent·shell, agent별 신원, 공유 연결(server)

명세: [../SPEC.md](../SPEC.md) s4 "트랙 D: Board workspace"(데이터 모델, agent별 home·메모리·system key, shell, session API, WebSocket v2, Hub 상태·정지·nav, 감사, 이행). 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-D1`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Opus(`model: "opus"`). Process 수명(여러 PTY session의 시작·종료), 신원(agent별 system key, 내부 listener 판정, 출처), 전달 순서(여러 연결의 입력 순서·replay와 live 사이), 삭제의 symlink 경계가 한 ticket에 걸린다.

## 제공할 동작

1. Terminal registry: `<hub state>/board-agents/registry.json`(SPEC 저장 규칙), 메모리 shell 목록, terminal view, 이름 규칙·기본 이름(WBX-D47·D48), 한도(agent 1~8 기본 4, shell 1~32 기본 10, WBX-D44·D46), `registry_invalid`.
2. `BoardSet`을 고정 channel 둘에서 terminal N개로: agent 만들기·시작·중지·재시작·삭제, shell 만들기·재시작·닫기, `POST /api/board/open`의 첫 shell 규칙과 `not_started` agent 시작(WBX-D56), terminal별 lock + registry lock.
3. Agent별 home(`board-agent/` 이행분, 새 것은 `board-agents/<id>/`), `--continue`, `omp-logs`, 공유 role prompt·model·격리 확인. 삭제는 symlink를 따라가지 않고 `<hub state>` 밖을 지우지 않는다.
4. Agent별 system key(`board-agent.<id>`), 사람 key의 `board-agent*` 이름 거절, process가 끝나면 key 행 삭제, Hub의 Board 생성 때 남은 system 행 삭제. 내부 listener는 실행 중인 agent 각자의 이번 key만 받음. 출처 `origin_name`과 `Board agent · <이름>` label(댓글·편집 작성자 포함).
5. Session API(SPEC 표 전부)와 없어지는 route(`/board/restart` GET·POST, `/ws/board`), route audit.
6. WebSocket v2 `GET /ws/board/{id}`: upgrade 전 한도(전체 128, 세션당 hello 전 8), hello v2, terminal당 8 연결(4429), attach·replay·구독, `clients`·`size`·`state`·`gone` message, 도착 순서 입력, 크기 주인 규칙, 연결별 heartbeat·세션·queue, close code v2. `board-state.js`를 protocol v2로 바꾸고(순수 상태 기계, terminal 하나당 하나) 현행 `/board` 화면이 `agent-1`·`shell-1` 두 tab으로 계속 동작하도록 `board.js`·`pages/board.py`의 최소 glue를 맞춘다(D2 전까지 `/board`가 깨지지 않게).
7. hub_v1 `status.board_agent` 새 모양, `wb hub status` 줄, `Hub.active()`와 정지 확인 목록, nav 상태 점, `/api/dashboard`의 Board 수.
8. 감사 event 표(SPEC), `terminal_takeover` 폐지.
9. 이행: registry가 없으면 `agent-1`(`home: "board-agent"`), 남은 `board-agent` system 행 삭제. 되돌리기 안전(이전 판이 `board-agent/`를 그대로 씀).

## 수락 기준

WBX-AC-27, 28, 29, 30(규칙·저장·전파), 31, 32·33(server와 `board-state.js`), 34, 37, 38. AC-25·26·35·36과 AC-30·32의 화면 표시는 WBX-D2.

## 계약과 맥락

현행: `hub/board/set.py`(`BoardSet`, `Slot`, `Channel`, `CHANNELS`, lease), `hub/board/ws.py`(`BoardTerminal`, `Connection`, `_paced`, `_watch`, close code), `hub/board/launch.py`(`BoardLauncher`, `prepare_board_home`, `board_dir`, `archive_omp_logs`, `record_board_event`), `hub/auth.py`(`SYSTEM_KEY_NAME`, `rotate_system_key`, `verify(system_only=)`, `issue`의 `name_reserved`), `hub/rest.py`(`create_internal_app`, `InternalListener`, `RestApi(system_only)`), `hub/inbox/service.py`(`Principal.board_agent`, `origin`, `origin_json`), `web/pages/requests.py`(`AUTHOR_LABELS`), `web/app.py`(`board_routes`, `BOARD_CHANNELS`), `web/pages/board.py`·`board_status.py`·`dashboard.py`, `hub/server.py`(`make_board`, `board_state`, `active`, status), `hub/cli.py`(status 줄), `static/board-state.js`·`board.js`. BRIEF WBX-D44~D48, D54~D58. ADR-0003, C-D64, WBX-D24·D37. `hub.sqlite3` schema version은 올리지 않는다.

## 검증

Seam(SPEC s4 "시험", 새 seam 없음): S1·S6(session API, CSRF, Bearer 401, route audit, page 틀, 이름 escape), 기존 Board seam(`tests/hub/test_board_set.py`·`test_board_launch.py`, temp state dir: registry·이름·한도·이행·삭제), T5(`tests/hub/test_board_ws.py`, recorder PTY + 여러 `Tab`: 공유 연결·크기·한도·감사), S3(`wb _hub --foreground` + `fixtures/fake_omp_board.py`: 여러 agent의 env·key·내부 listener·출처·정지 목록·Hub 재시작), S8(`board-state.js` v2), S5(외부 URL·inline script 없음).
독립 기대값: SPEC s4 표와 close code, `/proc/<pid>/environ`, `sqlite3`로 읽은 `audit`·`api_keys`·inbox `requests.origin_name`, registry 파일 bytes·mode, symlink 대상의 inode·내용.
Behavior 순서(한 behavior씩 RED → GREEN):
(1) 이름 검사·기본 이름 (2) registry 쓰기·읽기·`registry_invalid`·쓰기 실패 시 불변 (3) 이행: 파일 없음 → `agent-1`(`board-agent`) (4) agent 만들기·한도·`limit_reached` (5) agent별 home·`--continue`·`omp-logs` (6) agent별 system key·사람 key 이름 거절·끝나면 행 삭제·시작 때 남은 행 삭제 (7) 내부 listener 판정(실행 중 agent의 이번 key만) (8) 출처 `Board agent · <이름>`(제출 시점 이름, 뒤 rename 무관) (9) 중지 → `stopped`·메모리 유지·다시 시작 `--continue` (10) 삭제: home 삭제·symlink 대상 불변·`<hub state>` 밖 거절·부분 실패 (11) shell 만들기·닫기·첫 열기 `shell-1`·Hub 재시작 뒤 없음 (12) 한도 변경·범위·`limit_below_count`·Hub 재시작 뒤 유지 (13) session API 오류 code·CSRF·Bearer 401·없어진 route (14) terminal별 lock: 한 terminal 종료 중 다른 terminal 동작 진행 (15) WS v2 hello·upgrade 전 한도 (16) 두 연결 같은 replay·live, 도착 순서 입력 (17) terminal당 9번째 4429 (18) 크기 주인 규칙·hello 크기는 혼자일 때만·`size` broadcast (19) 연결별 heartbeat 4002·4401·4003, 다른 연결 영향 없음 (20) `clients`·`state`·`gone`(4404) (21) 감사 event와 detail (22) status·`wb hub status`·`active()`·정지 확인 목록·nav 점·dashboard 수 (23) `board-state.js` v2 상태 기계(재연결 규칙, 44xx 무재연결) (24) 현행 두 tab 화면의 v2 glue(S1·S6 + S8).
명령(공통 접두 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest`): `discover -s tests/hub -p 'test_board_*.py'`, `discover -s tests/hub -p 'test_hub_auth*.py'`, `discover -s tests/hub -p 'test_inbox_service.py'`, `discover -s tests/hub -p 'test_hub_board_status_text.py'`, `discover -s tests/hub -p 'test_web_route_audit.py'`, `discover -s tests/hub -p 'test_web_dashboard*.py'`, `discover -s tests/hub -p 'test_web_pages_board*.py'`, `discover -s tests/hub -p 'test_board_state_js.py'`; 회귀 `discover -s tests/hub -p 'test_hub_web_*.py'`, `discover -s tests/hub -p 'test_board_rest*.py'`.
독립 test_designer(Sonnet) 필수. 초점: 멈춘·삭제한 agent의 key가 내부 listener에서 통하는 경로, 삭제가 symlink를 따라가는 경로, replay와 live 사이의 빠짐·중복, 두 연결의 크기 ping-pong, 한 연결의 overflow·timeout이 다른 연결을 끊는 경로, registry 쓰기 실패 뒤 process가 남는 경로, 한도 경계(8·9, 32·33).
격리 실행 주의: S3는 실제 Hub·fake OMP·Board home을 띄운다. CLAUDE.md "테스트 실행"의 sandbox 규칙(짧은 private `XDG_RUNTIME_DIR`, temp HOME/XDG_STATE_HOME, `WORKBENCH_HUB_AUTOSTART=0`, 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l` 동일). 사용자 `~/.omp/agent/agent.db`를 쓰지 않는다(빈 placeholder). Checkpoint·hash manifest exclude: `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시(`*/.omp/natives/`, Board home). 전후 `du -sh .workflow graphify-out .worktrees`를 보고한다.

## 범위 밖과 중단 조건

격자·끌기·panel·좁은 화면·focus(WBX-D2), USAGE 재작성(WBX-D3), agent 사이의 OS 권한 분리(WBX-D58), Hub 재시작 뒤 shell 복원, `hub.sqlite3` schema 변경. 기존 seam으로 잡을 수 없는 behavior가 나오면 새 seam을 만들지 말고 반환한다. 이행에서 기존 `board-agent/`의 세션을 `--continue`로 잇지 못하는 OMP 동작이 보이면 멈추고 보고한다.

## 출력

변경 경로, behavior별 RED 실패 메시지와 GREEN 결과, registry 파일 예(값 가림), session API·close code 대조표, 감사 행 예, 이행 전후 home 목록, 명령·결과.
