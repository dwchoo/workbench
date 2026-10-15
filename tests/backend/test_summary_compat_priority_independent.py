"""Independent private-helper containment and partial-evidence requirements.

Only the external OMP/Core launch seam is replaced by an owned ordinary Python
program. Kernel process identity/readback, environment construction, runtime copy,
report ownership and wrapper cleanup are actual product behavior. No native/Core
periodic QUESTION or upper SDK capability is asserted by this fixture.
"""
from copy import deepcopy
import ast
from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from workbench.backend import summary_compat_priority as priority
from workbench.backend import summary_compat_provider as provider
from workbench.backend import summary_compat_runtime as runtime
from workbench.backend.summary_compatibility import SummaryCompatibility


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(os.environ["DVL14_PRIORITY_INDEPENDENT_OUTPUT"])

# Ordinary fixture waits for the product's identity record before reporting any
# work. It imports no Workbench/Core/OMP runtime and emits no model request.
HELPER = r'''
import json, os, pathlib, sys, time
root, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
deadline = time.monotonic() + 5
while not (root / 'helper-launch.json').exists():
    if time.monotonic() > deadline: raise RuntimeError('fixture launch identity deadline')
    time.sleep(.005)
launch = json.loads((root / 'helper-launch.json').read_text())
assert launch['pid'] == os.getpid() and launch['pgid'] == os.getpgrp() and launch['start_ticks']
assert os.environ['HOME'] == str(root / 'home') and os.getcwd() == str(root / 'project')
assert os.environ['PYTHONPATH'] == str(root / 'runtime/src')
assert os.environ['GIT_CONFIG_NOSYSTEM'] == '1' and os.environ['GIT_CONFIG_GLOBAL'] == '/dev/null'
assert not any(name in os.environ for name in ('OPENAI_API_KEY','HTTPS_PROXY','SSH_AUTH_SOCK','VIRTUAL_ENV','LD_PRELOAD','PYTHONSTARTUP'))
if mode == 'missing': sys.exit(0)
report = {'kind':'owned_product_priority_partial','result':'inconclusive','POSTs':[],
    'native_launches':0,'coverage_complete':False,'errors':[],'cleanup':{'complete':True},
    'fixture':{'identity_before_work':True,'environment_checked':True,'kind':'ordinary_python_external_launch_seam'}}
if mode == 'survivor':
    refs = root / 'owned-processes.json'
    refs.write_text(json.dumps({'processes':[json.loads(sys.argv[3])]})); refs.chmod(0o600)
if mode.startswith('daemon-'):
    meta = root / 'manager/home/tmp/owned/manager/config/run/daemons/textprediction/meta.json'
    meta.parent.mkdir(mode=0o700, parents=True)
    meta.write_text('{not-json' if mode == 'daemon-invalid' else json.dumps({'daemon':{'state':mode.removeprefix('daemon-')}}))
    meta.chmod(0o664)
if mode.startswith('graceful-report-'):
    report.update(json.loads(sys.argv[3]))
    meta = root / 'config/run/daemons/fixture/meta.json'
    meta.parent.mkdir(mode=0o700, parents=True)
    meta.write_text(json.dumps({'daemon':{'state':'exited'}})); meta.chmod(0o664)
path = root / 'priority-result.json'
path.write_text(json.dumps(report)); path.chmod(0o600)
'''


class PrivatePriorityWrapperTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(OUTPUT.is_absolute())
        OUTPUT.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="priority-fixture-", dir=OUTPUT)
        self.addCleanup(self.temp.cleanup)
        self.fixture = Path(self.temp.name)
        self.binary = self.fixture / "not-an-OMP-binary"
        self.binary.write_bytes(b"owned inert binary identity fixture\n")
        self.binary.chmod(0o700)
        self.real_popen = subprocess.Popen
        self.launches = []

    def observe_fixture(self, mode, survivor=None, report=None):
        root = self.fixture / mode
        root.mkdir(mode=0o700)
        def launch(argv, **options):
            self.launches.append((list(argv), deepcopy(options)))
            self.assertEqual(argv, [sys.executable, "-P", "-s", "-m",
                "workbench.backend.summary_compat_priority", "--owned-priority", str(root)])
            self.assertTrue(Path(argv[0]).is_absolute())
            self.assertEqual(options["cwd"], root / "project")
            self.assertIs(options["start_new_session"], True)
            self.assertNotIn("shell", options)
            env = options["env"]
            self.assertEqual(env["PYTHONPATH"], str(root / "runtime/src"))
            self.assertEqual(env["HOME"], str(root / "home"))
            for name in ("TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
                self.assertTrue(Path(env[name]).is_relative_to(root))
            for name in ("OPENAI_API_KEY", "HTTPS_PROXY", "SSH_AUTH_SOCK", "VIRTUAL_ENV", "LD_PRELOAD", "PYTHONSTARTUP"):
                self.assertNotIn(name, env)
            self.assertFalse((root / "runtime/tests").exists())
            self.assertFalse((root / "runtime/tools").exists())
            self.assertFalse((root / "runtime/docs").exists())
            self.assertFalse((root / "runtime/.workflow").exists())
            self.assertTrue((root / "runtime/src/workbench/backend/summary_compat_priority.py").is_file())
            args = [sys.executable, "-P", "-s", "-c", HELPER, str(root), mode]
            if survivor:
                args.append(json.dumps(survivor))
            elif report is not None:
                args.append(json.dumps(report))
            return self.real_popen(args, **options)
        with patch.object(priority.shutil, "which", return_value=str(self.binary)), \
             patch.object(priority.tempfile, "mkdtemp", return_value=str(root)), \
             patch.object(priority.subprocess, "Popen", side_effect=launch):
            observed = priority.observe_priority(ROOT, budget_seconds=1)
        self.assertEqual(observed["result"], "inconclusive")
        self.assertFalse(observed["coverage_complete"])
        self.assertEqual(observed["containment"]["python_lexical_executable"], sys.executable)
        self.assertEqual(observed["containment"]["remaining_python_pgid_members"], [])
        return observed, root

    def test_lexical_venv_argv_environment_and_caller_state_are_preserved(self):
        sentinels = {name: "owned-unused-sentinel" for name in
            ("OPENAI_API_KEY", "HTTPS_PROXY", "SSH_AUTH_SOCK", "VIRTUAL_ENV", "LD_PRELOAD", "PYTHONSTARTUP")}
        with patch.dict(os.environ, sentinels):
            before_env, before_cwd = dict(os.environ), os.getcwd()
            before_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
            before_umask = os.umask(0o077); os.umask(before_umask)
            observed, root = self.observe_fixture("success")
            after_umask = os.umask(0o077); os.umask(after_umask)
            self.assertEqual(dict(os.environ), before_env)
            self.assertEqual(os.getcwd(), before_cwd)
            self.assertEqual({sig: signal.getsignal(sig) for sig in before_signals}, before_signals)
            self.assertEqual(after_umask, before_umask)
        self.assertTrue(observed["fixture"]["identity_before_work"])
        self.assertTrue(observed["fixture"]["environment_checked"])
        self.assertEqual(len(self.launches), 1)
        self.assertTrue(observed["cleanup_complete"])
        self.assertTrue(observed["cleanup"]["private_root_removed"])
        self.assertFalse(root.exists())
        self.assertEqual(observed["native_launches"], 0)
        self.assertEqual(observed["POSTs"], [])

    def test_missing_result_preserves_unknown_launch_attribution_and_private_root(self):
        observed, root = self.observe_fixture("missing")
        self.assertIsNone(observed["native_launches"])
        self.assertTrue(observed["model_posts_unobserved"])
        self.assertFalse(observed["containment"]["result_file_present"])
        self.assertFalse(observed["cleanup_complete"])
        self.assertEqual(observed["retained_private_root"], str(root))
        self.assertTrue(root.exists())
        self.assertTrue(any("FileNotFoundError" in error for error in observed["errors"]))
        self.assertEqual(len(self.launches), 1)  # No fallback/retry launch.

    def test_live_owned_resource_readback_retains_root_despite_finished_helper(self):
        survivor = self.real_popen([sys.executable, "-P", "-s", "-c", "import sys; sys.stdin.buffer.read()"],
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.fixture)}, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            identity = {"role": "ordinary_fixture_survivor", "pid": survivor.pid,
                "pgid": survivor.pid, "start_ticks": runtime._identity(survivor.pid)}
            self.assertIsNotNone(identity["start_ticks"])
            observed, root = self.observe_fixture("survivor", identity)
            self.assertFalse(observed["cleanup_complete"])
            self.assertEqual(observed["retained_private_root"], str(root))
            rows = observed["containment"]["owned_process_readback"]
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["same_start_identity_present"])
            self.assertTrue(rows[0]["remaining_pgid_members"])
            self.assertIn("owned_priority_cleanup_incomplete", observed["errors"])
            self.assertIsNone(survivor.poll())
        finally:
            survivor.stdin.close()
            survivor.wait(timeout=3)
            self.assertEqual(runtime._remaining(survivor.pid), [])

    def test_unsupported_relative_interpreter_is_unknown_without_a_fallback(self):
        with patch.object(priority.shutil, "which", return_value=str(self.binary)), \
             patch.object(priority.sys, "executable", "python"), \
             patch.object(priority.subprocess, "Popen") as launch:
            observed = priority.observe_priority(ROOT)
        self.assertEqual(observed["result"], "inconclusive")
        self.assertEqual(observed["native_launches"], 0)
        self.assertIn("priority_runtime_unavailable", observed["errors"])
        launch.assert_not_called()

    def test_nested_daemon_metadata_running_or_invalid_retains_root_exited_allows_delete(self):
        observations = []
        real_delete = priority.shutil.rmtree
        for mode in ("daemon-running", "daemon-exited", "daemon-invalid"):
            with self.subTest(mode=mode):
                before = len(self.launches)
                with patch.object(priority.shutil, "rmtree", wraps=real_delete) as delete:
                    observed, root = self.observe_fixture(mode)
                self.assertEqual(len(self.launches), before + 1)
                expected = mode == "daemon-exited"
                self.assertEqual([observed["cleanup_complete"],
                    observed["containment"]["cleanup_complete"], observed["cleanup"]["complete"]],
                    [expected, expected, expected])
                metadata = observed["containment"]["owned_daemon_metadata"]
                self.assertIn("complete native I/O and process absence remain unknown", metadata["limit"])
                self.assertIn("missing metadata/unobserved writers", metadata["limit"])
                self.assertEqual(metadata["all_observed_exited"], expected)
                if expected:
                    delete.assert_called_once_with(root)
                    self.assertFalse(root.exists())
                    self.assertTrue(observed["cleanup"]["private_root_removed"])
                    self.assertNotIn("retained_private_root", observed)
                    self.assertEqual(metadata["entries"][0]["state"], "exited")
                else:
                    delete.assert_not_called()
                    self.assertEqual(observed["retained_private_root"], str(root))
                    self.assertTrue(root.exists())
                    self.assertFalse(observed["coverage_complete"])
                    path = root / "manager/home/tmp/owned/manager/config/run/daemons/textprediction/meta.json"
                    self.assertTrue(path.is_file())
                    self.assertEqual(path.stat().st_mode & 0o777, 0o664)
                    if mode == "daemon-running":
                        self.assertEqual(metadata["entries"][0]["state"], "running")
                    else:
                        self.assertIn("JSONDecodeError", metadata["error"])
                observations.append({"fixture_mode": mode,
                    "cleanup_values": [observed["cleanup_complete"],
                        observed["containment"]["cleanup_complete"], observed["cleanup"]["complete"]],
                    "delete_calls": delete.call_count, "retained_root": not expected,
                    "metadata_limit": metadata["limit"]})
        print("daemon_metadata_observations=" + json.dumps(observations, sort_keys=True))

    def graceful_block(self, case):
        # Compile actual statements, not a rewritten cleanup algorithm. The
        # surrounding Core/TUI setup is deliberately outside this boundary.
        tree = ast.parse(Path(priority.__file__).read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_execute")
        final = next(node.finalbody for node in ast.walk(function) if isinstance(node, ast.Try)
            and any(isinstance(item, ast.Assign) and isinstance(item.targets[0], ast.Tuple)
                and [getattr(value, "id", None) for value in item.targets[0].elts] == ["request_ns", "request_wall_ns"] for item in node.finalbody))
        request = next(index for index, node in enumerate(final) if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Tuple) and getattr(node.targets[0].elts[0], "id", None) == "request_ns")
        end = next(index for index, node in enumerate(final) if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "parent_observations")
        safe = next(index for index, node in enumerate(final) if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "safe")
        self.assertIsInstance(final[request - 1], ast.For)
        clocks, events, waits, sleeps = [1_000_000_000], [], {}, []
        top = self.fixture / ("block-" + case); top.mkdir(mode=0o700)
        roots, owned, paths = {}, {}, {}
        data = {"launches": {}, "peer_git_fixture": {}, "errors": [], "coverage_complete": True}
        def metadata(path, state):
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            path.write_text(json.dumps({"daemon": {"state": state}})); path.chmod(0o664)
        for role in ("manager", "worker"):
            root = top / role; root.mkdir(mode=0o700); (root / "project").mkdir(mode=0o700)
            roots[role] = root; paths[role] = root / "config/run/daemons/initial/meta.json"
            metadata(paths[role], "running" if role == "manager" and case in ("positive", "deadline") else "exited")
            if role == "manager" and case == "parse": paths[role].write_text("{bad-json")
            parent = SimpleNamespace(pid=os.getpid(), returncode=None)
            def wait(timeout, role=role, parent=parent):
                waits[role] = timeout; events.append("wait:" + role)
                if role == "manager" and case == "fallback":
                    raise subprocess.TimeoutExpired("controlled-parent", timeout)
                clocks[0] += int((.8 if role == "manager" else .4 if case == "positive" else 0) * 1e9)
                parent.returncode = 0
            parent.wait, parent.poll = wait, lambda parent=parent: parent.returncode
            owned[role] = (parent, {"start_ticks": runtime._identity(parent.pid)})
            data["launches"][role] = {}
            data["peer_git_fixture"][role] = {"head_before": "fixture-head", "status_before": ""}
        def git(*args):
            events.append("git:" + Path(args[1]).parent.name)
            return "fixture-head" if args[-1] == "HEAD" else ""
        def marker(path, value):
            self.assertTrue(all("actual_pid_cwd_after" in data["launches"][role]
                and data["peer_git_fixture"][role].get("unchanged") is True for role in owned))
            events.append("marker:" + path.parent.name)
            priority.write_private_json(path, value)
        def sleep(seconds):
            sleeps.append(seconds); clocks[0] += int(seconds * 1e9)
            if case == "positive":
                metadata(paths["manager"], "exited")
                metadata(roots["manager"] / "config/run/daemons/newly-observed/meta.json", "exited")
            elif case == "deadline": clocks[0] = 6_000_000_000
        def fallback(parent, ref):
            events.append("controlled-fallback"); parent.returncode = 0
        namespace = {**vars(priority), "roots": roots, "owned": owned, "data": data, "git": git,
            "now": lambda: clocks[0], "time": SimpleNamespace(time_ns=lambda: 1_700_000_000_000_000_000 + clocks[0], sleep=sleep),
            "write_private_json": marker, "rt": SimpleNamespace(_identity=runtime._identity, _stop_leader=fallback)}
        exec(compile(ast.Module(body=deepcopy(final[request - 1:end]), type_ignores=[]), str(priority.__file__), "exec"), namespace)
        # Late terminal registry values cannot erase a known earlier failure.
        for path in paths.values(): metadata(path, "exited")
        namespace.update(remaining={role: [] for role in owned}, live=[], sockets=0, handler_threads=[], top=top, violations=[])
        exec(compile(ast.Module(body=deepcopy(final[safe:safe + 4]), type_ignores=[]), str(priority.__file__), "exec"), namespace)
        return data, waits, sleeps, events

    def test_graceful_roles_share_deadline_reenumerate_and_terminal_has_no_quiet_wait(self):
        data, waits, sleeps, events = self.graceful_block("positive")
        manager = data["launches"]["manager"]["owned_graceful_stop"]
        worker = data["launches"]["worker"]["owned_graceful_stop"]
        self.assertEqual(manager["stop_request_ns"], worker["stop_request_ns"])
        self.assertEqual(manager["termination_deadline_ns"], worker["termination_deadline_ns"])
        self.assertEqual(manager["termination_deadline_ns"] - manager["stop_request_ns"], 5_000_000_000)
        self.assertIn("missing metadata/unobserved writers unknown", manager["registry_view"])
        self.assertEqual(waits["manager"], 5)
        self.assertAlmostEqual(waits["worker"], 4.19)
        self.assertLess(events.index("marker:worker"), events.index("wait:manager"))
        self.assertEqual(len(manager["last_registry_metadata"]), 2)
        self.assertEqual(sleeps, [.01])
        self.assertEqual(worker["barrier_started_ns"], worker["barrier_returned_ns"])
        self.assertGreater(manager["wait_returned_ns"], manager["wait_started_ns"])
        self.assertGreater(manager["barrier_returned_ns"], manager["barrier_started_ns"])
        self.assertNotEqual(manager["wait_started_wall_ms"], manager["wait_started_ns"] // 1_000_000)
        self.assertTrue(data["cleanup"]["complete"])
        self.assertEqual(data["errors"], [])
        print("graceful_common_budget=" + json.dumps({"parent_wait_seconds": waits, "barrier_sleeps": sleeps,
            "boundary": "actual_AST_block_controlled_parent_clock_not_native"}, sort_keys=True))

    def test_remaining_recheck_uses_existing_deadline_and_waits_for_exact_zombie_reap(self):
        child = self.real_popen([sys.executable, "-c", "pass"], start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            until = priority.time.monotonic() + 2
            while priority.time.monotonic() < until:
                state = Path(f"/proc/{child.pid}/stat")
                if state.exists() and state.read_text().rsplit(") ", 1)[1].split()[0] == "Z": break
                priority.time.sleep(.005)
            self.assertIn(child.pid, runtime._remaining(child.pid))
            clock = [1_000_000_000]
            with patch.object(priority, "now", side_effect=lambda: clock[0]), \
                 patch.object(priority.time, "sleep", side_effect=AssertionError("expired deadline must not wait")):
                residue = priority._remaining_until({"manager": child.pid}, clock[0])
            self.assertIn(child.pid, residue["manager"], "a zombie is residue until exact waitpid")
            self.assertIsNone(child.returncode)
            deadline = clock[0] + 5_000_000_000
            sleeps = []
            def owner_reaps(seconds):
                sleeps.append(seconds)
                child.wait(timeout=1)
                clock[0] += int(seconds * 1_000_000_000)
            with patch.object(priority, "now", side_effect=lambda: clock[0]), \
                 patch.object(priority.time, "sleep", side_effect=owner_reaps):
                cleared = priority._remaining_until({"manager": child.pid}, deadline)
            self.assertEqual(cleared, {"manager": []})
            self.assertEqual(sleeps, [.01])
            self.assertLessEqual(clock[0], deadline)
            self.assertFalse(Path(f"/proc/{child.pid}").exists())
            print("priority_deadline_recheck=" + json.dumps({"pid": child.pid, "expiredResidue": residue,
                "afterExactWait": cleared, "deadlineNs": deadline, "returnedNs": clock[0]}))
        finally:
            child.wait(timeout=2)

    def test_known_graceful_failures_stay_failed_after_late_terminal_and_wrapper_retains(self):
        observations = []
        for case in ("deadline", "parse", "fallback"):
            with self.subTest(case=case):
                data, waits, sleeps, events = self.graceful_block(case)
                self.assertFalse(data["cleanup"]["complete"])
                self.assertFalse(data["coverage_complete"])
                self.assertTrue(any(error.startswith("cleanup:") for error in data["errors"]))
                with patch.object(priority, "_remaining_until", wraps=priority._remaining_until) as recheck:
                    observed, root = self.observe_fixture("graceful-report-" + case,
                        report={key: data[key] for key in ("cleanup", "coverage_complete", "errors", "launches")})
                self.assertEqual(recheck.call_args.args[1],
                    data["launches"]["manager"]["owned_graceful_stop"]["termination_deadline_ns"])
                self.assertTrue(observed["containment"]["owned_daemon_metadata"]["all_observed_exited"])
                self.assertEqual([observed["cleanup_complete"], observed["containment"]["cleanup_complete"],
                    observed["cleanup"]["complete"]], [False, False, False])
                self.assertEqual(observed["retained_private_root"], str(root))
                self.assertTrue(root.exists())
                observations.append({"case": case, "known_errors": data["errors"], "late_terminal_stays_failed": True})
        print("graceful_known_failure_observations=" + json.dumps(observations, sort_keys=True))


class PartialPriorityAggregationTests(unittest.TestCase):
    def prior(self):
        data = runtime.unavailable("operating_MODEL_unknown")
        data.update(level="inconclusive", observations=[{"kind": "prior_RPC_C1_C2_C3",
            "cleanup_complete": True, "POSTs": ["synthetic_owned_post_identity"]}])
        data["io"]["model_calls"] = 3
        return data

    def test_priority_failure_keeps_prior_rpc_evidence_and_cannot_become_pass(self):
        prior = self.prior(); before = deepcopy(prior)
        with patch.object(provider, "observe_owned", return_value=prior), \
             patch.object(provider, "observe_priority", side_effect=OSError("owned fixture missing result")) as observe:
            result = provider.ScriptedCompatibilityProvider().observe()
        observe.assert_called_once()
        self.assertEqual(result["observations"][0], before["observations"][0])
        self.assertEqual(result["items"], {"Q01-route": "not_run", "Q01-tools": "not_run",
            "Q01-skill": "not_run", "Q01-context": "not_run", "Q01-result": "not_run",
            "Q01-origin": "not_run", "Q01-recovery": "not_run", "Q01-priority": "inconclusive"})
        self.assertEqual(result["level"], "inconclusive")
        self.assertIsNone(result["io"]["model_calls"])
        self.assertIsNone(result["io"]["omp_children"])
        self.assertEqual(result["observations"][-1]["kind"], "owned_product_priority_partial")
        self.assertFalse(result["observations"][-1]["coverage_complete"])
        self.assertIn("operating_MODEL_unknown", result["unknowns"])
        self.assertIn("priority_controls_incomplete", result["unknowns"])
        self.assertTrue(any("OSError" in value for value in result["unknowns"]))

    def test_prior_unavailable_or_ambiguous_cleanup_never_launches_priority_group(self):
        unavailable = runtime.unavailable("owned fixture unavailable")
        ambiguous = self.prior(); ambiguous["observations"][0]["cleanup_complete"] = False
        for prior in (unavailable, ambiguous):
            before = deepcopy(prior)
            with patch.object(provider, "observe_owned", return_value=prior), \
                 patch.object(provider, "observe_priority") as observe:
                self.assertEqual(provider.ScriptedCompatibilityProvider().observe(), before)
            observe.assert_not_called()

    def test_global_children_stays_null_and_named_counts_survive_record_consumption(self):
        identity = {"policy_version": 1, "unknowns": [], "policy_sha256": "1" * 64,
            "closure_digest": "2" * 64, "runtime_digest": "3" * 64,
            "stable_host": {"os_family": "Linux", "architecture": "x86_64", "python_minor": "3.12"}}
        cases = [("normal", 2), ("unknown", None), ("exception", None)]
        OUTPUT.mkdir(mode=0o700, parents=True, exist_ok=True)
        observations = []
        with tempfile.TemporaryDirectory(prefix="io-record-", dir=OUTPUT) as directory:
            for label, count in cases:
                with self.subTest(priority=label):
                    prior = self.prior()
                    prior["io"]["omp_children"] = 2
                    prior["observations"][0]["native_launches"] = 2
                    original_rpc = deepcopy(prior["observations"][0])
                    supplied = {"kind": "owned_product_priority_partial", "result": "inconclusive",
                        "coverage_complete": False, "native_launches": count, "POSTs": [], "errors": []}
                    options = {"side_effect": OSError("owned fixture report unknown")} if label == "exception" else {"return_value": supplied}
                    with patch.object(provider, "observe_owned", return_value=prior), \
                         patch.object(provider, "observe_priority", **options) as observe:
                        result = provider.ScriptedCompatibilityProvider().observe()
                    observe.assert_called_once()
                    self.assertIsNone(result["io"]["omp_children"])
                    self.assertIsNone(result["io"]["model_calls"])
                    self.assertEqual(result["observations"][0], original_rpc)
                    if label != "exception":
                        self.assertEqual(result["observations"][1], supplied)
                    else:
                        self.assertTrue(result["observations"][1]["errors"])
                    service = SummaryCompatibility(Path(directory) / label, identity=lambda: deepcopy(identity))
                    with patch.object(provider.ScriptedCompatibilityProvider, "observe", return_value=result):
                        returned = service.verify_scripted()
                    persisted = json.loads((Path(directory) / label / (returned["id"] + ".json")).read_text())
                    self.assertIsNone(json.loads(json.dumps(returned))["io"]["omp_children"])
                    self.assertIsNone(persisted["io"]["omp_children"])
                    self.assertEqual(persisted["observations"], result["observations"])
                    self.assertEqual(returned["result"], "inconclusive")
                    self.assertNotIn("pass", returned["items"].values())
                    self.assertFalse(service.admission())
                    observations.append({"priority_case": label, "global_omp_children": None,
                        "prior_named_native_launches": persisted["observations"][0]["native_launches"],
                        "priority_named_native_launches": persisted["observations"][1].get("native_launches"),
                        "record_and_return_json_null": True})
        # Original count remains meaningful when priority never starts.
        unavailable = runtime.unavailable("owned fixture unavailable")
        ambiguous = self.prior()
        ambiguous["io"]["omp_children"] = 2
        ambiguous["observations"][0]["cleanup_complete"] = False
        for prior in (unavailable, ambiguous):
            original = deepcopy(prior)
            with patch.object(provider, "observe_owned", return_value=prior), \
                 patch.object(provider, "observe_priority") as observe:
                self.assertEqual(provider.ScriptedCompatibilityProvider().observe(), original)
            observe.assert_not_called()
        print("global_children_observations=" + json.dumps(observations, sort_keys=True))


class ParentPayloadWireTests(unittest.TestCase):
    def test_actual_sse_roundtrips_execute_periodic_and_unicode_notice(self):
        marker = "<owned-execute>"
        # Fixed flat sample, independent of response-contract construction.
        execute = marker + '{"decision":"execute","task_id":"owned-task","revision_id":1,"run_id":"owned-run"}'
        samples = [("execute", execute),
            ("periodic", "Owned periodic review observed. No analysis verdict."),
            ("notice", '소유 알림 확인: 한글·인용 "완료"\n다음 줄')]
        observations = []
        for kind, text in samples:
            with self.subTest(kind=kind):
                wire, reported_sha = priority._parent_payload(text)
                blocks = [block for block in wire.decode("utf-8").split("\n\n") if block]
                self.assertTrue(all(block.startswith("data: ") for block in blocks))
                events = [block.removeprefix("data: ") for block in blocks]
                self.assertEqual(events.count("[DONE]"), 1)
                self.assertEqual(events[-1], "[DONE]")
                frames = [json.loads(event) for event in events if event != "[DONE]"]
                choices = [choice for frame in frames for choice in frame["choices"]]
                content = "".join(choice["delta"].get("content", "") for choice in choices)
                self.assertEqual(content, text)
                self.assertEqual(sha256(content.encode("utf-8")).hexdigest(), reported_sha)
                self.assertEqual([choice["finish_reason"] for choice in choices
                    if choice["finish_reason"] is not None], ["stop"])
                self.assertEqual([frame["usage"] for frame in frames if "usage" in frame],
                    [{"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}])
                if kind == "execute":
                    self.assertTrue(content.startswith(marker))
                    decoded = json.loads(content[len(marker):])
                    self.assertEqual(decoded, {"decision": "execute", "task_id": "owned-task",
                        "revision_id": 1, "run_id": "owned-run"})
                    self.assertTrue(all(type(value) in (str, int) for value in decoded.values()))
                observations.append({"kind": kind, "utf8_bytes": len(content.encode("utf-8")),
                    "wire_content_sha256": reported_sha, "finish": "stop", "DONE_count": 1})
        print("parent_payload_roundtrips=" + json.dumps(observations, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
