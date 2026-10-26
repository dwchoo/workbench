# Review axes and candidate identity

Root selects the review scope required by the current workflow contract. Final implement
features and approved investigate-bug repairs need Standards and Spec in **separate fresh
reviewer invocations**. An intermediate coherent implement unit needs one fresh general
reviewer that reports both headings; this is not two independent contexts. If one unit is
the whole feature at the same candidate/scope, the final pair also satisfies unit review.
Quick-change keeps its existing single fresh general review and risk/tester conditions.
Do not request a reviewer after every RED/GREEN cycle. Existing security triggers remain
separate; axis selection does not replace a required security review.

## Freeze the input

Root supplies both axes the same frozen candidate and exact diff identity, including
dirty/untracked product and test files, baseline and commands. Include the relevant
specification/acceptance revision, repository instructions/standards and verification
evidence. Resolve the baseline first; do not let each reviewer silently choose another
comparison or review a moving working tree. If the spec is unavailable, report that gap;
do not synthesize a requirement and label the Spec axis passed.

Packet content may identify `review_axis: standards|spec`; retain the existing
`review_kind: general|security`. These are packet semantics, not new host spawn arguments
or journal schema fields. Use the existing reviewer persona/model. Each axis receives
its own scope and raw inputs, not the other review's findings or the worker's self-grade.
Root may run them concurrently or sequentially within actual thread and cost limits;
sequential separate contexts still preserve independence. A leaf does not spawn reviewers.

## Standards

Judge the changed code against applicable repository standards and instructions, citing
the rule and affected file/hunk. Include CONTRIBUTING/CODING_STANDARDS when present.
Consult [code-smells.md](code-smells.md) for the Fowler smell baseline. Distinguish a
documented-rule violation from a labelled design heuristic; repository policy overrides
the heuristic. Skip checks deterministic tooling already enforces, and show concrete
impact before recommending refactoring.

## Spec

Compare changed behavior and tests with the originating specification/acceptance examples.
Report missing or partial requirements, incorrect implementation, and unrequested scope.
Cite the requirement and the evidence that disagrees. Treat an incident's approved expected
behavior as the repair specification. A green test result alone does not prove coverage.
Check that tests use the agreed public seams and independently derived expected values.
Flag internal-helper/call-count assertions used as primary proof and tautological coupling
between tests and implementation.

## Findings, corrections and reuse

Return findings, evidence, uncertainty and unverified scope separately for each axis.
Keep Standards and Spec conclusions visible; do not merge them into a score that masks
failure on one axis. Root decides which scope-relevant findings need correction and assigns
the worker to change the candidate, then runs affected checks. Use contract section 7's
supported-scenario/consequence/evidence judgment, retaining original findings separately from
Root dispositions. Its exact mechanical wording exception needs only Root's diff inspection.

For a correction, Root states which axes the delta affects and supplies a new frozen
identity for their review. One fresh general reviewer may check a bounded delta under both
headings only when the original independent Standards and Spec conclusions remain valid.
This is combined delta coverage, not a new independent pair. If requirement interpretation,
design or uncertain impact requires rejudging those conclusions, use separate affected-axis
contexts; expand to the whole unit only with an impact reason. Required security delta review
remains separate. Carry unaffected original coverage only with an explicit impact
argument and its original candidate identity; do not relabel an old result as a fresh pass.
Optional cleanup does not block completion. Avoid repeating the same unchanged opinion
without new evidence; a changed candidate or material new evidence warrants normal review.
