# Workbench 실행 명령 개별 stop — 구현 명세

Revision: s1; source brief: [BRIEF.md](BRIEF.md) r0.2 (승인 기록 `.workflow/command-stop/brief-approval.json`, 사용자 승인 2026-10-21). 이 문서는 **확인용 초안**이다. 승인 정본은 별도 `approval.json`과 실제 사용자 답변이다. 구현 baseline은 HEAD `53023c8`(`src/`, `omp_bridge/`, `tests/`에 미커밋 변경 없음)이다.

BRIEF의 사용자 결정(CS-D01~17)과 수락 예시(CS-AC-01~23)는 바꾸지 않는다. 이 문서의 "기술 보완"은 승인된 범위 안에서 코드 조사로 정한 구체 형태이며 사용자 노출 동작을 넓히거나 줄이지 않는다. 사용자가 확인한 항목은 "확정된 결정"에 모았다.

## 문제와 해결 방향

2026-10-21 사건에서 free-work Task를 취소해도 host 명령이 약 9분 35초 더 돌았고, 사용자 Ctrl+C·Worker·점유된 host terminal 모두 명령을 멈추지 못했다. 해결은 세 부분이다.

1. 취소 응답과 상태가 실행 중 명령을 드러내고 "취소는 중지가 아님"을 알린다(CS-D01).
2. Worker와 Manager가 terminal 점유와 무관하게 `command_id`로 명령 하나를 stop한다. 신호는 shell 입력이 아니라 백엔드가 pidfd로 신원을 고정한 프로세스에 직접 보낸다(CS-D02~03, 06~07, 11~15).
3. Workbench 실행 명령이 foreground일 때 사용자의 단독 Ctrl+C를 입력 owner·인수와 무관하게 그 명령에 전달한다(CS-D04, D17).

## 관련 사용자 이야기

- 사용자: 취소했다고 믿었는데 계속 도는 명령을 보고, Ctrl+C로 멈추고, Manager에게 멈추라고 시킬 수 있다. (2026-10-24 후속: `prefix x`로 직접 멈출 수도 있다. 멈춘 실험은 `stopped_by: user`다. [FOLLOWUPS-BRIEF.md](FOLLOWUPS-BRIEF.md) FU-D06~08)
- Worker: 자기 명령이 멎지 않거나 사용자가 멈추라고 하면 `terminal` 도구로 직접 멈춘다. 재시작된 새 Worker도 이전 Worker의 명령을 멈춘다.
- Manager: 승인 절차 없이 Worker의 명령과 자기 실험 명령을 멈추고 최종 상태를 받는다. 멈춘 실험은 `stopped`로 구분된다.

## 범위와 비목표

BRIEF "범위와 비목표"를 따른다. 대상은 Workbench가 `command_id`로 추적하는 host 명령(Worker `terminal` 명령, Manager 실험 host 명령)뿐이다. 비목표: Task 취소가 명령을 죽이게 하는 것, 임의 PID kill, 사용자용 stop UI, 협력적 취소 token, CS-O01~03·O10 보류 항목. (2026-10-24 후속: "사용자용 stop UI"는 해제됐다. CS-O01·O02·O10은 [FOLLOWUPS-BRIEF.md](FOLLOWUPS-BRIEF.md)가 해소했고 CS-O03만 보류다.) 지원 환경은 core와 같은 Linux PTY persistent shell(Python 3.12, `os.pidfd_open`/`signal.pidfd_send_signal`)이고 다른 uid 프로세스에는 신호를 보내지 않는다. 이번 기능은 SQLite schema·dependency·설치 방식을 바꾸지 않는다.

## 공개 인터페이스, 데이터 계약, 불변 조건

### 식별자와 소유권

- `command_id`: Worker `terminal` 명령은 기존 `command_id`(uuid4, `workflow/terminal/<id>.log`). Manager 실험의 host 명령은 `run_id`를 `command_id`로 쓴다(별도 id를 만들지 않는다). Manager는 `workbench_status`, 취소 응답, `/task`의 `host_commands`에서 id를 얻고, Worker는 `terminal` 결과·`terminal_check`/`terminal_done` 통지·`commands_run`(이번에 `command_id` 추가)에서 얻는다.
- command_id 형식: Worker `terminal` stop과 Manager `stop_command`가 같은 엄격한 canonical 소문자 uuid 패턴 하나(`command_stop_owner.py`의 `COMMAND_ID`, 브리지 `UUID_PATTERN`과 같음: 버전 `[1-8]`, variant `[89ab]`)를 쓴다. 패턴 밖 값은 `invalid_arguments`로 거절하고 값은 응답에 싣지 않는다.
- 소유자는 역할이다: `terminal` 명령 = `worker`, 실험 명령 = `manager`. OMP 인스턴스·session은 소유권에 쓰지 않는다(CS-D08).
- 요청자: `worker`, `manager`, 그리고 Ctrl+C의 `user`. 그 밖의 요청자는 없다. (2026-10-24 후속: `user`는 `prefix x`의 명령 stop 요청자이기도 하다. [FOLLOWUPS-BRIEF.md](FOLLOWUPS-BRIEF.md) FU-D07)

### Worker: `terminal` 도구의 `stop` 인자

- 입력 형태 `{command: string|null, stop?: string|null}`. `stop`은 `command_id`다(아래 형식 규칙). 비어 있거나 null이면 쓰지 않은 것으로 본다(strict schema 관례: 빈 문자열도 "미사용"). `stop`과 문자열 `command`를 함께 주면 `invalid_arguments`(필드 이름만, 값은 싣지 않음), 아무 일도 하지 않는다. `command: null, stop: null`은 기존 상태 조회, 기존 `command`만 있는 호출은 그대로다.
- stop 호출은 새 명령 실행이 아니다. `terminal_command_running`·`host_terminal_busy`·`paused`·`held`·`not_in_task_commands` 검사를 거치지 않고, host terminal에 아무것도 입력하지 않는다. stop 호출은 `_calls`/waiter에 등록되지 않아 명령의 `terminal_done` 통지를 소모하지 않는다(통지는 별도로 한 번 전달, CS-AC-03).
- 대기: 호출은 episode(아래)가 끝날 때까지 기다리며 상한 `CALL_WAIT=25 s`(식별 최대 3 s + 단계 20 s + 여유 2 s). 상한이 지나면 `status: "stopping"`과 진행 상태를 반환하고 episode는 계속된다(정상 경로에서는 일어나지 않음). 브리지 쪽 결과 timeout은 `STOP_RESULT_TIMEOUT_MS=45_000`이다. 이는 C-D68 (9)의 120 s 대기 안에 든다.

### Manager: `stop_command` 도구

- 입력 `{command_id: string(uuid), reason: string(1..500)}`(`command_id` 형식은 아래 "command_id 형식"), strict(둘 다 required). Manager 전용이며 Worker가 호출하면 `tool_not_allowed_for_role`. `reason`은 환경 변수 값 검사(`environment_value_findings`)를 거치고 감사 기록에 남는다(`stop_survivor` 선례). 수락 즉시가 아니라 `terminal` stop과 같은 최종 결과를 반환한다(`CALL_WAIT` 동일, 브리지 timeout 45 s). 같은 tool call key의 재전송은 캐시된 결과를 돌려준다.
- 사용자 건별 승인은 없다(CS-D06). 이 결정은 Workbench가 신원을 검증한 추적 명령에만 적용된다. 그 밖의 대상은 `stop_survivor`·C-AC-34를 따른다(CS-AC-13).

### 결과 계약(Worker·Manager 공통)

`status`와 이유 코드는 아래로 고정한다. 모든 결과에 `command_id`, `requested_by`가 있다.

| status | 의미 | 신호 |
|---|---|---|
| `exited` | 종료 확인됨. `exit_code`, `signal`(번호, 기존 필드 형식), `signal_name`(예 `SIGINT`), `signals`(보낸 신호와 시각·대상), `residual`(잔존 확인) | 보냄 |
| `unknown` | 종료 미확인. `reason`(`still_alive_after_sigkill`, `permission_denied`, `left_session_descendants`, `identity_unavailable`, `pidfd_unavailable`(`pidfd_open` 실패: EMFILE·ENOMEM 등은 신원 불일치가 아니므로 `stale`이 아니며 신호 없음), 또는 소유자가 선언한 unknown 이유). `signals`, `residual` 포함 | 보냈을 수 있음 |
| `already_exited` | 이미 끝난 command_id. 마지막 종료 상태(`exit_code`, `signal`, `ended_at`) | 안 보냄 |
| `not_found` | 모르는 command_id(`reason: unknown_command`) | 안 보냄 |
| `stale` | PID 재사용·시작 시각 불일치 등 신원 검증 실패(`reason: identity_mismatch` 등) | 안 보냄 |
| `refused` | `reason`: `not_owner`(다른 owner의 명령), `previous_backend_command`(이전 backend 시작분, `stop_survivor`로 안내), `backend_shutdown`, `registration_failed`(owner의 StopTarget 등록이 실패한 명령: 명령은 계속 실행되고 평소대로 추적·판정되지만 중지 대상이 아님. `not_owner` 판정이 먼저) | 안 보냄 |
| `stopping` | 호출 대기 상한 초과 시에만. 진행 중 episode 요약 | — |

- 같은 command_id의 반복 요청은 같은 episode에 합류하거나(진행 중) 원래의 최종 결과를 돌려준다(끝난 뒤). 결과에 `repeat: true`가 붙고 신호·타이머를 새로 만들지 않는다. 정정된 결과에는 `corrected: true`와 `history`(정정 전 `unknown`과 이유)가 있다.
- `residual`: `{checked: true, alive: [{pid,start_ticks}], left_session: [{pid,start_ticks,session}], unchecked: "host shell session 밖에서 시작된 프로세스는 확인하지 않음"}`. `unchecked`는 항상 있으며 "깨끗함"을 뜻하는 필드는 없다(CS-AC-05).
- 명령 stop으로 끝난 `terminal` 결과·`terminal_done`·`commands_run` 항목에 `stopped_by`(`worker|manager`)와 마지막 `signal`이 붙는다(기술 보완: Manager가 멈춘 명령을 Worker가 실패로 오해해 재실행하지 않게 한다). (2026-10-24 후속: 사용자가 멈춘 명령은 `stopped_by: user`다.)

### 명령 상태 표면

상태는 `running → stopping → exited | unknown`(CS-D11). `stopping`은 episode가 진행되는 동안, `unknown`은 stop 최종이 unknown이고 소유자가 아직 끝을 관측하지 못한 동안이다. 소유자가 끝을 관측하면 `exited`(정정이면 `corrected`)다.

- `TerminalService.current()`·`watch_state().last.state`(+`stop` 요약)·`terminal {command:null}` 조회가 `stopping`/`unknown`을 낸다. `watch_state().running`은 `stopping`/`unknown` 동안도 true다(watchdog이 idle로 오판하지 않는다).
- `/task`(= `snapshot()["task"]`, `workbench_status.task`)에 additive `host_commands: [{command_id, kind: "terminal"|"experiment", owner, state, started_at, log_path, stop: {...}|null}]`를 둔다. 해당 Task의 아직 끝나지 않은 명령(running/stopping/unknown)만 담고, 끝났으면 비어 있다. Task가 닫혀도(`cancelled`) 살아 있는 명령은 계속 보인다(CS-AC-02). `task_id=null` 명령은 어떤 Task에도 속하지 않는다.
- `ui_v1.py` docstring에 additive 항목으로 기록한다. 기존 필드·`TASK_STATUSES`는 바뀌지 않는다.

### 취소 응답(CS-D01)

- free-work 취소 `{status: "cancelled", task_id, worker_notified}`에 `host_commands`(실행 중인 해당 Task의 명령, 없으면 `[]`)를 더하고, 비어 있지 않을 때만 `host_command_note`("Cancel does not stop the command; stop it with terminal stop / stop_command")를 더한다. `detail`(통지 보류 문구)과는 키를 공유하지 않는다.
- 실험 취소 `cancel_requested`는 `held_reason=cancel_waiting_for_host_exit`와 `detail`을 그대로 유지하되 `CANCEL_WAIT_DETAIL`에 "취소는 명령을 멈추지 않는다, `stop_command`(command_id = run_id)로 멈춘다"를 더하고 `host_commands`를 같이 반환한다.
- 어떤 취소 응답도 `stopped`/`terminated` 등 종료를 뜻하는 문구·상태를 쓰지 않는다.

### 단계와 타이머

`StopPolicy`: SIGINT → 10 s → SIGTERM → 5 s → SIGKILL → 5 s 확인 → 미확인이면 `unknown`(CS-D03, D14). 모든 시각은 첫 SIGINT 전송 시점 t0 기준 단조 시계이고, 종료가 확인되면 즉시 끝난다. 신원 식별 대기(`identity_wait`)는 최대 3 s이며 이 20 s에 포함되지 않는다. 타이머는 episode(명령당 하나)가 가지며 호출자 수와 무관하다. 호출이 끝나도(abort·bridge timeout) episode와 `unknown` 정정 관측은 계속된다. 상수는 `StopPolicy`로 주입 가능하다(테스트는 줄인 값, 기본값 검증은 별도).

### 추적 범위와 잔존 확인(CS-D15) — 기술 보완

- 신원 기준(`RunIdentity`): control wait 중인 host shell parent, supervisor(subreaper), command child의 `pid`+`start_ticks`, `child_group`(= child pid), `session`(= parent pid), `uid`, `boot_id`. 출처는 `lifecycle` 관측(`SUPERVISOR`, `CHILD_PREPARED`, `EXEC_READY`)과 adapter가 기록한 start ticks이며 `PersistentShell.run_identity()`가 읽기 전용으로 제공한다.
- `tracked`(신호 대상): supervisor의 모든 후손(`/proc/*/stat` ppid 전수 조사; 기존 `_descendant_pids`의 `task/<tid>/children`은 비-main thread가 fork한 자식을 놓치므로 쓰지 않는다) 중 `session == host shell pid`인 것, 그리고 `process group == child_group`이고 같은 session인 것(supervisor가 이미 사라진 뒤의 재부모화 포함). 같은 uid, 살아 있음(Z/X 아님)이어야 한다. supervisor·host shell parent·backend·OMP pane pid는 어떤 경우에도 대상이 아니다(구조적으로 제외 + 명시적 금지 집합). 금지 집합은 service가 주입한 provider가 돌려주는 OMP pane pid이며 두 owner 모두 호출 시점에 평가한다. 종료 시 provider가 실패하면 마지막으로 성공한 집합(없으면 빈 집합)을 잔존 점검에 쓴다.
- `left_session`(보고만): supervisor 후손이지만 다른 session으로 옮긴(setsid) 프로세스. 신호를 보내지 않고 `residual.left_session`에 보고한다. C-D63의 host shell kill도 같은 선례로 setsid 프로세스를 대상에서 뺀다.
- 그 밖은 확인하지 않는다(`unchecked`).
- supervisor는 subreaper이고 후손이 남으면 lifetime이 `active`로 남아 소유자가 `exited`를 관측하지 못한다. 따라서 `exited`는 "main 종료 + tracked 후손 전부 종료 + 소유자의 lifetime ended 관측"일 때만 쓴다. tracked가 비었는데 `left_session` 후손만 남아 lifetime이 끝나지 않으면 신호를 더 보낼 수 없으므로 20 s를 채우지 않고 `unknown`(`left_session_descendants`, main의 `exit_code`/`signal`은 관측값으로 함께 보고)로 끝낸다. 후손이 나중에 끝나 소유자가 종료를 관측하면 CS-AC-23 정정으로 `exited`가 된다.

### 신원·신호 불변식

1. 신호는 host terminal 입력을 거치지 않는다. 백엔드 프로세스가 `os.pidfd_open` + `signal.pidfd_send_signal`로 보낸다(`killpg`/`kill(pid)` 금지: PID 재사용에 안전하지 않음).
2. 대상은 `pid`+`start_ticks`+`uid`+`boot_id`(scan이 session을 기록했으면 session도: scan 시점 session이 pin·검증·신호 직전에 같아야 하며 달라지면 `stale`)를 `/proc`에서 읽은 뒤 pidfd를 열고 다시 읽어 같음을 확인한(`panes._pin_member` 선례) 프로세스뿐이다. 매 단계 직전 tracked를 다시 조사하고, 신호 직전 각 pin의 start ticks·생존·uid를 다시 확인한다. 검증 중 바뀌면 그 대상은 건너뛰고 기록하며, 모든 대상이 검증 실패면 `stale`(시작 전) 또는 `unknown`(진행 중)이다.
3. argv·command 문자열·comm은 대상 판정이나 감사 기록에 쓰지 않는다.
4. 같은 command_id에 episode는 하나다. `exited`·`stale`·`unknown`·미등록 id에는 새 신호를 보내지 않는다.
5. 정지(T) 상태인 tracked 구성원에는 SIGINT/SIGTERM 뒤 SIGCONT를 보낸다(기록은 보조 신호로 표시).
6. 이 기능은 host shell parent·supervisor·Workbench·OMP를 종료하지 않는다(CS-AC-19). 명령이 끝나면 기존 give-back으로 shell이 사용자에게 돌아오고 로그는 보존된다.
7. 권한 오류(EPERM)·ESRCH는 구성원별로 기록하고 다른 구성원에는 계속 보낸다. tracked 전부가 EPERM이면 즉시 `unknown`(`permission_denied`).

### 인가 매트릭스(CS-D02, D06~09)

| 요청자 \ 대상 | Worker `terminal` 명령 | Manager 실험 명령 | 이전 backend 생존자 | 사용자 shell/foreground, 그 밖 프로세스 |
|---|---|---|---|---|
| worker | 허용(owner=worker, Task 상태·세션 무관) | `refused: not_owner` | `refused: previous_backend_command` | command_id가 없으므로 `not_found` |
| manager | 허용 | 허용 | `refused`(→ `stop_survivor`) | `not_found` |
| user (2026-10-24 후속, `prefix x`) | 허용 | 허용 | `refused: previous_backend_command` | `not_found` |

2026-10-24 후속: `user` 행은 [FOLLOWUPS-BRIEF.md](FOLLOWUPS-BRIEF.md) FU-D07이 더했다(`STOPPABLE_BY`). UI는 snapshot `host_shell.command`에 보인 `command_id`만 보낸다.

인가는 command_id를 등록부에서 찾아 `owner`와 비교한다. pause·boot 확인 보류(C-D71 (3))·Task 닫힘은 stop을 막지 않는다(CS-D13, D09). Task가 닫힌 뒤의 stop은 Task를 다시 열지 않고 새 명령 권한을 주지 않으며 감사 기록에 `task_id`와 `task_status`를 남긴다.

### 사용자 Ctrl+C(CS-D04, D17)

- 입력 종류 `input`의 payload가 정확히 `b"\x03"` 한 바이트이고, Workbench 실행 명령(Worker 또는 실험)이 검증된 host terminal foreground일 때 `ShellPane.admit`이 입력 owner·인수 진행 상태와 무관하게 그 바이트를 PTY master에 쓴다. 커널 line discipline이 foreground group(= `child_group`)에 SIGINT를 전달한다. paste, `\x03`을 포함한 2바이트 이상, 다른 키는 현행 규칙 그대로다(일부 바이트만 전달 금지).
- 대상 검증: `PersistentShell.interrupt_run()`이 lifecycle(실행 중, unknown 아님, main 미종료, 정지 아님)과 `/proc`(child의 start ticks·ppid=supervisor·pgid=child_group·sid=parent, `tcgetpgrp(master) == child_group`)을 쓰기 직전에 다시 확인한다. 하나라도 틀리면 전달하지 않고 현행 규칙으로 되돌린다(사용자 소유 prompt/job, 불명 상태 포함, CS-AC-17).
- 입력 owner·`takeover_requested`는 바뀌지 않는다. 경계의 `observe_*_bytes`는 호출하지 않는다(대상이 shell line이 아니다).
- 감사: `ShellPane.interrupt_listener`가 Backend에 전달 사실을 알리고 Backend가 `command_interrupt` 기록을 남긴다(요청자 `user`).
- 기술 보완: PTY에 바이트를 쓰므로 명령이 tty를 ISIG 끈 raw 모드로 바꿨다면 SIGINT가 되지 않고 입력 바이트가 된다. 이 경우는 사용자가 Manager에게 stop을 요청한다(BRIEF CS-O10과 같은 대응). (2026-10-24 후속: 이제 사용자가 `prefix x`로 직접 멈출 수 있다. 실제 PTY 테스트가 ISIG를 끈 명령을 이 경로로 멈추는 것을 확인했다. [FOLLOWUPS-BRIEF.md](FOLLOWUPS-BRIEF.md))

### 실험 명령 stop과 outcome(CS-D16)

- `TaskFlow`는 실행 중 실험을 `command_id=run_id`로 등록부에 올리고(`HostShellPort`, `request_id`, 끝 관측 hook 포함) 끝나면 내린다.
- 취소 없이 stop된 run: 소유자가 종료를 관측하면 판정·분석 없이 `last_result = {run_id, outcome: "stopped", stopped_by, exit_code, signal, run_closed: true}`로 닫고 Task는 `finished`(Worker는 free)다. 저장소에는 새 event kind를 만들지 않고(`run_events.kind` CHECK 제약, schema 변경 없음) `complete_run(run_id, {outcome: "stopped", stopped_by, judgment: null, ...})`로 닫는다(`_judgment_unavailable` 선례). 자동 재시도·실패/성공 판정은 만들지 않는다. stop이 종료 `unknown`이면 outcome은 확정하지 않고 `host_commands`에 이유가 보이며, 종료가 관측될 때 위 규칙이 적용된다.
- stop 표식은 첫 신호를 보내기 직전에 run에 붙인다. 신호 전에 명령이 스스로 끝났으면 표식이 없고 `already_exited`이며 평소 판정 경로를 탄다.
- 취소와 stop이 모두 있으면 취소가 우선한다: 종료 관측 시 기존 `_finish_cancel`로 Task가 `cancelled`로 닫힌다(판정 없음, CS-AC-12). 종료가 `unknown`이면 `_follow`가 `running`으로 계속 수집하므로 Task는 `cancelling`으로 남고(`held_reason` 유지) `host_commands[].stop.reason`이 이유를 보인다. 이후 종료가 관측되면 그때 닫힌다.

### 감사 기록

기존 handoff journal(`workflow/handoffs.jsonl`, `HandoffService.record`)에 additive 레코드 type을 쓴다(환경 변수 값·auth·argv·comm·/proc cmdline 없음):

- `command_stop_register_failed`: `type, command_id, kind, owner, error(예외 type 이름만), at`. owner의 StopTarget 등록이 실패하면 조용히 넘기지 않고 남긴다. 명령은 계속 추적되나 중지는 불가(`refused: registration_failed`).
- `command_stop_repeat`: `command_id, requested_by, repeat_requests`. episode가 끝난 뒤 들어온 반복 요청의 횟수만 남기며 새 신호·타이머는 없다.
- `stop_command_request`: Manager `stop_command` 호출 자체의 기록(`stop_survivor` 요청 기록 선례). `key, command_id, reason`; 검열된 요청은 `redacted`(`invalid_arguments`, `environment_value`)로 남긴다.
- `command_stop_requested`: `command_id, requested_by, kind, owner, task_id, task_status, reason(Manager 입력, 검열 후), at`
- `command_stop_refused`: `command_id, requested_by, status, reason, at` (not_found/refused/stale 포함)
- `command_stop_target`: `command_id, pid, start_ticks, process_group, session, uid, boot_id, supervisor{pid,start_ticks}, tracked[{pid,start_ticks}], left_session[...]`
- `command_stop_signal`: `command_id, signal, at, stage, targets[{pid,start_ticks}], failed[{pid,reason}], auxiliary(bool)`
- `command_stop_finished`: `command_id, status, exit_code, signal, reason, residual, elapsed_seconds, repeat_requests`
- `command_stop_corrected`: `command_id, from: "unknown", previous_reason, to: "exited", exit_code, signal, at`
- `command_interrupt`: `requested_by: "user", command_id, pid, start_ticks, process_group, delivered, input_owner, parent_mode, at`
- 실험: 기존 `run_ended` 레코드의 `outcome: "stopped"`와 `stopped_by`.

## 상태 전이, 실패와 경계 동작

- 소유자(`_follow`)가 종료를 관측하기 전에는 `exited`라고 하지 않는다. 단계 타이머가 끝나도 관측이 없으면 `unknown`이다(SIGKILL 뒤 5 s 포함, D 상태·권한 오류). 이 명령은 여전히 `terminal_command_running`으로 새 명령을 거절하며 수정·재실행 가능하다고 보고하지 않는다(CS-AC-18).
- 소유자가 선언한 `unknown`(shell 상태 불명, backend stopping 등)은 소유자의 최종 결과이므로 stop의 `unknown`도 같은 이유로 확정되고, 이후 관측이 없어 정정되지 않는다(한계로 기록).
- 호출자의 bridge가 끊기거나 abort해도 episode는 계속된다. backend 종료 중에는 새 stop을 `refused: backend_shutdown`으로 거절하고 진행 중 episode는 daemon thread로서 shutdown을 지연시키지 않는다.
- 식별 대기 3 s 안에 child가 준비되지 않으면(시작 직후 경합) 신호 없이 `unknown`(`identity_unavailable`)이다.
- 신호 직전 pin 검증 실패, 대상 집합이 비어 있음, host shell/pane 교체는 신호 없이 `stale`/`unknown`이다. "대상이 불명확하면 신호를 보내지 않는다"(C-D45)가 우선한다.
- Ctrl+C의 검증 실패는 조용히 현행 거절 경로로 되돌린다(전달하지 않음, 감사 기록 없음).

## 구현 결정

### 합의된 제약(BRIEF)

CS-D01~17 전체. 특히 `terminal`의 기존 명령 실행·점유 거절·1분 점검·완료 알림 계약 유지, 취소의 현행 의미 유지, C-AC-08/D52·C-D63·C-D71 (1)의 유지, 신원 불명이면 신호 없음.

### 기술 보완(이 문서가 정함)

| 항목 | 결정 | 이유 |
|---|---|---|
| Worker 입력 | `terminal.stop`(command_id) | CS-D12 |
| Manager 도구 | `stop_command {command_id, reason}` | CS-D12, `stop_survivor` 선례 |
| 실험 command_id | `run_id` | 별도 id 불필요, Manager가 이미 안다 |
| 신호 전달 | 구성원별 pidfd, `killpg` 금지 | PID 재사용 안전 (BRIEF 불변식의 "SPEC에서 정한다") |
| tracked 범위 | supervisor 후손 ∩ host shell session, 같은 pgrp | C-D63 선례, CS-D15 |
| setsid 후손 | 보고만, 남으면 `unknown` | CS-AC-05 예시, lifetime 의미 |
| SQLite | 변경 없음 (`complete_run` details에 `outcome: stopped`) | 마이그레이션 회피 |
| Ctrl+C | 단독 `\x03`을 PTY에 쓰기(검증 후) | 실제 사용자 Ctrl+C와 동일한 tty 의미, 기존 경계와 분리 |
| core 문서 | 수정하지 않음. 아래 표가 후속 주석 | CS-D05, 승인된 core bundle |
| ADR | 만들지 않음 | 되돌리기 쉬운 구현 선택, 사용자 결정은 BRIEF에 기록됨 |

### 모듈 배치

- 새 `src/workbench/backend/command_stop.py`: `StopPolicy`, `RunIdentity`, `ProcessPin`, `StopEnvironment`(시계·대기·`/proc` 조회·pin·signal을 한 port로 묶음; 기본 `LinuxStopEnvironment`, 테스트는 오류 주입 하위 클래스), `scan_tracked`, `StopEpisode`(thread 하나가 단계 구동), `CommandStopService`(등록부·인가·idempotency·정정·감사), `StopTarget`(provider가 돌려주는 command view: command_id, owner, kind, task_id, request_id, 종료 결과, identity 조회, stop 표식 hook), `STOP_COMMAND_TOOL`, `validate_stop_command_arguments`.
- `terminal/shell_persistent/adapter.py`(+필요 시 `shell_g2/lifecycle.py`): `PersistentShell.run_identity()`, `interrupt_run()`. lifecycle 상태기계 자체는 바꾸지 않는다.
- `backend/panes.py`: `HostShellPort.run_identity()`, `ShellPane.admit(data, kind="input")`의 Ctrl+C 예외, `interrupt_listener`.
- `backend/flow_terminal.py`: `stop` 인자·`_Command.port`·provider·`stopping` 표면·`stopped_by`·`commands_run.command_id`·안내 문구. 단계/신호 로직은 두지 않는다.
- `backend/flow_tasks.py`: `host_commands`, 실험 등록부·provider, `_finish_stopped`, `CANCEL_WAIT_DETAIL`.
- `backend/service.py`·`flow_recovery.py`: `CommandStopService` 구성·`stop_command` 라우팅(`_recovery_tool` 경로, MANAGER_TOOLS), `admit`에 `kind` 전달, interrupt listener→journal, `workbench_status`/snapshot 노출.
- `omp_bridge/g3/bridge.ts`, `omp_bridge/skills/**`(영문 model-facing): 도구 schema·timeout·안내.
- 위 외에는 건드리지 않는다. 특히 `ui/product/*`, lifecycle 상태기계, SQLite schema, core 문서.

## core 조항 한정·대체 대조(CS-D05)

core 문서(`docs/features/core-workbench/*`)는 승인된 bundle이므로 수정하지 않는다. 아래가 이 기능이 승인되는 시점부터의 후속 주석이며 core 해석 시 함께 읽는다.

| core 조항 | 이 기능에서의 효력 | 구현 위치 |
|---|---|---|
| C-D68 (1)(8)(9)(10) | `terminal` 입력에 `stop` 추가. stop 호출은 새 명령이 아니며 최대 약 20 s(CALL_WAIT 25 s) 대기, 120 s 안. 상태에 `stopping`. 새 명령 규칙은 유지 | `flow_terminal.py`, `bridge.ts` |
| C-D70 (3) | 재시작이 명령을 종료하지 않는 점 유지. 새 Worker가 `terminal.stop`으로 멈춘다 | `flow_terminal.py` |
| C-D63 | shell kill 불변. 명령 단위 stop 추가 | — |
| C-AC-34 / C-D45 | 신원 검증한 추적 명령에 한해 Manager 자체 판단 SIGTERM/SIGKILL 허용. 그 밖은 현행 | `command_stop.py` |
| C-AC-30 / C-D49, C-D71 (3) | 일시정지·boot 확인 전에도 stop 허용. 새 실행·자동 전달 보류는 유지 | `flow_terminal.py`, `service.py` |
| C-AC-08 / C-D52 | 단독 `\x03`만 인수 없이 전달. 나머지 유지 | `panes.py`, `adapter.py` |
| `flow_tasks.py` docstring "never killed" | 취소는 계속 죽이지 않는다. 종료는 stop이 맡는다 | `flow_tasks.py` |

## 수락 기준

BRIEF의 CS-AC-01~23이 정본이며 ID와 의미를 바꾸지 않는다(복사하지 않고 BRIEF를 참조). 각 ID의 책임 ticket, 시연 seam, 증거 수준은 아래와 같다. 추적 정본은 PLAN.json(`acceptance_trace`, `gate_items`)이다.

| ID | 책임 ticket | seam(아래 표) | 증거 | 최종 통합 |
|---|---|---|---|---|
| CS-AC-01 | CS-08 | S1 | fixture | CS-10 |
| CS-AC-02 | CS-08(free-work), CS-09(experiment) | S1 | fixture | CS-10 |
| CS-AC-03 | CS-02, CS-06 | S2, S4 | runtime | CS-10 (실시간·OMP schema) |
| CS-AC-04 | CS-03 | S2 | runtime | CS-10 (실제 10/5/5 s) |
| CS-AC-05 | CS-03 | S2 | runtime | CS-10 |
| CS-AC-06 | CS-05, CS-09(실험 owner) | S3 | fixture | CS-10 |
| CS-AC-07 | CS-02 | S2 | runtime | CS-10 |
| CS-AC-08 | CS-01, CS-02 | S2, S6 | runtime | CS-10 |
| CS-AC-09 | CS-02 | S2 | runtime | CS-10 |
| CS-AC-10 | CS-04 | S2 | runtime | CS-10 |
| CS-AC-11 | CS-05, CS-06 | S2, S4 | runtime | CS-10 |
| CS-AC-12 | CS-09 | S1, S2 | runtime | CS-10 |
| CS-AC-13 | CS-05 | S3 | fixture | CS-10 |
| CS-AC-14 | CS-05 | S2 | runtime | CS-10 |
| CS-AC-15 | CS-05 | S2 | runtime | CS-10 |
| CS-AC-16 | CS-07 | S5 | runtime | CS-10 |
| CS-AC-17 | CS-07 | S5 | runtime | CS-10 |
| CS-AC-18 | CS-04 | S2(S7 주입) | runtime | CS-10 |
| CS-AC-19 | CS-02 | S2 | runtime | CS-10 |
| CS-AC-20 | CS-02/03/04/05/07/09 (생산), CS-10 (완결 확인) | S2, S5 | runtime | CS-10 |
| CS-AC-21 | CS-09 | S1, S2 | runtime | CS-10 |
| CS-AC-22 | CS-05 | S2 | runtime | CS-10 |
| CS-AC-23 | CS-04(terminal), CS-09(실험 닫힘) | S2 | runtime | CS-10 |

실제 OMP가 필요한 확인은 CS-10의 `runtime` gate(OMP `terminal`/`stop_command` schema capture)이고, OMP 바이너리가 없으면 `not_run`으로 보고한다(다른 gate로 대체 주장 금지).

## 테스트 결정

BRIEF의 seam 5개 제안을 코드 조사 결과로 조정한 아래 S1~S7은 2026-10-21 사용자가 확정했다("확정된 결정" 참조). 조정점: BRIEF의 seam 2는 `tests/terminal`이 아니라 실제 PTY `ShellPane`+`HostShellPort`로 `TerminalService`를 쓰는 기존 패턴(`tests/backend/test_flow_terminal.py`)에 둔다. 신규 seam은 S6, S7이다.

| seam | 대상 | 잡는 것 | 놓치는 것 | 독립 기대값 출처 |
|---|---|---|---|---|
| S1 | 취소 결과 dict·`task_view`/`workbench_status`·실험 `last_result`(기존 `test_task_flow` fixture) | 문구/상태/outcome 형태, host_commands, stopped 닫힘 | 실제 프로세스 동작 | 계약 문구·BRIEF, 테스트가 직접 만든 Task/명령 상태 |
| S2 | 실제 PTY `ShellPane`+`HostShellPort`+`TerminalService`(stop 인자와 `CommandStopService`) | 신호 단계·tree·잔존·신원·점유 중 동작·unknown/정정·idempotency·journal | 호출 권한 경계 일부, 느림 | 테스트가 별도로 띄우거나 `/proc`에서 읽은 pid·start ticks·exit 128+n, 정책 상수 |
| S3 | `CommandStopService` 인가 규칙(owner×requester, 등록부 분류), 가짜 provider | owner 분류·거절 이유·이전 backend 분류 | 실제 신호 | BRIEF 매트릭스 |
| S4 | bridge.ts schema·timeout·도구 등록(fake backend peer, `node --test`)과 `Backend._tool_request` 분기 | 입력 형태, 호출 경로, 대기·반환 계약, 역할 제한 | 프로세스 동작 | SPEC의 인자/결과 계약 |
| S5 | 실제 `PersistentShell`을 쓰는 `ShellPane.admit` | 입력 owner 우회 범위, 단독 `\x03` 판정, 중간 상태 foreground 전달 | UI 키 입력 경로 | 테스트가 읽은 child_group·exit 130, 계약 |
| **S6 (신규)** | `PersistentShell.run_identity()`와 `ProcessPin`/`scan_tracked` 직접 호출(실제 프로세스) | identity 필드 정확성, tracked/left_session 분류, pin 불일치 | stop 흐름 전체 | 테스트가 `/proc`를 직접 읽은 값 |
| **S7 (신규)** | `StopEnvironment` port의 오류 주입 하위 클래스(signal 무효화/EPERM/start ticks 변경) | D 상태와 PID 재사용처럼 실제 PTY에서 재현이 불안정한 `unknown`·`stale` | 실제 커널 동작 | 정책 상수, 주입한 오류 |

원칙: 각 ticket은 위 seam에서 한 behavior씩 RED(의도한 behavior 부재/오류로 실패) → 최소 GREEN → 정리를 반복하고, 다음 behavior는 앞이 GREEN인 뒤 시작한다. 프로세스·signal·신원 ticket(CS-01~05, 07, 09)은 독립 `test_designer`가 요구에서 먼저 시나리오를 도출해 확인·보강한다(`*_independent*` 파일은 test_designer 소유). 최종 통합 전 ticket마다 단위 review를 하고, 최종 통합은 Standards/Spec 두 fresh reviewer와 별도 security review(신호·신원 경계가 trigger)를 받는다.

실행 명령(repo 루트, 공통 접두사 `env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python`):

- identity/pin: `... -m unittest discover -s tests/terminal -p 'test_run_identity*.py'`, `... -m unittest discover -s tests/backend -p 'test_command_stop_pins*.py'`
- stop 엔진·권한: `... -m unittest discover -s tests/backend -p 'test_command_stop_*.py'`
- Ctrl+C: `... -m unittest discover -s tests/terminal -p 'test_command_interrupt*.py'`
- 취소 표면·실험: `... -m unittest discover -s tests/backend -p 'test_cancel_host_command*.py'`, `... -p 'test_experiment_command_stop*.py'`
- bridge: `node --test tests/bridge/command_stop_bridge.test.ts` (+ 기존 bridge 회귀는 ticket CS-06에 열거)
- 회귀(관련 모듈만, 넓은 실행은 CS-10 한 번): `tests/backend` `-p 'test_flow_terminal*.py'`, `-p 'test_terminal_*.py'`, `-p 'test_task_flow*.py'`, `-p 'test_panes.py'`; `tests/terminal` `-p 'test_manual_input*.py'`, `-p 'test_cw18_host_shell*.py'`, `-p 'test_experiment_*.py'`; `tests/contracts`
- 실제 시간 확인(CS-10): `... tests/integration/live_command_stop.py`(기본 10/5/5 s). `tests/gates`는 쓰지 않는다.

TDD 예외: `omp_bridge/skills/**` 문서와 `ui_v1.py` docstring은 문서성 변경이라 RED를 만들지 않고 정적 확인(필수 문구·schema 일치)으로 검증한다. 모델이 문서를 따르는지는 증명하지 않는다.

## 호환성, 통합, 롤아웃 제약

- 모든 변경은 additive다: `terminal.stop`(없으면 기존 동작), 새 도구 `stop_command`, 새 결과/상태 필드, 새 journal type, `host_commands`. 기존 필드 의미·`TASK_STATUSES`·SQLite·설치 manifest는 그대로다.
- bridge와 backend는 한 패키지로 함께 배포된다. `stop`이 있는 새 bridge와 옛 backend 조합은 지원하지 않는다.
- 이전 backend 시작분 명령은 신호 대상이 아니며 `stop_survivor` 경로를 따른다.
- 실제 사용자 데이터·실제 연구 process·유료 provider·Docker·새 도구 설치는 쓰지 않는다. 운영 배포·commit/push는 별도 권한이다.
- `*_independent*` 테스트는 worker가 수정하지 않는다.

## 확정된 결정과 위임된 선택

2026-10-21 사용자가 아래 세 항목을 추천안대로 확정했다("추천대로 해줘").

1. **테스트 seam**: 위 S1~S7을 확정한다. 신규 S6(identity/pin 직접 호출), S7(오류 주입 port)과 BRIEF seam 2의 위치 조정(`tests/backend`)을 포함한다.
2. **setsid 후손**: 다른 session으로 옮긴 후손은 신호 대상이 아니라 보고 대상이다. 그것만 남으면 stop은 `unknown`(`left_session_descendants`)이고 host terminal은 그 후손이 끝날 때까지 점유된다(CS-AC-05 예시, C-D63 선례).
3. **raw 모드 Ctrl+C 한계**: Ctrl+C는 PTY에 `\x03`을 쓰는 방식이라 tty의 ISIG를 끈 명령에는 SIGINT가 되지 않을 수 있다. 이 한계를 받아들이고, 그 경우 사용자는 Manager에게 stop을 요청한다.

위임된 선택(사용자 확인 불필요): 필드·상수 이름, thread 구조, journal 레코드 세부, 정확한 안내 문구.

## Ticket 그래프

PLAN.json이 정본이다(ticket Markdown은 의존성을 복사하지 않는다). 요약(예시): CS-01 → CS-02 → CS-03 → {CS-04, CS-05}, CS-02 → CS-08, CS-01 → CS-07, CS-06은 선행 없음(계약-우선), {CS-04, CS-05, CS-08} → CS-09, {CS-06, CS-07, CS-09} → CS-10. 초기 frontier는 CS-01, CS-06이다. CS-02~05는 `command_stop.py`·`flow_terminal.py`를, CS-02·05·07·08·09는 `service.py`를 배타 claim하므로 의존 없이도 동시에 쓰지 않는다.
