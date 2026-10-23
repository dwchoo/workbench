"""Independent evidence and ownership checks for the local parent-context unit.

The recorded OMP trace is an explicit input. These tests never launch OMP.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from tests.backend.q01_sdk_context_probe import run_context
from tests.backend.q01_sdk_full_probe import full_probe


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def event(trace: list[dict], name: str, cycle: int) -> dict:
    found = [row for row in trace if row.get("event") == name and row.get("cycle") == cycle]
    assert len(found) == 1, (name, cycle, len(found))
    return found[0]


def texts(messages: list[dict]) -> list[str]:
    out: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out.extend(item["text"] for item in content
                       if isinstance(item, dict) and item.get("type") == "text"
                       and isinstance(item.get("text"), str))
    return out


def receipt_membership(messages: list[dict], receipts: list[dict]) -> list[int]:
    parsed = []
    for content in texts(messages):
        try:
            parsed.append(json.loads(content))
        except ValueError:
            continue
    return [sum(value == receipt for value in parsed) for receipt in receipts]


class ContextIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("Q01_SDK_CONTEXT_RESULT")
        output = os.environ.get("Q01_SDK_CONTEXT_TEST_DIR")
        if not supplied and not output:
            raise unittest.SkipTest("Q01_SDK_CONTEXT_RESULT and Q01_SDK_CONTEXT_TEST_DIR required; skipped tests are not a gate pass")
        if not supplied or not output:
            raise AssertionError("explicit actual result and private test output required")
        cls.result_path = Path(supplied).resolve(strict=True)
        cls.result = json.loads(cls.result_path.read_text())
        cls.artifact_paths = [Path(name).resolve(strict=True)
                              for name in cls.result["artifact_sha256"]]
        cls.paths = {Path(name).name: Path(name).resolve(strict=True)
                     for name in cls.result["artifact_sha256"]}
        cls.cycles_path = next(path for path in cls.artifact_paths
                               if path.name == "cycles.json" and "cycles02" in path.parts)
        cls.cycles = json.loads(cls.cycles_path.read_text())
        cls.output = Path(output).resolve(strict=True)

    def test_current_sources_raw_trace_sqlite_mirrors_and_owned_processes(self) -> None:
        for name, expected in self.result["current_source_sha256"].items():
            self.assertEqual(digest(full_probe.ROOT / name), expected)
        for name, expected in self.result["artifact_sha256"].items():
            self.assertEqual(digest(Path(name)), expected)
        self.assertEqual(digest(Path(self.cycles["argv"][0])), self.cycles["omp_sha256"])
        self.assertEqual(self.cycles["omp_sha256"], self.result["actual_runtime"]["omp_binary_sha256"])
        self.assertEqual(self.cycles["omp_version"], self.result["actual_runtime"]["omp_version"])
        self.assertEqual(self.cycles["source_sha256"],
                         {name: digest(run_context.HERE / name)
                          for name in ("context_extension.ts", "run_context.py")})
        private = self.cycles_path.parent / "private"
        self.assertEqual([json.loads(line) for line in
                          (private / "trace.jsonl").read_text().splitlines()], self.cycles["trace"])
        self.assertEqual(digest(private / "fixed_writer.py"),
                         self.result["source_provenance"]["fixed_writer_sha256"])
        self.assertEqual(digest(run_context.HERE.parent / "q01_sdk_private_probe/summary-skill.txt"),
                         self.result["source_provenance"]["skill_sha256"])
        self.assertNotEqual(digest(full_probe.BODY),
                            self.result["source_provenance"]["bundled_task_body_sha256"])
        with sqlite3.connect(f"file:{private / 'summary.sqlite3'}?mode=ro", uri=True) as db:
            rows = [list(row) for row in db.execute("SELECT id, summary FROM summaries ORDER BY id")]
        self.assertEqual(rows, [[n, f"Q01-private-raw-marker-{n}-REMOVE-FROM-PARENT"]
                                for n in range(1, 4)])
        self.assertEqual(self.cycles["db_rows"], rows)
        for process in (json.loads(self.paths["discriminator.json"].read_text())["process"],
                        json.loads(next(path for path in self.artifact_paths
                                        if path.name == "cycles.json" and "cycles01" in path.parts).read_text())["process"],
                        self.cycles["process"]):
            self.assertTrue(process["pump_stopped"])
            self.assertEqual(process["remaining_owned"], [])
            with self.assertRaises(ProcessLookupError):
                os.killpg(process["pgid"], 0)

    def test_three_parent_posts_contain_exact_parsed_receipt_membership_without_private_raw(self) -> None:
        posts = self.cycles["posts"]
        self.assertEqual([p["kind"] for p in posts],
                         ["child", "child", "parent"] * 3)
        parents = [p for p in posts if p["kind"] == "parent"]
        children = [p for p in posts if p["kind"] == "child"]
        receipts = [json.loads(event(self.cycles["trace"], "cycle_receipt_sent", n)["receipt"])
                    for n in range(1, 4)]
        expected = [[1, 0, 0], [1, 1, 0], [1, 1, 1]]
        self.assertEqual([receipt_membership(p["messages"], receipts) for p in parents], expected)
        # The JSON receipt is escaped inside the request body; a raw substring count is not membership.
        self.assertEqual([p["receipt_occurrences"] for p in parents], [0, 0, 0])
        for p in parents:
            self.assertTrue(p["tools_present"])
            self.assertTrue({"read", "bash", "edit", "write"}.issubset(p["tools"]))
            self.assertEqual(p["tools"], parents[0]["tools"])
            self.assertTrue(all(marker not in text for text in texts(p["messages"])
                                for marker in (f"Q01-private-raw-marker-{n}-REMOVE-FROM-PARENT"
                                               for n in range(1, 4))))
        for n in range(1, 4):
            self.assertTrue(any(f"Q01-private-raw-marker-{n}-REMOVE-FROM-PARENT" in text
                                for p in children[2*n-2:2*n] for text in texts(p["messages"])))
            sent = event(self.cycles["trace"], "cycle_receipt_sent", n)
            self.assertLessEqual(sent["bytes"], 4096)
            self.assertEqual(sent["bytes"], len(sent["receipt"].encode()))
            self.assertEqual(receipts[n-1], {"kind": "summary_result",
                                             "requestId": f"q01-context-{n}", "status": "accepted",
                                             "summarySha": sha256(sent["rawMarker"].encode()).hexdigest()})

    def test_receipt_membership_rejects_dropped_or_duplicated_message(self) -> None:
        parents = [p for p in self.cycles["posts"] if p["kind"] == "parent"]
        receipts = [json.loads(event(self.cycles["trace"], "cycle_receipt_sent", n)["receipt"])
                    for n in range(1, 4)]
        target = json.dumps(receipts[0], separators=(",", ":"))
        copied = json.loads(json.dumps(parents[0]["messages"]))
        for message in copied:
            if isinstance(message.get("content"), list):
                for item in message["content"]:
                    if item.get("text") == target:
                        item["text"] = "removed"
        self.assertEqual(receipt_membership(copied, receipts), [0, 0, 0])
        duplicated = json.loads(json.dumps(parents[0]["messages"]))
        duplicated.append(next(message for message in duplicated
                               if target in texts([message])))
        self.assertEqual(receipt_membership(duplicated, receipts), [2, 0, 0])

    def test_child_fresh_db_only_guard_body_skill_disposal_and_parent_roster(self) -> None:
        trace = self.cycles["trace"]
        posts = self.cycles["posts"]
        sessions = []
        for n in range(1, 4):
            startup = event(trace, "startup", n)
            sent = event(trace, "cycle_receipt_sent", n)
            sessions.append(startup["sessionId"])
            self.assertTrue(startup["admitted"] and startup["modelMatch"])
            self.assertEqual(startup["active"], ["db_summary_save"])
            self.assertEqual(startup["wrong"], "roster_mismatch")
            self.assertEqual(startup["stale"], "owner_inactive")
            self.assertEqual((startup["source"], startup["filePath"]),
                             ("bundled", "embedded:task.md"))
            self.assertEqual(startup["bodySha"],
                             self.result["source_provenance"]["bundled_task_body_sha256"])
            self.assertEqual(startup["skillSha"], self.result["source_provenance"]["skill_sha256"])
            self.assertEqual([p["tools"] for p in posts[3*n-3:3*n-1]],
                             [["db_summary_save"], ["db_summary_save"]])
            self.assertEqual(event(trace, "execute", n)["params"], {"summary": sent["rawMarker"]})
            self.assertEqual(event(trace, "writer", n)["stdout"], '{"saved":true}')
            self.assertIsNone(event(trace, "writer", n)["error"])
            self.assertLess(trace.index(startup), trace.index(event(trace, "child_prompt", n)))
            self.assertLess(trace.index(sent), trace.index(event(trace, "child_disposed", n)))
            self.assertEqual(sent["parentRosterBefore"], sent["parentRosterAfter"])
            self.assertEqual(set(posts[3*n-1]["tools"]) - set(sent["parentRosterBefore"]), set())
            self.assertNotEqual(posts[3*n-1]["tools"], sent["parentRosterBefore"])
        self.assertEqual(len(set(sessions)), 3)
        skill = (run_context.HERE.parent / "q01_sdk_private_probe/summary-skill.txt").read_text()
        for p in (row for row in posts if row["kind"] == "child"):
            system = p["messages"][0]["content"]
            self.assertTrue(system.endswith("\n\n" + skill))
            self.assertEqual(sha256(system[:-(len(skill) + 2)].encode()).hexdigest(),
                             self.result["source_provenance"]["bundled_task_body_sha256"])
        self.assertEqual([p["kind"] for p in posts], ["child", "child", "parent"] * 3)
        for n, row in enumerate(self.cycles["cycles"], 1):
            self.assertEqual(row["cycle"], n)
            self.assertTrue(row["receipt_sent"] and row["normal_ended"] and row["no_unsolicited_post"])
            self.assertEqual((row["posts_before"], row["posts_after"]), (3*n-1, 3*n))

    def test_concrete_parent_body_and_message_deltas_do_not_claim_token_counts(self) -> None:
        parents = [p for p in self.cycles["posts"] if p["kind"] == "parent"]
        raw_lengths = [len(Path(p["raw_body_path"]).read_bytes()) for p in parents]
        self.assertEqual([p["raw_bytes"] for p in parents], raw_lengths)
        self.assertEqual([len(p["messages"]) for p in parents], [3, 6, 9])
        deltas = [raw_lengths[n] - raw_lengths[n-1] for n in range(1, 3)]
        self.assertTrue(all(delta > 0 for delta in deltas))
        self.assertEqual(self.cycles["parent_context_deltas"], deltas)
        self.assertEqual(raw_lengths,
                         [row["request_bytes"] for row in self.cycles["parent_context"]])
        self.assertEqual([row["token_count"] for row in self.cycles["parent_context"]],
                         [None, None, None])
        first = json.loads(next(path for path in self.artifact_paths
                                if path.name == "cycles.json" and "cycles01" in path.parts).read_text())
        self.assertFalse(first["checks"]["parent_receipts_exact"])
        self.assertFalse(first["checks"]["parent_tools_unchanged"])

    def test_each_preserved_raw_provider_post_binds_reported_sha_messages_and_tools(self) -> None:
        manifest = {str(Path(name).resolve()): expected
                    for name, expected in self.result["artifact_sha256"].items()}
        self.assertEqual(len(self.cycles["posts"]), 9)
        raw_paths = []
        for index, post in enumerate(self.cycles["posts"], 1):
            path = Path(post["raw_body_path"]).resolve(strict=True)
            raw_paths.append(path)
            self.assertEqual(path.name, f"provider-post-{index:03d}.body")
            self.assertIn(str(path), manifest)
            raw = path.read_bytes()
            self.assertEqual(sha256(raw).hexdigest(), manifest[str(path)])
            self.assertEqual(sha256(raw).hexdigest(), post["sha256"])
            self.assertEqual(len(raw), post["raw_bytes"])
            parsed = json.loads(raw)
            self.assertEqual(parsed["messages"], post["messages"])
            self.assertEqual("tools" in parsed, post["tools_present"])
            names = [tool["function"]["name"] for tool in parsed.get("tools") or []]
            self.assertEqual(names, post["tools"])
            self.assertEqual(post["kind"], "child" if names == ["db_summary_save"] else "parent")
            if post["kind"] == "parent":
                self.assertTrue(all(f"Q01-private-raw-marker-{n}-REMOVE-FROM-PARENT".encode() not in raw
                                    for n in range(1, 4)))
            else:
                self.assertTrue(any(f"Q01-private-raw-marker-{n}-REMOVE-FROM-PARENT".encode() in raw
                                    for n in range(1, 4)))
        self.assertEqual(len(set(raw_paths)), 9)
        receipts = [json.loads(event(self.cycles["trace"], "cycle_receipt_sent", n)["receipt"])
                    for n in range(1, 4)]
        parents = [p for p in self.cycles["posts"] if p["kind"] == "parent"]
        self.assertEqual([receipt_membership(json.loads(Path(p["raw_body_path"]).read_bytes())["messages"],
                                             receipts) for p in parents],
                         [[1, 0, 0], [1, 1, 0], [1, 1, 1]])

    def test_first_public_send_message_discriminator_was_unsolicited_post_free(self) -> None:
        first = json.loads(self.paths["discriminator.json"].read_text())
        self.assertTrue(first["sent_before_prompt"])
        self.assertEqual(first["posts_before_normal_prompt"], 0)
        self.assertTrue(first["normal_response"])
        self.assertEqual(len(first["posts"]), 1)
        self.assertFalse(first["posts"][0]["tools_present"])
        self.assertEqual(first["posts"][0]["tools"], [])
        receipt = first["receipt"]
        self.assertEqual(sum(text.count(receipt) for text in texts(first["posts"][0]["messages"])), 1)

    def _failure_at(self, boundary: str) -> tuple[MagicMock, MagicMock, MagicMock]:
        server = MagicMock()
        server.server_port = 12345
        server_thread = MagicMock()
        pump_thread = MagicMock()
        pump_thread.start.side_effect = RuntimeError("pump start fault")
        process = MagicMock()
        process.pid = 999991
        process.poll.return_value = None
        with tempfile.TemporaryDirectory(dir=self.output, prefix="context-fault-") as tmp:
            target = Path(tmp) / "evidence"
            with patch.object(run_context, "ThreadingHTTPServer", return_value=server), \
                 patch.object(run_context.threading, "Thread", side_effect=[server_thread, pump_thread]), \
                 patch.object(run_context.shutil, "which", return_value="/bin/true"), \
                 patch.object(run_context.sys, "argv", ["run_context.py", str(target), "cycles"]), \
                 patch.object(run_context.os, "killpg") as killpg, \
                 patch.object(run_context.subprocess, "run", side_effect=(
                     RuntimeError("version fault") if boundary == "version" else None),
                     return_value=SimpleNamespace(stdout="omp/18.8.6")), \
                 patch.object(run_context.subprocess, "Popen", side_effect=(
                     OSError("popen fault") if boundary == "popen" else None),
                     return_value=process):
                with self.assertRaisesRegex((RuntimeError if boundary != "popen" else OSError),
                                            f"{boundary}( start)? fault"):
                    run_context.run(target)
                return server, server_thread, process, killpg

    def test_version_failure_closes_allocated_provider(self) -> None:
        server, thread, _process, _killpg = self._failure_at("version")
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()
        thread.join.assert_called_once()

    def test_popen_failure_closes_allocated_provider(self) -> None:
        server, thread, _process, _killpg = self._failure_at("popen")
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()
        thread.join.assert_called_once()

    def test_pump_start_failure_reaps_owned_child(self) -> None:
        server, thread, process, killpg = self._failure_at("pump")
        killpg.assert_called_once_with(process.pid, 15)
        process.wait.assert_called()
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()
        thread.join.assert_called_once()

    def _provider_setup_failure(self, boundary: str) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
        server = MagicMock()
        server.server_port = 12345
        provider_thread = MagicMock()
        if boundary == "thread_start":
            provider_thread.start.side_effect = RuntimeError("provider thread start fault")
        original_write = Path.write_text

        def write_with_fault(path: Path, data: str, *args, **kwargs) -> int:
            if (boundary == "models_write" and path.name == "models.yml" or
                    boundary == "overlay_write" and path.name == "overlay.yml"):
                raise OSError(f"{boundary} fault")
            return original_write(path, data, *args, **kwargs)

        with tempfile.TemporaryDirectory(dir=self.output, prefix="context-setup-fault-") as tmp:
            target = Path(tmp) / "evidence"
            with patch.object(run_context, "ThreadingHTTPServer", return_value=server), \
                 patch.object(run_context.threading, "Thread", return_value=provider_thread), \
                 patch.object(Path, "write_text", autospec=True, side_effect=write_with_fault), \
                 patch.object(run_context.subprocess, "run") as version, \
                 patch.object(run_context.subprocess, "Popen") as popen, \
                 patch.object(run_context.sys, "argv", ["run_context.py", str(target), "cycles"]):
                expected_error = RuntimeError if boundary == "thread_start" else OSError
                with self.assertRaisesRegex(expected_error, f"{boundary.replace('_', ' ')} fault"
                                            if boundary == "thread_start" else f"{boundary} fault"):
                    run_context.run(target)
                return server, provider_thread, version, popen

    def test_provider_thread_start_failure_closes_unstarted_server_without_shutdown_join(self) -> None:
        server, thread, version, popen = self._provider_setup_failure("thread_start")
        thread.start.assert_called_once()
        server.server_close.assert_called_once()
        server.shutdown.assert_not_called()
        thread.join.assert_not_called()
        version.assert_not_called()
        popen.assert_not_called()

    def test_models_write_failure_closes_started_provider_before_omp_launch(self) -> None:
        server, thread, version, popen = self._provider_setup_failure("models_write")
        thread.start.assert_called_once()
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()
        thread.join.assert_called_once()
        version.assert_not_called()
        popen.assert_not_called()

    def test_overlay_write_failure_closes_started_provider_before_omp_launch(self) -> None:
        server, thread, version, popen = self._provider_setup_failure("overlay_write")
        thread.start.assert_called_once()
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()
        thread.join.assert_called_once()
        version.assert_not_called()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
