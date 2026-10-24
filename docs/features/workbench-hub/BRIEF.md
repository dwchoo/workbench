# Workbench Hub·다중 WB 프로젝트 — 요구사항 합의 초안

Revision: r0.1; state: draft(문서 전체 승인 요청 전). 작성일: 2026-10-25.
아래 HB-D01~HB-D10은 2026-10-25 요구사항 인터뷰(grilling)에서 사용자가 결정하고, 같은 날 결정 목록 전체를 확인한 것이다. 개별 결정의 확인은 이 문서 전체의 승인이 아니며, 문서 승인과 구현 권한도 별개다. 승인 reference·digest는 승인 시 별도 파일로 남긴다.

## 문제, 사용자와 목표

- [관측 사실] Workbench는 이미 WB 프로젝트마다 backend 하나를 띄우고, 서로 다른 WB 프로젝트는 동시에 실행된다(아래 "저장소 근거"). 그러나 등록된 WB 프로젝트를 한곳에서 보고, 켜고 끄고, 골라 들어가는 수단은 없다. UI client는 한 socket에 고정되어 실행 중 다른 WB 프로젝트로 전환할 수 없다.
- [사용자 결정 HB-D01] 사용자는 여러 WB 프로젝트를 한 사람이 동시에 운영한다. 목표는 사용자당 하나의 Hub가 WB 프로젝트 목록·켜기/끄기·진입을 제공하고, 그 위에 [Task Inbox & Web Board](../task-inbox/BRIEF.md)(이하 (A))를 올리는 것이다. 이 문서는 (B)이며 (A)보다 먼저 진행한다.
- [사용자 결정 HB-D01] Core가 완성되었으므로 Phase 2 작업을 지금 시작할 수 있다. 이는 AGENTS.md의 "Phase 2 구현은 core 완성·검증 이후"를 충족한 것으로 사용자가 판단한 것이며, 이 문서의 구현 권한을 뜻하지 않는다.

## 공통 용어

[CONTEXT.md](../../../CONTEXT.md)의 WB 프로젝트, WB 프로젝트 root, WB 프로젝트 상태, Hub, Hub 등록, WB 프로젝트 켜기/끄기, 소유자, 대기 사유, 화면 분리 / detach, Workbench 종료를 따른다. 이 문서는 정의를 중복하지 않으며 충돌하면 CONTEXT.md가 우선한다.

- [사용자 결정 HB-D03] 인터뷰에서 사용자가 말한 "workspace"는 기존 용어 "WB 프로젝트"를 뜻한다. 새 용어 "workspace"는 만들지 않는다.

## 범위와 비목표

- 포함: (1) 사용자당 하나의 Hub와 그 자동 시작·수명. (2) WB 프로젝트 registry(자동·수동 등록, 등록 해제). (3) 목록 조회와 WB 프로젝트 켜기/끄기. (4) TUI에서 목록 → 진입 → detach 뒤 목록 복귀. (5) (A)의 웹 서버를 Hub가 제공하는 것(웹 기능 자체는 (A)에서 정한다).
- [사용자 결정 HB-D02] 소유자는 한 사람, 한 OS 사용자다. 계정 구조는 나중에 확장할 수 있게 두되 이번 범위에서 다중 사용자는 지원하지 않는다.
- 비목표: Hub 자체의 manager·worker, 새 종료 의미, 디렉터리 자동 탐색으로 WB 프로젝트 찾기, 같은 폴더의 두 번째 WB 프로젝트, 여러 폴더를 묶은 WB 프로젝트, 여러 host·여러 OS 사용자 관리, 프로젝트 간 자원 중재(GPU 등. (A) D-27은 안내만 한다).
- 지원 환경은 core와 같은 Linux다. 사용자 서비스 관리자(systemd --user)는 선택 기능 `wb hub enable`에서만 쓴다.

## 제약과 호환성 약속

- [기존 계약] WB 프로젝트 하나에 backend 하나다(`.wb/backend.lock`). Hub는 이 lock을 대체하거나 우회하지 않는다.
- [사용자 결정 HB-D04] Hub는 WB 프로젝트 실행의 전제가 아니다. Hub가 죽거나 `wb hub stop`으로 멈춰도 실행 중 WB 프로젝트의 backend·manager·worker·host terminal은 영향을 받지 않는다. WB 프로젝트 안에서 `wb`를 실행해 직접 연결하는 기존 경로도 Hub 없이 동작해야 한다.
- [사용자 결정 HB-D07] 끄기는 기존 Workbench 종료(`shutdown_request` → 진행 중 작업 표시 → 확인 → `shutdown_confirm`, 작업이 바뀌면 거절)를 그대로 쓴다. TUI와 웹이 같은 절차를 쓴다.
- [기존 계약 WI-AC-03, WI-D03] 프로젝트별 작업 이력·DB·로그는 각 `.wb/`에 둔다. Hub registry는 WB 프로젝트의 위치·식별만 기억하고 프로젝트 상태를 옮기거나 합치지 않는다. 등록 해제는 `.wb/`를 지우지 않는다.
- [기존 계약 WI-AC-15] 인식한 root·project_id·실제 프로세스가 맞지 않으면 상태를 알리고 자동 실행·잘못된 연결을 보류한다. Hub의 켜기·진입에도 같은 원칙을 적용한다.
- [기존 계약과의 관계, WI-P07] 설치 BRIEF는 "시스템 서비스 자동 등록"을 후속 범위로 유보했다. `wb hub enable`은 사용자가 명시적으로 실행하는 선택 기능이고 기본은 꺼짐이며 자동 등록이 아니다. 설치 중에 서비스를 등록하지 않는다.
- [기존 ADR-0001] Hub도 tmux 등 외부 terminal multiplexer를 실행·의존하지 않는다.

## 공개 동작과 수락 예시

ID는 HB-AC-nn이다. "관측"은 `wb` 화면, `wb hub status`, Hub 목록(TUI·웹), 프로세스 존재 여부처럼 사용자가 볼 수 있는 값이다.

- HB-AC-01 (HB-D05): Hub가 없을 때 어떤 `wb` 명령을 실행하면 Hub가 background로 시작된다. 그 terminal을 닫아도 Hub는 남는다. 같은 OS 사용자의 두 번째 `wb`는 새 Hub를 만들지 않고 기존 Hub를 쓴다(사용자당 하나).
- HB-AC-02 (HB-D04, D05): `wb hub stop`은 Hub만 멈춘다. 켜져 있던 WB 프로젝트는 계속 실행되고, 다음 `wb` 실행으로 시작된 Hub의 목록에서 켜짐으로 보인다.
- HB-AC-03 (HB-D04): Hub 프로세스가 비정상 종료해도 실행 중 WB 프로젝트의 manager·worker·host 실험은 계속되고, 그 WB 프로젝트 안에서 `wb`로 직접 연결할 수 있다.
- HB-AC-04 (HB-D05): 재부팅 뒤 기본 설정에서는 Hub가 자동으로 뜨지 않고 다음 `wb` 실행에서 시작된다. `wb hub enable`을 한 경우에만 부팅 뒤 사용자 서비스로 시작된다. `wb hub enable`의 세부(해제 명령, 로그인 전 시작 여부)는 미결정이다.
- HB-AC-05 (HB-D06): `wb init` 또는 WB 프로젝트의 첫 시작은 그 WB 프로젝트를 registry에 자동 등록한다. 이미 있던 WB 프로젝트도 이번 기능 도입 뒤 처음 시작할 때 등록된다. `wb hub add <path>`로 수동 등록할 수 있다.
- HB-AC-06 (HB-D06): Hub는 디렉터리를 탐색해 WB 프로젝트를 찾지 않는다. 등록도 시작도 하지 않은 `.wb/`는 목록에 나오지 않는다.
- HB-AC-07 (HB-D06): `wb hub remove <path>`는 목록에서만 뺀다. 그 WB 프로젝트의 `.wb/` 자료는 그대로 남고, 이후 다시 등록하면 이전 이력으로 이어서 사용한다.
- HB-AC-08 (HB-D07): 꺼진 WB 프로젝트를 켜면 화면 연결 없이 그 프로젝트의 backend가 background로 시작되고 목록이 켜짐으로 바뀐다. 이미 켜져 있으면 두 번째 backend를 만들지 않는다.
- HB-AC-09 (HB-D07): 켜진 WB 프로젝트를 끄면 진행 중 작업 목록을 보여 주고 확인을 받은 뒤 종료한다. 확인 전에 작업이 바뀌면 종료를 거절하고 새 목록을 다시 보여 준다. 확인하지 않으면 아무것도 멈추지 않는다. TUI와 웹에서 같다.
- HB-AC-10 (HB-D08): WB 프로젝트 밖에서 `wb`를 실행하면 Hub 목록이 보인다. 목록에서 WB 프로젝트를 고르면 같은 terminal에서 그 WB 프로젝트에 연결된다. Detach하면 목록으로 돌아오고, 그 WB 프로젝트는 계속 실행된다.
- HB-AC-11 (HB-D08): WB 프로젝트 안(또는 그 하위 폴더)에서 `wb`를 실행하면 지금처럼 가장 가까운 `.wb/`의 WB 프로젝트에 바로 연결한다. 이때도 Hub가 없으면 background로 시작된다(HB-AC-01).
- HB-AC-12 (HB-D09): 목록의 각 행에는 이름, 경로, 켜짐/꺼짐, 현재 Task와 단계, 명령 상태, 대기 사유(예: 일시정지, 재부팅 확인 전 보류, manager 입력창에 글이 있어 알림 대기), 처리 대기 요청 수, 마지막 활동 시각이 있다. TUI와 웹이 같은 항목을 보인다.
- HB-AC-13 (HB-D06, WI-AC-15): 등록된 경로가 없어졌거나 그 경로의 project_id가 등록 정보와 다르면 목록에 불일치로 표시하고 켜기·진입을 하지 않는다.
- HB-AC-14 (HB-D03): 같은 폴더에서 두 번째 WB 프로젝트를 만들 수 없고, 여러 폴더를 하나의 WB 프로젝트로 묶는 등록은 제공하지 않는다.

## 결정과 이유

모두 2026-10-25 인터뷰의 사용자 결정이다. 별도 ADR은 아직 없다. (A)의 결정은 [task-inbox BRIEF](../task-inbox/BRIEF.md) D-05~D-30과 [ADR-0003](../../adr/0003-board-agent-without-sandbox.md)~[ADR-0005](../../adr/0005-per-project-inbox-store-hub-single-writer.md)에 있다.

| ID | 상태·근거 | 결정 |
|---|---|---|
| HB-D01 | 사용자 결정 | 요구사항을 두 BRIEF로 나눈다. (B) Hub·다중 WB 프로젝트를 먼저, (A) Web Board를 그 위에 진행한다. 기존 task-inbox r0.2는 (A)의 입력이다. Core가 완성되었으므로 Phase 2를 지금 시작할 수 있다. |
| HB-D02 | 사용자 결정 | 소유자는 한 사람, 한 OS 사용자다. 외부 MCP client도 소유자 자신의 agent다. 계정 구조는 나중에 확장할 수 있게 둔다. |
| HB-D03 | 사용자 결정 | 기존 용어 "WB 프로젝트"를 쓴다. root 하나 = WB 프로젝트 하나 = backend 하나다. 같은 폴더에 두 번째 WB 프로젝트를 두지 않고 여러 폴더를 묶지 않는다. 사용자가 말한 "workspace"는 WB 프로젝트를 뜻한다. |
| HB-D04 | 사용자 결정 | Hub는 자체 manager·worker가 없다. registry, 수명 관리(목록·진입·켜기·끄기), 웹 서버를 맡는다. Hub가 죽어도 실행 중 WB 프로젝트는 영향이 없고, `wb hub stop`은 WB 프로젝트를 멈추지 않는다. |
| HB-D05 | 사용자 결정 | `wb`를 실행할 때 Hub가 없으면 background로 자동 시작한다. `wb hub stop`까지 남고 terminal을 닫아도 유지된다. 재부팅 뒤에는 다음 `wb` 실행에서 시작한다. 부팅 시 자동 시작은 선택 기능 `wb hub enable`(systemd --user)이며 기본은 꺼짐이다. OS 사용자당 Hub는 하나다(lock). |
| HB-D06 | 사용자 결정 | Registry는 `~/.local/state/omp-workbench/hub/` 아래에 WB 프로젝트 root 경로와 project_id를 기록한다. `wb init`·첫 시작에서 자동 등록하고 `wb hub add/remove <path>`로 수동 관리한다. 등록 해제는 `.wb/` 자료를 지우지 않는다. 디렉터리를 탐색하지 않는다. |
| HB-D07 | 사용자 결정 | 켜기는 그 WB 프로젝트의 backend를 background로 시작하는 것이다. 끄기는 기존 `shutdown_request` → 진행 중 작업 표시 → 확인 → `shutdown_confirm`이며 TUI·웹이 같다. 새 종료 의미를 만들지 않는다. |
| HB-D08 | 사용자 결정 | WB 프로젝트 밖의 `wb`는 Hub 목록을 보이고, 고르면 같은 terminal에서 연결하며 detach하면 목록으로 돌아온다. WB 프로젝트는 계속 실행된다. WB 프로젝트 안의 `wb`는 지금처럼 바로 연결한다. |
| HB-D09 | 사용자 결정 | 목록 항목(TUI·웹 공통): 이름, 경로, 켜짐/꺼짐, 현재 Task와 단계, 명령 상태, 대기 사유(일시정지, 재부팅 확인 전 보류, manager 입력창 대기 등), 처리 대기 요청 수, 마지막 활동 시각. |
| HB-D10 | 사용자 결정((A) D-07~D-11을 Hub 관점에서 다시 적은 것) | 네트워크·웹 접근의 경계(LAN 공개, HTTPS 선택, 로그인)와 웹에서 허용하는 제어(켜기/끄기·일시정지·재개·`confirm-boot`)는 (A) D-07~D-11에서 정한다. Hub는 그 웹 서버의 host다. 웹 listener가 열리지 않아도(비밀번호 미설정 등) Hub의 다른 기능은 동작한다. |

## 구현 방향과 검증 의도

아래는 방향이며 dispatch 범위나 seam 합의가 아니다. TDD seam은 계획 단계에서 orchestrator가 후보를 제안하고 사용자 확인을 받는다.

- 불변식: (1) Hub 유무와 무관하게 WB 프로젝트 backend는 혼자 산다. Hub는 backend의 부모·감독자가 아니다(켜기로 띄운 backend도 Hub 종료 뒤 살아 있어야 한다). (2) WB 프로젝트당 backend 하나라는 기존 lock이 최종 기준이다. (3) Hub의 목록 상태는 각 backend에서 관측한 값이며, 관측할 수 없으면 "연결 없음/불명"으로 구분해 표시하고 꺼짐으로 단정하지 않는다. (4) 끄기는 기존 종료 경로만 쓴다.
- 검증 의도: Hub 정지·비정상 종료 뒤 WB 프로젝트 생존, 사용자당 Hub 하나(동시 `wb` 실행 경합 포함), 자동 등록·수동 등록·등록 해제 뒤 `.wb/` 보존, 불일치 경로 보류, 목록 ↔ 진입 ↔ detach 왕복, 끄기 확인 중 작업 변경 시 거절, 재부팅 뒤 기본 미시작.
- 실제 OMP를 띄우는 live 검증은 기존 `tests/backend/installation/live_project_sessions.py`처럼 격리 home에서 수행한다.

## 저장소 근거

기준: HEAD `4ebaf9b`, 2026-10-25 정적 읽기(orchestrator 탐색 결과와 문서 작성 중 일부 줄 확인). 미커밋 변경이 있는 작업 트리다. 실행·재현은 하지 않았다.

| 사실 | 출처 |
|---|---|
| [관측 사실] 실행 위치에서 가장 가까운 `.wb/`를 찾아 WB 프로젝트를 정한다 | `src/workbench/backend/project.py:62-85` (`resolve_project_context`) |
| [관측 사실] `.wb/backend.lock`의 `InstanceLock`이 WB 프로젝트당 backend 하나를 보장한다 | `src/workbench/backend/paths.py:53`, `:115` |
| [관측 사실] 서로 다른 WB 프로젝트는 별도 runtime 디렉터리로 동시에 실행되며 테스트가 있다 | `src/workbench/backend/runtime_paths.py:46-72`, `tests/backend/installation/live_project_sessions.py:628-690` |
| [관측 사실] UI transport는 0600 Unix socket뿐이다. HTTP·WebSocket·MCP·인증·API key 코드는 `src`에 없다 | `src/workbench/backend/ui_server.py`, `src/workbench/contracts/ui_v1.py` |
| [관측 사실] UI client는 한 socket에 고정되며 실행 중 WB 프로젝트 전환 기능이 없다 | `src/workbench/backend/cli.py:302` (`_attach`), `src/workbench/ui/product/app.py:235` (`run_product`) |
| [관측 사실] 종료는 `shutdown_request`가 진행 중 작업과 token을 돌려주고, 사용자 확인 뒤 `shutdown_confirm`이 작업이 바뀌었으면 거절한다 | `src/workbench/backend/service.py:1427-1455`, `src/workbench/backend/cli.py:1010-1060` |
| [관측 사실] CONTEXT.md에는 "WB 프로젝트 root"·"WB 프로젝트 상태"가 있고 "workspace" 용어는 없다 | `CONTEXT.md` |
| [관측 사실] 설치 BRIEF는 시스템 서비스 자동 등록을 후속 범위로 유보했다(WI-P07, 최종 확인 대기) | `docs/features/workbench-installation/BRIEF.md` "범위와 비목표" |

## 미결정·보류 사항

| 항목 | 영향 / 차단되는 후속 작업 |
|---|---|
| HB-O01: Hub ↔ backend 사이의 정확한 IPC(기존 UI socket 재사용 여부, 상태 구독 방식) | 목록 상태 갱신·켜기/끄기 구현의 계약. SPEC 전 결정 필요 |
| HB-O02: `wb hub enable`의 세부(해제 명령, linger·로그인 전 시작, 실패 표시) | HB-AC-04의 선택 기능 부분 |
| HB-O03: 자동 시작을 하지 않는 명령의 범위(예: `wb hub stop`, `wb hub status`, `wb setup`) | HB-AC-01의 예외 목록 |
| HB-O04: 꺼진 WB 프로젝트를 목록에서 골랐을 때의 동작(켜고 연결할지, 켜기 확인을 받을지) | HB-AC-10의 꺼짐 사례 |
| HB-O05: 켜진 WB 프로젝트의 등록 해제, 초기화되지 않은 경로의 `wb hub add` 처리(거절 또는 init 안내) | HB-AC-05, 07 |
| HB-O06: 실행 중 Hub가 있을 때 설치본 업데이트의 순서(WI-AC-15는 backend 종료 후 업데이트를 안내한다. Hub도 같은 대상인지) | [workbench-update](../workbench-update/BRIEF.md)와의 정합 |
| HB-O07: 목록의 "처리 대기 요청 수"는 (A) 구현 전에는 값이 없다. 그동안의 표시 방법 | HB-AC-12 |
| HB-O08: ticket 분할과 일정, TDD seam | planning |

## 인계

HB-D01~10 확인 뒤 문서 전체 승인을 받으면 to-tickets 절차로 SPEC·ticket을 만든다. 승인 전에는 SPEC·ticket을 만들지 않는다. (A)는 이 문서 승인 뒤 같은 순서로 진행한다.
