# WB 안전 업데이트와 작업 재개 — 구현 명세

Revision: s1; source: BRIEF.md b1. 상태: 확인용 초안. 승인 정본은 별도 approval.json과 실제 사용자 답변이다.

## 문제와 해결 방향

사용자는 `wb update`로 WB를 업데이트하려 한다. 진행 중인 작업을 감지하면 먼저 사용자에게 묻고, 승인한 경우에만 종료·기록·백업한 뒤 업데이트한다. 거절하면 현재 작업을 유지한다. 작업이 없으면 별도 질문 없이 업데이트한다.

사용자의 두 답변: “사용자에게 물어보고 업데이트를 승인하면 작업을 종료하고 기록을 남겨서 재시작 할 수 있게 하고 업데이트.”, “WB 상태·세션·재개 정보 백업”. 프로젝트 소스·실험 산출물은 자동 복사하지 않으며 원래 위치를 유지한다. 사용자는 이 기능 개발·검증 후 커밋·푸시하고, 그 다음 `wb doctor` 개발을 진행하도록 요청했다.

기본 `main`/선택 `--ref`와 uv 관리 Git 설치의 재설치는 앞서 제시한 방향을 구체화한 제안이다. 아래 실행 명세·테스트 seam·범위는 이번 계획 묶음의 확인 대상이며, 사용자 요청 자체를 아직 존재하지 않았던 exact bundle의 승인으로 소급하지 않는다.

## 범위와 비목표

Linux·Python 중심, 기존 uv tool/Git 설치와 기존 backend identity·shutdown·일시정지·상태 조회를 재사용한다. 기존 현재 사용자 runtime binding, 명시/현재 data-dir와 실제 backend process identity를 대조한다. 검출 후보를 확인하지 못하면 작업 없음으로 간주하지 않는다. 다른 사용자의 프로세스를 조사하거나 종료하지 않는다.

업데이트는 WB 패키지에 한정하며 OMP·연구 환경 패키지·프로젝트 파일·raw rotation을 바꾸지 않는다. live process 메모리 복원, 실험 자동 재실행, 자동 rollback/restore 명령, 원격 백업·자동 백업 삭제, 모든 설치 도구 지원, 자동 관리자 권한·도구 설치, 새 상주 서비스는 범위 밖이다.

## 공개 계약과 불변 조건

### CLI와 설치 출처
`wb update [--ref REF]` 및 기존 alias의 같은 parser. 실행 cwd에 프로젝트가 없어도 설치 상태와 다른 실행 중 WB를 조사할 수 있다. REF는 branch/tag/commit이며 shell/URL/option 인자로 해석하지 않는다. uv 관리 Git 출처를 현재 실행 interpreter·distribution metadata·tool receipt와 대조하고, source URL의 인증 정보는 출력하지 않는다. Git ref는 설치 전에 한 commit으로 고정하고 이전/목표 identity를 기록한다. 지원하지 않는 설치는 자동 전환하지 않는다. 초기 버전에는 active work 확인을 우회하는 `--yes`/`--force`를 제공하지 않는다.

### 확인과 실행 상태
설치 관리와 backend start가 공유하는 사용자 범위 update fence를 둔다. 설치 확인 전 준비는 읽기/출처 확인에 한정한다. 사용자 승인 전에는 pause·종료·작업 기록 생성·백업을 하지 않는다. 표시한 작업/프로세스 identity가 바뀌거나 확인 token이 만료되면 새 확인 없이 다른 작업을 종료하지 않는다. 승인 후 새 자동 작업을 억제하고 재개 정보를 포착해 기존 shutdown을 실행한다. shutdown 결과와 exact PID/start ticks·writer 부재를 확인한 뒤에만 백업·설치한다. idle backend도 설치 전에 종료·상태 보존하되 active work가 없으면 별도 질문을 요구하지 않는다.

### 백업과 재개
업데이트용 manifest와 재개 metadata는 일반 작업 메모가 아닌 유지보수 상태다. 기존 records-v1의 manager/worker actor를 위조하거나 일반 메모를 프로젝트 docs에 쓰지 않는다. 해당 작업/Task/run/revision·worktree/프로젝트 경로·양 역할 session ID·종료 결과·결과 불명 작업·기록/로그 위치·재개 명령을 구조화해 보존한다. 원문 대화/기록은 백업 원본에 유지하며 새 모델 호출로 요약하지 않는다.

백업 기본 위치는 XDG_STATE_HOME(기본 ~/.local/state)/omp-workbench/updates의 고유 run directory다. directory0700·file0600, manifest에 source/project identity·이전/목표 commit·각 파일/DB hash·포함/제외·종료 확인·완료 여부를 보관한다. source 내부와 겹치는 백업 root는 거절한다. 종료·lock을 확인한 WB root의 SQLite family와 세션/작업 이력을 일관되게 포착하고 원본 inventory를 대조한다. source를 열어 schema를 고치거나 SQLite sidecar를 만들지 않는다. symlink는 따라가 외부 파일을 수집하지 않으며 특히 agent.db와 legacy credential 사본은 백업 대상에서 제외한다. 필수 WB 데이터 미확인·부분 복사는 실패다.

재시작 시 유지보수 재개 정보를 표시하고 기존 작업/세션·파일을 검토해 명시적으로 재개하도록 안내한다. 프로세스 메모리·중단된 terminal 실행의 연속성을 보장하지 않는다. 이미 적용된 취소·pause·승인을 덮어쓰지 않고 old command를 자동 재전송하지 않는다. 일반 재개에 원본 backup 복원은 필요하지 않으며 원본 `.wb/`는 제자리에 남는다. 실패 복구를 위한 별도 자동 restore/rollback surface는 만들지 않는다.

### 실패와 자기 교체
업데이트 runner는 자기 venv 교체 중에도 종료 결과·백업 안내·update fence를 보존해야 한다. 구현 방식은 WU-01의 실제 격리 uv 교체 실험으로 고른다. uv 실패를 원래 설치 보존 보장으로 둔갑시키지 않는다. 실패 시 이전 ref 재설치 명령·백업 위치와 확인되지 않은 설치 상태를 표시한다. 설치 성공 후 새 interpreter/entrypoint·배포 source commit·필수 assets를 확인한다. 외부 auth/provider·사용자 DB/실행 세션에서 개발 검증하지 않는다.

## 현재 구현의 접점

`service.py::_active_work`는 shell request/foreground·OMP idle·automation·task run·survivor/left_session을 구분한다. `shutdown_request/confirm`과 `cli.py::cmd_shutdown`에는 confirmation token·결과 확인이 있다. 별도 session 생존자는 기존 shutdown이 자동 종료하지 않는다. `project.py::resolve_project_context`와 `process_identity`가 side-effect-free identity 판별을 제공한다. `runtime_paths.py`에는 runtime.json binding이 있으나 전체 프로젝트 registry는 없다. 현재 OMP sessions/history는 omp-root/agent에 지속되고 agent.db는 공유 auth store로 향할 수 있다. 현재 CLI start는 update fence를 모른다. 기존 entrypoint는 설치 venv Python으로 `-I -m workbench`를 실행한다. 실제 uv 자기 교체·update 중 start 차단은 아직 미검증이다.

## 수락 기준

| ID | 동작 |
|---|---|
| WU-AC-01 | `wb update [--ref REF]`는 지원되는 현재 사용자 uv 관리 Git 설치의 출처를 확인하고, 기본 main 또는 지정 ref를 단일 commit으로 고정해 WB만 업데이트한다. 개발 checkout·미확인 설치·누락된 uv는 설명하고 거절한다. |
| WU-AC-02 | 현재 사용자 WB의 실행 중 backend/작업을 확인한다. 진행 중 작업이 있으면 종료 영향과 대상 프로젝트를 먼저 표시하고 묻는다. 거절·Ctrl-C·EOF·비대화형 입력은 작업을 중단하거나 상태를 수정하지 않는다. |
| WU-AC-03 | 승인한 작업만 기존 확인·종료 경로로 종료한다. 종료 불명·별도 session 생존자·writer·identity 변화가 남으면 업데이트하지 않는다. 작업이 없는 정상 상태에서는 질문 없이 진행한다. |
| WU-AC-04 | 중단 후 WB 상태 DB(모든 기록 revision 포함)·작업/실행 정보·OMP 세션/대화 이력·재개 안내를 검증 가능한 private 백업으로 보존한다. 완료되지 않은 백업으로 설치를 진행하지 않는다. |
| WU-AC-05 | 백업은 프로젝트 소스·실험 산출물·공유 인증 DB·credential symlink 대상·재생성 cache를 복사하지 않는다. 백업 경로와 제외 항목을 명시하고 원본을 변경하거나 기존 백업을 덮어쓰지 않는다. |
| WU-AC-06 | 업데이트 후 기존 프로젝트에서 `wb start`를 하면 이전 작업·세션·마지막 확인 상태·백업 위치와 재개 방법을 알 수 있다. 중단된 명령·실험을 자동 replay하거나 취소/일시정지/실행 승인 상태를 해제하지 않는다. |
| WU-AC-07 | 동시 update와 update 도중 새 backend 시작은 직렬화한다. ref/설치 준비 실패는 종료 전에, 종료·백업 실패는 설치 전에 거절한다. 설치 실패는 성공으로 표시하지 않고 백업·이전/목표 commit·수동 재설치 방법을 남긴다. |
| WU-AC-08 | 두 프로젝트의 Task·작업 기록 원문/이전 revision·OMP 세션·프로젝트 소스/산출물·기존 실행 승인과 원본을 보존한다. 실제 비editable 설치본의 자기 교체와 재시작 흐름을 관측하고 필수 독립 검증 후 커밋·푸시한다. |

## 검증 결정

공개 seam은 subprocess `wb update`/`wb start`/기존 shutdown와 private backup 결과, 실제 uv 비editable 설치다. 기대값은 직접 정한 두 프로젝트 sentinel·모든 revision·Task/run/session ID·source/export/산출물 hash·확인한 PID/start ticks·이전/목표 commit이다. 내부 함수 호출 횟수나 구현 formatter로 주 검증을 만들지 않는다.

WU-01은 소유한 짧은 /tmp HOME·UV_TOOL_DIR/BIN/CACHE와 로컬 Git refs/기존 cached dependencies로 자기 교체·실패 결과·fence 수명만 최소 실험한다. fake uv 성공은 실제 교체 증거가 아니다. 통과 후 WU-02를 진행하며 실패/미확정이면 해당 기술 원인만 다룬다.

각 동작은 실제 RED→최소 GREEN 뒤 다음 동작으로 진행한다. 독립 test_designer는 요구에서 먼저 사례를 도출하고 실제 빈틈만 보완한다. WU-01의 probe는 chronology 예외를 명시한다. WU-02+03은 한 최종 단위로 묶어 새 Standards/Spec 두 context, 별도 Security와 deterministic secret scan을 수행한다. WU-01에는 중간 단위 독립 평가·fresh general review가 있다. 동일 source/environment 통과 증거를 재사용하고 수정 후 영향 경계만 재검사한다. 전체 관련 검사는 마지막 결합 후보에서 한 번 수집한다.

예정 명령: `PYTHONPATH=src python3 -m unittest discover -s tests/backend/maintenance -p 'test_update*.py'`; `PYTHONPATH=src:tests/backend/installation python3 -m unittest tests.backend.test_paths_launcher tests.backend.installation.test_project_cli`; `WB_LIVE_UPDATE=1 PYTHONPATH=src python3 -m unittest tests.integration.live_workbench_update`. installed test는 checkout 밖 실행·실제 uv/Git·localhost scripted OMP·private data만 사용한다. 누락된 필수 runtime을 fixture로 대체하지 않는다.

## 통합·게시 순서와 미결정

PLAN.json p1이 ticket dependency/resource/gate supplier의 정본이다. update 전체 완료·게시 이전에는 doctor 제품 코드를 작성하지 않는다. 사용자에게 실제 업데이트·실험 종료를 수행하는 것은 이번 개발용 private 검증과 구분한다. 이번 권한으로 사용자 DB/auth/global tool 환경을 실제 업데이트하지 않는다. source code commit/push는 사용자 요청에 포함된다.

구체 helper 배치·lock 구현·runner 방식은 합의한 공개 동작 안에서 판별한다. source/프로세스/설치 identity·일관된 backup을 확인하지 못하면 보류하며 성공으로 가정하지 않는다. 승인 전 product/test 작성 금지, 필수 runtime 부재는 not_run이다.
