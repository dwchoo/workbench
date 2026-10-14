"""Process-owned 병렬·비용 비교의 독립 fixture. Native OMP 근거는 아니다."""
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools import dev_validate as runner


MODULE = "tests.dev_validation.test_parallel_cost_independent"
SENSITIVE = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TMUX", "HERDR_SOCKET", "SSH_AUTH_SOCK")


def _fixture_suite():
    """unittest loads this callable explicitly; discovery does not run it."""
    def observe():
        key = os.environ["WB_DVL_FIXTURE_KEY"]
        receipts = Path(os.environ["WB_DVL_FIXTURE_RECEIPTS"])
        started = time.monotonic()
        observed = {"pid": os.getpid(), "ticks": runner.harness.ticks(os.getpid()),
                    "parentPid": os.getppid(), "home": os.environ["HOME"],
                    "namespace": os.environ["WB_DVL_NAMESPACE"], "started": started,
                    "sensitivePresent": [name for name in SENSITIVE if name in os.environ],
                    "proxy": {name: os.environ.get(name) for name in runner.harness.PROXY_KEYS},
                    "sigtermBlocked": signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, set())}
        (receipts / f"{key}.ready.json").write_text(json.dumps(observed))
        mode = os.environ.get("WB_DVL_FIXTURE_MODE")
        if mode == "owned-orphan":
            orphan_code = """
import json,os,sys,time
from pathlib import Path
from tools import dev_validate as r
child=os.fork()
if child==0:
 os.chdir('/')
 Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'ticks':r.harness.ticks(os.getpid()),
                                        'session':os.getsid(0)}))
 time.sleep(15)
 os._exit(0)
"""
            subprocess.run([sys.executable, "-B", "-c", orphan_code, str(receipts / f"{key}.orphan.json")],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=True)
        if mode in ("hold", "spawn-gap"):
            time.sleep(15)
        else:
            time.sleep(0.5)  # controlled overlap fixture, not a product semantic wait
        observed["finished"] = time.monotonic()
        print("FIXTURE_OBSERVATION=" + json.dumps(observed), flush=True)
        scenario = "fixture-" + key
        report = Path(os.environ["WB_CW16_REPORT_DIR"]) / os.environ["WB_CW16_RUN_ID"]
        report.mkdir(parents=True)
        (report / f"{scenario}.json").write_text(json.dumps({
            "run_id": os.environ["WB_CW16_RUN_ID"], "scenario": scenario, "result": "fail" if mode == "fail" else "pass",
            "steps": {"observed": {"status": "fail" if mode == "fail" else "pass"}, "finished": {"status": "pass"}},
            "cleanup": {"root_removed": True, "shutdown_exit": 0}, "provider": {"errors": []}}))
    return unittest.FunctionTestCase(observe)


def _install_worker_fixture_hook(item):
    """Observe real results or deliver a kernel signal at the approved spawn boundary."""
    mode = item.env["WB_DVL_FIXTURE_MODE"]
    receipts = Path(item.env["WB_DVL_FIXTURE_RECEIPTS"])
    key = item.env["WB_DVL_FIXTURE_KEY"]
    if mode == "fail":
        original = runner._run_item
        def observed_result(*args, **kwargs):
            row = original(*args, **kwargs)
            (receipts / f"{key}.original-result.json").write_text(json.dumps(row))
            return row
        runner._run_item = observed_result
    elif mode == "spawn-gap":
        original = runner.harness.ticks
        fired = False
        def observe_ticks(pid):
            nonlocal fired
            ticks = original(pid)
            fields = runner.harness.stat_fields(pid)
            if not fired and ticks is not None and fields is not None and int(fields[1]) == os.getpid():
                fired = True
                values = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                namespace = next(value.split(b"=", 1)[1].decode() for value in values
                                 if value.startswith(b"WB_DVL_NAMESPACE="))
                (receipts / f"{key}.spawn-gap.json").write_text(json.dumps({
                    "pid": pid, "ticks": ticks, "session": os.getsid(pid), "namespace": namespace,
                    "workerPid": os.getpid(), "workerTicks": original(os.getpid())}))
                os.kill(os.getpid(), signal.SIGTERM)
            return ticks
        runner.harness.ticks = observe_ticks


WORKER_BOOT = """
import json, os, sys
from pathlib import Path
from hashlib import sha256
from tools import dev_validate as r
payload = json.loads(sys.argv[1])
item = r.Item(**payload)
args = sys.argv[2:]
namespace = Path(args[6])
receipts = Path(item.env['WB_DVL_FIXTURE_RECEIPTS'])
key = item.env['WB_DVL_FIXTURE_KEY']
def snapshot():
 return {'pid':os.getpid(), 'ticks':r.harness.ticks(os.getpid()), 'home':os.environ['HOME'],
         'namespace':str(namespace), 'sensitivePresent':[k for k in
         ('OPENAI_API_KEY','ANTHROPIC_API_KEY','TMUX','HERDR_SOCKET','SSH_AUTH_SOCK') if k in os.environ],
         'sentinelSha256':sha256((namespace/'sentinel').read_bytes()).hexdigest()}
(receipts/(key+'.worker-before.json')).write_text(json.dumps(snapshot()))
from tests.dev_validation.test_parallel_cost_independent import _install_worker_fixture_hook
_install_worker_fixture_hook(item)
code = r._cell_worker_main(item, int(args[0]), args[1], args[2], int(args[3]), int(args[4]),
                           int(args[5].split(',')[0]), int(args[5].split(',')[1]),
                           namespace, Path(args[7]), Path(args[8]))
(receipts/(key+'.worker-after.json')).write_text(json.dumps(snapshot()))
raise SystemExit(code)
"""


def fixture_worker_argv(item, index, invocation, nonce, parent_pid, parent_start,
                        control_fd, result_fd, namespace, report_dir, omp):
    # Substitute the external worker command only to inject a fixture Item into
    # the fresh interpreter. Production worker, supervisor and cleanup run unchanged.
    return [sys.executable, "-B", "-c", WORKER_BOOT, json.dumps(asdict(item)), str(index),
            invocation, nonce, str(parent_pid), str(parent_start), f"{control_fd},{result_fd}",
            str(namespace), str(report_dir), str(omp)]


def _stop_fixture_identity(pid, ticks):
    """Fixture-owner fallback: only generated exact PID/ticks; never namespace-based signalling."""
    if runner.harness.alive(pid, ticks):
        runner.harness.kill_exact(pid, ticks)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        fields = runner.harness.stat_fields(pid)
        if fields is None or int(fields[19]) != ticks:
            return
        time.sleep(0.02)
    if runner.harness.alive(pid, ticks):
        raise AssertionError(f"fixture could not stop exact owned pid {pid}")


class ParallelCostIndependent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        output = os.environ.get("WB_DVL_PRECISE_TEST_OUTPUT")
        if output and not Path(output).is_absolute():
            raise AssertionError("output must be absolute before import")
        cls.root = Path(tempfile.mkdtemp(prefix="parallel-proof-", dir=output))
        if not output:
            import shutil
            cls.addClassCleanup(shutil.rmtree, cls.root)
        cls.receipts = cls.root / "receipts"
        cls.receipts.mkdir(mode=0o700)
        cls.items = tuple(cls.make_item(key) for key in ("a", "b"))
        cls.omp = Path("/usr/bin/true")
        serial_dir, parallel_dir = cls.root / "serial", cls.root / "parallel"
        serial_dir.mkdir(mode=0o700)
        parallel_dir.mkdir(mode=0o700)
        start = time.monotonic()
        cls.serial = [runner._run_item(item, i, serial_dir, cls.omp, "independent-serial")
                      for i, item in enumerate(cls.items)]
        cls.serial_wall = time.monotonic() - start
        hostile = {name: "private-fixture-marker" for name in SENSITIVE}
        hostile["HOME"] = str(cls.root / "unrelated-home")
        start = time.monotonic()
        with patch.dict(os.environ, hostile), patch.object(runner, "_worker_argv", side_effect=fixture_worker_argv):
            cls.parallel, cls.interrupted = runner._run_cell_batch(list(enumerate(cls.items)), parallel_dir,
                                                                 cls.omp, "independent-parallel")
        cls.parallel_wall = time.monotonic() - start
        identity = runner._input_manifest()
        cls.source = {"sha256": identity["sha256"], "fileCount": identity["fileCount"]}
        cls.cost_identity = runner._cost_identity(cls.items, cls.omp)
        cls.before = cls.report(cls.serial, cls.serial_wall, jobs=1, attempt="fixture-before")
        cls.after = cls.report(cls.parallel, cls.parallel_wall, jobs=2, attempt="fixture-after")
        cls.baseline = cls.root / "baseline.json"
        cls.baseline.write_text(json.dumps(cls.before))
        (cls.root / "current.json").write_text(json.dumps(cls.after))

    @classmethod
    def make_item(cls, key, mode="pulse"):
        return runner.Item("fixture-" + key, ("DV-AC-02", "DV-AC-04", "DV-AC-05"),
                           (MODULE + "._fixture_suite",), "fixture",
                           env={"WB_DVL_FIXTURE_KEY": key, "WB_DVL_FIXTURE_MODE": mode,
                                "WB_DVL_FIXTURE_RECEIPTS": str(cls.receipts)},
                           scenario_steps={"fixture-" + key: ("observed", "finished")})

    @classmethod
    def report(cls, rows, wall, *, jobs, attempt):
        pending = [{"id": key, "required": True, "status": "not_run", "collectedTests": 0,
                    "durationSeconds": 0} for key in runner.PRECISE_PENDING]
        first = {"item": "q01_product_adapter_recheck", "status": "not_run", "attemptId": attempt}
        return {"schemaVersion": 1, "attemptId": attempt, "inputBefore": cls.source,
                "inputAfter": cls.source, "versionsBefore": {"python": sys.version, "omp": "fixture only"},
                "versionsAfter": {"python": sys.version, "omp": "fixture only"},
                "ompPath": str(cls.omp), "mode": "precise", "cwd": str(runner.REPO),
                "costIdentity": cls.cost_identity, "execution": {"jobs": jobs},
                "environment": {"privateNamespace": str(cls.root / attempt)},
                "items": [*rows, *pending], "elapsedSeconds": wall, "result": "inconclusive",
                "firstFailure": first}

    def compare(self, *, before=None, after=None):
        path = self.root / "comparison-baseline.json"
        path.write_text(json.dumps(self.before if before is None else before))
        return runner._compare_cost(self.after if after is None else after, path)

    def test_00_real_workers_overlap_clean_environment_and_persist_sentinel_provenance(self):
        self.assertFalse(self.interrupted)
        observations = []
        workers = []
        for row in self.parallel:
            self.assertEqual((row["status"], row["collectedTests"], row["skippedTests"]), ("passed", 1, 0), row)
            self.assertEqual(row["cleanup"]["remaining"], [])
            self.assertTrue(row["cleanup"]["namespaceRemoved"])
            meta = row["cellWorker"]
            workers.append(meta)
            key = row["id"].removeprefix("fixture-")
            before = json.loads((self.receipts / f"{key}.worker-before.json").read_text())
            after = json.loads((self.receipts / f"{key}.worker-after.json").read_text())
            ready = json.loads((self.receipts / f"{key}.ready.json").read_text())
            observations.append(json.loads(row["stdoutTail"].split("FIXTURE_OBSERVATION=", 1)[1].splitlines()[0]))
            self.assertEqual(before, after)
            self.assertEqual(before["sentinelSha256"], meta["sentinelBeforeSha256"])
            self.assertEqual(after["sentinelSha256"], meta["sentinelAfterSha256"])
            self.assertEqual((before["pid"], before["ticks"]), (meta["pid"], meta["startTicks"]))
            self.assertEqual(ready["parentPid"], meta["pid"])
            self.assertEqual(meta["ownedIdentities"][ready["pid"]], ready["ticks"])
            self.assertEqual(before["home"], str(Path(before["namespace"]) / "home"))
            self.assertEqual(before["sensitivePresent"], [])
            self.assertEqual(ready["sensitivePresent"], [])
            self.assertTrue(all(value == runner.harness.BLOCKED_PROXY for value in ready["proxy"].values()))
            self.assertEqual(meta["remaining"], [])
            self.assertTrue(meta["namespaceRemoved"])
            self.assertFalse(runner.harness.alive(meta["pid"], meta["startTicks"]))
            self.assertFalse(runner.harness.alive(ready["pid"], ready["ticks"]))
        self.assertNotEqual(workers[0]["pid"], workers[1]["pid"])
        self.assertNotEqual(workers[0]["home"], workers[1]["home"])
        self.assertLess(max(value["started"] for value in observations),
                        min(value["finished"] for value in observations))

    def test_01_worker_signal_cleans_owned_driver_and_spares_unrelated_process(self):
        key = "interruption"
        ready = self.receipts / f"{key}.ready.json"
        before_record = self.receipts / f"{key}.worker-before.json"
        marker = self.root / "unrelated-marker"
        marker.write_bytes(b"owned unrelated sentinel")
        marker_before = {"sha256": sha256(marker.read_bytes()).hexdigest(), "mtime": marker.stat().st_mtime_ns}
        unrelated = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                     env=runner.harness.clean_base_env(PYTHONPATH=str(runner.REPO / "src")))
        unrelated_ticks = runner.harness.ticks(unrelated.pid)
        signal_code = """
import json,sys,time,signal
from pathlib import Path
from tools import dev_validate as r
ready,worker=map(Path,sys.argv[1:])
deadline=time.monotonic()+10
while not ready.exists() and time.monotonic()<deadline: time.sleep(.02)
assert ready.exists(), 'fixture driver did not start'
identity=json.loads(worker.read_text())
assert r.harness.kill_exact(identity['pid'],identity['ticks'],signal.SIGTERM)
"""
        signaler = subprocess.Popen([sys.executable, "-B", "-c", signal_code, str(ready), str(before_record)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=runner.harness.clean_base_env(PYTHONPATH=str(runner.REPO / "src")))
        output = self.root / "interrupted"
        output.mkdir(mode=0o700)
        try:
            with patch.object(runner, "_worker_argv", side_effect=fixture_worker_argv):
                rows, _ = runner._run_cell_batch([(0, self.make_item(key, "hold"))], output,
                                                 self.omp, "fixture-interrupted")
            _, stderr = signaler.communicate(timeout=12)
            self.assertEqual(signaler.returncode, 0, stderr)
            row, = rows
            self.assertEqual(row["status"], "inconclusive", row)
            self.assertEqual(row["cleanup"]["remaining"], [])
            self.assertTrue(row["cleanup"]["namespaceRemoved"])
            self.assertEqual(row["cellWorker"]["remaining"], [])
            self.assertTrue(row["cellWorker"]["namespaceRemoved"])
            driver = json.loads(ready.read_text())
            self.assertFalse(runner.harness.alive(driver["pid"], driver["ticks"]))
            self.assertTrue(runner.harness.alive(unrelated.pid, unrelated_ticks))
            marker_after = {"sha256": sha256(marker.read_bytes()).hexdigest(), "mtime": marker.stat().st_mtime_ns}
            self.assertEqual(marker_after, marker_before)
            (self.root / "interruption-result.json").write_text(json.dumps({
                "row": row, "unrelatedPid": unrelated.pid, "unrelatedTicks": unrelated_ticks,
                "markerBefore": marker_before, "markerAfter": marker_after}))
        finally:
            for proc in (signaler, unrelated):
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=5)

    def test_02_scheduler_preserves_inventory_and_only_pairs_tmux(self):
        batches = []
        def batch(items, *args):
            batches.append([item.key for _, item in items])
            return [{"id": item.key, "status": "passed"} for _, item in items], False
        with patch.object(runner, "_run_cell_batch", side_effect=batch):
            rows = runner._parallel_items(runner.PRECISE_ITEMS, self.root, self.omp, "fixture", 3)
        self.assertEqual([row["id"] for row in rows], [item.key for item in runner.PRECISE_ITEMS])
        self.assertEqual([items for items in batches if len(items) == 2],
                         [["compat_tmux_bash", "compat_tmux_dash"]])
        self.assertTrue(all(len(items) == 1 for items in batches if any("herdr" in name for name in items)))

    def test_03_core_cost_comparable_pending_gates_wall_sum_and_first_failure_preserved(self):
        comparison = self.compare()
        self.assertEqual(comparison["status"], "comparable", comparison)
        self.assertEqual((comparison["baselineJobs"], comparison["currentJobs"]), (1, 2))
        self.assertEqual(comparison["before"]["wallSeconds"], self.serial_wall)
        self.assertEqual(comparison["after"]["wallSeconds"], self.parallel_wall)
        self.assertEqual(comparison["after"]["itemSumSeconds"],
                         round(sum(row["durationSeconds"] for row in self.parallel), 3))
        self.assertEqual(comparison["baselineFirstFailure"], self.before["firstFailure"])
        self.assertEqual(runner._overall(self.after["items"], self.after["firstFailure"]), ("inconclusive", 2))

    def test_04_cost_rejects_changed_positive_collection_count(self):
        current = deepcopy(self.after)
        current["items"][0]["collectedTests"] = 2
        comparison = self.compare(after=current)
        self.assertEqual(comparison["status"], "unknown", comparison)

    def test_05_cost_rejects_changed_source_tools_scope_and_missing_descriptor(self):
        changes = (
            lambda value: value["inputBefore"].update(sha256="other"),
            lambda value: value["versionsAfter"].update(omp="changed"),
            lambda value: value.update(ompPath="/different/omp"),
            lambda value: value.update(mode="fast"),
            lambda value: value.update(cwd="/different"),
            lambda value: value["costIdentity"]["environmentTemplate"].update(PATH="/different"),
            lambda value: value["costIdentity"]["selectedItems"][0].update(testIds=["other"]),
            lambda value: value["costIdentity"]["selectedItems"][0].update(assertionIds=["other"]),
            lambda value: value["costIdentity"]["selectedItems"][0].update(requiredSteps={}),
            lambda value: value.pop("costIdentity"),
        )
        for index, mutate in enumerate(changes):
            with self.subTest(index=index):
                baseline = deepcopy(self.before)
                mutate(baseline)
                self.assertEqual(self.compare(before=baseline)["status"], "unknown")

    def test_06_cost_rejects_failed_unknown_zero_skip_and_missing_or_failed_cleanup(self):
        changes = (
            lambda row: row.update(status="failed"),
            lambda row: row.update(status="inconclusive"),
            lambda row: row.update(collectedTests=0),
            lambda row: row.update(skippedTests=1),
            lambda row: row.pop("cleanup"),
            lambda row: row["cleanup"].pop("remaining"),
            lambda row: row["cleanup"].update(remaining=[999]),
            lambda row: row["cleanup"].update(namespaceRemoved=False),
        )
        for index, mutate in enumerate(changes):
            with self.subTest(index=index):
                baseline = deepcopy(self.before)
                mutate(baseline["items"][0])
                self.assertEqual(self.compare(before=baseline)["status"], "unknown")

    def test_07_main_default_and_fast_jobs_two_keep_direct_serial_dispatch(self):
        for mode, jobs in (("precise", None), ("fast", 2)):
            report_dir = self.root / ("main-" + mode)
            order = []
            def direct(item, *args):
                order.append(item.key)
                return {"id": item.key, "required": True, "status": "passed",
                        "durationSeconds": 0, "expectedWaitSeconds": item.expected_wait_seconds}
            argv = ["--mode", mode, "--report-dir", str(report_dir), "--omp", str(self.omp)]
            if jobs:
                argv.extend(("--jobs", str(jobs)))
            with patch.object(runner, "_run_item", side_effect=direct), \
                    patch.object(runner, "_parallel_items", side_effect=AssertionError("must remain serial")), \
                    patch.object(runner, "_input_manifest", return_value=self.source), \
                    patch.object(runner.harness, "tool_versions", return_value={"omp": "fixture"}), \
                    redirect_stdout(io.StringIO()):
                code = runner.main(argv)
            expected = runner.FAST_ITEMS + (runner.PRECISE_ITEMS if mode == "precise" else ())
            self.assertEqual(order, [item.key for item in expected])
            self.assertEqual(code, 2 if mode == "precise" else 0)

    def test_10_g1_confirmed_failure_survives_supervisor_sigterm(self):
        failed_key, live_key = "g1-failed", "g1-live"
        original_path = self.receipts / f"{failed_key}.original-result.json"
        worker_path = self.receipts / f"{failed_key}.worker-before.json"
        worker_done = self.receipts / f"{failed_key}.worker-after.json"
        live_ready = self.receipts / f"{live_key}.ready.json"
        code = """
import json,sys,time,signal
from pathlib import Path
from tools import dev_validate as r
worker,done,ready=map(Path,sys.argv[1:4]); parent=int(sys.argv[4]); ticks=int(sys.argv[5])
deadline=time.monotonic()+10
while time.monotonic()<deadline:
 if done.exists() and ready.exists():
  pid=json.loads(worker.read_text())['pid']
  if r.harness.stat_fields(pid) is None: break
 time.sleep(.02)
else: raise AssertionError('completed failed worker not reaped before signal')
assert r.harness.kill_exact(parent,ticks,signal.SIGTERM)
"""
        signaler = subprocess.Popen([sys.executable, "-B", "-c", code, str(worker_path), str(worker_done),
                                     str(live_ready), str(os.getpid()), str(runner.harness.ticks(os.getpid()))],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=runner.harness.clean_base_env(PYTHONPATH=str(runner.REPO / "src")))
        output = self.root / "g1"
        output.mkdir(mode=0o700)
        try:
            with runner._interrupt_signals(), patch.object(runner, "_worker_argv", side_effect=fixture_worker_argv):
                rows, interrupted = runner._run_cell_batch(
                    [(0, self.make_item(failed_key, "fail")), (1, self.make_item(live_key, "hold"))],
                    output, self.omp, "g1-supervisor-interruption")
            _, stderr = signaler.communicate(timeout=12)
            self.assertEqual(signaler.returncode, 0, stderr)
            original = json.loads(original_path.read_text())
            (self.root / "g1-result.json").write_text(json.dumps({"original": original, "rows": rows,
                                                                 "interrupted": interrupted}))
            self.assertTrue(interrupted)
            self.assertEqual(original["status"], "failed")
            self.assertEqual(rows[0]["status"], "failed", rows)
            self.assertEqual(rows[0]["reason"], original["reason"])
            self.assertEqual(runner._overall(rows, None), ("failed", 1))
            self.assertNotEqual(rows[1]["status"], "passed")
        finally:
            if signaler.poll() is None:
                signaler.terminate()
            signaler.wait(timeout=5)

    def test_11_g2_actual_worker_signal_after_popen_cleans_driver_without_supervisor_fallback(self):
        key = "g2-gap"
        item = self.make_item(key, "spawn-gap")
        namespace = self.root / "g2-worker"
        namespace.mkdir(mode=0o700)
        (namespace / "sentinel").write_bytes(b"g2 actual observed sentinel")
        output = self.root / "g2-reports"
        output.mkdir(mode=0o700)
        control_read, control_write = os.pipe()
        result_read, result_write = os.pipe()
        proc = None
        gap = None
        with runner._owned_subreaper():
            try:
                argv = fixture_worker_argv(item, 0, "g2", "g2-nonce", os.getpid(), runner.harness.ticks(os.getpid()),
                                           control_write, result_write, namespace, output, self.omp)
                proc = subprocess.Popen(argv, cwd=runner.REPO,
                                        env=runner._base_env(namespace / "home", namespace, self.omp, item),
                                        pass_fds=(control_write, result_write), stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True)
                os.close(control_write); control_write = None
                os.close(result_write); result_write = None
                _, stderr = proc.communicate(timeout=8)
                self.assertEqual(proc.returncode, 0, stderr)
                with os.fdopen(result_read, "rb") as stream:
                    envelope = json.loads(stream.read())
                result_read = None
                gap = json.loads((self.receipts / f"{key}.spawn-gap.json").read_text())
                members = runner.harness.session_members((gap["session"],))
                (self.root / "g2-result.json").write_text(json.dumps({"envelope": envelope, "gap": gap,
                    "driverAliveBeforeFixtureFallback": runner.harness.alive(gap["pid"], gap["ticks"]),
                    "driverSessionMembersBeforeFixtureFallback": members,
                    "namespacePresentBeforeFixtureFallback": Path(gap["namespace"]).exists()}))
                for ordinary in ("a", "b"):
                    self.assertFalse(json.loads((self.receipts / f"{ordinary}.ready.json").read_text())["sigtermBlocked"])
                self.assertFalse(runner.harness.alive(gap["pid"], gap["ticks"]), envelope)
                self.assertEqual(members, {})
                self.assertFalse(Path(gap["namespace"]).exists())
            finally:
                if proc is not None:
                    if proc.poll() is None:
                        proc.terminate()
                    proc.wait(timeout=5)
                if gap is None and (self.receipts / f"{key}.spawn-gap.json").exists():
                    gap = json.loads((self.receipts / f"{key}.spawn-gap.json").read_text())
                if gap:
                    for pid, ticks in runner.harness.session_members((gap["session"],)).items():
                        _stop_fixture_identity(pid, ticks)
                    _stop_fixture_identity(gap["pid"], gap["ticks"])
                    shutil.rmtree(gap["namespace"], ignore_errors=True)
                for fd in (control_read, control_write, result_read, result_write):
                    if fd is not None:
                        os.close(fd)
                shutil.rmtree(namespace, ignore_errors=True)

    def test_12_s1_unrelated_adoption_survives_and_owned_driver_session_orphan_is_cleaned(self):
        key = "s1-owned"
        unknown_path = self.root / "s1-unrelated.json"
        release = self.root / "s1-release-parent"
        marker = self.root / "s1-unrelated-marker"
        marker.write_bytes(b"controlled unrelated marker")
        marker_before = sha256(marker.read_bytes()).hexdigest()
        ancestor_code = """
import json,os,sys,time
from pathlib import Path
from tools import dev_validate as r
receipt,release=map(Path,sys.argv[1:])
child=os.fork()
if child==0:
 os.setsid(); os.chdir('/')
 receipt.write_text(json.dumps({'pid':os.getpid(),'ticks':r.harness.ticks(os.getpid()),'parent':os.getppid()}))
 time.sleep(15); os._exit(0)
deadline=time.monotonic()+10
while not release.exists() and time.monotonic()<deadline: time.sleep(.02)
"""
        release_code = """
import sys,time
from pathlib import Path
ready,release=map(Path,sys.argv[1:]); deadline=time.monotonic()+10
while not ready.exists() and time.monotonic()<deadline: time.sleep(.02)
assert ready.exists(), 'owned session orphan never created'
release.write_bytes(b'release controlled ancestor')
"""
        unknown = owned = None
        ancestor = releaser = None
        output = self.root / "s1"
        output.mkdir(mode=0o700)
        with runner._owned_subreaper():
            try:
                env = runner.harness.clean_base_env(PYTHONPATH=str(runner.REPO / "src"))
                ancestor = subprocess.Popen([sys.executable, "-B", "-c", ancestor_code, str(unknown_path), str(release)],
                                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + 5
                while not unknown_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(unknown_path.exists())
                unknown = json.loads(unknown_path.read_text())
                self.assertEqual(unknown["parent"], ancestor.pid)
                releaser = subprocess.Popen([sys.executable, "-B", "-c", release_code,
                                             str(self.receipts / f"{key}.orphan.json"), str(release)],
                                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                with patch.object(runner, "_worker_argv", side_effect=fixture_worker_argv):
                    rows, interrupted = runner._run_cell_batch([(0, self.make_item(key, "owned-orphan"))],
                                                             output, self.omp, "s1-adoption")
                _, stderr = releaser.communicate(timeout=12)
                self.assertEqual(releaser.returncode, 0, stderr)
                ancestor.wait(timeout=5)
                owned = json.loads((self.receipts / f"{key}.orphan.json").read_text())
                driver = json.loads((self.receipts / f"{key}.ready.json").read_text())
                self.assertEqual(owned["session"], driver["pid"])
                unknown_alive = runner.harness.alive(unknown["pid"], unknown["ticks"])
                owned_alive = runner.harness.alive(owned["pid"], owned["ticks"])
                (self.root / "s1-result.json").write_text(json.dumps({"rows": rows, "interrupted": interrupted,
                    "unrelated": unknown, "unrelatedSurvivedSourceCleanup": unknown_alive,
                    "ownedOrphan": owned, "ownedOrphanSurvivedSourceCleanup": owned_alive,
                    "markerBefore": marker_before, "markerAfter": sha256(marker.read_bytes()).hexdigest()}))
                self.assertFalse(owned_alive, "corroborated driver-session orphan must be cleaned")
                self.assertTrue(unknown_alive, "unrelated adopted B was incorrectly granted worker kill ownership")
                self.assertNotIn(unknown["pid"], rows[0]["cellWorker"]["signalledPids"])
                self.assertEqual(sha256(marker.read_bytes()).hexdigest(), marker_before)
            finally:
                for proc in (releaser, ancestor):
                    if proc is not None:
                        if proc.poll() is None:
                            proc.terminate()
                        proc.wait(timeout=5)
                if owned is None and (self.receipts / f"{key}.orphan.json").exists():
                    owned = json.loads((self.receipts / f"{key}.orphan.json").read_text())
                for receipt in (unknown, owned):
                    if receipt:
                        _stop_fixture_identity(receipt["pid"], receipt["ticks"])

    def test_13_s2_worker_sentinel_digest_presence_format_and_equality_are_required(self):
        self.assertEqual(self.compare(before=self.after)["status"], "comparable")
        changes = (
            ("missing", lambda worker: (worker.pop("sentinelBeforeSha256"), worker.pop("sentinelAfterSha256"))),
            ("null", lambda worker: worker.update(sentinelBeforeSha256=None, sentinelAfterSha256=None)),
            ("invalid_hex", lambda worker: worker.update(sentinelBeforeSha256="g" * 64, sentinelAfterSha256="g" * 64)),
            ("uppercase", lambda worker: worker.update(sentinelBeforeSha256="A" * 64, sentinelAfterSha256="A" * 64)),
            ("wrong_length", lambda worker: worker.update(sentinelBeforeSha256="a" * 63, sentinelAfterSha256="a" * 63)),
            ("different", lambda worker: worker.update(sentinelBeforeSha256="a" * 64, sentinelAfterSha256="b" * 64)),
        )
        observations = []
        try:
            for name, mutate in changes:
                with self.subTest(case=name):
                    baseline = deepcopy(self.after)
                    mutate(baseline["items"][0]["cellWorker"])
                    comparison = self.compare(before=baseline)
                    observations.append({"case": name, "comparison": comparison})
                    self.assertEqual(comparison["status"], "unknown", comparison)
        finally:
            (self.root / "s2-result.json").write_text(json.dumps(observations))

    def test_14_late_parent_death_after_closed_pipes_still_cleans_orphan_and_returns_clean_result(self):
        key = "late-parent-death"
        item = self.make_item(key, "owned-orphan")
        namespace = self.root / "late-worker"
        namespace.mkdir(mode=0o700)
        (namespace / "sentinel").write_bytes(b"late cleanup fixture sentinel")
        output = self.root / "late-reports"
        output.mkdir(mode=0o700)
        supervisor_record = self.root / "late-supervisor.json"
        boundary_record = self.root / "late-boundary.json"
        stderr_path = self.root / "late-worker.stderr"
        trace_setup = """
import signal,time
_late_fired=False
def _late_boundary_trace(frame,event,arg):
 global _late_fired
 if not _late_fired and event=='line' and frame.f_code.co_name=='_run_item_body':
  local=frame.f_locals; lifetime=local.get('lifetime',{}); proc=local.get('proc')
  if proc is not None and 'collected' in local and 'remaining_before' not in local and proc.stdout.closed and proc.stderr.closed:
   fd_closed=False
   try: os.fstat(local['control_read'])
   except OSError: fd_closed=True
   if fd_closed:
    _late_fired=True
    orphan=json.loads((receipts/(key+'.orphan.json')).read_text())
    record={'workerPid':os.getpid(),'workerTicks':r.harness.ticks(os.getpid()),
            'supervisorPid':int(args[3]),'supervisorTicks':int(args[4]),
            'driverPid':proc.pid,'driverExited':proc.poll() is not None,
            'stdoutClosed':proc.stdout.closed,'stderrClosed':proc.stderr.closed,
            'controlFdClosed':fd_closed,'cleanupDoneObserved':lifetime.get('cleanupDone'),
            'namespace':str(lifetime['namespace']),'orphan':orphan,
            'orphanAliveAtBoundary':r.harness.alive(orphan['pid'],orphan['ticks'])}
    Path(item.env['WB_DVL_LATE_BOUNDARY']).write_text(json.dumps(record))
    assert os.getppid()==int(args[3]), 'fixture supervisor identity changed'
    assert r.harness.kill_exact(int(args[3]),int(args[4]),signal.SIGKILL)
    time.sleep(2)  # kernel PDEATHSIG must interrupt this real worker
 return _late_boundary_trace
"""
        worker_code = trace_setup + WORKER_BOOT.replace(
            "code = r._cell_worker_main", "sys.settrace(_late_boundary_trace)\ncode = r._cell_worker_main")
        item = runner.Item(**{**asdict(item), "env": {**item.env, "WB_DVL_LATE_BOUNDARY": str(boundary_record)}})
        supervisor_code = """
import json,os,subprocess,sys
from pathlib import Path
from tools import dev_validate as r
item=r.Item(**json.loads(sys.argv[1])); worker_code=sys.argv[2]
namespace,output,record,stderr_path=map(Path,sys.argv[3:7])
control_fd,result_fd=map(int,sys.argv[7:9])
parent=os.getpid(); parent_ticks=r.harness.ticks(parent)
argv=[sys.executable,'-B','-c',worker_code,json.dumps(json.loads(sys.argv[1])),
      '0','late-parent-death','late-nonce',str(parent),str(parent_ticks),
      str(control_fd)+','+str(result_fd),str(namespace),str(output),'/usr/bin/true']
with stderr_path.open('wb') as errors:
 proc=subprocess.Popen(argv,cwd=r.REPO,env=r._base_env(namespace/'home',namespace,Path('/usr/bin/true'),item),
                       pass_fds=(control_fd,result_fd),stdout=subprocess.DEVNULL,stderr=errors,start_new_session=True)
 os.close(control_fd); os.close(result_fd)
 record.write_text(json.dumps({'supervisorPid':parent,'supervisorTicks':parent_ticks,
                               'workerPid':proc.pid,'workerTicks':r.harness.ticks(proc.pid)}))
 proc.wait(timeout=12)
"""
        control_read, control_write = os.pipe()
        result_read, result_write = os.pipe()
        supervisor = None
        identity = boundary = None
        worker_exit = None
        with runner._owned_subreaper():
            try:
                supervisor = subprocess.Popen(
                    [sys.executable, "-B", "-c", supervisor_code, json.dumps(asdict(item)), worker_code,
                     str(namespace), str(output), str(supervisor_record), str(stderr_path),
                     str(control_write), str(result_write)], cwd=runner.REPO,
                    env=runner.harness.clean_base_env(PYTHONPATH=str(runner.REPO / "src")),
                    pass_fds=(control_write, result_write), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True)
                os.close(control_write); control_write = None
                os.close(result_write); result_write = None
                _, supervisor_stderr = supervisor.communicate(timeout=10)
                self.assertEqual(supervisor.returncode, -signal.SIGKILL, supervisor_stderr)
                identity = json.loads(supervisor_record.read_text())
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    waited, status = os.waitpid(identity["workerPid"], os.WNOHANG)
                    if waited:
                        worker_exit = os.waitstatus_to_exitcode(status)
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(worker_exit, "orphaned fixture worker did not terminate")
                with os.fdopen(result_read, "rb") as stream:
                    envelope = json.loads(stream.read())
                result_read = None
                boundary = json.loads(boundary_record.read_text())
                orphan = boundary["orphan"]
                alive = runner.harness.alive(orphan["pid"], orphan["ticks"])
                evidence = {"boundary": boundary, "supervisorExit": supervisor.returncode,
                            "workerExit": worker_exit, "envelope": envelope,
                            "workerStderr": stderr_path.read_text(),
                            "orphanAliveBeforeFixtureFallback": alive,
                            "namespaceExistsBeforeFixtureFallback": Path(boundary["namespace"]).exists()}
                (self.root / "late-parent-death-result.json").write_text(json.dumps(evidence))
                self.assertTrue(boundary["driverExited"])
                self.assertTrue(boundary["stdoutClosed"] and boundary["stderrClosed"] and boundary["controlFdClosed"])
                self.assertEqual(worker_exit, 0, evidence)
                self.assertFalse(alive, evidence)
                row = envelope["result"]
                self.assertEqual(row["status"], "inconclusive", row)
                self.assertEqual(row["cleanup"]["remaining"], [], row)
                self.assertTrue(row["cleanup"]["namespaceRemoved"], row)
                self.assertNotIn("OSError", row.get("reason", ""), row)
                self.assertNotIn("Bad file descriptor", evidence["workerStderr"])
                self.assertFalse(Path(boundary["namespace"]).exists())
            finally:
                if supervisor is not None:
                    if supervisor.poll() is None:
                        supervisor.kill()
                    supervisor.wait(timeout=5)
                if identity is None and supervisor_record.exists():
                    identity = json.loads(supervisor_record.read_text())
                if boundary is None and boundary_record.exists():
                    boundary = json.loads(boundary_record.read_text())
                if identity:
                    _stop_fixture_identity(identity["workerPid"], identity["workerTicks"])
                if boundary:
                    orphan = boundary["orphan"]
                    _stop_fixture_identity(orphan["pid"], orphan["ticks"])
                    shutil.rmtree(boundary["namespace"], ignore_errors=True)
                for fd in (control_read, control_write, result_read, result_write):
                    if fd is not None:
                        os.close(fd)
                shutil.rmtree(namespace, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
