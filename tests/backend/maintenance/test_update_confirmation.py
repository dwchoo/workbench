"""기존 shutdown controller 공개 request/confirm의 token-bound 대상."""
from pathlib import Path
import tempfile
import unittest
import os
import signal
import time

from workbench.backend.launcher import LaunchPlan
from workbench.backend.paths import DataLayout
from workbench.backend.service import Backend
from workbench.backend.ui_server import Held
from workbench.terminal.shell_g2.prototype import ShellChoice
from workbench.backend.panes import ShellPane
from workbench.contracts.v1 import PaneId


class BoundShutdown(unittest.TestCase):
    def test_changed_task_after_displayed_request_does_not_shutdown_new_task(self):
        with tempfile.TemporaryDirectory(prefix='wu02-consent-') as private:
            plan = LaunchPlan(ShellChoice('sh','/bin/sh'), '/not-executed/omp', 'fixture', '/not-executed/bridge')
            backend = Backend(DataLayout(Path(private)), plan, project_dir=private, environment={})
            class TaskState:
                task = {'task_id':'task-a','run_id':'run-a'}
                def task_view(self): return dict(self.task)
            state = TaskState()
            backend.flow = state
            pending = backend.shutdown_request()
            self.assertIn({'kind':'task_run','task_id':'task-a','run_id':'run-a'},pending['active'])
            state.task = {'task_id':'task-b','run_id':'run-b'}
            with self.assertRaises(Held):
                backend.shutdown_confirm(pending['token'])
            self.assertFalse(backend._shutdown_confirmed)
            self.assertEqual(state.task,{'task_id':'task-b','run_id':'run-b'})
            self.assertEqual(backend.hold.view(),[])


class ShellWorkConsent(unittest.TestCase):
    def setUp(self):
        private = tempfile.TemporaryDirectory(prefix='update-shell-')
        self.addCleanup(private.cleanup)
        self.root = Path(private.name)
        choice = ShellChoice('bash', '/usr/bin/bash')
        self.shell = ShellPane(choice, {'HOME':str(self.root),'PATH':'/usr/bin:/bin','TERM':'xterm'})
        self.addCleanup(self.shell.close)
        plan = LaunchPlan(choice, '/not-executed/omp', 'fixture', '/not-executed/bridge')
        self.backend = Backend(DataLayout(self.root/'data'),plan,project_dir=str(self.root),environment={})
        self.backend.shell = self.shell
        self.backend.panes[PaneId.HOST_SHELL] = self.shell
        class Idle:
            def probe(self, *args, **kwargs): return {'idle':True}
        self.backend.bridge = Idle()
        self.wait(lambda: self.shell.state['parent_mode']=='manual_prompt')

    def wait(self, predicate):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            self.shell.pump()
            if predicate():return
            time.sleep(.01)
        self.fail('owned shell boundary did not reach expected state')

    def test_manual_background_process_is_displayed_before_shutdown_consent(self):
        pidfile=self.root/'background.pid'
        self.assertIsNone(self.shell.admit(('sleep 120 & echo $! > '+str(pidfile)+'\r').encode()))
        self.wait(lambda: pidfile.exists() and self.shell.state['parent_mode']=='manual_prompt')
        pid=int(pidfile.read_text())
        ticks=int(Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
        pending=self.backend.shutdown_request()
        jobs=[item for item in pending['active'] if item['kind']=='shell_jobs']
        self.assertTrue(jobs,pending['active'])
        self.assertIn({'pid':pid,'start_ticks':ticks,'process_group':os.getpgid(pid)},jobs[0]['processes'])

    def foreground(self, label):
        path=self.root/(label+'.pid')
        command="/bin/sh -c 'echo $$ > "+str(path)+"; exec sleep 120'\r"
        self.assertIsNone(self.shell.admit(command.encode()))
        self.wait(lambda: path.exists() and os.tcgetpgrp(self.shell._master_fd)==int(path.read_text()))
        return int(path.read_text())

    def test_replaced_manual_foreground_cannot_use_previous_consent(self):
        first=self.foreground('first')
        pending=self.backend.shutdown_request()
        descriptor=os.pidfd_open(first)
        try:signal.pidfd_send_signal(descriptor,signal.SIGTERM)
        finally:os.close(descriptor)
        self.wait(lambda: self.shell.state['parent_mode']=='manual_prompt')
        second=self.foreground('second')
        self.assertNotEqual(first,second)
        with self.assertRaises(Held):self.backend.shutdown_confirm(pending['token'])
        self.assertFalse(self.backend._shutdown_confirmed)
        self.assertEqual(os.tcgetpgrp(self.shell._master_fd),second)
        self.assertEqual(self.backend.hold.view(),[])
