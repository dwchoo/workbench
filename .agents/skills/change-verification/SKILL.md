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

After the previous writer/subprocesses stop, transfer the mutation lease to `test_designer`
with requirements, acceptance IDs and test-only scope. It derives scenarios/expected values
before worker code/tests, then reuses or extends meaningful coverage. Worker GREEN is not
independent assessment; author identity does not invalidate tests. Documentation can justify
no executable addition. Audit against baseline.

Classify implementation/test/environment/contract failures. Apply section 7's supported-scenario,
consequence and evidence judgment before work: labels/assertions alone do not expand guarantees.
Preserve real rare defects; investigate material uncertain premises and consider scope-preserving
simplification. Keep findings/dispositions separate. Assign accepted fixes to the caller-selected
writer (`worker_senior` for investigate-bug), preserving independent acceptance tests.
For a hard diagnosis Root may consult Oracle under the common budget. Do not let fixes
silently alter the desired behavior or turn failed tests into skipped tests.
Budgets, retry checkpoints and reassessment follow contract section 8; reserve the next
correction, independent tests and review together.

## Frozen review

Early review addresses one feasibility question and consumes budget, never replacing final
review. Resolve required failures/missing decisive evidence through a bounded experiment/fix first.

After tests/fixes, freeze code+tests. Follow section 7 and `references/review-axes.md`:

| Entry/stage | General review |
|---|---|
| implement intermediate coherent unit | one fresh reviewer; label both axes, no independent-context claim |
| implement final feature / authorized bug repair final | Standards and Spec in two fresh separate contexts |
| entire feature equals one unit/candidate/scope | final pair also satisfies unit review |
| quick-change | existing one fresh reviewer; preserve risk/tester rules |
| repository development / standalone tdd | implement distinction / caller's existing lane |
| corrections | section 7 mechanical exception or bounded combined delta; otherwise affected axes; justify carry-forward |

Root gives each required reviewer the same frozen candidate/full relevant diff (including
new/dirty files), SPEC/incident criteria, base and applicable standards/source context, never
worker self-evaluation or the sibling axis's answer. Include relevant CODING_STANDARDS.md/
CONTRIBUTING.md. Optional packet review_axis is standards|spec, separate from review_kind;
no spawn/journal keys. Use concurrent/sequential fresh invocations within actual limits.
Keep findings/evidence/unknowns per axis. Repository rules override `references/code-smells.md`
heuristics. No writer mutates reviewed inputs.

Apply section 7 correction rules: Root may apply a reviewer's exact semantics-neutral wording
and inspect the diff without new tester/reviewer calls; substantive document review stays in
the unit. For bounded combined delta review use `references/review-axes.md`. Requirements/design
changes hold affected work and reopen affected independent axes. Full-unit review needs an
impact reason. Supply original findings/dispositions and current inputs; justify coverage reuse,
never relabel it as fresh. Assess fixture expectations/affected cases, rerun environment-failed
subsets, and reopen installation checks for packaging/entrypoint/asset-resolution changes.
Stop when required coverage passes; optional hypotheses do not keep the unit open.
Passing coverage returns to integration; no-progress failures follow section 8 and return
preserved findings/blocker to Root.

## Security review

When a contract section 7 security trigger applies to the frozen candidate, also read
`references/security-review.md`: a separate `reviewer` call with `review_kind: security`, a threat
packet and deterministic secret/dependency checks; unavailable tools are `not_run`, never a pass.
Without a trigger, note "no security signal" in the review record.

## Integration and final evidence

Verification remains provisional until Root integrates: audit/test cross-ticket contracts and
review merge/conflict deltas. Include approved final acceptance/repository checks. Record each
check's before/after candidate, command/cwd/environment/exit/result/log and acceptance coverage.
Link ledger checks to approved gate items/evidence levels; missing validators are enforcement
gaps. Never relabel stale/missing evidence or fixture success as runtime pass. Return evidence
and gaps to Root; no remote publication.

## What the evidence establishes

Use agreed public seams, independent expectations and meaningful regression sensitivity.
Avoid tautologies or mocking away behavior; name integration limits of external/nondeterministic
doubles. GREEN does not establish worker RED-first chronology. Distinguish static parsing,
fixture execution, model/tool behavior and actual host runtime evidence; none proves the next
level. Prompting guides do not waive independent testing/frozen review or acceptance.

Record authorized execution with `scripts/verify.py check` and compare current evidence to
approved required items with `gate`, inside the phase `batch` (examples:
`workflow_tools.py formats --actions check,batch`). Exit 0 alone is not acceptance. Preserve
execution records; add item observations, unknowns and evidence in a new record. Link path/hash
through workflow-ledger. Read-only review/navigation never executes checks.
