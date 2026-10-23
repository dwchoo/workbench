# Claude Code 작업 규칙

공통 제품·작업 규칙은 `AGENTS.md`에 있다. 메인 세션에는 SessionStart hook이 Codex workflow·graphify 섹션을 제외하고 주입한다. 이 문서는 Claude Code 전용 규칙이며 subagent도 읽는다.

## 역할: Opus orchestrator, Sonnet 실행

- 메인 세션(Opus)은 orchestrator다. 요구 정리, 작업 분해, assignment packet 작성, subagent 배정, 결과 판정·통합, 사용자 보고를 맡는다. 제품 코드 구현과 코드·원인 조사는 직접 하지 않는다.
- 구현·테스트 작성·코드 조사·리뷰·재검증은 subagent에 위임한다. subagent는 항상 최신 Sonnet을 쓴다. 버전을 고정하지 않고 `sonnet` alias를 쓴다.
  - Agent 호출에는 `model: "sonnet"`을 명시한다. `fork`는 부모 모델을 상속하므로 쓰지 않는다. Workflow의 agent도 같다.
  - 기본 subagent 모델은 `.claude/settings.json`의 `CLAUDE_CODE_SUBAGENT_MODEL=sonnet`이다. 프로젝트 agent(`wb-worker`, `wb-explorer`, `wb-test-rerun`, `wb-delta-reviewer`)도 `model: sonnet`이다.
- orchestrator가 직접 해도 되는 일: 위임 판단에 필요한 최소 확인(`git status`, `du`, 파일 목록, graphify query), subagent 결과를 검증하는 짧은 테스트 재실행, 작업 규칙·설정 파일(`CLAUDE.md`, `.claude/**`) 편집.
- subagent는 leaf다. 다른 agent를 띄우지 않는다. 막히면 근거와 함께 orchestrator에 반환한다.

## TDD

- 동작 변경과 버그 수정은 TDD로 개발한다. 방법은 `.agents/skills/tdd/SKILL.md`와 `references/`를 따른다.
- 한 behavior씩 RED → 최소 GREEN → 정리 순서로 진행한다. 다음 behavior는 앞 behavior가 GREEN이 된 뒤 시작한다.
- RED는 의도한 behavior가 없거나 틀려서 실패해야 한다. import 오류, 환경 실패, 일부러 깨뜨린 코드는 RED가 아니다.
- 테스트는 합의된 public seam에서만 작성한다. seam이 합의되지 않았으면 orchestrator가 seam 후보와 각각 잡는 것·놓치는 것을 제안하고, 사용자 확인을 받은 뒤 진행한다.
- worker packet에는 seam, behavior 순서, 실행할 테스트 명령을 적는다. worker 보고에는 behavior별 RED 실패 메시지와 GREEN 통과 결과를 담는다.
- 테스트를 약화·skip·삭제해 통과시키지 않는다. 문서·설정만 바꾸는 변경은 TDD 대상이 아니다.

## 테스트 실행

- 인터프리터는 repo 루트의 `.venv`(Python 3.12, `pyte`·`wcwidth`만 설치)다. worktree에서는 main checkout의 `.venv`를 절대 경로로 쓴다.
- 공통 접두사: `env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python`
  - 단일 모듈: `... -m unittest tests.<dir>.test_<name>` 또는 `... -m unittest discover -s tests/<dir> -p 'test_<name>.py'`
  - 디렉터리: `... -m unittest discover -s tests/<dir>`. 전체는 디렉터리별로 따로 실행한다. `-t .`는 쓰지 않는다.
  - `tests/gates`: `PYTHONPATH=src:.`. live probe라 실제 OMP·terminal을 쓴다. 명시적으로 필요할 때만 실행한다.
- `tests/terminal` 등 일부 suite는 실제 OMP를 띄우는 live harness라 수 분 걸린다. TDD loop에서는 변경과 관련된 모듈만 실행하고, 넓은 회귀 실행은 통합 단계에서 한 번 한다.
- 테스트가 띄운 process와 temp dir은 띄운 쪽이 정리한다.
- 이 호스트는 디스크 fsync가 20~70ms라 `/dev/shm`(tmpfs)에서 훨씬 빠르다. 위 명령은 `python3 tools/shm_tests.py -- <위 명령>`으로 감싸 실행하는 것을 기본으로 한다.
  - `/dev/shm` 여유가 8GiB 이상이면 `TMPDIR=/dev/shm/wb-tests.<rand>`를 주고, 모자라면 `/tmp`로 fallback한다. 끝나면 root를 지우고 peak 크기를 출력한다.
  - 테스트는 `tempfile` 기본값(`TMPDIR`)을 따른다. 의도적으로 `/tmp`에 남긴 것: `tests/integration`의 `test_cw16_runtime_matrix.py`(`/tmp/<한 글자>` 경로와 RLIMIT_FSIZE), `cw16_harness.py` 기본 `/tmp/wb-cw16-reports`, `live_workbench_doctor.py`·`live_workbench_update.py`·`live_workbench_update_probe.py`(제품 fence `/tmp/omp-workbench-*`), `vm_reboot_driver.py`, `tests/backend/installation`의 `live_setup.py`·`live_project_sessions.py`. 제품이 고정 `/tmp` 경로를 쓰는 expectation 문자열도 그대로 둔다.
  - 강제 종료 등으로 남은 root는 `python3 tools/shm_tests.py --clean-stale`로 확인(dry-run)하고 `--apply`를 붙여 삭제한다.

## graphify

- 코드베이스 질문은 `graph.json`이 있으면 먼저 `graphify query "<question>"`로 방향을 잡는다. 관계는 `graphify path "<A>" "<B>"`, 개념은 `graphify explain "<concept>"`를 쓴다.
- 넓은 탐색은 `graphify-out/wiki/index.md`가 있으면 그것을 쓴다. `graphify-out/GRAPH_REPORT.md`는 전체 아키텍처 검토 때만 읽는다.
- 코드를 수정한 뒤에는 repo 루트에서 `graphify update .`를 실행한다(AST만, API 비용 없음). worktree·격리 실행 home에서는 실행하지 않는다.
- 사용자가 `/graphify`를 입력하면 graphify skill을 먼저 사용한다.

## `.workflow` 용량 관리

과거에 `.workflow`가 60GB를 넘은 적이 있다. 기준은 `AGENTS.md` "개발 증거 용량 관리"이고, 아래는 Claude 작업에서 지킬 절차다.

- 상한은 `.workflow` 10GiB, 경고선은 8GiB다. SessionStart hook이 `.workflow`·`graphify-out`·`.worktrees` 중 8GiB 이상인 것을 경고한다. 경고가 뜨면 새 격리 실행이나 worktree를 시작하기 전에 정리부터 사용자에게 제안한다.
- 새 격리 실행 전후에 `du -sh .workflow graphify-out .worktrees`를 확인하고 증감을 보고한다.
- 용량 대부분은 다음 두 가지다.
  - 격리 실행마다 만드는 checkpoint(`*.json.gz`, 개당 약 70MB).
  - 격리 home의 `.omp/natives/*.node`(개당 약 180MB).
- graphify가 `.workflow`에 남기는 것은 수 KB짜리 실행 메타데이터뿐이다. 하지만 checkpoint에 `graphify-out/`이 섞이면 실행마다 수십~수백 MB가 늘어난다.
- checkpoint·hash manifest 범위에서는 `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시를 제외한다. worker packet에 이 exclude를 명시한다.
- 같은 후보의 checkpoint를 단계마다 반복해서 만들지 않는다. 복원이 필요한 변경 직전에만 만든다.
- 실행이 끝나고 소유 process 종료를 확인하면 `python3 .agents/skills/workspace-evidence/scripts/storage_maintenance.py`를 실행한다. 처음에는 dry-run으로 후보를 확인하고, 그다음 `--apply`를 붙인다. 오래된 대형 checkpoint는 `--archive-checkpoints`로 압축한다.
- 격리 실행을 마친 worktree를 제거할 때는 내부 `.workflow/`도 같이 정리한다. `graphify-out/`의 일자별 snapshot 디렉터리는 최근 3개만 남긴다.
- 보고서, trace, 실패 근거, DB 결과는 보존한다. 삭제·압축은 dry-run 결과를 보고하고 사용자 확인을 받은 뒤 수행한다. 실행 중인 process가 쓰는 자료는 건드리지 않는다.
