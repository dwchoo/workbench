"""HB-11 S8: the TUI Hub list on a pty against real backends (fake OMP), end to end.

Two registered projects, ``alpha`` on and ``beta`` off, a bare ``wb`` outside any project (Hub auto-started by that
``wb`` itself): list text, ``Enter`` on the on row enters the product UI and detach returns to the list with the
backend unchanged, ``Enter`` on the off row asks "켤까요?", ``n`` keeps the list with no backend, ``Enter`` ``y`` powers
on and enters, detach, both rows on (HB-AC-08/10/16); the ``x`` power-off confirmation (HB-AC-09) and the refusal of
an unmatched row (HB-AC-13). Expected values: BRIEF/SPEC sentences, ``/proc``, the snapshot read through
``wb status --json``, never the product's own helpers. Run with ``discover -s tests/integration -p 'live_hub_tui.py'``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hub_final_support import (  # noqa: E402
    DETACH, HubBox, Terminal, alive, snapshot_attached, wait_until)


class TuiCase(unittest.TestCase):
    def setUp(self):
        self.box = HubBox(self, autostart='1')  # the bare `wb` below starts the Hub itself
        self.plain = self.box.folder('plain')   # outside any WB project
        self.alpha, self.alpha_id = self.box.project('alpha')
        self.beta, self.beta_id = self.box.project('beta')
        self.box.register(self.box.entry(self.alpha, self.alpha_id), self.box.entry(self.beta, self.beta_id))

    def enter_and_detach(self, term: Terminal, root: Path, name: str) -> None:
        """Wait for the product UI of ``root`` (the backend sees an attached UI), then detach with Ctrl-] q."""
        def attached() -> bool:
            term.pump(0.1)
            return snapshot_attached(self.box, root) is True

        self.assertTrue(wait_until(attached, 90), f'{name} never attached; screen:\n{term.text()}')
        self.assertNotIn('Workbench Hub', term.text(), 'the list is gone while the product UI runs')
        term.send(DETACH)
        term.wait_for('Workbench Hub', 30)
        self.assertTrue(wait_until(lambda: snapshot_attached(self.box, root) is False, 15), 'detach left the slot')


class ListEnterDetachFlow(TuiCase):
    def test_s8_the_whole_round_trip_between_the_list_and_two_projects(self):
        # alpha is on before the list is opened; beta stays off
        alpha_pid, alpha_ticks = self.box.power_on(self.alpha, self.alpha_id)
        self.assertFalse((self.beta / '.wb' / 'backend.json').exists())
        term = Terminal(self, self.box.default_env(), cwd=self.plain)
        screen = term.wait_for('beta', 40)
        term.until(lambda: '켜짐' in term.text() and '꺼짐' in term.text(), 20)
        screen = term.text()
        for needle in ('Workbench Hub', '이름', str(self.alpha), str(self.beta), 'alpha', 'beta', '켜짐', '꺼짐'):
            self.assertIn(needle, screen)
        self.assertEqual(len(self.box.hubs()), 1, 'the bare wb started exactly one Hub')
        # (1) Enter on the on row (first registered) -> product UI -> detach -> list again, same backend
        term.send(b'\r')
        self.enter_and_detach(term, self.alpha, 'alpha')
        pid, ticks, _record = self.box.backend_ref(self.alpha)
        self.assertEqual((pid, ticks), (alpha_pid, alpha_ticks), 'detach must not restart or replace the backend')
        self.assertTrue(alive(alpha_pid, alpha_ticks))
        self.assertIn('detach', term.text())  # the list says the project keeps running
        # (2) Enter on the off row asks first; anything but y keeps the list and starts nothing
        term.send(b'j')
        term.pump(0.5)
        term.send(b'\r')
        term.wait_for('"beta"을(를) 켤까요? [y/N]', 20)
        term.send(b'n')
        term.wait_gone('켤까요', 15)
        self.assertIn('Workbench Hub', term.text())
        time.sleep(1.0)
        self.assertFalse((self.beta / '.wb' / 'backend.json').exists(), 'declining must not start beta')
        self.assertFalse((self.beta / '.wb' / 'backend.lock').exists(), 'no backend ever locked beta')
        self.assertEqual({pid for pid in self.box.backends()}, {alpha_pid})
        # (3) Enter, y -> beta is started and entered; detach -> list
        term.send(b'\r')
        term.wait_for('"beta"을(를) 켤까요? [y/N]', 20)
        term.send(b'y')
        self.enter_and_detach(term, self.beta, 'beta')
        beta_pid, beta_ticks, _record = self.box.backend_ref(self.beta)
        self.assertTrue(alive(beta_pid, beta_ticks))
        self.assertEqual(self.box.backends(), {alpha_pid: alpha_ticks, beta_pid: beta_ticks})
        self.assertEqual(self.box.lock_holders(self.beta), {beta_pid})
        # (4) the list shows both on (the Hub's polling, <= a few seconds)
        self.assertTrue(term.until(lambda: sum(line.count('켜짐') for line in term.text().splitlines()) >= 2, 20),
                        term.text())
        self.assertNotIn('꺼짐', term.text())
        term.send(b'q')
        self.assertEqual(term.wait_exit(), 0, term.text())
        # leaving the list stops nothing: both backends and the Hub live on
        time.sleep(1.0)
        self.assertEqual(self.box.backends(), {alpha_pid: alpha_ticks, beta_pid: beta_ticks})
        self.assertEqual(len(self.box.hubs()), 1)
        self.assertEqual((self.box.backend_ref(self.alpha)[:2]), (alpha_pid, alpha_ticks))


class PowerOffFromTheList(TuiCase):
    def test_x_asks_and_a_no_stops_nothing_then_a_yes_ends_only_that_project(self):
        alpha_pid, alpha_ticks = self.box.power_on(self.alpha, self.alpha_id)
        beta_pid, beta_ticks = self.box.power_on(self.beta, self.beta_id)
        self.box.attach_client = self.box.attach(self.alpha)  # the user's screen, to start the work below
        sleeper = self.box.sleeper(self.alpha, 300)  # active work the confirmation must list
        term = Terminal(self, self.box.default_env(), cwd=self.plain)
        term.wait_for('beta', 40)
        self.assertTrue(term.until(lambda: sum(line.count('켜짐') for line in term.text().splitlines()) >= 2, 30),
                        term.text())
        term.send(b'x')  # the first row: alpha
        term.wait_for('[y/N]', 60)
        self.assertIn('끌까요', term.text())
        self.assertIn('shell_jobs', term.text(), 'the confirmation lists the running work')
        self.assertTrue(alive(sleeper))
        term.send(b'n\r')
        term.wait_for('Workbench Hub', 30)
        self.assertTrue(alive(alpha_pid, alpha_ticks) and alive(beta_pid, beta_ticks), 'a no must stop nothing')
        self.assertIsNotNone(self.box.status(self.alpha))
        term.send(b'x')
        term.wait_for('[y/N]', 60)
        term.send(b'y\r')
        self.assertTrue(wait_until(lambda: (term.pump(0.1), not alive(alpha_pid, alpha_ticks))[1], 120),
                        'confirmed power off did not end alpha: ' + term.text())
        term.until(lambda: '꺼짐' in term.text(), 30)
        self.assertTrue(alive(beta_pid, beta_ticks), 'only the chosen project may end')
        self.assertEqual(self.box.lock_holders(self.alpha), set())
        self.assertIsNone(self.box.status(self.alpha))
        term.send(b'q')
        self.assertEqual(term.wait_exit(), 0, term.text())


class MismatchRowIsRefused(TuiCase):
    def test_enter_on_a_missing_or_wrong_id_row_enters_and_starts_nothing(self):
        # HB-AC-13: beta's registered project_id is not the marker's; alpha's folder is gone
        gone = self.box.folder('gone')
        gone_id = str(uuid.uuid4())
        self.box.register(self.box.entry(gone, gone_id), self.box.entry(self.beta, str(uuid.uuid4())))
        shutil.rmtree(gone)
        term = Terminal(self, self.box.default_env(), cwd=self.plain)
        term.wait_for('불일치', 40)
        before = sorted(os.listdir(self.beta / '.wb'))
        for _ in range(2):  # first row: root_missing; second row: project_id_mismatch
            term.send(b'\r')
            term.pump(1.0)
            self.assertIn('Workbench Hub', term.text())
            self.assertNotIn('켤까요', term.text())
            term.send(b'j')
            term.pump(0.5)
        self.assertEqual(self.box.backends(), {})
        self.assertFalse((self.beta / '.wb' / 'backend.json').exists())
        self.assertEqual(sorted(os.listdir(self.beta / '.wb')), before)
        self.assertFalse(gone.exists(), 'a missing root must not be recreated')
        term.send(b'q')
        self.assertEqual(term.wait_exit(), 0, term.text())


if __name__ == '__main__':
    unittest.main()
