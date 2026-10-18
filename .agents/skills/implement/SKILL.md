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

Establish explicit authorization for the current plan/scope, recording its `bundle-digest` and
approval reference. Reconcile state with files, candidates, evidence and host execution status;
do not duplicate an active/unknown assignment. A plan-only or review-only request is not
permission to implement. If no valid ticket bundle exists, return the need for to-tickets
(for a tiny change the user may invoke `$quick-change`; never switch automatically).

Check availability of the named workers, `test_designer`, `reviewer` and workspace capabilities.
Do not declare supported isolation/model/effort merely because the config names it. Reserve
finite capacity and record every started call under contract section 8 and
`../workflow-ledger/SKILL.md`. Reconcile unknown usage before spending. Missing instructions block affected dispatch; never silently substitute another
installation scope. Report progress at events per `references/progress-report.md`. Write run records only under `.workflow/`, never `.codex-swarm/`.
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
Handle partial blocking through the frontier procedure below.

Before reassignment, confirm the previous writer and child processes stopped, inspect the partial
delta and release safe leases; unknown state forbids another writer in that workspace. Link the
prior assignment and create a new invocation without resetting usage.

## Resolve feasibility before expanding implementation

For a ticket with a critical unverified runtime/API assumption, follow contract section 7:
smallest real bounded experiment first, then choose the scope. Reuse current evidence; routine
changes need no new ritual. A failed or inconclusive probe calls for a focused repair or an
explicit blocker, not a larger candidate.

## Continue through decisions and blockers

Apply contract sections 3, 4 and 9: classify the discovery, resolve technical/factual questions
within authority, and preserve material blockers in their canonical record. Link direct ticket
and gate impact; derive downstream impact from PLAN and actual consumers, including integrated
prerequisites. Unknown impact holds affected work. Never split tickets or rewrite approval bytes
to manufacture readiness.

After each result, integration, blocker or answer, select work with validated integrated inputs,
current contracts/candidate and available resources/leases/capacity; no global wave barrier.
Continue independent implementation, tests, review and integration. Use permitted asynchronous
questions; otherwise record the question and ask after independent work is exhausted. Follow
section 9 for active-result waits, answer reconciliation and final status, without duplicate
questions or treating silence as approval.

Before concurrent mutation, verify workspace binding, inputs, config, permissions and resource
claims under contract sections 4–5. Default pilot: two writers, within the effective thread cap;
one mutation owner per workspace includes Root, tests and subprocesses. Unknown isolation means
serial execution, with the limitation reported. Branches do not isolate files or shared services;
live exclusive resources serialize. Never invent tool parameters or hidden alternate agents.
For non-worktree workspaces require the binding smoke. Root-created detached worktrees use
`references/worktrees.md` and detective audits instead; failed creation means serial fallback.

Delegate each bounded ticket using the selection criteria and assignment packet.
Workers use small relevant red/green/refactor checks where practical. Root audits each
assignment against its own recoverable checkpoint and collects attributable output only.

## Verification is automatic, not another user command

For each coherent verification unit, apply `../change-verification/SKILL.md` automatically.
Root dispatches independent `test_designer` and a fresh `reviewer`; no user stage-start request.
Use `../change-verification/references/security-review.md` when contract section 7 triggers.
Tests must meaningfully cover behavior or establish sufficient existing coverage. Workers fix
production code; reviewers inspect frozen code+tests. Rerun affected checks and review changed
impact. Pipeline units only in safe separate workspaces. Never final-review a moving candidate,
known required failure or missing decisive gate evidence.

Root integrates one verified attributable output at a time into the integration workspace.
Take a checkpoint before each integration and a manifest after it. Recheck the new integrated
candidate; integration/conflict deltas receive affected tests and review. Only then satisfy
dependency gates. If a required check fails because of this integration, restore only that
delta per `references/rollback.md` (dry run first, apply when its conditions hold, record it; never reset, clean or stash). Dependent work starts from that integrated base,
not an earlier parent commit that lacks the prerequisite. Final feature verification must
cover cross-ticket behavior, not only isolated successes.

## Handle deep blockers through Oracle

Workers/testers/reviewers return `needs_oracle` evidence to Root; they do not spawn.
Root may identify the question itself. After process/lease reconciliation, consult an Oracle
sibling under contract section 8, relay guidance, delegate the experiment/fix and repeat
required tests/review. Advice alone never resolves an issue; use the frontier procedure.

## Close or pause

Record current candidate, acceptance coverage, tests, review/delta review, integration and
scope-audit evidence. Declare only contract section 9 status; preserve every blocker.
On interruption persist resumable state and pending assignments;
do not promise future execution. No commit/push/PR/deploy/production migration without authorization.

## Existing ticket identifiers

In an existing PLAN or ticket read `luna_worker` as `worker`, `luna_worker_max` and
`complex_worker` as `worker_senior`, `deep_oracle` as `oracle_senior`. Dispatch only the
current identifier without rewriting stored tickets; report an unknown identifier, never guess.

Use `../workflow-ledger/SKILL.md` for durable invocation/reservation records and
`../workspace-evidence/SKILL.md` for checkpoints, candidates and scope audits. On resume compare
host assignments/processes and retained checks; unknown starts hold capacity. Read a supplied
handoff as evidence, preserving prior authority, never as a new authorization by itself.
