---
name: ask-workflow
description: "Inspect workflow evidence and recommend one next step. Guidance only; no execution or state changes."
---

# Ask workflow — evidence-based navigation

Print sections 1, 3, 9, 11 of `.codex/workflow-contract.md` (`python3 ../workflow-ledger/scripts/workflow_tools.py contract --sections 1,3,9,11`, relative to this skill). This skill is a navigator, not an orchestrator or a
new model role. It may run in any user-selected top-level session. Do not spawn Oracle or
workers, write documents/state, claim approval, start/resume execution, or switch models.

Resolve the target feature from the explicit path and available context. Inspect relevant
BRIEF, SPEC, PLAN, ticket files, operational state, approval references and validation evidence.
If more than one feature plausibly matches and available context cannot resolve it, ask which
feature rather than guessing the most recent filename. Use read-only inspection only.

Before describing a skill or recommending that its stage be skipped, inspect only the
relevant parts of its current SKILL.md; reuse instructions already inspected and still current.
Read them as navigation references without running the procedure. If unavailable, state the
material limitation instead of assuming the summary is sufficient.

Determine stage from evidence, not merely file presence or an `approved`/`complete` flag:
- No settled intent: refine-spec in Astra.
- Agreed brief but missing/stale executable plan: to-tickets in Sol XHigh/Max.
- Plan draft: review/approve that actual revision; do not say coding is authorized.
- Approved runnable tickets: implement in GPT-5.6 Sol (High default).
- Partial decision block: inspect canonical issues, current PLAN and actual consumer/evidence
  dependencies. Recommend implement for independent work still runnable, not whole-run approval
  or planning again. Missing expected references/revision mismatches mean unknown impact;
  legacy runs without issues need no new artifact. No safe work left: state the decision/resume
  condition, distinguishing active-result waits from final blocked.
- Interrupted run: reconcile execution state and resume implement without duplicate writers.
- A user seeking a separate focused bug investigation: recommend explicit
  `$investigate-bug <symptom-or-incident>` with diagnosis/repair intent, preferably in Astra.
  Recommendation is not invocation; do not read and execute it as an internal procedure.
- A change small enough for one sentence of acceptance with no interface, security, dependency or
  design risk, and no ticket bundle: recommend explicit `$quick-change <request>`. Recommendation
  is not invocation; if any risk signal is unclear, recommend refine-spec/to-tickets instead.
- Hard implementation blocker: resume the Sol owner with the incident context; Root may
  consult Oracle, but ask-workflow must not invoke it.
- Implementation exists but review/tests/integration evidence is absent or stale: resume
  implement for verification, not another requirements interview.
- Actual final evidence is current and sufficient: complete; identify remaining separately
  authorized delivery actions without executing them.

For contradictions, say unknown/stale and explain the specific missing evidence. Do not
run tests merely to answer navigation; report their recorded validity and limitations.

Return: feature, observed stage, brief supporting evidence/uncertainty, unresolved blocker,
recommended model/effort, and exactly one next command/action with a reason. A recommendation
is not permission to run it. Prefer the narrowest continuation over regenerating all artifacts.

When operational helper records exist, use workflow-ledger's read/reconcile and handoff's
read-only validation against current files. Preserve unknown/legacy records and report any
assignment, candidate, check or authority disagreement. If a run manifest
`.workflow/*/runs/*/worktrees.json` lists worktrees still shown by `git worktree list` (or marked
prunable), report them as leftover Root worktrees and recommend resuming `implement` to clean them
up. Never remove or prune them here. Recommend `$handoff` when the user
wants a model/session transfer; recommendation never invokes it or resumes implementation.
