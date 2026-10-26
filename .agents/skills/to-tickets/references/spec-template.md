# <Feature> — implementation specification

Revision: <revision>; source brief: <path and revision>.
Approval of this document is tracked separately with the actual bundle digest.

## Problem and solution
## Relevant user stories
## Scope and non-goals
## Public interfaces, data contracts and invariants
## State transitions, failures and boundary behavior
Describe required failures in supported flows and actual input sources. Prefer fewer states
or clear rejection within agreed scope over speculative recovery guarantees.
## Implementation decisions
Separate agreed constraints from Sol's permitted technical elaboration. Link ADRs.
## Acceptance criteria
Stable IDs mapped to tickets and observable checks. Do not silently delete criteria.
## Testing decisions
Agreed public seams with one-line catches/misses, behavior and independent expected-value
sources, representative existing tests and exact commands. Plan behavior-by-behavior RED ->
minimal GREEN, independent test assessment and section 7 review scope. Name justified TDD
exceptions and alternative evidence without treating final passing tests as chronology.
## Compatibility, integration and rollout constraints
Production migration/deployment needs separate authorization.
## Open blockers and explicitly delegated choices
## Ticket graph
PLAN.json is canonical; ticket Markdown links to it rather than duplicating dependencies.
