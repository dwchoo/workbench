---
name: to-worker
description: Delegate experiments and free work to the Workbench worker via to_worker, one task at a time. Use for delegation, follow-ups, re-runs and cancellation.
---

# to-worker (manager)
As Workbench manager, you delegate the user's work: `to_worker` creates and immediately delivers a Task without approval. Use `to_worker` for every worker instruction. The worker does the work you delegate: do not do it yourself (no Task commands, edits or checks); wait for the worker's `to_manager` report. Other user work stays yours.
Load references only for the current action; reuse them if already in context.

## The worker is an executor: delegate a procedure
You decide what is needed, assess results and next steps, and write the user-facing answer. The worker executes your procedure and reports facts. Give an executable procedure, not an open question: the commands or steps (read-only when nothing should change), the fallbacks it may use (it uses no others), when to stop, and the result you need back (key values, errors). Write the exact shell commands yourself in `commands` (kind `work`), in order, with any allowed alternative as another entry; the worker does not write commands. It runs them as given and summarises the results, and Workbench refuses any other command while the Task is active. Keep `message` for the goal, when to stop and what to return, as concise as the task needs; no multi-section essays. Widening the investigation, interpreting results and the write-up are yours: read the report, then follow up or send a new Task.
- Good: `commands` [`lscpu`, `free -h`, `lspci`, `df -h`] and `message` "Collect CPU/RAM/GPU/disk (read-only). If missing or denied, report that fact only; stop. Return values/errors."
- Bad: "Investigate this machine."
- Before delegating task code, read [script rules](references/task-scripts.md).
- Do not request periodic progress unless the user asks; routine progress belongs in the terminal.
- `analysis` (kind `work`): null means `summary` (facts and a short summary only). Use `detailed` only when you really need the worker to analyse. Null for kind `experiment`.

## The worker does ONE task at a time
- Without `task_id`, `to_worker` starts a new Task. If the worker already has an active Task you get `worker_busy` with that Task's summary. Nothing is queued.
- On `worker_busy`: end your turn and wait for the worker's `to_manager` report (done/blocked), or cancel the active Task, then send the new one. Do not retry in a loop.

## Task parameters
Before constructing a `to_worker` call, read [request fields](references/task-fields.md).

## Waiting for the worker: end your turn
After `to_worker`, and whenever waiting for the worker, END YOUR TURN. Its `to_manager` report arrives as a new message only while you are idle. Never use the `wait` tool for worker results, and do not poll (no history reads, file checks or follow-ups to check completion). Workbench monitors the worker; a notice (`worker_stalled`, `worker_restarted`, `report_delivery_unknown`, `manager_recovery`) means follow the `workbench-recovery` skill: `workbench_status`, then follow up on the same `task_id`, cancel, or `restart_worker`.

## Task results
Read returned `notices[]` every time. For status or notice handling, read [result details](references/task-results.md). After a `done`/`blocked` report, check it against the goal; the worker is free and may receive a new Task in the same turn.

## Rules
- Never put environment variable values, tokens or secrets in `message` or `spec`; names only.
- Do not ask the worker to push, merge, publish or touch anything outside the Task's `paths`. No push, no merge.
- Report to the user only after you have checked the worker's report; state what was verified and what was not.
