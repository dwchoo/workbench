# Task request details

## Fields
Send every field; set each one you do not use to null (not a placeholder value).
- `kind` (required): `experiment` runs a command and judges criteria; `work` is work the worker does with its own tools by following your procedure (commands, code changes).
- When a command result has `log_format: segmented-v1`, its `log_path` is a private source descriptor rather than full output. Use `read_command` or read-only `workbench_logs` `raw` pages by source ID and offset; keep missing or evicted ranges explicit. Legacy results still provide a full `log_path` file.
- `message` (required): what the worker sees; for a new Task, the procedure above. It may be long (8192 characters, sent in full).
- `commands` (kind `work`; else null): the exact commands, as above. A follow-up with `commands` replaces the list; null keeps it. The worker's `done` report carries `commands_run` (each command's exit code, duration, `log_path`) recorded by Workbench.
- `spec` (required for a new Task; null for a follow-up or cancel): `goal`, `paths` (the only paths the worker may change; stay narrow), `instructions` (or null).
- `spec.paths` are repo-relative (to the project root), e.g. `src/parser/` or `work/hello.txt`; an absolute path outside the project is rejected.
- `spec.execution` must be null for kind `work`; for `experiment` it is required: `source`, `commit`, `command` (string or list), `criteria` (`log_contains`, `result_file`, `result_contains`), `environment` (variable NAMES only), `shell` (`bash` or `sh`).
- `criteria`: all three are required (non-empty); Workbench judges success = exit 0 AND `log_contains` in the log AND `result_contains` in `result_file`.
  - `log_contains`: text the command prints (stdout/stderr; the raw log).
  - `result_file`: repo-relative path of a file the command itself writes during this run (e.g. `out/result.txt`); a file the run does not write or leaves unchanged (an input, the script itself) makes the result indeterminate.
  - `result_contains`: text that file must contain after the run.
  - If the user gave only a log condition, do not invent a result file or text: ask which file the run writes, or (with their agreement) make the command write one, e.g. `set -o pipefail; ./exp.sh | tee out/result.txt` with `result_contains` equal to `log_contains`.
- `task_id`: the active Task a follow-up or cancel belongs to (null for a new Task). With it, `to_worker` messages the worker on the same Task (status `queued`).
- `run: true` (with the current `task_id`): re-run the experiment (e.g. a new commit). At most 3 re-runs per Task; after that report to the user instead. Otherwise null.
- `cancel: true` (with `task_id`): cancel the Task; the worker is told and becomes free. Otherwise null. Cancel does not stop a command: a worker `terminal` command or an experiment run keeps running after the Task is cancelled. To stop it, call `stop_command` with its `command_id` (the experiment's `run_id`) and a `reason`; the cancel response lists such commands in `host_commands`.
