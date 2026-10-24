# Issue tracker: docs/features (local markdown)

Work for this repo is tracked as markdown under `docs/features/`, not GitHub Issues.
Repo procedures in `.agents/skills/` (refine-spec, to-tickets, implement) own the format;
follow their templates over this plugin's defaults.

## Conventions

- One feature per directory: `docs/features/<feature-slug>/`
- Requirements: `BRIEF.md`; contract: `SPEC.md`; schedule/deps/scope: `PLAN.json`; verification: `VERIFICATION.md`
- Tickets: one file each at `docs/features/<feature-slug>/tickets/<PREFIX>-NN.md`
  (e.g. `CS-01.md`), using `.agents/skills/to-tickets/references/ticket-template.md`
- Execution state of tickets lives in `PLAN.json` and Root's `.workflow/` records only.
  Do not add status lines to existing tickets.
- Incoming requests/bugs not yet tied to a feature: `docs/issues/<YYYY-MM-DD>-<slug>.md`,
  with a `Triage:` line near the top (see `triage-labels.md`) and conversation under `## Comments`.

## When a skill says "publish to the issue tracker"

New untriaged item → `docs/issues/`. Spec or tickets for an agreed feature → `docs/features/<feature-slug>/`.

## When a skill says "fetch the relevant ticket"

Read the file at the referenced path or ticket ID (`grep -l` under `docs/features/*/tickets/`).

## Wayfinding operations

Used by `/wayfinder`.

- **Map**: `docs/features/<effort>/wayfinder/map.md` (the Notes / Decisions-so-far / Fog body).
- **Child ticket**: `docs/features/<effort>/wayfinder/NN-<slug>.md`, numbered from `01`, with the question in the body. A `Type:` line records the ticket type (`research`/`prototype`/`grilling`/`task`); a `Status:` line records `claimed`/`resolved`.
- **Blocking**: a `Blocked by: NN, NN` line near the top. A ticket is unblocked when every file it lists is `resolved`.
- **Frontier**: scan `docs/features/<effort>/wayfinder/` for child files that are open, unblocked, and unclaimed; first by number wins.
- **Claim**: set `Status: claimed` and save before any work.
- **Resolve**: append the answer under an `## Answer` heading, set `Status: resolved`, then append a context pointer (gist + link) to the map's Decisions-so-far in `map.md`.
