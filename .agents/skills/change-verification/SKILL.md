---
name: change-verification
description: "Root-only independent testing, code review and final candidate verification used inside implement or an explicitly requested investigate-bug repair. Root-internal: not selected for general review or status questions. Preserve read-only review requests; never turn review-only into test/code writes."
---

# Change verification — internal procedure

Print sections 6–9, 11 of `.codex/workflow-contract.md` (`python3 ../workflow-ledger/scripts/workflow_tools.py contract --sections 6-9,11`; skip if printed). Root performs this procedure; leaf agents return results
instead of delegating. Establish mode, approved criteria, verification-unit scope, candidate
and environment. Explicit review-only mode permits inspection/findings only: no tests-as-code,
fixes, integration or implementation dispatch. Do not call implement recursively.

For investigate-bug repairs, use the incident's user request, behavior criteria, scope and
required evidence instead of a ticket SPEC.
Return results to the invoking Root without starting implement or modifying its ledger.
Diagnosis-only authorizes neither production fixes nor this repair pipeline.

## Independent tests

In authorized implementation mode, transfer the workspace mutation lease only after the
previous writer and subprocesses stop. Dispatch `test_designer` with acceptance IDs,
requirements and test-only write scope. Expected outcomes come from contracts first.
`test_designer` must add/update meaningful tests for new behavior or uncovered regressions; proven
existing coverage can be reused with named evidence. Documentation-only work may need
no executable test changes. Audit test delta against its baseline.

Classify failures by implementation/test/environment/contract using evidence. For a valid
failing test Root assigns a production fix to the caller-selected writer (`worker_senior` for investigate-bug), preserving independent acceptance tests.
For a hard diagnosis Root may consult Oracle under the common budget. Do not let fixes
silently alter the desired behavior or turn failed tests into skipped tests.
Budgets, retry checkpoints and reassessment follow contract section 8; reserve the next
correction, independent tests and review together.

## Frozen review

Optional early review addresses one feasibility question, consumes call budget and never
replaces final review. Failed required checks or missing decisive runtime evidence require
a bounded experiment/fix before final review.

After tests/fixes, freeze code+tests. Give a fresh `reviewer` SPEC/criteria, the complete relevant
diff (including new files), base/candidate identity and source context, excluding worker
self-evaluation. In relevant_contracts, include only instructions and standards applicable
to candidate paths (including CODING_STANDARDS.md and CONTRIBUTING.md). Read-only reviews of separate
stable candidates may run concurrently; writers must not mutate reviewed inputs.

Root adjudicates findings. Route accepted bounded fixes to a writer. If requirements
or accepted design change, pause affected work. Material deltas need delta/impact review and
affected checks; if impact cannot be bounded, review the unit again. No repeat reviews without
new code, evidence or uncovered scope. Carry forward unaffected original coverage and give delta
reviewers the original findings, dispositions, changed inputs and affected contracts. Record an
impact reason before another full review. Optional improvements never block unless tied
to an approved criterion. Passing coverage ends the cycle and returns to integration.
Repeated failures without progress follow contract section 8; return the blocker and preserved
findings to Root rather than repeating an unchanged cycle.

## Security review

When a contract section 7 security trigger applies to the frozen candidate, also read
`references/security-review.md`: a separate `reviewer` call with `review_kind: security`, a threat
packet and deterministic secret/dependency checks; unavailable tools are `not_run`, never a pass.
Without a trigger, note "no security signal" in the review record.

## Integration and final evidence

Isolated unit verification is provisional until integrated. Root integrates under exclusive
integration ownership, audits the delta, checks cross-ticket contracts and runs relevant
integration tests. New merge/conflict changes require review coverage. Include final feature
acceptance and required whole-repository checks where the approved verification plan requires.
Record candidate before/after each check, command/cwd/environment/exit code/result/log and
acceptance coverage. Stale or missing evidence cannot become a final pass by relabeling.
Link structured gate results in ledger `checks` and compare them with the approved required
items and evidence levels. Reject a fixture-only pass for a runtime requirement; report a
missing project validator as an enforcement gap, not as validated gate execution.
Return evidence and gaps; Root declares the contract's final status. No remote publication.

## What the evidence establishes

`test_designer` exercises the agreed public behavior with contract-derived expectations,
including a relevant failure or regression; avoid assertions that recompute the implementation or
mock away the behavior under test. Use doubles only at external or nondeterministic boundaries
and state what integration stays untested. Reuse approved test seams without new approval.

Label parser and file checks as static, controlled helper executions as fixture tests,
model responses and tool actions as skill behavior evidence, and actual host sessions as
runtime evidence. A result at one level does not establish a result at the next level.
Keep independent testing and frozen review even when a newer prompting guide recommends
less; scale checks without weakening acceptance.

Use `scripts/verify.py check` to record authorized check execution and `gate` to compare all
current evidence with approved required items, inside the phase `batch`; print examples with
`workflow_tools.py formats --actions check,batch`. Exit code 0
is not an acceptance assertion. Preserve execution records and add explicit item observations,
unknowns and evidence in a new record. Record the result path/hash through workflow-ledger.
Read-only review/navigation uses recorded evidence only and never runs the check command.
