"""Actual Python CLI in a runtime-only copy; no OMP/network/model launch.

Audit events prove the Python operation boundary, not SDK child I/O or actual
Q01 observations. Canonical collector tests operate on this same owned copy.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from workbench.backend.summary_compatibility import POLICY_PATH, collect_runtime_identity

ROOT = Path(__file__).resolve().parents[2]


class RuntimeOnlyCompatibilityIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        for name in ("src/workbench", "omp_bridge"):
            shutil.copytree(ROOT / name, cls.root / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(ROOT / "pyproject.toml", cls.root / "pyproject.toml")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_collector_uses_canonical_policy_and_distinguishes_related_from_unrelated(self):
        original = collect_runtime_identity(ROOT)
        copied = collect_runtime_identity(self.root)
        self.assertEqual(copied["unknowns"], [])
        self.assertEqual(copied["policy_sha256"], sha256((ROOT / POLICY_PATH).read_bytes()).hexdigest())
        self.assertEqual(copied["closure_entries"], original["closure_entries"])
        for relative, closure_changed in (("src/workbench/ui/independent_fixture.py", False),
                                          ("src/workbench/tasks/independent_fixture.py", True)):
            path = self.root / relative
            try:
                path.write_text("VALUE = 1\n")
                after = collect_runtime_identity(self.root)
                self.assertNotEqual(after["runtime_digest"], copied["runtime_digest"])
                self.assertEqual(after["closure_digest"] != copied["closure_digest"], closure_changed)
            finally:
                path.unlink(missing_ok=True)
        self.assertEqual((self.root / POLICY_PATH).read_bytes(), (ROOT / POLICY_PATH).read_bytes())

    def test_incomplete_or_unsafe_runtime_and_unknown_imports_are_explicit_unknowns(self):
        seam = self.root / "src/workbench/ipc/bridge_g3/mailbox.py"
        saved = seam.with_suffix(".saved")
        seam.rename(saved)
        try:
            observation = collect_runtime_identity(self.root)
            self.assertIn("missing_required_seam:src/workbench/ipc/bridge_g3/mailbox.py", observation["unknowns"])
            seam.symlink_to(saved)
            observation = collect_runtime_identity(self.root)
            self.assertIn("unsafe_runtime_input:src/workbench/ipc/bridge_g3/mailbox.py", observation["unknowns"])
            seam.unlink()
        finally:
            saved.rename(seam)
        new = self.root / "src/workbench/backend/independent_unknown.py"
        try:
            new.write_text("import importlib\ndef unknown(name): return importlib.import_module(name)\n")
            self.assertIn("unknown_dynamic_import:src/workbench/backend/independent_unknown.py",
                          collect_runtime_identity(self.root)["unknowns"])
            new.write_text("import tests.some_verdict\n")
            self.assertTrue(any(v.startswith("development_import:") for v in
                                collect_runtime_identity(self.root)["unknowns"]))
        finally:
            new.unlink(missing_ok=True)

    def test_actual_runtime_only_cli_status_is_read_only_and_verify_is_non_pass(self):
        self.assertTrue(all(not (self.root / name).exists() for name in
                            ("tools", "tests", "docs", ".workflow")))
        data = self.root / "owned-data"
        home = self.root / "unused-home"
        environment = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(self.root / "src"),
                       "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(home)}
        # Actual CLI runs with Python audit checks. No foreign record, runtime
        # supplier, login path or OMP SDK is injected into the product.
        script = '''
import contextlib, io, json, pathlib, sys
events = []
def audit(name, args):
    if name.startswith("socket.") or name in ("subprocess.Popen", "os.system", "os.posix_spawn"):
        raise AssertionError("unexpected I/O: " + name)
    if name == "import" and str(args[0]).split(".")[0] in {"tools", "tests", "docs"}:
        raise AssertionError("development import")
    if name == "open": events.append(str(args[0]))
sys.addaudithook(audit)
from workbench.backend.cli import main
output = io.StringIO()
with contextlib.redirect_stdout(output):
    code = main(json.loads(sys.argv[1]))
modules = {name: ([str(module.__file__)] if getattr(module, "__file__", None)
                 else [str(p) for p in getattr(module, "__path__", [])])
           for name, module in sys.modules.items()
           if name == "workbench" or name.startswith("workbench.")}
print(json.dumps({"code": code, "response": json.loads(output.getvalue()),
                  "modules": modules, "opened": events}))
sys.exit(code)
'''
        common = ["logs", "summary-compat"]
        for action in ("status", "verify"):
            argv = common + [action, "--json", "--data-dir", str(data)]
            if action == "verify": argv += ["--scripted"]
            result = subprocess.run([sys.executable, "-c", script, json.dumps(argv)], cwd=self.root,
                                    env=environment, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0 if action == "status" else 1, result.stderr)
            observed = json.loads(result.stdout)
            self.assertTrue(all(paths and all(Path(path).is_relative_to(self.root / "src")
                                             for path in paths)
                                for paths in observed["modules"].values()))
            self.assertFalse(home.exists())
            self.assertFalse(any(Path(p).name in {"auth.json", "models.yml", "settings.json"}
                                 for p in observed["opened"]))
            if action == "status":
                self.assertFalse(data.exists(), "passive status must not allocate store")
                self.assertFalse(observed["response"]["admitted"])
            else:
                answer = observed["response"]
                self.assertEqual(answer["result"], "unavailable")
                self.assertEqual(len(answer["items"]), 8)
                self.assertEqual(set(answer["items"].values()), {"not_run"})
                self.assertEqual(answer["model_metadata"]["status"], "inconclusive")
                self.assertTrue(all(value == 0 for value in answer["io"].values()))
                self.assertFalse((data / "raw-v2").exists())
                self.assertFalse((data / "metadata.sqlite3").exists())
                self.assertEqual((data / "summary-compatibility").stat().st_mode & 0o777, 0o700)
                for file in (data / "summary-compatibility").glob("*.json"):
                    self.assertEqual(file.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
