# Progress report to the user

Shared by implement, quick-change and investigate-bug. Write the report in the user's language
unless explicitly requested otherwise, and keep it to about twelve lines. Read state from the ledger summary and
the PLAN/incident records; never estimate. Do not invent a figure you did not read.

## When

Only at events: after a dispatch batch, after an integration, after a verification result or a
rollback, when blocked or a decision is needed, right after a resume, and at the end. Do not
report on a timer or while merely waiting. Do not call `ledger-read` just to report; reuse the
last read and show its as-of time.

## Fields (in order)

1. Phase (contract section 9) and as-of time or candidate revision.
2. Tickets: counts by state (ready/running/candidate_ready/verifying/integrated/blocked/stale)
   and the frontier by name, with the ID in parentheses.
3. Active: current calls as role, ticket and workspace.
4. Verification: per unit, tests / review / security / checks as passed, failed, not_run or unknown.
5. Budget: `limit / used / reserved / available` and the limit origin (user, host, operating)
   copied from the ledger summary with its `ledger-read` time. Unknown is written as unknown,
   never 0.
6. Blockers or needed decisions: canonical reference and affected work; write "none" when absent.
   Distinguish pending decisions from a stopped run; respect host question-channel restrictions.
7. Next: one action, naming the independent work continuing or the actual wait/resume condition.

quick-change and investigate-bug omit the ticket lines and report the change or incident instead.
The final report adds the contract section 9 result, unverified scope and artifact paths.
