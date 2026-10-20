"""Independent setup contracts through public APIs and CLI/filesystem results.

Local executables establish setup decisions, never actual OMP SDK support. The
opt-in installed test reuses an independently verified official binary read-only.
"""
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from workbench.backend.cli import main
from workbench.backend.setup import SetupError, resolve_selection, run_setup, setup_paths


class IndependentSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wb-independent-setup-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("home", "bin", "tmp", "research"):
            (self.root / name).mkdir(mode=0o700)
        self.env = {"HOME": str(self.root / "home"), "PATH": str(self.root / "bin"),
                    "TMPDIR": str(self.root / "tmp"), "LANG": "C.UTF-8"}
        self.record = self.root / "home/.config/omp-workbench/setup.json"

    def executable(self, name="chosen", version="53.7.9", sdk_version=None):
        path = self.root / "bin" / name
        path.write_text(f'''#!{sys.executable}
import json, os, sys, time
if '--version' in sys.argv:
    print('omp/{version}')
    raise SystemExit(0)
request = json.loads(sys.stdin.readline())
if request.get('type') != 'get_state': raise SystemExit(31)
print(json.dumps({{'type': 'workbench_setup_ready', 'sdk': True,
    'nonce': os.environ.get('WORKBENCH_SETUP_NONCE'),
    'version': {sdk_version or version!r}}}), flush=True)
time.sleep(30)
''')
        path.chmod(0o700)
        return path

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        with patch.dict(os.environ, self.env, clear=True), redirect_stdout(out), redirect_stderr(err):
            code = main(["setup", *args, "--json"])
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_passive_relative_xdg_falls_back_and_explicit_missing_choice_wins(self):
        self.env.update(XDG_CONFIG_HOME="relative/config", XDG_DATA_HOME="relative/data",
                        WORKBENCH_OMP=str(self.root / "missing-env"))
        paths = setup_paths(self.env)
        self.assertEqual(paths.config, self.root / "home/.config/omp-workbench")
        self.assertEqual(paths.tools, self.root / "home/.local/share/omp-workbench/tools")
        explicit = self.root / "missing-flag"
        selected = resolve_selection(explicit, self.env)
        self.assertEqual((selected.path, selected.source), (explicit, "explicit"))
        selected = resolve_selection(environment=self.env)
        self.assertEqual((selected.path, selected.source), (self.root / "missing-env", "environment"))
        self.assertEqual(list((self.root / "home").iterdir()), [])

    def test_changed_existing_version_reuses_actual_binary_and_refreshes_selection(self):
        binary = self.executable(version="53.7.9")
        run_setup(binary, self.env)
        self.executable(version="54.0.1")
        before = binary.read_bytes()
        with patch("workbench.backend.install_recipe._open_https", side_effect=AssertionError("no replacement")):
            result = run_setup(environment=self.env)
        self.assertEqual((result["status"], result["version"], result["path"]),
                         ("reused", "54.0.1", str(binary)))
        record = json.loads(self.record.read_text())
        self.assertEqual((record["version"], record["binary_sha256"]),
                         ("54.0.1", sha256(before).hexdigest()))
        self.assertEqual(binary.read_bytes(), before)

    def test_sdk_version_disagreement_preserves_previous_selection_and_user_binary(self):
        prior = self.executable("prior")
        run_setup(prior, self.env)
        saved = self.record.read_bytes()
        bad = self.executable("mismatch", version="54.0.1", sdk_version="53.7.9")
        original = bad.read_bytes()
        with patch("workbench.backend.install_recipe._open_https", side_effect=AssertionError("no fallback")):
            code, result, err = self.cli("--omp", str(bad))
        self.assertEqual(code, 1, err)
        self.assertEqual((result["state"], result["reason"], result["path"]),
                         ("failed", "omp_changed_or_sdk_version_mismatch", str(bad)))
        self.assertEqual(self.record.read_bytes(), saved)
        self.assertEqual(bad.read_bytes(), original)
        self.assertEqual(list((self.root / "tmp").iterdir()), [])

    def test_corrupt_saved_selection_reports_effective_path_without_replacement(self):
        run_setup(self.executable("prior"), self.env)
        payload = b'{"schema_version": 1, "state": "ready", broken'
        self.record.write_bytes(payload)
        self.executable("omp")
        with patch("workbench.backend.install_recipe._open_https", side_effect=AssertionError("no fallback")):
            code, result, err = self.cli()
        self.assertEqual(code, 1, err)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.record.read_bytes(), payload)
        self.assertEqual(result["path"], str(self.record), "diagnosis must name the corrupt selection file")
        self.assertTrue(result["reason"])
        self.assertIn("retry", result)

    def test_nonexecutable_requested_program_preserves_prior_ready_and_reports_cause(self):
        run_setup(self.executable("prior"), self.env)
        saved = self.record.read_bytes()
        bad = self.root / "bin/no-execute"
        bad.write_bytes(b"user-owned file, preserve bytes\n")
        bad.chmod(0o600)
        code, result, err = self.cli("--omp", str(bad))
        self.assertEqual(code, 1, err)
        self.assertEqual((result["reason"], result["path"]), ("existing_omp_broken", str(bad)))
        self.assertTrue(result["detail"])
        self.assertEqual(self.record.read_bytes(), saved)
        self.assertEqual((bad.read_bytes(), bad.stat().st_mode & 0o777),
                         (b"user-owned file, preserve bytes\n", 0o600))

    def test_symlinked_managed_tools_directory_never_mutates_foreign_target(self):
        binary = self.executable()
        outside = self.root / "user-data"
        outside.mkdir(mode=0o700)
        marker = outside / "keep"
        marker.write_bytes(b"independent marker\n")
        managed = self.root / "home/.local/share/omp-workbench"
        managed.mkdir(parents=True, mode=0o700)
        (managed / "tools").symlink_to(outside, target_is_directory=True)
        # Force installation, rather than the unrelated existing-program branch.
        binary.unlink()
        with patch("workbench.backend.install_recipe._open_https", side_effect=AssertionError("unsafe prefix must fail before network")):
            code, result, err = self.cli()
        self.assertEqual(code, 1, err)
        self.assertEqual(result["state"], "failed")
        self.assertFalse(self.record.exists())
        self.assertTrue((managed / "tools").is_symlink())
        self.assertEqual({p.name: p.read_bytes() for p in outside.iterdir()}, {"keep": b"independent marker\n"})

    def test_downloaded_recipe_version_mismatch_never_publishes_ready(self):
        artifact = self.executable("download-payload", version="61.2.3").read_bytes()
        metadata = {"build": {"version": "61.2.4", "channel": "stable"},
                    "file": {"platform": "linux", "arch": "x86_64", "name": "omp-linux-x64",
                             "sha256": sha256(artifact).hexdigest(), "size": len(artifact)},
                    "download": "https://build.stencil.so/independent/omp"}
        def transport(url, **kwargs):
            data = json.dumps(metadata).encode() if "/latest/" in url else artifact
            response = BytesIO(data)
            response.geturl = lambda: url
            return response
        with patch("workbench.backend.install_recipe._open_https", side_effect=transport):
            code, result, err = self.cli()
        self.assertEqual(code, 1, err)
        self.assertEqual(result["reason"], "omp_changed_or_sdk_version_mismatch")
        self.assertFalse(self.record.exists())
        self.assertEqual(list(setup_paths(self.env).tools.iterdir()), [])

    @unittest.skipUnless(os.environ.get("WORKBENCH_TEST_INSTALLED_WB") and os.environ.get("WORKBENCH_TEST_VERIFIED_OMP"),
                         "requires retained installed WB and independently verified official OMP")
    def test_installed_cli_loads_real_sdk_with_no_source_path_or_project_initialization(self):
        command = Path(os.environ["WORKBENCH_TEST_INSTALLED_WB"])
        binary = Path(os.environ["WORKBENCH_TEST_VERIFIED_OMP"])
        original = sha256(binary.read_bytes()).hexdigest()
        process = subprocess.Popen([str(command), "setup", "--omp", str(binary), "--json"],
                                   cwd=self.root / "research", env=self.env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            out, err = process.communicate(timeout=90)
        except BaseException:
            try:
                os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                pass
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.communicate(timeout=10)
            raise
        self.assertEqual(process.returncode, 0, out + err)
        result = json.loads(out)
        self.assertEqual((result["state"], result["status"], result["path"]), ("ready", "reused", str(binary)))
        self.assertEqual(result["sdk"]["status"], "loaded")
        self.assertEqual(result["version"], result["sdk"]["version"])
        self.assertEqual(result["real_models"], "unverified")
        self.assertIn("not checked", result["login_guidance"])
        self.assertEqual(list((self.root / "research").iterdir()), [])
        self.assertEqual(list((self.root / "tmp").iterdir()), [])
        self.assertFalse((self.root / "home/.omp").exists())
        self.assertEqual(sha256(binary.read_bytes()).hexdigest(), original)


if __name__ == "__main__":
    unittest.main()
