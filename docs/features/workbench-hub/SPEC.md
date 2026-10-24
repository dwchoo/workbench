# Workbench Hub·다중 WB 프로젝트 — 구현 명세

Revision: s1; source brief: [BRIEF.md](BRIEF.md) r0.2 (approved 2026-10-25, orchestrator 검토 승인 사전 위임). state: approved (2026-10-25, 사용자가 SPEC·seam 확정을 orchestrator에 사전 위임했고 orchestrator가 검토해 승인했다). 구현 baseline은 HEAD `e73cf39`(작업 트리에 문서·skill 미커밋 변경이 있고 `src/`·`tests/`에는 hub 코드가 없다)이다.

BRIEF의 사용자 결정(HB-D01~D10), orchestrator 결정(HB-D11~D15)과 수락 예시(HB-AC-01~19)는 바꾸지 않는다. 이 문서의 "기술 보완"은 코드 조사로 정한 구체 형태다. 사용자 노출 동작을 바꾸거나 BRIEF에 없는 예외를 더하는 항목은 "열린 결정·위임된 선택"에 따로 모았다. (A) [Task Inbox BRIEF](../task-inbox/BRIEF.md) r0.4는 이 문서의 범위가 아니며, (A)가 붙을 자리만 "확장 지점"에 적는다.

## 문제와 해결 방향

WB 프로젝트마다 backend 하나가 따로 돌지만, 등록된 WB 프로젝트를 한곳에서 보고 켜고 끄고 골라 들어가는 수단이 없다. 해결은 다섯 부분이다.

1. OS 사용자당 하나인 Hub 프로세스를 둔다. `wb`가 없으면 background로 띄우고(HB-D05), Hub는 어느 backend의 부모·감독자도 아니며 어느 WB 프로젝트의 process tree에도 들어가지 않는다(HB-D04).
2. Hub registry는 상태 디렉터리의 JSON 파일 하나다. `wb init`·첫 시작이 자동 등록하고 `wb hub add/remove`가 수동 관리한다(HB-D06).
3. Hub는 각 backend를 기존 ui_v1 UDS에 **attach하지 않은 연결**로 관측해 목록 행을 만든다. 관측 실패는 "연결 없음/불명"이며 꺼짐이 아니다(HB-D09, 불변식 3).
4. WB 프로젝트 밖의 `wb`는 TUI Hub 목록을 띄운다. 고르면 기존 `run_product`로 연결하고 detach하면 목록으로 돌아온다(HB-D08). 켜기는 기존 `wb start --no-attach` 경로, 끄기는 기존 `shutdown_request`/`shutdown_confirm`이다(HB-D07).
5. (A)의 웹 제어가 쓸 backend 계약 변경(attach 없이 `pause`/`resume`)을 지금 정하고 구현한다.

## 관련 사용자 이야기

- 소유자: 어느 폴더에서든 `wb`를 치면 등록된 WB 프로젝트 목록이 보이고, 고르면 들어가고, detach하면 목록으로 돌아온다. 프로젝트 안에서 `wb`를 치면 지금처럼 바로 들어간다.
- 소유자: 꺼진 프로젝트를 고르면 "켤까요?"를 묻고, 켜진 프로젝트를 끌 때는 진행 중 작업을 보고 확인한다.
- 소유자: Hub를 멈추거나 Hub가 죽어도 돌던 실험은 계속되고, 프로젝트 안에서 `wb`로 직접 들어갈 수 있다.
- 소유자: `wb update`는 Hub도 멈추고, 다음 `wb`에서 새 버전 Hub가 뜬다.

## 범위와 비목표

- 포함: Hub 프로세스 수명(spawn, lock, 제어 socket, 로그, 상태 파일, 비정상 종료 뒤 재시작, 단일 instance, update fence와의 관계), registry, 상태 집계, `wb hub start|stop|status|add|remove|list|enable|disable`, `wb init`, `wb --version`, bare `wb`의 프로젝트 안/밖 분기, TUI Hub 목록, `wb update`의 Hub 정지, ui_v1 비attach `pause`/`resume`, WB 관리 process tree 안에서의 자동 시작 보류.
- 예약만: `wb hub passwd`, `wb hub tls`(이름만 두고 "(A)에서 제공 예정"으로 거절), hub_v1의 `power_on`·`power_off_*`·`pause`·`resume`·`confirm_boot` 요청 이름, `hub.toml`.
- 비목표(BRIEF와 같음): Hub의 manager·worker, 새 종료 의미, 디렉터리 탐색, 같은 폴더의 두 번째 WB 프로젝트, 여러 폴더 묶기, 다중 host·다중 OS 사용자, 자원 중재. 그리고 (A) 전체(웹 서버, 로그인, MCP, inbox, Board agent).
- 등록 대상은 `<root>/.wb` 형태의 WB 프로젝트뿐이다. `--data-dir`·`WORKBENCH_DATA_DIR`로 지정한 legacy 저장소(`.wb`가 아닌 위치)는 자동 등록하지 않고 `wb hub add`도 받지 않는다.
- 지원 환경은 core와 같은 Linux다. systemd --user는 `wb hub enable`/`disable`에서만 쓴다.

## 공개 인터페이스, 데이터 계약, 불변 조건

### 위치(기술 보완)

| 대상 | 위치 | 비고 |
|---|---|---|
| Hub 상태 디렉터리 | `$XDG_STATE_HOME/omp-workbench/hub/`(절대 경로일 때), 아니면 `~/.local/state/omp-workbench/hub/` | HB-D06. `update.py`의 backup 위치 규칙과 같다. 0700, 사용자 소유, symlink 금지 |
| 단일 instance lock | `<state>/hub.lock` | `InstanceLock`(flock, 0600). Hub 수명 동안 Hub 프로세스만 잡는다 |
| Hub record | `<state>/hub.json` | 아래 schema. client는 socket 위치를 여기서만 읽는다(환경 변수로 계산하지 않는다) |
| Hub log | `<state>/hub.log` (+ `hub.log.1`) | 시작할 때 1 MiB를 넘으면 `.1`로 한 번 돌린다. 0600 |
| Registry | `<state>/registry.json`, `<state>/registry.lock` | 아래 계약 |
| 제어 socket | `$XDG_RUNTIME_DIR/omp-workbench-hub/hub.sock`(XDG_RUNTIME_DIR이 절대 경로이고 사용자 전용 디렉터리일 때), 아니면 `/tmp/omp-workbench-hub-<uid>/hub.sock` | 디렉터리 0700, socket 0600. 경로 길이 ≤ `MAX_SOCKET_PATH_BYTES`(107). **`omp-workbench/` 아래에 두지 않는다**: `maintenance.discover()`가 그 아래의 모든 디렉터리를 runtime binding으로 검사해 실패하기 때문이다 |
| 설정(예약) | `$XDG_CONFIG_HOME/omp-workbench/hub.toml` | (B)는 읽거나 쓰지 않는다. (A)가 쓸 때도 `wb setup`의 setup lock을 잡지 않는다 |
| 사용자 서비스 unit | `$XDG_CONFIG_HOME/systemd/user/omp-workbench-hub.service` | `wb hub enable`만 만든다 |

### `hub.json` schema(기술 보완)

`{schema_version: 1, pid, start_ticks, boot_id, uid, socket, started_at, mode: "spawned"|"foreground"|"systemd", log, version: {release, commit, kind: "git"|"editable"|"other"|"unknown"}}`. Socket을 bind한 뒤 원자적으로 쓴다(`write_private_json`). 정상 종료 때 자기 pid의 record면 지운다.

Client 판정(`HubIdentity.state`):
- `not_running`: `hub.lock`을 아무도 잡고 있지 않다(record가 남아 있어도 stale로 본다).
- `running`: lock이 잡혀 있고 그 holder(`/proc/locks`)가 record의 pid이며, `uid`·`boot_id`가 현재와 같고, `process_state(pid, start_ticks) == "alive"`이며, socket 연결의 `SO_PEERCRED` pid·uid가 record와 같고, `welcome.hub.pid`·`start_ticks`가 같다.
- `unknown`: lock은 잡혀 있는데 위 확인 중 하나라도 실패한다(시작 직후 record를 쓰기 전 포함). `unknown`에서는 새 Hub를 띄우지 않는다(어차피 lock에서 진다). 상태를 알리고 끝낸다.

### Hub 프로세스 수명(HB-D04, D05)

Entry는 내부 subcommand `wb _hub`(help에 숨김)다. argv는 `sys.executable -I -c <bootstrap> _hub ...`로, 기존 `spawn_backend`의 bootstrap과 같은 방식이며 `_backend` 원소를 포함하지 않는다(`maintenance.discover()`가 backend로 오인하지 않는다).

시작 순서:
1. 관리 tree 자기 확인(아래 "WB 관리 process tree"): 안이면 ready fd에 `M`을 쓰고 exit 3.
2. 상태 디렉터리 준비, `hub.lock` 획득. 다른 Hub가 잡고 있으면 ready fd에 `L`을 쓰고 exit 0.
3. log 회전, 제어 socket 디렉터리 준비, 남은 socket 파일 제거(lock을 잡은 뒤에만, `unlink_stale_socket`), bind, `hub.json` 기록, ready fd에 `R`.
4. 하나의 asyncio event loop에서 제어 socket을 서비스한다. backend 관측 같은 blocking 작업은 thread pool executor에서 돈다. (A)의 웹 서버(aiohttp)는 같은 loop에 붙는다.
5. 2초마다 watchdog: `hub.lock` 경로의 (dev, ino)가 잡고 있는 fd와 다르거나 상태 디렉터리가 없어지면 종료한다. lock 파일이 지워지고 다시 만들어지면 두 번째 Hub가 lock을 얻을 수 있으므로 단일 instance를 지키기 위한 조건이다(테스트 temp HOME 삭제 뒤 남는 Hub도 이것으로 끝난다).
6. `SIGTERM`·`SIGINT`는 정상 정지, `SIGHUP`은 무시한다.

정상 정지 순서: 새 연결 거절 → 진행 중 요청 정리 → (A 확장 지점: Board agent 세트 종료) → socket 제거(bind할 때 기록한 dev/ino가 같을 때만) → 자기 `hub.json` 제거 → lock 해제 → exit 0. Hub는 어떤 backend에도 신호·종료 요청을 보내지 않는다(불변식 1).

Spawn(`spawn_hub`, 기술 보완): 기존 `spawn_backend` 패턴을 따른다.
- 사용자 범위 update `fence(shared=True)`를 **spawn하는 동안만** 잡는다(non-blocking. 실패하면 `update_in_progress`로 자동 시작을 건너뛴다). Hub 프로세스는 fence를 잡지 않는다.
- fork → `setsid` → fork. 중간 프로세스는 바로 끝난다. 손자는 stdin을 `/dev/null`, stdout/stderr를 `hub.log`(O_APPEND|O_NOFOLLOW, 0600)로 돌리고, ready fd 외의 fd를 닫고, `chdir("/")`, `umask(0o077)` 뒤 exec한다. `--spawner-pid`를 넘겨 Hub가 중간 프로세스가 끝나 재부모화된 뒤(≤ 2 s) 자기 확인을 하게 한다.
- 환경: 호출자의 환경에서 프로젝트·세션 전용 변수(`WORKBENCH_DATA_DIR`, `WORKBENCH_G3_*`, `WORKBENCH_AGENT_ROLES`, `WORKBENCH_OMP_RUNTIME_ROOT`)를 뺀다. 나머지는 유지한다((A)의 Board agent와 Hub 쪽 켜기가 사용자 환경을 필요로 한다).
- 호출자는 ready fd를 최대 5 s 읽는다. `R`이면 `running` 확인, `L`이면 이긴 Hub가 `running`이 될 때까지 최대 5 s 기다림, `M`이면 관리 tree 거절, EOF·시간 초과면 실패(`hub.log` 경로 안내).
- 동시 실행: 여러 `wb`가 동시에 spawn해도 lock이 하나만 남긴다. 진 Hub는 `L` 뒤 exit 0이고, 모든 호출자는 같은 pid의 Hub를 본다(HB-AC-01).
- 비정상 종료 뒤: lock은 커널이 풀고, 남은 `hub.json`은 `not_running` 판정에서 무시된다. 다음 자동 시작 대상 `wb`가 새 Hub를 띄운다. 자동 재시작 감독자는 두지 않는다(`wb hub enable`의 `Restart=on-failure` 제외).

### WB 관리 process tree와 자동 시작 보류(기술 보완, orchestrator 결정 사전 위임)

[관측 사실] 실험 run은 host shell 아래 `shell_g2/lifecycle.py --supervisor`가 `PR_SET_CHILD_SUBREAPER`를 켜고 실행하며, 주 명령이 끝난 뒤에도 남은 후손이 끝날 때까지 기다린다(`src/workbench/terminal/shell_g2/lifecycle.py:615-617`, `:780` 이후). 이중 fork한 Hub가 그 supervisor로 재부모화되면 (1) 실험 명령이 Hub 때문에 끝나지 않고, (2) 종료 때 `left_session` 생존자로 표시되며(`service.py:1396-1418`), (3) `wb update`가 `left_session`을 보고 업데이트를 거절한다(`update.py:251-253`). Hub가 띄운 다른 프로젝트 backend도 같은 supervisor로 재부모화된다. Backend 자신은 subreaper가 아니므로 supervisor 밖에서 실행한 `wb`의 Hub는 init/systemd --user로 재부모화된다.

규칙:
- **관리 tree 판정**(`managed_ancestor(pid)`): `pid`부터 조상을 따라가며 argv에 원소 `_backend`가 있고 어느 원소에 `workbench`가 있는 프로세스(=`maintenance.discover()`와 같은 기준), 또는 argv에 `--supervisor`가 있고 어느 원소가 `shell_g2/lifecycle.py`로 끝나는 프로세스가 있으면 "안"이다. `/proc`을 읽지 못하면 "안"으로 본다(시작하지 않는 쪽으로 실패).
- **호출자 사전 확인**: 자동 시작과 `wb hub start`는 spawn 전에 호출자 자신의 조상을 확인한다. 안이면 spawn하지 않는다. 자동 시작은 명령을 계속 진행하고, stderr가 TTY일 때만 한 줄을 보인다: `[workbench] WB가 관리하는 terminal 안에서는 Hub를 자동으로 시작하지 않습니다. 외부 terminal에서 'wb hub start'를 실행하세요.` `wb hub start`는 같은 문구로 exit 2.
- **Hub 자기 확인**: exec된 Hub는 재부모화 뒤 자기 조상을 같은 기준으로 확인하고 안이면 `M`·exit 3(lock을 잡기 전). 사전 확인과 실제 재부모화 사이의 경합을 막는 결정적 확인이다.
- Hub가 이미 실행 중이면 관리 tree 안의 `wb`도 그 Hub를 쓴다(목록·등록). 시작만 보류한다.
- 이 보류는 HB-AC-01 "Hub가 없을 때 `wb`를 실행하면 시작"의 추가 예외다("열린 결정" 1).

### Registry 계약(HB-D06, 기술 보완)

`registry.json`: `{schema_version: 1, projects: [{root, project_id, name, registered_at, source: "start"|"init"|"add"}]}`.
- `root`는 resolve된 절대 경로이고 `<root>/.wb/project.json`의 `project_root`와 같다. `name`은 `root`의 basename이다.
- 유일성: 같은 `root`는 하나, 같은 `project_id`는 하나. 여러 root를 한 항목으로 묶지 않는다(HB-AC-14).
- 쓰기: `registry.lock`을 `LOCK_EX`로 잡고(최대 5 s, 실패하면 `registry_busy`) 읽기 → 수정 → `write_private_json`(임시 파일 + rename + fsync) → 해제. 어느 `wb` 프로세스든 쓸 수 있다. Hub는 읽기만 하고 요청마다 다시 읽는다(Hub가 없어도 등록이 된다).
- 손상(JSON·schema 오류, 권한 이상): 읽기 쪽은 `registry_invalid`를 보고하고, 쓰기 쪽은 파일을 덮어쓰지 않고 거절한다(경로를 안내). 자동 등록에서는 경고 한 줄만 보이고 명령은 계속한다.
- `ensure(root, project_id, source)`: 없으면 추가, 같으면 그대로(멱등). 같은 root에 다른 project_id가 있으면 덮어쓰지 않고 `registry_mismatch`(목록은 HB-AC-13의 불일치로 보인다. 경고: `wb hub remove <root>` 뒤 `wb hub add <root>`).
- 등록 해제는 registry 항목만 지운다. `.wb/`는 건드리지 않는다(HB-AC-07).

### CLI 계약

Exit code는 기존 0 성공 / 1 실패·거절 / 2 요건 없음(환경·TTY·초기화·관리 tree) / 3 실행 중 아님을 쓴다.

| 명령 | 동작 | Hub 자동 시작 |
|---|---|---|
| `wb`(인자 없음) | 프로젝트 안(가장 가까운 `.wb` 또는 `WORKBENCH_DATA_DIR`): 지금의 `start`. 프로젝트 밖: TUI Hub 목록. 밖 + TTY 아님: exit 2 `"Hub 목록은 terminal이 필요합니다. 'wb hub list'를 쓰세요"` | 예 |
| `wb start …`(명시) | 지금과 같다(프로젝트 밖이면 지금처럼 초기화 확인을 묻는다). 성공·이미 실행 중 연결 모두 registry `ensure(source="start")` | 예 |
| `wb init [PATH]` | 새 명령. PATH(기본 cwd)를 TTY `[y/N]` 확인 뒤 초기화한다(`initialize_project`, backend는 시작하지 않는다). 성공하면 `ensure(source="init")`. 이미 그 폴더가 WB 프로젝트면 등록만 하고 exit 0. PATH가 다른 WB 프로젝트의 하위 폴더면 거절(exit 1, 중첩 초기화 없음). TTY가 아니면 exit 2, `.wb`를 만들지 않는다 | 예 |
| `wb hub start` | 없으면 spawn, 있으면 그대로. 결과(pid, version) 출력. 관리 tree 안이면 exit 2 | (자기 자신) |
| `wb hub stop [--yes]` | 아래 "정지 2단계". 실행 중 아님이면 `Hub 실행 중 아님` exit 3. 성공하면 `Hub를 멈췄습니다. 켜져 있던 WB 프로젝트는 계속 실행됩니다.` | 아니오 |
| `wb hub status [--json]` | `running`: pid, mode, version(+ 이 `wb`의 설치 version과 다르면 drift 경고), started_at, socket, log, registry 수·상태, 부팅 자동 시작(enabled/disabled/unknown), `web: not_available`. `not_running`: `Hub 실행 중 아님` exit 3. `unknown`: 이유와 exit 1 | 아니오 |
| `wb hub list [--json]` | Hub `list` 결과를 TUI와 같은 열로 출력(`--json`은 hub_v1 결과 그대로). Hub가 없고 시작할 수 없으면 exit 1(관리 tree 거절이면 2) | 예 |
| `wb hub add <PATH>` | PATH가 WB 프로젝트 root(`<PATH>/.wb` 정상 marker)면 `ensure(source="add")`. `.wb`가 없으면 `.wb`를 만들지 않고 exit 1 + `wb init <PATH>` 안내. 다른 프로젝트의 하위 폴더면 exit 1 + 그 root 안내. 같은 root가 다른 id로 등록되어 있으면 exit 1 | 예 |
| `wb hub remove <PATH>` | 등록되어 있지 않으면 exit 1. 그 항목의 상태가 `on`·`starting`·`closing`·`unknown`이면 `먼저 끄세요`로 exit 1, registry 그대로. `off`·`mismatch`면 항목 삭제, `.wb` 보존 | 예 |
| `wb hub enable` / `disable` | 아래 "부팅 자동 시작" | 아니오 |
| `wb hub passwd` / `tls` | 예약. `Web Board 기능에서 제공 예정입니다` exit 2 | 아니오 |
| `wb setup`·`update`·`doctor`, `--help`, `--version`, `_backend`, `_hub` | 기존 동작. `--version`은 새로 둔다: `wb <release> (<commit 12자>|editable|unknown)` | 아니오 |
| 그 밖의 명령(`attach`, `status`, `shutdown`, `confirm-boot`, `logs`, `records`, `migrate`) | 기존 동작 | 예 |

자동 시작 규칙(HB-D05, D11): `main()`이 argparse를 통과한 뒤, handler 전에 실행한다(argparse 오류·`--help`·`--version`은 handler 전에 끝나므로 시작하지 않는다). Hub가 `running`이면 아무것도 하지 않는다. `not_running`이면 위 spawn을 하고 최대 5 s 기다린다. 실패해도 명령은 계속한다(HB-D04, stderr 한 줄). Hub가 꼭 필요한 명령(bare 목록, `wb hub list`)만 실패로 끝낸다. 환경 변수 `WORKBENCH_HUB_AUTOSTART=0`이면 자동 시작만 끈다(테스트·CI용 opt-out, "열린 결정" 4). `wb hub start`는 이 값과 무관하게 동작한다.

### 정지 2단계(기술 보완, (A) 확장 지점)

`wb hub stop`과 `wb update`는 같은 함수 `stop_hub(ask)`를 쓴다.
1. hub_v1 `stop_request` → `{token, active, expires_in: 60}`. `active`는 Hub가 소유한 살아 있는 것들의 목록이다. (B)에서는 항상 `[]`이다. (A)는 여기에 Board agent 세트를 넣는다(orchestrator 결정: 세트가 돌고 있으면 확인 뒤 종료).
2. `active`가 비어 있지 않으면 내용을 보이고 확인을 받는다(`--yes`, 아니면 TTY `[y/N]`, TTY가 아니면 거절하고 아무것도 멈추지 않는다).
3. `stop_confirm{token}`: token이 다르거나 만료되었거나 그 사이 `active`가 바뀌었으면 `stop_token_mismatch`로 거절한다. 성공하면 `{stopping: true}`를 답하고 정상 정지를 시작한다.
4. 호출자는 `hub.lock`이 풀릴 때까지 최대 10 s 기다린다.
5. Hub가 5 s 안에 답하지 않으면(`running`이었던 같은 identity에 한해) pidfd로 `SIGTERM`을 보내고 다시 최대 5 s 기다린다. 그래도 남으면 pid와 함께 실패(exit 1). `SIGKILL`은 보내지 않는다. `SIGTERM` 경로에는 확인이 없으므로 (A)는 이 경로에서 Board agent 세트를 확인 없이 끝내는 동작을 정해야 한다.

### hub_v1 제어 socket 프로토콜(HB-O01 일부, 기술 보완)

용도: 같은 OS 사용자의 `wb` 프로세스(TUI 목록, `wb hub list/status/stop`, `wb update`)가 Hub에 묻는 통로다. (A)의 웹 서버는 Hub 프로세스 안에서 같은 handler를 직접 부르며 이 socket을 거치지 않는다. 계약 상수는 `src/workbench/contracts/hub_v1.py`에 둔다.

- Transport: 위 UDS. Server는 accept할 때 `SO_PEERCRED` uid가 Hub euid와 다르면 바로 닫는다. Client는 "`hub.json` schema"의 `running` 확인을 한다. 연결은 최대 16개, 60 s 동안 요청이 없으면 닫는다.
- Framing: UTF-8 JSON 객체 한 줄(`\n` 끝), 한 줄 ≤ 1 MiB. 넘거나 JSON이 아니면 `{"type":"reject","reason":"protocol_error"}` 뒤 닫는다.
- 첫 줄 `{"v":1,"type":"hello","client":"<이름>"}` → `{"v":1,"type":"welcome","hub":{pid,start_ticks,version,started_at,mode}}`. `v`가 지원 목록에 없으면 `reject`/`version_mismatch`.
- 요청 `{"v":1,"id":"<≤64자>","type":<op>,…}` → `{"v":1,"id":…,"type":"result","ok":true,…}` 또는 `{"ok":false,"reason":<code>,"detail":<문장>}`. 모르는 op는 `unsupported_type`.
- Handler는 `(principal, request)`를 받는다. (B)의 principal은 `local`(같은 uid의 socket 연결) 하나다. (A)는 웹 세션·API key principal을 더한다(사용자 사전 위임 결정: 다른 editor API key는 Board agent와 같은 요청 권한).

| op | 입력 | 결과 |
|---|---|---|
| `status` | — | `{hub:{…hub.json 필드}, registry:{state:"ok"|"invalid", count, path}, web:{state:"not_available"}, board_agent:null}` |
| `list` | `fresh`(bool, 기본 false) | `{observed_at, registry:{state, path}, rows:[행]}`. 아래 "상태 집계" |
| `stop_request` | — | `{token, active:[], expires_in:60}` |
| `stop_confirm` | `token` | `{stopping:true}` 또는 `stop_token_mismatch` |
| (예약) `power_on`, `power_off_request`, `power_off_confirm`, `pause`, `resume`, `confirm_boot` | — | (B)에서는 `unsupported_type`. (A)가 웹과 같은 handler로 연다 |

### Hub ↔ backend IPC(HB-O01, 기술 보완)

선택: **기존 프로젝트 UDS(`ui.sock`)의 ui_v1을 attach하지 않은 연결로 재사용**하고 상태는 **polling**한다. 새 channel은 만들지 않는다.
- 이유: 0600 UDS + client 쪽 `SO_PEERCRED`·`expected_process` 검증(`client.py:61-69`)이 이미 있다. `snapshot`·`shutdown_request`·`shutdown_confirm`·`confirm_boot`는 이미 attach 없이 받는다(`ui_server.py:363-381`). attach하지 않은 연결은 attach된 UI를 방해하지 않는다. `state` push는 attach된 연결에만 가므로 구독은 계약 변경이 되는데, 목록은 2~3 s polling이면 충분하다.
- 연결: 관측마다 `UiClient(ui_socket, name="workbench-hub", timeout=2, expected_process=record.processes.backend)` → `snapshot` → 닫기. 신원 대조는 `cli._running_snapshot`(`backend/cli.py:65-85`)과 같은 규칙(pid·start_ticks·data_dir·project_dir, project_id·runtime_root·uid·boot_id)이며 같은 함수를 공유한다. backend 연결 상한 16에 비해 관측 연결은 순간 1개다.
- 켜기·끄기·일시정지·재개·`confirm-boot`는 `workbench.hub.projects`의 한 구현을 TUI(이번 범위)와 (A)의 웹이 함께 쓴다(아래).

### ui_v1 변경: attach 없는 `pause`/`resume`(기술 보완, orchestrator 결정 사전 위임)

현재 `pause`/`resume`은 attach가 필요하고(`ui_server.py:382`의 `_require_attached`, `:409-412`) attach는 한 연결만 할 수 있다. 웹(A)이 attach를 빼앗지 않고 일시정지·재개하려면 계약을 바꿔야 한다.

선택: 새 요청 type을 만들지 않고, **기존 `pause`/`resume`을 attach하지 않은 연결에서도 받되 hello `client`가 `workbench-hub`이고 그 연결의 server 쪽 `SO_PEERCRED` uid가 backend euid와 같을 때로 한정**한다.
- 이유: 의미가 기존과 같다(BRIEF (A) 불변식 4 "웹 제어는 기존 backend 동작을 호출할 뿐"). 더 파괴적인 `shutdown_*`·`confirm_boot`가 이미 attach 없이 열려 있으므로 보안 경계가 넓어지지 않는다. client 이름 제한은 보안 경계가 아니라 의도 표시이고(같은 uid는 어차피 attach할 수 있다), 보안 경계는 0600 socket과 server 쪽 uid 확인이다. 새 type을 만들면 controller·문서·테스트가 두 벌이 된다.
- 계약: `ui_v1.NON_ATTACHED_CONTROL_CLIENTS = frozenset({"workbench-hub"})`. UiServer는 accept할 때 peer 신원(pid, uid)을 연결에 기록한다. `PAUSE`/`RESUME`: attach된 연결이면 지금과 같다. attach하지 않은 연결이면 `client ∈ NON_ATTACHED_CONTROL_CLIENTS`이고 peer uid == `os.geteuid()`일 때 같은 controller 메서드를 부른다. 아니면 지금처럼 `not_attached`. `resume`의 `reconciled: true` 요구는 그대로다. 성공 뒤 attach된 UI가 있으면 지금처럼 `state`를 push한다. 이 요청은 attach 자리를 차지하지 않는다.
- 호환: ui_v1 `VERSION`은 그대로다(추가 허용). 옛 backend는 `not_attached`로 거절하며, `workbench.hub.projects`는 이를 `backend_too_old`(재시작 필요)로 보고한다. ui_v1 모듈 docstring의 `pause`/`resume` 설명을 고친다.

### 프로젝트 제어 라이브러리 `workbench.hub.projects`(기술 보완)

입력은 registry 항목이다. 모든 함수는 먼저 `check_entry`(아래 불일치 규칙)를 하고 불일치면 아무것도 하지 않고 `mismatch`로 거절한다(HB-AC-13).
- `power_on(entry, attach)`: 기존 `cmd_start`를 `data_dir=<root>/.wb`, `no_attach=True`로 실행한다(update fence 공유, 시작 요건 확인, `project_initialization_lock`, `spawn_backend` 이중 fork, `_wait_started`). 이미 실행 중이면 두 번째 backend를 만들지 않는다(기존 lock·`inspect_backend`가 기준). Backend는 호출 프로세스의 자식이 아니라 새 session의 고아다. 이번 범위에서는 TUI `wb` 프로세스가 호출하므로 backend 환경은 사용자가 그 terminal에서 `wb`를 실행했을 때와 같다.
- `power_off(entry, ask, out)`: `shutdown_request` → `ask(active)` → 거절이면 아무것도 멈추지 않음 → `shutdown_confirm(token)` → `shutdown_token_mismatch`면 "작업이 바뀌었습니다"를 알리고 새 `shutdown_request`로 새 목록을 다시 묻는다(HB-AC-09) → 성공이면 `closing` frame의 결과를 `result_deadline`까지 기다린다. `verified`가 아니거나 결과가 없으면 성공으로 보이지 않는다(C-AC-22). 기다리는 부분은 `cli.cmd_shutdown`의 loop를 함수로 빼서 공유한다.
- `pause(entry)`, `resume(entry)`(`reconciled: true`), `confirm_boot(entry)`(현재 snapshot의 `boot.boot_id`): `UiClient(name="workbench-hub")`로 attach 없이 보낸다. 이번 범위의 호출자는 테스트뿐이고 (A)의 웹이 쓴다(계약 우선 작업).

### 상태 집계(HB-D09, HB-D15, 불변식 3)

Hub는 `list`를 받으면 registry를 다시 읽고 항목마다 아래를 병렬로(최대 8 thread, 항목당 2 s) 관측한다. 결과는 2 s 동안 cache하고 `fresh: true`면 새로 관측한다.

행: `{root, name, project_id, power, power_detail, task, command, waits:[{code, since}], pending_requests, last_activity, backend:{pid,start_ticks}|null, observed_at}`.

`power` 결정 순서(먼저 맞는 것):

| 조건 | `power` | `power_detail` |
|---|---|---|
| `root`가 없다 | `mismatch` | `root_missing` |
| `<root>/.wb`가 없다 | `mismatch` | `not_initialized` |
| `read_project_marker` 실패 | `mismatch` | `marker_invalid` |
| marker의 `project_id` ≠ registry | `mismatch` | `project_id_mismatch` |
| `inspect_backend`가 `BackendIdentityError` | `unknown` | `identity:<reason>` |
| `stopped` | `off` | — |
| `starting` | `starting` | — |
| `running`인데 snapshot 연결·응답·신원 대조 실패 | `unknown` | `unreachable` / `timeout` / `snapshot_mismatch` |
| `running`, holds에 `shutdown_closing` | `closing` | — |
| `running`, `phase == "starting"` | `starting` | — |
| `running` | `on` | — |

관측에 실패한 행은 절대 `off`가 되지 않는다. `unknown`·`mismatch` 행의 다른 필드는 `null`·`[]`이다.

필드 출처(`on`·`closing`·`starting`(snapshot이 있을 때)):
- `task`: snapshot `task`가 있고 `status != "closed"`이면 `{task_id, kind, status, summary}`, 아니면 `null`. 단계는 `status`다.
- `command`: snapshot `panes.host_shell.command`(`{command_id, owner, label, state}`, CS-O10)이면 그것, 없으면 `null`.
- `waits`(이 순서): `automation.paused`가 true → `paused` / holds에 `boot_confirmation_required` → `boot_confirmation_required` / `recovery.report_wait.reason`이 `manager_editor_not_empty` 또는 `manager_session_not_connected` → 그 값(`since`는 report_wait의 값) / holds에 `metadata_unavailable` → `metadata_unavailable` / holds에 `model_hold:*` → `model_hold` / `task.status == "held"` → `task_held`(detail은 `held_reason`).
- `pending_requests`: (B)에서는 항상 `null`. 표시는 `—`이고 0이 아니다(HB-AC-19). (A)가 inbox에서 채운다.
- `last_activity`: `on` 계열은 `max(backend.started_at, task.since)`(있는 값만), `off`는 `<root>/.wb/backend.json`의 mtime(없으면 `null`), 나머지는 `null`.

표시(TUI와 `wb hub list`가 같은 `workbench.hub.presentation`을 쓰고, (A)의 웹도 같은 label을 쓴다): 열은 이름, 경로, 상태, Task·단계, 명령, 대기 사유, 요청, 마지막 활동이다. 상태 label: `on` 켜짐, `off` 꺼짐, `starting` 시작 중, `closing` 끄는 중, `unknown` 연결 없음/불명, `mismatch` 불일치(detail). 대기 사유 label: `paused` 일시정지, `boot_confirmation_required` 재부팅 확인 전 보류, `manager_editor_not_empty` manager 입력창에 글이 있어 알림 대기, `manager_session_not_connected` manager 세션 연결 없음(알림 대기), `metadata_unavailable` metadata 저장 장애로 보류, `model_hold` 모델 오류로 보류, `task_held` Task 보류. 명령 label: `running` 실행 중, `stopping` stop 중, `unknown` 불명. 값이 없으면 `—`. 마지막 활동은 상대 시간(예: `3분 전`), `--json`은 epoch 초.

### TUI Hub 목록(HB-D08, D12)

구조는 `ui/product`처럼 model/view/app을 나눈다(`src/workbench/ui/hub_list/`). Model은 순수 상태 기계(행, 선택, modal, notice)이고 key를 받아 action을 돌려준다. App은 action을 실행한다.

- 목록: Hub `list`를 3 s마다(그리고 `r`) 받는다. Hub 연결이 끊기면 상단에 "Hub 연결 없음"을 보이고 행을 지우지 않되 모든 행의 상태를 `연결 없음/불명`으로 바꿔 보인다. 행이 없으면 `등록된 WB 프로젝트가 없습니다. 'wb init <폴더>'로 만들거나 'wb hub add <폴더>'로 등록하세요.`
- Key: `↑`/`↓`/`k`/`j` 선택, `Enter` 진입, `o` 켜기(연결 없이), `x` 끄기, `r` 새로 고침, `q` 종료(exit 0).
- `Enter`: `on` → `Attach`. `off` → modal `"<이름>"을(를) 켤까요? [y/N]` → `y`는 `PowerOn(attach=True)`, 그 밖의 key는 아무것도 하지 않고 목록에 머문다(HB-AC-16). `starting`·`closing`은 notice만. `unknown`·`mismatch`는 이유 notice만, 켜기·진입 없음(HB-AC-13).
- `o`: `off`에서만 같은 modal → `PowerOn(attach=False)`.
- `x`: `on`에서만 `PowerOff`.
- App 실행: curses를 끝낸 상태(일반 terminal)에서 실행한다. `Attach`는 실행 시점에 `inspect_backend`로 identity를 다시 확인한 뒤 `run_product(ui_socket, expected_process)`. 반환 0(detach)·1(backend 닫힘·오류)이면 목록으로 돌아가고 notice를 보인다(HB-AC-10). 2(TTY 없음)면 exit 2. `PowerOn`은 `projects.power_on`의 출력을 그대로 보이고, 실패하면 "Enter를 누르면 목록으로"를 보인 뒤 목록으로, 성공하고 `attach`면 이어서 `Attach`. `PowerOff`는 `projects.power_off`를 터미널 문답(`[y/N]`)으로 실행한 뒤 결과를 보이고 목록으로.
- `run_product`가 매번 부르는 `setup_tmux_for_ui()`의 문답은 그대로 둔다.

### 부팅 자동 시작 `wb hub enable`/`disable`(HB-O02, 기술 보완)

- `enable`: `systemctl`이 PATH에 있고 `systemctl --user show-environment`가 성공해야 한다. 아니면 exit 2 `systemd 사용자 서비스 관리자를 쓸 수 없습니다. 설정은 바뀌지 않았습니다.` unit을 원자적으로 쓴다: `Type=simple`, `ExecStart=<sys.executable> -I -c <bootstrap> _hub --foreground --mode systemd`(각 인자는 systemd 문법으로 따옴표·escape하고 `%`→`%%`, `$`→`$$`), `Restart=on-failure`, `RestartSec=5`, `[Install] WantedBy=default.target`. 그다음 `systemctl --user daemon-reload`, `systemctl --user enable omp-workbench-hub.service`. 중간에 실패하면 unit을 이전 상태로 되돌리고 systemctl stderr와 함께 exit 1. 지금 실행 중인 Hub는 바꾸지 않는다.
- Linger: `enable`은 linger를 바꾸지 않는다. `loginctl show-user <uid> -p Linger` 결과를 보이고, `no`이면 `로그인 전(부팅 직후)에 시작하려면 'loginctl enable-linger'가 필요합니다. 이 명령은 그것을 바꾸지 않습니다.`를 보인다("열린 결정" 3).
- `disable`: `systemctl --user disable …`, unit 삭제, `daemon-reload`. 실행 중인 Hub는 멈추지 않는다고 알린다. unit이 없으면 `이미 꺼져 있습니다` exit 0.
- 표시: `wb hub status`의 부팅 자동 시작은 `systemctl --user is-enabled` 결과(enabled/disabled), 확인할 수 없으면 unknown.
- systemd가 Hub를 띄울 때 이미 다른 Hub가 있으면 lock에서 져 exit 0이고 재시작하지 않는다. `wb hub stop`·`wb update`의 정상 정지도 exit 0이라 systemd가 다시 띄우지 않는다.
- 기본값(HB-AC-04): 설치·`wb` 어느 것도 unit을 만들지 않는다.

### `wb update`와의 관계(HB-D14, 기술 보완)

`update._run`에서 `refuse_managed_caller` 다음, 진행 중 작업 확인 문답 단계에서 Hub를 다룬다.
1. Hub가 `running`이면 `Hub(pid N, version V)를 멈춥니다`를 확인 목록에 더하고 `stop_request`의 `active`(B에서는 빈 목록)를 같은 문답에 넣는다. Hub 정지만으로는 문답을 새로 만들지 않는다(진행 중 작업이 없으면 지금처럼 묻지 않는다).
2. 백엔드 종료(`shutdown(...)`)보다 먼저 `stop_hub`를 실행한다. 실패하거나 Hub가 `unknown`이면 어떤 backend도 종료하지 않고 업데이트를 중단한다(exit 1, `wb hub stop` 안내).
3. Hub를 멈춘 뒤에는 update가 잡은 배타 fence 때문에 다른 `wb`가 Hub를 다시 띄우지 못한다(spawn은 공유 fence가 필요하다).
4. 업데이트가 끝나도 Hub를 다시 띄우지 않는다. 다음 자동 시작 대상 `wb`가 새 설치본으로 Hub를 띄우고 `wb hub status`가 새 version을 보인다(HB-AC-18).
5. `require_no_writers`·backup은 Hub 정지 뒤에 일어난다((A)의 inbox writer를 위한 순서).

## 상태 전이, 실패와 경계 동작

- Hub 상태: `not_running` → (spawn, ready `R`) → `running` → (stop 2단계 / SIGTERM / watchdog / 비정상 종료) → `not_running`. 시작 직후 record 전 짧은 구간은 `unknown`이고 호출자는 기다린다.
- Hub 시작 실패의 대표 원인: update 진행 중(`update_in_progress`, 시작 건너뜀), 관리 tree(`managed_tree`, 시작 보류), 상태·runtime 디렉터리 안전성 오류(`DataDirError`, 시작 실패, 명령은 계속), socket 경로 길이(fallback 뒤에도 길면 실패).
- Registry: 손상이면 읽기는 `registry_invalid`, 쓰기는 거절. lock 경합은 5 s 뒤 `registry_busy`.
- 목록 관측: 항목별 2 s. 넘으면 그 행만 `unknown/timeout`이다. 다른 행에는 영향이 없다.
- 끄기 중 작업 변경: 거절하고 새 목록으로 다시 묻는다(최대 5회, 넘으면 "작업이 계속 바뀌어 끄지 않았습니다"로 끝, 아무것도 멈추지 않음).
- 진입 직전 backend가 꺼졌으면 `run_product`가 1을 돌려주고 목록에 notice가 뜬다.
- Hub 비정상 종료 중 목록 TUI: "Hub 연결 없음"을 보이고, `r`이나 다음 polling에서 자동 시작 규칙에 따라 새 Hub를 띄워 다시 연결한다(관리 tree 안이면 띄우지 않는다).

## 구현 결정

### 합의된 제약(BRIEF)

HB-D01~D15 전부. 특히 Hub는 backend의 부모·감독자가 아니다, WB 프로젝트당 backend 하나의 lock이 최종 기준이다, 끄기는 기존 종료 경로만 쓴다, 관측 실패는 꺼짐이 아니다, `wb hub enable` 기본 꺼짐, Hub는 tmux 등에 의존하지 않는다(ADR-0001).

### 기술 보완(이 문서가 정함)

1. Hub 상태는 registry 파일 + 매번 관측이다. Hub는 프로젝트 상태를 cache 외에는 기억하지 않으므로 재시작해도 목록이 그대로 맞다(HB-AC-02).
2. Registry writer는 lock을 잡은 어느 `wb` 프로세스든 된다. Hub를 단일 writer로 두지 않는 이유: Hub가 없거나 시작할 수 없는 상황(관리 tree, update 중)에서도 첫 시작 자동 등록이 되어야 한다.
3. Hub ↔ backend는 ui_v1 attach 없는 연결 + polling. 새 channel 없음.
4. 켜기는 이번 범위에서 TUI `wb` 프로세스가 실행한다(환경이 사용자 terminal의 것과 같다). Hub 쪽 켜기(웹)는 (A)가 같은 라이브러리로 연다.
5. Hub event loop는 asyncio다((A)의 aiohttp를 같은 loop에 붙이기 위해).
6. 관리 tree 자동 시작 보류 + Hub 자기 확인.
7. `wb init`, `wb --version`을 새로 둔다(BRIEF HB-AC-05·15·17이 존재를 전제한다).
8. `WORKBENCH_HUB_AUTOSTART=0` opt-out.

### 모듈 배치

| 모듈 | 내용 |
|---|---|
| `src/workbench/contracts/hub_v1.py` | 버전, op·reason 상수, line framing encode/decode, 크기 한도 |
| `src/workbench/hub/paths.py` | 상태·runtime·config·unit 위치, 디렉터리 안전성 |
| `src/workbench/hub/process.py` | `HubIdentity`, `hub.json`, `managed_ancestor`, `spawn_hub`, `ensure_hub`, `stop_hub`, 환경 정리 |
| `src/workbench/hub/server.py` | `_hub` main, asyncio 제어 server, watchdog, signal, 정지 |
| `src/workbench/hub/client.py` | `HubClient`(신원 확인, 요청) |
| `src/workbench/hub/registry.py` | registry 읽기·쓰기·`ensure`·`remove` |
| `src/workbench/hub/status.py` | `check_entry`, 행 관측·집계, cache |
| `src/workbench/hub/presentation.py` | label·열 서식(TUI·`hub list`·(A) 웹 공용) |
| `src/workbench/hub/projects.py` | `power_on`, `power_off`, `pause`, `resume`, `confirm_boot` |
| `src/workbench/hub/service_unit.py` | systemd unit 생성·enable/disable·상태 |
| `src/workbench/hub/cli.py` | `wb hub …`, `wb init` handler |
| `src/workbench/ui/hub_list/{model,view,app}.py` | TUI 목록 |
| 변경: `src/workbench/backend/cli.py` | parser(`hub`, `init`, `_hub`, `--version`), `main()` 자동 시작·bare 분기, `_cmd_start` 자동 등록, shutdown 대기 loop 함수화 |
| 변경: `src/workbench/backend/ui_server.py`, `src/workbench/contracts/ui_v1.py` | 비attach `pause`/`resume` |
| 변경: `src/workbench/backend/update.py` | Hub 정지 |

## 수락 기준

BRIEF의 HB-AC-01~19를 그대로 쓰고, 기술 보완에 대한 기준 HB-T01~T08을 더한다. "증거"는 `runtime`(실제 프로세스·socket), `fixture`(계약 fixture backend·fake systemctl), `static`(문서·서식), `pending`(이 개발 host에서 실행할 수 없음, `not_run`으로 보고)이다.

| ID | 관측 | 책임 ticket | seam | 증거 | 최종 통합 |
|---|---|---|---|---|---|
| HB-AC-01 | Hub 없을 때 자동 시작, PTY 닫아도 생존, 동시 8개 `wb` 뒤 Hub 1개·같은 pid | HB-02 | S1 | runtime | HB-11 |
| HB-AC-02 | `wb hub stop` 뒤 backend 생존, 새 Hub의 `list`에 `on` | HB-07 | S1, S6 | runtime | HB-11 |
| HB-AC-03 | Hub `SIGKILL` 뒤 backend·pane 생존, 프로젝트 안 `wb status`/attach 동작 | HB-07 | S6 | runtime | HB-11(PTY attach) |
| HB-AC-04 | 기본 설정에서 unit 없음·자동 시작 없음, `enable`/`disable`의 unit·systemctl 호출, 실패 표시 | HB-10 | S1(fake systemctl) | fixture | HB-11: 실제 재부팅 `pending` |
| HB-AC-05 | `wb init`·첫 `wb start`·이미 실행 중 연결이 registry에 등록, `wb hub add` | HB-04 | S1, S3 | runtime | HB-11 |
| HB-AC-06 | 등록·시작하지 않은 `.wb`는 `list`에 없다 | HB-05 | S1 | runtime | HB-11 |
| HB-AC-07 | `remove` 뒤 `.wb` bytes 보존, 재등록 뒤 같은 project_id·이력 | HB-04 | S1, S3 | runtime | HB-11 |
| HB-AC-08 | 켜기 → backend가 화면 없이 시작, `list`가 `on`, 이미 켜졌으면 두 번째 backend 없음, Hub·TUI 종료 뒤 생존 | HB-07 | S6 | runtime | HB-11 |
| HB-AC-09 | 끄기: 목록 표시·확인, 거절이면 아무것도 멈추지 않음, 작업 변경 시 거절·새 목록 | HB-07(흐름), HB-08(TUI action) | S6, S4, S7 | runtime, fixture | HB-11 |
| HB-AC-10 | 밖의 `wb` → 목록, 진입, detach → 목록 복귀, 프로젝트 계속 실행 | HB-08 | S7 | fixture | HB-11(S8 PTY) |
| HB-AC-11 | 안(하위 폴더 포함)의 `wb` → 바로 연결 + Hub 자동 시작 | HB-02 | S1 | runtime | HB-11 |
| HB-AC-12 | 행 필드·열·label, 관측 실패는 `unknown`(꺼짐 아님) | HB-05 | S4, S1 | fixture | HB-11(실제 backend 필드) |
| HB-AC-13 | 경로 없음·`.wb` 없음·marker 손상·id 불일치 → `mismatch`, 켜기·진입 거절 | HB-05(표시), HB-07(켜기 거절), HB-08(진입 거절) | S4, S6, S7 | fixture | HB-11 |
| HB-AC-14 | 같은 root·같은 id 중복 등록 없음, 하위 폴더 add·중첩 init 거절 | HB-04 | S1, S3 | runtime | — |
| HB-AC-15 | 예외 명령 뒤 Hub 프로세스 없음, `wb hub status` → `Hub 실행 중 아님` exit 3 | HB-02(목록 전체), HB-10(`enable`/`disable`/`passwd`/`tls`/`--version`) | S1 | runtime | HB-11 |
| HB-AC-16 | 꺼진 행 `Enter` → "켤까요?", `y`만 켜기+진입, 그 밖은 목록 유지 | HB-08 | S7 | fixture | HB-11(S8) |
| HB-AC-17 | 켜진 프로젝트 `remove` 거절·registry 불변, 미초기화 `add` 거절·`wb init` 안내·`.wb` 미생성 | HB-04 | S1 | runtime | — |
| HB-AC-18 | `wb update`가 설치 전에 Hub를 멈춤, Hub 정지 실패면 backend 종료 전 중단, 업데이트 뒤 Hub 없음 | HB-09 | S1(update fixture) | fixture | HB-11: 실제 재설치 뒤 새 version 표시 `pending` |
| HB-AC-19 | `pending_requests: null`, 표시 `—`(0 아님) | HB-05 | S4, S1 | fixture | HB-08(TUI 열) |
| HB-T01 | 관리 tree(가짜 `_backend` 조상, subreaper supervisor 조상) 안의 자동 시작·`wb hub start`는 spawn하지 않음, Hub 자기 확인은 lock 전에 exit 3 | HB-03 | S1, S9 | runtime | HB-11(실제 host shell 안 `wb`) |
| HB-T02 | attach 없는 `workbench-hub`의 `pause`/`resume` 수락, 다른 client 이름·다른 uid는 `not_attached`, attach 자리 불변, attach된 UI에 `state` push | HB-06 | S5, S6 | fixture, runtime | HB-11 |
| HB-T03 | 단일 instance: lock 경로 inode가 바뀌거나 상태 디렉터리가 사라지면 Hub 종료, stale `hub.json` 무시 | HB-01 | S1, S2 | runtime | — |
| HB-T04 | Hub 환경에 `WORKBENCH_DATA_DIR`·`WORKBENCH_G3_*` 등 없음, cwd `/`, stdin `/dev/null` | HB-02 | S1 | runtime | — |
| HB-T05 | 손상 registry: 읽기 `registry_invalid`, 쓰기 거절·bytes 불변, 동시 쓰기 유실 없음 | HB-04 | S3 | runtime | — |
| HB-T06 | Hub 실행 중에도 `maintenance.discover()`·`require_no_writers`가 Hub를 backend·writer로 보지 않음 | HB-09 | S1 | runtime | — |
| HB-T07 | hub_v1: hello·버전·크기 한도·모르는 op·uid 불일치·연결 상한·stop token 불일치 | HB-01 | S2 | runtime | — |
| HB-T08 | `wb --version` 출력과 exit 0, Hub 미시작 | HB-10 | S1 | runtime | — |

## 테스트 결정

**아래 seam은 제안이다. CLAUDE.md TDD 규칙에 따라 orchestrator가 사용자 확인을 받은 뒤 테스트를 쓴다.** S4·S5·S9는 기존 테스트 패턴을 따르는 조정이고, 새로 만드는 것은 S2(hub_v1)와 S7(TUI 목록 model)이다.

| seam | 대상 | 잡는 것 | 놓치는 것 | 독립 기대값 출처 |
|---|---|---|---|---|
| S1 | CLI subprocess(`python -m workbench …`, temp HOME·`XDG_STATE_HOME`·`XDG_RUNTIME_DIR`·`XDG_CONFIG_HOME`, `tests/backend/installation/test_project_cli.py` 패턴) | 명령 분기, 자동 시작·예외, exit code·문구, registry 효과, Hub 프로세스 존재(`/proc`, lock holder), 동시 실행, PTY 닫기 생존 | TUI 화면, 실제 OMP | BRIEF 문구·예외 목록, 테스트가 `/proc`·`/proc/locks`에서 직접 읽은 pid, 파일 bytes |
| S2 | hub_v1 socket(실제 `_hub --foreground` 프로세스에 테스트가 직접 socket으로 말함) | framing·hello·버전·한도·uid 검사·stop 2단계·`list`/`status` 결과 형태 | 화면, backend 실물 | 이 SPEC의 프로토콜 표 |
| S3 | `workbench.hub.registry` API + `registry.json` bytes(여러 프로세스 동시) | 원자성·유일성·멱등·손상 거절·`.wb` 보존 | Hub 통합 | 테스트가 쓴 입력과 파일 bytes |
| S4 | 계약 fixture backend(`test_project_cli.py`의 delayed-backend처럼 `backend.lock`·`backend.json`·`runtime.json`을 만들고 테스트가 준 snapshot을 ui_v1로 답하는 프로세스) + `wb hub list --json` | `power`·`waits`·`task`·`command`·`last_activity` 매핑, timeout·신원 불일치 → `unknown`, `mismatch` 4종, `—` | 실제 backend 필드 drift(→ S6) | 이 SPEC의 매핑 표, 테스트가 준 snapshot |
| S5 | 실제 `UiServer` + 가짜 controller(`tests/backend/test_ui_server.py` 패턴). peer uid 불일치는 연결 peer 신원을 읽는 메서드를 덮어쓴 하위 클래스로 주입 | 비attach `pause`/`resume` 허용 조건, attach 자리·push 불변, `reconciled` 규칙 | automation 실제 동작 | ui_v1 계약 문구 |
| S6 | 실제 backend + fake OMP(`tests/recovery_boot/fake_omp.py`, `test_live_restart_entrypoint.py:65-96` 환경). `.wb` 프로젝트를 라이브러리로 초기화하고, 라이브러리 호출은 테스트가 띄운 driver subprocess에서 한다(test runner 안에서 fork하지 않음) | 켜기·끄기·작업 변경 거절·`pause`/`resume`/`confirm_boot`, Hub 정지·`SIGKILL` 뒤 생존, 실제 snapshot 필드 | 실제 OMP·모델 | `/proc`의 pid·start_ticks·ppid, snapshot 값, BRIEF |
| S7 | `workbench.ui.hub_list.model`(순수) + `presentation` | key → action, modal(켤까요?), 상태별 허용 동작, 열·label·`—` | curses 그리기·실제 진입 | BRIEF HB-AC-10·13·16, 이 SPEC label 표 |
| S8 | PTY e2e(`pty.fork`로 프로젝트 밖 `wb`, S6 backend) | 목록 → 진입 → detach → 목록, 켤까요? → 켜기 → 진입 | 다양한 terminal 종류 | 화면 문자열, `/proc` |
| S9 | 관리 tree fixture: argv에 `_backend`·`workbench`가 있는 가짜 조상, `PR_SET_CHILD_SUBREAPER`를 켠 `--supervisor` 모양 조상 아래에서 `wb`·`_hub` 실행 | 사전 확인·자기 확인·재부모화 대상 | 실제 실험 run 경로(→ HB-11) | 테스트가 만든 조상 pid, `/proc`의 ppid |

원칙: 각 ticket은 해당 seam에서 behavior 하나씩 RED(의도한 behavior 부재로 실패) → 최소 GREEN → 다음 behavior 순서로 진행한다. 프로세스 수명·lock·spawn·관리 tree·ui_v1 계약·신원 ticket(HB-01, 02, 03, 05, 06, 07, 09)은 독립 test_designer가 요구에서 시나리오를 먼저 도출해 확인·보강한다(`*_independent*` 파일은 test_designer 소유). 최종 통합 전 ticket마다 단위 review, 최종 통합은 Standards/Spec 두 fresh reviewer와 별도 security review(새 local socket, 프로세스 신원, 비attach 제어가 trigger)를 받는다.

테스트 위생:
- Hub를 띄우는 테스트는 끝에 `wb hub stop`(또는 lock holder pid에 `SIGTERM`)으로 정리하고 프로세스 부재를 확인한다. watchdog은 안전망일 뿐이다.
- 기존에 CLI subprocess를 temp HOME에서 띄우는 테스트(현재 35개 파일, `grep -rln "'-m', 'workbench'\|\"-m\", \"workbench\"" tests`)는 자동 시작 대상 명령을 쓰면 Hub를 남긴다. HB-02에서 공용 env helper(`tests/backend/live_harness.py`의 `sandbox_env`, `tests/backend/installation/test_project_cli.py`의 `self.env`, `tests/recovery_boot/test_live_restart_entrypoint.py`, `tests/integration/cw16_harness.py` 등 `_independent`가 아닌 파일)에 `WORKBENCH_HUB_AUTOSTART=0`을 넣는다. `*_independent*` 파일은 test_designer가 HB-11에서 같은 처리를 한다.
- 프로젝트 초기화는 테스트 안에서 `initialize_project(..., interactive=True, confirm=lambda root: True)`로 하거나 PTY로 `wb init`에 답한다.

실행 명령(repo 루트, 공통 접두사 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python`, `-t .` 없음):
- Hub 프로세스·프로토콜: `… -m unittest discover -s tests/hub -p 'test_hub_process*.py'`, `-p 'test_hub_protocol*.py'`
- spawn·자동 시작: `… -m unittest discover -s tests/hub -p 'test_hub_spawn*.py'`
- 관리 tree: `… -m unittest discover -s tests/hub -p 'test_hub_managed*.py'`
- registry·init·add/remove: `… -m unittest discover -s tests/hub -p 'test_registry*.py'`, `-p 'test_hub_cli_registry*.py'`
- 상태 집계: `… -m unittest discover -s tests/hub -p 'test_hub_list*.py'`
- ui_v1: `… -m unittest discover -s tests/backend -p 'test_ui_server_hub_control*.py'`, `… -m unittest tests.contracts.test_ui_v1`
- 프로젝트 제어(S6): `… -m unittest discover -s tests/hub -p 'test_project_control*.py'`
- TUI model: `… -m unittest discover -s tests/ui -p 'test_hub_list_model*.py'`
- update: `… -m unittest discover -s tests/backend/maintenance -p 'test_update_hub*.py'`, 회귀 `-p 'test_update_*.py'`
- systemd·version: `… -m unittest discover -s tests/hub -p 'test_hub_service*.py'`
- 회귀(관련 모듈만, 넓은 실행은 HB-11 한 번): `tests/backend/installation`(`-p 'test_project_cli*.py'`), `tests/backend`(`-p 'test_ui_server*.py'`), `tests/backend/maintenance`, `tests/recovery_boot`(`-p 'test_live_restart_entrypoint.py'`), `tests/contracts`.
- 통합(HB-11): `… -m unittest discover -s tests/integration -p 'live_hub*.py'`. `tests/gates`는 쓰지 않는다.

TDD 예외: `ui_v1.py` docstring, README·CONTEXT·USAGE 문서(HB-12)는 문서성 변경이라 RED를 만들지 않고 정적 확인(필수 문구·명령 이름이 SPEC과 일치)으로 검증한다. curses 그리기(`view.py`)는 model seam과 S8 PTY로 확인하고 별도 RED를 만들지 않는다.

## 호환성, 통합, 롤아웃 제약

- **bare `wb`의 동작이 바뀐다**: 프로젝트 밖에서 지금은 그 폴더를 초기화할지 묻고 시작하지만, 이제 Hub 목록을 띄운다(HB-D08). 새 프로젝트는 `wb init` 또는 명시 `wb start`로 만든다. WI-AC-05·WI-AC-13(설치 SPEC)의 "`wb` 무인자는 start"는 프로젝트 안에서만 유지된다.
- 나머지는 추가다: 새 명령·새 파일·ui_v1 허용 범위 확장(옛 client 영향 없음). `backend.json`·`.wb` 구조·SQLite·설치 manifest는 바꾸지 않는다.
- Hub와 `wb`는 같은 설치본이다. 다른 설치본의 Hub가 떠 있으면 `wb hub status`가 version drift를 경고한다(자동으로 멈추지 않는다).
- 이미 실행 중인 옛 backend는 비attach `pause`/`resume`을 거절한다(`backend_too_old`). `wb update`가 모든 backend를 재시작하므로 정상 경로에서는 생기지 않는다.
- systemd unit은 사용자가 `wb hub enable`을 실행할 때만 생긴다(WI-P07 유지).
- 실제 사용자 데이터·실제 연구 process·유료 provider·Docker·새 도구 설치는 쓰지 않는다. 테스트에서 실제 `systemctl --user enable`을 실행하지 않는다(fake `systemctl`). 운영 배포·commit/push는 별도 권한이다.

## (A) 확장 지점(이번 범위에서 막지 않을 것)

1. 웹 서버: Hub의 asyncio loop에 aiohttp app을 붙인다. 비밀번호가 없으면 listener를 열지 않고 `status.web.state`가 이유를 보인다(P2-AC-15). `hub.toml`은 (A)가 읽고 setup lock을 잡지 않는다.
2. Inbox writer: Hub가 `<root>/.wb/inbox.sqlite3`의 유일한 writer다(ADR-0005). `wb update`가 Hub를 backup 전에 멈추는 순서가 이미 있다. inbox backup 범위(Q-11)는 (A)가 정한다.
3. Board agent PTY: Hub의 자식으로 띄운다(어느 backend의 후손도 아니다). `stop_request.active`에 세트를 넣어 `wb hub stop`·`wb update`가 확인을 받고 끝낸다. `SIGTERM` 경로에서는 확인 없이 끝낸다. 다음 시작에서의 복원은 HB-O09로 (A)가 정한다. Board agent 안의 `wb`는 조상이 Hub이므로 관리 tree가 아니다.
4. 인증·신원: hub_v1 handler의 `principal`에 웹 세션·API key principal을 더한다. 다른 editor key는 Board agent와 같은 요청 권한이다(사용자 사전 위임).
5. 목록: `pending_requests`를 inbox에서 채운다. label은 `workbench.hub.presentation`을 웹도 쓴다.
6. 웹 제어: hub_v1 예약 op와 `workbench.hub.projects`(켜기·끄기·`pause`·`resume`·`confirm_boot`)를 그대로 쓴다. Hub 프로세스에서 켜면 backend가 Hub 환경을 물려받는다(systemd 모드는 PATH 등이 좁다). 이 차이를 어떻게 다룰지는 (A)가 정한다.

## 열린 결정·위임된 선택

**2026-10-25 orchestrator 확정(사용자 사전 위임)**: 아래 1~6을 모두 이 문서의 안대로 확정한다. 4의 `WORKBENCH_HUB_AUTOSTART=0`은 개발·테스트용 opt-out으로 HB-12에서 문서화한다. 5는 HB-12에서 설치 문서(WI-AC-05/13)를 bare `wb` 밖 = Hub 목록으로 갱신한다. 6의 seam S1~S9를 확정한다. 추가 안전장치: Hub는 자기 상태 디렉터리(`hub.json`이 있는 디렉터리)가 사라지거나 다른 inode로 바뀌면 스스로 종료한다(temp HOME을 쓰는 테스트가 Hub를 남기지 않게 하는 이중 장치, HB-02).

사용자 확인이 필요한 항목:
1. **관리 tree 안 자동 시작 보류**(HB-T01): HB-AC-01·HB-D11에 없는 예외다. orchestrator가 사전 위임 아래 위험 해소책으로 정했다. 대안(systemd-run 등으로 tree 탈출)은 systemd를 기본 경로에 끌어들여 HB-D05와 맞지 않아 택하지 않았다.
2. **`wb init` 새 명령**: BRIEF가 존재를 전제하지만 지금 없다. TTY 확인만 받고 `--yes`는 두지 않는다(WI-AC-05의 사용자 확인 유지. (A)의 Board agent는 PTY에서 답할 수 있다). 중첩 초기화는 거절한다.
3. **`wb hub enable`의 linger**: linger를 바꾸지 않으므로 기본적으로 "부팅 뒤"가 아니라 "로그인 뒤" 시작이다. HB-AC-04의 "부팅 뒤 사용자 서비스로 시작"은 사용자가 linger를 켠 경우에만 문자 그대로 성립한다.
4. **`WORKBENCH_HUB_AUTOSTART=0`**: 기본 동작은 그대로인 opt-out이다. 테스트 위생에 필요하다. 사용자 문서에 적을지(HB-12) 확인한다.
5. **bare `wb` 밖 동작 변경**에 따른 WI-AC-05/13 문구 정리(설치 문서 갱신 여부).
6. **seam S1~S9 확정**(TDD 규칙).

위임된 선택(사용자 확인 불필요): 필드·상수·reason 이름, thread·executor 구성, cache 시간(2 s)·polling 주기(3 s)·timeout 값, 정확한 안내 문구, log 회전 크기, TUI key 배치(`o`/`x`/`r`).

관찰했지만 범위 밖인 위험: 실험 run(supervisor) 안에서 `wb start`로 **다른 프로젝트의 backend**를 띄우면 그 backend도 supervisor로 재부모화된다(기존 동작, Hub와 무관). 별도 BRIEF가 필요하다.

## Ticket 그래프

이번 판은 PLAN.json을 만들지 않았다. 아래 표가 의존성의 정본이며, ticket Markdown은 이 표를 참조한다. Executor는 Claude orchestrator가 배정하는 subagent이며 모델은 CLAUDE.md 기준이다.

| ID | 제목 | 선행 | 모델 | 주요 쓰기 범위(추정) |
|---|---|---|---|---|
| [HB-01](tickets/HB-01.md) | Hub 프로세스·신원·hub_v1·`wb hub status/stop` | — | Opus | `contracts/hub_v1.py`, `hub/{paths,process,server,client}.py`, `backend/cli.py`(parser) |
| [HB-02](tickets/HB-02.md) | spawn·자동 시작·단일 instance·`wb hub start` | HB-01 | Opus | `hub/process.py`, `backend/cli.py`(`main`), 공용 test env helper |
| [HB-03](tickets/HB-03.md) | WB 관리 process tree 안 자동 시작 보류 | HB-02 | Opus | `hub/process.py`, `hub/server.py` |
| [HB-04](tickets/HB-04.md) | registry·자동 등록·`wb init`·`wb hub add/remove` | HB-01 | Sonnet | `hub/{registry,cli}.py`, `backend/cli.py`(`_cmd_start`, parser) |
| [HB-05](tickets/HB-05.md) | 상태 집계·hub_v1 `list`·`wb hub list` | HB-04 | Opus | `hub/{status,presentation,server}.py`, `hub/cli.py` |
| [HB-06](tickets/HB-06.md) | ui_v1 attach 없는 `pause`/`resume` | — | Opus | `backend/ui_server.py`, `contracts/ui_v1.py` |
| [HB-07](tickets/HB-07.md) | 프로젝트 제어 라이브러리(켜기·끄기·pause·resume·confirm-boot) | HB-05, HB-06 | Opus | `hub/projects.py`, `backend/cli.py`(shutdown 대기 함수화) |
| [HB-08](tickets/HB-08.md) | TUI Hub 목록·bare `wb` 밖 분기 | HB-03, HB-07 | Sonnet | `ui/hub_list/*`, `backend/cli.py`(`main`) |
| [HB-09](tickets/HB-09.md) | `wb update`의 Hub 정지 | HB-02 | Opus | `backend/update.py` |
| [HB-10](tickets/HB-10.md) | `wb hub enable/disable`, `--version`, 예약 명령 | HB-02 | Sonnet | `hub/service_unit.py`, `hub/cli.py`, `backend/cli.py`(parser) |
| [HB-11](tickets/HB-11.md) | 최종 통합·live·독립 검증 | HB-08, HB-09, HB-10 | Sonnet(검증), 결함 수정은 해당 ticket 모델 | `tests/integration/live_hub*.py`, `docs/features/workbench-hub/VERIFICATION.md` |
| [HB-12](tickets/HB-12.md) | 문서(README·CONTEXT·USAGE) | HB-11 | Sonnet | `README.md`, `CONTEXT.md`, `docs/features/workbench-hub/USAGE.md` |

의존 이유: HB-02는 HB-01의 `_hub`·identity를 띄운다. HB-03은 HB-02의 spawn 경로에 확인을 넣는다. HB-04는 HB-01이 만든 `hub` parser group을 쓴다. HB-05는 registry(HB-04)와 제어 server(HB-01)를 쓴다. HB-07은 `check_entry`(HB-05)와 비attach 제어(HB-06)를 쓴다. HB-08은 `list`(HB-05), 제어(HB-07), 자동 시작·관리 tree 규칙(HB-02·03)을 쓴다. HB-09·HB-10은 `stop_hub`·`_hub --foreground`(HB-01·02)를 쓴다.

초기 frontier: HB-01, HB-06. 그 뒤 병렬 후보: {HB-03, HB-04, HB-09, HB-10}(HB-02 뒤), HB-06은 내내 독립. 배타 자원: `src/workbench/backend/cli.py`는 HB-01·02·04·07·08·10이 함께 쓰므로 동시에 쓰지 않는다. `hub/process.py`는 HB-01·02·03, `hub/server.py`는 HB-01·03·05, `hub/cli.py`는 HB-04·05·10이 쓴다.
