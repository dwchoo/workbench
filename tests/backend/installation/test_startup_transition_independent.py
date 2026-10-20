"""Independent public-CLI refusal boundaries; expectations recorded before code review.

A real, separately owned child holds the lock. A valid stopped historical record
plus malformed admission must neither contact an endpoint nor create a backend.
These scheduled same-UID fixtures do not claim foreign-user capabilities.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[3]


def reference(pid):
    fields = Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ', 1)[1].split()
    return {'pid': pid, 'start_ticks': int(fields[19])}


class IndependentStartupAdmissionTests(unittest.TestCase):
    def test_malformed_admission_cannot_authorize_a_held_unknown_owner(self):
        with tempfile.TemporaryDirectory(prefix='wi31-admission-') as temp:
            root = Path(temp)
            data, project, home, runtime = (root / n for n in ('state', 'project', 'home', 'runtime'))
            for path in (data, project, home, runtime):
                path.mkdir(mode=0o700)
            env = {'HOME': str(home), 'XDG_RUNTIME_DIR': str(runtime),
                   'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(REPO / 'src'),
                   'PYTHONDONTWRITEBYTECODE': '1', 'LANG': 'C.UTF-8'}
            for key in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'TMPDIR'):
                path = root / key
                path.mkdir(mode=0o700)
                env[key] = str(path)
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            old = subprocess.Popen([sys.executable, '-c', 'input()'], env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            old_ref = reference(old.pid)
            old.stdin.write(b'\n'); old.stdin.close(); old.wait(timeout=3)
            previous = {'data_dir': str(data), 'project_dir': str(project), 'runtime_root': str(data),
                        'ui_socket': str(data / 'ui.sock'), 'bridge_socket': str(data / 'bridge.sock'),
                        'uid': os.geteuid(), 'boot_id': boot, 'processes': {'backend': old_ref}}
            record = data / 'backend.json'
            record.write_text(json.dumps(previous)); record.chmod(0o600)
            nonce = 'b6' * 16
            source = ('import fcntl,os,sys,time;'
                      'fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600);'
                      'fcntl.flock(fd,fcntl.LOCK_EX);print("locked",flush=True);time.sleep(60)')
            child = subprocess.Popen([sys.executable, '-c', source, str(data / 'backend.lock'),
                                      '--startup-id', nonce], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            child_ref = reference(child.pid)
            try:
                self.assertEqual(child.stdout.readline().strip(), b'locked')
                lock = (data / 'backend.lock').stat()
                admission = {'schema_version': 1, 'data_dir': str(data), 'project_dir': str(project),
                             'project_id': None, 'runtime_root': str(data), 'uid': os.geteuid(),
                             'boot_id': boot, 'startup_id': nonce, 'backend': child_ref,
                             'frontend': reference(os.getpid()),
                             'lock': {'device': lock.st_dev, 'inode': lock.st_ino},
                             'previous_record_sha256': hashlib.sha256(json.dumps(previous,sort_keys=True,separators=(',', ':')).encode()).hexdigest()}
                cases = {
                    'foreign_uid': {'uid': os.geteuid() + 103},
                    'wrong_root': {'data_dir': str(root / 'elsewhere')},
                    'noncanonical_runtime': {'runtime_root': 'relative-runtime'},
                    'foreign_boot': {'boot_id': 'not-this-boot'},
                    'wrong_nonce': {'startup_id': 'a7' * 16},
                    'unknown_tick': {'backend': dict(child_ref, start_ticks=child_ref['start_ticks'] + 1)},
                    'wrong_lock': {'lock': {'device': lock.st_dev, 'inode': lock.st_ino + 1}},
                    'replaced_prior_record': {'previous_record_sha256': '0' * 64},
                }
                for name, changed in cases.items():
                    with self.subTest(name=name):
                        marker = data / 'backend-starting.json'
                        marker.write_text(json.dumps(dict(admission, **changed))); marker.chmod(0o600)
                        def state():
                            return {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                                    for p in data.iterdir() if p.is_file()}
                        before = state()
                        for command in (('status', '--json'), ('start', '--no-attach', '--timeout', '.2')):
                            result = subprocess.run([sys.executable, '-m', 'workbench', *command,
                                                     '--data-dir', str(data)], env=env, cwd=project,
                                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=4)
                            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                            self.assertIn('identity', result.stderr)
                            self.assertNotIn('Traceback', result.stderr)
                            self.assertEqual(reference(child.pid), child_ref)
                            self.assertEqual(state(), before)
                            self.assertFalse((data / 'ui.sock').exists())
                            self.assertFalse((data / 'backend.log').exists())
            finally:
                # Do not poll/wait before the last signal: the direct child PID is
                # still reserved even if it exited; reap only after cleanup.
                os.kill(child.pid, signal.SIGTERM)
                child.wait(timeout=3)
                child.stdout.close()


if __name__ == '__main__':
    unittest.main()
