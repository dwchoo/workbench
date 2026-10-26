# Claude Code 작업 규칙

공통 제품·작업 규칙은 `AGENTS.md`에 있다. 메인 세션에는 SessionStart hook이 Codex workflow·graphify 섹션을 제외하고 주입한다. 이 문서는 Claude Code 전용 규칙이며 subagent도 읽는다.

## 역할: Opus orchestrator, Opus 핵심 구현·주요 문서, Sonnet 실행

- 메인 세션(Opus)은 orchestrator다. 요구 정리, 작업 분해, assignment packet 작성, subagent 배정, 결과 판정·통합, 사용자 보고를 맡는다. 제품 코드 구현과 코드·원인 조사는 직접 하지 않는다.
- 구현·문서·테스트 작성·코드 조사·리뷰·재검증은 subagent에 위임한다. 모델은 일의 종류로 나눈다(2026-10-24 사용자 결정). 버전을 고정하지 않고 `opus`/`sonnet` alias를 쓴다.
  - Opus(`model: "opus"`): 주요 문서(BRIEF·SPEC·PLAN 등 결정·계약을 담는 문서)와 핵심 구현(동시성, signal·process 수명, 신원, 전달 순서·보안 경계, 여러 모듈에 걸친 설계 판단이 필요한 변경).
  - Sonnet(`model: "sonnet"`): 정형화된 구현(UI rendering, CLI, 기존 패턴을 따르는 변경), 독립 테스트 설계·재검증, 코드 조사, 리뷰, 진행 기록 같은 보조 문서.
  - Agent 호출에는 `model`을 항상 명시한다. `fork`는 부모 모델을 상속하므로 쓰지 않는다. Workflow의 agent도 같다.
  - 기본 subagent 모델은 `.claude/settings.json`의 `CLAUDE_CODE_SUBAGENT_MODEL=sonnet`이고, 프로젝트 agent(`wb-worker`, `wb-explorer`, `wb-test-rerun`, `wb-delta-reviewer`)의 frontmatter도 `model: sonnet`이다. Opus로 보낼 때는 호출에서 `model: "opus"`로 덮어쓴다.
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

- 인터프리터는 repo 루트의 `.venv`(Python 3.12, `pyte`·`wcwidth`·`aiohttp`(와 하위 의존성)만 설치. `aiohttp`는 2026-10-25 Phase 2 Hub·Web Board용으로 추가)다. worktree에서는 main checkout의 `.venv`를 절대 경로로 쓴다.
- 공통 접두사: `env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python`
  - 단일 모듈: `... -m unittest tests.<dir>.test_<name>` 또는 `... -m unittest discover -s tests/<dir> -p 'test_<name>.py'`
  - 디렉터리: `... -m unittest discover -s tests/<dir>`. 전체는 디렉터리별로 따로 실행한다. `-t .`는 쓰지 않는다.
  - `tests/gates`: `PYTHONPATH=src:.`. live probe라 실제 OMP·terminal을 쓴다. 명시적으로 필요할 때만 실행한다.
- `tests/terminal` 등 일부 suite는 실제 OMP를 띄우는 live harness라 수 분 걸린다. TDD loop에서는 변경과 관련된 모듈만 실행하고, 넓은 회귀 실행은 통합 단계에서 한 번 한다.
- 테스트가 띄운 process와 temp dir은 띄운 쪽이 정리한다.
- 이 호스트는 디스크 fsync가 20~70ms라 `/dev/shm`(tmpfs)에서 훨씬 빠르다. 위 명령은 `python3 tools/shm_tests.py -- <위 명령>`으로 감싸 실행하는 것을 기본으로 한다.
  - `/dev/shm` 여유가 8GiB 이상이면 `TMPDIR=/dev/shm/wb-tests.<rand>`를 주고, 모자라면 `/tmp`로 fallback한다. 끝나면 root를 지우고 peak 크기를 출력한다.
  - 테스트는 `tempfile` 기본값(`TMPDIR`)을 따른다. 의도적으로 `/tmp`에 남긴 것: `tests/integration`의 `test_cw16_runtime_matrix.py`(`/tmp/<한 글자>` 경로와 RLIMIT_FSIZE), `cw16_harness.py` 기본 `/tmp/wb-cw16-reports`, `live_workbench_doctor.py`·`live_workbench_update.py`·`live_workbench_update_probe.py`(제품 fence `/tmp/omp-workbench-*`), `vm_reboot_driver.py`, `tests/backend/installation`의 `live_setup.py`·`live_project_sessions.py`. 제품이 고정 `/tmp` 경로를 쓰는 expectation 문자열도 그대로 둔다.
  - Hub·backend runtime을 만들 수 있는 sandbox는 `/tmp` 아래 짧은 private `XDG_RUNTIME_DIR`(`tempfile.mkdtemp(prefix='r')`, 0700, cleanup에서 삭제)를 쓰고 temp `HOME`/`XDG_STATE_HOME`, `WORKBENCH_HUB_AUTOSTART=0`(Hub가 필요 없을 때)을 준다. shm root 경로가 길면 socket 107 byte 한도 때문에 runtime이 실제 `/tmp/omp-workbench-<uid>`로 fallback하기 때문이다(2026-10-25 Phase 2에서 확인). 실제 backend는 설계상 OMP temp dir을 `/tmp/omp-workbench-<uid>/<ns>`에 만든다. 테스트는 자기 project의 정확한 dir 이름을 계산해 사전에 없음을 확인하고, 소유 프로세스 종료 뒤 그 dir만 지운다. 그 안의 파일은 열지 않는다. 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l`이 같아야 한다.
  - 강제 종료 등으로 남은 root는 `python3 tools/shm_tests.py --clean-stale`로 확인(dry-run)하고 `--apply`를 붙여 삭제한다.

## graphify

- 코드베이스 질문은 `graph.json`이 있으면 먼저 `graphify query "<question>"`로 방향을 잡는다. 관계는 `graphify path "<A>" "<B>"`, 개념은 `graphify explain "<concept>"`를 쓴다.
- 넓은 탐색은 `graphify-out/wiki/index.md`가 있으면 그것을 쓴다. `graphify-out/GRAPH_REPORT.md`는 전체 아키텍처 검토 때만 읽는다.
- 코드를 수정한 뒤에는 repo 루트에서 `graphify update .`를 실행한다(AST만, API 비용 없음). worktree·격리 실행 home에서는 실행하지 않는다.
- 사용자가 `/graphify`를 입력하면 graphify skill을 먼저 사용한다.

## `.workflow` 용량 관리

과거에 `.workflow`가 60GB를 넘은 적이 있다. 기본 규칙(상한, checkpoint·hash manifest 범위, 정리 도구 사용, 보존 대상)은 `AGENTS.md` "개발 증거 용량 관리"를 따르고, 아래는 Claude 작업에서 추가로 지킬 절차다.

- 경고선은 8GiB다(상한은 AGENTS.md). SessionStart hook이 `.workflow`·`graphify-out`·`.worktrees` 중 8GiB 이상인 것을 경고한다. 경고가 뜨면 새 격리 실행이나 worktree를 시작하기 전에 정리부터 사용자에게 제안한다.
- 새 격리 실행 전후에 `du -sh .workflow graphify-out .worktrees`를 확인하고 증감을 보고한다.
- 용량 대부분은 다음 두 가지다.
  - 격리 실행마다 만드는 checkpoint(`*.json.gz`, 개당 약 70MB).
  - 격리 home의 `.omp/natives/*.node`(개당 약 180MB).
- graphify가 `.workflow`에 남기는 것은 수 KB짜리 실행 메타데이터뿐이다. 하지만 checkpoint에 `graphify-out/`이 섞이면 실행마다 수십~수백 MB가 늘어난다.
- worker packet에 checkpoint·hash manifest의 exclude를 명시한다: `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시.
- 격리 실행을 마친 worktree를 제거할 때는 내부 `.workflow/`도 같이 정리한다. `graphify-out/`의 일자별 snapshot 디렉터리는 최근 3개만 남긴다.
- 삭제 확인 범위(2026-10-24 사용자 결정):
  - 사용자 확인 없이 삭제하고 결과만 보고한다: 소유 process 종료를 확인한 재생성 가능 자료. `storage_maintenance.py --apply` 대상 캐시, `shm_tests.py --clean-stale --apply` 대상 root, 격리 실행을 마친 worktree와 내부 `.workflow/`, 최근 3개를 넘는 `graphify-out/` snapshot, 이번 작업·테스트가 `/tmp`에 만든 temp dir·파일(scratchpad, `/tmp/omp-workbench-*` 등)이 대상이다. 도구에 dry-run이 있으면 먼저 돌리고, 후보에 보고서·trace·실패 근거·DB 결과가 섞여 있으면 멈추고 묻는다.
  - 사용자 확인을 받은 뒤 수행한다: 보고서·trace·실패 근거·DB 결과(`/tmp/wb-cw16-reports` 포함)의 삭제, `--archive-checkpoints` 압축, 이번 작업이 만들지 않은 `/tmp` 항목, 위 범위에 없는 삭제.

## Agent skills

`mattpocock-skills` plugin(local scope)이 읽는 repo 설정이다. 이 repo의 `.agents/skills/` 절차·template과 충돌하면 repo 절차가 우선한다.

### Issue tracker

Local markdown under `docs/features/<feature>/` (BRIEF·SPEC·`tickets/<ID>.md`). See `docs/agents/issue-tracker.md`.

### Triage labels

Default five roles, recorded as a `Triage:` line in the issue file. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` (glossary) + `docs/adr/`. See `docs/agents/domain.md`.
