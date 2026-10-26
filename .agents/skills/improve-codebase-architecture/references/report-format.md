# Architecture report

Create one standalone HTML document in an owned OS temp directory. Resolve the platform's
temp directory (honoring TMPDIR where supported), choose a unique name such as
`architecture-review-<timestamp>-<unique>.html`, and create it without replacing an existing
file. Use UTF-8 and inline CSS/SVG; do not fetch CDN scripts, fonts or other dependencies.
Keep source excerpts and labels escaped. No application code or execution controls belong
in the report. A no-write request receives the same information in the response only.

## Structure

Use a compact header: project, survey scope/date/candidate identity and evidence limits.
Include a visual legend: solid box = module, dashed line = seam, red arrow = leakage,
thick box = deep module. Explain domain concepts using their glossary terms.

Each candidate card contains:

- Short title naming the proposed deepening and the involved files/modules.
- Evidence of present friction, linked to inspected paths/revisions or commands.
- Plain proposed direction, without a detailed unselected interface design.
- Dependency category: in-process, local-substitutable, remote-owned/ports-and-adapters,
  or true-external/mock. State whether the required stand-in/adapter exists.
- Side-by-side **before / after** visuals. Show what moves behind the interface, how the
  seam changes and where complexity concentrates; label the after view as a proposal.
- Benefits explained as locality, leverage and preserved/improved behavioral tests.
- Relevant ADR conflicts and why actual evidence might warrant reopening them; otherwise
  respect the ADR. Include uncertainty and evidence still needed.
- One strength badge: **Strong**, **Worth exploring**, or **Speculative**.

Finish with a top recommendation, its reason and an anchor to the card when justified.
When no candidate is supported, say so with scope/evidence limits; do not fill the report
with theoretical refactors. A speculative badge never means the user agreed to a change.

## Visual and delivery guidance

Choose a diagram that explains the candidate: a call graph, a collapse of small modules,
a cross-section, or interface/implementation mass. Use static SVG or styled HTML boxes and
arrows; vary the form to fit the structure. Keep labels legible and before/after comparable.
Use restrained color, clear spacing and brief prose. “Cleaner” or “easier to maintain”
needs a concrete explanation in the shared design vocabulary.

If a renderer/UI is available, inspect the generated report and repair observed clipping,
unreadable labels or unclear relationships. Do not call a file visually verified because
it exists or a parser accepts it. Report the absolute path and actual observation status;
if opening is unsupported, disclose that and leave the artifact available. Do not claim
a browser opened from an unobserved command result.

End with the candidate-choice prompt only when the user wants further exploration. Keep
`report_ready` distinct from selection, BRIEF agreement and implementation permission.
