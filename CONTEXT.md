# 도메인 용어

정의의 출처: [Phase 2 PRD](docs/OMP_Workbench_Phase2_Task_Inbox_PRD_v0.1.md)와 [Core 인터뷰 결정](docs/features/core-workbench/BRIEF.md).
요구사항의 합의 상태와 미결정 사항은 해당 기능의 BRIEF에서 관리한다.

| 용어 | 의미 |
|---|---|
| Workbench | Manager OMP, worker OMP, host persistent terminal과 내부 mailbox를 연결하는 작업 환경. 이전 제품명은 OMP Workbench이며 기본 명령은 `wb`다. 배포 이름·진입점은 `pyproject.toml`, 설치 방식은 [설치 BRIEF](docs/features/workbench-installation/BRIEF.md)를 따른다. PRD는 1차 core와 2차 부속 기능을 구분한다. |
| WB 설치본 | 연구 프로젝트의 실행 환경과 독립적으로 설치·관리하는 Workbench 프로그램과 배포 자산. 프로젝트별 작업 이력·로그와 구분한다. |
| WB 내장 skill | Workbench가 제공하고 설치본과 함께 관리하는 skill과 reference·helper 자산. 사용 중인 연구 프로젝트에 설치·복사하는 skill과 구분한다. |
| WB 프로젝트 상태 | 특정 연구 프로젝트에 속하는 작업 이력·DB·로그 등 이어서 작업하는 데 필요한 영속 자료. 프로그램 설치본과 구분하며 저장·복구 경계는 설치·사용 BRIEF에서 정한다. |
| WB 프로젝트 root | 한 WB 프로젝트의 작업 기준 디렉터리. 처음 실행한 폴더를 사용자 확인으로 정하고, 이후 하위 폴더에서 실행해도 가장 가까운 초기화된 프로젝트에 연결한다. Git root와 반드시 같지는 않다. |
| WB 프로젝트 | WB 프로젝트 root 하나와 그 상태, 하나의 Workbench 실행 환경으로 이루어진 작업 단위. 같은 폴더에 두 번째 WB 프로젝트를 두지 않고 여러 폴더를 하나로 묶지 않는다. 사용자가 말하는 "workspace"는 이것을 뜻하며 별도 용어로 쓰지 않는다. 개발 workflow 문서(`.codex/workflow-contract.md`, AGENTS.md의 Codex 절)의 "workspace"는 물리 작업 디렉터리를 뜻하며 이 용어와 다르다. |
| WB 상태 이관 | 기존 WB 상태 저장소의 작업 이력·로그 등을 새 프로젝트 상태 위치에서 이어 사용할 수 있도록 전환하는 작업. 원본 보존 여부와 지원 경계는 해당 기능의 BRIEF에서 정한다. |
| Core Workbench / 1차 | 자체 wrapper 안에서 manager와 worker의 대화·명세 전달·실행·관측을 제공하는 기반 기능. |
| Manager | 사용자와 논의하고 작업을 계획하며 코드를 수정하는 역할. Worker에게 실행할 작업을 전달한다. |
| Worker | 실험 실행, 로그 분석과 결과 보고를 맡는 역할. 코드 수정은 manager의 역할로 구분한다. |
| 위임 범위 | 사용자가 manager에게 자율 판단을 맡긴 작업의 경계. 구체적인 허용 행동과 예산·재확인 조건은 제품 동작 규약에서 정한다. |
| 자동 복구 재시도 | 실패한 작업을 복구하기 위한 재실행 시도. 최초 실행과 승인된 정상 개선 실험은 구분하며, commit 변경만으로 다른 작업이 되지 않는다. |
| Hang 의심 | 실행 진행이 정체되었을 가능성이 있어 로그와 프로세스 상태의 추가 조사가 필요한 상태. 단순한 무출력이나 실행 시간 초과를 뜻하지 않는다. |
| 실행용 worktree | Worker가 기준 commit의 코드를 실행하기 위해 사용하는 별도 Git 작업 디렉터리. |
| 개발용 worktree | Manager 또는 사용자가 코드를 수정하는 Git 작업 디렉터리. 실행용 worktree와 분리한다. |
| Terminal 입력 제어권 | Host terminal에 입력할 주체를 구분하는 권한. 사용자 인수 요청 즉시 새 자동 전송을 막는다. 이미 전달된 명령의 실행 상태와 실제 인수 확인은 별도로 다루며, 인수만으로 실행 중 프로세스를 종료하지 않는다. |
| Terminal 인수 확인 | 인수 요청 뒤 현재 입력 대상과 제어 상태를 확인한 사실. 이미 전달된 명령의 취소·종료를 뜻하지 않는다. 수동 입력은 확인된 현재 대상에 전달한다. |
| Host persistent terminal | Workbench가 실행되는 host의 shell 환경과 상태를 유지하며 명령을 실행하고 출력을 보여주는 terminal. 사용자·agent의 입력 권한과 앱 종료 후 생존 여부는 별도 동작 정책이다. |
| 관리된 shell | cwd·export·검증된 conda/venv 전환을 지원하며, 비호환 hook·trap 변경이나 상태 불명에서는 자동 복귀를 보류하는 Workbench 전용 shell. 같은 사용자 권한 코드의 고의적 내부 변조·event 위조를 방어하는 보안 경계는 아니다. |
| 자동 제어 대기 | 사용자가 같은 shell에서 수동 작업 뒤 handoff 절차(명령은 [Core BRIEF](docs/features/core-workbench/BRIEF.md)의 C-AC-08)를 실행하고 상태 확인을 거친 뒤, 그 interpreter가 prompt로 돌아가지 않고 전용 control 경로로 자동 명령을 기다리는 상태. 같은 shell의 PID·cwd·환경을 보존한다. |
| 화면 분리 / detach | 사용자 화면 연결만 닫는 동작. 같은 host에서 Workbench 실행·manager·worker는 유지된다. 주기 모델 점검은 자동화 활성 중 계속되고 일시정지 중 새로 시작하지 않는다. |
| Workbench 종료 | 화면 분리와 구분되는 명시적 실행 환경 종료 동작. 실행 중 실험의 중단·정리 순서는 제품 동작 규약에서 정한다. |
| 소유자 / owner | Hub와 모든 WB 프로젝트를 쓰는 단일 사용자(한 사람, 한 OS 사용자). 외부 agent와 Board agent도 소유자의 것이다. 웹에서는 로그인으로 소유자를 확인한다. |
| Hub | 한 OS 사용자에 하나만 실행되며 그 사용자의 WB 프로젝트를 등록·조회하고 켜고 끄고 진입시키며 Web Board를 제공하는 관리 서비스. 자체 manager·worker가 없고, Hub가 멈춰도 실행 중인 WB 프로젝트는 영향을 받지 않는다. 세부 합의는 [Hub BRIEF](docs/features/workbench-hub/BRIEF.md)에서 관리한다. |
| Hub 등록 | WB 프로젝트가 Hub의 관리 목록에 들어 있는 상태. 초기화·첫 시작 때 자동으로, 또는 사용자가 직접 등록한다. 등록 해제는 그 WB 프로젝트 상태를 지우지 않는다. |
| WB 프로젝트 켜기 / 끄기 | 켜기는 화면 연결 없이 그 WB 프로젝트의 실행 환경을 시작하는 것이다. 끄기는 진행 중 작업을 보여 주고 확인받는 기존 Workbench 종료이며 새 종료 의미가 아니다. |
| 대기 사유 | WB 프로젝트가 자동 진행을 멈추고 기다리는 이유. 예: 자동화 일시정지, 재부팅 확인 전 보류, manager 입력창에 글이 있어 알림 대기. 목록과 Web Board에 표시한다. |
| Task Inbox & Web Board / Phase 2 | 소유자·Board agent·외부 MCP client의 요청을 WB 프로젝트별로 접수·보관하고, Hub가 제공하는 웹에서 조회·작성하며, manager 검토, 승인 명세 및 실행 결과에 연결하는 선택적 부속 기능. 세부는 [Task Inbox BRIEF](docs/features/task-inbox/BRIEF.md)를 따른다. |
| 요청 / request | 소유자·Board agent·외부 MCP client가 제출한 검토 대상 자료. 접수 자체는 실행 승인이 아니다. |
| 요청 출처 | 요청을 작성한 주체: 웹의 소유자, Board agent, 또는 API key 이름으로 식별되는 소유자의 외부 agent. 요청은 외부 agent 외에도 이 출처들에서 올 수 있으며 출처는 항상 표시한다. |
| 요청 상태 | 요청이 놓인 단계. 접수부터 manager 검토, 질문 대기, 승인 명세 연결, 연결된 Task 진행, 종료까지의 단계를 구분한다. 단계 목록은 [Task Inbox BRIEF](docs/features/task-inbox/BRIEF.md)(D-13)를 따른다. Task 상태와 별개다. |
| 요청 revision | 실행 준비 이후 요청 내용을 고쳐 남긴 버전. 진행 중 Task를 바꾸지 않으며 새 명세 revision이 필요한지는 manager가 정한다. 명세 revision과 구분한다. |
| 검토 방식 | WB 프로젝트별 설정. 자동이면 manager가 쉬고 있을 때 Workbench가 새 요청을 manager에게 전달하고, 수동이면 자동으로 전달하지 않는다. 어느 쪽도 실행 중 작업을 선점하지 않는다. |
| 검토 후 자동 실행 | Manager 검토로 승인 명세가 연결된 요청이 출처와 관계없이 소유자의 건별 승인 없이 상시 위임에 따라 실행되는 것. 접수만으로는 실행되지 않는다. |
| API key 등급 | 외부 agent의 접근 범위. 등급별 권한은 [Task Inbox BRIEF](docs/features/task-inbox/BRIEF.md)(D-12)를 따른다. 어떤 등급도 terminal 사용, 승인 명세 연결, 실행 환경 제어를 할 수 없다. |
| Board agent | Hub가 실행·소유하는 소유자 전용 OMP agent. 어느 WB 프로젝트의 manager와도 별개이며, 등록된 WB 프로젝트들의 요청을 찾고 작성·정리한다. 사용자의 일반 shell과 같은 권한으로 동작하고 Hub 외의 외부 도구 연결은 쓰지 않는다. |
| Board agent 세트 / 웹 터미널 | Web Board에서 소유자가 쓰는 Board agent 화면과 일반 shell 화면 한 벌. 브라우저를 닫아도 유지되고 새로 연결한 곳이 즉시 인수한다. Manager·worker·host terminal 화면은 포함하지 않는다. |
| 승인 명세 / task specification | Manager가 권한 범위 안에서 확정하는 실행 조건과 완료 기준. 원본 요청과 구분하며 task ID와 revision으로 식별한다. |
| Task | 하나의 목표·완료 조건과 실행·복구 이력을 묶는 작업 단위. 명세 revision이나 commit 변경만으로 별도 작업이 되지 않는다. |
| 명세 revision | 작업 명세의 한 버전. 실행 중 변경은 새 버전으로 기록하며 기존 실행에 사용한 버전과 결과 이력을 보존한다. |
| 작업 완료 | Manager가 승인된 완료 조건과 worker의 검증 근거를 대조해 목표 달성을 확정한 상태. 명령의 정상 종료와 구분한다. |
| Run | 특정 작업 명세와 실행 입력에 연결된 한 번의 실행 시도. 재시도는 별도 run으로 구분한다. |
| 자동화 일시정지 | 새 자동 지시와 manager·worker의 새 자동 모델 작업을 보류하고 manager OMP의 진행 중 turn 중단을 요청한 상태. 주기 worker 모델 점검은 멈추며 로그·프로세스 수집과 이미 실행 중인 host 실험은 계속한다. |
| 일시정지 중 | 일시정지 요청 수락부터 사용자의 명시적 재개 또는 작업 취소 전까지의 상태. Turn 중단 요청·실제 중단 확인·도구 결과 불명은 서로 구분하며 수동 조작만으로 자동화가 재개되지 않는다. |
| Turn 중단 확인 | Manager OMP에서 진행 중이던 turn의 종료 event를 관측한 사실. 이미 실행된 도구의 결과나 rollback을 뜻하지 않는다. |
| 실험 환경 | 사용자가 선택한 Python/conda/venv와 실행 설정. Workbench 자체를 구동하는 환경과 구분한다. |
| 모델 점검 중지 | 주기 worker 모델 점검을 멈추는 동작. 자동화 일시정지 중에는 적용되며, 로그·프로세스 수집이나 실행 중 실험의 종료를 뜻하지 않는다. |
| Mailbox | 검토할 변경의 통지와 내부 역할 간 전달 수단. 영속 요청 저장소와 구분한다. |
| Fast 검증 | 현재 개발 저장소에서 핵심 OMP 호환 경로를 빠르게 확인하는 검증 모드. 모델 service tier의 fast와 구분한다. |
| 정밀 검증 | Fast보다 넓은 runtime·장애·호환 경계를 확인하는 개발용 검증 모드. 전체 출시 검증과 구분한다. |
| Raw log rotation | 수집 원문을 제한된 파일 집합에 순환 저장하고 오래된 구간을 삭제하는 보관 동작. 실행·관측 종료와 구분한다. |
| 중요 로그 | 실행 정보와 Task 명세·지시·보고·결정에서 추린 정보와 요약을 출처에 연결한 보조 기록. 원문이나 승인·완료 근거 자체를 대신하지 않는다. |
| 조건 충족 근거 | 승인된 실행 조건을 실제로 만족한 관측과 필요한 최소 원문·출처를 연결한 기록. Agent의 해석·요약과 구분한다. |
| 중요 로그 활성 DB | 새 중요 로그를 저장하는 현재 SQLite DB. 용량 정리 후에도 가득 차면 새 DB로 전환하고 이전 DB는 보존한다. |
| 중요 로그 보존 DB | 활성 DB 전환 뒤 사용자 정리까지 보존하는 이전 중요 로그 SQLite DB. 자동 삭제 대상에서 제외하며 운영 metadata DB와 구분한다. 복수 DB의 정리가 필요함을 사용자에게 표시한다. |
| WB 문제 보고서 | 사용자가 Manager OMP에서 직접 호출하는 skill로 작성하는 단일 Markdown 문서. WB 자체의 오류·불편·기대와 다른 동작에 관한 사용자 설명과 기존 자료의 읽기 조사 결과를 담고, 관측 사실·원인 가설·미확인을 구분한다. WB 이상과 관련된 실험 실패도 포함하며 코드 수정·재현 실행은 별도 작업이다. |
| Workbench 실행 명령 | Worker의 `terminal` 도구 또는 Manager의 실험이 host terminal에서 시작해 Workbench가 명령 ID로 추적하는 명령. 사용자가 직접 시작한 shell·foreground 프로그램과 구분하며, 명령 stop과 Ctrl+C 직접 전달의 대상은 이것뿐이다. 세부 합의는 [명령 stop BRIEF](docs/features/command-stop/BRIEF.md)에서 관리한다. |
| 명령 stop | Workbench 실행 명령 하나를 지정해 종료시키는 동작(신호를 단계적으로 높여 보내고 잔존 확인까지 하는 절차는 [명령 stop BRIEF](docs/features/command-stop/BRIEF.md)의 CS-D03을 따른다). Task 취소나 host shell 전체 강제 종료와 다르며, 요청 수락만으로 종료됐다고 보고하지 않는다. |
| 명령 상태 | Workbench 실행 명령의 상태. 실행 중, stop 진행 중, 종료 확인됨, 종료 미확인을 구분하며 미확인은 이유를 기록하고 이후 종료가 관측되면 정정하되 이력을 보존한다. 상태 이름은 [명령 stop BRIEF](docs/features/command-stop/BRIEF.md)를 따른다. Task의 취소·완료 상태와 구분한다. |
| 명령 소유권 | Workbench 실행 명령을 시작한 역할(Worker 또는 Manager)로 정하는 stop 권한의 기준. OMP 프로세스·세션이 아니라 역할 기준이므로 Worker 재시작 뒤에도 유지된다. |
| 잔존 확인 | 명령 stop 뒤 그 process tree의 자식·재부모화된 프로세스·추적 가능한 별도 session 프로세스가 남았는지 확인한 결과. 확인하지 못한 범위는 확인 불가로 구분하며 깨끗함을 뜻하지 않는다. |
| stopped (Run 결과) | 취소 없이 명령 stop으로 끝난 실험 Run의 결과. 실패·취소와 구분하며 성공/실패 판단을 하지 않고 stop 요청자를 기록한다. |
