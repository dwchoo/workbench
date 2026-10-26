---
name: improve-codebase-architecture
description: Inspect a user-selected codebase scope for deepening opportunities, present a visual report, and grill one chosen candidate into an agreed brief. Explicit user invocation only.
---

# Improve codebase architecture

Use only on explicit user invocation. Read sections 1–3 of `.codex/workflow-contract.md`
with `python3 ../workflow-ledger/scripts/workflow_tools.py contract --sections 1-3`, relative
to this skill. A delegated leaf returns to Root. Keep the user's selected session/model.
Root alone may assign bounded exploration to an existing code_explorer; otherwise inspect
directly. No product-code or existing-test changes, installation, commits or publication.

## Scope and survey

Honor the named module, subsystem or pain point. Otherwise use recent commit history and
recurring changes to choose useful hot spots before widening the survey. If history is
unavailable, state the limit and use accessible evidence. Read relevant glossary/CONTEXT,
ADRs and instructions first; do not reopen settled decisions without observed friction.

Consult the shared [design vocabulary](../tdd/references/codebase-design.md). Look for
understanding spread across modules, interfaces as complex as implementations, leaking
knowledge across seams, and behavior difficult to test through its interface. Apply the
deletion test; explain how responsibility would concentrate. Name concepts using the
domain glossary and explain benefits through depth, leverage, locality and testability.

## Report before designing

Follow [report-format.md](references/report-format.md). Write a unique self-contained HTML
report in an owned OS temp location, using inline CSS/SVG for before/after visuals. Return
its absolute path. Open or inspect it with available authorized tools when supported;
distinguish file creation, an open attempt and actual visual observation.
For a no-write request, provide the report in the response and create no files.

Show files/evidence, concrete friction, proposed direction, dependency category, testing
effect, ADR conflict, uncertainty and recommendation strength for each candidate. Recommend
a first candidate when evidence supports it. No candidate is also a valid outcome.
Do not design detailed interfaces yet or turn a speculative candidate into a requirement.

Report-only/no-candidate requests finish at `report_ready`. Otherwise reach
`awaiting_selection` and ask which candidate the user wants to explore. Wait for that choice;
the report and recommendation do not choose on the user's behalf.

## Grill the chosen candidate here

After selection, use the shared [discovery method](../refine-spec/references/discovery-method.md)
inside this skill, for one chosen candidate per session. Reuse known answers. Discuss
constraints, dependencies, module/interface shape, seam placement, surviving tests and
compatibility through decision frontiers. Use the shared design vocabulary for these
decisions; do not introduce a separate parallel design process.

As decisions settle, update only the applicable agreed domain documentation under the
project's document policy. Offer a selective ADR for a load-bearing rejection reason that
future reviews need; do not create ADRs for temporary priorities. Under no-write scope,
keep proposed terms/decisions/BRIEF in the response until writing is authorized.

Move through `scoped → surveyed/report_ready → awaiting_selection → grilling → brief_draft
→ brief_agreed`. Produce the BRIEF using
[brief-template.md](../refine-spec/references/brief-template.md), preserve real approval and
revision separately, and give the same handoff as refine-spec: a fresh Sol XHigh/Max session,
`$to-tickets <brief-path>`. Do not restart refine-spec, launch that session, invoke planning
or implement automatically. Candidate selection and BRIEF agreement are not implementation
authorization. If the user ends discovery early, return actual state and unresolved impact.
