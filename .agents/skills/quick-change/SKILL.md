---
name: quick-change
description: "Make one small, low-risk, single-writer change with reduced but independent verification. Explicit user invocation only; not for changes with interface, security, dependency or design risk."
---

# Quick change — small verified change without a ticket graph

Start only when the user invokes this
skill; an ordinary "fix this" request or an implement run never enters it. The invocation is the
approval for this one change and its verification, nothing wider. Only Root delegates; all
personas are leaves. Root is the user-selected session (Sol, High default); do not switch models. Run `python3 ../workflow-ledger/scripts/workflow_tools.py brief --for quick-change` once: it prints sections 6–9, 11 of
`.codex/workflow-contract.md`, the change template, the ledger, evidence and change-verification skills, the dispatch and progress
references and the request examples, so read none of them again.

## Check eligibility first

Record the request, the source of the expected behavior and a one- or two-sentence acceptance
criterion in `.workflow/quick/<id>/CHANGE.md` ([template](references/change-template.md)).
Then check every contract section 7 risk signal against the request and the code you read:
public interface/contract change, security trigger, dependency/lockfile/migration, cross-module
invariant, unverified runtime/API assumption, conflict with an approved ADR/SPEC. Also require
one writer in one workspace. Judge by these signals only; file count, description length and
confidence never decide. If you cannot state the acceptance criterion briefly, or any signal
applies, stop before editing and report that `refine-spec`/`to-tickets` is needed. There is no
automatic switch and no partial fast path. Recheck when new facts appear during the change.

## Execute

Never read helper source. Keep every record under `.workflow/quick/<id>/`, never `.codex-swarm/`. Run each helper phase as ONE heredoc command `batch --input /dev/stdin` (examples in the brief).
Sequence: (1) write CHANGE.md from the template; (2) setup batch = `ledger-init` (limits: total 4, role:worker 2,
role:reviewer 1, role:test_designer 1; unknown usage is never zero) + baseline `snapshot` with `checkpoint: true` (`--output`);
(3) worker: one dispatch batch (`reserve`+`starting` events), then dispatch a bounded `worker` by exact name with the
assignment fields of `../implement/references/dispatch-and-oracle.md`, then one result batch (`running`+`finished` events +
`snapshot` + `audit` against the baseline). Budget, retry and reassessment follow contract section 8.
A worker that reports `needs_reroute`, `needs_oracle` or `blocked`, or a delta reaching a risk
signal, ends the quick lane: report it rather than growing the change.

## Verify independently

Freeze code and tests and run the required checks as plain commands, recording command, cwd, exit code and log excerpt in
CHANGE.md (use the `check` action only when a requirements file exists). Skip `test_designer` only when CHANGE.md names existing
coverage of the behavior with evidence, or states that the change cannot alter behavior (documentation or comment text);
otherwise dispatch it (one dispatch batch, one result batch). A fresh `reviewer` is always mandatory: one dispatch batch, then
one result batch whose final `snapshot` must equal the frozen candidate (see `../change-verification/SKILL.md`).
A worker's tests or Root self-review never substitute. Apply corrections through a worker and
review only the delta. Missing verification is not a pass.

## Close

Report done_verified, implemented_unverified or blocked by contract section 9, with the
candidate, checks, review result, unverified scope and the CHANGE.md path. Give progress reports
per `../implement/references/progress-report.md` at events only. No commit, push, PR or deploy
without authorization; do not resume or alter another run's ledger or leases.
