# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root: this repo's glossary (a Korean term table). It plays the role the skills call `GLOSSARY.md`.
- **`docs/adr/`**: read ADRs that touch the area you're about to work in.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront.

Do not create `GLOSSARY.md`; add terms to `CONTEXT.md`.

## File structure

Single-context repo:

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-no-external-multiplexer.md
│   ├── 0002-managed-shell-control-wait.md
│   ├── 0003-board-agent-without-sandbox.md
│   ├── 0004-auto-execution-after-manager-review.md
│   ├── 0005-per-project-inbox-store-hub-single-writer.md
│   └── 0006-rest-skill-single-external-contact.md
└── src/
```

ADRs are named `docs/adr/NNNN-<slug>.md`.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0001 (no external multiplexer), but worth reopening because…_
