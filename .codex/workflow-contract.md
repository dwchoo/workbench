# Repository workflow contract — v4

Repository policy, NOT a Codex API/schema or executable scheduler. Read this document
explicitly from the workflow skills. It never overrides system/developer instructions,
user authorization, managed permissions, or stronger repository rules.

**Section index.** Read only the sections your skill names, not the whole file. Print one
section with `sed -n '/^## 5\./,/^## 6\./p' .codex/workflow-contract.md`.

| § | Purpose |
|---|---|
| 1 | Entry points, internal procedures, roles, thread limit |
| 2 | Persistent artifacts (BRIEF/SPEC/PLAN/tickets) and handoff layout |
| 3 | Approval, revision, source of truth |
| 4 | Logical dependencies vs execution conflicts, resource claims |
| 5 | Workspaces, mutation leases, Root-created worktrees |
| 6 | Checkpoints, scope audits, restore and evidence |
| 7 | Mandatory tests, review, integration, quick-change risk signals, security triggers |
| 8 | Oracle and execution budgets, retries (single source for budget rules) |
| 9 | Status vocabulary, final results, resumption |
| 10 | User-invoked bug investigation |
| 11 | Operational tools (ledger, evidence, plan/gate), requested handoff, helper output |
| 12 | Limits |

## 1. Three phases, eight public entry points, a methodology and internal procedures

- `refine-spec`: user-selected Astra session; interview and document agreed intent.
- `to-tickets`: fresh user-selected Sol XHigh/Max session; compile the persisted brief
  into an implementation specification plus dependency-aware tickets in one invocation.
- `implement`: Sol-owned development; dispatch ready tickets, integrate, independently
  test and review, repair and run final checks. GPT-5.6 Sol High is the project default;
  an explicitly chosen supported higher effort is not automatically lowered.
- `ask-workflow`: inspect current evidence and recommend the next step; do not execute it.
- `brief-workflow`: user-explicit invocation only, in a main or side chat. Read-only full
  ticket/status briefing and explained decision options. Never invoke automatically or as
  an internal procedure, delegate it, or replace ordinary short progress reports with it.
- `investigate-bug`: explicit user invocation only; investigate a named bug and, when
  requested, repair it with independent verification. Recommended user-selected Astra Root,
  `oracle_senior` advice and `worker_senior` experiments/fixes; no model/config switch.
- `quick-change`: explicit user invocation only; a small low-risk single-writer change
  whose acceptance fits one or two sentences. The invocation is the approval; no BRIEF,
  SPEC or PLAN, and independent review stays mandatory (section 7). Ask-workflow may only recommend it.
- `improve-codebase-architecture`: explicit user invocation only; scope, survey and report
  deepening candidates. Report-only/no-candidate is a normal finish. After one selection,
  use the shared discovery method within this skill for grilling, domain decisions and an
  agreed BRIEF. Do not re-enter refine-spec or start implementation automatically.
- `tdd`: public methodology with implicit invocation allowed, not a workflow entry lane.
  Root explicitly supplies it to applicable development assignments; discovery is not required.
  Calling it grants no implementation authority and never bypasses another skill's explicit invocation.
- `handoff`: not a public entry point; used only when the user explicitly requests a
  handoff of workflow state to a new session.
- Root-internal procedures, not entry points: `change-verification` (implement and
  authorized bug repair, also usable for explicit verification), `workflow-ledger` and
  `workspace-evidence`. Root uses them during implement/investigate-bug; they are not
  selected for general review or status questions.

Refine-spec and architecture share `refine-spec/references/discovery-method.md`: resolve
facts from the codebase, ask the current decision frontier, maintain accepted vocabulary and
selective ADRs, then close BRIEF. Architecture writes an owned unique OS-temp HTML report
with inline CSS/SVG before/after diagrams, files, friction, benefits, ADR conflicts,
uncertainty and recommendation strength. Honor no-write requests with an inline report.
Do not change product code/tests, design a detailed interface before selection, or claim
visual verification/browser opening without observation. Accepted terms use existing glossary
conventions; unresolved repository proposals remain in its authorized plan location.
BRIEF approval authorizes fresh-session planning, not implementation (section 3).

In shared discovery rounds, number questions `1.`, `2.` and label each question's real
alternatives `a.`, `b.`, `c.`. Identify the recommended letter and reason, and show the
compact answer form `1.a, 2.b` and explicitly offer answering in the user's own words instead.
Accept free-form answers too; do not invent alternatives
or treat unanswered questions as accepted recommendations. Preserve the shown mapping
when interpreting replies; dependent questions still wait for their prerequisites.

Model-facing skills, MCP instructions and agent references default to English. Write
user-facing plans, specifications, reports and explanations in the user's language.
An explicit user language request takes precedence.

Skills do not select/switch the active model. Accept the user's stated model selection;
inspect authoritative host metadata when available. Label unavailable metadata unverified,
not a fabricated mismatch. Never claim a model switch from prompt text or self-report.
Actual known mismatches/capability limitations must be surfaced.

Only the active top-level owner delegates, integrates, updates shared workflow state and
spends the parent budget. All custom agents are leaves: no recursive spawning, no lifecycle
skill re-entry, no hidden CLI/SDK agent processes. This is enforced only by each agent's
`developer_instructions`: the agent files carry no tool-level block, and `[agents]`/`[features]`
keys in an agent TOML were runtime-tested (2026-09-30) and do not prevent a child from
spawning. Treat a leaf's compliance as observed behavior, not a security boundary. A worker
requests Oracle help through Root; Root calls an Oracle sibling and relays the evidence-backed answer. Domain skills
within assigned scope remain allowed. Root may consult Oracle during planning or development
for a specific hard technical question; Oracle is not a mandatory planning stage.

When Root has no independent work and is waiting for agent results, use a 60-second
wait timeout by default. Do not repeatedly request shorter timeouts without a concrete
reason. Longer waits are allowed only within host limits and higher-priority instructions.
Messages, completion and user input may return early; respond to those events promptly.
This is a wait policy, not a mandatory sleep, scheduler, or change to agent/call budgets.
The installed minimum/default settings require a new session and separate runtime evidence;
configuration acceptance alone does not prove effective host behavior.

Address a custom agent by its exact name (`worker`, `worker_senior`, `test_designer`,
`reviewer`, `oracle`, `oracle_senior`, `code_explorer`) as the spawn agent type. Never rely on
Root listing or discovering agents. An unavailable name is a reported capability gap; do not
substitute the generic default agent silently. `agents.max_concurrent_threads_per_session`
caps concurrent child threads excluding Root: 3 when unset in the observed build (codex-cli
0.159.0), 4 as installed. A spawn over the cap fails with `agent thread limit reached`; wait
for a child to finish instead of retrying, and never plan beyond the effective cap.

## 2. Persistent handoff, not shared conversation memory

Honor existing repository documentation layout. Defaults:
- `CONTEXT.md`: domain glossary only; use `CONTEXT-MAP.md` if the project already has contexts.
- `docs/adr/`: accepted hard-to-reverse, non-obvious trade-off decisions, created sparingly.
- `docs/features/<feature>/BRIEF.md`: approved intent, constraints, decisions, evidence,
  acceptance examples, non-goals, open/deferred questions and revision.
- `docs/features/<feature>/SPEC.md`: executable behavior, interfaces, invariants,
  failure semantics and verification requirements derived from that brief.
- `docs/features/<feature>/PLAN.json`: canonical ticket graph and scheduling constraints.
  Establish behavior, public test seams and independent expected values before making
  vertical tickets that each pair a tested behavior with its implementation. Keep pure
  refactors/probes explicit; do not split all tests from all implementation.
- `docs/features/<feature>/tickets/<id>.md`: one self-contained narrative per ticket.
- `.workflow/<feature>/state.json` and `runs/<run-id>/`: Root-owned operational records,
  in an authorized writable location. Relocate if repository policy requires it.
  Workflow records live only under `.workflow/`; never create or modify anything under
  `.codex-swarm/` (installer-owned state) or `.worktrees/` other than the worktrees themselves.

Create artifacts lazily. Do not put implementation plans into the glossary. Do not use
ADRs as a replacement for the brief. New sessions must read the full relevant brief,
glossary, ADRs and source references; absence from current chat is not absence of evidence.
Distinguish user decisions, observed facts, recommendations, assumptions, and unresolved items.
Do not publish issues, alter global configs, create PRs, commit/push, deploy, or migrate
production merely because a source skill recommends doing so.

## 3. Approval, revision and source of truth

A file saying approved is not authorization by itself. Record a user approval reference
and tool-computed manifest/digest in a separate state/run record. The approved bundle covers
BRIEF, SPEC, PLAN and ticket contents as relevant; hashes do not include their own record.
The digest formula is fixed: `approval.json` (feature folder; fields feature_id, status, approved_by,
approval_reference, bundle_files, bundle_sha256, brief/spec/plan revisions) lists `bundle_files` relative to its own folder,
and `bundle_sha256` is the SHA-256 of those files' bytes concatenated in the listed order, never including approval.json.
Compute or verify it with the `bundle-digest` helper action (`{"dir":"docs/features/x"}`); do not reconstruct it by hand.
Approval of a brief authorizes downstream planning, not product implementation. An explicit
request to implement an unambiguous presented plan may approve it and authorize execution
in one message. Do not ask again at each worker/test/review boundary. An explicit
`quick-change` invocation approves that one small change and its verification only.

Sol may elaborate technical details inside approved constraints. It must not silently
change user-facing behavior, scope, acceptance, compatibility promises or accepted ADRs.
Missing material decisions produce concrete blockers or a focused decision request, not a
second general interview. A prototype/research question can be recorded as deferred;
unresolved prerequisites block affected tickets rather than being invented away. This does not
authorize automatic ticket splitting, dependency edits or rewriting approval digests: even a
meaning-preserving change to approved bundle bytes follows the existing authorization rule.

For a material blocker, reuse its canonical record under `.workflow/<feature>/`; create a
local Markdown issue only if that record cannot preserve the needed context, following the
project's issue location. Keep a stable reference, classification, question/problem, observations,
directly affected tickets, acceptance/gate IDs, resume condition and status (`open`, `resolved`,
`dismissed`). For user decisions include recommendation/alternatives and the actual answer
reference. Preserve cause and resolution history; update the same issue for the same cause.
If a separate issue is canonical, state links its ID/path instead of copying questions/answers.
A justified non-applicability decision may dismiss an issue; Root cannot dismiss a required
product decision or demote a required defect to optional backlog. Accepted durable decisions
belong in BRIEF/SPEC and, when warranted, an ADR; unresolved questions are not ADRs.
Routine technical choices within approval need no issue. Investigate observable facts within
authority; distinguish user decisions, required defects, optional improvements and external
permission/resource/environment blockers. Preserve out-of-scope improvements as follow-up work
without blocking current scope.

PLAN.json is canonical for dependencies, resource claims, ticket IDs and scheduling fields;
ticket Markdown holds intent and criteria, referencing that plan without independently
maintained status/graph copies. The state file is an operational index, not proof. Verify
its revisions, candidates and evidence against the actual repository and host on resume.
Changed inputs make dependent evidence stale. Preserve completed/unaffected work when
regenerating a plan; retain stable IDs and record superseded tickets instead of silently
renumbering active work. A changed approved bundle needs scope-appropriate authorization.
PLAN's `worker` is a recommendation, not the actual dispatch role. Optional ticket
`routing_hint` contains `reason_codes` and `evidence_refs` string lists; validation checks
structure, not routing quality. Preserve existing PLAN bytes and legacy role spellings.
Root records the actual choice in its assignment under implement's worker selection rules.
Explicit user role/model pins take precedence; plan approval alone does not imply a pin.


## 4. Logical dependencies versus execution conflicts

Use dependency edges only for real prerequisite deliverables. Prefer verifiable vertical
slices; shared interfaces can be a bounded prerequisite. A wide refactor may use an approved
expand/migrate/contract sequence instead. Do not manufacture parallelism by claiming a
consumer is independent of an unsettled API. Contract-first parallelism must state the
shared interface, contract tests and subsequent integration verification.

For every required gate item, record its owning ticket, observable check, required evidence
level and the tickets supplying its prerequisites. All suppliers must be the owner itself
or its transitive predecessors. An acyclic graph can still have circular acceptance if an
early gate requires a later ticket's behavior. Split verification ownership without dropping
the requirement: for example, local redraw/state restoration and live detach/reconnect
belong to different checks when process separation is delivered later. Final integration
owns the combined behavior. Correct an existing approved plan with a recorded scope review;
do not silently weaken acceptance or add a reverse edge that creates a cycle.

Separately record write ownership and mutable-resource constraints: generated files,
lockfiles, ports, database/schema, caches, service instances and credentials where relevant.
A resource claim has a canonical resource name and mode `shared` or `exclusive`.
Two live claims conflict if names match and either is exclusive. Shared mode is valid
only for genuinely concurrent-safe access; read-only code may still invoke mutating tools.
File overlap or semantic conflict may require serialization despite separate worktrees.

A runnable ticket needs: authorized current plan; dependencies integrated and sufficiently
validated at the approved integration boundary; a current base snapshot; an available
worker slot; confirmed writable workspace and permissions; a free mutation lease and
compatible resource claims; a recoverable checkpoint; and reserved verification budget.
`parallelizable: true` alone is never sufficient. Recompute readiness after every event;
do not wait for an unrelated slow ticket to finish a whole artificial wave.

Derive downstream blocker impact from the current PLAN, without copying descendants into an
issue. Check actual contracts and consumers too: an omitted edge/supplier can pass structural
validation while hiding a real API dependency. Hold uncertain work; correct the plan only under
section 3. Reassess already integrated prerequisites if a new issue invalidates their contracts
or evidence. A missing expected issue reference or revision mismatch means unknown impact;
a normal legacy run with no issues needs no migration or new blocker. Shared exclusive
resources or unavailable isolation can require serial execution even for independent work.

## 5. Workspaces and mutation leases

Default to one mutation owner per physical worktree/workspace, NOT one writer globally.
Count Root, workers, test authors, formatters, generators and other mutating subprocesses.
Concurrent writers require distinct verified worktrees/isolated workspaces plus safe
shared-resource access. A new agent thread or different branch name alone is not isolation.
A worktree is filesystem-change isolation, not a security sandbox or database isolation.

Before dispatch, Root provisions or identifies an authorized workspace and verifies actual
cwd/repository identity, current base, writable roots, necessary config/skills, dependencies,
logs and service namespace. Do not invent a spawn `cwd` parameter. Use only capabilities
actually exposed by the installed host. Merely telling an agent a path is an instruction,
not an OS-level boundary. If isolation cannot be established, execute serially and report
the limitation; do not silently start multiple writers in the shared checkout.

For workspaces other than Root-created worktrees, before the first parallel writer dispatch, use a bounded smoke test of the actual agent
workspace binding and effective write boundary. Record observed cwd/roots, disjoint scratch
outputs and process/service namespaces. A Root shell writing two paths does not establish
agent isolation. Reuse evidence while the host, binding and environment remain unchanged;
otherwise recheck. Shared exclusive runtime resources still serialize live probes even in
separate workspaces. Lack of isolation blocks concurrent writes, not independent serial work.

A Git checkout at HEAD does not automatically contain dirty/untracked input. Preserve and
transfer the exact approved input snapshot through an authorized mechanism. Never commit,
stash, reset or clean user changes just to create a baseline. Use a manifest-backed
patch/output bundle when commits are not authorized.

Worktrees: Root may create detached worktrees at `.worktrees/<run-id>-<ticket>` (unique per run,
so they never collide with leftovers or user worktrees) for parallel writers under the implement
approval. Each `git worktree add|remove|list|prune` is a plain command: a standalone shell command,
not inside a script, subprocess or heredoc, and not chained with other commands through `&&`, `;`,
`|` or redirection (Codex evaluates a chain per command and the most restrictive result wins, so
the chain runs sandboxed). The project rules file `.codex/rules/codex-swarm.rules` allows exactly
those subcommands outside the sandbox; it exists only for project installs and applies only when
the project is trusted. If `git worktree add` fails with a read-only/permission error, the rule is
not loaded (global install or untrusted project): run serially and say why. Any other failure (path
exists, bad base) is reported with the actual error; never delete or reuse a path Root did not
record. Root records each worktree it creates in a run manifest and removes only those; it never
touches user worktrees or branches and never commits. Limitation: the rule allows
`git worktree add|remove|list|prune` with any arguments, so path safety rests on these
instructions (manifest-only removal), not on the rule. Collect a worker's delta as a patch, apply patches one at a time in the
integration workspace, and never let a worker create/remove worktrees. The procedure is
the implement skill's `references/worktrees.md`. A worktree remains filesystem isolation only. Codex offers no cwd binding for a spawned
worker, so a Root-created worktree needs no prior binding smoke: its binding is
instruction-level (an absolute path in the assignment) and isolation is detective. Evidence is a
successful `git worktree add`, a per-worktree baseline manifest, and a scope audit before each
integration showing that the worker's writes stay inside its worktree and allowed paths and that the
integration workspace and sibling worktrees still match their manifests. On any write outside the
worktree or scope: stop further parallel dispatch, restore the affected workspace by the section 6
procedure, mark the run serial and report.

Do not leave essential configuration
behind when workspaces are created before uncommitted configuration is installed.

A blocked worker must finish/stop mutating processes and report its partial delta before
its lease transfers. Do not hold a worker thread indefinitely just to await Oracle if that
starves capacity. Root alone integrates one attributable verified delta at a time into the
integration workspace. Merge conflicts are new work, not permission to choose a side blindly.
Rebase/merge/patch adaptation changes the candidate and invalidates affected check/review
coverage. Reassess remaining ticket assumptions after each integration.

## 6. Checkpoints, scope audits and evidence

Capture enough bytes/provenance to preserve staged, unstaged and untracked pre-existing
user work before each writer. A digest is not a backup. Audit actual changes against the
assignment baseline, not only HEAD: additions, removals, renames, modes, symlinks, relevant
ignored/generated inputs and submodule state. Resolve path boundaries. Scope restrictions
in a prompt are detective unless enforced by effective permissions.

Keep recovery checkpoints (bytes needed to undo attributable writes) separate from candidate
manifests (inputs needed to identify a check). Reuse unchanged content and reference existing
artifacts; do not copy the whole tree for every status update or read-only review. Produce a
new manifest when validation inputs change, and a checkpoint when a new writer needs recovery.

On out-of-scope writes: stop dependent integration, retain evidence, isolate/remove only
the attributable agent delta without harming user work, audit and rerun affected checks.
If safe attribution/recovery is impossible, leave files intact and report blocked. No
blanket reset/clean/restore, and never `git reset`, `clean` or `stash` for recovery. The one
allowed undo is Root's delta-limited restore after a failed integration: `evidence.py restore`
takes the pre-integration checkpoint and the post-integration manifest, is a dry run unless
`apply` is true, and writes only when every delta path still matches the post-integration
manifest; it restores only the integration delta and leaves other paths untouched. Workers never
directly edit the central plan/state/approvals or run restore.

A candidate ID resolves to a tool-observed immutable snapshot/manifest of validation inputs,
including relevant tests, fixtures, config, locks, new and dirty files, modes and submodules.
Record toolchain/service environment separately. Exclude only true operational logs/state,
not test inputs. Check input identity before and after a frozen review/check. Changes during
execution make the result stale. A bare commit SHA cannot identify an arbitrary dirty tree.

Each check records command, cwd, candidate, environment, exit code when executed, criteria,
result (`passed`, `failed`, `not_run`) and evidence. Empty collection is not useful pass proof.
Final coverage comprises the relevant original reviews plus bounded delta reviews; if impact
is uncertain, review the whole unit again. Do not relabel an older result as a final pass.

For feasibility/runtime gates, link structured results from the run ledger's `checks`.
Each result names gate/item IDs, candidate and approved requirements identity, required and
observed evidence levels (`static`, `fixture`, `skill_behavior`, `runtime`), command/cwd,
environment, exit code, result, evidence reference and unknowns. Required items come from
the approved plan, not from whichever checks happened to run. The project gate runner must
reject `passed` for missing/stale/failed items, required unknowns, or absent required runtime
evidence; fixture success cannot substitute. A schema alone does not enforce this decision.
Without a working validator, record the enforcement gap and do not claim a machine-validated
gate. The shared helper validates required evidence records; project-specific runtime assertions remain external.

## 7. Mandatory tests, review and integration

### Judge findings before creating work

Root adjudicates each finding against the supported scenario, consequence and evidence before
assigning a fix or another test. Keep the original finding and a brief disposition in existing
result/issue evidence; do not add a checklist, approval phase or journal schema. A tester status,
severity label or failing assertion alone is not a required defect. Explicit acceptance IDs are
not necessary to establish concrete correctness, data-loss or security violations.

- Accept violations of approved behavior or concrete correctness/data/security invariants in
  supported paths, including rare failures. Preserve the relevant regression checks.
- If a concrete material failure path has one uncertain premise, investigate that premise with
  a bounded check. Lack of a runnable reproducer alone does not justify dismissal.
- If an existing entry boundary/invariant excludes the premise, cite that boundary and close
  the finding; do not add redundant guards/tests. Check actual producers and bypass paths.
- Correct unsupported test expectations/fixtures rather than expanding product guarantees;
  retain coverage of real supported inputs. Fix environment failures and rerun the failed subset.
- Dismiss or defer unsupported assumptions, optional hardening and immaterial style suggestions
  without mandatory follow-up tickets. Reopen only on new material evidence.

Before accepting more defensive states, consider a smaller state space, single source of truth
or clear rejection that preserves approved behavior. Do not silently remove promised behavior,
compatibility or data preservation; ask only for the material product choice when needed.
This is implementation judgment, not a mandatory refactoring phase in every RED/GREEN cycle.
Existing structural cleanup remains bounded accepted review work or an approved pure refactor.

### Build and independently assess the agreed behavior

`implement` includes independent `test_designer` test assessment/code and fresh `reviewer` review for
each coherent verification unit, followed by feature integration verification. Group small
tickets where justified, but make a dependency gate explicit. Worker TDD does not replace
`test_designer`; passing tests do not replace reviewer analysis. Reviewer is not Root self-review.

For testable new behavior and regression repair, the assigned worker/worker_senior runs
one public-behavior test to a meaningful RED, makes only the implementation needed for
GREEN, then selects the next behavior. Do not write all tests first, add speculative next
features, or call an environment/import failure unrelated to the behavior a valid RED.
An existing exact regression failure may supply RED. An absent proposed public interface
may be a meaningful failure when it directly establishes the agreed behavior.

Root supplies packet content: actual resolved `method_reference` path and file/candidate
identity, `seam_refs`, observed behavior/independent expected-value source, authorized test
and production paths, and exact check commands. These are packet contents, not new spawn
arguments or journal schema keys. Resolve scope/receipt and actual file identity: project
`<project>/.agents/skills/tdd/SKILL.md`; global the selected actual home's
`.agents/skills/tdd/SKILL.md`. The global contract separately uses actual CODEX_HOME.
Never guess CODEX_HOME/skills or cwd. Conflicting/missing references or exact-role capability
gaps return to Root without silent model/role fallback.

Use agreed seams without repeated approval. Without an agreed seam, the Root/caller proposes
what each public interface check catches/misses and obtains user confirmation before tests;
a standalone tdd call does not itself agree a seam or expand authority. A leaf returns new
material seam/requirements gaps to Root. Tests use public behavior and independent expected
values, avoiding internal helper/call-count assertions as primary proof. Doubles isolate
external/nondeterministic dependencies and expose untested integration limits.

Reuse result/check/candidate records to link behavior/seam, RED command/cause/then-candidate,
minimal production delta and GREEN command/candidate. No per-cycle snapshot or new timing
schema is mandatory. Missing chronology is unverified; post-hoc old-fail/new-pass proves
sensitivity, not RED-first development. Documentation/comments, necessary probes and approved
pure refactors use appropriate checks; pure refactors retain the existing GREEN safety net
rather than fabricate RED. Record the behavior, exception reason, alternative verification and
uncertainty. Config/migration/performance labels alone are no exception. If no harness exists,
consider a small authorized fixture; do not add out-of-scope dependencies or infrastructure.

`test_designer` derives scenarios and expected outcomes from approved behavior before
inspecting worker tests or implementation, then assesses and reuses/extends meaningful coverage.
Add meaningful tests for new behavior/uncovered regressions; reuse proven existing coverage
with named evidence. Documentation-only work can justify no executable test addition.
Do not weaken acceptance tests to fit an implementation. Test failure may arise from code,
test, environment or contract; classify it using evidence. A known bug need not have a
perfect reproducer before Oracle can help design a discriminating experiment.

For a feasibility ticket or a change resting on an unverified critical runtime/API assumption,
first record: required assumption -> smallest real experiment -> passed/failed/inconclusive
with evidence -> next implementation scope. Reuse current decisive evidence. Use bounded
connection, one-turn and tool-control experiments as separate observations when relevant.
Implement only the probe and prerequisites needed to answer the question before expanding
the candidate. A timeout is inconclusive about its cause until the probe itself is sound:
drain child pipes/PTYs while running, bound execution and clean up its process group without
retaining sensitive output bodies. A broken probe needs a bounded repair, not provider blame.
Failed or inconclusive prerequisites block affected expansion; keep unrelated ready work moving.

Small changes (`quick-change`) are eligible only when all hold: acceptance fits one or two
sentences; one writer in one workspace; no risk signal. Risk signals: public interface or
contract change; a security trigger below; dependency, lockfile or migration change;
cross-module invariant; unverified runtime/API assumption; conflict with an approved ADR or
SPEC. File count, description length and confidence never decide eligibility. A risk signal
found at any point stops the change and reports the need for refine-spec/to-tickets. Verification
is reduced but independent: one frozen `reviewer` review and the required checks are mandatory;
`test_designer` may be skipped only by naming existing coverage.

Security review: security triggers are changes to authentication/authorization, attacker-
controlled parsing/deserialization, path/shell/SQL execution semantics, network trust boundaries,
cryptography or secret handling, dependency/lockfile resolution, CI execution, permissions or
sandbox settings. Judge changed behavior, input control and reachable execution boundaries,
not the presence of path/SQL/network words or a filename alone. Cite unchanged semantics and
prior evidence when reusing security coverage; uncertainty about a material boundary needs
focused investigation, not an unsupported no-trigger claim. When triggered, Root runs a separate `reviewer` invocation
with `review_kind: security` and a threat packet, plus deterministic checks (secret scan of the
diff; dependency audit when a lockfile changed) recorded through `verify.py check`. A missing
tool, unavailable network or blocked execution is `not_run` with unknowns, never a pass; do
not install tools automatically. A model security review is hypothesis-level evidence, not
proof of safety. Without a trigger, note "no security signal" in the review record.
Procedure: the change-verification skill's `references/security-review.md`.

Within each workspace/unit: decisive experiment when needed -> worker behavior-by-behavior
RED/minimal GREEN (or justified alternative) -> independent test assessment/completion ->
necessary production fix -> frozen code+tests review -> bounded corrections/delta review -> checks.

| Entry/stage | Required general review | Reuse and scope |
|---|---|---|
| implement intermediate coherent unit | one fresh general reviewer | Label Standards/Spec findings, but do not claim independent contexts. |
| implement final integrated feature | two fresh reviewer invocations, Standards and Spec | Same frozen candidate/scope for both; if the unit is the entire feature, this pair also fulfills its unit review. |
| investigate-bug authorized repair final | two fresh axis invocations | Incident expected behavior is the Spec; diagnosis alone never enters repair. |
| quick-change | existing one fresh general reviewer | Preserve risk gate and named-coverage condition for skipping tester; no independent-two-axis claim. |
| repository development | implement unit/final distinction | Actual roles/tools/authority must be available; instruction presence alone does not prove TDD. |
| standalone tdd | caller's existing path above | No new execution lane or review exemption. |
| accepted correction/delta | affected axes; bounded combined delta permitted below | Carry unaffected original coverage with impact evidence; do not relabel it as a fresh pass. |

For an independent pair, Root invokes the same reviewer persona in separate fresh contexts,
concurrently or sequentially within actual limits. Both inspect the same frozen code+tests
and full relevant diff, including new/dirty files; neither receives the other answer or worker
self-assessment. Optional packet `review_axis: standards|spec` differs from existing
`review_kind: general|security`; no host/journal schema change follows. Preserve each axis's
findings/evidence/unknowns. Repository standards override Fowler smell heuristics; label smells
as judgment, not hard rules. Accepted bounded corrections/refactors belong to a worker and
need affected checks/delta review, except the mechanical correction below. Optional cleanup does not block acceptance. Security review
uses the existing separate trigger/invocation above. Budget actual invocations (section 8).
Different units may pipeline concurrently in separate verified workspaces. Never review a
moving target simply because the reviewer itself is read-only. Final integration gets
cross-ticket tests and review coverage of integration deltas before required final checks.
Every approved acceptance ID must map to implementation and current verification evidence.

### Corrections and stopping

A reviewer-prescribed exact wording/typo correction with no product or contract meaning change
may be applied by Root under the single-writer lease, followed by exact diff inspection. Reuse
the original review and record the delta; no new tester/reviewer call is needed. New semantic
judgment is not this exception. Group substantive document review into the existing unit;
document-only changes do not by themselves require runtime tests.

For a bounded correction whose original Standards and Spec conclusions remain valid, one
fresh general reviewer may check the delta under both labelled headings. Preserve both original
results and candidate identities separately; this is not a new independent full pair or a
merged score. Changed requirement interpretation/design or uncertain impact requires separate
affected-axis contexts (full-unit scope only when impact warrants it). Required security delta
review stays separate. Fixture-only changes need affected expectations/cases assessed; carry
installation evidence only if its product/package inputs are unchanged. Environment correction
reruns the failed subset, not TDD or the full pipeline. Reopen installation checks when
packaging, entrypoints or asset resolution change. Scanner evidence is independently reusable
only while each scanner's relevant inputs/configuration remain unchanged.

Early technical review is optional, narrowly scoped to a concrete feasibility uncertainty,
and never final acceptance. Do not submit partial candidates to repeated final review while
known required failures or decisive runtime evidence remain unresolved. Start final review
when the unit's required checks pass and the candidate is stable. After corrections, retain
valid original coverage and review only the delta and affected contracts. Reopen the whole
unit only with an explicit impact reason. Optional cleanup does not block completion unless
it violates an approved criterion. When required coverage passes, integrate and advance the
frontier; another unchanged review is not a next action.

## 8. Oracle and Root-managed execution budgets

This section is the single source for budget and retry rules. Skills point here and add
only their own action, not a restatement.

`oracle` (High) and `oracle_senior` (Max) are two configured variants of one
read-only advisory role. No Astra Max planner, automatic pre-implementation Oracle pass,
self-dispatch, product modifications or final approval powers. Root owns decisions.

Call for a specific hard root cause, conflicting observations, repeated bounded failure,
cross-module invariant or design contradiction. Do not call for a routine typo, missing
install/permission, or unknown file location that a focused lookup can resolve. Worker,
tester or reviewer may request advice, but Root selects and invokes the sibling Oracle.
High is the default; Max can be selected directly for a documented deep question or once
as escalation. Do not require a wasted High call first. Repeated calls need new evidence or
a genuinely unresolved question. Stronger model output is still a hypothesis until checked.

Consultation input: ticket/incident ID, spec and candidate, constraints, expected/observed
behavior, logs/reproducer if available, attempts and outcomes, known/unknown facts, precise
question. Output: supported observations, hypotheses with discriminating experiments,
smallest fix direction, risks, regression-test ideas, whether an approved decision is
invalidated, and what remains unknown. Root/worker executes mutating experiments/fixes.
Oracle advice never closes a ticket, waives verification or changes acceptance criteria.

Declare finite operating budgets before dispatch, including independent tests, reviews,
corrections and final integration. Root may allocate and increase these internal call budgets
within authorized development without another user approval. Estimate remaining mandatory
work plus a justified correction reserve; do not double a limit without explaining the need.
Resize before capacity is exhausted, preserving capacity for verification. A depleted internal
estimate alone is not a blocker or a reason to ask the user whether development may continue.

Record each limit's origin. Explicit user ceilings on cost, time or calls and host limits are
hard constraints; Root cannot raise or bypass them. A Root-authored number in an approved
PLAN/ledger is an operating estimate unless the user explicitly made it a ceiling. Call counts
are not monetary budgets. Resolve unknown origins from actual instructions before changing
the affected limit; never invent authorization. Scope, acceptance, permissions, model/effort
assignments and publication boundaries remain unchanged. Concurrency defaults remain 4 open
child threads, 2 isolated writer workspaces and 1 integration owner; budget growth does not
increase concurrency. Incident tracking and resource locks remain procedural unless enforced.

Use 2 worker attempts per unresolved issue, 2 corrective review cycles per unit, and 2 Oracle
calls per incident / 4 per run as Root reassessment checkpoints, not user-approval gates.
At a checkpoint reassess whether the scenario is required and whether a scope-preserving
design simplification removes its cause, then compare failed scenarios and observations.
A new reproducer, ruled-out cause or verified affected behavior is progress only when it moves
a required acceptance decision forward; expanding hypothetical cases, restating a hypothesis
or assigning a new incident ID is not. Apply section 7 adjudication from the first finding. If progress supports another attempt, allocate a finite correction-and-verification batch
with a specific next action and success/stop criteria. Further Oracle calls need a concrete
unresolved question and new evidence or a discriminating experiment, not another opinion.
Without progress, change approach through a smaller reproducer, the existing senior worker
or targeted Oracle diagnosis. If that bounded change of approach also yields no progress and
no concrete next experiment, block the affected ticket instead of repeatedly increasing its
budget. Continue independent ready work; report a genuine impasse when no safe work remains.
Ask for user decisions only when needed to resolve scope/requirements/permissions or an
explicit user ceiling, not to approve routine internal allocation.

On an authorized resume under this policy, reconcile prior usage, reservations and limit
origins. Record the policy transition and Root's allocation decision; an old pending request
for internal budget growth need not remain an approval gate. Do not silently reclassify an
explicit user ceiling. Preserve unresolved-issue history across runs, tickets and incidents.
This policy does not authorize starting/resuming work from a guidance-only request.

Maintain an append-only invocation ledger: call ID, role, ticket/unit/incident, purpose,
configured model/effort, host-observed model/effort (or `unverified`), status and evidence.
Count every started child invocation, including failed/interrupted calls and resumed turns
that start a new assignment. Polls/messages are not new calls. Record limit, used, reserved
and available (`limit - used - reserved`) for total calls and the applicable role/incident/
unit budgets; reservations are unspent capacity and are consumed on dispatch. Oracle calls
count toward both Oracle and total budgets. Initial final review and each corrective cycle
are distinct; early reviews consume calls too. Reconcile resumed runs with host/run records.
Missing history means usage unknown, not zero: resolve affected capacity before dispatch.
Record budget changes with old/new limits, remaining-work estimate, reason, decision owner
and authority reference. For internal changes cite this policy and the existing development
authorization; do not claim a new user approval. Never reset usage or erase failed attempts.
Configured models and self-reports cannot establish the actual assignment or explain defects.

## 9. Status and resumption

Suggested phase: discovery / planning / ready / implementing / verifying / blocked / complete.
Ticket states: pending -> ready -> running -> candidate_ready -> verifying -> integrated;
blocked and stale are explicit alternatives. Worker 'done' does not satisfy an integration edge.
Final result:
- done_verified: current approved scope, tests, independent review, scope audit and required
  final checks all satisfied on the delivered integrated candidate; no material unresolved issue.
- implemented_unverified: implementation exists but necessary evidence is missing; do not
  hide an observed required failure here.
- blocked: known required failure, contract/scope/permission/capability/recovery blocker,
  exhausted explicit user/host limit, or a no-progress impasse; not a depleted internal estimate.

Once required behavior and current evidence are sufficient, stop. Do not add experiments to
refute every nonblocking hypothesis. Classify pre-existing test failures by regression and
required-evidence impact; never delete an approved required gate for convenience. Preserve
original findings separately from Root dispositions; a dismissed finding is not a reviewer
retraction. Unresolved accepted required findings still prevent completion.

Additional done_verified conditions: no unresolved High-or-above security finding and no
required security check left `not_run`.

A blocked ticket does not make the run blocked while safe independent implementation, tests,
review or integration remain: keep phase implementing/verifying. If only active work remains,
wait for its results under section 1. Stop after safe work is exhausted, an explicit user stop
or a user/host limit; retain partial results and resume conditions. A required unresolved
decision or defect means final blocked, even if stored elsewhere; evidence-only gaps retain
implemented_unverified. A common permission/environment blocker may affect the entire run.

Use a permitted asynchronous question channel when available and continue independent work.
Otherwise record the decision and exhaust independent work before asking through a permitted
channel. Obey higher-priority channel/mode restrictions; do not put prohibited questions into
progress reports or misuse a blocking tool. Silence, elapsed time and recommendations never establish approval. Do not repeat a question without new information or changed impact.
On an answer, update its canonical record and recheck authority, revision and candidate impact.
Confirm affected writers/subprocesses stopped, preserve partial deltas and reconcile leases
before changing their inputs; receiving an answer must not mutate a frozen review candidate.
Rerun affected checks/review against current requirements and candidate identity; an answer
alone neither completes a ticket nor makes old evidence current. Preserve unaffected work,
usage and failed-attempt history. Never end solely to await a decision while runnable work remains.

Progress reports to the user are event-driven (after a dispatch batch, integration,
verification result or rollback, blocker or needed decision, resume, and the final report);
never periodic. Fields: phase with as-of time; ticket counts by state and frontier names;
active calls; verification status per unit (passed/failed/not_run/unknown); budget from the
ledger summary (limit/used/reserved/available and origin; unknown is never shown as 0, with the
`ledger-read` time); blockers or decisions needed; one next action. About twelve lines.
Format: the implement skill's `references/progress-report.md`.

An explicit `$brief-workflow` request uses its separate detailed format: all tickets with
current stage, remaining work and issue presence; issues classified as agent-resolvable,
user input useful, or user decision required; explained options and recommendation reasons.
No target argument is required: resolve the current feature from context and records, asking
its plain name only when ambiguous. Explain for a reader unfamiliar with the project and
its documents. Read the fixed briefing reference on every invocation and retain its five
sections, table columns and empty-state rows.
The twelve-line progress-report target does not apply to that requested briefing. Read
persisted records and available context, show their as-of revision and uncertainty, and do
not infer current execution from file presence or a worker's completion claim. The briefing
does not change state, run checks, resume work or forward answers between chats. In a side
chat, offer a copyable decision message with target, issue ID and revision for the owning
session to reconcile. Existing authorization remains valid; classification creates no new
approval gates. A user-required decision blocks only its actual affected work.

On interruption record active assignments, candidates, partial deltas and evidence, then stop.
No promise of future/background execution. On resume inspect the host and processes; do not
assume recorded leases are free or recorded workers remain active. Do not launch duplicate
workers for a ticket with unknown execution state. `ask-workflow` reports uncertainty and
one next step; it never writes state, releases leases, invokes Oracle, or resumes work itself.

## 10. User-invoked bug investigation

Start `investigate-bug` only when the user invokes that skill. A bug report, repeated failure,
or a skill-explanation question is not invocation. Implement must not enter it automatically,
even by reading its instructions as an internal procedure. Ask-workflow may recommend it only.

Resolve diagnosis versus repair from the actual request. Diagnosis permits safe local repros
and isolated temporary probes, not permanent product changes. Explicit read-only restrictions
also prohibit writing diagnostic artifacts. Repair requests authorize bounded fixes and
section 7 verification without another permission question at each phase. Neither mode
permits unrelated refactors, production changes, or publication without authorization.

A brief/ticket bundle is optional. Record the user request, expected-behavior source, scope,
acceptance/checks, candidate and environment in an incident under
`.workflow/diagnostics/<incident-id>/`, respecting project conventions. These are the approved
criteria for independent verification; they do not create a parallel ticket graph. Keep
observations distinct from hypotheses and unresolved product decisions. Default to diagnosis
when an explicit invocation does not request repair. Do not reopen settled requirements.

Reuse existing evidence and link prior incidents, attempts and cumulative budgets. A new
session does not reset user limits. Do not take over another Root's ledger, active assignments
or mutation leases. Resolve ownership before mutating the same workspace. Section 8 governs
internal budgets; use senior personas for this skill's experiments/fixes and hard advice.
An absent decisive repro allows bounded observation experiments, not claims of a proven fix.

After authorized repair, apply change-verification with the incident's criteria and all
required evidence levels. Recheck the original scenario on the final cleaned candidate.
Report diagnosis or verified repair, uncertainty and artifacts; do not mark an unrelated
feature complete or resume implement. A later explicitly resumed owner reconciles the new
candidate and evidence against its full ticket acceptance. Without a productive next experiment,
report the incident blocker; do not expand into other tickets.

## 11. Operational tools and requested handoff

Skill profile 3 supplies workflow-ledger and workspace-evidence as internal Root procedures,
and handoff only on explicit user request. Resolve the shared Python helpers relative to the
installed skills, including global installs. They record evidence, not host scheduling or authority.
Separate requirements revision, candidate, invocation, check and execution state from task outcome.
Worker results candidate_ready/needs_reroute/needs_oracle/blocked are task outcomes, not
journal lifecycle states or proof of stopped processes. Reviewer results are
no_material_findings/findings/needs_oracle/blocked; test_designer results are
tests_ready/coverage_sufficient/implementation_defect/needs_oracle/blocked. Each names its
candidate_id and the scope inspected. Security review reuses the reviewer statuses. Assignment/result packets link via
invocation and existing transition evidence; Root checks the journal ID and actual role.
Rerouting creates a new invocation and retains all prior usage; journal v1 fields stay unchanged.
Use only the typed `workflow-invocation-journal` v1 for live accounting. Initialize explicitly;
updates never create a missing ledger. Read the compact summary directly; use full view for
diagnosis. Legacy records require explicit, source-preserving conversion, never an empty reset.
Imported baselines exclude every individually represented call; unknown counts remain null.
Keep operating/user/host limits separately and bind to their minimum. Resolve unknown scope,
usage and limit origins before the affected dispatch. Conversion retains source hashes, copies,
field mappings and uncertain facts; source interpretation is not proved by a matching hash.
Reconcile retained history and active writers before resuming; unknown starts retain capacity.
Helper stdout is a compact summary, never file bytes: snapshot, audit, restore, plan, check, gate
and handoff print status, ids, counts and capped path lists (first 20 plus totals) and the
`--output` path; ledger commands already print summaries without event history. The complete result
(checkpoint blobs, identity maps, check records) goes only to the create-only `--output` file, or
to stdout with `--full` for diagnosis. Save snapshots, check records and other later inputs with
`--output`; do not read those files into context, pass their paths. Request fields and one
example per common action are at the top of `workflow-ledger/references/formats.md`; print the whole start-up
reading of `implement` or `quick-change` with `workflow_tools.py brief --for NAME` and only
the part you need with `workflow_tools.py formats --actions A,B` or `contract --sections 1,3-4`. To spend
fewer turns, `ledger-update` takes an `events` batch (ordered, all-or-nothing; a subagent call is two
commands: `reserve`+`starting` before the host call, `running`+`finished` after the result) and
one `batch` per phase (dispatch, result, integration checkpoint, integration verification) runs several helper actions in order from inline requests, with the same validation and atomicity per step; git commands, `restore` and `handoff` never join a batch.
Use the common PLAN and gate validators with approved requirements. Project-specific assertions
and independent review remain necessary; zero exit status alone never establishes acceptance.

Handoff may describe a feature, incident or planning task without a PLAN or ledger. Preserve
project conventions, otherwise create a new `.workflow/handoffs/<id>/HANDOFF.md` and manifest.
Never overwrite an earlier handoff. Include purpose, goals, authority and decision evidence,
partial/failed/unverified results, active work, failed attempts, next actions and purposeful links.
Use the user's language unless explicitly requested otherwise. Exclude secrets. Capture candidate and referenced record identities;
mark active writers, changing input, missing records and contradictions explicitly.
Generation does not resume work, change models, stop processes or run checks. At reception,
compare actual files, authority, ledger and evidence before acting. Reading/status requests
remain read-only; retain valid prior authorization without requiring approval merely for a new
session. Report missing fields, broken references, stale evidence, mismatches and unknowns
separately, never a single readiness score. Provide a short next-session starter and artifact path.

Handoff manifests written by the helper use version 2 and compact typed invocation snapshots
bound to the exact journal bytes/hash. They omit completed history. Version 1 manifests remain
read-only historical evidence; embedded old ledger state is opaque and does not enable legacy
operational reads. Re-read the current journal and reconcile host observations at resume.

## 12. Limits

This bundle is instruction/configuration scaffolding, not a worktree manager beyond the
Root-created worktrees above, a lock service, durable job runner, sandbox profile or CI gate. Static parsing cannot prove
runtime model selection, root-only delegation, permissions, actual parallel execution or
workflow compliance. Test those with the supplied smoke cases on the installed Codex build.
