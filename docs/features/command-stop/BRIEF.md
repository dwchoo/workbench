# Workbench 실행 명령 개별 stop — 요구사항 합의

Revision: r0.2; state: 승인됨(approved, 외부 기록 `.workflow/command-stop/brief-approval.json`). 아래 CS-D01~17은 2026-10-21 대화에서 사용자가 밝힌 결정이고(CS-D12~17은 r0.1의 열린 결정 CS-O04~09에 대한 권고안 수락), r0.2 전체는 2026-10-21 사용자 승인("승인")을 받았다. 승인 label만으로는 구현 권한이 되지 않으며, 승인 reference·digest는 승인 시 별도 파일로 남긴다.

## 문제, 사용자와 목표

2026-10-21 사건에서 사용자는 free-work Task를 취소했지만 같은 host 명령이 약 9분 35초 더 계속됐다. 사용자는 Ctrl+C로도 끊지 못했고, Worker는 자기 명령을 중지할 수단이 없었으며, 점유된 host terminal은 종료용 새 명령도 거절했다. 결국 Manager가 별도 OS 경로로 신원을 확인한 뒤 SIGINT를 보내 끝냈다([문제 보고서](../../../bug-report/wb-20261021T014331151600Z-49ca426ea2fe4efc9154afef94d310f9.md), E-01~E-06, E-08. 이 보고서는 아직 커밋되지 않은 자료다).

목표는 Task 취소를 종료로 오해하지 않게 하는 것과, Workbench가 시작한 host 명령을 정해진 권한자가 개별로 안전하게 멈추는 stop 기능을 두는 것이다. 사용자·Worker·Manager 모두 점유된 terminal과 무관한 경로가 필요하다.

## 공통 용어

[CONTEXT.md](../../../CONTEXT.md)의 Workbench 실행 명령, 명령 stop, 명령 상태, 명령 소유권, 잔존 확인, Terminal 입력 제어권, Worker, Manager, Task를 따른다. 이 문서는 그 정의를 중복하지 않는다. 일반 용어 정의와 이 BRIEF가 충돌하면 CONTEXT.md가 우선한다.

## 범위와 비목표

- 포함: (1) free-work Task 취소 응답·상태가 실행 중인 명령을 드러내는 것. (2) Worker와 Manager가 호출하는 명령 stop. (3) SIGINT → SIGTERM → SIGKILL 자동 단계와 잔존 확인. (4) 사용자 Ctrl+C의 직접 전달. (5) stop의 감사 기록.
- 대상은 Workbench가 시작해 command_id로 추적하는 host 명령(Worker `terminal` 명령, Manager의 실험 host 명령)뿐이다. 사용자가 직접 시작한 shell·foreground 프로그램, 그 밖의 프로세스, Workbench·OMP 자신은 stop 대상이 아니다.
- 비목표: Task 취소가 명령을 죽이게 하는 것(CS-D01), 임의 PID kill 권한, 사용자용 명령 stop UI 신설(Ctrl+C 전달만 포함), 협력적 취소 token을 runner 내부 단계에 전달하는 것, 아래 "열린·보류 결정"의 보류 항목.
- 지원 환경은 core와 같은 Linux PTY persistent shell이다. 다른 사용자 소유 프로세스는 신호를 보내지 않는다.

## 제약과 호환성 약속

- 기존 `terminal` 도구의 명령 실행·점유 거절·1분 점검·완료 알림 계약은 stop을 추가해도 유지한다. stop은 새 명령 거절 규칙(`terminal_command_running`)을 우회 경로로 쓰지 않는다.
- Task 취소(free-work: `cancelled`, 실험: `cancel_requested`)의 현행 의미는 유지한다. 이 정책은 현재 `flow_tasks.py` docstring과 테스트에만 있었고, CS-D01로 사용자 결정이 된다.
- 인수(takeover)만으로 실행 중 프로세스를 종료하지 않는 C-AC-08/C-D52, 호스트 shell kill(C-D63), 이전 backend 생존 프로세스의 `stop_survivor`(C-D71 (1))는 그대로 둔다.
- stop은 실제 종료 확인 전에 수정·재실행을 허용하지 않는 C-D45의 원칙과, 대상이 불명확하면 신호를 보내지 않는 원칙을 유지한다. argv·command 문자열은 보안 경계가 아니다.

### 이 기능이 대체·한정하는 core 조항

core 문서는 이 BRIEF에서 수정하지 않는다. 승인 뒤 SPEC·계획 단계에서 해당 조항에 후속 주석을 붙이는 작업을 정한다.

| core 조항 | 이 기능의 영향 |
|---|---|
| C-D68 (1)(8)(9)(10): `terminal`의 입력 형태(`command`만), 한 번에 한 명령, 명령 없는 호출은 즉시 상태 반환 | Worker의 stop은 기존 `terminal` 도구의 stop 인자로 요청한다(CS-D12). 입력 형태가 `command`만이라는 제한과 stop에 한해 "한 번에 한 명령" 제한을 좁힌다. 새 명령 실행 규칙은 유지한다. stop 호출의 대기(최대 약 20초)는 C-D68 (9)의 최대 120초 대기 안에 든다. 상태 반환에는 `stopping`을 추가한다. |
| C-D70 (3): `restart_worker`는 worker가 시작한 host 명령을 그대로 둔다 | 재시작이 명령을 종료하지 않는 점은 유지한다. 다만 그 명령은 stop 가능한 상태로 남고 새 Worker가 stop할 수 있다(CS-D08). |
| C-D63: host shell 강제 종료(`prefix k`) | shell 전체 종료와 별개로 명령 단위 stop이 추가된다. shell kill의 동작은 바뀌지 않는다. |
| C-AC-34 / C-D45: 강제 종료는 위임 범위 포함 시에만, 아니면 사용자 확인 | Workbench가 신원을 검증한 추적 명령에 한해 Manager의 자체 판단·자동 SIGTERM/SIGKILL 단계를 허용한다(CS-D06). 그 밖의 대상은 현행 조항을 따른다. |
| C-AC-30 / C-D49, C-D71 (3): 자동화 일시정지·재부팅 확인 전에는 Workbench가 스스로 시작하는 동작을 보류 | stop은 위험을 줄이는 동작이므로 두 보류 상태에서도 허용한다(CS-D13). 새 명령 실행·자동 전달 보류는 바뀌지 않는다. |
| C-AC-08 / C-D52: 입력 owner가 manager면 수동 입력 거부, `prefix t,c` 인수 뒤 전달 | 단독 Ctrl+C(`\x03`)만 인수 없이 foreground group에 전달한다(CS-D04). 다른 키·인수 규칙은 그대로다. |
| 제품 policy: `flow_tasks.py` docstring의 "host 명령은 Workbench가 죽이지 않는다" | free-work·실험 취소 모두 이 정책을 유지한다. 종료는 stop 기능이 맡는다. |

## 합의된 공개 동작과 수락 예시

ID는 CS-AC-nn이다. "관측"은 취소 응답, `/task`·`/terminal` 상태, 완료 알림, 감사 기록 중 사용자·Manager·Worker가 볼 수 있는 값이다.

- CS-AC-01 (CS-D01): free-work Task 취소 응답과 이후 `/task` 상태에, 해당 Task의 command가 아직 실행 중이면 command_id와 상태(`running`)가 있고 "취소는 명령을 멈추지 않는다, 멈추려면 stop"이라는 안내가 있다. 명령이 없거나 이미 끝났으면 실행 중이라고 표시하지 않는다.
- CS-AC-02 (CS-D01): 취소 응답은 어떤 경우에도 종료를 뜻하는 문구·상태를 쓰지 않는다. 취소 뒤 명령이 계속 도는 동안 `/task`는 닫힘이지만 명령 상태는 `running`으로 별도 표시된다. 실험 취소는 지금처럼 `cancel_requested`와 대기 이유를 반환하고, 같은 command 상태 정보를 함께 준다.
- CS-AC-03 (CS-D02, D03, D12): Worker가 자기가 시작한 명령에 `terminal` 도구의 stop 인자로 stop을 요청하면 호출은 종료가 확인되거나 시한(약 20초)이 될 때까지 기다린 뒤 최종 상태를 반환한다. 명령이 SIGINT에 종료하면 반환 상태는 `exited`이고 exit code(예: 130)와 종료 signal(SIGINT)을 보인다. 요청 수락만으로 성공을 보고하지 않으며, 대기 중 다른 경로로 조회하면 `stopping`이 보인다. 통상의 명령 종료 알림은 별도로 한 번 전달된다.
- CS-AC-04 (CS-D03, D07, D12, D14): 명령이 SIGINT를 무시하면 SIGINT 후 10초에 SIGTERM, 다시 5초에 SIGKILL이 자동으로 이어지고 SIGKILL 뒤 최대 5초 종료를 기다린다. 각 신호와 전송 시각이 감사 기록에 있고, 종료되면 `exited`와 마지막 종료 signal을 반환한다. 단계가 진행되는 동안 상태는 `stopping`이며, stop 호출은 전체 시한(약 20초) 안에 최종 상태를 반환한다.
- CS-AC-05 (CS-D03, D15): 신호는 명령의 process group/tree에 보낸다. 종료 뒤 supervisor가 추적하는 잔존 프로세스(같은 process group, subreaper로 재부모화된 후손)가 있으면 같은 SIGINT → SIGTERM → SIGKILL 단계를 적용하고 결과를 보고한다. 추적 밖으로 빠진 프로세스(예: 별도 session)는 신호를 보내지 않고 보고만 한다. 확인하지 못한 범위는 "확인 불가"로 명시하며 "깨끗함"으로 표시하지 않는다.
- CS-AC-06 (CS-D02): 거절 사례는 신호 없이 이유와 함께 결과를 반환하고 감사 기록에 남는다. 다른 owner 소유 명령(Worker가 Manager의 실험 명령을 stop), 사용자가 직접 시작한 shell·foreground 프로그램, Workbench·OMP 프로세스, 범위 밖 command_id가 모두 거절 대상이다.
- CS-AC-07 (CS-D02): 존재하지 않거나 이미 `exited`인 command_id에 stop을 요청하면 어떤 프로세스에도 신호를 보내지 않고, "대상 없음/이미 종료"와 마지막 종료 상태를 반환한다.
- CS-AC-08 (CS-D03): 대상 PID가 재사용됐거나 시작 시각 등 신원 검증이 실패하면 신호를 보내지 않고 stale로 결과를 반환한다. 신호 직전에 신원을 다시 확인하며, 검증 중 신원이 바뀌면 중단한다. argv 일치만으로 대상을 확정하지 않는다.
- CS-AC-09 (CS-D11, D12): 명령이 host terminal을 점유한 동안에도 stop 요청이 처리된다. 같은 시점의 새 `terminal` 명령은 기존대로 `terminal_command_running`으로 거절되고, stop을 위해 host terminal에 입력하지 않는다. stop 인자는 새 명령 실행으로 취급되지 않는다.
- CS-AC-10 (CS-D11, D12): 같은 명령에 stop을 반복해도 멱등이다. 진행 중에 온 두 번째 요청은 같은 최종 결과를 기다려 반환하고, 끝난 뒤의 요청은 원래의 최종 결과를 반환한다. 어느 쪽도 신호 단계나 타이머를 새로 시작하지 않는다.
- CS-AC-11 (CS-D02, D06, D12): Manager는 별도 stop 도구로 Worker `terminal` 명령을 stop할 수 있다. 사용자 승인 없이 SIGTERM/SIGKILL 단계까지 진행하며, 요청자는 `manager`로 기록된다. 도구 호출은 CS-AC-03과 같이 최종 상태를 반환한다.
- CS-AC-12 (CS-D02, D06, D11, D16): Manager는 자기 실험의 host 명령을 stop할 수 있다. 취소가 `cancel_requested`로 호스트 종료를 기다리던 중이면 stop이 종료를 확인한 시점에 Task가 `cancelled`로 닫힌다(판정 없음). 종료가 `unknown`이면 닫히지 않고 이유가 표시되며, 이후 종료가 관측되면(CS-AC-23) 그때 닫힌다. 취소 없이 stop된 실험은 CS-AC-21을 따른다.
- CS-AC-13 (CS-D06): Workbench가 신원을 검증하지 못한 프로세스(예: 이전 backend 생존자, 신원 불명)에는 Manager의 자체 판단 stop이 적용되지 않는다. 기존 C-AC-34/C-D71 (1)의 확인·전용 도구 경로를 따른다.
- CS-AC-14 (CS-D08): `restart_worker`로 새 Worker 세션이 된 뒤 이전 Worker가 시작한 명령은 계속 실행되고, 새 Worker가 그 명령을 stop할 수 있다. 소유자 표기는 role(`worker`)이다.
- CS-AC-15 (CS-D09): 닫힌(`cancelled`) Task의 남은 명령을 Worker가 stop할 수 있다. 이 stop은 Task를 다시 열지 않고, 새 명령 실행·재개 권한을 주지 않으며, 감사 기록에 해당 Task가 명시된다.
- CS-AC-16 (CS-D04, D17): Workbench 실행 명령(Worker 또는 Manager 실험)이 host terminal의 foreground일 때, 사용자의 단독 `\x03`(입력이 `\x03` 한 바이트뿐)은 `prefix t,c` 인수 없이 그 명령의 foreground group에 전달된다. 입력 owner가 manager인 경우는 물론 인수·handoff 중간 상태에서도 foreground가 Workbench 실행 명령이면 같다. 입력 owner는 바뀌지 않으며 명령은 보통의 SIGINT 종료(exit 130)를 한다. 감사 기록에 요청자 `user`와 전달 사실이 남는다.
- CS-AC-17 (CS-D04, D17): `\x03` 외의 키·paste, 그리고 `\x03`이 다른 바이트와 섞인 입력은 현재처럼 `INPUT_OWNER_MANAGER` 등 기존 규칙으로 거절되며 일부 바이트만 전달하지 않는다. foreground가 Workbench 실행 명령이 아닌 경우(사용자 소유 prompt/job, 불명 상태)는 현행 규칙 그대로 동작하고 Ctrl+C 예외가 적용되지 않는다.
- CS-AC-18 (CS-D11, D14): SIGKILL을 보내고 5초를 더 기다려도 종료를 확인하지 못했거나(D 상태 등) 권한 오류로 신호를 못 보내면 stop 호출은 `unknown`과 이유를 반환하고 기록·표시한다. 성공·종료됨으로 표시하지 않으며 해당 command는 수정·재실행 가능하다고 보고하지 않는다.
- CS-AC-19 (CS-D02): stop은 host shell 자체와 Workbench·OMP를 종료하지 않는다. 명령 종료 뒤 host shell은 기존 규칙대로 사용자에게 돌아오고 출력 로그는 보존된다.
- CS-AC-20 (CS-D11, D14, D16): 모든 stop과 Ctrl+C 전달에 대해 요청자, 대상(command_id, 검증된 pid·시작 시각), 보낸 신호와 각 전송 시각, 종료 상태(exit code/signal), 잔존 확인 결과, 거절·`unknown` 이유, `unknown` 정정 이력, 실험 run의 `stopped` 요청자가 사후 조회 가능한 기록으로 남는다. 환경 변수 값·auth 정보는 기록하지 않는다.
- CS-AC-21 (CS-D16): 취소 없이 Manager가 stop한 실험 run의 outcome은 `stopped`이다. 실패·취소와 구분되고 성공/실패 판단이나 자동 복구 재시도를 만들지 않으며, 누가 stop했는지 보인다. 종료가 `unknown`이면 outcome은 확정하지 않는다.
- CS-AC-22 (CS-D13): 자동화 일시정지 중, 그리고 재부팅 확인(`confirm-boot`) 전 보류 중에도 Worker·Manager의 stop이 처리된다. 같은 상태에서 새 `terminal` 명령 실행과 자동 전달은 기존대로 보류된다.
- CS-AC-23 (CS-D14): `unknown`이던 명령의 종료가 나중에 관측되면 상태를 `exited`로 정정하고 exit code·signal을 채운다. 정정 이전의 `unknown`과 이유는 이력으로 남아 감사 기록에서 확인된다. 정정을 이유로 새 신호를 보내지 않는다.

## 결정과 이유

모두 2026-10-21 대화의 사용자 결정이다(CS-D01~11은 U1~U11에 대응, CS-D12~17은 r0.1의 열린 결정 CS-O04~09에 대한 권고안 수락). 별도 ADR은 아직 없고, 계획 단계에서 필요 여부를 정한다.

| ID | 상태·근거 | 결정 |
|---|---|---|
| CS-D01 | 사용자 결정 | free-work 취소는 계속 host 명령을 멈추지 않는다. 취소 응답과 `/task` 상태에 실행 중 명령(command_id, 상태)과 "취소는 중지가 아님" 안내를 보인다. 멈춤은 별도 stop 기능이다. 취소를 종료로 보고하지 않는다. |
| CS-D02 | 사용자 결정 | stop 요청자는 Worker와 Manager다. Worker는 자기 role이 시작한 명령만, Manager는 Workbench가 시작한 모든 host 명령(Worker `terminal` 명령과 Manager 실험 명령)을 멈춘다. 사용자 자신의 shell·foreground 프로그램, 다른 프로세스, Workbench·OMP는 대상이 아니다. |
| CS-D03 | 사용자 결정 | 종료 절차는 SIGINT → 10초 → SIGTERM → 5초 → SIGKILL이며 명령의 process group/tree에 적용한다. 이후 잔존 확인(자식, subreaper로 재부모화, 추적 가능한 별도 session)을 하고 상태를 구분해 보고한다. 요청 수락·큐잉을 성공으로 보고하지 않는다. |
| CS-D04 | 사용자 결정 | Workbench 실행 명령이 foreground이면 사용자 단독 Ctrl+C(`\x03`)를 인수 없이 해당 foreground group에 직접 전달한다. 그 밖의 키는 지금처럼 거절한다. |
| CS-D05 | 사용자 결정 | 문서는 별도 feature 폴더 `docs/features/command-stop/`에 둔다. 대체·한정하는 core 조항은 위 표에 목록으로 남기고 core 문서는 이번에 고치지 않는다. |
| CS-D06 | 사용자 결정 | Manager는 사용자 건별 승인 없이 자체 판단으로 stop하며 SIGTERM/SIGKILL 자동 단계도 포함한다. 범위는 Workbench가 검증한 추적 명령으로 한정한다. 이는 해당 대상에 한해 C-AC-34의 "위임 범위/사용자 확인" 요구를 좁히는 결정이다. 그 밖의 대상에는 C-AC-34가 그대로 적용된다. |
| CS-D07 | 사용자 결정 | Worker의 stop도 자기 명령에 대해 SIGINT부터 SIGKILL까지 전체 자동 단계를 수행한다. |
| CS-D08 | 사용자 결정 | 소유권은 OMP 인스턴스가 아니라 Worker role이다. `restart_worker` 뒤 새 Worker가 이전 Worker가 시작한 명령을 stop할 수 있다. |
| CS-D09 | 사용자 결정 | Task가 취소된 뒤에도 Worker는 그 Task의 남은 명령을 stop할 수 있다. 재실행·재개 권한은 주지 않는다. |
| CS-D10 | 사용자 결정 | Task 없는(task_id=null) 명령의 취소 fencing(E-11/H-05), 진행 보고 순서(H-06), 변환·검증 완료 의미(E-07/E-12)는 이번 범위 밖이며 보류 항목으로 남긴다. |
| CS-D11 | 사용자 결정 | 명령 상태는 `running` → `stopping` → `exited`(exit code와 있다면 종료 signal) 또는 `unknown`(종료 미확인, 이유 기록)이다. stop은 host terminal 점유와 무관하게 동작하고(`terminal_command_running` 경로 비경유), 중복 stop은 멱등이며, stale·unknown·exited command_id와 PID 재사용에는 신호를 보내지 않는다. 요청자·대상·신호와 시각·종료 상태·잔존 확인은 감사 기록으로 남기고, 실험 취소의 호스트 종료 대기는 stop이 종료를 확인하면 완료된다. |
| CS-D12 | 사용자 결정(CS-O04 권고 수락) | Worker는 기존 `terminal` 도구에 stop 인자를 추가해 요청하고, Manager는 별도 stop 도구를 쓴다. stop 호출은 종료가 확인되거나 시한(약 20초: SIGINT 10초 + SIGTERM 5초 + SIGKILL 후 확인 5초)이 될 때까지 기다린 뒤 최종 상태(`exited` 또는 `unknown`)를 반환한다. 통상의 명령 종료 알림은 별도로 전달한다. `terminal`의 기존 최대 120초 대기(C-D68 (9))와 일관된다. |
| CS-D13 | 사용자 결정(CS-O05 권고 수락) | stop은 위험을 줄이는 동작이므로 자동화 일시정지 중과 재부팅 확인 전 보류(C-D71 (3)) 중에도 허용한다. |
| CS-D14 | 사용자 결정(CS-O06 권고 수락) | SIGKILL 뒤 최대 5초 종료를 기다리고, 확인하지 못하면 `unknown`과 이유를 기록한다. 이후 종료가 관측되면 `exited`로 정정하되 정정 이력을 남긴다. |
| CS-D15 | 사용자 결정(CS-O07 권고 수락) | supervisor가 추적하는 잔존 프로세스(같은 process group과 subreaper 후손)에는 같은 SIGINT → SIGTERM → SIGKILL 단계를 적용한다. 추적 밖으로 빠진 프로세스는 신호 없이 보고만 한다. |
| CS-D16 | 사용자 결정(CS-O08 권고 수락) | 취소 없이 stop된 실험 run의 outcome은 `stopped`로, 실패·취소와 구분한다. 성공/실패 판단은 하지 않으며 stop 요청자를 기록한다. |
| CS-D17 | 사용자 결정(CS-O09 권고 수락) | 단독 `\x03`(입력에 `\x03` 한 바이트뿐)만 우회하고, 다른 바이트와 섞인 입력은 지금처럼 거절한다. 인수·handoff 중간 상태에서도 foreground가 Workbench 실행 명령이면 단독 `\x03`을 그 명령에 전달한다. |

## 구현 방향과 테스트 의도

아래 seam은 Root의 제안이며 사용자는 아직 확정하지 않았다. 확정 전에는 dispatch 범위로 보지 않는다.

- 불변식: stop 신호 경로는 host terminal 입력(shell 입력 파이프)에 의존하지 않는다. 신호 직전 신원(pid, 시작 시각, uid, boot id 등)을 다시 확인한다. 상태 `exited`는 종료 확인 뒤에만 쓴다. 잔존 확인 결과는 `exited`와 별도 필드다. stop 호출의 대기 시한(약 20초)은 단계 타이머의 합이며 호출이 끝나도 종료 관측과 `unknown` 정정은 계속된다. group 전체 신호(`killpg`)와 pidfd 기반 개별 신호 중 PID 재사용에 안전한 방식은 SPEC에서 정한다.
- 제안 seam:
  1. 취소 결과 dict와 workbench status — CS-AC-01, 02, 21. 잡는 것: 사용자·Manager가 보는 문구와 상태, run outcome. 놓치는 것: 실제 프로세스 동작.
  2. backend terminal-flow의 stop 메서드를 실제 PTY shell(`tests/terminal`)에서 실행 — CS-AC-03~05, 08~10, 18, 19, 22, 23. 잡는 것: 신호·tree·잔존 단계·신원·점유 중 동작·`unknown` 정정. 놓치는 것: 호출 권한 경계, 느림.
  3. 같은 메서드의 unit 수준 권한 검사 — CS-AC-06, 07, 11~15. 잡는 것: owner·대상 분류. 놓치는 것: 실제 신호 전달.
  4. Worker `terminal` 도구의 stop 인자 검증·bridge schema와 Manager stop 도구 dispatch — CS-AC-03, 06, 11, 14. 잡는 것: 입력 형태, 호출 경로, 대기·반환 계약. 놓치는 것: 프로세스 동작.
  5. 실제 `PersistentShell`을 쓴 `ShellPane.admit` — CS-AC-16, 17. 잡는 것: 입력 owner 우회 범위, 단독 `\x03` 판정, 중간 상태의 foreground group 전달. 놓치는 것: UI 키 입력 경로.
- 프로세스 수명·signal·신원 작업이므로 독립 test_designer 검증이 필수이고, worker는 범위를 좁힌 packet으로만 구현한다.

## 저장소 증거

기준: HEAD `31b80a4`, 2026-10-21 정적 읽기. 미커밋 변경이 있는 작업 트리를 읽었으며 `src/workbench/ui/product/model.py`에 기존 미커밋 변경이 있다. 실행·재현은 하지 않았다. 사건 자료(E-ID)는 보고서의 설치 snapshot에서 나온 것이며 실행 revision과의 동일성은 미확인이다.

| 사실 | 출처 |
|---|---|
| free-work 취소는 run을 취소하고 Task를 닫고 Worker에 알린다. 응답은 `{"status":"cancelled","task_id","worker_notified"}`이며 실행 중 명령 정보가 없다. 사건 E-02~E-04와 일치 | `src/workbench/backend/flow_tasks.py:1102-1111`, `_finish_cancel` `:1122-1158` |
| 실험 취소는 `cancel_requested`, `held_reason=cancel_waiting_for_host_exit`, `CANCEL_WAIT_DETAIL`("Workbench never kills it")을 반환한다 | `flow_tasks.py:184-186`, `:1094-1101` |
| "host 명령은 Workbench가 죽이지 않는다" 정책은 docstring과 테스트에만 있다 | `flow_tasks.py:51-59`, `tests/terminal/test_cw18_host_shell_independent_p27w.py:12` |
| `terminal` 도구 인자는 `command`(문자열/null)뿐이다 | `omp_bridge/g3/bridge.ts:398-411`, `src/workbench/backend/flow_terminal.py:124`, `:215-224` |
| 명령 실행 중 새 명령은 `terminal_command_running`으로 거절된다 | `flow_terminal.py:1108-1118` (`_start`, 거절은 `:1112-1118`) |
| Task 없이도 `terminal`이 실행되고 `task_id=null`로 기록된다(계약) | `flow_terminal.py:37-38` |
| 명령 spawn: supervisor가 자식을 자기 process group으로 분리하고 `tcsetpgrp`로 PTY foreground로 만든다 | `src/workbench/terminal/shell_g2/lifecycle.py:663`, `:522-529`, `:698` |
| 명령 중 사용자 입력은 입력 owner가 manager라 `INPUT_OWNER_MANAGER`로 거절된다. 이유 문구는 "request takeover first"다 | `src/workbench/backend/panes.py:793-806`, `src/workbench/terminal/shell_persistent/adapter.py:390` |
| 인수는 UI의 `prefix t`(요청)와 `prefix c`(확인)이다. `prefix t,c` 안내는 job 보류 힌트(`JOB_HELD_HINT`)에만 있다 | `src/workbench/ui/product/model.py:152`, `:1245-1248`, `adapter.py:347-382` |
| 명령 단위 stop은 없다. UI `prefix k`는 host shell 전체를 종료한다(C-D63). `stop_survivor`는 이전 backend 생존자용이다(C-D71 (1)) | `src/workbench/ui/product/model.py:82`, `tests/recovery_boot/test_survivors_holds_independent_p27cw19.py:306` |
| 신원 pinning 선례: `process_state(pid, start_ticks, uid, boot_id)`와, OMP pane session 구성원을 pidfd로 고정하는 `_pin_member` | `src/workbench/backend/process_identity.py:47`, `src/workbench/backend/panes.py:125-141`, `:111-114` |

정적 추정: 위 pinning 방식이 command의 process group/tree stop에 그대로 쓰일 수 있는지는 미확인이다.

## 열린·보류 결정

| 항목 | 영향 / 차단되는 후속 작업 |
|---|---|
| CS-O01 (CS-D10, E-11/H-05): 취소 뒤 Task 없는 명령의 fencing 보류 | 취소 이후 Worker가 `task_id=null`로 새 명령을 시작할 수 있어 취소 효과가 약하다. stop 기능은 이와 독립이며, fencing은 별도 BRIEF 필요. |
| CS-O02 (CS-D10, H-06): 진행 보고 순서·지연 보류 | 종료 뒤 과거 진행 보고가 상태를 덮어쓸 수 있다. stop 상태는 command 단위 시각으로 구분하지만 보고 메시지 순서는 이번에 해결하지 않는다. |
| CS-O03 (CS-D10, E-07/E-12): 변환 완료와 독립 검증 통과의 구분 보류 | stop 후 인계 시 완료 의미 혼동 위험은 남는다. |
| CS-O10: 사용자용 명령 stop UI | 이번에는 Ctrl+C 전달과 Manager 요청만 제공한다. SIGINT를 무시하는 명령은 사용자가 Manager에게 요청하거나 `prefix k`(shell kill)로 대응한다. |

## Handoff

CS-D01~17 확인(열린 결정 CS-O01~03·10은 보류) 후 fresh Sol XHigh/Max session에서 `$to-tickets docs/features/command-stop/BRIEF.md`. 승인 전에는 SPEC·ticket을 만들지 않는다.
