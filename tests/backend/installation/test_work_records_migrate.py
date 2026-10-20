"""Stopped migration adds records without changing the legacy transaction contract."""
import json
import unittest

from tests.backend.installation import test_migration as support
from workbench.storage.work_records.store import RecordStore
from workbench.tasks.repository import TaskRepository


class WorkRecordsMigrationTests(unittest.TestCase):
    def test_stopped_records_and_revisions_migrate_with_original_source_unchanged(self):
        case = support.MigrationTests("test_nonempty_rollback_journal_is_explicitly_unsupported_without_source_writes")
        case.setUp()
        self.addCleanup(case.doCleanups)
        with TaskRepository(case.source / 'tasks.sqlite3') as tasks:
            store = RecordStore(case.source / 'records.sqlite3', task_repository=tasks)
            first = store.create(title='이관 전 기록', body='# 최초 원문\n"한글" % _ 😀\n',
                                 task_id='task-fixed', actor='manager')['record']
            second = store.revise(first['record_id'], expected_revision=1, title='정정 기록',
                                  body='보존할 최신 원문\n', task_id=None, actor='worker')['record']
        journal = case.source / 'records.sqlite3-journal'
        journal.touch(mode=0o600)
        raw = case.source / 'raw-logs'
        raw.mkdir(mode=0o700)
        (raw / 'existing.log').write_bytes(b'LEGACY_RAW_SENTINEL\n')
        before = support.inventory(case.source)
        result = case.migrate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(support.inventory(case.source), before)
        target = case.project / '.wb'
        reopened = RecordStore(target / 'records.sqlite3')
        self.assertEqual(reopened.get(first['record_id'], 1), {'status': 'ok', 'record': first})
        self.assertEqual(reopened.get(first['record_id']), {'status': 'ok', 'record': second})
        self.assertEqual((target / 'raw-logs/existing.log').read_bytes(), b'LEGACY_RAW_SENTINEL\n')
        shown = case.cli('records', 'show', first['record_id'], '--revision', '1', '--json')
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        self.assertEqual(json.loads(shown.stdout)['record'], first)
        manifest = json.loads((target / 'migration-manifest.json').read_text())
        self.assertEqual(manifest['databases']['records.sqlite3']['schema_version'], 1)
        self.assertEqual(manifest['databases']['records.sqlite3']['logical_rows']['revisions']['rows'], 2)

        # Reuse the same new behavior check for its two important distinct failures.
        # Existing stopped/unknown/target rejection tests remain authoritative below.
        import sqlite3
        for failure in ('schema', 'journal'):
            with self.subTest(failure=failure):
                other = support.MigrationTests('test_nonempty_rollback_journal_is_explicitly_unsupported_without_source_writes')
                other.setUp()
                self.addCleanup(other.doCleanups)
                RecordStore(other.source / 'records.sqlite3').create(title='keep', body='sentinel', actor='manager')
                if failure == 'schema':
                    with sqlite3.connect(other.source / 'records.sqlite3') as db:
                        db.execute('PRAGMA user_version=99')
                else:
                    (other.source / 'records.sqlite3-journal').write_bytes(b'NONEMPTY_JOURNAL_KEEP')
                untouched = support.inventory(other.source)
                refused = other.migrate()
                self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                self.assertIn('records schema is unsupported' if failure == 'schema' else 'rollback journal is unsupported', refused.stderr)
                self.assertEqual(support.inventory(other.source), untouched)
                self.assertFalse((other.project / '.wb/project.json').exists())
