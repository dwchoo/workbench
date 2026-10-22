---
name: workbench-recovery
description: Recover a Workbench Task after a notice (worker_stalled, worker_restarted, report_delivery_unknown, manager_recovery, backend_restarted) - read workbench_status, then follow up, cancel, restart_worker, stop_command or stop_survivor. Agent-only; not for the user.
---

# workbench-recovery (manager)

Workbench watches the worker; never poll it yourself. It tells you with a Workbench notice: a user message whose JSON has `workbench_notice` (no `workbench_message_id`; not a worker report).

## Notices
- `worker_stalled`: after 2 automatic status checks the open Task got no report and no command.
- `worker_restarted`: the worker OMP is a new session (user restart, `restart_worker`, or a crash) with no memory of the open Task. It is the one cue to continue: one follow-up on the same `task_id` (Workbench re-sends the Task), or cancel.
- `report_delivery_unknown`: a worker report may not have reached you; it is not re-sent (its text is in `workbench_status`).
- `worker_terminal_done` (information): a command the previous worker session started has ended (`command_id`, exit, `log_path`); it is in `commands_run`; never run it again yourself.
- `backend_restarted`: the Task is held, nothing was re-sent or re-run. `survivors` are processes the old backend left running; per survivor: `stop_survivor` (`survivor_id`, `reason`) or leave it.
- `manager_recovery`: your own OMP session is new. Missed reports come as messages (`reports_resent` counts them; wait for them before you cancel the Task); reports with an unknown delivery are only listed.

## Procedure
1. Call `workbench_status` (no arguments, or the notice's `task_id`). It is read-only and answers at once: the Task (`commands`, `commands_run` with exit code and `log_path`), the worker (idle/busy, restarts), the host terminal (running command or idle) and reports to you that are pending, deferred, of unknown delivery or `not_sent` (never sent: ask the worker again if needed), with their text.
2. Decide, once:
   - The work is done or the facts are enough (for example `commands_run` shows every command finished): answer the user from the report text; cancel the Task if still open (`to_worker` with `task_id`, `cancel: true`).
   - The worker is idle but has not finished, or it is a new session: send a follow-up `to_worker` on the same `task_id` that says what to do next (a new session gets the full Task and commands already run).
   - The worker is stuck or silent (busy long with no terminal command running, or your follow-up got no answer): call `restart_worker` with a short `reason` and end your turn; on the `worker_restarted` notice send exactly one follow-up on the same `task_id` (or cancel).
   - The Task no longer matters: cancel it.
   - A command or experiment run runs on and should not: `stop_command` with its `command_id` (an experiment's `run_id`) and a `reason`. Take the id from `workbench_status` `task.host_commands` or a cancel response's `host_commands`; results are in the to-worker skill's task-results reference. `unknown`/`stale`: not confirmed; tell the user, never claim it stopped. `stopping`: check `workbench_status` later; do not loop. `refused` with `previous_backend_command`: use `stop_survivor`. A stopped command or run is not a failure to retry.
3. Tell the user briefly what happened and what you did, then end your turn and wait for the report.

## Rules
- Never run the worker's commands yourself or operate the host terminal; a worker command keeps running through a restart.
- Cancel is not stop, nor is `restart_worker`: after a cancel, `host_commands` may still list a running command; `stop_command` it only if it should not finish.
- `restart_worker` is refused while another restart runs (`restart_in_progress`); do not loop.
- Do not re-send a report's content to the worker or invent results the status does not show.
