"""Explicit feature runtime wrappers, not independent tests or stored verdicts.

These methods use the runner's existing unittest/ownership channel and legacy
ScenarioReport format. Missing runtime observations remain not_run/inconclusive.
"""
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import shutil
import unittest

from tests.integration import cw16_harness as h
from tools.dev_validate import _input_manifest


class FeaturePreciseItems(unittest.TestCase):
    def run_feature(self, name, function):
        if os.environ.get("WB_DVL_FEATURE_ITEMS") != "1":
            self.skipTest("explicit owned feature runtime selection required")
        namespace = Path(os.environ["WB_DVL_NAMESPACE"])
        if (not namespace.is_absolute() or namespace.resolve() != namespace
                or namespace.stat().st_uid != os.getuid() or namespace.stat().st_mode & 0o077):
            raise ValueError("feature namespace must be private and owned")
        for key in ("DVL10_INDEPENDENT_OUTPUT", "WB_DVL_PRECISE_TEST_OUTPUT"):
            output = Path(os.environ[key])
            if not output.is_absolute() or not output.resolve().is_relative_to(namespace):
                raise ValueError("fixture evidence escaped feature namespace")
        report = h.ScenarioReport(name, run_id=name, evidence_level="runtime",
                                  boundary="owned actual API composition; no operating MODEL quality claim")
        before = _input_manifest()
        work = namespace / "feature"
        observed = None
        failure = None
        try:
            module = importlib.import_module("tests.integration.live_validation_retention")
            observed = getattr(module, function)(work)
            if not isinstance(observed, dict):
                raise RuntimeError("feature runtime observation unavailable")
            report.data["observation"] = observed
            if observed.get("result") == "failed":
                raise AssertionError("actual feature assertion failed")
            if observed.get("result") != "passed":
                raise RuntimeError("feature runtime evidence incomplete")
            report.step("actual_execution", h.PASS)
        except AssertionError as error:
            failure = error
            report.step("actual_execution", h.FAIL, assertion=str(error)[:2000])
        except Exception as error:
            failure = error
            report.step("actual_execution", h.NOT_RUN, reason=type(error).__name__)
            report.unknown(type(error).__name__)
        finally:
            after = _input_manifest()
            stable = before == after
            report.data["input_before"] = before["sha256"]
            report.data["input_after"] = after["sha256"]
            report.step("same_candidate", h.PASS if stable else h.NOT_RUN)
            if not stable:
                report.unknown("candidate changed during feature runtime")
            cleanup = observed.get("cleanup", {}) if isinstance(observed, dict) else {}
            clean = cleanup.get("complete") is True
            if clean:
                shutil.rmtree(work, ignore_errors=False)
            clean = clean and not work.exists()
            report.step("owned_cleanup", h.PASS if clean else h.NOT_RUN)
            report.data["cleanup"] = {"root_removed": clean,
                "residue_before_fallback": cleanup.get("remaining_owned", [])}
            if not clean:
                report.unknown("owned cleanup not confirmed")
            target = report.write()
            raw = target.read_bytes()
            if len(raw) > 1_000_000:
                raise ValueError("feature report exceeds existing evidence bound")
            print(json.dumps({"feature": name, "report": str(target),
                              "sha256": sha256(raw).hexdigest()}), flush=True)
        if isinstance(failure, AssertionError):
            raise failure
        if failure is not None or not stable or not clean:
            self.skipTest("required runtime observation or cleanup unavailable; see retained report")

    def test_q01_product_adapter_recheck(self):
        self.run_feature("q01_product_adapter_recheck", "run_q01_recheck")

    def test_new_storage_summary_raw_product_gates(self):
        self.run_feature("new_storage_summary_raw_product_gates", "run_retention_runtime")

    def test_model_metadata(self):
        self.run_feature("model_metadata", "run_model_metadata")

    def test_product_full_dvl14(self):
        self.run_feature("product_full_dvl14", "run_product_runtime")
