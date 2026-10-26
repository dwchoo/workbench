# Shared discovery method

Use within refine-spec or the selected-candidate phase of improve-codebase-architecture.
The calling skill owns entry, state and allowed writes; this reference does not start a
workflow, change models or grant implementation authority.

## Explore facts, ask for decisions

Read applicable instructions, glossary/CONTEXT-MAP, ADRs, current feature artifacts and
provided evidence. Preserve prior answers and unaffected decisions. Find observable facts
with tools or Root's bounded code_explorer assignment rather than asking the user to
recite code. A leaf requests exploration through Root and never delegates.

Distinguish current behavior from desired behavior. Surface contradictions and investigate
uncertainty; do not manufacture certainty from a proposed answer. Mark pending exploration
as unresolved. Independent questions may proceed while only downstream decisions wait.

## Decision tree and frontier rounds

Map the design as a tree. A round contains only decisions whose prerequisites are settled.
Number each current-frontier question `1.`, `2.`, and so on. Label that question's real
alternatives with lowercase letters `a.`, `b.`, `c.`, each with its main trade-off. State
the recommended letter and reason so acceptance identifies the decision. Show the compact
reply format `1.a, 2.b` and explicitly say the user can answer in their own words instead,
using the host's permitted question channel. Write the questions,
options and recommendation text in the user's language; retain the numeric/letter labels.
Wait for the user's answers before advancing dependent questions. Recompute the frontier
after each round.
Group a large frontier by topic or split it to suit the user; there is no universal
one-question limit. Clarify materially ambiguous answers without narrowing an explicit
broader approval the user actually gave.

Keep the displayed question/option mapping stable while interpreting its answers. Accept
free-form answers, including explanations combined with a choice. A partial reply leaves
unanswered decisions open; it does not accept their recommendations. Use as many real
alternatives as needed, without padding to three or inventing choices for a factual input.

Example format only; these choices are not requirements for the user's project:

```text
1. Where should the document live?
   a. In the repository — changes are versioned with the code.
   b. In an external document tool — sharing outside the repository is easier.
   Recommendation: a — keeps the document and code history together.

2. How should the existing document be handled?
   a. Replace it — useful when its structure no longer fits.
   b. Update it — preserves useful context and existing links.
   Recommendation: b — preserves the current context and links.

Reply example: 1.a, 2.b
You can also answer in your own words instead of using the labels.
```

Use edge cases to clarify supported user flows and important failures, not to invent guarantees.
Before proposing recovery/retry/concurrency support, compare a clear rejection, single owner
or smaller state space that satisfies the user outcome. State only relevant environment and
trust-boundary assumptions in existing scope/non-goals. A possibility alone is not acceptance;
select representative cases with distinct failure causes. Preserve promised behavior and
ask about material scope/compatibility reductions, not routine implementation choices.
Use edge cases to challenge vague or overloaded language against existing definitions.
Cross-check factual statements with code, and make any requirement/implementation gap
explicit. A gap does not silently replace the agreed requirement or reopen unrelated choices.

## Record agreement as it emerges

Follow the project's document locations and requested write scope. As a term is resolved,
update the applicable glossary/CONTEXT immediately; create it lazily. Keep it a domain
glossary, not a feature specification or implementation scratchpad. Link one canonical
definition rather than copying competing versions across artifacts.

Offer an ADR sparingly: the decision is hard to reverse, surprising without its context,
and arose from genuine alternatives. Capture context, choice, rationale, consequences and
rejected alternatives under the project's ADR format. Skip trivial implementation details
and ephemeral priorities. Keep undecided proposals out of current contracts. Under no-write
scope, hold drafts in the response instead of modifying documents.

Maintain the BRIEF with [brief-template.md](brief-template.md): problem/users/outcome,
scope/non-goals, constraints, agreed behavior and acceptance IDs/examples, errors,
important interfaces/seams and testing intent, decisions/reasons, evidence, and open or
deferred work. Link glossary/ADRs. Concrete ownership/scheduling remain planning work.
For material claims distinguish repository observation, user-agreed decision and deferred
question. Give inspected file/command and revision/date when available.

## Agreement and handoff

Present the BRIEF and unresolved/deferred decisions with downstream consequences. Discovery
finishes when the agreed scope's blocking frontier is empty or the user explicitly defers
remaining questions with their consequences. Obtain genuine user confirmation of shared
understanding, preserving any approval already supplied; do not manufacture it or ask again
about unchanged decisions. Record actual approval reference and BRIEF revision in a separate
handoff/state record. An “approved” label alone is not authority.

Return the BRIEF path, decisions, deferred impact and next command:
`$to-tickets <brief-path>` in the user's fresh Sol XHigh/Max session. Do not create that
session or invoke the command. BRIEF approval does not start implementation. Do not install
upstream skills, depend on a vendor-specific Skill tool, or require an issue tracker to
save local documentation. Preserve the calling skill's product/test/commit/publication limits.
