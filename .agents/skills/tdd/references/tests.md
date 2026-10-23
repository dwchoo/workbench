# Tests worth keeping

Test behavior through the agreed public interface. A good test reads as a specification
and still passes when the implementation is reorganized without changing behavior.
Use real in-process modules together; consult [mocking.md](mocking.md) for external seams.

```python
def test_created_user_can_be_retrieved():
    user = create_user(name="Alice")
    assert get_user(user.id).name == "Alice"
```

This observes the capability callers use. Reading a database row behind `create_user`,
asserting an internal collaborator's call count, or testing a private method reaches past
that interface. If tests break when only internal structure changes, examine that coupling.

Keep one logical behavior per test, with a name explaining the observable outcome.
Cover relevant failure behavior through the same interface. Choose critical paths and
complex behavior within the agreed seams rather than manufacturing every possible case.

Expected values need an independent source: an approved example, a worked result, or a
known literal. Do not repeat the production calculation in the assertion or derive the
expected snapshot using the same rule you intend to test.

```python
def test_total_includes_each_line_item():
    assert calculate_total([{"price": 10}, {"price": 5}]) == 15
```

Here `15` is the worked example. Computing `expected` with the same sum/reduce used by
`calculate_total` would duplicate its reasoning and could duplicate its mistake.

Do not make implementation shape the specification. A payment test should assert the
agreed checkout result or externally observable payment contract, not require an internal
`payment_service.process()` call. State which real integrations the chosen test does not
exercise; a useful fixture is not evidence that an unexecuted external integration works.
