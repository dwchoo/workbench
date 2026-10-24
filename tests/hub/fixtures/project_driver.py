"""Driver for ``workbench.hub.projects`` (HB-07, seam S6): one library call in its own process.

``python -I project_driver.py SPEC_JSON``. The test starts this process so nothing forks inside the
test runner. ``SPEC_JSON`` keys:

- ``op``: ``power_on`` | ``power_off`` | ``pause`` | ``resume`` | ``confirm_boot``;
- ``entry``: the registry entry (``root``, ``project_id``, ``name``);
- ``result``: path that receives the outcome (``Outcome.as_dict()``) as JSON once the call returns;
- ``power_off`` only: ``answers`` (the ``ask`` replies in call order; missing ones are ``false``),
  ``asks`` (path; one JSON line ``{"call": n, "active": [...]}`` per ``ask`` call, written before it
  answers), ``gate`` (path prefix: call ``n`` answers only once ``<gate>.<n>`` exists, ≤ 120 s),
  ``out`` (path of the ``out`` stream).

The environment (HOME, XDG dirs, ``WORKBENCH_OMP``) comes from the test.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))


def main() -> int:
    spec = json.loads(sys.argv[1])
    from workbench.hub import projects
    from workbench.hub.registry import Entry
    item = spec['entry']
    entry = Entry(item['root'], item['project_id'], item.get('name') or Path(item['root']).name, 1.0, 'add')
    answers = list(spec.get('answers') or [])
    calls = 0

    def ask(active):
        nonlocal calls
        calls += 1
        if spec.get('asks'):
            with open(spec['asks'], 'a') as stream:
                stream.write(json.dumps({'call': calls, 'active': active}) + '\n')
        if spec.get('gate'):
            gate = Path(f"{spec['gate']}.{calls}")
            deadline = time.monotonic() + 120
            while not gate.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
        return answers.pop(0) if answers else False

    op = spec['op']
    if op == 'power_on':
        outcome = projects.power_on(entry, attach=False)
    elif op == 'power_off':
        with open(spec.get('out') or os.devnull, 'w') as out:
            outcome = projects.power_off(entry, ask, out)
    else:
        outcome = getattr(projects, op)(entry)
    result = Path(spec['result'])
    temporary = result.with_name(result.name + '.tmp')
    temporary.write_text(json.dumps(outcome.as_dict()))
    temporary.replace(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
