"""Explicit opt-in actual installed SDK test, never an inferred product8 pass."""
import json
from hashlib import sha256
import os
from pathlib import Path
import unittest
from uuid import uuid4

from tests.backend.q01_product_adapter_probe.runtime import ROOT, execute, NO_SEND


class CurrentProductRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("WORKBENCH_RUN_PRODUCT_SDK") == "1", "explicit owned SDK runtime opt-in required")
    def test_current_product_tools_and_lifecycle_compositions(self):
        output = Path(os.environ.get("WORKBENCH_PRODUCT_TEST_OUTPUT", str(
            ROOT / ".workflow/dvl-local" / ("dvl09-independent-product-" + str(uuid4())))))
        frozen = execute(output)
        report = json.loads((output / "runtime-report.json").read_text())
        self.assertTrue(all(report["results"].get(name, {}).get("status") == "summary_result"
                            for name in ("positive", "positive2", "positive3")), "normal actual product controls must succeed")
        self.assertTrue(any(row.get("event") == "owner_request_changed" for row in report["events"]))
        self.assertFalse([row for row in report["posts"] if row["case"] == "request_mismatch"],
                         "SPEC 8.1 requires request/receipt mismatch to be blocked before dispatch")
        for name in NO_SEND:
            with self.subTest(negative=name):
                self.assertIs(report["results"][name].get("sent"), False)
                self.assertFalse([row for row in report["posts"] if row["case"] == name])
                self.assertEqual(report["entries"][name], [])
                self.assertIsNone(report["records"][name]["receipt"])
        for name in ("saved_then_bash", "duplicate", "late_before_commit"):
            with self.subTest(refused_result=name):
                self.assertEqual(report["results"][name]["status"], "rejected")
                self.assertEqual(report["entries"][name], [])
                self.assertIsNone(report["records"][name]["receipt"])
        ready=next(row for row in report["events"] if row.get("event")=="ready")
        dto_rows=[row for row in report["events"] if row.get("event")=="fixture_dto_bound"]
        self.assertEqual(len(dto_rows),len(report["results"]))
        for dto in dto_rows:
            with self.subTest(dto_clone=dto["case"]):
                self.assertIs(dto["cloneCanonicalEquivalent"],True)
                self.assertEqual(dto["dtoDigest"],dto["descriptorDigest"])
                self.assertEqual(dto["tierTypeof"],"object")
                self.assertEqual(dto["tierPrototype"],"Object.prototype")
                self.assertIs(dto["tierPlainObject"],True)
                self.assertIs(dto["detachedTier"],True)
        for event in report["events"]:
            if event.get("event")in ("case_done","parent_continuation_start"):
                self.assertEqual(event["nativeParentState"],ready["nativeParentState"])
                self.assertEqual(event["nativeParentDigest"],ready["nativeParentDigest"])
        for name in ("metadata_epoch", "metadata_thinking", "metadata_tier", "metadata_nested_model", "metadata_replace"):
            with self.subTest(metadata_snapshot=name):
                self.assertTrue(any(row.get("event")=="owner_metadata_changed" and row.get("case")==name and row.get("sameObserverObject") is (name!="metadata_replace") for row in report["events"]))
                self.assertIs(report["results"][name]["sent"],False)
                self.assertTrue(any(row.get("event")=="stream_guard" and row.get("case")==name and row.get("reason")=="model_mismatch" for row in report["events"]))
        self.assertEqual(report["results"]["prepared_metadata"]["reason"],"request_mismatch")
        self.assertEqual(report["results"]["sdk_tier_clone"]["status"],"summary_result")
        self.assertEqual(len(report["entries"]["sdk_tier_clone"]),1)
        clone=next(row for row in report["events"] if row.get("event")=="owner_sdk_tier_return_mutated")
        self.assertIs(clone["independentObjects"],True)
        self.assertEqual(clone["second"],clone["expectedOriginal"])
        self.assertEqual(clone["observerOriginal"],clone["expectedOriginal"])
        self.assertTrue(any(row.get("event") == "parent_continuation_done" for row in report["events"]))
        self.assertTrue(all(all(report["composition"][name]["checks"].values()) for name in ("origin", "recovery")))
        self.assertFalse(report["composition"]["priority"].get("error"))
        self.assertEqual(report["composition"]["priority"]["measurement_issues"], [])
        socket_boundary=report["composition"]["priority"]["socket_boundary"]
        self.assertLessEqual(socket_boundary["path_utf8_bytes"],107)
        self.assertEqual(socket_boundary["uid"],os.getuid())
        self.assertEqual(socket_boundary["mode"],"0o700")
        self.assertIs(report["composition"]["priority"]["socket_cleaned"],True)
        self.assertEqual(report["process"]["remaining_owned"], [])
        def verify_custody(value):
            if isinstance(value, dict):
                if "pgid_absent" in value:
                    self.assertIs(value["pgid_absent"], True)
                for post in value.get("posts", []):
                    self.assertEqual(sha256(Path(post["body_path"]).read_bytes()).hexdigest(), post["sha256"])
                for child in value.values(): verify_custody(child)
            elif isinstance(value, list):
                for child in value: verify_custody(child)
        verify_custody(report)
        self.assertTrue(frozen["passed"], json.dumps(frozen["verdicts"], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
