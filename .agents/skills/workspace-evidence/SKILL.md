---
name: workspace-evidence
description: "Root-internal candidate manifests, recovery checkpoints and write-scope audits for authorized workflow work. Used by Root during implement or investigate-bug; not selected for general review or status questions."
---

# Workspace evidence

Print sections 5, 6, 11 of `.codex/workflow-contract.md` (`python3 ../workflow-ledger/scripts/workflow_tools.py contract --sections 5,6,11`; skip if already printed). Use
`scripts/evidence.py snapshot|audit|restore --input request.json` relative to this skill. The shared
[formats](../workflow-ledger/references/formats.md) opens with request fields and an example
for each routine action; print just the ones you need with
`python3 ../workflow-ledger/scripts/workflow_tools.py formats --actions snapshot,audit`.

Stdout is a compact summary (status, ids, counts, first 20 paths with totals, `--output` path),
never file bytes. Always pass `--output NEW_FILE` for a snapshot, checkpoint or restore result
you will use later, and pass that file's path in later requests (`audit`, `restore`, handoff). Do
not read manifests or checkpoints into context; `--full` prints the complete result and is for
diagnosis only. Root reads the summary and the file paths, not the full stdout of any helper.

Resolve the actual Git workspace root, relevant ignored inputs and operational exclusions.
Observe the whole relevant workspace; allowed write scope is a separate audit input. Default
Git discovery includes tracked and non-ignored new files, including outside the allowed scope.
Add relevant ignored/generated paths to watch. Do not exclude tests, fixtures or configuration.
Reuse the same observation policy across checkpoints, checks and handoff comparisons.

Before a writer use checkpoint=true to retain bytes including index content; for checks use
small manifests. Stop/reconcile writers through the existing owner before claiming a frozen
candidate. A stable double collection does not prove no active writer exists. Dirty submodules
need their own snapshot/checkpoint. Audit additions, removals, content, modes, links and index.

Never reset/clean/stash user work. The only restore is `restore`: a dry run by default that
undoes just the integration delta between a pre-integration checkpoint and the post-integration
manifest, and writes (`apply: true`) only if every delta path still matches that manifest;
Root applies it during implement after a failed integration (see the implement skill's rollback reference). Checkpoints contain sensitive
project bytes: keep them in local private operational storage. Unknown input or collection
changes invalidate stability. Report scope violations and preserve evidence for safe attribution.
