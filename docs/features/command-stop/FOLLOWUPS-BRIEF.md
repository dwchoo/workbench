# Workbench 명령 stop 후속 3건 — 요구사항 합의

Revision: r0.3; state: 구현·검증 완료(main `5704482`), 결정은 Root 결정(사용자 위임) 그대로. 2026-10-24 사용자는 Root가 결정 대기로 정리한 세 항목, 즉 (1) CS-O01·CS-O02, (2) CS-O10 사용자 stop UI, (3) raw log rotation 활성화에 대해 "1,2,3 전부 완료해"라고 Root에게 위임했다. FU-D01~08은 그 위임 아래 Root가 고른 설계이고, FU-D09는 (3)에 대한 사용자 결정이다. 테스트 seam도 위임 아래 채택했다. 다만 CLAUDE.md가 seam 합의를 요구하므로 최종 보고에서 사용자 확인을 받는다(FU-O02). 이 문서는 [BRIEF.md](BRIEF.md)(r0.2, CS-D01~17)를 고치지 않고 그 보류 항목을 이어받는다. CS-O03은 계속 보류한다.

구현 상태: FU-D01~08은 main에 구현됐고 검증을 마쳤다(`ee5a312..5704482`). FU-D01~05(CS-O01·O02)는 Opus, FU-D06~08(CS-O10)은 Sonnet이 구현했다. 결과는 아래 "구현 결과"에 있다. FU-D09의 활성화 실행은 아직 하지 않았다(FU-O01). seam 사용자 확인(FU-O02)도 아직 남아 있다.

## 문제와 목표

[BRIEF](BRIEF.md)는 CS-D10으로 CS-O01·O02를, 비목표로 CS-O10을 보류했다. 이번에 셋을 닫고 raw rotation 활성화 결정을 기록한다. 사건 근거는 [문제 보고서](../../../bug-report/wb-20261021T014331151600Z-49ca426ea2fe4efc9154afef94d310f9.md)다(미커밋 자료).

- 보고 순서(CS-O02; E-08, H-06): Manager는 elapsed 125.1s/73.15% 보고를 먼저 받고 55.0s/42.13% 보고를 나중에 받았다. 같은 시기에 deferred(`current_omp_state_not_safe`, blockers=[busy])가 관측됐다. 유실은 확인되지 않았고, 메시지의 생성·배달 시각은 미확인이다. 거꾸로 온 진행률이 복구 판단을 흐렸다.
- 취소 뒤 Task 없는 명령(CS-O01; E-02b, E-11, H-05): 중지용 Task가 01:20:52Z에 취소됐다(commands_run=[]). 약 3분 뒤 01:23:49Z에 Worker의 checkpoint 명령이 owner=worker, task_id=null로 실행됐다. 이 checkpoint는 중지용 Task가 지시한 작업이었다(E-08). 취소 통지의 수신·처리 순서는 미확인이라 권한 우회로 단정하지 않는다. 현재 계약은 Task가 없으면 확인 없이 `task_id=null`로 실행한다. 그래서 Worker가 취소를 모른 채 취소된 일을 이어갈 수 있다.
- 사용자 stop(CS-O10): CS-D04의 Ctrl+C는 PTY에 `\x03`을 쓴다. SIGINT를 무시하는 명령은 이것으로 끝나지 않고, tty의 ISIG를 끈 raw 모드 명령에서는 SIGINT조차 되지 않는다(SPEC 기술 보완). 지금 사용자 수단은 Manager에게 stop을 부탁하거나, shell 전체를 죽이는 `prefix k`뿐이다.
- raw rotation(LG-D12): 활성화는 Q-01 통과 뒤 별도 사용자 결정을 요구한다. FU-D09가 그 결정이다.

## 범위와 비목표

- 포함: (1) Worker→Manager 보고의 Task별 FIFO, `report_seq`, 같은 run의 done/blocked 뒤 보고 거절. (2) free-work 취소 뒤 task-less `terminal` 명령의 1회 fence와 `after_cancel_of` 기록. (3) 사용자 명령 stop UI(`prefix x`)와 backend `user` 권한. (4) raw rotation 활성화 결정 기록.
- 비목표: Task 취소가 명령을 죽이게 하는 것(CS-D01 유지), task-less 명령 일괄 금지, 보고의 drop·병합, 다른 stream의 전달 차단, 사용자가 직접 시작한 shell·foreground 프로그램 stop, 임의 PID kill, rotation 비활성화 명령 신설, CS-O03.
- 대상 명령의 정의는 [BRIEF](BRIEF.md) "범위와 비목표"를 따른다. 용어는 [CONTEXT.md](../../../CONTEXT.md)가 우선한다.

## 결정과 이유

| ID | 상태·근거 | 결정 |
|---|---|---|
| FU-D01 (CS-O02) | Root 결정(사용자 위임). 코드상 추월 경로가 있다. `to_manager` 보고에는 순번이 없다. `_defer`는 재시도 `due`를 뒤로 미루고, `_next_due`는 큐를 순서대로 훑어 `due`가 지난 첫 항목을 고른다. 그래서 backoff 중인 앞 보고를 뒤 보고가 앞지를 수 있다. E-08이 이 경로였는지는 미확인이다. Backend는 progress를 Task 상태에 쓰지 않고, done/blocked 수락은 run_id·closed 확인으로 보호한다. | Worker→Manager 보고는 Task별 FIFO로 전달한다. 같은 Task의 앞 보고가 deferred면 뒤 보고는 기다린다. FIFO는 보고를 버리거나 합치지 않는다. pause 중 progress를 다시 보내지 않는 기존 규칙과 취소 withdraw는 그대로다. payload에 Task별 단조 증가 `report_seq`를 싣는다. 다른 Task와 다른 stream은 막지 않는다. |
| FU-D02 (CS-O02) | Root 결정(사용자 위임). 지금은 막혀 있지 않다. done/blocked가 큐에 들어간 뒤 수락 전까지 Task는 run_id를 유지한다(`*_report_pending`). blocked 수락 뒤에도 run_id가 남는다. 이 동안 같은 run의 progress/report는 받아들여진다. | 같은 run의 done/blocked가 큐에 들어간 뒤 오는 progress/report는 거절한다. 다른 run의 보고는 영향이 없다. |
| FU-D03 (CS-O01) | Root 결정(사용자 위임). task-less 명령에는 정당한 경우가 있다. Task 없이 사용자가 Worker에 직접 맡기는 작은 일이 그렇다(C-D68 (7), C-D69 (6)(b), C-D72 (3)). live probe `test_f45`는 취소 알림을 받은 Worker 턴의 task-less 명령이 10초 안에 `exited`로 끝나길 기대한다. E-11의 checkpoint도 정당했을 수 있다. | 일괄 금지하지 않고 취소 fence를 둔다. free-work Task 취소 시점부터 다음 중 가장 이른 시점까지 Worker의 task-less `terminal` 명령을 거절한다: (a) 취소 알림이 Worker에 전달됨, (b) fence 거절이 한 번 반환됨, (c) 더 새로운 Task가 생김. 거절은 아무것도 입력하지 않는다. reason은 `task_cancelled`이고, detail에 취소된 `task_id`와 "명령은 실행되지 않았다. 취소된 Task의 일이 아니면 다시 보내도 된다"를 담는다. 명령 없는 상태 조회는 허용한다. stop 인자는 새 명령이 아니므로 fence 대상이 아니다(CS-D09). |
| FU-D04 (CS-O01) | Root 결정(사용자 위임) | fence가 끝난 뒤의 task-less 명령은 실행하고 journal에 `after_cancel_of: <task_id>`를 남긴다. `task_id`는 null 그대로이고, 취소된 Task의 `host_commands`·`commands_run`에는 넣지 않는다. 실험 취소 흐름은 바뀌지 않는다. |
| FU-D05 (CS-O01) | 이유 | H-05의 위험은 통지 지연·순서 때문에 Worker가 취소를 알기 전에 취소된 일을 계속하는 것이다. fence는 취소 사실을 Worker에 한 번 확실히 알리고, 정당한 task-less 작업은 막지 않는다. 한계: 취소를 알고도 계속하는 Worker는 막지 못하고 기록만 남는다. E-11처럼 취소 몇 분 뒤 알림이 이미 전달된 상태라면 fence는 걸리지 않고 `after_cancel_of`만 남는다. |
| FU-D06 (CS-O10) | Root 결정(사용자 위임) | `prefix x`(Ctrl-x, 한글 ㅌ)는 `prefix k`와 같은 방식의 확인 modal을 연다. modal은 현재 host terminal의 Workbench 명령(owner, label)을 보인다. `x` 한 번이면 확정, 다른 키는 취소다. 확정하면 UI가 modal에 표시한 `command_id`를 담아 backend에 `stop_command`를 보낸다. |
| FU-D07 (CS-O10) | Root 결정(사용자 위임) | backend는 `CommandStopService.stop("user", command_id)`를 부른다. `user` 권한은 worker·manager 명령을 모두 대상으로 한다. 신호 단계(SIGINT→SIGTERM→SIGKILL), 신원 확인, 멱등, 상태(`running`/`stopping`/`exited`/`unknown`)는 CS-D03·D11·D14와 같다. 감사 기록의 `requested_by`는 `user`다. |
| FU-D08 (CS-O10) | Root 결정(사용자 위임) | 명령이 없으면 알림만 보이고 아무것도 보내지 않는다. 사용자가 직접 시작한 foreground 프로그램은 대상이 아니다. ui-server는 stop 동안 막히지 않고, 최종 결과(`exited`/`unknown`/refused)를 알림으로 보인다. `unknown`은 성공으로 보이지 않는다. snapshot은 Task 없는 명령까지 현재 host 명령을 노출한다. 이 경로가 CS-D04 Ctrl+C의 한계를 메운다. |
| FU-D09 (LG-D12) | 사용자 결정(2026-10-24 "1,2,3 전부 완료해"의 3). LG-D12가 요구한 별도 결정이다. | raw log rotation을 활성화한다. 코드 변경은 없다. 실행은 대상 프로젝트가 정해질 때까지 보류한다(FU-O01). log retention 문서에는 [BRIEF LG-D34](../dev-validation-log-retention/BRIEF.md#lg-d34--raw-rotation-활성화-결정-2026-10-24)로 기록했다. |

활성화 절차와 효과(FU-D09):

- 명령은 `omp-workbench logs raw-rotation activate --yes --evidence <UUID>`다. 대상 data dir의 `raw-rotation.json`을 쓰고, 이미 있으면 그 내용을 그대로 돌려준다.
- `--evidence`는 같은 data dir의 최신 `summary-compatibility` 기록 UUID여야 한다. preflight는 상태 admitted, 최신 기록 일치, identity·boundary 일치, OMP binary 경로·sha256·asset selection 일치를 본다. 그래서 대상 data dir에서 `logs summary-compat verify --scripted`를 먼저 새로 실행한다.
- 활성화 명령도 PATH의 `omp`로 credential 없는 live 관측을 한 번 하고, 통과와 정리 완료를 요구한다. `--yes`로도 건너뛸 수 없다.
- 되돌리는 명령은 없다. 파일을 직접 지우는 것은 지원 경로가 아니다.
- 예산은 segment 100,000,000 B × 5개다. 활성화 뒤 새로 시작한 source에만 적용되고, 기존·legacy 파일은 전환하거나 지우지 않는다. compatibility admission이 깨지면 새 source는 다시 legacy-stop 정책을 쓴다.
- 현황(2026-10-24 Root 확인): 이 호스트에서 대상 프로젝트 `.wb/`를 찾지 못했고, `omp-workbench`는 PATH에 없다. 실행 중인 Workbench(wb-demo)는 Root가 만질 수 없다. 사용자가 대상 프로젝트를 정해야 한다.

## 공개 동작과 수락 예시

ID는 FU-AC-nn이다. "관측"은 보고 payload, `terminal` 응답, journal·감사 기록, UI 표시 중 Manager·Worker·사용자가 볼 수 있는 값이다.

- FU-AC-01 (FU-D01): 같은 Task의 42% 보고가 backoff로 deferred인 동안 73% 보고가 생겨도 Manager는 42%를 먼저 받는다. 두 보고 모두 전달되고 `report_seq`는 단조 증가한다.
- FU-AC-02 (FU-D01): 한 Task의 deferred 보고는 다른 Task나 다른 stream(예: Manager→Worker)의 전달을 늦추지 않는다.
- FU-AC-03 (FU-D02): 같은 run의 done 또는 blocked가 큐에 들어간 뒤 progress/report를 보내면 거절된다. 다른 run의 보고는 영향이 없다.
- FU-AC-04 (FU-D03): free-work Task를 취소하고 알림이 Worker에 전달되기 전에 task-less `terminal` 명령이 오면, 아무것도 입력되지 않고 `task_cancelled`로 거절된다. detail에 취소된 `task_id`와 "실행되지 않았음"이 있다. 같은 시점의 상태 조회와 stop은 정상이다.
- FU-AC-05 (FU-D03): fence는 (a)(b)(c) 중 가장 먼저 오는 것으로 끝난다. 거절 뒤 새 호출로 같은 명령을 보내면 실행된다. 알림을 받은 Worker 턴의 task-less 명령은 처음부터 거절되지 않고 10초 안에 `exited`를 돌려준다(`test_f45`).
- FU-AC-06 (FU-D04): fence가 끝난 뒤 실행된 task-less 명령의 journal 항목에는 `after_cancel_of: <task_id>`가 있고 `task_id`는 null이다. 취소된 Task의 `host_commands`·`commands_run`에는 나타나지 않는다. 실험 취소의 응답과 상태는 이전과 같다.
- FU-AC-07 (FU-D06): `prefix x`를 누르면 현재 host 명령의 owner와 label이 modal에 보이고, `x` 한 번이면 stop이 시작된다. 다른 키는 아무것도 보내지 않고 닫는다. Ctrl-x와 한글 입력 상태의 ㅌ도 같다.
- FU-AC-08 (FU-D07): Worker 명령과 Manager 실험 명령 모두 사용자 stop으로 멈춘다. 감사 기록의 `requested_by`는 `user`다. 반복 stop은 멱등이고, SIGINT를 무시하는 명령은 SIGTERM→SIGKILL로 이어진다.
- FU-AC-09 (FU-D08): host 명령이 없을 때 `prefix x`는 알림만 띄운다. 사용자가 직접 시작한 foreground 프로그램은 대상으로 보이지 않는다. stop 동안 UI는 멈추지 않고 최종 결과를 보이며, `unknown`은 성공으로 보이지 않는다.
- FU-AC-10 (FU-D08): snapshot은 task-less 명령을 포함해 현재 host 명령(`command_id`, owner, label, 상태)을 노출한다.

## 테스트 seam

Root의 제안이며 위임 아래 채택했다. 사용자 확인은 FU-O02다. 표의 테스트 파일은 신설한다.

| 항목 | seam | 잡는 것 | 놓치는 것 |
|---|---|---|---|
| A (FU-AC-01~03) | `HandoffService`/`TaskFlow` + fake mailbox, `tests/backend/test_report_order.py` | backoff 중 추월, Task별 FIFO, 다른 stream 비차단, `report_seq`, done/blocked 뒤 거절 | 실제 OMP 전달 지연 |
| B (FU-AC-04~06) | `TerminalService.handle` + `TaskFlow` fixture, `tests/backend/test_cancel_fence.py`. 통합 단계 회귀는 `test_f45` | fence 종료 조건 (a)(b)(c), 거절 응답, 입력 없음, 사후 journal 표식 | 실제 취소 알림 경로의 타이밍(`test_f45`가 일부 보완) |
| C (FU-AC-07~10) | backend 권한·service·`ui_server`·protocol(`tests/backend/test_command_stop_user.py`, `tests/contracts`), UI model(`tests/ui/test_product_command_stop.py`), 실제 PTY raw 모드 stop | `user` 권한 범위, 감사 `requested_by`, protocol schema, modal·키 처리, snapshot 노출, ISIG off 명령의 실제 종료 | 실제 터미널 에뮬레이터의 키 입력 |

B와 C는 신호·수명·권한 경계를 건드리므로 독립 test_designer 검증이 필수다.

## 대체·한정하는 기존 조항

- CS-O01은 FU-D03~05, CS-O02는 FU-D01~02, CS-O10은 FU-D06~08로 해소한다.
- CS-D10은 일부 해제한다. task-less 취소 fencing과 보고 순서는 이 문서가 다루고, 변환·검증 완료 의미(E-07/E-12, CS-O03)는 계속 보류한다.
- [SPEC.md](SPEC.md):23 비목표의 "사용자용 stop UI"를 해제한다. 사용자 stop은 Worker·Manager stop과 같은 서비스를 쓴다.
- C-D68 (7)의 Task 없는 `terminal` 허용은 유지한다. 취소 fence 동안 한 번 거절하는 좁은 예외만 둔다.
- BRIEF CS-D02의 "stop 요청자는 Worker와 Manager"에 `user`를 더한다. CS-D04의 Ctrl+C 전달은 그대로 두고 `prefix x`가 그 한계를 보완한다.
- LG-D12가 요구한 별도 사용자 결정은 FU-D09다.

## 저장소 증거

기준: HEAD `ee5a312`, 2026-10-24 정적 읽기. 실행·재현은 하지 않았다. 아래는 구현 전 상태이고 줄 번호는 구현으로 바뀌었다. 구현 뒤 상태는 "구현 결과"를 따른다.

| 사실 | 출처 |
|---|---|
| `TO_MANAGER_KINDS`는 answer·progress·done·blocked·report다 | `src/workbench/backend/flow.py:129` |
| `_next_due`는 큐 순서대로 훑어 `due`가 지난 첫 pending 항목을 고른다. `_defer`는 지수 backoff로 `due`를 미룬다 | `flow.py:1191-1200`, `:1374-1394` |
| pause 중 keep 표시 없는 메시지(progress 등)는 `held_paused`로 끝나 다시 보내지 않는다 | `flow.py:1284-1293` |
| `_to_manager` payload에 순번이 없다. done/blocked는 `*_report_pending`과 keep·wait 표시를 단다 | `src/workbench/backend/flow_tasks.py:1307-1367` |
| done 수락은 Task를 닫고(`_close`가 run_id를 지움), blocked 수락은 run_id를 남긴 채 `blocked`로 둔다 | `flow_tasks.py:1398-1421`, `:1442-1447` |
| `active()`는 닫히지 않은 최신 Task다. 없으면 `terminal`의 task_id는 null이 된다 | `flow_tasks.py:682-686`, `src/workbench/backend/flow_terminal.py:843-848` |
| `_finish_cancel`은 Task를 닫고 미전송 메시지를 withdraw한 뒤 취소 알림을 큐에 넣는다. 실험 취소 종료·시작 실패에서도 불린다 | `flow_tasks.py:1247-1284`, 호출 `:1228`, `:1804`, `:2063`, `:2082`, `:2346`, `:2394`, `:2424` |
| `terminal` 계약: Task 없이도 실행하고 journal에 `task_id: null`을 남긴다. `_handle`은 상태 조회(`command: null`)와 stop을 새 명령 경로보다 먼저 처리한다 | `flow_terminal.py:37-38`, `_handle` `:776-819`, `_stop_request` `:821-828` |
| `STOPPABLE_BY`에는 `user`가 없어 지금 `stop("user", …)`는 `not_owner`로 거절된다 | `src/workbench/backend/command_stop.py:331`, `:947-965` |
| `stop_command`는 지금 Manager bridge 도구 이름이다 | `src/workbench/backend/flow_recovery.py:67` |
| snapshot `host_commands`는 Task별이고, Task 없는 명령은 어느 Task에도 속하지 않아 빠진다 | `src/workbench/contracts/ui_v1.py:118-133` |
| `prefix k` 확인 modal과 Ctrl 별칭. Ctrl-x(0x18)는 비어 있고, 자모표에서 ㅌ은 x다 | `src/workbench/ui/product/model.py:70-72`, `:83-90`, `src/workbench/ui/product/input.py:25-26` |
| raw rotation CLI, preflight, live 관측, 활성화 파일, 예산 | `src/workbench/backend/cli.py:690-722`, `:1262-1267`; `summary_compatibility.py:598-614`; `summary_compat_identity.py:50`; `raw_rotation.py:42-67`; `paths.py:73`; `src/workbench/storage/log_raw/segments.py:31-32` |
| `test_f45`: 취소 알림이 주입된 Worker 턴의 `echo F45_C`가 10초 안에 `exited`여야 한다 | `tests/integration/live_cw16_flow_policy.py:693-761` |

## 구현 결과

2026-10-24 main에 반영했다(`ee5a312..5704482`). CS-O01·O02는 Opus, CS-O10은 Sonnet이 구현했다. 독립 test_designer 검증과 리뷰는 Sonnet이 했다. 두 묶음의 리뷰 판정은 모두 approve-with-nits였고, nit는 모두 고쳤다. 결정(FU-D01~08)은 바꾸지 않았다. 아래는 결정 문구가 정하지 않은 세부를 구현이 정한 값이다.

| 묶음 | 커밋 |
|---|---|
| CS-O10 사용자 stop | `7cf90d1` backend `stop_command`, `89bb8b5` UI `prefix x`, `523506d` `host_shell.command` 구성 정리, `dfed7cf` 독립 검증과 권한표 `user` 행, `d72a043` 인코딩할 수 없는 결과, `cb6f674` ui_v1 문서, `7c2c310` 테스트 고정, `1f87a00` thread 시작 실패 시 slot 반환·modal label 정리 |
| CS-O02 보고 순서 | `a8e630d` Task별 FIFO와 `report_seq`, `0d2ff37` answer 포함 고정, `f67f26d` payload 기대값, `19ffd08` gate를 저장 성공 뒤에만 설정(리뷰 P3-1) |
| CS-O01 취소 fence | `8d09b67` fence 1회 거절과 `after_cancel_of` |
| 공통 | `53fb0cd` 독립 검증과 닫힌 Task stop 테스트 5개의 재전송, `39ae2b7` 독립 검증 teardown, `8a0d4be` worker·manager skill 문서(리뷰 P2-1), `5704482` refactor-equivalence suite 정렬 |

### CS-O02 보고 순서 (FU-D01·D02)

- Task별 FIFO는 `HandoffService._next_due`가 지킨다. Worker→Manager 메시지는 같은 Task의 더 오래된 Worker→Manager 항목이 `pending`이나 `delivering`일 때만 기다린다. `delivered`·`unknown`·`rejected`·`withdrawn`·`held_paused`로 끝난 항목은 막지 않는다. 다른 Task, backend 알림, Manager→Worker는 영향이 없다.
- `report_seq`는 Task별로 1부터 센다. 값은 `FlowTask` ledger에 저장되어 backend 재시작 뒤에도 이어진다. 메시지마다 `_save_task`를 부르므로 progress·answer마다 fsync가 한 번 늘었다.
- ledger 저장이 실패하면 메시지는 큐에 들어가지 않고 `metadata_unavailable`로 held된다. 그래서 번호에 빈칸이 생길 수 있다. 같은 번호를 다시 쓰지는 않는다.
- 같은 run의 done/blocked가 큐에 들어간 뒤 오는 progress/report는 `run_report_final`로 거절한다. gate는 Task 저장이 성공한 뒤에만 선다(`19ffd08`).
- gate는 두 경우에 풀린다. (1) 그 done/blocked가 큐에 들어가지 못함(not queued). (2) Manager follow-up(`_work_follow_up`). follow-up은 같은 run_id에서 blocked를 running으로 되돌린다. done이 큐에 있는 동안 온 follow-up도 gate를 푼다. follow-up은 새 지시이므로 의도한 동작이다.
- gate는 메모리에만 있어 재시작하면 사라진다. outbox도 메모리에만 있으므로 받아들였다.
- `answer`는 FIFO와 `report_seq`에 포함되고, gate로 거절되지 않는다.

### CS-O01 취소 fence (FU-D03~05)

- fence는 free-work Task의 즉시 취소 경로에서, Worker에 취소 알림을 보내야 할 때만 선다. 알림이 필요 없는 취소(`not_needed`, Worker가 Task를 받은 적 없음)에는 fence가 없다. 알림이 큐에 들어가지 못한 경우(`not_queued:*`)에는 fence가 선다. 그때는 거절이 Worker가 취소를 알 유일한 길이기 때문이다.
- 상태는 `FlowTask.cancel_fence`로 저장된다. 거절은 `task_cancelled` 한 번이다.
- fence는 다음 중 먼저 오는 것으로 풀린다. (a) 취소 알림이 `submitted`(api_accepted)·`delivered`·`unknown`이 됨. (b) 거절이 한 번 반환됨. (c) 더 새로운 Task가 생김. 알림 전달 시도(`delivering`) 중에는 거절하지 않는다. 그 시도가 deferred로 끝나면 fence가 다시 선다.
- 명령 없는 상태 조회와 `stop` 인자는 fence 대상이 아니다. 다른 이유의 거절은 fence를 소모하지 않는다.
- backend가 재시작해도 armed fence는 남는다. 취소 알림은 다시 보내지 않는다.
- `after_cancel_of`는 더 새로운 Task가 생길 때까지 `terminal_request`·`terminal_started` 기록에 붙는다. `not_needed` 취소 뒤의 task-less 명령에도 붙는다.
- 알려진 작은 틈: `delivering` 통지 직전의 짧은 구간과 pause 재시도 loop에서 거절이 한 번 더 나올 수 있다. 영향이 작고 다시 보내면 실행된다.
- 닫힌 Task의 stop 테스트 5개(`tests/backend/test_command_stop_authority*.py`)는 fence 거절 뒤 한 번 다시 보내도록 바꿨다(`53fb0cd`).

### CS-O10 사용자 stop (FU-D06~08)

- 키는 `prefix x`, Ctrl-x, 한글 ㅌ이다. modal은 owner와 label을 보인다. Worker 명령의 label은 첫 줄 최대 120자, 실험은 `experiment <run_id 앞 8자>`다. modal은 이를 표시 폭에 맞춰 자른다.
- 확정은 x·ㅌ·Ctrl-x 한 번이다. 다른 키, paste, resize는 취소다. 일반 키로 취소할 때의 알림은 `prefix k`와 같다.
- 명령이 없을 때, 이미 stopping일 때, stop 요청이 진행 중일 때는 modal을 열지 않는다.
- UI는 ui_v1 `stop_command{command_id}`를 보낸다. `command_id`는 canonical UUID이고 payload는 거절한다. 모르는 header 필드는 다른 ui_v1 요청처럼 무시한다. `kill_pane`처럼 attach된 client만 보낼 수 있다.
- backend는 `stop("user", id, reason="user stop")`를 부른다. `STOPPABLE_BY`의 `user`는 worker·manager 명령을 모두 대상으로 한다. 감사 기록은 `requested_by: user`, 실험 outcome은 `stopped_by: user`다.
- UiServer는 stop마다 daemon thread를 하나 띄운다. 동시에 최대 8개이고, 넘으면 `queue_full`이다. thread를 띄우지 못하면 slot을 돌려주고 `internal_error`로 답한다.
- 응답은 두 번이다. 바로 `stop.status: stopping` ack를 보낸다. 끝나면 같은 id로 `final: true` 결과 frame(`exited`/`unknown`/refused/error)을 보내고 state를 push한다. 인코딩할 수 없는 결과는 error `unencodable_outcome`으로 바꾼다.
- backend shutdown 뒤의 새 stop은 `backend_shutdown`으로 거절된다.
- snapshot은 `panes.host_shell.command = {command_id, owner, label, state}`를 싣는다. Task 없는 Worker 명령도 포함한다.
- UI는 `exited`만 성공으로 보인다.
- 실제 PTY 테스트가 raw 모드(ISIG off) 명령을 사용자 경로로 멈추는 것을 확인했다.
- 남은 틈: `_current`가 설정되기 전의 짧은 구간은 snapshot에 없다. pause·boot hold 중 사용자 stop은 backend 통합 테스트가 없다.

### 검증

- 새 테스트 파일: `tests/backend/test_report_order*.py`, `tests/backend/test_cancel_fence*.py`, `tests/backend/test_command_stop_user*.py`, `tests/backend/test_ui_server_stop_command*.py`, `tests/contracts/test_ui_v1_stop_command_independent.py`, `tests/ui/test_product_command_stop*.py`.
- `*_independent*`는 독립 test_designer가 결정 문구에서 시나리오를 도출해 썼다. mutation 확인에서 주입한 결함을 모두 잡았다.
- refactor-equivalence suite(`tests/backend/test_flow_refactor_equivalence_independent.py`, `test_judge_when_ready_equivalence_independent.py`)는 새 `FlowTask` 필드 두 개(`cancel_fence`, `report_seq`)만 정규화하고 나머지 비교는 그대로다(`5704482`).
- 상세 근거는 [VERIFICATION.md](VERIFICATION.md) 10절에 있다.
- 통합 회귀(main `5704482`, 2026-10-24, gates 제외): 모든 suite exit 0.
  - backend 2390(skip 36, expected failure 1), ui 1003, terminal 305, integration 85(skip 18), workflow 78, recovery_boot 188, dev_validation 134(skip 1), tasks 43, storage 147, contracts 80, lifecycle 38, observation 95, backend/installation 214(skip 3), backend/maintenance 44, ui/status_workbench 2, storage/important_log 99, policy/recovery_manager 26, policy/pause_automation 129, node bridge 217(pass 197, skip 20).
  - 예외 하나: `tests/backend/maintenance`의 `test_update_cli.UpdateCLI.test_development_checkout_is_explained_and_refused`가 backend suite와 동시에 돌 때 한 번 실패했다. 다른 테스트의 backend 시작이 사용자별 maintenance `flock`을 잡고 있었다. 단독으로는 5번 중 5번 통과해 이번 변경과 무관한 부하 flake로 분류한다. 잔여물과 남은 process는 없다.

## 열린 항목

| 항목 | 내용 |
|---|---|
| FU-O01 | raw rotation 활성화 대상 프로젝트와 실행 시점. 결정은 [LG-D34](../dev-validation-log-retention/BRIEF.md#lg-d34--raw-rotation-활성화-결정-2026-10-24)로 기록했다. 사용자가 대상을 정하면 그 프로젝트에서 설치된 Workbench로 `logs summary-compat verify --scripted` 뒤 `activate`를 실행한다. |
| FU-O02 | 테스트 seam A·B·C의 사용자 확인. 구현은 위임 아래 채택한 seam으로 끝났고, 최종 보고에서 확인을 요청한다. |
| FU-O03 | 해소(2026-10-24). "전달됨"은 취소 알림의 `submitted`(api_accepted)·`delivered`·`unknown`이다. 전달 시도(`delivering`) 중에는 거절하지 않아, 알림이 주입되자마자 Worker가 보낸 명령도 거절되지 않는다. 남은 작은 틈은 "구현 결과" CS-O01에 있다. 실제 `test_f45` 결과는 통합 회귀에서 확인한다. |
| FU-O04 | 해소(2026-10-24). (1) `not_needed` 취소에는 fence를 두지 않는다. `after_cancel_of`는 붙인다. (2) `after_cancel_of`는 더 새로운 Task가 생길 때까지 붙인다. (3) `answer`는 FIFO·`report_seq`에 넣고, done/blocked 뒤 gate로 거절하지 않는다. |
| CS-O03 | 변환 완료와 독립 검증 통과의 구분(E-07/E-12). 계속 보류한다. |

## Handoff

FU-D01~08 구현과 검증은 끝났다. SPEC·BRIEF·VERIFICATION에 후속 표시를 남겼다. 남은 일은 FU-O01(raw rotation 활성화 실행)과 FU-O02(seam 사용자 확인)다. FU-O02 확인 전에는 seam을 사용자 합의로 보지 않는다.
