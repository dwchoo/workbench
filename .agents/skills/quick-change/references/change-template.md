# Quick change record

Use `.workflow/quick/<id>/CHANGE.md` unless the project has a convention. One record per change.

## Request and criterion

- User request/reference:
- Source of the expected behavior (test, doc, issue text, user statement):
- Acceptance criterion (one or two sentences):
- Allowed write paths and prohibited paths:

## Eligibility (mark each yes/no with evidence)

- Public interface or contract change:
- Security trigger (auth, input parsing, path/shell/SQL, network, crypto/secrets, deps/CI, permissions):
- Dependency, lockfile or migration change:
- Cross-module invariant:
- Unverified runtime/API assumption:
- Conflict with an approved ADR/SPEC:
- Single writer, single workspace:
- Result: eligible / stopped (refine-spec or to-tickets needed)

## Execution

- Workspace, base, checkpoint and candidate (including dirty/new files):
- Ledger run, reservations and actual calls:
- Worker result and audited delta:

## Verification

- Required checks (command/cwd/environment/exit code/result):
- test_designer result, or the named existing coverage that replaces it:
- Frozen reviewer result and candidate:
- Unverified scope and unknowns:
- Final status (done_verified / implemented_unverified / blocked):
