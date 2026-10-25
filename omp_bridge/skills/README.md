# Workbench skills

The installed Workbench asset bundle carries this directory as product skill
assets; it is the only skill source the Workbench-launched OMP processes load
(C-D59). The launcher passes it as `skills.customDirectories` in the
per-role overlay while every ambient skill source (`~/.agents/skills`,
`~/.omp/agent/skills`, project `.omp/skills`, `.claude/skills`, ...) is turned
off by `omp_bridge/omp-isolation.yml`.

Layout: one directory per skill, `<name>/SKILL.md` with `name` and
`description` frontmatter. This README is not a skill.

Keep the `to-worker` and `to-manager` entrypoints within 5,000 characters,
600 whitespace-delimited words and 60 lines, including frontmatter. Existing
skill tests enforce all three limits. Keep essential role boundaries in the
entrypoints; put field tables and conditional procedures in linked references
with explicit read conditions. Read only the reference needed for the current
action; do not preload the whole reference tree or duplicate its instructions.
Reuse details already in context instead of reading them again on every call.

The Workbench worker launch also appends a short reporting policy to OMP's
system prompt. Routine progress stays in the host terminal/logs; Manager reports
are reserved for actionable issues/decisions, explicit status requests and final
Task results. Automatic monitor questions require no healthy acknowledgement.

Role filtering: `ROLE_SKILL_PATTERNS` in `src/workbench/backend/launcher.py`
maps `manager`/`worker` to glob patterns written as `skills.includeSkills` in
that role's overlay. An empty tuple means no role filter (every skill here is
offered to that role). Current skills (C-D65, CW-18 U6): `to-worker` (manager
only; how to use the `to_worker` tool), `to-manager` (worker only; how to use
the `to_manager` tool and answer staged deliveries), and (C-D70 (7))
`workbench-recovery` (manager only, agent-only: what to do on a Workbench
recovery notice with `workbench_status`, a follow-up, cancel or
`restart_worker`). These tool instructions match the schemas in
`omp_bridge/g3/bridge.ts`. `workbench-report` is a separate Manager-only,
user-only skill with its own report contract. `workbench-inbox` (Task Inbox
TI-11, manager only, agent-only) covers the `inbox_request` notice, the
`workbench_inbox` tool and `to_worker` with `request_id`.

OMP 18.11.1 (measured by the invocation probe) has no per-skill
frontmatter that hides a skill from the user's `/skill:` menu while the model
still sees it: `hide`/`disableModelInvocation` hide it from the model, and
`skills.enableSkillCommands: false` drops every skill command. So
`workbench-recovery` is listed in the manager's slash menu; its description says
it is agent-only.

Use `/skill:workbench-report [symptom description]` as a leading Manager user
command to prepare a Korean Markdown report about a Workbench problem. The
skill reads `workbench-report/references/report-template.md` and saves through
`workbench-report/scripts/report_io.py`; its instructions describe the bounded
read-only investigation and sanitized save-failure fallback. Follow-up answers
continue the same report without another slash invocation.

`disableModelInvocation: true` excludes this skill from model discovery while
retaining its user command. The Manager bridge input guard also prevents the
OMP 18.11.1 inline parser from expanding this token in ordinary interactive/RPC
user prose: it forwards the original text and images once with user attribution.
Leading explicit commands retain native handling, as do unrelated skills.
Workbench notices and peer messages use agent attribution and do not expand the
token. Worker neither discovers nor offers the report skill. These observations
are for OMP 18.11.1; other versions require their own runtime verification.

In the installed persistent OMP 18.11.1 Manager session, a long helper JSON
stdout line was truncated inline while the same call returned a readable
artifact pointer. For this helper, use the built-in `read` tool with
`{"path":"artifact://<same-call-returned-id>:raw:1-3000"}` to read only that
pointer, and use the result only after verifying it contains the complete
JSON. Shaking may wrap the current result in a numbered region; select only
the region named by that helper response. Its line-truncation notice may
return the original stdout's nested pointer. The skill permits only that
same-call chain, at most two reads with distinct IDs. The tested range
recovered a 32 KiB Markdown body, including escaping-heavy JSON above 50 KiB;
`raw:1-1` was insufficient. Never discover artifacts or treat a truncated
inline excerpt as complete. Missing, ambiguous or incomplete retrieval leaves
save status unconfirmed. This behavior was measured for the default
persistent session and is not guaranteed for other modes or OMP versions.

The backend start-up isolation check reports any loaded skill that is not in
this directory (after the role filter) as a leak.

`important-log-summary` is an internal SDK-session asset. The host supplies its
exact bytes only to the request-specific restricted `task@Manager` summary
session. It is not added to the ordinary Manager or Worker skill roster. The
sole `db_summary_save` tool and startup checks live in the product adapter;
the skill text itself grants no tool or repair authority.

## Installed assets and project state

The uv tool installation includes this skill tree, its references and helpers
with the Workbench package. A research project does not need this checkout or
a copy of `omp_bridge/skills/`; OMP receives the installed asset path through
its role overlay. The Workbench package provides the immutable skill source,
while each project's `.wb/` stores that project's durable task/log/workflow
state and OMP conversation history.

The project's OMP agent directory may link `agent.db` to the user's existing
OMP authentication store. The link reuses that store in place; Workbench does
not copy credential contents into `.wb/`. Per-project sockets and regenerable
runtime caches live in a private runtime namespace outside `.wb/`.
