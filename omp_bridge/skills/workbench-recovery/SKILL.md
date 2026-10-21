---
name: workbench-recovery
description: Recover a Workbench Task after a Workbench notice (worker_stalled, worker_restarted, report_delivery_unknown, manager_recovery, backend_restarted) - read workbench_status, then follow up, cancel, restart_worker, stop_command or stop_survivor. Agent-only; not for the user.
---

# workbench-recovery (manager)

Workbench watches the worker for you; never poll it yourself. It tells you with a Workbench notice: a user message whose JSON has `workbench_notice` (no `workbench_message_id`; it is not a worker report and needs no reply to the worker).

## Notices
- `worker_stalled`: the worker's Task is open, but after 2 automatic status checks it sent no report and ran no command.
- `worker_restarted`: the worker OMP is a new session (user restart, your `restart_worker`, or a crash). It has no memory of the Task; the Task is still open. This notice is the one cue to continue: one follow-up on the same `task_id` (Workbench re-sends the full Task), or cancel.
- `report_delivery_unknown`: a worker report may not have reached you; it is not re-sent. Its text is in `workbench_status`.
- `worker_terminal_done` (information): a terminal command the previous worker session started has ended (`command_id`, exit, `log_path`); the new worker has not got the Task yet. It is in `commands_run` and in the Task re-sent with your follow-up; never run it again yourself.
- `backend_restarted`: the Workbench backend restarted; you are a new session, the Task is held, nothing was re-sent or re-run. `survivors` are processes the old backend left running; decide per survivor: `stop_survivor` (`survivor_id`, `reason`) or leave it. Only verified ones can be stopped.
- `manager_recovery`: your own OMP session is new. Reports that never reached your old session, or that the worker sent while your OMP was down, arrive as messages (`reports_resent` counts them; wait for them before you cancel the Task); reports with an unknown delivery are only listed.

## Procedure
1. Call `workbench_status` (no arguments, or the notice's `task_id`). It is read-only and answers at once: the Task (message, `commands`, `commands_run` with exit code, duration and `log_path`), the worker (idle/busy, session, restarts with reasons), the host terminal (running command or idle) and reports to you that are pending, deferred, of unknown delivery or `not_sent` (queued but never sent: ask the worker again if it matters), with their text.
2. Decide, once:
   - The work is done or the facts are enough (for example `commands_run` shows every command finished): read the report text there and answer the user. If the Task is still open, cancel it (`to_worker` with `task_id`, `cancel: true`).
   - The worker is idle but has not finished, or it is a new session: send a follow-up `to_worker` on the same `task_id` that says what to do next (continue with the remaining commands, or clarify). A new worker session gets the full Task and the commands already run from Workbench.
   - The worker is stuck mid-turn or does not respond (busy for a long time with no terminal command running, or your follow-up got no answer): call `restart_worker` with a short `reason` and end your turn; when the `worker_restarted` notice arrives, send exactly one follow-up on the same `task_id` (or cancel). Never send a second follow-up for the same restart.
   - The Task no longer matters: cancel it.
   - A command it started (or your experiment run) runs on and should not: `stop_command` with its `command_id` (the experiment's `run_id`) and a `reason`. Cancelling the Task or restarting the worker does not stop a command. Take the id from `workbench_status` (`task.host_commands`: `command_id`, `kind`, `owner`, `state`, `log_path`, `stop`) or a cancel response (`host_commands`, `host_command_note`). The call waits for the final result: `exited` (done; `residual.unchecked` lists what was not checked), `unknown` (not confirmed; tell the user, do not claim it stopped), `already_exited`, `not_found`, `stale` (nothing was sent), `refused` (`previous_backend_command`: use `stop_survivor`), or `stopping` (still in progress; check `workbench_status` later, do not loop). A stopped command shows `stopped_by` and is not a failure to retry.
3. Tell the user briefly what happened and what you did, then end your turn and wait for the worker's report.

## Rules
- Never run the worker's commands yourself and never operate the host terminal; a command the worker started keeps running through a restart.
- Cancel is not stop: after a cancel, `host_commands` may still list a running command; stop it with `stop_command` only if it should not finish. Stop only commands listed there; use `stop_survivor` for a previous backend's leftovers.
- `restart_worker` is refused while another restart runs (`restart_in_progress`); do not retry in a loop.
- Do not re-send a report's content to the worker; do not invent results the status does not show.
