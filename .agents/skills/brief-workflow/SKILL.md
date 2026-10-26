---
name: brief-workflow
description: "User-invoked development briefing: all tickets, current stage, remaining work, issues and explained decision options. Use only when the user explicitly invokes brief-workflow; never automatically or as an agent's internal procedure."
---

# Brief workflow

Run only on an explicit user invocation of `$brief-workflow` (or an equivalent explicit
selection of this skill). A generic status question, an agent's suggestion, a quoted command
or another skill's procedure is not invocation. Do not delegate this briefing or start it
automatically during development. Keep ordinary short progress reports unchanged.

`$brief-workflow` alone is the normal invocation. A feature name/path is optional.
Explain the project to a reader who knows neither the project nor its documents. Write in
the user's language unless explicitly requested otherwise, with short tables and enough context to decide.
Read [the fixed output template](references/briefing-format.md) on every invocation.
Preserve its five sections, field labels, table columns and empty-state rows. Do not
reconstruct or abbreviate the format from memory.

## Read-only scope in main and side chats

Read `.codex/workflow-contract.md` sections 2, 3, 4 and 9 as needed for artifact locations,
authority, dependencies and status. Use existing records and available conversation only.
The briefing does not execute its recommendations: no code/document/state writes, tests,
approval changes, worker/Oracle calls, implementation resumption or messages to other chats.
Do not run another lifecycle skill. Read-only inspection of files and existing helper
records is allowed; it does not take ownership of a running workflow.

In a side chat, do not assume access to the main chat, unsaved work, live actor state or
automatic answer delivery. Say what is visible and its observation time. Offer a copyable
decision message for the owning chat when useful. The owning session must reconcile a
decision with current authority, revision and affected candidates before applying it.
Preserve already-granted authority; briefing or moving chats does not require reapproval.

## Resolve the target without requiring a path

1. Use an explicit target if supplied; otherwise the feature clearly identified by this
   conversation, then the current project's persistent feature/run references.
2. Respect project layout. Defaults are `docs/features/<feature>/{BRIEF.md,SPEC.md,PLAN.json}`,
   `tickets/`, and `.workflow/<feature>/{state.json,runs/}`. Use existing local plans or
   incident/change records when the project has no ticket bundle; do not create artifacts.
3. If exactly one target is supported, proceed and name it. Do not choose from multiple
   unrelated features merely by newest filename or modification time. Present their plain
   names and ask the user to choose only when context cannot resolve the ambiguity; do
   useful unambiguous inspection first. Do not demand a filesystem path from the user.
4. If records are missing or inaccessible, provide what is supported, explicitly label
   gaps, and ask only for the missing target/context needed. Do not invent tickets or
   treat an unavailable PLAN as an empty, completed project.

## Build an evidence-backed snapshot

Read the relevant brief/spec for purpose and accepted decisions, PLAN for the complete
ticket graph, ticket narratives for deliverables, and Root state/run/issue/check records
for actual progress. Follow existing approval, candidate and check references where needed
to justify status. Read known unresolved decisions even when their tickets claim completion.
Reuse records already read if still current. Do not browse unrelated projects or private
session databases to reconstruct another chat.

Show observation time, available plan/run revision and material access limits. Recorded
running work is not confirmed live work; worker completion is not verified integration.
Distinguish recorded status from evidence-supported status when they disagree. Use the
contract vocabulary internally, translate it into plain language in the report, and mark
stale/missing/conflicting evidence as unknown with the concrete reason. Approval flags,
file presence and old passing tests alone do not establish current completion.

Include every ticket in the selected scope, including completed and final integration
tickets. Group long tables by milestone if useful, without silently omitting rows. List
orphan ticket files separately as not reconciled with PLAN; do not silently add them to
the approved graph. With no tickets yet, say which pre-ticket phase is supported and what
remains before planning; do not manufacture IDs or a percentage. Count each canonical
ticket once; distinguish counts from effort/completion estimates. Show all current parallel
tickets instead of inventing one current ticket. Missing status is unknown, not pending.

For each issue, trace direct and downstream impact through PLAN and actual consumed
artifacts/check requirements. Separate waiting on predecessors from an actual defect or
decision. Partial blocking does not mean the whole project has stopped. When impact cannot
be bounded, state that uncertainty rather than declaring other work safe. If records change
during reading, re-read the affected records once and label any remaining inconsistency;
do not stop writers, lock files or claim an atomic live snapshot.

## Classify and explain issues

Use existing issue IDs; where absent, assign report-local labels and identify them as such.
Preserve accepted/resolved decisions and do not ask them again. Classify unresolved issues:

- **Agent-resolvable / 에이전트 해결 가능:** routine, bounded action within existing scope,
  authority and acceptance. Explain the proposed fix and verification, without asking the
  user to approve ordinary technical work or claiming the briefing performed it. Technical
  difficulty alone does not turn an issue into a user decision.
- **User input useful / 사용자 의견 권장:** a preference could improve the result, but an
  already-authorized baseline allows progress. State that baseline and what can continue
  without an answer. Do not invent a default or treat silence as consent to a scope change.
- **User decision required / 사용자 결정 필수:** an unresolved product/scope/acceptance,
  compatibility, permission or explicit user-limit choice lacks authority. Explain the
  exact boundary and affected work; do not create a new gate based on hypothetical risk.

A missing environment, unavailable tool or unknown record is not necessarily a choice.
Describe the missing input or required action honestly; do not force every blocker into a
fabricated A/B decision. Uncertain classification stays unconfirmed with the reason.

For useful/required user decisions, explain the situation and a concrete consequence before
presenting options. Usually give two or three meaningful alternatives, their user-visible
results and main trade-offs, then a recommendation tied to the stated goal. Label estimates
and assumptions; never invent schedules, costs or confidence figures. If evidence cannot
support a recommendation, state what is missing. Include the effect of deferring and which
work remains independent. Finish with a specific, easy-to-answer choice, not jargon or
"approve?" without context. Follow the host's allowed question channel.

Keep the report self-contained: start with what is being built and for whom, translate
internal terms at first use, and attach evidence links after the explanation. Links support
the answer rather than requiring the user to read documents. Prefer a small concrete example
over implementation detail. Be brief on clear routine issues and spend detail on consequential
decisions; do not compress this requested briefing into the normal twelve-line progress limit.
