# Manager 작업 계획과 Worker 실행자 역할 강화 — 요구사항 합의

Revision: r0.2. 상태: 사용자 결정 반영, 구현 완료(미커밋). 원문 요구는 [USER-PRD-20261029.md](USER-PRD-20261029.md)(2026-10-29 16:40 사용자 작성)이고, 아래 MWR-D01~06은 같은 날 대화에서 사용자가 Q63~Q68을 모두 권고안대로 고른 결정, MWR-D07은 MWR-D05 확인 결과를 보고받은 뒤의 Q69 결정이다. r0.2는 MWR-D07(worker `edit`·`write` 제거)과 live probe 결과를 반영한다.

## 문제와 목표

- Manager가 작업 지시서를 받으면 한 번에 처리하려 해서 worker가 큰 일을 떠안거나 제대로 처리하지 못한다. 여러 상황에 쓰는 다목적 스크립트를 만들고, 진행 표시 없이 줄을 계속 내려 쓰는 출력도 나온다.
- Worker가 OMP 안에서 명령을 직접 실행하려다 막히는 일이 다시 생겼다. 사용자 관찰로는 Ctrl+D로 worker OMP를 끝내고 다시 시작한 뒤에 주로 생긴다.
- 목표: (1) 역할 핵심 규칙을 OMP 시작 때 항상 들어가는 prompt에 둔다. (2) Manager는 먼저 계획하고 작은 단위로 나눠 단위마다 스크립트 하나를 만든다. (3) Worker는 실행·감시·보고만 하고 복잡한 지시는 단순화를 요청한다.

## 관측 사실(2026-10-29 조사)

- 역할 문구는 `omp_command()`가 `--append-system-prompt` 하나로 넘긴다(`src/workbench/backend/launcher.py` `omp_command`). 지금까지 항상 들어간 것은 `WORKER_REPORTING_PROMPT`(worker만, 보고 절약 규칙)와 `RECORDS_PROMPT`(공통)뿐이다. Manager에게는 역할 문구가 없었다.
- 실제 역할 규칙(명령은 `terminal`로만, 받은 `commands`만 그대로, 스크립트는 `.wb/scripts/`, `python -c`·heredoc 금지, 한 줄 진행 표시)은 필요할 때 읽는 skill(`omp_bridge/skills/to-worker/SKILL.md`, `references/task-scripts.md`, `to-manager/SKILL.md`, `references/terminal.md`·`reports.md`)과 bridge 도구 설명(`omp_bridge/g3/bridge.ts`)에만 있다.
- Worker에는 `bash`가 없다: `WORKER_TOOLS`=(read, grep, glob, edit, write, web_search, todo, task), overlay가 bash·eval을 끄고, 시작 격리 확인이 허용 목록 밖 도구를 누출로 보고한다(`launcher.py` `WORKER_TOOLS`, `role_overlay`, `isolation_leaks`).
- Pane 재시작은 같은 argv를 다시 보낸다(`service.py::restart_pane`, `tests/backend/test_restart_pane*.py`). 따라서 prompt가 빠지는 것은 아니다. 다만 새 worker 세션은 기억이 없고, manager의 후속 메시지(`flow_recovery.py`, `flow_tasks.py` 재전송) 전에는 skill도 읽지 않았다. 재시작 뒤 직접 실행 시도의 가장 유력한 원인은 핵심 규칙이 항상 켜진 prompt에 없었다는 점이다.
- 관련 결정: core-workbench `DECISIONS.md` C-D68(skill 안내만으로는 강제되지 않아 `--tools` 허용 목록 도입), C-D69(manager가 정확한 `commands`를 정하고 worker는 실행·사실 보고), C-D72 (3)(열린 Task 밖 요청은 manager에게 보고), `OPERATING-CONTRACT.md` 12행(승인 범위 안 자동 진행)·77행(진행률·경과·잔여 시간은 terminal·로그에 두고 manager에게 정기 보고하지 않음).

## 범위와 비목표

- 포함: manager·worker의 항상 켜진 역할 prompt, `to-worker`·`to-manager` skill과 `task-scripts.md` 보강, worker `edit`·`write` 제거(MWR-D07)와 그에 맞춘 Task 계약 문구(`bridge.ts` `to_worker` 설명, `task-fields.md`, `MANAGER_RULE`), CW-16 F3 시나리오 코드 갱신, 관련 단위 테스트, live probe.
- 비목표: bridge 도구 schema(필드·형식) 변경, 계획 파일을 Workbench가 생성·검증하는 기능, 설치 승인 상태를 Workbench가 저장·강제하는 기능(승인 규칙은 manager 지침이다), Board agent prompt, 실제 모델 live probe 실행(다른 agent가 맡는다).

## 결정

| ID | 상태·근거 | 결정 |
|---|---|---|
| MWR-D01 | [사용자 결정 2026-10-29] Q63 | 역할 핵심 규칙은 manager·worker 모두 항상 켜진 역할 prompt(`--append-system-prompt`)에 짧게 넣는다. 세부 절차는 skill에 둔다. |
| MWR-D02 | [사용자 결정 2026-10-29] Q64 | Manager는 작업 지시서·작업 명령을 받으면 먼저 계획한다. 단순 스크립트 하나나 명령 하나로 끝나는 단위로 나누고 OMP `todo`에 기록한다. 단계가 많으면 `<project>/.wb/plans/<YYYYMMDD>-<slug>.md` 체크리스트 파일도 만들고 스스로 갱신한다. |
| MWR-D03 | [사용자 결정 2026-10-29] Q65 | 다목적 스크립트를 만들지 않는다. 단위마다 스크립트 하나를 `.wb/scripts/`에 두고, shell script 또는 inline 의존성(PEP 723)을 가진 `uv run` Python script로 쓴다. 환경에 영향을 주는 설치·설정은 먼저 사용자 승인을 받고, 승인 뒤에는 그 manager 세션 동안 같은 종류의 설치·설정을 묻지 않고 계속한다. |
| MWR-D04 | [사용자 결정 2026-10-29] Q66 | 오래 걸리는 스크립트는 한 줄을 갱신해 퍼센트·경과 시간·예상 종료 시간(ETA)을 보인다. 전체량을 모르면 처리 개수나 단계와 경과 시간을 보인다. 단계·주기마다 새 줄을 출력해 terminal을 내려가게 하지 않는다. manager 역할 prompt와 task-script 규칙에 적는다. |
| MWR-D05 | [사용자 결정 2026-10-29] Q67 (b) | Worker는 실행·감시·보고만 한다. 지시가 복잡하거나 코드 작성·판단이 필요하면 직접 하지 않고 `to_manager`로 manager에게 나누거나 단순화해 달라고 요청한다. `WORKER_TOOLS`에서 `edit`·`write`를 뺀다. 단, 빼기 전에 현재 worker 흐름 중 파일 쓰기가 필요한 것이 있는지 확인하고, 있으면 빼지 않고 보고한다. 확인 결과는 아래 "MWR-D05 확인 결과"에 있다. |
| MWR-D06 | [사용자 결정 2026-10-29] Q68 | 구현 뒤 실제 모델 live probe로 재시작한 worker의 동작을 확인한다(`tests/backend/probes/live_worker_restart_role_probe.py`, 결과는 아래 "Live probe 결과"). |
| MWR-D07 | [사용자 결정 2026-10-29] Q69 (b) | MWR-D05 확인 결과(아래)를 보고받고, "지시로만은 안될 것 같다"는 이유로 worker의 `edit`·`write`를 뺀다: `WORKER_TOOLS`=(read, grep, glob, web_search, todo, task). 파일·스크립트는 manager가 만들고 고치며, worker는 Task의 `commands`만 실행한다. orchestrator 정리: 작은 변경은 manager가 정확한 편집 명령(예: `sed -i …`)을 `commands`에 넣어 worker가 실행한다. C-D68 (1)의 "edit/write 유지"와 C-D69 (6)(e)를 대체한다. |

### MWR-D05 확인 결과(2026-10-29)와 MWR-D07

- 파일 쓰기가 필요 없는 흐름: 보고는 `to_manager`, 기록은 `workbench_records`(둘 다 bridge 도구로 `--tools`와 무관하다), terminal 로그는 backend journal이 쓴다. 긴 명령은 harness가 임시 스크립트로 쓴다(C-D69 (6)(c)). staged `response_contract` 답변은 도구를 쓰지 않는다. `explorer` subagent는 원래 읽기 전용이다.
- 파일 쓰기를 전제로 했던 것: C-D68 (1)·C-D69 (6)(e)의 도구 유지 결정, CW-16 F3 시나리오(worker가 `write`로 `src/f3.txt`를 쓰고 commit), Task 계약 문구(`spec.paths`="worker가 바꿀 수 있는 경로", `kind: work`="commands, code changes"), C-D72 (3)의 Task 없는 직접 요청.
- 1차 작업은 이 의존 때문에 도구 제거를 멈추고 보고했고, 사용자가 Q69 (b)로 제거를 결정했다(MWR-D07). 처리: `WORKER_TOOLS`와 `WORKER_FORBIDDEN_TOOLS`(bash·eval·edit·write) 갱신. 격리 누출 확인은 허용 목록에서 계산하므로 worker의 `edit`·`write`는 이제 누출로 보고된다. 계약 문구는 "Task의 commands가 바꿀 수 있는 경로"와 "worker는 commands를 실행하고 manager가 파일·스크립트를 만든다(작은 변경은 `sed -i` 같은 정확한 편집 명령)"로 바꿨다. `MANAGER_RULE`·`bridge.ts`·`to-worker`의 "직접 하지 말 것" 괄호는 "명령·확인을 직접 실행하지 말고, 파일·스크립트는 미리 manager가 쓴다"로 바꿨다. F3는 manager가 자기 `write` 도구로 `src/f3.txt`를 쓰고 worker는 `git add`·`git commit`만 실행하도록 코드를 고쳤다(live harness는 다시 돌리지 않았다. `VERIFICATION.md`의 F3 근거는 MWR-D07 이전 기록이다). C-D72 (3)의 직접 요청은 이제 `terminal` 명령으로만 수행된다.

### 기존 결정과의 관계

- 역할 prompt·skill 변경은 C-D68·C-D69를 확장한다. 명령 실행은 여전히 `terminal`로만 하고(C-D68 (1)), 정확한 명령은 manager가 정한다(C-D69 (6)(a)). 같은 규칙을 skill에서 항상 켜진 prompt로 끌어올리고, manager 쪽에 계획·단위 스크립트 규칙을 더한다. MWR-D07은 C-D68 (1)의 "edit/write 유지"와 C-D69 (6)(e)를 대체한다. `DECISIONS.md`의 두 조항 옆에 한 줄 개정 표시를 달았다(조항 본문은 그대로).
- `OPERATING-CONTRACT.md` 77행과 맞는다. MWR-D04의 퍼센트·경과·ETA는 host terminal의 한 줄에 보이는 것이고, worker는 이를 manager에게 정기 보고하지 않는다. `WORKER_REPORTING_PROMPT`는 그대로 둔다.
- `OPERATING-CONTRACT.md` 12행(승인 범위 안 자동 진행)과 MWR-D03의 "승인 뒤 같은 세션의 같은 종류 설치는 묻지 않음"은 같은 원칙이다.

## 수락 기준

관측 대상은 OMP argv(`--append-system-prompt` 값, `--tools` 값), skill 파일 텍스트, 격리 확인 결과, 단위 테스트다.

| ID | 관측할 동작 |
|---|---|
| MWR-AC-01 (D01, D02, D03, D04) | Manager OMP argv의 `--append-system-prompt`에 manager 역할 문구가 records 문구보다 먼저 들어 있다. 문구는 먼저 계획하기, 단순 명령·스크립트 하나 단위로 나누기, `todo` 기록, `.wb/plans/<YYYYMMDD>-<slug>.md` 체크리스트, 단위마다 스크립트 하나(`.wb/scripts/`), shell 또는 `uv run`+PEP 723 inline 의존성, 설치 승인과 세션 내 지속, 한 줄 갱신 진행 표시(퍼센트·경과·ETA, 전체량 모르면 개수·단계+경과, 새 줄 금지)를 말한다. Worker argv에는 이 문구가 없다. |
| MWR-AC-02 (D01, D05, D07) | Worker OMP argv의 `--append-system-prompt`에 worker 역할 문구가 보고 문구·records 문구보다 먼저 들어 있다. 문구는 파일 편집·shell 도구가 없다는 것, 파일·코드·스크립트를 쓰거나 고치지 않고 스스로 판단하지 않는다는 것, Task의 명령만 `terminal`로 실행한다는 것, 코드·파일 변경·판단이 필요하거나 복잡한 지시는 `blocked`로 보고하고 나누거나 단순화해 달라고 요청한다는 것, `response_contract` 단계가 우선한다는 것, 끝에 또는 조치가 필요한 문제에서 사실을 보고한다는 것, `response_contract`가 없으면 manager 메시지 전에 to-manager skill을 읽는다는 것을 말한다. Manager 문구는 여러 단계 요청에서 먼저 계획하고(단일 사소한 명령은 계획 불필요), worker가 실행하는 모든 스크립트·파일을 manager가 쓰며 작은 변경은 정확한 편집 명령을 줄 수 있다고 말한다. 서로의 문구는 다른 역할 argv에 없다. |
| MWR-AC-03 (D01) | 두 역할 문구는 짧다. Manager 역할 문구 1,200자 이하, worker 역할 문구 800자 이하를 테스트가 고정한다. |
| MWR-AC-04 (D01, D07) | Pane 재시작(Ctrl+D 뒤 `restart_pane`) 뒤의 argv도 시작 때와 같고 두 역할 문구를 그대로 담는다. 재시작한 worker의 `--tools`에는 bash·eval·edit·write가 없다(`test_restart_pane_independent_p27q.py`). |
| MWR-AC-05 (D02, D03, D04) | `to-worker` skill(entry와 링크된 reference)은 먼저 계획하기, `todo`·계획 파일, 단위 스크립트를 직접 쓰는 것은 manager 일이라는 점을 말한다. `task-scripts.md`는 다목적 스크립트 금지, 단위마다 스크립트 하나, shell 또는 `uv run`+PEP 723, 설치 승인 규칙, 퍼센트·경과·ETA 한 줄 진행 표시와 새 줄 금지를 말한다. 기존 skill 길이 제한(5,000자·600단어·60줄)을 지킨다. |
| MWR-AC-06 (D05) | `to-manager` skill은 worker가 코드·스크립트를 만들거나 고치지 않고, 복잡하거나 코드·판단이 필요한 지시는 `blocked`와 `reason`으로 manager에게 나누거나 단순화해 달라고 요청한다고 말한다. "자기 도구(read, edit)로 작업한다"는 안내는 없다. |
| MWR-AC-07 (D07) | Worker `--tools` 값은 read, grep, glob, web_search, todo, task이고, 격리 확인은 worker에서 관측된 `edit`·`write`(와 bash·eval)를 누출로 보고한다. Manager는 자기 파일 도구를 유지한다. `to_worker` 설명·`task-fields.md`·`MANAGER_RULE`은 manager가 파일·스크립트를 만들고 worker는 Task의 명령만 실행한다고 말한다. |

## Live probe 결과(MWR-D06, 2026-10-29)

다른 agent가 실제 모델로 `tests/backend/probes/live_worker_restart_role_probe.py`를 실행했다(OMP 18.13.2, 결과 `/tmp/mwr-probe/`). 통과: Ctrl+D 뒤 재시작한 worker가 명령을 `terminal`로만 실행했고(bash·eval 시도 없음), manager는 `todo`로 계획하고 단위 명령으로 위임했다. 확인하지 않은 경로: `.wb/scripts/` 스크립트와 계획 파일 작성, 복잡한 Task에서 worker의 `blocked` 단순화 요청. probe는 MWR-D07 이전의 prompt로 실행됐다.

## 테스트

- 새 테스트: `tests/backend/test_launcher_role_prompts.py`(AC-01~03, AC-07 도구·누출), `tests/backend/test_workbench_skills_role_rules.py`(AC-05~07 문구).
- 바뀐 기대값: 역할 문구가 붙은 정확한 argv를 고정한 테스트(`test_paths_launcher.py`, `test_omp_home.py`, `test_omp_isolation_independent_p27m.py`의 `expected_append_prompt`, `test_restart_pane_independent_p27q.py`), worker 도구 목록을 고정한 테스트(`test_launcher_models.py` 2곳, `test_paths_launcher.py`, `workbench_report/test_manager_integration.py`).
- 회귀: `tests/backend`의 `test_launcher*`, `test_workbench_skills*`, `test_role_boundary*`, `test_restart_pane*`, `test_omp_isolation*`, `test_omp_home*`, `test_paths_launcher.py`, `tests/backend/work_records`, `tests/backend/workbench_report`, `tests/bridge`, `tests/integration`(live 제외 기본 discover).
