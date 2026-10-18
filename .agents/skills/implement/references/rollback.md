# Restore after a failed integration

Read only when an integrated candidate fails. Contract sections 6 and 9 apply. Restoring is
allowed only for the integration delta Root just made; it is never a substitute for reset,
clean or stash, which stay forbidden.

## Before each integration

Take a checkpoint (`snapshot` with `checkpoint: true`) of the integration workspace as the
pre-integration record, and after applying the delta take a normal snapshot as the
post-integration manifest, both with the run's watch/exclude policy (`batch-pre`, standalone `git apply`, `batch-post`). Pass `--output NEW_FILE`
for each so the file holds the bytes; stdout prints only a summary and the candidate id. Keep both
paths in the run evidence and never read the files into context.

## Trigger

A required check fails on the integrated candidate, an integration conflict stays unresolved,
or a scope audit violation is attributable to that delta. First classify the failure
(implementation/test/environment/contract). A test defect or environment problem is not a
rollback trigger; wait for the writer and its subprocesses to stop before restoring.

## Restore

1. Run `evidence.py restore` with `{"root", "pre", "post"}` (no `apply`): it is a dry run
   and lists the delta, the actions and any conflicts.
2. Apply it in the same run only when all hold: status `restorable`, no conflicts (every delta
   path still matches the post-integration manifest and index), no active writer, and the
   failure is attributable to this delta. Then repeat with `"apply": true`.
3. Any conflict or `blocked` result means someone changed those paths afterwards: leave files
   as they are, report the paths and ask the user. Do not force it.
4. Save the result (`--output`; stdout is a summary) beside the run evidence and cite its path in the next ledger transition or
   check evidence, then take a new manifest and confirm `matches_pre_delta`. The candidate
   stays stale until affected checks pass again.

Budget already spent is not returned. The affected ticket becomes `stale`; reassign it with a
new invocation as in the retry rules of contract section 8.
