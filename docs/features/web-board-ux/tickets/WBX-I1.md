# WBX-I1: 최종 통합·검증

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-I1`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Orchestrator(통합) + Sonnet test_designer 재검증(`model: "sonnet"`). 교차 ticket 통합 확인이다.

## 제공할 동작

모든 ticket 통합 뒤: (1) 디렉터리별 회귀 `tests/hub`, `tests/backend/installation`, `tests/backend -p 'test_omp_isolation*.py'`, `tests/backend -p 'test_workbench_skills.py'`; (2) live: `tests/integration/live_inbox_final_independent.py`(REST판) 1회; (3) 실제 Hub(`WebSandbox`)에서 `wb_inbox.py`로 submit → list → get → comment → cancel → result 한 바퀴, `/skill/README.md` 내려받기, `/mcp` 404, Board 세트 시작·`/proc` env 확인(fake OMP); (4) wheel build에서 `_assets/skills` 확인; (5) route audit 최종; (6) 수락 기준 표(SPEC)의 각 행에 증거 연결, 사용자 browser·대상 host 항목은 `pending`으로 표시; (7) repo 루트에서 `graphify update .`.

## 수락 기준

WBX-AC-01~20 전부의 증거 연결. 자동 증거가 불가능한 항목: AC-04(A5 기록), AC-01·03·05·19·20의 시각·JS 동작(사용자 browser), AC-16의 실제 OMP·모델(대상 host), Codex·Claude Code의 skill 사용(대상 host).

## 계약과 맥락

선행: WBX-A4, WBX-A5, WBX-B9, WBX-C2(그리고 그 선행 전부).

## 검증

명령: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub`, `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/backend/installation`, `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/backend -p 'test_omp_isolation*.py'`; live는 `env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv/bin/python tests/integration/live_inbox_final_independent.py`(통합 때 1회).
격리 실행 주의: 실제 Hub·OMP·isolation home을 띄운다. CLAUDE.md "테스트 실행"의 sandbox 규칙(짧은 private `XDG_RUNTIME_DIR`, temp HOME/XDG_STATE_HOME, `WORKBENCH_HUB_AUTOSTART=0`, 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l` 동일)을 지킨다. Checkpoint·hash manifest exclude: `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시(`*/.omp/natives/`, Board home). 전후 `du -sh .workflow graphify-out .worktrees`를 보고한다.

## 범위 밖과 중단 조건

새 기능. Commit·push(별도 요청). 실패는 원 ticket으로 돌려보낸다.

## 출력

수락 기준별 증거 표, 명령·결과, pending 목록, `.workflow`·`graphify-out`·`.worktrees` 용량 증감.
