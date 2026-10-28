# WBX-B4: Board overlay 아래 `wb-inbox` skill probe(WBX-O02)

명세: [../SPEC.md](../SPEC.md) s3. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-B4`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Opus(`model: "opus"`). 실제 OMP 프로세스·격리 설정·실험 판정이다.

## 재실행(2026-10-28, WBX-D33)

1차 실행(2026-10-27)은 inconclusive(로그인 사용 불가)였고 그 중에 사용자 OMP 로그인이 사라졌다([../INCIDENT-20261027-omp-login.md](../INCIDENT-20261027-omp-login.md)). [사용자 결정 2026-10-28] WBX-B12 통합 뒤 사용자의 실제 로그인으로 다시 실행한다.
- 선행: WBX-B12(격리 확인 OMP graceful 종료, Board OMP log 보존, helper의 `agent.db` 복사 없음). 실행 전 사용자가 `omp`에서 로그인 상태를 확인했다는 orchestrator 기록이 있어야 한다.
- 이번 실행의 Board·격리 확인 OMP log(stderr 전체, OMP 자체 log, 종료 단계)를 probe root 밖 `/tmp/wbx-b4-out-<YYYYMMDD>/logs/`에 남기고 지우지 않는다(삭제는 사용자 확인 뒤). 2026-10-27 증거 `/tmp/wbx-b4-out/`도 그대로 둔다.
- 실행 전후 사용자 `~/.omp/agent/agent.db`의 mtime·크기만 기록한다(내용은 열지 않는다). 2단계 뒤 `get_login_providers`의 authenticated가 실행 전과 같은지 기록한다. 줄었으면 판정과 무관하게 멈추고 보고한다.
- Passed면 WBX-B5 → B8 → B9를 계속한다. Failed·inconclusive면 기존 규칙대로 멈추고 사용자에게 묻는다.
- `PROBE-BOARD-SKILL.md`는 재실행 결과로 다시 생성하고, 1차 실행의 사람이 쓴 해석과 2026-10-28 정정은 "1차 실행(2026-10-27)" 절로 보존한다.

## 제공할 동작

결정적 가정: "Board overlay 아래 OMP가 지정한 skill 디렉터리의 `wb-inbox` 하나만 보고, 모델이 bash로 `wb_inbox.py`를 실행해 내부 loopback REST에서 요청 목록을 받는다."
가장 작은 실험(관측을 나눈다):
1. 설정 관측(모델 호출 없음): probe script가 Board home·overlay 후보(skill customDirectories = Board skill 복사 디렉터리, MCP 없음)로 `launcher.check_isolation(..., allowed_skills=("wb-inbox",))`를 실행해 skills = {`wb-inbox`}, MCP tool 0개, 그 밖 leak 0개를 확인. 어떤 OMP 설정 key가 필요했는지 기록.
2. 한 turn 실험(실제 모델): 같은 구성의 OMP에 "use the wb-inbox skill to list requests" 한 turn. 테스트 Hub(S2 방식, loopback)의 REST 접근(API key `last_used_at`·요청 기록)과 bash tool 호출을 관측. 출력 본문은 보존하지 않고 판정에 필요한 사실만 기록.
결과는 `docs/features/web-board-ux/PROBE-BOARD-SKILL.md`: 환경(OMP 버전, 모델 selector), 설정, 관측, 판정 passed / failed / inconclusive, WBX-B5가 쓸 구성.

## 수락 기준

- WBX-AC-17: probe 기록이 있고 passed면 WBX-B5 진행. Failed·inconclusive면 WBX-B5·B8·B9를 멈추고 사용자에게 묻는다(대안 "Board 전용 MCP 유지"는 결정되지 않음, BRIEF WBX-D11).

## 계약과 맥락

선행: WBX-B1(REST), WBX-B2(`wb_inbox.py`), 재실행은 WBX-B12. 참고: task-inbox `PROBE-OMP-MCP.md`와 `tests/hub/probes/omp_mcp_probe.py`의 방식, `board/launch.py`, `backend/launcher.py:check_isolation`, `omp_home.prepare_omp_home`. 2단계는 이 host의 기존 OMP 로그인으로 실제 모델을 한 번 호출한다(작은 비용 수용, WBX-D27).

## 검증

Probe는 TDD 대상이 아니다(실험 기록). Script는 `tests/hub/probes/board_skill_probe.py`. 실행 시간 제한, 자식 process group 정리, PTY·pipe drain, 사용자 실제 OMP 설정을 바꾸지 않음(격리 home). Timeout은 원인 판정 전까지 inconclusive.
격리 실행 주의: 실제 Hub·OMP·isolation home을 띄운다. CLAUDE.md "테스트 실행"의 sandbox 규칙(짧은 private `XDG_RUNTIME_DIR`, temp HOME/XDG_STATE_HOME, `WORKBENCH_HUB_AUTOSTART=0`, 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l` 동일)을 지킨다. Checkpoint·hash manifest exclude: `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시(`*/.omp/natives/`, Board home). 전후 `du -sh .workflow graphify-out .worktrees`를 보고한다.

## 범위 밖과 중단 조건

Product 코드 변경 금지(probe script와 기록만). 로그인이 동작하지 않으면 1단계만 기록하고 inconclusive로 사용자에게 묻는다. 재실행 중 authenticated provider가 줄면 즉시 멈추고 OMP log 위치와 함께 보고한다(다시 실행하지 않는다).

## 출력

`PROBE-BOARD-SKILL.md`, probe script, 판정과 WBX-B5 구성, 보존한 OMP log 위치, 실행 전후 auth store mtime·크기와 authenticated provider 비교.
