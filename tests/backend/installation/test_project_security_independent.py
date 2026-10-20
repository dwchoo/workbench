"""WI-AC-11: public project init must preserve another project's marker.

A syscall-boundary scheduling hook performs the real mkdir, then waits for an
actor subprocess to rename that directory and substitute a real symlink. It
neither mocks publication outcomes nor asserts implementation helper counts.
Same-UID execution proves this fixed interleaving; distinct-UID execution needs
an already capable root test runner and is otherwise explicitly skipped.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from workbench.backend.paths import DataDirError
from workbench.backend.project import initialize_project, resolve_project_context


ACTOR_SCRIPT = """import os, sys
from pathlib import Path
new_state, displaced, existing_state = map(Path, sys.argv[1:])
new_state.rename(displaced)
new_state.symlink_to(existing_state, target_is_directory=True)
print(os.geteuid(), flush=True)
"""


def can_switch_to_other_uid():
    """Only use existing capabilities; never sudo/install/alter host mappings."""
    if os.geteuid() != 0:
        return False
    try:
        status = Path('/proc/self/status').read_text()
        effective = next(line.split()[1] for line in status.splitlines()
                         if line.startswith('CapEff:'))
        if not int(effective, 16) & (1 << 7):  # CAP_SETUID
            return False
        for line in Path('/proc/self/uid_map').read_text().splitlines():
            inside, _, count = map(int, line.split())
            if inside <= 65534 < inside + count:
                return True
    except (OSError, ValueError, StopIteration):
        return False
    return False


class IndependentInitializationSecurityTests(unittest.TestCase):
    def exercise_marker_replacement(self, *, other_uid):
        with tempfile.TemporaryDirectory(prefix='wi-security-regression-') as directory:
            base = Path(directory)
            # The actor may traverse only the scratch container and the chosen
            # shared project; the existing project/state remain user-private.
            base.chmod(0o711)
            project = base / 'chosen-shared-project'
            project.mkdir(mode=0o700)
            project.chmod(0o777)  # Non-sticky write+execute models the reported threat.
            existing_project = base / 'existing-private-project'
            existing_project.mkdir(mode=0o700)
            existing_state = existing_project / '.wb'
            existing_state.mkdir(mode=0o700)
            marker = existing_state / 'project.json'
            original = (json.dumps({
                'schema_version': 1,
                'project_id': 'e416b2fc-68d5-469a-a94e-941661545b90',
                'project_root': str(existing_project),
                'state': 'completed',
            }, sort_keys=True) + '\n').encode()
            marker.write_bytes(original)
            marker.chmod(0o600)
            original_hash = hashlib.sha256(original).hexdigest()
            runtime = base / 'run'
            runtime.mkdir(mode=0o700)
            environment = {'HOME': str(base), 'XDG_RUNTIME_DIR': str(runtime)}
            context = resolve_project_context(project, environment)
            new_state = project / '.wb'
            displaced = project / '.wb-displaced'
            actual_mkdir = os.mkdir
            observed = []

            def mkdir_then_schedule_actor(path, mode=0o777, *, dir_fd=None):
                # Resolve a relative dir-fd syscall lexically before replacement;
                # resolving after replacement would follow the hostile symlink.
                location = Path(os.fsdecode(path))
                if not location.is_absolute():
                    parent = (Path(os.readlink(f'/proc/self/fd/{dir_fd}'))
                              if dir_fd is not None else Path.cwd())
                    location = parent / location
                location = Path(os.path.abspath(location))
                result = actual_mkdir(path, mode, dir_fd=dir_fd)
                if location == new_state:
                    def change_identity():
                        os.setgroups([])
                        os.setgid(65534)
                        os.setuid(65534)
                    actor = subprocess.run(
                        [sys.executable, '-c', ACTOR_SCRIPT, str(new_state),
                         str(displaced), str(existing_state)],
                        preexec_fn=change_identity if other_uid else None,
                        capture_output=True, timeout=5, check=True)
                    observed.append(int(actor.stdout.strip()))
                return result

            caught = None
            with patch('os.mkdir', new=mkdir_then_schedule_actor):
                try:
                    initialize_project(context, confirm=lambda _: True,
                                       interactive=True, environment=environment)
                except DataDirError as error:
                    caught = error

            # Check actual adversary effects first to disambiguate a hook/harness
            # miss from preservation. The marker oracle predates production calls.
            self.assertTrue(observed, 'controlled adversary interleaving did not run')
            self.assertTrue(new_state.is_symlink())
            self.assertEqual(os.readlink(new_state), str(existing_state))
            self.assertTrue(displaced.is_dir())
            self.assertEqual(stat_mode(existing_state), 0o700)
            if other_uid:
                self.assertTrue(all(uid != os.geteuid() for uid in observed))
            else:
                self.assertTrue(all(uid == os.geteuid() for uid in observed))
            self.assertIsNotNone(caught, 'init must refuse the replaced .wb path')
            actual = marker.read_bytes()
            # Preserve bytes even if final discovery correctly refuses the alias.
            self.assertEqual(hashlib.sha256(actual).hexdigest(), original_hash,
                             'init overwrote the existing private project marker')
            self.assertEqual(actual, original)

    def test_replaced_new_state_cannot_overwrite_existing_marker(self):
        self.exercise_marker_replacement(other_uid=False)

    @unittest.skipUnless(can_switch_to_other_uid(),
                         'distinct UID requires existing mapped UID/CAP_SETUID; no sudo or tool installation')
    def test_other_uid_replacement_cannot_overwrite_existing_marker(self):
        self.exercise_marker_replacement(other_uid=True)


def stat_mode(path):
    return path.stat().st_mode & 0o7777


if __name__ == '__main__':
    unittest.main()
