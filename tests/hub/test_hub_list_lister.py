"""``workbench.hub.status.Lister`` scheduling with an injected observation hook (HB-05 follow-up).

The Hub's ``list`` handler is ``Lister.list``; these tests drive it in an event loop of
their own with ``status.observe_entry`` replaced, so an observation can block forever
(a hung mount) or be slow on purpose. Nothing here starts a backend or a Hub, and no
runtime directory is created. Expected values are the hook's own call counts and rows.
Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from workbench.hub import status  # noqa: E402
from workbench.hub.paths import HubLayout  # noqa: E402

IDS = ['00000000-0000-4000-8000-%012d' % number for number in range(1, 20)]


def write_registry(state: Path, names: list[str]) -> None:
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    projects = [{'root': f'/nonexistent/{name}', 'project_id': IDS[index], 'name': name,
                 'registered_at': 1.0, 'source': 'add'} for index, name in enumerate(names)]
    (state / 'registry.json').write_text(json.dumps({'schema_version': 1, 'projects': projects}))
    os.chmod(state / 'registry.json', 0o600)


def observation_threads() -> int:
    return sum(1 for thread in threading.enumerate() if thread.name.startswith('hub-observe'))


class Hook:
    """Replaces ``status.observe_entry``: ``stuck`` names block until released, others answer."""

    def __init__(self, case: unittest.TestCase, *, stuck=(), slow=(), delay: float = 0.0):
        self.stuck, self.slow, self.delay = set(stuck), set(slow), delay
        self.release = threading.Event()
        self.calls: dict[str, int] = {}
        self.lock = threading.Lock()
        original = status.observe_entry
        status.observe_entry = self
        case.addCleanup(setattr, status, 'observe_entry', original)
        case.addCleanup(self.release.set)

    def __call__(self, entry, timeout):
        with self.lock:
            self.calls[entry.name] = self.calls.get(entry.name, 0) + 1
        if entry.name in self.stuck:
            self.release.wait()
        if entry.name in self.slow:
            time.sleep(self.delay)
        return status._row(entry, 'on')


class ListerCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='wb-lister-')
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / 'hub'
        self.layout = HubLayout(self.state)

    def run_loop(self, coroutine, timeout: float = 15.0):
        async def bounded():
            return await asyncio.wait_for(coroutine, timeout)
        return asyncio.run(bounded())

    def lister(self, **options) -> status.Lister:
        lister = status.Lister(self.layout, **options)
        self.addCleanup(lister.close)
        return lister

    @staticmethod
    def powers(result: dict) -> dict:
        return {row['name']: (row['power'], row['power_detail']) for row in result['rows']}


class StuckObservationTests(ListerCase):
    def test_a_forever_blocked_entry_never_takes_the_last_worker_and_gets_one_thread(self):
        write_registry(self.state, ['stuck', 'good'])
        hook = Hook(self, stuck={'stuck'})
        lister = self.lister(workers=1, timeout=0.2, cache_seconds=0.0)

        async def scenario():
            results = []
            for _ in range(5):
                results.append(await lister.list(fresh=True))
            return results

        for result in self.run_loop(scenario()):
            self.assertEqual(self.powers(result), {'stuck': ('unknown', 'timeout'), 'good': ('on', None)})
        self.assertEqual(hook.calls['stuck'], 1)
        self.assertEqual(hook.calls['good'], 5)
        self.assertLessEqual(observation_threads(), 2)

    def test_many_blocked_entries_leave_room_for_the_others(self):
        names = [f'stuck{number}' for number in range(4)] + ['good']
        write_registry(self.state, names)
        hook = Hook(self, stuck={name for name in names if name.startswith('stuck')})
        lister = self.lister(workers=2, timeout=0.2, cache_seconds=0.0)

        async def scenario():
            return [await lister.list() for _ in range(3)]

        for result in self.run_loop(scenario()):
            self.assertEqual(self.powers(result)['good'], ('on', None))
            self.assertEqual({self.powers(result)[name] for name in names[:-1]}, {('unknown', 'timeout')})
        self.assertEqual({hook.calls[name] for name in names[:-1]}, {1})

    def test_a_process_with_a_blocked_observation_exits_promptly_after_close(self):
        write_registry(self.state, ['stuck'])
        script = textwrap.dedent('''
            import asyncio, sys, threading
            from workbench.hub import status
            from workbench.hub.paths import HubLayout
            status.observe_entry = lambda entry, timeout: threading.Event().wait()
            async def main():
                lister = status.Lister(HubLayout(__import__("pathlib").Path(sys.argv[1])), timeout=0.2)
                print(lister and (await lister.list())["rows"][0]["power_detail"], flush=True)
                lister.close()
            asyncio.run(main())
        ''')
        env = {**os.environ, 'PYTHONPATH': str(REPO / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        process = subprocess.Popen([sys.executable, '-c', script, str(self.state)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            self.fail('a blocked observation thread kept the process alive after close()')
        self.assertEqual((process.returncode, out.strip()), (0, 'timeout'), err)


class InflightTests(ListerCase):
    def test_a_cancelled_caller_does_not_pin_its_observation_for_later_lists(self):
        write_registry(self.state, ['slow'])
        hook = Hook(self, stuck={'slow'})
        lister = self.lister(timeout=5.0, cache_seconds=0.0)

        async def scenario():
            caller = asyncio.create_task(lister.list())
            while hook.calls.get('slow') != 1:
                await asyncio.sleep(0.01)
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)
            hook.release.set()
            await asyncio.sleep(0.3)  # the observation completes; its cache is already stale
            return await lister.list()

        result = self.run_loop(scenario())
        self.assertEqual(self.powers(result), {'slow': ('on', None)})
        self.assertEqual(hook.calls['slow'], 2)

    def test_fresh_lists_share_the_observation_in_progress(self):
        write_registry(self.state, ['slow'])
        hook = Hook(self, slow={'slow'}, delay=0.4)
        lister = self.lister(timeout=5.0, cache_seconds=0.0)

        async def scenario():
            return await asyncio.gather(lister.list(fresh=True), lister.list(fresh=True))

        first, second = self.run_loop(scenario())
        self.assertEqual(self.powers(first), {'slow': ('on', None)})
        self.assertEqual(first['rows'], second['rows'])
        self.assertEqual(hook.calls['slow'], 1)


class ListOneTests(ListerCase):
    """``Lister.list_one`` (TI-12): one project's fresh row; the other projects are not observed."""

    def test_only_the_named_project_is_observed_afresh_even_when_cached(self):
        write_registry(self.state, ['alpha', 'beta'])
        hook = Hook(self)
        lister = self.lister()
        self.run_loop(lister.list())  # both cached
        self.assertEqual(hook.calls, {'alpha': 1, 'beta': 1})
        result = self.run_loop(lister.list_one(IDS[1]))
        self.assertEqual([row['name'] for row in result['rows']], ['beta'])
        self.assertEqual(result['registry']['state'], 'ok')
        self.assertEqual(hook.calls, {'alpha': 1, 'beta': 2})

    def test_an_unregistered_project_has_no_row_and_nothing_is_observed(self):
        write_registry(self.state, ['alpha'])
        hook = Hook(self)
        result = self.run_loop(self.lister().list_one(IDS[5]))
        self.assertEqual(result['rows'], [])
        self.assertEqual(hook.calls, {})

    def test_a_stuck_project_does_not_block_the_one_asked_for(self):
        write_registry(self.state, ['alpha', 'beta'])
        Hook(self, stuck=['alpha'])
        result = self.run_loop(self.lister().list_one(IDS[1]))
        self.assertEqual(self.powers(result), {'beta': ('on', None)})

    def test_an_unreadable_registry_is_invalid_not_empty_ok(self):
        self.state.mkdir(parents=True, mode=0o700)
        (self.state / 'registry.json').write_text('not json')
        os.chmod(self.state / 'registry.json', 0o600)
        result = self.run_loop(self.lister().list_one(IDS[0]))
        self.assertEqual((result['registry']['state'], result['rows']), ('invalid', []))


if __name__ == '__main__':
    unittest.main()
