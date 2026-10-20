"""Record tool routing over the authenticated real G3 socket and disposable DB."""
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from workbench.backend.paths import DataLayout
from workbench.backend.service import Backend
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer
from workbench.storage.work_records.store import RecordStore
from tests.bridge.test_tool_request_bridge import FakePeer


def arguments(action, **changes):
    value = dict(action=action, record_id=None, revision=None, expected_revision=None,
                 title=None, body=None, task_id=None, query=None, limit=None,
                 after=None, output_path=None)
    value.update(changes)
    return value


class RecordToolServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wr05-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.project = self.root / "project"
        self.project.mkdir()
        self.backend = Backend.__new__(Backend)
        self.backend.layout = DataLayout(self.state)
        self.backend.project_dir = str(self.project)
        self.tokens = {"manager": str(uuid4()), "worker": str(uuid4())}
        self.bridge = G3BridgeServer(self.root / "bridge.sock", self.tokens)
        self.bridge.set_tool_handler(self.backend._tool_request)
        self.bridge.start()
        self.addCleanup(self.bridge.close)
        self.peers = {}
        for role in self.tokens:
            peer = FakePeer(self.root / "bridge.sock", role, self.tokens[role])
            self.addCleanup(peer.close)
            self.assertIsNotNone(peer.read_kind("ready"))
            self.peers[role] = peer

    def call(self, role, args, **overrides):
        peer = self.peers[role]
        peer.tool_request("workbench_records", args, tool_call_id=str(uuid4()), **overrides)
        answer = peer.read_kind("tool_result")
        self.assertIsNotNone(answer)
        return answer["result"]

    def test_manager_create_saves_exact_body_in_host_project_without_task_db(self):
        body = '# 기록\n\n"원문" **그대로** 😀\n'
        result = self.call("manager", arguments("create", title="실험 메모", body=body))
        self.assertEqual(result["status"], "saved")
        record = result["record"]
        self.assertEqual(record["actor"], "manager")
        self.assertEqual(record["body"], body)
        self.assertEqual(RecordStore(self.backend.layout.records).get(record["record_id"]),
                         {"status": "ok", "record": record})
        self.assertFalse(self.backend.layout.tasks.exists())
        self.assertFalse((self.project / "docs").exists())

    def test_worker_reads_and_revises_manager_record_then_manager_reads_both_snapshots(self):
        first = self.call("manager", arguments("create", title="첫 메모", body="이전 원문\n"))["record"]
        self.assertEqual(self.call("worker", arguments("show", record_id=first["record_id"])),
                         {"status": "ok", "record": first})
        result = self.call("worker", arguments("revise", record_id=first["record_id"],
            expected_revision=1, title="정정", body="worker의 정정 원문\n"))
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["record"]["actor"], "worker")
        self.assertEqual(result["record"]["revision"], 2)
        self.assertEqual(self.call("manager", arguments("show", record_id=first["record_id"])),
                         {"status": "ok", "record": result["record"]})
        self.assertEqual(self.call("manager", arguments("show", record_id=first["record_id"], revision=1)),
                         {"status": "ok", "record": first})

    def test_task_link_uses_existing_host_task_repository_on_request_thread(self):
        from workbench.tasks.repository import TaskRepository
        with TaskRepository(self.backend.layout.tasks) as tasks:
            tasks.create_task({"goal": "host task"}, task_id="task-host")
        saved = self.call("worker", arguments("create", title="연결 메모", body="Task 원문\n", task_id="task-host"))
        self.assertEqual(saved["status"], "saved")
        self.assertEqual(saved["record"]["task_id"], "task-host")
        self.assertEqual(saved["record"]["actor"], "worker")
        self.assertEqual(self.call("manager", arguments("create", title="bad", body="bad", task_id="missing")),
                         {"status": "rejected", "reason": "task_not_found"})

    def test_query_actions_return_existing_page_contract_over_uds(self):
        store = RecordStore(self.backend.layout.records)
        first = store.create(title="선택", body="literal % _ sentinel", actor="manager")["record"]
        store.create(title="다른 기록", body="not a match", actor="worker")
        page = self.call("worker", arguments("list", limit=1))
        self.assertEqual(page["status"], "ok")
        self.assertEqual(len(page["records"]), 1)
        self.assertTrue(page["has_more"])
        found = self.call("manager", arguments("search", query="% _"))
        self.assertEqual([item["record_id"] for item in found["records"]], [first["record_id"]])
        history = self.call("worker", arguments("history", record_id=first["record_id"]))
        self.assertEqual([item["revision"] for item in history["revisions"]], [1])
        self.assertNotIn("body", history["revisions"][0])

    def test_recent_is_bounded_metadata_with_explicit_omission_and_preserved_body(self):
        import json
        store = RecordStore(self.backend.layout.records)
        body = "😀" * 200
        latest = None
        for _ in range(7):
            latest = store.create(title="😀" * 256, body=body, actor="manager")["record"]
        recent = self.call("worker", arguments("recent"))
        self.assertEqual(recent["status"], "ok")
        self.assertLessEqual(len(recent["records"]), 5)
        self.assertEqual(recent["records"][0]["record_id"], latest["record_id"])
        self.assertLessEqual(len(json.dumps(recent).encode("utf-8")), 16384)
        self.assertTrue(recent["omitted"])
        self.assertTrue(recent["context_truncated"])
        for item in recent["records"]:
            self.assertNotIn("body", item)
            self.assertEqual(item["preview"], "😀" * 160)
            self.assertTrue(item["preview_truncated"])
        self.assertEqual(store.get(latest["record_id"])["record"]["body"], body)

    def test_export_requires_path_and_resolves_relative_path_from_host_project(self):
        store = RecordStore(self.backend.layout.records)
        first = store.create(title="출력", body="이전 원문\n", actor="manager")["record"]
        store.revise(first["record_id"], expected_revision=1, title="수정", body="최신 원문\n", actor="worker")
        self.assertEqual(self.call("manager", arguments("export", record_id=first["record_id"])),
                         {"status": "needs_output_path", "reason": "needs_output_path"})
        result = self.call("worker", arguments("export", record_id=first["record_id"], revision=1, output_path="copy.md"))
        output = self.project / "copy.md"
        self.assertEqual(result, {"status": "exported", "path": str(output), "record_id": first["record_id"], "revision": 1})
        self.assertTrue(output.read_text().endswith("이전 원문\n"))
        self.assertEqual(self.call("manager", arguments("export", record_id=first["record_id"], output_path="copy.md"))["reason"], "output_exists")

    def test_oversize_serialized_show_is_explicit_failure_without_truncating_body(self):
        from workbench.tasks.repository import TaskRepository
        task_id = "t" * 900001
        with TaskRepository(self.backend.layout.tasks) as tasks:
            tasks.create_task({"goal": "large identity"}, task_id=task_id)
            store = RecordStore(self.backend.layout.records, task_repository=tasks)
            first = store.create(title="한도", body="original body sentinel\n", task_id=task_id, actor="manager")["record"]
        result = self.call("worker", arguments("show", record_id=first["record_id"]))
        self.assertEqual(result, {"status": "unavailable", "reason": "bridge_response_bound"})
        self.assertEqual(RecordStore(self.backend.layout.records).get(first["record_id"])["record"]["body"],
                         "original body sentinel\n")
