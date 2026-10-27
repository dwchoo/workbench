# WBX-B4: Board overlay 아래 `wb-inbox` skill probe(WBX-O02)

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-B4`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Opus(`model: "opus"`). 실제 OMP 프로세스·격리 설정·실험 판정이다.

## 제공할 동작

결정적 가정: "Board overlay 아래 OMP가 지정한 skill 디렉터리의 `wb-inbox` 하나만 보고, 모델이 bash로 `wb_inbox.py`를 실행해 내부 loopback REST에서 요청 목록을 받는다."
가장 작은 실험(관측을 나눈다):
1. 설정 관측(모델 호출 없음): probe script가 Board home·overlay 후보(skill customDirectories = Board skill 복사 디렉터리, MCP 없음)로 `launcher.check_isolation(..., allowed_skills=("wb-inbox",))`를 실행해 skills = {`wb-inbox`}, MCP tool 0개, 그 밖 leak 0개를 확인. 어떤 OMP 설정 key가 필요했는지 기록.
2. 한 turn 실험(실제 모델): 같은 구성의 OMP에 "use the wb-inbox skill to list requests" 한 turn. 테스트 Hub(S2 방식, loopback)의 REST 접근(API key `last_used_at`·요청 기록)과 bash tool 호출을 관측. 출력 본문은 보존하지 않고 판정에 필요한 사실만 기록.
결과는 `docs/features/web-board-ux/PROBE-BOARD-SKILL.md`: 환경(OMP 버전, 모델 selector), 설정, 관측, 판정 passed / failed / inconclusive, WBX-B5가 쓸 구성.

## 수락 기준

- WBX-AC-17: probe 기록이 있고 passed면 WBX-B5 진행. Failed·inconclusive면 WBX-B5·B8·B9를 멈추고 사용자에게 묻는다(대안 "Board 전용 MCP 유지"는 결정되지 않음, BRIEF WBX-D11).

## 계약과 맥락

선행: WBX-B1(REST), WBX-B2(`wb_inbox.py`). 참고: task-inbox `PROBE-OMP-MCP.md`와 `tests/hub/probes/omp_mcp_probe.py`의 방식, `board/launch.py`, `backend/launcher.py:check_isolation`, `omp_home.prepare_omp_home`. 2단계는 이 host의 기존 OMP 로그인으로 실제 모델을 한 번 호출한다(작은 비용 수용, WBX-D27).

## 검증

Probe는 TDD 대상이 아니다(실험 기록). Script는 `tests/hub/probes/board_skill_probe.py`. 실행 시간 제한, 자식 process group 정리, PTY·pipe drain, 사용자 실제 OMP 설정을 바꾸지 않음(격리 home). Timeout은 원인 판정 전까지 inconclusive.
격리 실행 주의: 실제 Hub·OMP·isolation home을 띄운다. CLAUDE.md "테스트 실행"의 sandbox 규칙(짧은 private `XDG_RUNTIME_DIR`, temp HOME/XDG_STATE_HOME, `WORKBENCH_HUB_AUTOSTART=0`, 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l` 동일)을 지킨다. Checkpoint·hash manifest exclude: `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시(`*/.omp/natives/`, Board home). 전후 `du -sh .workflow graphify-out .worktrees`를 보고한다.

## 범위 밖과 중단 조건

Product 코드 변경 금지(probe script와 기록만). 로그인이 동작하지 않으면 1단계만 기록하고 inconclusive로 사용자에게 묻는다.

## 출력

`PROBE-BOARD-SKILL.md`, probe script, 판정과 WBX-B5 구성.
