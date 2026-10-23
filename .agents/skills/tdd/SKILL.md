---
name: tdd
description: Apply test-driven development to an authorized feature or bug repair through agreed public seams, one failing behavior and minimal implementation at a time.
---

# Test-driven development

TDD is the RED → GREEN loop. This is a methodology reference for the caller's current
workflow, not a new execution lane. Read the applicable `.codex/workflow-contract.md`;
preserve its scope, Root-only delegation, independent verification and review requirements.
A leaf performs its assignment without spawning agents. Loading this skill does not
authorize implementation, bypass an explicit-only entry point or select another model.

Read the relevant glossary/CONTEXT and ADRs. Use domain terms in behavior and test names.

## Agree the test surface

A seam is where the module's public interface is exercised. Test only at agreed seams.
Reuse approved specification/assignment seams without asking again. If none is agreed,
the Root/caller proposes each seam with what it catches and misses, then obtains the user's
confirmation before tests are written. A leaf returns a missing or materially changed seam
to Root. For interface shape or seam placement, consult
[codebase-design.md](references/codebase-design.md).

Choose a fast, meaningful test at that seam; browser E2E is not the required first test.
Use behavior examples and independently derived expected values. Choose agreed important
failures, not every hypothetical combination. A reported gap needs Root's supported-scenario
and consequence judgment before becoming the next RED; retain real rare data/security defects.
Consult
[tests.md](references/tests.md) before and during the loop and
[mocking.md](references/mocking.md) when external or nondeterministic dependencies matter.

## One behavior at a time

1. Write one test for the next agreed behavior. Run it and inspect the failure: **RED**.
   Confirm that the missing or incorrect behavior caused the failure. A missing public
   entry point can be valid; unrelated imports, environment failure and deliberate breakage
   are not behavioral RED. Reuse an existing exact regression failure when suitable.
2. Implement only enough to pass that test. Run it and the relevant fast checks: **GREEN**.
   Do not implement anticipated tests or speculative capabilities.
3. Select the next behavior using what this cycle taught you and repeat. Make the first
   slice exercise the path end to end through its agreed interface.

Never write the full test collection and then the full implementation. That horizontal
split locks in imagined structure. Each test and minimal implementation form a vertical slice.
Refactoring is not a required step in this loop; the caller's review stage owns proposed
cleanup and corrections. An already approved pure refactor uses its existing GREEN safety
net rather than inventing RED or deferring the whole assignment until review.

## Return evidence, not a success label

Use the existing result/check records to link behavior/seam and expected-value source to
the RED command, cause and candidate, minimal implementation delta, and GREEN command and
candidate. Preserve the actual order; a later old-fail/new-pass check shows sensitivity,
not that development was test-first. Missing chronology is unverified. No new per-test
snapshot or helper schema is required.

For documents, an exploratory feasibility probe, or another justified exception, identify
the behavior, why the loop does not fit, the alternative check and remaining uncertainty.
Configuration, migration and performance changes are not blanket exceptions. If the harness
is absent, consider a small in-scope fixture; return a scope or dependency gap to Root.
Stop on inaccessible instructions, unresolved authority or missing role/environment support;
do not hide it with an unapproved model, seam or workflow substitution.
