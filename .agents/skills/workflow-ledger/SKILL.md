---
name: workflow-ledger
description: "Root-internal invocation accounting, reservations and evidence reconciliation during authorized workflow work. Used by Root during implement or investigate-bug; not selected for general review or status questions."
---

# Workflow ledger

Print sections 8, 9, 11 of `.codex/workflow-contract.md` (`python3 scripts/workflow_tools.py contract --sections 8,9,11`; skip if already printed). This procedure records authorized
work; it never launches agents, establishes host truth or grants authority. Status requests
permit reads only. Use Python 3.11+ `scripts/workflow_tools.py` relative to this skill and
[formats](references/formats.md) for exact JSON requests, errors and recovery.

Read `ledger-read` directly (summary by default). It reports origin-specific ceilings,
usage, held reservations, active calls and unresolved facts without completed history.
Use full view only for diagnosis. `ledger-inspect` is optional when the file type is unclear;
do not add an inspect call before every read. Reconcile host observations before dispatch.
Unknown usage is null, never zero; `usage_known` describes arithmetic, not execution authority.

Use `ledger-init` only for an explicitly new run. `ledger-update` requires an existing valid
journal; never replace missing or legacy accounting with an empty run to regain capacity.
The request's `path` is the journal that `ledger-update` mutates directly. Optional CLI
`--output` saves only the command result summary to a new, create-only file; stdout also
receives the result. The summary has no event history. Ledger stdout is already compact
(summary view, no events); the other helpers' stdout is a summary too and their full result is
only in `--output` or behind `--full` (see formats.md). Never use the journal path as
`--output`, or copy/move a result over the journal, even as a workaround for a rejection.
Use a fresh result path or omit `--output`:

```bash
python3 scripts/workflow_tools.py ledger-update --input reserve.json --output reserve-result.json
python3 scripts/workflow_tools.py ledger-update --input reserve.json
```

These are alternatives, not two required steps. An output conflict requires correcting the
command. A recognized result summary is still not a journal: preserve it as evidence and
reconstruct from original sources and events rather than inferring missing history.
Supply a short event ID, expected_revision and event data using a JSON request file.
To save turns, `ledger-update` accepts `events` (an ordered list, applied all-or-nothing): record a
subagent call with two commands, `reserve` + `starting` before the host call and `running` +
`finished` once the result is back; run a phase's steps (ledger events, snapshots, audits, checks) as ONE `batch --input steps.json` whose inline requests behave exactly like the individual commands (`formats --actions batch`). Print request
examples with `python3 scripts/workflow_tools.py formats --actions ledger-update` rather than reading formats.md. Reserve
all applicable total/role/unit/incident buckets before dispatch and record starting before
the host call. Record observations and outcomes separately. Failed/interrupted starts remain
used; configured model/effort does not prove the observed model/effort.

On `conversion_required`, normal read/update/reconcile cannot repair the record. For authorized
conversion, stop and observe source writers, supply source hashes and explicit mapping, run
`ledger-convert` check, then write to a new destination with a dedicated preservation directory.
Compare source, mapping and replayed usage before switching references, then verify no writer
still uses the old source. Preserve all originals, user/host ceilings, failed calls and unknowns.
Journal conversion needs source-referenced limit metadata; when unavailable use an opening
summary with explicit unresolved facts, never invent prior history. Overlapping aggregates
need documented inclusion/deduplication; materialized calls are excluded from opening_used.
The helper validates structure and equations, not the truth of the supplied interpretation.

Inspect structured error code, next_action and mutation. A stale revision needs a fresh read
and decision. Retry an uncertain update with the identical event ID/data; a reused ID with
changed content is an error. Init/convert never overwrite an existing destination, including
on retry: read it and compare provenance. `mutation.ledger=unknown` after publication means
read/reconcile before further work. After `output_failed`, the ledger may already be committed;
do not dispatch twice. Preserve lock files and partial preservation artifacts; use the same
conversion identity for recovery. Never turn an unresolved exit 1 into a fresh zero baseline.

Keep operational records outside validation inputs. Operating limits alone may be revised
with an estimate and decision evidence; user/host entries are immutable. Resolve unknown
facts only through supported resolve-import targets and source evidence. This tool does not
find every historical ledger or enforce external-writer quiescence.
