# Deep modules and test seams

Use this shared design vocabulary in test planning and architecture discovery. Use the
project's glossary for domain names; an architectural label does not rename a domain concept.

| Term | Meaning |
|---|---|
| Module | Something with an interface and an implementation: a function, class, package, or larger slice |
| Interface | Everything callers must know: inputs/results, invariants, ordering, errors, configuration and relevant performance |
| Implementation | What the module hides behind that interface |
| Depth | Leverage at the interface: behavior available per amount a caller must learn |
| Seam | Where an interface permits behavior to vary without editing callers there |
| Adapter | A concrete implementation of an interface at a seam |
| Leverage | Capability paid back across callers and tests by learning one interface |
| Locality | Change, bugs, knowledge and verification concentrating in one place |

Prefer **module**, **interface**, **seam**, **adapter**, **depth**, **leverage** and **locality**
when describing architecture. Do not replace these precise terms with overloaded labels
such as component, service or boundary. Depth is not implementation line count.

A deep module hides substantial behavior behind a small interface. A shallow module asks
callers to learn nearly as much as its implementation does. Ask whether fewer operations,
simpler inputs or more hidden complexity would improve leverage.

Apply the **deletion test**: imagine removing the module. If complexity disappears, it may
be a pass-through worth removing. If complexity spreads back across callers, the module
was concentrating it and earning its keep. A candidate must show observed friction and a
credible concentration of responsibility, not merely a small file count.

The interface is the test surface: callers and behavior tests cross the same seam.
Internally a deep module may contain small parts or internal seams; do not expose them to
callers solely so tests can reach inside. Accept dependencies rather than creating them
inside, return useful results when appropriate, and keep the public surface small.

## Dependency strategy

| Dependency | Deepening/test approach |
|---|---|
| In-process | Concentrate behavior and test it through the module's interface; no adapter needed |
| Local-substitutable | Exercise a real local stand-in inside the tests; keep that seam internal |
| Remote but owned | Put a port at the seam; production transport and in-memory test adapters satisfy it |
| True external | Inject the external dependency and use a focused mock adapter in tests |

One adapter is a hypothetical seam; two justified adapters make variation real. Do not
add a port for a future dependency that does not exist. When approved deepening replaces
shallow modules, replace obsolete tests with preserved behavior coverage at the deepened
interface; do not simply stack another test layer or delete meaningful coverage first.
