---
name: implement
description: "Execute an approved ticket graph: schedule safe independent work in parallel, integrate dependencies, write independent tests, review, fix and verify. Use when the user asks to implement or continue an approved ticket graph; not for discovery, planning, or review-only requests."
---

# Implement — dependency-aware verified delivery

Run `python3 ../workflow-ledger/scripts/workflow_tools.py brief --for implement` once: it prints sections 1, 3–9, 11 of
`.codex/workflow-contract.md`, the ledger, evidence and change-verification skills, the dispatch and progress references and the
request examples, so read none of them again. Read feature BRIEF/SPEC/PLAN, relevant tickets/ADRs; address exact agent names, never list them. Sol owns the complete execution; the project default is
High, but honor an explicitly selected supported higher effort. Do not switch models.

## Entry and resume

Record current scope authorization, `bundle-digest` and approval reference. Reconcile files,
evidence, candidates and host execution; active/unknown assignments forbid duplicate dispatch.
Plan/review-only is not implementation authority. Missing valid tickets means to-tickets;
never switch automatically to quick-change. Verify exact workers/test_designer/reviewer and actual
workspace capabilities; config names alone prove no isolation/model/effort. Reserve finite
capacity and record calls under section 8 and `../workflow-ledger/SKILL.md`; reconcile unknown
usage first. Missing instructions block affected dispatch; never substitute installation scopes. Report progress at events per `references/progress-report.md`. Write run records only under `.workflow/`, never `.codex-swarm/`.
Helpers: never read helper source. Follow the brief's setup/dispatch/result/integration examples:
one `batch --input /dev/stdin` per phase (reserve+starting before dispatch); `git apply`, other Git commands and `restore` stay standalone.

## Select an implementation worker

Use observed implementation constraints, preserving explicit user role/model pins:
- `bounded_pattern`: clear contracts and an established bounded pattern → `worker`.
- `coupled_invariants`: interacting invariants, concurrency or complex compatibility →
  direct `worker_senior`; no prior failure is required.
- `stalled_implementation`: known cause/fix direction but a concrete implementation obstacle →
  consider `worker_senior` using the returned delta and failed checks.
- `unresolved_diagnosis`: unresolved cause/design question → precise Oracle question or
  discriminating experiment; use the common contract's Oracle tier/read-only rules.
- `external_blocker`: environment, permissions, requirements or prerequisites → repair within
  authority or block affected work, not escalation to a stronger implementer.

File count, description length, keywords, confidence and failure count alone never select
senior. Senior preserves scope, permissions and independent verification. PLAN is advice;
record the actual selection and evidence in the assignment (`user_pin` when a pin decides).
Surface unavailable roles.

## Reassess results and retries

Classify worker results: candidate_ready needs independent verification; needs_reroute needs
implementation reassessment; needs_oracle needs a concrete diagnostic/design question;
blocked needs its authority/environment/contract prerequisite resolved. Apply the retry,
progress and budget rules of contract section 8; do not repeat an unchanged failing approach.
Apply section 7 finding adjudication and scope-preserving simplification before creating work.
At section 8 checkpoints reassess necessity; hypothetical expansion is not progress.

Before reassignment, confirm the previous writer and child processes stopped, inspect the partial
delta and release safe leases; unknown state forbids another writer in that workspace. Link the
prior assignment and create a new invocation without resetting usage.

## Resolve feasibility before expanding implementation

For a ticket with a critical unverified runtime/API assumption, follow contract section 7:
smallest real bounded experiment first, then choose the scope. Reuse current evidence; routine
changes need no new ritual. A failed or inconclusive probe calls for a focused repair or an
explicit blocker, not a larger candidate.

## Continue through decisions and blockers

Under sections 3, 4 and 9, resolve authorized facts/technical choices and preserve material
blockers canonically. Link direct ticket/gate impact; derive downstream impact from PLAN and
actual consumers, including integrated prerequisites. Unknown impact holds affected work.
Never split tickets or rewrite approval bytes to invent readiness.

After every result/integration/blocker/answer, select validated integrated inputs with current
contracts/candidate and free resources/leases/capacity; no wave barriers. Continue independent
implementation/tests/review/integration. Use permitted asynchronous questions; otherwise record
and ask after independent work ends. Section 9 governs waits, answer reconciliation and status;
no duplicate questions or silence-as-approval.

Before concurrent mutation, verify workspace binding, inputs, config, permissions and resource
claims under contract sections 4–5. Default pilot: two writers, within the effective thread cap;
one mutation owner per workspace includes Root, tests and subprocesses. Unknown isolation means
serial execution, with the limitation reported. Branches do not isolate files or shared services;
live exclusive resources serialize. Never invent tool parameters or hidden alternate agents.
For non-worktree workspaces require the binding smoke. Root-created detached worktrees use
`references/worktrees.md` and detective audits instead; failed creation means serial fallback.

Delegate bounded tickets with the assignment packet and explicitly resolved tdd reference
(scope/receipt, absolute path and file identity), agreed seams, independent expectation sources,
authorized test/production paths and commands. Follow section 7: one behavior's real RED ->
minimal GREEN -> next behavior; retain chronology or a justified exception, never blanket
stable-harness exemption. Audit against each recoverable checkpoint; collect attributable output only.

## Verification is automatic, not another user command

For each coherent verification unit, apply `../change-verification/SKILL.md` automatically.
Root dispatches independent test_designer and required reviewers without stage-start questions.
Use section 7's intermediate-one/final-two review table and separate security trigger
(`../change-verification/references/security-review.md`). Workers fix production; reviewers inspect
frozen code+tests. Section 7 permits Root's exact semantics-neutral reviewer wording correction
and bounded combined delta review; neither replaces the initial final pair. Recheck affected changes. Pipeline only isolated workspaces; never final-review
moving candidates, known required failures or missing decisive evidence.

Integrate verified attributable outputs serially, checkpointing before and manifesting after.
Test/review integration/conflict deltas on the integrated candidate before satisfying dependency
gates or starting dependents from that base. Integration-caused required failures use only
`references/rollback.md`'s delta restore: dry-run first, conditions checked, recorded, never
reset/clean/stash. Final verification covers cross-ticket behavior and the two independent axes.

## Handle deep blockers through Oracle

Workers/testers/reviewers return `needs_oracle` evidence to Root; they do not spawn.
After process/lease reconciliation, consult an Oracle
sibling under contract section 8, relay guidance, delegate the experiment/fix and repeat
required tests/review. Advice alone never resolves an issue; use the frontier procedure.

## Close or pause

Record current candidate, acceptance coverage, tests, review/delta review, integration and
scope-audit evidence. Declare only contract section 9 status; preserve every required blocker.
Stop when required scope/evidence is satisfied; optional hypotheses need no exhaustive rebuttal.
On interruption persist resumable state and pending assignments;
do not promise future execution. No commit/push/PR/deploy/production migration without authorization.

## Existing ticket identifiers

In an existing PLAN or ticket read `luna_worker` as `worker`, `luna_worker_max` and
`complex_worker` as `worker_senior`, `deep_oracle` as `oracle_senior`. Dispatch only the
current identifier without rewriting stored tickets; report an unknown identifier, never guess.

Use workflow-ledger for invocation/reservation records and workspace-evidence for checkpoints,
candidates and audits. Reconcile host/process state on resume; unknown starts hold capacity.
A handoff preserves prior authority as evidence, never new authorization.
