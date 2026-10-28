"""WBX-B12 independent: Board OMP log preservation (archive of OMP's own logs, redaction, bounds, safety).

Expected values come from WBX-B12.md and SPEC s3 "공유 OMP 로그인 완화" (0700 dir / 0600 files, bounded count and
size, no system key or auth content in any log, never blocks a Board start, no store copying) and from the safety
properties of the sandbox (never follow a symlink, never touch foreign-owned files).  ``ContractTests`` check the
literal SPEC wording (``board-agent/logs/`` with the isolation check's whole stderr); ``HardeningTests`` are redaction
forms beyond the SPEC's minimum.  Fake OMP and temp HOME only: the user's ``agent.db`` is never opened.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from test_board_launch import FAKE_OMP, LauncherCase

from workbench.hub.board import launch

MIB = 1 << 20


class ArchiveCase(LauncherCase):
    def setUp(self):
        super().setUp()
        self.home = launch.prepare_board_home(self.box.state, self.box.env)
        self.src = self.home.root / 'logs'
        self.dst = self.box.state / 'board-agent' / 'omp-logs'

    def plant(self, name, data, mtime=None, mode=None):
        self.src.mkdir(parents=True, exist_ok=True)
        path = self.src / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        if mode is not None:
            os.chmod(path, mode)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def archive(self):
        return launch.archive_omp_logs(self.box.state, self.home)

    def kept(self):
        return sorted(p.name for p in self.dst.iterdir()) if self.dst.exists() else []

    def read(self, name):
        return (self.dst / name).read_bytes()


class ModeAndBoundTests(ArchiveCase):
    def test_directory_0700_files_0600_even_when_sources_and_old_dirs_are_wider(self):
        self.dst.mkdir(parents=True)
        os.chmod(self.dst, 0o755)
        (self.dst / 'omp.2026-10-01.1.log').write_text('old wide file\n')
        os.chmod(self.dst / 'omp.2026-10-01.1.log', 0o666)
        self.plant('omp.2026-10-01.1.log', 'fresh\n', mode=0o666, mtime=1_760_000_000)
        self.plant('omp.2026-10-02.2.log', 'other\n', mode=0o644)
        self.archive()
        self.assertEqual(stat.S_IMODE(self.dst.stat().st_mode), 0o700)
        for path in self.dst.iterdir():
            self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o600, path)
            self.assertTrue(stat.S_ISREG(path.lstat().st_mode), path)

    def test_count_is_bounded_newest_stay_and_the_archive_outlives_the_sources(self):
        total = launch.OMP_LOG_KEEP + 7
        for index in range(total):
            self.plant(f'omp.2026-10-01.{100 + index}.log', f'run {index}\n', mtime=1_700_000_000 + index * 10)
        self.archive()
        names = self.kept()
        self.assertEqual(len(names), launch.OMP_LOG_KEEP)
        self.assertIn(f'omp.2026-10-01.{100 + total - 1}.log', names)
        self.assertNotIn('omp.2026-10-01.100.log', names)
        for path in self.src.iterdir():  # OMP pruned its own logs
            path.unlink()
        self.archive()
        self.assertEqual(self.kept(), names)  # nothing lost by a start without sources

    def test_a_start_with_a_new_log_drops_exactly_the_oldest(self):
        for index in range(launch.OMP_LOG_KEEP):
            self.plant(f'omp.2026-10-01.{100 + index}.log', f'run {index}\n', mtime=1_700_000_000 + index * 10)
        self.archive()
        self.plant('omp.2026-10-09.999.log', 'newest\n', mtime=1_700_100_000)
        self.archive()
        names = self.kept()
        self.assertEqual(len(names), launch.OMP_LOG_KEEP)
        self.assertNotIn('omp.2026-10-01.100.log', names)
        self.assertIn('omp.2026-10-01.101.log', names)
        self.assertIn('omp.2026-10-09.999.log', names)

    def test_a_huge_log_keeps_head_and_tail_inside_the_size_bound(self):
        body = b''.join(b'line %08d\n' % number for number in range(400_000))  # ~5.2 MB
        self.plant('omp.2026-10-01.5.log', b'HEAD-MARK\n' + body + b'TAIL-MARK\n')
        self.archive()
        kept = self.read('omp.2026-10-01.5.log')
        self.assertTrue(kept.startswith(b'HEAD-MARK') and kept.endswith(b'TAIL-MARK\n'))
        self.assertLess(len(kept), MIB + 1024)
        self.assertLess(sum(p.stat().st_size for p in self.dst.iterdir()), launch.OMP_LOG_KEEP * (MIB + 1024))

    def test_an_unchanged_log_is_not_rewritten_and_a_changed_one_is(self):
        self.plant('omp.2026-10-01.5.log', 'v1\n', mtime=1_700_000_000)
        self.assertEqual(self.archive(), ['omp.2026-10-01.5.log'])
        self.assertEqual(self.archive(), [])
        self.plant('omp.2026-10-01.5.log', 'v2 longer\n', mtime=1_700_000_500)
        self.assertEqual(self.archive(), ['omp.2026-10-01.5.log'])
        self.assertEqual(self.read('omp.2026-10-01.5.log'), b'v2 longer\n')

    def test_only_omp_log_names_are_taken(self):
        self.plant('notes.txt', 'x')
        self.plant('omp.log.bak', 'x')
        self.plant('omp.2026-10-01.5.log', 'x')
        self.archive()
        self.assertEqual(self.kept(), ['omp.2026-10-01.5.log'])


class SafetyTests(ArchiveCase):
    def test_symlinked_log_file_is_not_followed(self):
        outside = self.box.root / 'outside.txt'
        outside.write_text('OUTSIDE-SECRET\n')
        self.src.mkdir(parents=True)
        (self.src / 'omp.2026-10-01.5.log').symlink_to(outside)
        self.archive()
        self.assertEqual(self.kept(), [])

    def test_symlinked_logs_directory_is_never_followed(self):
        outside = self.box.root / 'elsewhere'
        outside.mkdir()
        (outside / 'omp.2026-10-01.6.log').write_text('OUTSIDE-DIR-CONTENT\n')
        self.home.root.joinpath('logs').symlink_to(outside)
        self.archive()
        self.assertEqual(self.kept(), [], 'a log inside a symlinked logs directory was copied')

    def test_foreign_owned_log_is_skipped(self):
        self.plant('omp.2026-10-01.5.log', 'mine\n')
        self.plant('omp.2026-10-01.6.log', 'theirs\n')

        class Foreign:
            """os, but every file looks owned by somebody else (a non-root test cannot chown)."""

            def __getattr__(self, name):
                return getattr(os, name)

            @staticmethod
            def geteuid():
                return os.geteuid() + 1

        with mock.patch.object(launch, 'os', Foreign()):
            self.archive()
        self.assertEqual(self.kept(), [])

    def test_fifo_and_directory_named_like_a_log_do_not_hang_or_copy(self):
        self.src.mkdir(parents=True)
        os.mkfifo(self.src / 'omp.2026-10-01.7.log')
        (self.src / 'omp.2026-10-01.8.log').mkdir()
        thread = threading.Thread(target=self.archive, daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), 'archive blocked on a FIFO')
        self.assertEqual(self.kept(), [])

    def test_planted_symlink_in_the_archive_is_replaced_not_followed(self):
        outside = self.box.root / 'victim.txt'
        outside.write_text('VICTIM\n')
        self.dst.mkdir(parents=True)
        (self.dst / 'omp.2026-10-01.5.log').symlink_to(outside)
        self.plant('omp.2026-10-01.5.log', 'new content\n')
        self.archive()
        self.assertEqual(outside.read_text(), 'VICTIM\n')

    def test_a_symlinked_archive_directory_does_not_block_a_start_or_write_outside(self):
        outside = self.box.root / 'archive-target'
        outside.mkdir()
        self.dst.symlink_to(outside)
        self.plant('omp.2026-10-01.5.log', 'x\n')
        launched = self.prepare()  # diagnostics must never stop the Board
        self.assertIn('WB_BOARD_MCP_KEY', launched.env)
        self.assertEqual(list(outside.iterdir()), [])

    def test_an_archive_path_that_is_a_file_does_not_block_a_start(self):
        self.dst.write_text('not a directory\n')
        self.plant('omp.2026-10-01.5.log', 'x\n')
        launched = self.prepare()
        self.assertIn('WB_BOARD_MCP_KEY', launched.env)

    def test_a_failing_write_does_not_block_a_start(self):
        self.plant('omp.2026-10-01.5.log', 'x\n')
        real = launch.write_private_bytes

        def full_disk(path, payload, **kwargs):  # only the archive's writes fail; the Board's own files are written
            if Path(path).parent.name == 'omp-logs':
                raise OSError(28, 'No space left on device')
            return real(path, payload, **kwargs)

        with mock.patch.object(launch, 'write_private_bytes', full_disk):
            launched = self.prepare()
        self.assertIn('WB_BOARD_MCP_KEY', launched.env)

    def test_an_unreadable_log_does_not_block_a_start_and_others_are_kept(self):
        path = self.plant('omp.2026-10-01.5.log', 'secret\n', mode=0)
        self.plant('omp.2026-10-01.6.log', 'fine\n')
        launched = self.prepare()
        self.assertIn('WB_BOARD_MCP_KEY', launched.env)
        self.assertEqual(self.kept(), ['omp.2026-10-01.6.log'])
        os.chmod(path, 0o600)

    def test_the_archive_never_touches_the_auth_store(self):
        self.plant('omp.2026-10-01.5.log', 'x\n')
        real_open = open
        opened = []

        def spy(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        with mock.patch('builtins.open', spy):
            self.archive()
        self.assertFalse([name for name in opened if name.endswith('agent.db')], opened)


class RedactionTests(ArchiveCase):
    SECRET = 'S3CR3T-VALUE-0123456789abcdef'

    def redacted(self, text):
        self.plant('omp.2026-10-01.5.log', text, mtime=1_700_000_000 + int(time.monotonic() * 1000) % 100000)
        self.archive()
        return self.read('omp.2026-10-01.5.log').decode()

    def test_current_and_previous_system_keys_never_reach_the_archive(self):
        first = self.prepare().env['WB_BOARD_MCP_KEY']
        second = self.prepare().env['WB_BOARD_MCP_KEY']
        self.assertNotEqual(first, second)
        self.src.mkdir(parents=True, exist_ok=True)
        text = ''.join(f'env WB_BOARD_MCP_KEY={key}\nheaders {{"x-api-key":"{key}"}}\nurl http://h/?k={key}&z=1\n'
                       f'prefix:{key}:suffix\n' for key in (first, second))
        out = self.redacted(text)
        for key in (first, second):
            self.assertNotIn(key, out)
            self.assertNotIn(key.split('_', 2)[2], out)  # the secret part alone
        self.assertIn('suffix', out)

    def test_bearer_and_authorization_forms(self):
        out = self.redacted(f'Authorization: Bearer {self.SECRET}\nauthorization: bearer {self.SECRET}2\n'
                            f'BEARER\n{self.SECRET}3\nheader=Bearer {self.SECRET}4,next\n')
        self.assertNotIn(self.SECRET, out)

    def test_json_token_fields_single_line_nested_pretty_and_multi_line(self):
        text = ('{"access_token":"' + self.SECRET + 'A","refresh_token": "' + self.SECRET + 'B"}\n'
                '{"outer":{"clientSecret":"' + self.SECRET + 'C","api_key":"' + self.SECRET + 'D"}}\n'
                '{\n  "password"\n  :\n  "' + self.SECRET + 'E",\n  "Authorization": "' + self.SECRET + 'F"\n}\n'
                '{"id_token": "' + self.SECRET + 'G", "note": "invalid_grant"}\n')
        out = self.redacted(text)
        self.assertNotIn(self.SECRET, out)
        self.assertIn('invalid_grant', out)  # the diagnosis stays readable

    def test_query_and_env_forms(self):
        out = self.redacted(f'GET /cb?code=1&access_token={self.SECRET}A&state=2\nREFRESH_TOKEN={self.SECRET}B\n'
                            f'client_secret={self.SECRET}C password={self.SECRET}D\nANTHROPIC_API_KEY={self.SECRET}E\n')
        self.assertNotIn(self.SECRET, out)

    def test_sk_style_api_keys(self):
        out = self.redacted('key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv\nkey2 sk-proj_0123456789abcdefghij\n')
        self.assertNotIn('AbCdEfGhIjKlMnOpQrStUv', out)
        self.assertNotIn('0123456789abcdefghij', out)

    def test_a_secret_cut_by_the_head_tail_split_leaves_no_usable_fragment(self):
        # A token that starts just before the tail window: the visible tail begins inside the secret, where no
        # label (Bearer, "token") is in front of it any more.
        secret = 'ZX9Q-' + 'tokenbody0123456789ABCDEFGHIJKLMNOPQR'
        filler = b''.join(b'filler line %07d\n' % number for number in range(160_000))  # ~3 MB
        inserted = b'Authorization: Bearer ' + secret.encode() + b'\n'
        tail_start = len(filler) + len(inserted) - MIB // 2  # where the kept tail window begins in the file
        start = tail_start - len(b'Authorization: Bearer ') - 30  # the window opens 30 characters into the secret
        data = filler[:start] + inserted + filler[start:]
        self.assertTrue(data[tail_start:].startswith(secret.encode()[30:]))  # the premise: the window opens inside it
        self.plant('omp.2026-10-01.5.log', data)
        self.archive()
        kept = self.read('omp.2026-10-01.5.log').decode()
        self.assertFalse(secret[-12:] in kept, 'the last 12 characters of a secret straddling the tail window survived')

    def test_redaction_of_a_dense_hyphen_run_stays_fast(self):
        # catastrophic-backtracking guard: a long run of "a-a-a-..." must not stall a Board start
        code = ('import time\nfrom workbench.hub.board import launch\nstarted = time.monotonic()\n'
                'launch.redact_log(b"a-" * 32768)\nprint(time.monotonic() - started)\n')
        try:
            done = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'PYTHONPATH': os.pathsep.join(sys.path)},
                                  capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            self.fail('redact_log did not finish a 64 KiB hyphenated run within 15 s (quadratic backtracking)')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertLess(float(done.stdout), 5.0)

    def test_ordinary_text_is_untouched(self):
        text = 'oauth refresh failed: invalid_grant (expired)\nrequest id 7f3a-91 took 120 ms\nBearer-less line\n'
        self.assertEqual(launch.redact_log(text.encode()).decode(), text)


class HardeningTests(ArchiveCase):
    """Forms beyond the SPEC minimum that real OAuth logs plausibly contain."""
    SECRET = 'H4RD3N-VALUE-0123456789abcdef'

    def redacted(self, text):
        return launch.redact_log(text.encode()).decode()

    def test_escaped_quote_inside_a_json_secret_leaves_no_tail(self):
        out = self.redacted('{"refresh_token":"abc\\"' + self.SECRET + '","x":1}')
        self.assertNotIn(self.SECRET, out)

    def test_single_quoted_dict_style_fields(self):
        out = self.redacted("{'access_token': '" + self.SECRET + "'}")
        self.assertNotIn(self.SECRET, out)

    def test_yaml_or_header_colon_style_fields(self):
        out = self.redacted('access_token: ' + self.SECRET + '\nX-Api-Key: ' + self.SECRET + 'B\n')
        self.assertNotIn(self.SECRET, out)

    def test_basic_authorization_header(self):
        out = self.redacted('Authorization: Basic ' + self.SECRET + '==\n')
        self.assertNotIn(self.SECRET, out)

    def test_cookie_headers(self):
        out = self.redacted('Cookie: session=' + self.SECRET + '\nSet-Cookie: sid=' + self.SECRET + 'B; Path=/\n')
        self.assertNotIn(self.SECRET, out)


WRAPPER = '''#!/bin/sh
if [ "$1" != --version ]; then
  head -c 200000 /dev/zero | tr '\\0' E >&2
  echo "TAIL-MARK-STDERR key=$WB_BOARD_MCP_KEY" >&2
fi
exec {python} {fake} "$@"
'''


class ContractTests(LauncherCase):
    """Decided layout (orchestrator, 2026-10-28; replaces SPEC s3's literal ``board-agent/logs/`` with the isolation
    check's whole stderr): one small JSON line per Board agent start (start time, isolation verdict summary) in
    ``board-agent/omp-logs/board-events.log`` (0700 dir, 0600 file, bounded size), no system key."""

    def events(self) -> Path:
        return self.box.state / 'board-agent' / 'omp-logs' / 'board-events.log'

    def start(self):
        self.box.omp.write_text(WRAPPER.format(python=sys.executable, fake=FAKE_OMP))
        self.launcher = self.make_launcher(self.config)
        started = self.prepare()
        verdict = self.arun(self.launcher.check_agent(started))
        return started, verdict

    def rows(self) -> list[dict]:
        return [json.loads(line) for line in self.events().read_text().splitlines() if line.strip()]

    def test_each_start_records_its_time_and_isolation_verdict_without_the_key(self):
        started, verdict = self.start()
        self.assertIsNone(verdict)
        events = self.events()
        self.assertTrue(events.is_file(), 'board-agent/omp-logs/board-events.log does not exist after a Board start')
        self.assertEqual(stat.S_IMODE(events.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(events.lstat().st_mode), 0o600)
        row = self.rows()[-1]
        self.assertEqual((row['event'], row['isolation']), ('start', 'ok'))
        self.assertIn('at', row)
        text = events.read_text()
        key = started.env['WB_BOARD_MCP_KEY']
        self.assertNotIn(key, text)
        self.assertNotIn(key.split('_', 2)[2], text)

    def test_the_event_log_stays_bounded(self):
        for _ in range(12):
            self.start()
        rows = self.rows()
        self.assertEqual([row['event'] for row in rows], ['start'] * 12)
        self.assertLessEqual(self.events().stat().st_size, 256 << 10)


if __name__ == '__main__':
    unittest.main()
