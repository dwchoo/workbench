"""CLI export adapter coverage; exporter filesystem invariants are already tested."""
import json
import unittest
from pathlib import Path
from tests.backend.work_records import test_cli
from workbench.storage.work_records.store import RecordStore

class CliExportTests(unittest.TestCase):
    setUp=test_cli.RecordsCliTests.setUp
    run_wb=test_cli.RecordsCliTests.run_wb
    make_project=test_cli.RecordsCliTests.make_project

    def test_selected_revision_exports_relative_to_cli_cwd_and_preserves_database(self):
        project,data=self.make_project("export-project","초안","OLD_BODY 😀\n")
        store=RecordStore(data/"records.sqlite3")
        first=store.list()["records"][0]
        store.revise(first["record_id"],expected_revision=1,title="확정",body="NEW_BODY\n",actor="worker")
        before=(data/"records.sqlite3").read_bytes()
        nested=project/"nested";nested.mkdir()
        run=self.run_wb(nested,"records","export",first["record_id"],"--revision","1","--output","copy.md","--json")
        self.assertEqual(run.returncode,0,run.stderr)
        result=json.loads(run.stdout)
        self.assertEqual(result,{"status":"exported","path":str(nested/"copy.md"),"record_id":first["record_id"],"revision":1})
        output=(nested/"copy.md").read_text()
        self.assertTrue(output.endswith("OLD_BODY 😀\n"));self.assertIn("Revision: 1",output)
        self.assertNotIn("NEW_BODY",output)
        self.assertEqual((data/"records.sqlite3").read_bytes(),before)
        self.assertFalse(self.marker.exists())

    def test_required_path_default_latest_and_collision_status_survive_the_adapter(self):
        project,data=self.make_project("export-latest","원본","BODY_1")
        store=RecordStore(data/"records.sqlite3");record=store.list()["records"][0]
        store.revise(record["record_id"],expected_revision=1,title="최신",body="BODY_2",actor="manager")
        missing=self.run_wb(project,"records","export",record["record_id"],"--json")
        self.assertEqual(missing.returncode,2);self.assertIn("--output",missing.stderr)
        latest=self.run_wb(project,"records","export",record["record_id"],"--output","latest.md","--json")
        self.assertEqual(json.loads(latest.stdout)["revision"],2)
        before=(project/"latest.md").read_bytes()
        collision=self.run_wb(project,"records","export",record["record_id"],"--output","latest.md","--json")
        self.assertEqual(collision.returncode,1)
        self.assertEqual(json.loads(collision.stdout),{"status":"unavailable","reason":"output_exists","path":str(project/"latest.md")})
        self.assertEqual((project/"latest.md").read_bytes(),before)
        self.assertEqual(sorted(p.name for p in project.glob("*.md")),["latest.md"])
        self.assertFalse(self.marker.exists())
