"""승인된 읽기 전용 DB/schema 진단의 서로 다른 실패 경계.

공개 cli.main·실제 SQLite/filesystem을 관찰한다. 기존 fixture의 설치
metadata adapter와 private uv/OMP version scripts만 재사용한다.
"""
import json
from pathlib import Path
import sqlite3
import unittest

from tests.backend.maintenance import test_doctor_cli as support


class DoctorIndependent(unittest.TestCase):
    def setUp(self):
        self.fixture = support.DoctorCLI()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.data = self.fixture.project()

    def diagnose(self):
        status, output, error = self.fixture.invoke('--data-dir', str(self.data), '--json')
        self.assertEqual(error, '')
        report = json.loads(output)
        self.assertEqual(report['schema_version'], 1)
        checks = {item['id']: item for item in report['checks']}
        return status, report, checks

    def test_valid_sqlite_with_unsupported_schema_is_error_without_migration(self):
        # Producer: an existing store written by another WB schema version.
        with sqlite3.connect(self.data/'records.sqlite3') as database:
            database.execute('PRAGMA user_version=77')
        database.close()
        before = self.fixture.inventory()
        status, report, checks = self.diagnose()
        self.assertEqual(status, 1, report)
        self.assertEqual(report['overall'], 'error')
        self.assertEqual(checks['db.records']['status'], 'error')
        self.assertEqual(checks['db.tasks']['status'], 'ok')
        self.assertEqual(self.fixture.inventory(), before)

    def test_nonprivate_database_is_error_without_permission_repair(self):
        # Producer: a user changed the existing DB to group/other-readable mode.
        (self.data/'records.sqlite3').chmod(0o644)
        before = self.fixture.inventory()
        status, report, checks = self.diagnose()
        self.assertEqual(status, 1, report)
        self.assertEqual(checks['permissions']['status'], 'ok')
        self.assertEqual(checks['db.records']['status'], 'error')
        self.assertEqual(self.fixture.inventory(), before)

    def test_active_rollback_journal_is_unknown_without_opening_source_sqlite(self):
        # Producer: an actual uncommitted SQLite rollback-mode transaction.
        # The transaction/rollback belongs to fixture setup/cleanup; diagnosis
        # must leave the exact DB family intact while its state is unconfirmed.
        path = self.data/'records.sqlite3'
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        self.addCleanup(writer.rollback)
        self.assertEqual(writer.execute('PRAGMA journal_mode=DELETE').fetchone(), ('delete',))
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('PRAGMA user_version=77')
        journal = Path(str(path)+'-journal')
        self.assertGreater(journal.stat().st_size, 0)
        before = self.fixture.inventory()
        status, report, checks = self.diagnose()
        self.assertEqual(status, 1, report)
        self.assertEqual(report['overall'], 'error')
        self.assertEqual(checks['db.records']['status'], 'unknown')
        self.assertEqual(self.fixture.inventory(), before)
