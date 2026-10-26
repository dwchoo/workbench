# Fowler smell baseline

Use these as labelled heuristics when reviewing the changed code, not universal rules or
automatic failures. Applicable repository standards take precedence. Skip properties that
existing deterministic tooling enforces. Cite the actual file/hunk, explain observed cost,
and distinguish a possible smell from a documented violation. Do not manufacture a
refactor to satisfy a label; retain useful adapters and modules that concentrate complexity.

| Possible smell | What to look for | Direction to consider |
|---|---|---|
| Mysterious Name | A name conceals what the value or behavior means | Use the domain's honest name; unresolved naming may expose a design question |
| Duplicated Code | The same meaningful logic shape recurs | Share the behavior when the reasons to change are the same |
| Feature Envy | Behavior repeatedly reaches into another module's data | Place behavior with the knowledge it needs |
| Data Clumps | The same related fields travel together | Represent the actual domain concept together |
| Primitive Obsession | A primitive/string carries a richer domain concept | Consider a small concept with its invariant |
| Repeated Switches | The same dispatch cascade is repeated | Consolidate the decision or use justified polymorphism |
| Shotgun Surgery | One behavioral change scatters across callers/files | Concentrate the knowledge behind one interface |
| Divergent Change | One module changes for unrelated reasons | Separate responsibilities with different change drivers |
| Speculative Generality | Hooks, options or abstraction serve imagined requirements | Remove unsupported variation until a real need exists |
| Message Chains | Callers navigate internal objects across a long chain | Expose the behavior callers need at the appropriate interface |
| Middle Man | A module contributes only pass-through indirection | Apply the deletion test before removing it |
| Refused Bequest | An implementation rejects most inherited obligations | Reconsider the interface/inheritance; composition may fit better |

This baseline adapts the code-review source's fixed selection of Fowler's *Refactoring*,
chapter 3. It is a review aid, not a substitute for the specification, architectural
evidence or tests. Root judges proposed corrections; the worker implements accepted changes
within scope and verifies preserved behavior. Refactor is not a mandatory TDD loop step.
