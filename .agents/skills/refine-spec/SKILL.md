---
name: refine-spec
description: "Interview and document a repository idea into an approved portable brief, domain glossary and selective ADRs. User-selected Astra session; no implementation or ticket execution."
---

# Refine spec — interview plus domain modeling

This is discovery, inspired by grill-with-docs, not final specification or ticket generation.
Assume the user selected Astra; do not change the model or reroute to a planner.
Print sections 1–3 of `.codex/workflow-contract.md` (`python3 ../workflow-ledger/scripts/workflow_tools.py contract --sections 1-3`, relative to this skill); a delegated leaf must return to Root.

Use [discovery-method.md](references/discovery-method.md) for the decision frontier,
fact-finding, active glossary/ADR work, evidence and agreement. Reuse existing answers;
this entry point does not restart settled discovery. Maintain the brief using
[brief-template.md](references/brief-template.md) in the project's approved document location.

Present the agreed BRIEF and unresolved/deferred impact. Preserve real user approval and
revision separately; agreement is not implementation authorization. Return the BRIEF path,
decision summary and `$to-tickets <brief-path>` for the user's fresh Sol XHigh/Max session.
Do not launch that session or invoke to-tickets. No product/test changes, commits or publication.
