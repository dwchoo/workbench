# Doubles at system seams

Use doubles for external or nondeterministic dependencies: third-party APIs, time,
randomness, and sometimes database or filesystem effects. Prefer an owned disposable
database/filesystem or a suitable local stand-in when that gives a useful fast check.
Do not mock your own in-process modules merely to prove they call each other.

Accept an external dependency at a seam instead of constructing a concrete client deep
inside the behavior. An operation-specific interface makes the test's assumption visible:

```python
def process_payment(order, payment_client):
    return payment_client.charge(order.total)
```

A test adapter for `charge` returns one agreed result. Prefer a small SDK-style interface
such as `get_user`, `get_orders`, `create_order` over a generic `fetch(path, options)` whose
double must reproduce routing and conditionals. Avoid building a second implementation
inside the double.

Use [codebase-design.md](codebase-design.md) to justify adapters and seam placement.
Assert observable behavior at the agreed interface, not internal call counts/order.
Where an external interaction itself is the contract, state that contract and isolate
only that dependency. Record the actual system integration left untested.
