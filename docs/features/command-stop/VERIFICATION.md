# command-stop 검증 근거

승인 기준은 BRIEF r0.2 / SPEC s1 / PLAN p1이다. 이 문서는 CS-10(최종 통합·실제 시간·감사·독립 검증)의 관측 결과만 기록하며 승인 기준을 바꾸지 않는다. 증거 원본은 `.workflow/command-stop/`(`progress.md`, `open-questions.md`, `runs/impl-20261021/`)에 있고, 아래에서 파일명으로 인용한다.

이 문서의 결과는 모두 위 증거로 뒷받침된다.

## 1. 범위와 후보

| 커밋 | 내용 |
|---|---|
| `628e5e5` | docs: command-stop 계획(BRIEF/SPEC/PLAN/tickets) |
| `00dccdc` | CS-06 terminal `stop`과 Manager `stop_command`의 bridge 노출 |
| `41febcb` | CS-01 run identity, pidfd pin, tracked-process scan |
| `56cef48` | CS-07 단독 사용자 Ctrl+C를 실행 중 명령에 전달 |
| `ecc3905` | CS-02 Worker가 SIGINT로 자기 명령 stop |
| `55f6904` | CS-03 SIGINT → SIGTERM → SIGKILL 단계 승격 |
| `49d0a53` | Q1 오래된 기대값(`workbench_records`, `workbench-report`) 정렬. 이 기능 범위 밖 quick-change |
| `d3e6513` | CS-08 free-work 취소와 `/task`의 `host_commands` |
| `3d92fc3` | CS-04 반복 stop 멱등성과 늦은 종료 정정 |
| `03febce` | CS-05 Manager `stop_command`와 인가 |
| `edf38fa` | CS-09 실험 host 명령 stop과 `stopped` 종료 |
| CS-10 (이 문서를 포함한 커밋) | 최종 통합 검증, 리뷰 수정, skill 문서, 이 문서 |

최종 후보는 `edf38fa` 위에 CS-10 변경을 얹은 것이다. CS-10 변경은 `src/workbench/backend/{command_stop,flow_tasks,flow_terminal,service}.py`, 그 테스트(`tests/backend/test_command_stop_hardening.py` 신규 포함), `tests/integration/live_command_stop.py`, 독립 최종 시나리오 `tests/integration/test_command_stop_final_independent.py`, skill 문서 3개(`omp_bridge/skills/to-manager/references/terminal.md`, `to-worker/references/task-results.md`, `workbench-recovery/SKILL.md`)다.

범위 감사(`628e5e5..HEAD` 커밋 diff 대조): `src/workbench/ui`, `src/workbench/tasks`, `docs/features/core-workbench`의 변경이 없고, SQLite schema(`CREATE TABLE`, `CHECK (`)를 건드린 줄이 없다. CS-10 변경(미커밋, 수정 worktree 포함)이 건드린 `src`는 위 네 파일이다.

## 2. Gate별 명령과 결과

공통 접두사는 `env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python`이다(`live_command_stop.py`를 직접 쓰는 gate는 `PYTHONPATH=src:.`).

| gate | 환경 변수 / 대상 | 결과 | 근거 |
|---|---|---|---|
| CS-G12 실제 OMP schema 수용 | `WB_LIVE_COMMAND_STOP_SCHEMA=1`, `tests/integration/live_command_stop.py` `OmpSchemaAcceptanceTests` | pass. 5/5 OK, 14.1 s | `progress.md`(2026-10-21) |
| CS-G10 실제 대기 측정 | `WB_LIVE_COMMAND_STOP_TIMING=1`, `RealTimeStopTests` 외 | pass. 14 tests OK, 154 s | `progress.md`, 후보 `03febce`(CS-09 미포함) |
| CS-G11 감사 완결성 | `AuditCompletenessTests`(gate 없음, 항상 실행) | pass. 3회 연속 통과, 약 20 s | `progress.md` |
| 독립 최종 시나리오 | `WB_FINAL_COMMAND_STOP=1`, `tests/integration/test_command_stop_final_independent.py` | 15/15 pass (결함 수정 후) | `progress.md` |
| CS-G13 정적(review·security·scope·trace) | 3절, 1절 범위 감사 | 3절 참조 | — |

### CS-G12 실제 OMP schema 수용

- 설치된 OMP는 `omp/18.11.1`이다. 티켓(`tickets/CS-10.md`)이 기대한 버전은 18.6.1이다. 버전이 다르지만 대체 결과가 아니라 이 환경에 실제 설치된 OMP로 실행한 결과다. 버전은 테스트가 기록만 하고 고정하지 않는다(`live_command_stop.py` 머리 설명, 독립 시나리오의 `test_the_two_sessions_were_captured_from_the_recorded_omp_version`).
- `omp_bridge/g3/bridge.ts` sha256 `ecde4130cfd7d0c01358c57941a335c57c3043175e0debc58586b5c921f50a51`. 커밋된 파일 기준 실행 전과 최종 단계(이 문서 작성 시점)의 해시가 같다. 따라서 CS-G12 증거가 최종 후보에도 유효하다.
- 확인 항목: Worker `terminal`의 `stop`은 nullable·비required, Manager `stop_command`는 strict(`command_id` uuid, `reason` 1..500)이며 역할이 서로 섞이지 않는다. OMP가 모든 도구에 `i` 필드를 덧붙이는 것이 관측됐는데 계약 밖이라 비교에서 제외했다.
- 리뷰 수정 1차 뒤 PATH의 `omp`로 CS-G12를 다시 실행했고 5 OK였다(`progress.md`). 최종 수정 뒤 재실행 결과는 5절에 있다.

### CS-G10 실제 대기 측정 (기본 정책 10/5/5 s)

| 사례 | SIGINT → SIGTERM | SIGTERM → SIGKILL | 호출 시간 | 종료 |
|---|---|---|---|---|
| (a) SIGINT 무시 | 10.056 s | — | 10.87 s | exit 143 (SIGTERM) |
| (b) SIGINT·SIGTERM 무시 | 10.091 s | 5.051 s | 15.91 s | exit 137 (SIGKILL) |
| (c) 자식도 신호 무시 | — | — | 16.09 s | `residual` 빈 목록 |

- 정책 최대는 10 + 5 + 5 = 20 s이고 `CALL_WAIT` 25 s 안에 든다. 관측된 최장 호출은 16.09 s다.
- (d) 점유 중에는 `terminal_command_running`이 유지되고 host terminal에 입력이 들어가지 않는다.
- (e) 진행 중 동시 반복 요청은 같은 episode에 합류해 신호 한 세트만 보낸다.
- 같은 실행에서 `BridgeBackendCrossCheckTests`, `BridgeSchemaFrameTests`, `InputFrameCtrlCTests`도 통과했다.
- 이 측정은 `03febce` 기준이다(CS-09 미포함). 최종 후보에서 다시 잰 값은 5절에 있다.

### CS-G11 감사 완결성

`AuditCompletenessTests`가 하나의 결합 시나리오(Worker stop, Manager stop, 거절, unknown → 정정, Ctrl+C, 실험 stopped)를 실제 PTY·프로세스로 실행하고 journal(`workflow/handoffs.jsonl`)과 task ledger를 읽어 확인한다.

| 항목 | 조회 위치(journal type) | 확인 |
|---|---|---|
| 요청자 | `command_stop_requested.requested_by`, `command_stop_refused.requested_by`, 실험 `run_ended.stopped_by` | 조회됨 |
| 대상(command_id, pid, start_ticks) | `command_stop_target` | `/proc`에서 테스트가 따로 읽은 값과 일치 |
| 신호와 시각 | `command_stop_signal`(`signal`, `at`, `stage`, `targets`, `failed`, `auxiliary`) | 조회됨 |
| 종료 상태 | `command_stop_finished`(`status`, `exit_code`, `signal`, `elapsed_seconds`) | 조회됨 |
| 잔존 확인 | `command_stop_finished.residual` | 조회됨 |
| 거절·unknown 이유 | `command_stop_refused.reason`, `command_stop_finished.reason` | 조회됨 |
| 정정 이력 | `command_stop_corrected`(`previous_reason`) | 조회됨 |
| 사용자 Ctrl+C | `command_interrupt`(요청자 `user`) | 조회됨 |
| 실험 `stopped_by` | `run_ended`, `last_result` | 조회됨 |
| 비밀 부재 | stop 감사 기록 전체에서 시험용 비밀 환경 변수 값, bridge token, argv 문자열 검색 | 없음 |

판정: "argv 없음"은 SPEC대로 stop 감사 기록(`command_stop_*`, `command_interrupt`) 기준이다. 기능 이전부터 있던 `terminal_request` 등 기록에는 명령 텍스트가 있으며 이는 기존 설계다(`progress.md`).

### 독립 최종 시나리오

`test_designer`가 요구에서 도출한 15개 시나리오다(취소 후 명령 표시와 Worker stop, 새 Worker·Manager의 stop, ended/unknown/이전 backend/사용자 프로그램에 신호 없음, unknown·정정, `left_session`, pause·boot hold, Ctrl+C, 실험 취소·stopped 결합 등). 처음 실행에서 결함 1건이 나왔다. `commands_run` 항목에 `command_id`가 없었다(SPEC `command_id` 조항). `flow_terminal.py`를 TDD로 고쳤다(RED 3 → GREEN). 수정 뒤 terminal 67, flow_terminal 95, 독립 최종 15가 통과했다. 같은 실행에서 모호하다고 지적된 2건(무전달이면 평소 판정, 원 요청자 유지)은 기존 판정과 일치했고, 취소된 Task 종료 알림의 지연은 기존 C-D70 동작이다.

## 3. 최종 리뷰

| 리뷰 | 결과 | 요약 |
|---|---|---|
| Standards | approve | P2 5건, P3 다수 |
| Spec | approve | CS-AC-01~23 전부 구현·입증. P2-1: 사용자 TUI가 `host_commands`를 표시하지 않음(→ Q3) |
| Security | approve | 위협 8개 중 7 mitigated, DoS 항목은 partially. diff 비밀 스캔 clean |

리뷰 뒤 수정(worktree `cs-20261022-cs10fix`, 기준 임시 커밋 `039f50f`):

| 항목 | 수정 |
|---|---|
| 이전 backend 판정 | 매 호출 journal 전수 조회 대신 첫 조회 때 한 번 만들어 고정(`frozenset`)하는 색인(`service.py`). stop 서비스가 등록한 id는 `_registered`에 남겨 classifier에 묻지 않음(`command_stop.py`). 시작 시점 경계 방식은 기존 계약 테스트 2건(`PreviousBackendCommandTests`, `PreviousBackendWiringTests`)과 충돌해 쓰지 않음 |
| 종료 command 참조 | `StopTarget.release()` 도입. episode가 없을 때의 release는 `_lock` 안에서 수행해, 진행 중 episode가 forbidden 집합을 잃지 않음 |
| 결과 보관 | `RESULTS_KEPT = 256` 상한(`flow_terminal.py`) |
| pidfd 실패 | `pidfd_open` 실패(`EMFILE` 등)는 `unknown` + `pidfd_unavailable`, 신호 없음 |
| Worker `stopping` | `stopping` 응답을 캐시하지 않음 |
| 주석·docstring·live 테스트 | 정리 |
| 철회 | 시각을 ms 단위로 바꾸는 항목은 독립 테스트 2건이 초 단위 형식을 고정하므로 철회 |

수정 직후 결과(delta 리뷰 전): `command_stop_*` 284 OK, flow_terminal 95, experiment 46, cancel 35, `live_command_stop` 15(skip 10), 독립 최종 15, CS-G12 5 OK.

Delta 리뷰는 changes_required였다. P2-1: lazy 색인이 이 backend의 명령을 `previous_backend`로 오분류할 수 있음 → 시작 이전 journal만으로 immutable 색인 + 자기 id 필터. P2-2: `release()`가 진행 중 episode의 forbidden을 비움 → episode 종료 뒤 release. P3-3 주석. P3-4: SPEC이 `pidfd_unavailable`을 열거하지 않음 → 4절에 기록. 마지막 수정 라운드는 Sonnet worker가 5시간 정체한 뒤 사용자 지시로 예외적으로 Opus worker가 수행했다.

Delta 재리뷰 결과: approve(P0~P2 없음). P2-1·P2-2·P3-3 해소. 새 테스트 `test_an_own_command_whose_end_record_was_lost_is_never_a_survivor`(RED `('refused', 'previous_backend_command') != ('not_found', 'unknown_command')`)와 `StopArrivingAsTheOwnerEndsTests`(RED forbidden pid 923 pin)로 고정했다. 남은 P3: `_registered` 무제한(명령당 약 100 B, 다른 명령별 상태와 같은 수명이고 상한을 두면 오분류가 되살아나므로 수용), SPEC에 `pidfd_unavailable` 미열거(7절), journal 읽기 실패 시 색인을 기억하지 않아 다음 조회에서 다시 읽음(결과는 안전), P2-2 테스트가 "신호 없음"만 단언함. 재리뷰 시 hardening 14 OK.

최종 커밋(`b9742f6`) 기준 새 리뷰(이전 findings 없는 reviewer):

| 리뷰 | 결과 | 요약 |
|---|---|---|
| Standards | approve, P1 없음 | P2: `already_exited`의 `ended_at`이 실제 종료 시각이 아닌 요청 시각, `_stage` 길이, 테스트 `/proc` helper 중복. P3 10건(대부분 9절 항목과 같음) |
| Spec | approve, P0~P2 없음 | CS-AC-01~23 전부 구현·테스트 확인. 7절 이탈은 모두 SPEC 위임 범위 안이고 BRIEF 밖으로 사용자 동작을 넓히거나 줄이지 않음. P3: 명시적 금지 집합 미연결(7절), 이 문서의 `stop_signal` 위치 표기(수정함), 재취소 `task_closed`에 `host_commands` 없음(이후 추가됨, 8절 8번) |

## 4. 넓은 회귀

실행: `final-regression.log`(디렉터리별 `tests/<dir>` unittest, node bridge). 기준선 `53023c8`은 `backend-split/baseline`(b0·b1, 38 파일)로 비교했다.

| 묶음 | 결과 | 시간 |
|---|---|---|
| `tests/terminal` | 305 OK | 739.9 s |
| `tests/workflow` | 78 OK | 474.1 s |
| `tests/tasks` | 43 OK | 189.7 s |
| `tests/contracts` | 70, error 1 | 1.8 s |
| `tests/recovery_boot` | 188, failures 4 / errors 29 | 202.9 s |
| `tests/bridge` | 54, failures 3 / skipped 1 | 179.2 s |
| `tests/integration` | 99, failures 2 / errors 17 / skipped 17 | 418.8 s |
| `tests/ui` | 891, failures 7 | 397.6 s |
| node bridge | 217 tests, pass 197 / fail 20 | — |
| `tests/backend` | 전체 한 번에는 2시간 timeout. 4묶음으로 분할 재실행: 186 파일 / 1625 tests | 아래 |

`tests/backend`는 2시간 timeout에 걸려(`final-regression.log` 02:04 → 04:04) 파일 단위 4묶음(g0~g3)으로 나눠 다시 실행했다. 기준선 대조는 같은 방식으로 `53023c8`에서 실패 파일을 다시 돌린 b0·b1이다.

기준선과 비교한 분류:

- 새 실패 1건: `test_workbench_skills` `RecoverySkillTests.test_frontmatter_and_size`. `workbench-recovery/SKILL.md`가 5182자로 4000자 상한을 넘었다(`stopped` outcome과 `stop_signal` 안내를 보강한 결과, 기준선 `53023c8`의 크기는 3858자). 상한은 바꾸지 않고 skill을 3893자로 줄여 해결했다. 이 문서 작성 시점에 `test_workbench_skills` 46 tests OK를 확인했다.
- 부하성 실패 3 파일: `test_delivery_polish_p27`(6 OK, 12.4 s), `test_live_input_independent`(4 OK, 88.2 s), `test_shell_kill_restart_independent_p27s`(16 OK, 94.4 s)는 분할 실행에서 실패했으나 단독 재실행에서 통과했다(`backend-split/rerun/summary.txt`). 같은 재실행에서 `test_dvl12_runtime_independent`(3 tests, failure 1)와 `test_omp_isolation_independent_p27m`(45 tests, failures 9)는 단독에서도 실패했고 기준선 b1에도 같은 실패가 있다.
- 나머지 실패는 모두 기준선에서도 실패하는 기존 실패다(`tests/backend` 실패 파일 집합이 위 4개를 제외하고 b0·b1과 같음).

기존 실패 모듈 목록(이 기능과 무관):

| 묶음 | 모듈 |
|---|---|
| contracts | `test_ui_v1`(`test_every_client_type_has_a_documented_request_shape`, 'subject_kind must be task or command') |
| recovery_boot | `test_cli_down_alias_independent`, `test_cli_independent_p27cw19`, `test_cli_shutdown_wait_cw16`, `test_faults_holds`(`ConfirmBootCliTests`), `test_survivors_holds_independent_p27cw19` |
| bridge | `test_delivery_serialization`, `test_handoff_tools_extension` |
| integration | `test_summary_compat_runtime_only_independent`, `test_validation_retention`(`DVL10_INDEPENDENT_OUTPUT` 환경 변수), `test_cw16_final_evidence_validators`, `test_cw16_runtime_matrix`, `tests.gates.g2_shell.test_canonical_matrix_integration` |
| ui | 7건(`test_product_entrypoint`, `test_product_pty`, `test_product_resize_independent_p27l` 2건, `test_product_direct_scroll_independent_p27k`, `test_product_jamo_detach_independent_p27p`, `test_product_layout_scroll_independent_p27j`). 앞의 4건은 계획 커밋 직후 기준선에서 이미 확인됨 |
| node bridge | 20건(`DVL10_INDEPENDENT_OUTPUT` 관련) |
| terminal_fetch | `test_terminal_fetch_independent_p27cd68` `FetchNeverWaits`(CHECK_INSTRUCTION 문구 기대값) |

계획 커밋 직후와 53023c8 export에서 따로 확인한 기존 실패는 `progress.md`에 있다(`test_survivors_holds` `AttributeError 'NoneType'.get`, `test_ui_v1` 문구, `FetchNeverWaits` 문구 등). Q1(`workbench_records`·`workbench-report` 기대값 4파일)은 `49d0a53`으로 정렬했다.

## 5. 수정 후 재실행 결과

최종 후보(리뷰 수정을 main에 적용한 상태, worktree와 파일 단위 동일 확인)에서 2026-10-22에 다시 실행했다. 로그는 `runs/impl-20261021/postfix/`.

| 대상 | 결과 | 시간 |
|---|---|---|
| `tests/backend` `test_command_stop_*`(hardening 14 포함) | 289 중 failure 1 → 해당 클래스 단독 재실행 4 OK | 1241 s |
| `tests/backend` `test_experiment_command_stop*` | 46 OK | 508 s |
| `tests/backend` `test_cancel_host_command*` | 35 OK | 258 s |
| `tests/backend` `test_flow_terminal*` | 95 OK | 107 s |
| `tests/backend` `test_workbench_skills`, `test_cw18_fixes` | 46 OK, 6 OK | — |
| `tests/terminal` `test_command_interrupt*` | 64 OK | 18 s |
| 독립 최종(`WB_FINAL_COMMAND_STOP=1`) | 15 OK | 223 s |
| `live_command_stop.py`(`WB_LIVE_COMMAND_STOP_SCHEMA=1 WB_LIVE_COMMAND_STOP_TIMING=1`) | 동시 실행에서 failure 1 → 단독 재실행 15 OK | 164 s |

동시 실행 실패 2건의 판정:

- `live_command_stop` (c) "자식도 신호 무시": `_pids_running("sleep", "0.2")`가 `/proc` 전체에서 같은 argv를 찾는다. 같은 시각에 돌던 `test_command_stop_*`가 `sleep 0.2`를 띄우므로 남의 프로세스를 셌다. 단독 재실행에서 통과했다. 테스트 격리 약점으로 9절에 올린다.
- `StoppedByTests.test_the_workers_result_notice_and_commands_run_say_the_manager_stopped_it`: live gate와 동시에 돌 때 `terminal_done` 알림 대기가 timeout됐다. 단독 재실행 4 OK, 수정 worker의 worktree 실행(289 OK)에서도 통과했다. 부하성으로 판정한다.

CS-G10 최종 후보 실측(단독 실행):

| 사례 | SIGINT → SIGTERM | SIGTERM → SIGKILL | 호출 시간 | 종료 |
|---|---|---|---|---|
| (a) Worker `terminal` stop, SIGINT 무시 | 10.052 s | — | 10.552 s | exit 143 |
| (b) Manager `stop_command`, SIGINT·SIGTERM 무시 | 10.038 s | 4.954 s | 15.680 s | exit 137 (SIGKILL) |
| (c) 자식도 신호 무시 | — | — | 15.480 s | exit 137, `residual.alive`·`left_session` 빈 목록 |
| (d) 점유 중 요청 | — | — | 10.765 s | 관측 `stopping`, 다른 요청은 `terminal_command_running` |
| (e) 동시 반복 | — | — | 첫 호출 10.998 s, 반복 8.733 / 8.791 s | 한 episode에 합류 |

(b)의 SIGTERM → SIGKILL 4.954 s는 테스트가 신호를 관측한 시각의 차이다. SIGTERM 관측이 송신보다 늦으면 5 s보다 짧게 보인다. 같은 사례의 SIGINT → SIGKILL은 14.992 s다. 테스트 허용 범위 안이라 통과했고, 동시 실행에서 잰 값은 5.052 s다.

CS-G12 재확인: `omp/18.11.1`, `OmpSchemaAcceptanceTests` 5개 통과. `omp_bridge/g3/bridge.ts` sha256 `ecde4130…0a51`, HEAD와 차이 없음.

### 최종 커밋 기준 재확인 (`3f64eca`)

리뷰 수정 뒤 커밋한 후보로 넓은 회귀와 보안 delta 리뷰를 다시 했다. 로그는 `runs/impl-20261021/final-head/`.

| 대상 | 결과 | 시간 |
|---|---|---|
| `tests/terminal` | 305 OK | 733 s |
| `tests/workflow` | 78 OK | 492 s |
| `tests/tasks` | 43 OK | 169 s |
| `tests/contracts` | 70, error 1 | 2 s |
| `tests/recovery_boot` | 188, failures 4 / errors 29 | 196 s |
| `tests/bridge` | 54, failures 3 / skipped 1 | 174 s |
| `tests/integration` | 99, failures 2 / errors 17 / skipped 17 (`tests.gates` 별도 failures 18) | 410 s |
| `tests/ui` | 891, failures 7 | 395 s |
| node bridge | 217, pass 197 / fail 20 | — |
| `tests/backend` | 216 파일(하위 디렉터리와 command-stop 파일 전부 포함), 2246 tests, 4묶음 동시 실행 | — |

- backend 외 디렉터리와 node bridge의 실패 집합은 4절의 넓은 회귀와 같다. 모두 기존 실패다.
- backend: 이전 실패 집합에서 `test_workbench_skills`(수정 확인)와 부하성 2 파일이 빠졌다.
- 새로 보인 4건은 단독 재실행에서 모두 통과해 부하성으로 판정했다.
  - `test_command_stop_outcomes_independent`의 0.5 s 상한 2 ms 초과
  - `test_experiment_command_stop_independent`의 `started` 10 s 대기 초과
  - `installation/test_setup_command_link` 3건
  - `test_shell_kill_restart`의 fd 개수
- 보안 delta 리뷰(마지막 리뷰 수정분만 대상): approve, P0~P2 없음. 수정이 pidfd 검증 신호 경로를 우회하지 않고, `_registered`에는 Workbench가 만든 uuid만 들어가며, 어느 쪽으로 오분류해도 신호로 이어지지 않는다. hardening 14 OK(152 s).
  - P3: `_finish`에서 `forbidden()`이 예외를 내면 빈 집합으로 대체돼 읽기 전용 residual 보고에 host 쪽 pid가 섞일 수 있음.
  - P3: journal 색인 빌드가 실패하면 기억하지 않음(delta 재리뷰와 같은 지적).

## 6. not_run

없음. 필수 runtime인 OMP가 설치되어 있어 CS-G12를 실행했다. 단, 기대 버전 18.6.1이 아닌 실제 설치본 18.11.1로 실행했다는 점은 2절에 명시했다.

## 7. SPEC과 다른 점

| 항목 | SPEC | 구현 | 위치 |
|---|---|---|---|
| 마지막 신호 필드 이름 | "마지막 `signal`"(60행) | `stop_signal`. `signal`은 이미 종료 신호(`already_exited`·`exited`)를 뜻하므로 겹치지 않게 분리 | `flow_terminal.py` `STOP_MARKS = ("stopped_by", "stop_signal")`, `command_stop.py` `stop_marker`, `flow_tasks.py` 실험 `complete_run` details(stopped `last_result`에는 없음. SPEC의 `last_result` 필드 목록과 일치) |
| `pidfd_unavailable` | unknown 이유로 열거하지 않음(50행: `still_alive_after_sigkill`, `permission_denied`, `left_session_descendants`, `identity_unavailable`) | `pidfd_open` 실패(`EMFILE`·`ENOMEM`)는 신원 불일치가 아니므로 `stale`이 아닌 `unknown` + `pidfd_unavailable`, 신호 없음. 불변식 2의 "검증 실패 시 stale/unknown"과 "대상이 불명확하면 신호 없음"(C-D45) 범위 안에서 이유 코드만 추가 | `command_stop.py` `PIDFD_UNAVAILABLE`, `pin` 거절 `pidfd_unavailable:<errno 이름>` |
| 감사 type `command_stop_repeat` | 감사 목록(`command_stop_requested` … `command_interrupt`)에 없음 | episode가 끝난 뒤 들어온 반복 요청의 횟수(`repeat_requests`)를 남기는 추가 레코드. 새 신호·타이머는 만들지 않음 | `command_stop.py`(반복 요청 처리부) |
| 감사 type `stop_command_request` | 목록에 없음 | Manager `stop_command` 호출 자체의 기록(`stop_survivor`의 요청 기록 선례). 검열 사유(`invalid_arguments`, `environment_value`)도 같은 type에 `redacted`로 남김 | `service.py` `_stop_command` |
| `host_commands_unavailable` | `host_commands`만 정의 | provider가 없거나 실패하면 `host_commands: []`에 `host_commands_unavailable: true`를 더해 "읽을 수 없음"이 "명령 없음"으로 보이지 않게 함(CS-08 리뷰 P2) | `flow_tasks.py` `_host_command_fields` |
| 명시적 금지 집합 | 구조적 제외 + 명시적 금지 집합 | 2026-10-23 연결함(96eca26). 구조적 제외(session, uid, 자손 조건, parent·supervisor·backend pid)에 더해, service가 주입한 provider가 돌려주는 OMP pane pid를 `StopTarget.forbidden`으로 넘긴다(두 owner 모두, 호출 시점에 평가). `_finish`에서 provider가 실패하면 마지막으로 성공한 집합을 residual 점검에 쓴다(bb0f810). 최초부터 실패했다면 빈 집합이다 | `command_stop_owner.py`, `flow_terminal.py` `_register_stop_target`, `flow_tasks.py` `_register_stop_target`, `service.py`, `command_stop.py` `_finish` |
| refused 이유 `registration_failed` | refused 이유 목록에 없음 | stop 등록이 실패한 명령에 `stop`을 보내면 `refused` + `registration_failed`로 답한다. 등록 실패는 흐름을 바꾸지 않으므로 명령은 계속 추적되지만 중지 대상은 아니다(135ce17) | `command_stop.py`, `command_stop_owner.py` |
| 감사 type `command_stop_register_failed` | 감사 목록에 없음 | 등록 예외를 조용히 넘기지 않고 남기는 기록. 사용자 결정(2026-10-23): 추적은 계속하고 기록하며 중지는 불가(135ce17, cd42a92) | `command_stop_owner.py`, `flow_terminal.py`, `flow_tasks.py` |
| command_id 패턴 | canonical lowercase uuid(8절 6번) | 엄격한 패턴(버전 `[1-8]`, variant `[89ab]`)을 `command_stop_owner.py`의 `COMMAND_ID` 하나로 모으고 owner·`flow_recovery.py`가 같이 쓴다. Manager `stop_command`에도 같은 패턴이 적용되어, 전에 임의의 nibble을 받던 `flow_recovery.py`가 이제 거절한다(96eca26) | `command_stop_owner.py`, `flow_recovery.py` |

`pidfd_unavailable` 행의 코드 근거는 delta 리뷰 이후 수정을 반영한 worktree 후보에 있다(커밋 `edf38fa`에는 없음). 위 행 중 SPEC 본문을 고치지 않은 것은 계획 문서 수정 금지 때문이다. SPEC 본문 정정은 사용자 확인 대기 중이다(이 표가 정본).

## 8. 판정 목록

`progress.md`의 판정을 이 기능의 의도된 동작으로 기록한다.

1. 신호 표식 필드는 `stop_signal`이다(7절).
2. 전달된 비보조 신호가 0이면 stop 표식(`stopped_by`)을 철회한다. 평소 판정 경로를 타고 terminal에는 `stopped_by`가 없다(CS-09). 이는 CS-05의 "EPERM이어도 `stopped_by`" 판정을 갱신한다.
3. 같은 command_id의 반복 요청은 원래 최종 결과와 `repeat: true`, 원 `requested_by`를 돌려준다. `already_exited`는 stop episode 없이 끝난 명령에만 쓴다(CS-04).
4. 소유자가 선언한 `unknown`은 정정하지 않는다(SPEC이 한계로 명시).
5. 신원 uid 불일치는 `identity_unavailable`로 끝난다(CS-05 리뷰 P3-4).
6. `stop`은 canonical lowercase uuid만 받는다. 빈 `command`는 쓰지 않은 것으로 본다.
7. `command_interrupt.delivered`는 PTY에 바이트를 썼다는 뜻이다. 프로세스가 실제로 SIGINT를 받았다는 뜻이 아니다. 명령이 tty를 ISIG 끈 raw 모드로 바꿨다면 SIGINT가 되지 않는다.
8. 이미 닫힌 Task를 다시 취소한 응답(`rejected`, `task_closed`)에도 `host_commands`가 있다(provider 실패 시 `host_commands_unavailable: true`, 첫 취소와 같은 조건으로 `host_command_note`). 처음에는 "`task_closed` 응답에는 `host_commands`가 없다"로 기록했으나 2026-10-22 사용자 승인으로 바꿨다. 추가 필드만이고 `status`·`reason` 값은 그대로다. 취소가 아닌 `to_worker`의 `task_closed`는 그대로 `host_commands`가 없다.
9. `stopped_by`는 첫 신호 시도 직전에 부착한다. `reason` 길이는 strip 전 500자로 센다. call-key가 진행 중일 때 재전송은 `duplicate_in_flight`로 수용한다(CS-05).
10. 취소가 없는 stopped 실험은 `last_result.outcome = "stopped"`, Task `finished`다. `held_reason`은 view에 남는다(CS-09).
11. `left_session` unknown의 main `exit_code`는 CS-03에서 고쳤다(CS-02 판정 철회 항목).
12. 기존 `terminal_request` 등 기록의 명령 텍스트는 기능 이전 설계이며 "argv 없음"은 stop 감사 기록 기준이다(CS-G11).
13. 최종 시나리오의 모호 3건: 무전달이면 평소 판정, 원 요청자 유지, 취소된 Task 종료 알림 지연(기존 C-D70).
14. 시각 표현은 ms 단위로 바꾸지 않는다. 독립 테스트가 초 단위 형식을 고정한다.
15. 기록만 하는 한계: CS-07 감사 `input_owner`/`parent_mode`는 쓰기 이후 값, PTY 큐 0.25 s 초과 시 `fail_closed`가 이후 Ctrl+C·dispatch를 hold(기존 manual write와 동일), journal fsync가 `io_lock` 안(데드락 없음). CS-03 `send`가 session을 재확인하지 않음(SPEC 불변식 범위 밖)은 2026-10-23에 pin·verify·send 모두 재확인하도록 바뀌었다(a81d43f). `close` 직후 한 단계 신호 가능. CS-09 등록 예외 시 명령이 추적 없이 남음(runner_error 보류)은 2026-10-23 사용자 결정으로 추적 계속 + `command_stop_register_failed` 기록 + 중지 불가(`registration_failed`)로 바뀌었다(135ce17). CS-06 P3-3 stop 대기 abort 시 일반 명령 문구(보류).

## 9. 후속 과제

| 항목 | 출처 |
|---|---|
| Q3(처리됨, 후속 커밋): 사용자 TUI Task 줄이 stopped 실험을 `중지됨(사용자/Manager/Worker)`로, 남은 host 명령을 `명령 확인 불가` > `명령 실행 중` > `명령 종료 미확인` 중 하나로 표시. 계약·backend 변경 없이 `ui/product/model.py`만 수정. worker TDD(B1~B5), 독립 검증 pass(`test_product_task_stop_labels_independent` 24), 리뷰 approve | `open-questions.md` Q3 답변 a |
| 처리됨(2026-10-23): 두 owner의 safe-wrapper를 `command_stop_owner.py` 공용 helper 하나로 통합 (9446e31) | Standards P2-2 |
| 부분 처리(2026-10-23): `_stage`만 pin·signal·wait helper로 분해(89cbc08, 차등 테스트 5dea474). `_start_unlocked`·`_follow`·`_execute` 분해는 기존 부채로 열려 있음 | Standards P2-3 |
| 처리됨(2026-10-23): pin 때 대상 session이 스캔 시점과 다르면 거절 (a81d43f, 독립 테스트 2d24f07) | P3-1 |
| 처리됨(2026-10-23): service provider가 OMP pane pid를 두 owner의 `forbidden`으로 연결 (96eca26, 7절) | P3-2 |
| 부분 처리(2026-10-23): command-stop이 `flow_tasks.py`에서 쓰던 `run._record`를 `WorkflowRun.shell_request_id` accessor로 대체 (9446e31, d3a0436). 그 밖의 `_record` 사용은 남아 있음 | 리뷰 |
| 처리됨(2026-10-23): 엄격한 단일 `COMMAND_ID`를 `command_stop_owner.py`에 두고 3곳이 공유 (96eca26, 7절) | 리뷰 |
| 처리됨(2026-10-23, 사용자 결정): 등록 실패여도 추적(follow)을 계속하고 `command_stop_register_failed`를 기록하며 중지는 불가 (96eca26, 135ce17, cd42a92) | CS-09 |
| 수용(2026-10-23): 유입이 owner가 등록한 uuid뿐이고, 상한을 두면 `previous_backend`로 오분류되므로 무제한으로 둔다 | 리뷰 |
| DoS(partially): 위 id 집합은 수용(2026-10-23, 유입이 owner 등록 uuid뿐), 결과 보관(`RESULTS_KEPT` 256)은 일부 완화 | Security |
| 처리됨(2026-10-22): 테스트가 띄운 명령의 범위로 좁힘 (e326fdd) | 5절 |
| 처리됨(2026-10-23): 읽기 실패를 기억하고 30 s 뒤 재시도 (44ef1d0) | delta 재리뷰 P3 |
| 처리됨(2026-10-23): provider 실패 시 마지막 성공 집합을 residual 점검에 사용 (bb0f810) | 보안 delta 리뷰 P3 |
| 처리됨(2026-10-22): `already_exited`의 `ended_at`이 실제 종료 시각(소유자가 종료를 보고한 시각). `_Entry.ended_at`에 기록 | 최종 Standards P2 |
| 처리됨(2026-10-23): `_stage` 분해(89cbc08)와 테스트 `/proc` helper 공용 모듈 `tests/backend/command_stop_support.py`(12a4129). 이 모듈로 신원 helper 위치도 정리됨 | 최종 Standards P2 |
| 처리됨(2026-10-22, 사용자 승인): 재취소 `task_closed` 응답에 `host_commands` 추가(8절 8번) | 최종 Spec P3 |
| 처리됨(2026-10-23): 고정 상한을 부하에 견디는 한도로 바꿈 (12a4129) | 5절 |
| P3(2026-10-23): `registration_failed` 응답이 `_live` 항목을 지우지 않음. 실제로는 도달 불가라 수정하지 않음 | C-T3 리뷰 |
| P3(2026-10-23): 원 예외 미보존, 30 s 재시도 경계 테스트 없음 | 44ef1d0 리뷰 |
