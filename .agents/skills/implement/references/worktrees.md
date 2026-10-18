# Parallel writers in Root-created worktrees

Read only when two or more ready tickets could run as concurrent writers. Contract section 5
governs. Everything here is Root-only; workers never create, remove or list worktrees, and
no worker commits, stages or resets. There is no spawn `cwd` argument: a worker learns its
workspace from an absolute path in the assignment, which is an instruction, not a boundary, so
isolation is detective and no prior binding smoke is required. Evidence: successful `git worktree add`, the baseline manifest, and the audit below.

## Eligibility

Use worktrees only when all hold: at least two ready tickets; disjoint `allowed_write_paths` and
compatible resource claims (contract section 4); no ticket needs to edit a file that the
integration workspace already has uncommitted changes in; every input a ticket needs can be
reproduced in the worktree (no needed ignored, generated or submodule input); and a first
`git worktree add` succeeds. Otherwise run serially and say why.

## Create (plain commands, as Root)

Run every `git worktree` command as its own plain shell command, e.g.
`git worktree add .worktrees/mod-ab-t01 -d HEAD`: standalone, not inside a script, subprocess
or heredoc, and not chained with other commands through `&&`, `;`, `|` or redirection. Codex
evaluates a chain per command and the most restrictive result wins, so a chained
`git worktree` command runs in the sandbox and fails. The project rule matches only the plain form.

1. Run `git worktree list`; never touch entries you did not create.
2. For each writer, `git worktree add .worktrees/<run-id>-<ticket> -d <base>` where `<base>` is the
   current integration HEAD (detached, no branch) and `<run-id>` is this run's unique id, so a
   path never collides with a leftover or a user worktree. This depends on the project's
   `.codex/rules/codex-swarm.rules` (project installs, trusted project). If it fails:
   - a read-only/permission error on `.git/worktrees` means the rule is not loaded (global
     install or untrusted project): do not work around it, run serially and report the reason;
   - any other error (path exists, bad base): report the actual error. Never delete or reuse a
     path you did not record in your manifest, and do not use an escalation or another sandbox override.
3. If the integration workspace has uncommitted approved changes (earlier integrated tickets),
   reproduce them in the worktree with `git diff HEAD --binary | git -C .worktrees/<id> apply`
   and copy any needed untracked files; record the changed-path list as `baseline_paths`.
4. Record every worktree in `.workflow/<feature>/runs/<run>/worktrees.json` (never under `.codex-swarm/` or `.worktrees/`): id, absolute path,
   base, `baseline_paths`, ticket and creation time. Snapshot it with `evidence.py snapshot`
   (root set to the worktree, `--output` a new file) as the baseline manifest; stdout is a summary.
5. Confirm config, skills and dependencies that the worker needs exist in the worktree.

Only `git` commands must stand alone. After the `add` commands, write the manifest, then run ONE `batch`: the ledger
`events` (all `reserve`, then all `starting`) plus every worktree's baseline snapshot (`--output`). Examples:
`workflow_tools.py formats --actions batch`; never read helper source.

## Dispatch

Assign as in `dispatch-and-oracle.md` with `actual_workspace` set to the absolute worktree
path, its baseline manifest and the ticket's allowed paths. Tell the worker to work and run
checks only inside that path and to leave `.git` alone. Ask it to run checks without writing tool
caches where supported (for example `PYTHONDONTWRITEBYTECODE=1`, pytest `-p no:cacheprovider`). Cap concurrent writers at 2 and open
child threads at the effective thread limit (contract section 1).

## Collect and integrate

Before integrating, also confirm the integration workspace and every sibling worktree still match
their manifests. Any write outside the worker's worktree or allowed paths stops further parallel
dispatch: restore the affected workspace with `references/rollback.md`, mark the run serial and report. The one exception: caches generated only by the worker's own required
checks (such as `__pycache__`) are removable artifacts; remove them, record it and re-audit instead of
going serial. Every other out-of-scope write still stops parallel dispatch.

After the worker reports and its subprocesses have stopped, take a final snapshot of the
worktree (`--output`) and `evidence.py audit` it against the baseline (both steps of one result `batch`, together with the running+finished events). Changed paths outside the allowed
scope, or inside `baseline_paths`, stop integration for that ticket (report; do not guess).
Build the delta patch with `git -C .worktrees/<id> diff --binary -- <changed tracked paths>`
and, for new files, `git -C .worktrees/<id> diff --no-index --binary -- /dev/null <file>`.
In the integration workspace, take the pre-integration checkpoint
(`references/rollback.md`), run `git apply --check` and then `git apply` for one patch at a
time, take the post-integration manifest, then verify as normal. A conflict is new work.

## Clean up

After the output is collected (or the ticket is abandoned), remove only worktrees listed in
your manifest with `git worktree remove --force .worktrees/<id>`, then `git worktree prune`;
force is acceptable because the worktree holds only this run's disposable copy and its delta is
already saved as a patch. Verify with `git worktree list`; if an entry shows `prunable` or a
remove failed with a read-only error, rerun the plain `git worktree prune` / `remove` command. On interruption leave the manifest
so a resume or ask-workflow can report leftover worktrees; on resume remove only the ones the manifest lists.
The rule allows `git worktree add|remove|list|prune` with any arguments, so path safety rests on this
manifest-only removal.
`.worktrees/` is git-ignored by the installer; do not commit or edit that ignore file.
