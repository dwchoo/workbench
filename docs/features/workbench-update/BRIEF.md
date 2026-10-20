# WB 안전 업데이트와 작업 재개

Revision: b1. 상태: 확인용 초안. 사용자 의도와 아래 구체 계획 묶음의 승인을 구분한다.

## 사용자 요청과 목표

사용자는 `wb update`로 WB를 업데이트하려 한다. 진행 중인 작업을 감지하면 먼저 사용자에게 묻고, 승인한 경우에만 종료·기록·백업한 뒤 업데이트한다. 거절하면 현재 작업을 유지한다. 작업이 없으면 별도 질문 없이 업데이트한다.

사용자의 두 답변: “사용자에게 물어보고 업데이트를 승인하면 작업을 종료하고 기록을 남겨서 재시작 할 수 있게 하고 업데이트.”, “WB 상태·세션·재개 정보 백업”. 프로젝트 소스·실험 산출물은 자동 복사하지 않으며 원래 위치를 유지한다. 사용자는 이 기능 개발·검증 후 커밋·푸시하고, 그 다음 `wb doctor` 개발을 진행하도록 요청했다.

기본 `main`/선택 `--ref`와 uv 관리 Git 설치의 재설치는 앞서 제시한 방향을 구체화한 제안이다. 아래 실행 명세·테스트 seam·범위는 이번 계획 묶음의 확인 대상이며, 사용자 요청 자체를 아직 존재하지 않았던 exact bundle의 승인으로 소급하지 않는다.

## 범위와 비목표

Linux·Python 중심, 기존 uv tool/Git 설치와 기존 backend identity·shutdown·일시정지·상태 조회를 재사용한다. 기존 현재 사용자 runtime binding, 명시/현재 data-dir와 실제 backend process identity를 대조한다. 검출 후보를 확인하지 못하면 작업 없음으로 간주하지 않는다. 다른 사용자의 프로세스를 조사하거나 종료하지 않는다.

업데이트는 WB 패키지에 한정하며 OMP·연구 환경 패키지·프로젝트 파일·raw rotation을 바꾸지 않는다. live process 메모리 복원, 실험 자동 재실행, 자동 rollback/restore 명령, 원격 백업·자동 백업 삭제, 모든 설치 도구 지원, 자동 관리자 권한·도구 설치, 새 상주 서비스는 범위 밖이다.

## 수락 기준

| ID | 관측할 동작 |
|---|---|
| WU-AC-01 | `wb update [--ref REF]`는 지원되는 현재 사용자 uv 관리 Git 설치의 출처를 확인하고, 기본 main 또는 지정 ref를 단일 commit으로 고정해 WB만 업데이트한다. 개발 checkout·미확인 설치·누락된 uv는 설명하고 거절한다. |
| WU-AC-02 | 현재 사용자 WB의 실행 중 backend/작업을 확인한다. 진행 중 작업이 있으면 종료 영향과 대상 프로젝트를 먼저 표시하고 묻는다. 거절·Ctrl-C·EOF·비대화형 입력은 작업을 중단하거나 상태를 수정하지 않는다. |
| WU-AC-03 | 승인한 작업만 기존 확인·종료 경로로 종료한다. 종료 불명·별도 session 생존자·writer·identity 변화가 남으면 업데이트하지 않는다. 작업이 없는 정상 상태에서는 질문 없이 진행한다. |
| WU-AC-04 | 중단 후 WB 상태 DB(모든 기록 revision 포함)·작업/실행 정보·OMP 세션/대화 이력·재개 안내를 검증 가능한 private 백업으로 보존한다. 완료되지 않은 백업으로 설치를 진행하지 않는다. |
| WU-AC-05 | 백업은 프로젝트 소스·실험 산출물·공유 인증 DB·credential symlink 대상·재생성 cache를 복사하지 않는다. 백업 경로와 제외 항목을 명시하고 원본을 변경하거나 기존 백업을 덮어쓰지 않는다. |
| WU-AC-06 | 업데이트 후 기존 프로젝트에서 `wb start`를 하면 이전 작업·세션·마지막 확인 상태·백업 위치와 재개 방법을 알 수 있다. 중단된 명령·실험을 자동 replay하거나 취소/일시정지/실행 승인 상태를 해제하지 않는다. |
| WU-AC-07 | 동시 update와 update 도중 새 backend 시작은 직렬화한다. ref/설치 준비 실패는 종료 전에, 종료·백업 실패는 설치 전에 거절한다. 설치 실패는 성공으로 표시하지 않고 백업·이전/목표 commit·수동 재설치 방법을 남긴다. |
| WU-AC-08 | 두 프로젝트의 Task·작업 기록 원문/이전 revision·OMP 세션·프로젝트 소스/산출물·기존 실행 승인과 원본을 보존한다. 실제 비editable 설치본의 자기 교체와 재시작 흐름을 관측하고 필수 독립 검증 후 커밋·푸시한다. |

## 관측 사실

조사 HEAD `a6d877fa3d38f04d816c2f105f131a9be93fdac1`, 2026-10-20. 기존 미커밋 skill/config/AGENTS/CONTEXT 변경은 보존한다. 실제 사용자 backend·DB·auth·설치는 조작하지 않았다.

`service.py::_active_work`는 shell request/foreground·OMP idle·automation·task run·survivor/left_session을 구분한다. `shutdown_request/confirm`과 `cli.py::cmd_shutdown`에는 confirmation token·결과 확인이 있다. 별도 session 생존자는 기존 shutdown이 자동 종료하지 않는다. `project.py::resolve_project_context`와 `process_identity`가 side-effect-free identity 판별을 제공한다. `runtime_paths.py`에는 runtime.json binding이 있으나 전체 프로젝트 registry는 없다. 현재 OMP sessions/history는 omp-root/agent에 지속되고 agent.db는 공유 auth store로 향할 수 있다. 현재 CLI start는 update fence를 모른다. 기존 entrypoint는 설치 venv Python으로 `-I -m workbench`를 실행한다. 실제 uv 자기 교체·update 중 start 차단은 아직 미검증이다.

## 권한과 미결정

사용자는 개발·검증·commit/push 및 update→doctor 순서를 요청했다. 제품 코드/테스트 작성은 시작하지 않았다. 이 BRIEF와 SPEC·PLAN·tickets·검증 seam의 exact bundle은 아직 제시 전이므로 승인으로 표시하지 않는다. SPEC의 구체화가 합의된 의도와 맞는지 한 번의 묶음 확인이 필요하다. 모델/설정 변경은 하지 않는다.
