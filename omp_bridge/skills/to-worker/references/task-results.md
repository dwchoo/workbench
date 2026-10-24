# Task result details

## Results
The result returns at once. The worker's outcome arrives later as a `to_manager` report; it is not in the tool result. Statuses and what to do:
- `dispatched`: the Task (or re-run) started; `manager_rule` repeats that the worker does it. End your turn; the report comes as a new message.
- `queued`: your follow-up message reached the same Task. End your turn.
- `cancelled` / `cancel_requested`: the Task is closed / the worker is being told. End your turn and wait for the confirmation before a new Task.
  - Cancelling does not stop a running command. If the response has `host_commands` (each with `command_id`, `kind` `terminal` or `experiment`, `owner`, `state`, `log_path`) or a `host_command_note`, a command is still running (for an experiment `command_id` is the `run_id`; `cancel_requested` with `held_reason` `cancel_waiting_for_host_exit` waits for it). A repeated cancel of an already closed Task (`rejected`, `task_closed`) carries the same `host_commands`. The same list is in `workbench_status` as `task.host_commands`. Stop it only if it should not finish: `stop_command` (`command_id`, `reason`), then read its result (see [stop results](#stop_command-results)). The response never says the command stopped.
- `worker_busy`: see above; end your turn and wait for the report, or cancel.
- `rejected` (with a reason): the call was invalid. `errors`/`detail` name the field and the expected value (for example `spec.execution` must be null for kind `work`); fix exactly that and send once more; do not resend it unchanged.
- `held` (nothing was sent; the reason says why, so do not loop):
  - `held:paused`: the user paused automation. Stop sending, tell the user, wait for their resume.
  - `held:no_active_run` / `held:run_starting` / `held:another_task_active`: the `task_id` has no run in progress yet or the worker is on another Task. Re-check the Task state; for a re-run send `run: true` or start a new Task without `task_id`.
  - `held:retry_limit`: 3 re-runs are used. Report to the user instead of re-running.
  - `held:target_not_connected`, `held:mailbox_unavailable`, `held:journal_unavailable`, `held:metadata_unavailable`: the worker or Workbench is not ready. Tell the user; retry only after they say it is fixed. `held:boot_confirmation_required` (user runs `python -m workbench confirm-boot`), `held:model_hold:worker` (worker model error; lifts on its next clean turn), `held:shutdown_closing`: tell the user once; do not loop.
- A Task can also show `held:<reason>` on its own. `held:host_terminal_busy`: the experiment runs in the user's host terminal only when it is idle (clean prompt, no job, no background or suspended job); the start is retried, so do not work around it and never operate the host terminal yourself. If it stays, ask the user to clear the host shell: take it over (prefix t, then c), finish or kill their jobs, run `wb-handoff` again, hand the shell back to the manager (prefix h), then take it over again (prefix t, c) so it sits at an idle user-owned prompt, where automation starts. `held:host_terminal_busy:worker_terminal_command`: the worker's own `terminal` command still runs in the host terminal. Wait; the run starts after it ends. Do not ask the user to take over or kill it. `held:backend_restarted`: Workbench restarted mid-Task; nothing is replayed. Follow its `backend_restarted` notice (workbench-recovery). `held:worker_busy`, `held:manager_busy`, `held:worker_not_connected`: wait; it is retried. `held:shell_unknown`: the host shell state is unknown; no result was judged; ask the user.
- Each worker `to_manager` message has `payload.report_seq`, a number per Task that starts at 1 and grows with each message. Workbench delivers a Task's worker messages in that order. A missing number is a message that was never sent. If numbers look out of order, trust the higher one as the later report.
- After a `done`/`blocked` report, read it, check it against the goal, then decide: follow-up, re-run, a new Task, or answer the user. The worker is free as soon as the report reached you, so you may send a new `to_worker` in the same turn that reads the report; do not wait for another turn.

## `stop_command` results
`stop_command` waits for the stop (about 25 s) and returns `command_id`, `requested_by` `manager` and a `status`:
- `exited`: it ended (`exit_code`, `signal`, `residual`; `residual.unchecked` lists what Workbench did not check).
- `unknown`: not confirmed ended (`reason`); do not claim it stopped. `host_commands` keeps showing it; check `workbench_status` later, or `stop_command` again (joins the same stop, `repeat: true`).
- `already_exited`, `not_found`, `stale`: nothing was sent (finished, unknown id, or the process changed).
- `refused`: `reason` `previous_backend_command` (use `stop_survivor` with the `survivor_id` from `workbench_status`) or `backend_shutdown`.
- `stopping`: still in progress after the wait; check `workbench_status` later. Do not repeat the call in a loop.
- A stopped worker command ends with `stopped_by` and `stop_signal` (the last signal sent) and the worker should not re-run it.
- Stopping a running experiment (`command_id` = its `run_id`) without a cancel: once Workbench sees the command end, the run closes with `outcome` `stopped` and no analysis, judgment or automatic retry. The Task becomes `finished`, the worker is free, and the Task's `last_result` (in `workbench_status.task`) is `{run_id, outcome: "stopped", stopped_by, exit_code, signal, run_closed: true}`; it is not judged. A `stopped` run is neither success nor failure: tell the user, and re-run (`run: true`) or start a new Task only if they want it.
- If the stop ended `unknown`, the outcome is not set yet; `host_commands` shows the `reason`. When the command is later seen to end, the rules above apply.
- If the command ended on its own before any signal was sent (`already_exited`, no `stopped_by`), nothing was stopped and the run is judged as usual.
- If you cancelled the Task first and then stopped the command, the cancel wins: the Task closes `cancelled` (no `stopped` outcome, no judgment).

## Notices
A `to_worker` result may carry `notices[]`: things that ended a Task while you were not looking. Read them every time and update your picture of the Task; each is shown once.
- `report_outcome_unknown`: the worker's report may not have reached you; it is not resent and the Task is closed. Ask the worker with a new Task or check the result yourself; do not assume success or failure.
- `report_not_delivered`: the report never reached you; the Task is closed. Start a new Task if the work still matters.
- `task_cancelled`: the Task you cancelled is closed; the worker is free. Do not keep waiting for a report.
- `task_not_started`: the Task never started (see `reason`); the worker is free. Fix the cause or tell the user, then send a new Task or `run: true`.
- `run_start_failed`: the experiment run could not start (see `error`); the worker is free. Tell the user or re-run once the cause is fixed.
- `run_judgment_unavailable`: the experiment ran and exited, but the worker's analysis was rejected or never obtained (see `reason`); the result is indeterminate (not success), nothing was resent and the worker is free. Tell the user; re-run (`run: true`) or send a new Task.

Workbench sends one new message when an experiment fails to start (not a worker report): `payload.handoff` is `workbench_notice`, `notice` is `run_start_failed`, with `task_id`, `error` and `message`. Tell the user why it did not start; re-run (`run: true`) only after the cause is fixed. No reply is needed. For rejected or unknown worker analysis of a finished run, Workbench sends one notice with `notice` `run_judgment_unavailable`, `reason`, `judgment` `indeterminate`, `judged` (Workbench's checks) and `not_judged`: tell the user the result is indeterminate and why; it needs no reply.
