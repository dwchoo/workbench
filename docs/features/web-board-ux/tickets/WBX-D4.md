# WBX-D4: Board shell 기본 program을 bash로(없으면 sh)

명세: [../SPEC.md](../SPEC.md) s5 "Board shell 결정". 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-D4`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Sonnet(`model: "sonnet"`). `prepare_shell` 한 곳의 결정 순서를 바꾸는 작은 변경이다.

## 제공할 동작

1. `BoardLauncher.prepare_shell`: `[board_agent] shell`(설정되어 있으면 그 절대 경로만, 실행할 수 없으면 `shell_unavailable`) → bash(`/bin/bash`, `/usr/bin/bash`, `hub_environment()`의 `PATH`에서 찾은 `bash`의 절대 경로 순서, 실행 가능한 일반 파일) → `/bin/sh`(실행할 수 없으면 `shell_unavailable`). `$SHELL`은 읽지 않는다.
2. Shell process env의 `SHELL`을 고른 program 경로로 둔다(SPEC 기술 보완). argv·cwd·`TERM`·UTF-8 locale은 그대로.
3. 고정 후보(`/bin/bash`, `/usr/bin/bash`, `/bin/sh`)를 `BoardLauncher` 인자(기본값 그 값)로 받는다(시험에서 "bash 없음"을 만들기 위해).
4. 문서: `hub/config.py` 템플릿 주석("기본값 ""(bash, 없으면 /bin/sh)"), task-inbox USAGE §1.2 표의 `shell` 줄, task-inbox SPEC:68·:389에 `[2026-10-29 개정: … web-board-ux WBX-D68·D69]` 주석.

## 수락 기준

WBX-AC-48.

## 계약과 맥락

현행: `hub/board/launch.py` `prepare_shell`(`self.config.shell or self.environment.get("SHELL") or DEFAULT_SHELL`), `DEFAULT_SHELL`, `hub/config.py`(`[board_agent] shell` 검사·템플릿). BRIEF WBX-D68(사용자 결정)·D69(orchestrator 결정). 새로 시작하는 shell(만들기·재시작)에만 적용하고 돌던 shell은 그대로다. 선행 없음.

## 검증

Seam(새 seam 없음): 기존 Board seam(`tests/hub/test_board_launch.py`: temp 후보 경로와 주입한 environment로 `prepare_shell` 결과 argv·env), S3(`wb _hub --foreground` + `$SHELL=/bin/zsh`(또는 없음)인 Hub 환경에서 만든 shell의 `/proc/<pid>/exe`), 템플릿 정적 검사(`tests/hub/test_hub_config_template.py`).
독립 기대값: SPEC s5 순서, `/proc/<pid>/exe`·`/proc/<pid>/environ`의 `SHELL`.
Behavior 순서(한 behavior씩 RED → GREEN):
(1) 설정 없음 + `$SHELL=/bin/zsh` → bash (2) 설정 없음 + `$SHELL` 없음 → bash (3) 첫 후보 없음 → 둘째, 고정 후보 없음 → `PATH`의 bash (4) bash 전혀 없음 → `/bin/sh` (5) 설정 있음 → 그 경로, 실행 불가면 `shell_unavailable`(넘어가지 않음) (6) env `SHELL` = 고른 경로 (7) 템플릿 주석.
명령(공통 접두 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest`): `discover -s tests/hub -p 'test_board_launch*.py'`, `discover -s tests/hub -p 'test_hub_config_template.py'`; S3 회귀 `discover -s tests/hub -p 'test_board_*.py'`.
격리 실행 주의: S3는 실제 Hub를 띄운다. CLAUDE.md sandbox 규칙(짧은 private `XDG_RUNTIME_DIR`, temp HOME/XDG_STATE_HOME, `WORKBENCH_HUB_AUTOSTART=0`, 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l` 동일). Checkpoint·hash manifest exclude: `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시.

## 범위 밖과 중단 조건

Login shell·rc 파일 처리, 실행 중 shell 바꾸기, 다른 terminal 종류. `[board_agent] shell` 검사 규칙(`hub/config.py`)을 바꿔야 하면 멈추고 보고한다.

## 출력

변경 경로, behavior별 RED 실패 메시지와 GREEN 결과, 경우별 고른 program 표, 명령·결과.
