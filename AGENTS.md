# Workbench

Manager OMP·worker OMP·host persistent terminal을 연결하는 작업 환경. Task Inbox & Web Board는 Phase 2 기능이며, Hub BRIEF(`docs/features/workbench-hub/BRIEF.md`) → Task Inbox BRIEF 순서로 명세·구현을 진행 중이다.

## 기본 규칙

- 한국어로 응답하고 기술 용어·라이브러리·코드 식별자는 원문을 유지한다.
- 문서는 한국어, skill·MCP 관련 문서는 영어가 기본이다. 사용자가 다른 문서 언어를 요청하면 전환 전에 확인한다.
- 요청 범위만 최소한으로 변경하고 기존 사용자 변경을 보존한다. 합의되지 않은 기능·기술 선택을 확정하지 않는다.
- 관측 사실·사용자 결정·미결정을 구분하고, 변경에 맞는 최소 검증 결과를 보고한다.
- MCP 서버 실행·설치에 Docker를 사용하지 않는다. Commit·게시·배포는 별도 요청이 있어야 한다.

## 제품 경계

- 본체는 Python 중심으로 개발한다. Linux를 우선 지원하고 OS 의존 처리를 분리한다. Windows는 제외한다.
- 1차 core와 Phase 2를 구분한다. Phase 2 구현은 core 완성·검증 이후다(2026-10-25 사용자가 core 완성으로 판단해 Phase 2 착수).
- Workbench 내부에서 tmux 등 외부 terminal multiplexer를 실행·의존하지 않는다. 외부 tmux·herdr 안에서 실행할 수 있어야 한다.
  - 예외(2026-10-21 사용자 결정): 이미 tmux 안에서 시작된 경우에 한해 `wb` 시작 시 실행 중인 tmux의 `mouse`·`allow-passthrough`를 확인·적용하는 `tmux show`/`tmux set -g` 호출과, 사용자가 승인한 경우의 tmux 설정 파일 블록 추가는 허용한다. 실패해도 시작을 막지 않는다.
- 외부 요청 접수는 실행 승인이 아니다. Manager의 승인 명세와 worker의 실행·결과 보고를 구분한다.
- 현재 요구사항은 [Core BRIEF](docs/features/core-workbench/BRIEF.md), 용어는 [CONTEXT.md](CONTEXT.md)를 확인한다. 참고 스펙과의 차이는 [대조표](docs/features/core-workbench/SOURCE-RECONCILIATION.md)를 따른다. 초안은 승인이 아니다.
- 개발용 검증·로그 보관은 [BRIEF](docs/features/dev-validation-log-retention/BRIEF.md)를 따른다. 신규 raw 정책(rotation) 활성화는 별도 사용자 결정이다.
- 실험 위임·worktree·수동 개입은 [제품 동작 규약](docs/features/core-workbench/OPERATING-CONTRACT.md)의 합의와 제안을 구분해 따른다.

<!-- BEGIN CODEX_ORCHESTRATION_WORKFLOW -->
## Codex feature workflow

Preserve repository safety/build rules. `.codex/workflow-contract.md` holds the shared
handoff, scope, concurrency, Oracle and evidence contracts. Do not open the whole file: print only
the sections your skill names with `workflow_tools.py contract --sections`.

Use `$refine-spec` for an explicitly requested Astra requirements/design interview. Save the
agreed BRIEF plus glossary/selective ADR updates, not product code. In the fresh user-selected
Sol XHigh/Max planning session, `$to-tickets <brief>` creates SPEC and the dependency/resource
aware ticket bundle in one command. Do not implement or invent missing product decisions.

Use `$improve-codebase-architecture [scope]` only on explicit user invocation. Survey and
report deepening candidates; report-only is a normal finish. A selected candidate continues
through grilling/domain decisions to BRIEF within that skill, without restarting refine-spec
or automatically authorizing implementation.

`tdd` is a public methodology, not another execution lane. In authorized behavior/regression
development, Root passes its actual scope/receipt-resolved reference and agreed seam; workers
run one behavior's RED -> minimum GREEN before the next. Missing harness is assessed, not a
blanket exemption. Preserve justified exceptions and chronology evidence under contract §7.

Use `$implement <feature>` for authorized development. Sol owns ready-ticket scheduling,
safe workspaces, worker TDD, independent test_designer assessment, integration, corrections
and final checks. Intermediate units need one fresh general review; final features and bug
repairs need fresh independent Standards/Spec contexts. quick-change keeps one review; apply
contract §7's whole-unit reuse, affected-axis delta and separate security rules. Do not ask separately whether to test/review. Skills do
not switch models; development defaults to GPT-5.6 Sol High unless the user selects otherwise.

Use `$ask-workflow` when asked what stage/command comes next. Inspect artifacts/evidence,
recommend one next action, and never execute that recommendation as part of navigation.

Use `$brief-workflow` only when the user explicitly invokes it. No argument is required;
resolve the current feature from context and records, asking its name only if ambiguous.
It provides a read-only full ticket and decision briefing for a reader unfamiliar with the
project, in a main or side chat. Never select it automatically, delegate it or call it as an
internal procedure; keep ordinary short progress reports unchanged.

Use `$investigate-bug <symptom-or-incident>` only when the user explicitly invokes it.
Recommend Astra Root with oracle_senior advice and worker_senior experiments/fixes, without
switching models or settings. Diagnosis is the default; requested repairs include independent
verification. Do not invoke it automatically from implement, take over its ledger, or resume
implement after investigation. Reuse relevant evidence and return a bounded incident result.

`change-verification` is the internal verification procedure for implement and requested bug repair. Preserve explicit review-only
mode without writes. All custom agents are leaves. Root alone invokes `oracle` or
`oracle_senior` for concrete deep blockers, including worker help requests.

One mutation owner per physical workspace; simultaneous writers require verified isolation
and compatible shared resources. Root integrates serially and verifies the final combined
candidate. Root adjusts finite internal call budgets under contract section 8 without routine
approval waits; explicit user/host limits, user changes and authorization remain binding.
Never infer success from a status flag alone or publish/commit/deploy without separate authorization.

Use `$handoff <target> [purpose]` only on user request to prepare a model/session transfer.
Creation or reading never resumes implementation, runs checks, stops writers or switches models.
Keep valid prior authority and reconcile actual files, assignments and evidence on reception.
Root uses workflow-ledger and workspace-evidence internally for authorized accounting,
checkpoints and candidate/scope checks. Internal classification does not guarantee UI hiding.
<!-- END CODEX_ORCHESTRATION_WORKFLOW -->

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

## 개발 증거 용량 관리

- `.workflow/`와 `graphify-out/`는 생성물이다. 일반 source corpus나 복구 checkpoint에 통째로 넣지 않는다. 필요한 운영 기록은 파일별로 지정해 관측한다.
- 새 격리 실행 전 `du -sh .workflow graphify-out`으로 용량을 확인한다. `.workflow`가 10GiB 이상이면 새 실행을 시작하지 않고, 종료된 실행의 재생성 가능 캐시부터 정리한다. 그래도 초과하면 큰 항목과 보존 이유를 보고한다. 이는 개발 증거의 운영 경계이며 제품의 로그·조회·Q01 예산을 변경하지 않는다.
- checkpoint는 수정 복구에 필요한 source·index만 보관한다. 그래프·캐시·격리 실행 home은 제외하며, 복구 데이터 32MiB 상한 초과 시 범위를 좁힌다. 증거를 조용히 생략하지 않는다.
- 일반 확인·리뷰·테스트 전후에는 hash manifest를 사용한다. 복원 필요가 있는 변경 직전에만 byte checkpoint를 만들고, 동일 후보의 checkpoint를 단계마다 반복 생성하지 않는다. 비교하는 manifest들은 같은 watch/exclude 범위를 사용한다.
- 격리 실행이 끝나고 소유 process 종료를 확인한 뒤 `python3 .agents/skills/workspace-evidence/scripts/storage_maintenance.py`를 먼저 dry-run으로 실행해 후보를 확인한 뒤 `--apply`를 붙여 재생성 가능한 OMP native/model 캐시를 정리한다. 보고서·trace·실패 근거·DB 결과는 유지한다. 실행 중인 자료는 정리하지 않는다.
- 성공·실패·중단 모두 종료 후 용량을 다시 확인한다. 실패한 실행의 private home을 통째로 보존하지 말고 진단에 필요한 기록만 유지한다. 정리하지 못한 자원은 경로·용량·사용 중인 owner를 보고한다. 복구 자료의 보관·압축·삭제는 참조와 원본 hash를 확인하고 수행한다.
- 오래된 대형 checkpoint의 압축은 명시적 정리 때만 `--archive-checkpoints`로 수행한다. 원본 hash와 압축 경로를 기록하고, 필요하면 같은 도구의 `--restore <원래 경로>`로 복원한다. 압축된 checkpoint를 없거나 검증 실패한 것으로 판정하지 않는다.
