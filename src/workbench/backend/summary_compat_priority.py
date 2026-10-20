"""Explicit owned upper-SDK/public-Core priority capability, never a Q01 pass.

The one-shot private Python helper contains Core subprocess environment. Model
dispatch stays in the native Manager extension; the caller environment is untouched.
"""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import select
import shutil
import signal
import sqlite3
import tempfile
from dataclasses import asdict
import stat
import struct
import subprocess
import sys
import termios
import threading
import time
from uuid import uuid4

from workbench.assets import runtime_root

SOURCE = runtime_root()
from workbench.backend import summary_compat_runtime as rt
from workbench.backend.flow import HandoffJournal
from workbench.backend.flow_terminal import TerminalService, _Command
from workbench.backend.launcher import LaunchPlan, omp_environment
from workbench.backend.paths import DataLayout, ensure_private_dir, write_private_json
from workbench.backend.summary_adapter import SDKSummaryAdapter, SKILL_NAME, prepare_summary, skill_bytes
from workbench.backend.summary_scheduler import SummarySourceCollector
from workbench.contracts.logs_v1 import canonical_json, summary_input_key
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer, TaskMailbox
from workbench.observation.worker_review import ActiveRunRef, SerializedReviewAdmission, WorkerReviewScheduler
from workbench.storage.important_log import ActiveSubjectLog, ImportantLogCatalog, ImportantLogStore
from workbench.storage.log_raw.port import raw_query
from workbench.tasks.repository import InvalidTransitionError, TaskRepository

def now():
    return time.monotonic_ns()

def _remaining_until(groups: dict, deadline_ns: int) -> dict:
    """Allow the containing subreaper to reap, within the existing common deadline."""
    remaining = {key: rt._remaining(pgid) for key, pgid in groups.items()}
    while any(remaining.values()) and now() < deadline_ns:
        time.sleep(min(.01, max(0, (deadline_ns - now()) / 1000000000)))
        if now() >= deadline_ns:
            break
        remaining = {key: rt._remaining(pgid) for key, pgid in groups.items()}
    return remaining

def utc():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
def artifact(path):
    raw = path.read_bytes()
    return {'bytes': len(raw), 'sha256': sha256(raw).hexdigest()}

def _parent_payload(text: str) -> tuple[bytes, str]:
    """Encode the same owned assistant text that its observation digest names."""
    text_sha256 = sha256(text.encode()).hexdigest()
    frames = [{'id': 'owned-notice', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'scripted',
        'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': text}, 'finish_reason': None}]},
        {'id': 'owned-notice', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'scripted',
        'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}]
    payload = b''.join(('data: ' + json.dumps(frame) + '\n\n').encode() for frame in frames) + b'data: [DONE]\n\n'
    return payload, text_sha256

def _persist_processes(root: Path, data: dict) -> None:
    """Publish actual owned identities immediately; no PID-name discovery."""
    refs = [dict(row["owned_process"]) for row in data.get("launches", {}).values() if "owned_process" in row]
    for name in ("owned_shell", "managed_supervisor"):
        if name in data:
            refs.append({"role": name, **data[name]})
    life = data.get("owned_managed_lifecycle", {})
    if type(life.get("child_pid")) is int:
        refs.append({"role": "managed_finite_child", "pid": life["child_pid"], "pgid": life["child_group"],
            "start_ticks": rt._identity(life["child_pid"])})
    write_private_json(root / "owned-processes.json", {"processes": refs, "observed_ns": now()})


def _owned_json(path: Path, limit: int = 2097152) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError("unsafe_owned_priority_result")
        value = json.loads(os.read(descriptor, limit + 1))
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise ValueError("invalid_owned_priority_result")
    return value


def _daemon_states(root: Path) -> list[dict]:
    """Read observed registry files; missing metadata/writer absence is unknown."""
    rt._private_root(root)
    observed = []
    for path in root.glob("**/run/daemons/**/meta.json"):
        if path.resolve() != path or path.is_symlink():
            raise ValueError("unsafe_owned_daemon_metadata_path")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or before.st_nlink != 1 or before.st_size > 2097152):
                raise ValueError("unsafe_owned_daemon_metadata")
            raw = os.read(descriptor, 2097152 + 1)
            after = os.fstat(descriptor)
            if (len(raw) != before.st_size or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns):
                raise ValueError("owned_daemon_metadata_changed")
        finally:
            os.close(descriptor)
        value = json.loads(raw)
        state = value["daemon"]["state"]
        observed.append({"path": str(path.relative_to(root)), "bytes": len(raw),
                         "sha256": sha256(raw).hexdigest(), "state": state,
                         "exited_at_wall_ms": value["daemon"].get("exitedAt"),
                         "mtime_ns": after.st_mtime_ns})
    return observed


def observe_priority(source: Path, *, budget_seconds: float = 90.0) -> dict:
    """One credential-free Core helper; caller cwd/env/signals stay untouched."""
    binary_name = shutil.which("omp")
    budget_seconds = min(90.0, budget_seconds)
    if budget_seconds <= 0 or sys.platform != "linux" or binary_name is None or not os.path.isabs(sys.executable):
        return {"kind": "owned_product_priority_partial", "result": "inconclusive", "errors": ["priority_runtime_unavailable"],
            "native_launches": 0, "POSTs": [], "coverage_complete": False, "cleanup_complete": True}
    binary = Path(binary_name).resolve()
    root = Path(tempfile.mkdtemp(prefix="wb-summary-priority-")).resolve()
    process = identity = drain = None
    data = {"kind": "owned_product_priority_partial", "result": "inconclusive", "errors": [],
        "native_launches": 0, "POSTs": [], "coverage_complete": False}
    stdout = {"bytes_seen": 0, "bytes_saved": 0, "truncated": False}
    try:
        rt._private_root(root)
        for name in ("home", "project", "tmp", "agent", "config", "xdg-config", "xdg-data", "xdg-state", "xdg-cache"):
            (root / name).mkdir(mode=0o700)
        runtime = rt._copy_runtime(source, root)
        env = rt._environment(root)
        env.update(PYTHONPATH=str(runtime / "src"), PYTHONNOUSERSITE="1", GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="Owned fixture",
            GIT_AUTHOR_EMAIL="fixture@localhost", GIT_COMMITTER_NAME="Owned fixture", GIT_COMMITTER_EMAIL="fixture@localhost")
        argv = [sys.executable, "-P", "-s", "-m", "workbench.backend.summary_compat_priority", "--owned-priority", str(root)]
        write_private_json(root / "priority-owner.json", {"root": str(root), "binary": str(binary),
            "binary_sha256": sha256(binary.read_bytes()).hexdigest(), "body_budget_seconds": budget_seconds})
        process = subprocess.Popen(argv, cwd=root / "project", env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        identity = {"role": "private_Core_python", "pid": process.pid, "pgid": process.pid, "start_ticks": rt._identity(process.pid)}
        write_private_json(root / "helper-launch.json", {**identity, "argv": argv, "env_names": sorted(env), "requested_ns": now()})
        if identity["start_ticks"] is None:
            raise ValueError("owned_python_identity_unavailable")
        def drain_stdout():
            descriptor = os.open(root / "helper.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    while raw := process.stdout.read(65536):
                        stdout["bytes_seen"] += len(raw)
                        saved = raw[:max(0, 262144 - stdout["bytes_saved"])]
                        output.write(saved)
                        stdout["bytes_saved"] += len(saved)
                        stdout["truncated"] |= len(saved) < len(raw)
            finally:
                process.stdout.close()
        drain = threading.Thread(target=drain_stdout, name="priority-helper-output", daemon=True)
        drain.start()
        process.wait(timeout=budget_seconds + 20)
        candidate = _owned_json(root / "priority-result.json")
        if candidate.get("kind") != "owned_product_priority_partial" or candidate.get("result") != "inconclusive" or not isinstance(candidate.get("POSTs"), list):
            raise ValueError("priority_result_schema_unavailable")
        data = candidate
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        data["errors"].append(type(error).__name__ + ":" + str(error))
        data["coverage_complete"] = False
    finally:
        if process is not None:
            try:
                error = rt._stop_leader(process, identity)
                if error:
                    data["errors"].append(error)
            except (OSError, subprocess.SubprocessError) as error:
                data["errors"].append("private_helper_cleanup:" + str(error))
        if drain is not None:
            drain.join(3)
        if process is not None and not (root / "priority-result.json").is_file():
            data.update(native_launches=None, model_posts_unobserved=True)
        refs = []
        helper_remaining = rt._remaining(identity["pgid"]) if identity else []
        try:
            if (root / "owned-processes.json").exists():
                refs = _owned_json(root / "owned-processes.json")["processes"]
            deadlines = [row["owned_graceful_stop"]["termination_deadline_ns"]
                for row in data.get("launches", {}).values() if "owned_graceful_stop" in row]
            groups = {index: ref["pgid"] for index, ref in enumerate(refs)}
            if identity:
                groups["helper"] = identity["pgid"]
            observed = _remaining_until(groups, min(deadlines, default=now()))
            helper_remaining = observed.get("helper", [])
            remaining = [{"owned_ref": ref, "remaining_pgid_members": observed[index],
                "same_start_identity_present": rt._identity(ref["pid"]) == ref["start_ticks"] if ref.get("start_ticks") else None} for index, ref in enumerate(refs)]
        except (OSError, ValueError, KeyError, TypeError) as error:
            data["errors"].append("owned_resource_readback:" + str(error))
            remaining = [{"identity": "unavailable", "remaining_pgid_members": None}]
        safe = (not helper_remaining and not any(row["remaining_pgid_members"] is None or row["remaining_pgid_members"] for row in remaining)
            and not (drain and drain.is_alive()) and (process is None or process.returncode is not None)
            and data.get("cleanup", {}).get("complete", process is None))
        data["containment"] = {"helper_count": int(process is not None), "owned_process": identity,
            "python_lexical_executable": sys.executable, "python_real_binary": str(Path(sys.executable).resolve()),
            "python_real_binary_sha256": sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),
            "stdout": stdout, "remaining_python_pgid_members": helper_remaining, "owned_process_readback": remaining,
            "caller_environment_cwd_signals_mutated": False, "credential_environment_entries": 0,
            "result_file_present": (root / "priority-result.json").exists(), "cleanup_complete": safe}
        if (root / "helper.log").is_file():
            data["containment"]["log"] = artifact(root / "helper.log")
        daemon_metadata = {"entries": [], "all_observed_exited": False,
                           "limit": "observed owned registry JSON only; missing metadata/unobserved writers, complete native I/O and process absence remain unknown"}
        try:
            daemon_metadata["entries"] = _daemon_states(root)
            daemon_metadata["all_observed_exited"] = all(row["state"] == "exited" for row in daemon_metadata["entries"])
            safe = safe and daemon_metadata["all_observed_exited"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            daemon_metadata["error"] = type(error).__name__ + ":" + str(error)
            safe = False
        data["containment"]["owned_daemon_metadata"] = daemon_metadata
        if safe:
            shutil.rmtree(root)
            safe = not root.exists()
            data.setdefault("cleanup", {})["private_root_removed"] = safe
            if not safe:
                data["errors"].append("late_root_recreated")
        data["cleanup_complete"] = safe
        data["containment"]["cleanup_complete"] = safe
        data.setdefault("cleanup", {})["complete"] = safe
        if not safe:
            data["retained_private_root"] = str(root)
            data["coverage_complete"] = False
            data["errors"].append("owned_priority_cleanup_incomplete")
    return data


class ObservedBridge(G3BridgeServer):
    """Observe accepted frames under the existing RLock; delegate exactly once."""
    def __init__(self, *args):
        self.observed = []
        self.observation_overflow = False
        super().__init__(*args)
    def _receive(self, peer, frame):
        with self._condition:
            before = self._event_sequence
            pending = self._pending_acks.get(frame.get('requestId')) if frame.get('kind') == 'api_ack' else None
            super()._receive(peer, frame)
            row = None
            if self._event_sequence > before:
                row = {'kind': 'accepted_event', 'name': frame['name'], 'sequence': self._event_sequence}
            elif pending is not None and pending[0] is peer and pending[1] is None:
                accepted = self._pending_acks.get(frame.get('requestId'))
                if accepted is not None and accepted[0] is peer and accepted[1] is frame:
                    row = {'kind': 'accepted_ack', 'requestId': frame['requestId'], 'status': frame.get('status')}
            if row is not None:
                row.update(role=peer.public.role.value, session_id=peer.public.session_id,
                           generation=peer.public.generation, host_receive_ns=now())
                if len(self.observed) < 256:
                    self.observed.append(row)
                else:
                    self.observation_overflow = True
    def accepted_events(self, role, name, after):
        with self._condition:
            return [dict(row) for row in self.observed if row['kind'] == 'accepted_event'
                and row['role'] == role and row['name'] == name and row['host_receive_ns'] >= after]

class Hold:
    def __init__(self, root):
        self.root = root
        self.seen, self.release, self.done = threading.Event(), threading.Event(), threading.Event()
        self.facts = {}
    def before_response(self, expected, body, text):
        prompt = rt._matched_prompt(expected, body, self.facts)
        with TaskRepository(DataLayout(self.root / 'data').tasks) as repo:
            row = repo.get_summary_request(expected['requestId'])
        if not isinstance(prompt, dict) or row is None or row['state'] != 'sending' or canonical_json(prompt.get('request')) != canonical_json(row['request']):
            raise ValueError('prompt_journal_binding_mismatch')
        self.facts.update(expected=expected, request_binding_matches=True, validated_post_ns=now())
        self.seen.set()
        try:
            self.facts['explicit_release'] = self.release.wait(5)
        finally:
            self.facts['release_ns'] = now()
            self.done.set()

def producer(root, result):
    """Three real Task spec deliveries, not a fabricated terminal-start event."""
    layout = DataLayout(root / 'data')
    ensure_private_dir(layout.workflow)
    with TaskRepository(layout.tasks) as journal:
        parts = [''.join('private-work-' + sha256(f'dvl14-upper-fixed-{index}-{line}'.encode()).hexdigest() + '\n'
                         for line in range(64))[:4096] for index in range(3)]
        task = journal.create_task({'description': 'owned upper input', 'parts': parts})
        scope = journal.approve_scope(task, 1, {'operation': 'owned upper summary'})
        grant = journal.proceed(task, 1, 'owned manual upper admission')
        run = journal.start_run(task, 1)
    layout.tasks.chmod(0o600)
    catalog = ImportantLogCatalog(layout.important_logs, create=True)
    sink = ActiveSubjectLog(catalog)
    flow = HandoffJournal(layout.workflow / 'handoffs.jsonl')
    try:
        sink.record_flow(flow.append({'type': 'delegated', 'task_id': task, 'revision': 1,
            'scope_decision_id': scope, 'proceed_decision_id': grant, 'run_id': run}))
    finally:
        flow.close()
    subject = {'schemaVersion': 1, 'kind': 'task', 'id': task}
    messages = []
    for text in parts:
        message = str(uuid4())
        messages.append(message)
        sink.record_delivery(subject, message_id=message, revision=1, kind='task_spec', content=text, delivered_at=utc())
    originals = []
    with TaskRepository(layout.tasks) as journal:
        with ImportantLogStore.open(layout.important_logs, catalog.active_uuid(), readonly=True) as store:
            entries = [store.get_entry(row[0]) for row in store._connection.execute(
                "SELECT entry_id FROM entries WHERE subject_kind='task' AND subject_id=?", (task,))]
            result['catalog_initial_entries'] = store._connection.execute('SELECT COUNT(*) FROM entries').fetchone()[0]
        for message in messages:
            entry = next(entry for entry in entries if entry['sources'][0]['source'].get('messageId') == message)
            span = entry['sources'][0]
            originals.append(span)
            journal.ingest_summary_source(subject, span, entry['entryId'])
        records = journal.pending_summary_sources(subject)
        pieces, gaps = SummarySourceCollector(layout.root, lambda: sink).collect(journal, subject, records)
        if gaps or len(pieces) != 3 or any(len(p.text.encode()) != 4096 or sha256(p.text.encode()).hexdigest() != p.range['digest'] for p in pieces):
            raise RuntimeError('owned_upper_source_collection_mismatch')
    authority = sink.authority(subject, active=True, paused=False, revoked=False, has_new_input=True)
    request = {'schemaVersion': 1, 'requestId': str(uuid4()), 'subject': subject, 'revision': 1,
        'authorizationRef': authority['authorization_ref'], 'authorizationEpoch': authority['authorization_epoch'],
        'skillName': SKILL_NAME, 'skillDigest': sha256(skill_bytes()).hexdigest(), 'sources': originals,
        'sourceGaps': [], 'priorCumulativeEntryId': None, 'dbHealth': catalog.health_snapshot()['active'], 'requestedAt': utc()}
    request['inputKey'] = summary_input_key(subject, 1, originals, [], request['skillDigest'])
    if sink.authorize_summary_request(request, active=True, paused=False, revoked=False, has_new_input=True)['allowed'] is not True:
        raise RuntimeError('owned_upper_authority_unavailable')
    result['producer'] = {'original_ranges': originals, 'bytes': 12288, 'collector_pieces': 3,
        'capture_failures': [], 'authority': authority, 'task_id': task, 'run_id': run,
        'label': 'actual owned Task spec deliveries and collector; manual host admission, not Backend boot'}
    return layout, catalog, sink, authority, request, pieces, originals

def _execute(top: Path, binary: Path, body_budget_seconds: float):
    from workbench.app.lifecycle import LifecycleJournal
    from workbench.backend.automation import AutomationController
    from workbench.backend.flow_tasks import HOLD_REASON
    from workbench.backend.panes import ShellPane, HostShellPort
    from workbench.storage.log_raw import RawLogStore
    from workbench.terminal.shell_g2.prototype import ShellChoice
    from workbench.workflow import G3WorkerResponsePort, TaskWorkflow
    from workbench.workflow.run import WorkflowHeld
    started, deadline = time.monotonic(), time.monotonic() + body_budget_seconds
    OUT = top / "evidence"
    data = {"kind": "owned_product_priority_partial", "result": "inconclusive",
        "clock": "time.monotonic_ns", "errors": [], "raw_queries": [], "count_queries": [],
        "states": [], "requests": [], "POSTs": [], "native_launches": 0, "native_retries": 0,
        "coverage_complete": False, "automation_logs": [], "body_budget_seconds": body_budget_seconds,
        "limitations": ["Whole Q01/full14/operatingMODEL and native I/O remain unverified.",
            "Owner invokes public experiment_started; Backend boot and TaskFlow notify/eventproducer unverified.",
            "Native G3 notices are owned fixtures; Core periodic QUESTION uses actual TaskMailbox delivery.",
            "Injected review clock0/60; internal mutex wait unavailable; only Python monotonic_ns is compared.",
            "Host output default and current context/reasoning/enforcement unknowns are retained; ClaudeV3 is controlled model metadata, not operating proof.",
            "Native entry JSON.stringify view only; memory/storage serialization equality unknown.",
            "COMMON5s shutdown budget is a technical bound, not universal completion proof; missing daemon metadata/unobserved writers remain unknown.",
            "Upper summary subject is its separately bound owned producer command, not the Core Task grant.",
            "Workflow completion/Worker analysis/TaskFlow workerheld/_remember_home_cwd/_give_back/_HandoffWatch unverified."]}
    roots, owned, servers, serves, drains, masters, stops, readies = {}, {}, {}, {}, {}, {}, {}, {}
    tokens = {role: uuid4().hex for role in ('manager', 'worker')}
    bridge = pane = port = controller = workflow_run = repository = raw_store = pump = None
    pump_stop = threading.Event()
    clock = [0.0]
    completed = None
    child_posts, violations = [], []
    hold = Hold(top / 'manager')
    parent_hold, parent_release = threading.Event(), threading.Event()
    notice_ids = {role: str(uuid4()) for role in tokens}
    tty = {role: {'bytes_seen': 0, 'bytes_saved': 0, 'truncated': False} for role in tokens}
    handler_threads = []
    peers = {}
    def check():
        if violations:
            raise RuntimeError('provider_violation:' + violations[0])
        if time.monotonic() >= deadline:
            raise TimeoutError('inner90s_deadline')
    def wait_for(predicate, seconds=2):
        until = min(deadline, time.monotonic() + seconds)
        while not predicate():
            check()
            if time.monotonic() >= until:
                raise TimeoutError('bounded_observation_wait')
            time.sleep(.01)
    def request(role, frame, timeout=1):
        row = {'role': role, 'frame': deepcopy(frame), 'sent_ns': now()}
        data['requests'].append(row)
        ack = bridge.request(role, frame, timeout=timeout, expected_peer=(peers[role].session_id, peers[role].generation))
        row.update(ack=ack, ack_observed_ns=now())
        return ack
    def probe(role, label):
        sent = now()
        value = bridge.probe(role, timeout=.65)
        data['states'].append({'role': role, 'label': label, 'sent_ns': sent, 'observed_ns': now(), 'state': value})
        return value
    def provider(role):
        base = rt._handler(top / 'manager', child_posts, violations, hold)
        class Provider(base):
            def do_POST(self):
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 262144:
                        raise ValueError('owned_provider_body_bound')
                    raw = self.rfile.read(size)
                    body = json.loads(raw)
                    tools = [tool.get('function', {}).get('name') for tool in body.get('tools', [])]
                    stage = 'child' if tools == ['db_summary_save'] else 'parent'
                    row = {'role': role, 'stage': stage, 'path': self.path, 'body_bytes': size,
                        'body_sha256': sha256(raw).hexdigest(), 'tools': tools, 'provider_post_ns': now(),
                        'model': body.get('model'), 'max_completion_tokens': body.get('max_completion_tokens')}
                    data['POSTs'].append(row)
                    if len(data['POSTs']) > 6 or self.path != '/v1/chat/completions' or body.get('model') != ('worker' if stage == 'child' else 'scripted'):
                        raise ValueError('owned_provider_request_boundary')
                    if stage == 'child':
                        if role != 'manager':
                            raise ValueError('worker_child_forbidden')
                        row['owner_request_id'] = json.loads((top / 'manager/expected.json').read_text())['requestId']
                        original_stream = self.rfile
                        try:
                            self.rfile = io.BytesIO(raw)  # identical external bytes; base guards unchanged
                            base.do_POST(self)
                        finally:
                            self.rfile = original_stream
                        return
                    expected_tools = {"manager": ["workbench_logs", "to_worker", "restart_worker", "workbench_status", "stop_survivor"],
                        "worker": ["workbench_logs", "to_manager", "terminal"]}
                    if tools != expected_tools[role] or b"private-work-" in raw:
                        raise ValueError("parent_tools_or_upper_contamination")
                    injected_messages = []
                    for message in body.get("messages", []):
                        if message.get("role") != "user":
                            continue
                        content = message.get("content")
                        texts = [content] if isinstance(content, str) else [part.get("text") for part in content or [] if part.get("type") == "text"]
                        for text in texts:
                            try:
                                value = json.loads(text)
                            except (ValueError, TypeError):
                                continue
                            if isinstance(value, dict) and (value.get("notice_id") or value.get("workbench_message_id")):
                                injected_messages.append(value)
                    if not injected_messages:
                        raise ValueError("actual_parent_message_missing")
                    injected = injected_messages[-1]
                    row.update(owner_session_id=peers[role].session_id, raw_upper_contamination=False)
                    if injected.get("notice_id"):
                        row.update(notice_id=injected.get("notice_id"), stage=injected.get("workbench_notice"))
                        wanted = "worker_terminal_done" if role == "manager" else "terminal_check"
                        if injected.get("notice_id") != notice_ids[role] or row["stage"] != wanted or sum(p.get("notice_id") == notice_ids[role] for p in data["POSTs"]) != 1:
                            raise ValueError("parent_notice_identity_mismatch")
                        text = "Owned fixture notice acknowledged."
                    else:
                        row.update(stage=injected.get("payload", {}).get("stage"), message_id=injected["workbench_message_id"],
                            delivery_attempt_id=injected["workbench_delivery_attempt_id"], task_id=injected["task_id"],
                            revision_id=injected["revision_id"], run_id=injected["run_id"],
                            session_id=injected["session_id"], session_generation=injected["session_generation"])
                        core_posts = [p for p in data["POSTs"] if p.get("message_id")]
                        wanted = "execute" if len(core_posts) == 1 else "periodic_review"
                        if role != "worker" or len(core_posts) > 2 or row["stage"] != wanted or row["task_id"] != task or row["session_id"] != peers[role].session_id or row["session_generation"] != peers[role].generation:
                            raise ValueError("actual_Core_parent_binding_or_stage_mismatch")
                        if row["stage"] == "execute":
                            contract = injected.get("response_contract")
                            if not isinstance(contract, dict) or contract.get("format") != "marker_plus_compact_flat_json":
                                raise ValueError("actual_execute_contract_missing")
                            fields = {field["name"]: field for field in contract["fields"]}
                            frame = {}
                            for name in contract["field_order"]:
                                field = fields[name]
                                if name == "decision":
                                    if "execute" not in field.get("allowed", []):
                                        raise ValueError("execute_not_allowed_by_actual_contract")
                                    frame[name] = "execute"
                                else:
                                    frame[name] = field["value"]
                            text = contract["marker"] + json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
                            row["response_contract_sha256"] = sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
                        else:
                            if "response_contract" in injected or row["run_id"] != workflow_run.run_id:
                                raise ValueError("actual_periodic_contract_or_run_mismatch")
                            text = "Owned periodic review observed. No analysis verdict."
                    if role == "manager" and row.get("notice_id") == notice_ids["manager"]:
                        parent_hold.set()
                        if not parent_release.wait(timeout=max(0, deadline - time.monotonic())):
                            raise TimeoutError("owned_busy_parent_release_timeout")
                    payload, row["assistant_response_sha256"] = _parent_payload(text)
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, RuntimeError, AssertionError, subprocess.SubprocessError) as error:
                    violations.append(type(error).__name__ + ':' + str(error))
                    self.send_error(400)
        return Provider
    try:
        rt._private_root(top)
        data["private_python"] = {"sys_prefix": sys.prefix, "sys_base_prefix": sys.base_prefix, "executable": sys.executable, "safe_path": sys.flags.safe_path, "no_user_site": sys.flags.no_user_site}
        if Path(os.environ["HOME"]).resolve() != top / "home" or Path.cwd().resolve() != top / "project":
            raise ValueError("unsafe_private_helper_environment")
        OUT.mkdir(mode=0o700)
        operator = top / "operator"
        operator.mkdir(mode=0o700)
        for name in ("home", "tmp", "project", "hooks", "artifacts", "raw"):
            (operator / name).mkdir(mode=0o700)
        main_env = dict(os.environ)
        def git(*args):
            value = subprocess.run(["/usr/bin/git", *args], env=main_env, cwd=operator / "project",
                capture_output=True, text=True, timeout=5, check=True)
            return value.stdout.strip()
        source = operator / "source"
        source.mkdir(mode=0o700)
        git("init", "-q", str(source))
        git("-C", str(source), "config", "core.hooksPath", str(operator / "hooks"))
        script = source / "run.sh"
        script.write_text("#!/bin/bash\nprintf 'CORE_PHASEA_MARKER\\n'\nsleep 4\nprintf 'PASS\\n' > PASS.txt\nprintf 'PASS\\n'\n")
        script.chmod(0o700)
        git("-C", str(source), "add", "run.sh")
        git("-C", str(source), "commit", "-qm", "Owned finite Core fixture")
        commit = git("-C", str(source), "rev-parse", "HEAD")
        data["private_git"] = {"source": str(source), "commit": commit, "script": artifact(script)}
        data["peer_git_fixture"] = {}
        for role in tokens:
            root = top / role
            root.mkdir(mode=0o700)
            rt._private_root(root)
            roots[role] = root
            for name in ('home', 'agent', 'config', 'project', 'data', 'tmp', 'xdg-config', 'xdg-data', 'xdg-state', 'xdg-cache'):
                (root / name).mkdir(mode=0o700)
            git("init", "-q", str(root / "project"))
            git("-C", str(root / "project"), "config", "core.hooksPath", str(operator / "hooks"))
            git("-C", str(root / "project"), "commit", "--allow-empty", "-qm", "Owned peer baseline")
            head = git("-C", str(root / "project"), "rev-parse", "--verify", "HEAD")
            info = (root / "project").stat()
            if len(head) != 40 or any(c not in "0123456789abcdef" for c in head) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("owned_peer_HEAD_unavailable")
            data["peer_git_fixture"][role] = {"path": str(root / "project"), "head_before": head,
                "uid": info.st_uid, "mode": "0700", "commits_created": 1,
                "status_before": git("-C", str(root / "project"), "status", "--porcelain")}
            rt._copy_runtime(SOURCE, root)
        layout, catalog, sink, authority, original_request, pieces, originals = producer(roots['manager'], data)
        socket_path = top / 'g3.sock'
        assert len(str(socket_path).encode()) <= 107
        bridge = ObservedBridge(socket_path, tokens)
        info = socket_path.stat()
        assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid()
        bridge.start()
        data['socket'] = {'path': str(socket_path), 'bytes': len(str(socket_path).encode()), 'mode': '0600', 'uid': info.st_uid}
        data['binary_sha256'] = sha256(binary.read_bytes()).hexdigest()
        data['launches'] = {}
        for role, root in roots.items():
            server = rt._Server(('127.0.0.1', 0), provider(role))
            servers[role] = server
            serve = threading.Thread(target=server.serve_forever, name=role + '-HTTP', daemon=True)
            serves[role] = serve
            serve.start()
            endpoint = f'http://127.0.0.1:{server.server_port}/v1'
            write_private_json(root / 'fixture.json', {'root': str(root), 'endpoint': endpoint})
            rt._write_owned_models(root, endpoint, tokenizer='claude-v3')
            overlay = root / 'overlay.yml'
            overlay.write_text('startup:\n  setupWizard: false\n')
            overlay.chmod(0o600)
            runtime = root / 'runtime'
            plan = LaunchPlan(ShellChoice('bash', '/bin/bash'), str(binary), '18.8.6',
                              str(runtime / 'omp_bridge/g3/entry.ts'))
            env = omp_environment(rt._environment(root), plan, role=role, token=tokens[role],
                                  bridge_socket=socket_path,
                                  home={"PI_CODING_AGENT_DIR": str(root / "agent"), "PI_CONFIG_DIR": str(root / "config")},
                                  models=rt._owned_model_table(runtime))
            env.update(PATH=main_env['PATH'], GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0',
                TERM='xterm-256color', COLORTERM='truecolor')
            argv = ['/usr/bin/setsid', '--ctty', str(binary), '--thinking', 'off', '--no-session', '--no-title', '--no-extensions',
                '--no-skills', '--no-rules', '--no-lsp', '--no-prewalk', '--no-tools', '--config', str(overlay), '--extension',
                str(root / 'runtime/omp_bridge/g3/entry.ts'), '--extension', str(root / 'runtime/omp_bridge/g3/summary_priority_verification.ts'),
                '--model', 'ownedverify/scripted', '--cwd', str(root / 'project')]
            launch = {'argv': argv, 'cwd': str(root / 'project'), 'env_names': sorted(env), 'credential_environment_entries': 0,
                'stdin_policy': 'Manager draft/CtrlU only, Enter0; Worker stdin0; RPC JSON0',
                'environment_source': 'constructed runtime allowlist plus fixed owned TUI/G3',
                'configuration': {name: artifact(root / name) for name in ('agent/models.yml', 'overlay.yml', 'fixture.json')}}
            data['launches'][role] = launch
            check()
            master, slave = os.openpty()
            masters[role] = master
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 100, 0, 0))
            try:
                launch['requested_ns'] = now()
                process = subprocess.Popen(argv, cwd=root / 'project', env=env, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            finally:
                os.close(slave)
            ref = {'pid': process.pid, 'pgid': process.pid, 'start_ticks': rt._identity(process.pid), 'role': role}
            owned[role] = (process, ref)
            launch['owned_process'] = ref
            data['native_launches'] += 1
            write_private_json(OUT / (role + '-launch.json'), {**ref, 'deadline_monotonic': deadline, 'observed_ns': now()})
            _persist_processes(top, data)
            assert ref['start_ticks'] is not None
            until = time.monotonic() + .5
            while os.getpgid(process.pid) != process.pid and time.monotonic() < until:
                time.sleep(.005)
            assert os.getpgid(process.pid) == process.pid
            launch['setsid_no_fork_verified'] = True
            launch['actual_pid_cwd_before'] = os.readlink(f'/proc/{process.pid}/cwd')
            os.set_blocking(master, False)
            stop = threading.Event()
            stops[role] = stop
            def drain_pty(role=role, fd=master, stop=stop):
                with (OUT / (role + '-tty.log')).open('wb') as output:
                    while not stop.is_set():
                        try:
                            if select.select([fd], [], [], .05)[0]:
                                raw = os.read(fd, 65536)
                                if not raw:
                                    return
                                tty[role]['bytes_seen'] += len(raw)
                                saved = raw[:max(0, 262144 - tty[role]['bytes_saved'])]
                                output.write(saved)
                                tty[role]['bytes_saved'] += len(saved)
                                tty[role]['truncated'] |= len(saved) < len(raw)
                        except OSError:
                            return
            drain = threading.Thread(target=drain_pty, name=role + '-PTY', daemon=True)
            drains[role] = drain
            drain.start()
        for role, root in roots.items():
            wait_for(lambda root=root: (root / 'ownerready.json').exists(), max(0, deadline - time.monotonic()))
            ready = json.loads((root / 'ownerready.json').read_text())
            if ready.get('error'):
                raise RuntimeError(role + ':' + ready['error'])
            readies[role] = ready
            peers[role] = bridge.peer(role, timeout=1)
            parent_ready = ready['metadata'] if role == 'manager' else ready['parent']
            assert peers[role].pid == owned[role][0].pid and peers[role].session_id == parent_ready['parentSessionId']
            assert parent_ready['model']['tokenizer'] == 'claude-v3'
            state = probe(role, 'ready_before_child')
            assert state['idle'] is True and state['pending'] is False and state['editorKnown'] is True and state['editorEmpty'] is True
            data.setdefault('hello', {})[role] = {'session_id': peers[role].session_id, 'generation': peers[role].generation, 'pid': peers[role].pid}
        ready = readies['manager']
        metadata = deepcopy(ready['metadata'])
        class Authority:
            def current(self):
                if violations or time.monotonic() >= deadline:
                    return False
                peer = bridge.peer('manager')
                return peer == peers['manager'] and sink.authority(original_request['subject'], active=True, paused=False, revoked=False, has_new_input=True) == authority
            def claim(self):
                return self.current()
        adapter = SDKSummaryAdapter(bridge, timeout=5)
        def count(query):
            check()
            row = {'sent_ns': now(), 'ownerDigest': query['ownerDigest'], 'fragmentDigests': query['fragmentDigests'],
                'fragmentBytes': [len(text.encode()) for text in query['fragments']]}
            data['count_queries'].append(row)
            proof = adapter.count_fragments(query, expected_peer=(peers['manager'].session_id, peers['manager'].generation), authority_token=Authority())
            row.update(proof=proof, returned_ns=now())
            if (not isinstance(proof, dict) or proof.get("exact") is not True
                    or proof.get("estimator") != "native_known_family_strict"
                    or proof.get("encoding") != "ClaudeV3"):
                raise RuntimeError("owned_strict_ClaudeV3_count_unavailable")
            return proof
        prepared = prepare_summary(original_request, pieces, metadata=metadata, instructions=ready['definition'], prior_cumulative=None, count_fragments=count)
        selected = [span for item in prepared['selected'] for span in item.get('ranges', [item.get('range')])]
        omitted = [item['range'] for item in prepared['omitted']]
        assert selected and sorted(selected + omitted, key=lambda span: span['start']) == originals
        assert len({canonical_json(span) for span in selected + omitted}) == len(originals)
        data['prepared'] = {'request': prepared['request'], 'budget': prepared['budget'], 'countBinding': prepared.get('countBinding'),
            'selected_ranges': selected, 'omitted': prepared['omitted'], 'definitionDigest': prepared['definitionDigest'],
            'metadata': metadata, 'whole_range_identity_preserved': True,
            'selected_count': len(selected), 'omitted_count': len(omitted),
            'omission_observed': bool(omitted)}
        encoded = canonical_json(prepared)
        data['prepared_owner_file'] = {'bytes': len(encoded), 'sha256': sha256(encoded).hexdigest(), 'writes': 1}
        database = operator / 'tasks.sqlite3'
        repository = TaskRepository(database)
        database.chmod(0o600)
        shell_env = {**main_env, 'HOME': str(operator / 'home')}
        pane = ShellPane(ShellChoice('bash', '/usr/bin/bash'), shell_env)
        data['owned_shell'] = {'pid': pane.pid, 'start_ticks': rt._identity(pane.pid), 'pgid': os.getpgid(pane.pid), 'environment_names': sorted(shell_env)}
        _persist_processes(top, data)
        def pump_shell():
            while not pump_stop.is_set():
                pane.pump()
                time.sleep(.01)
        pump = threading.Thread(target=pump_shell, name='owned-ShellPane-pump', daemon=True)
        pump.start()
        wait_for(lambda: pane.automation_busy() is None, 5)
        raw_store = RawLogStore(operator / 'raw')
        lifecycle = LifecycleJournal(operator / 'lifecycle.json')
        clock = [0.0]
        controller = AutomationController(bridge=bridge, database=database, journal=lifecycle, raw=raw_store,
            shell_pane=lambda: pane, project_dir=source, artifacts_root=operator / 'artifacts',
            boot_marker=lambda: Path('/proc/sys/kernel/random/boot_id').read_text().strip(), clock=lambda: clock[0],
            log=lambda value: data['automation_logs'].append({'observed_ns': now(), 'text': value}))
        automation_source = controller.pause_policy.automation_state
        execution = {'source': str(source), 'commit': commit, 'command': './run.sh',
            'criteria': {'log_contains': 'PASS', 'result_file': 'PASS.txt', 'result_contains': 'PASS'},
            'environment': sorted(shell_env), 'shell': 'bash'}
        task = repository.create_task({'goal': 'Owned public Core finite fixture', 'paths': ['PASS.txt'], 'execution': execution})
        repository.approve_scope(task, 1, {'paths': ['PASS.txt'], 'execution': execution})
        repository.proceed(task, 1, 'Owned finite Core fixture only')
        mailbox = TaskMailbox(repository, bridge)
        workflow = TaskWorkflow(repository, mailbox, worker_port=G3WorkerResponsePort(bridge, timeout=10), automation_source=automation_source)
        port = HostShellPort(pane, lambda: pane)
        cursor = bridge.event_cursor()
        def before_shell_input():
            if controller.paused():
                raise WorkflowHeld('paused')
            if port.busy() is not None or port.hold(HOLD_REASON) is not None:
                raise WorkflowHeld('host_terminal_busy')
            data['public_hold'] = {'reason': HOLD_REASON, 'held_ns': now(), 'callback': 'before_shell_input'}
        workflow_run = workflow.start(task, 1, worktree_path=operator / 'worktree', artifacts_root=operator / 'artifacts',
            automation=automation_source(), shell=port, before_shell_input=before_shell_input)
        port.release_hold(HOLD_REASON)
        data['public_hold_release_ns'] = now()
        released = port.poll(0)
        data['after_hold_release'] = released
        if released['input_owner'] != 'manager' or released['parent_mode'] != 'control_wait':
            raise WorkflowHeld('manager_control_wait_not_observed_after_release')
        live_state = workflow_run.shell.snapshot()
        data['owned_managed_lifecycle'] = live_state.get('lifecycle', {})
        supervisor = live_state.get('lifecycle', {}).get('supervisor_pid')
        if type(supervisor) is int and supervisor > 0:
            data['managed_supervisor'] = {'pid': supervisor, 'start_ticks': rt._identity(supervisor), 'pgid': os.getpgid(supervisor)}
        _persist_processes(top, data)
        data['workflow'] = {'task_id': task, 'revision': 1, 'run_id': workflow_run.run_id, 'task_message_id': workflow_run.task_message_id,
            'record': json.loads(workflow_run.result_path.read_text())}
        controller.experiment_started(workflow_run)
        data['pause_status_before_tick'] = controller.pause_policy.status().to_dict()
        record_dir = Path(workflow_run.record_dir)
        baseline = record_dir / 'entry-file-baseline.json'
        pause_journal = record_dir / 'pause.jsonl'
        journal_events = [json.loads(line) for line in pause_journal.read_text().splitlines() if line] if pause_journal.exists() else []
        data['entry_baseline_observation'] = {'record_dir': str(record_dir),
            'path': str(baseline), 'exists': baseline.is_file(), 'artifact': artifact(baseline) if baseline.is_file() else None,
            'journal_path': str(pause_journal), 'journal_exists': pause_journal.is_file(),
            'journal_artifact': artifact(pause_journal) if pause_journal.is_file() else None,
            'entry_baseline_recorded_events': [event for event in journal_events if event.get('kind') == 'entry_baseline_recorded']}
        if data['pause_status_before_tick']['persistence_error'] is not None:
            raise RuntimeError('public_pause_persistence_error_before_tick:' + data['pause_status_before_tick']['persistence_error'])
        data['controller_bound'] = controller.status()
        bound = data['controller_bound']['run']
        if not bound or bound['bound'] is not True or bound['error'] is not None:
            raise RuntimeError('actual_core_run_not_bound')
        durable = lifecycle.read()
        if durable is None:
            raise RuntimeError('actual_lifecycle_journal_missing')
        data['lifecycle_journal'] = asdict(durable)
        data["tick0"] = controller.tick()
        if data["tick0"]["outcome"] != "admitted":
            raise WorkflowHeld("actual_Core_tick0_not_admitted")
        with TaskRepository(layout.tasks) as journal:
            journal.reserve_summary_request(prepared['request'])
            journal.record_summary_state(prepared['request']['requestId'], 'sending')
        path = roots['manager'] / 'prepared.tmp'
        path.write_bytes(encoded)
        path.chmod(0o600)
        path.rename(roots['manager'] / 'prepared.json')
        while not hold.seen.wait(.01):
            check()
            if (roots['manager'] / 'completed.json').exists():
                completed = json.loads((roots['manager'] / 'completed.json').read_text())
                data['dispatch_feasibility'] = 'input_upper_dispatch_refused' if completed.get('outcome', {}).get('sent') is False else 'input_upper_dispatch_failed_before_POST'
                raise RuntimeError(data['dispatch_feasibility'])
        data['dispatch_feasibility'] = 'validated_upper_child_POST_held'
        data["Core_lifecycle_at_child_hold"] = workflow_run.shell.snapshot().get("lifecycle", {})
        clock[0] = 60.0
        question_start = now()
        data["tick60"] = controller.tick()
        periodic_posts = [row for row in data["POSTs"] if row.get("stage") == "periodic_review"]
        if data["tick60"]["outcome"] != "admitted" or len(periodic_posts) != 1:
            raise RuntimeError("actual_periodic_QUESTION_POST_missing_or_not_admitted")
        question_post = periodic_posts[0]
        message = repository.get_message(question_post["message_id"])
        history = repository.get_delivery_history(question_post["delivery_attempt_id"])
        _, events = bridge.events_after(("assistant_message_end", "delivery_omp_processed", "worker_response_rejected", "agent_start", "agent_end"), cursor)
        if message["task_id"] != task or message["run_id"] != workflow_run.run_id or not any(row["status"] == "omp_processed" for row in history):
            raise RuntimeError("actual_periodic_QUESTION_binding_or_receipt_missing")
        if not any(row.get("name") == "delivery_omp_processed" and row.get("messageId") == question_post["message_id"] for row in events):
            raise RuntimeError("actual_periodic_QUESTION_native_processed_missing")
        data["Core_periodic_question"] = {"requested_ns": question_start, "returned_ns": now(),
            "message": message, "delivery_history": history, "accepted_events": events,
            "status": controller.status(), "lifecycle_at_return": workflow_run.shell.snapshot().get("lifecycle", {})}
        baseline = probe('manager', 'held_before_draft')
        assert baseline['idle'] is True and baseline['pending'] is False and baseline['editorEmpty'] is True
        draft = b'DVL14-PRIVATE-UNSENT-DRAFT'
        data['draft'] = {'bytes': len(draft), 'sha256': sha256(draft).hexdigest(), 'sent_ns': now(), 'Enter_sent': False}
        os.write(masters['manager'], draft)
        time.sleep(.08)
        state = probe('manager', 'held_draft')
        assert state['editorKnown'] is True and state['editorEmpty'] is False
        task_id = task
        mframe = {'kind': 'notice', 'notice': {'type': 'worker_terminal_done', 'notice_id': notice_ids['manager'], 'task_id': task_id, 'status': 'finished'}}
        data['draft_ack'] = request('manager', mframe)
        assert data['draft_ack']['status'] == 'deferred'
        active = ActiveRunRef(task_id=task, revision_id=question_post["revision_id"], revision=1,
            run_id=workflow_run.run_id, session_id=peers["worker"].session_id, session_generation=peers["worker"].generation)
        notice_clock, dispatched = [0.0], []
        def dispatch_review(review):
            enqueue = now()
            frame = {'kind': 'notice', 'notice': {'type': 'terminal_check', 'notice_id': notice_ids['worker'],
                'task_id': review.run.task_id, 'run_id': review.run.run_id, 'due_at': review.due_at}}
            ack = request('worker', frame)
            row = {'notice_id': notice_ids['worker'], 'enqueue_ns': enqueue, 'ack': ack}
            dispatched.append(row)
            wait_for(lambda: any(p.get('notice_id') == notice_ids['worker'] for p in data['POSTs']), 1)
            wait_for(lambda: bool(bridge.accepted_events('worker', 'agent_end', enqueue)), 1)
            return {'status': 'omp_processed' if ack.get('status') == 'api_accepted' else 'deferred'}
        scheduler = WorkerReviewScheduler(admission=SerializedReviewAdmission(active, automation_source()), automation_state=automation_source,
            active_run=lambda: active, worker_state=lambda _run: probe('worker', 'scheduler_state'),
            collect_non_model=lambda _run: {'phase': 'running', 'process_alive': owned['worker'][0].poll() is None,
                'exit_confirmed': False, 'raw_log_excerpt': 'owned fixture process', 'unknowns': []},
            dispatch_review=dispatch_review, user_priority=lambda _run: False, on_exit=lambda event: None, clock=lambda: notice_clock[0])
        t0 = scheduler.tick()
        notice_clock[0] = 60
        t60 = scheduler.tick()
        data['scheduler'] = {'tick0': t0.status, 'tick60': t60.status, 'notice_dispatches': dispatched, 'injected_clock': [0, 60]}
        assert t60.status == 'dispatched'
        os.write(masters['manager'], b'\x15')
        time.sleep(.08)
        state = probe('manager', 'held_clear')
        assert state['editorKnown'] is True and state['editorEmpty'] is True
        menqueue = now()
        data['end_ack'] = request('manager', mframe)
        wait_for(lambda: any(p.get('notice_id') == notice_ids['manager'] for p in data['POSTs']), 1)
        wait_for(parent_hold.is_set, 1)
        busy_parent = probe('manager', 'busy_parent_during_summary')
        data['busy_parent_during_summary'] = {'state': busy_parent,
            'child_still_held': not hold.done.is_set(), 'observed_ns': now()}
        if busy_parent.get('idle') is not False or hold.done.is_set():
            raise RuntimeError('actual_busy_parent_overlap_unobserved')
        parent_release.set()
        wait_for(lambda: bool(bridge.accepted_events('manager', 'agent_end', menqueue)), 1)
        if hold.done.is_set():
            raise TimeoutError("child_hold_released_before_priority_observations")
        data['release_requested_ns'] = now()
        hold.release.set()
        wait_for(lambda: hold.done.is_set(), 1)
        windows = []
        for role, kind, enqueue in (('manager', 'worker_terminal_done', menqueue), ('worker', 'terminal_check', dispatched[0]['enqueue_ns'])):
            start_rows = bridge.accepted_events(role, 'agent_start', enqueue)
            end_rows = bridge.accepted_events(role, 'agent_end', enqueue)
            if len(start_rows) != 1 or len(end_rows) != 1:
                raise RuntimeError('accepted_event_attribution_not_unique')
            rec = next(row for row in data['requests'] if row['role'] == role and row['frame'].get('notice', {}).get('notice_id') == notice_ids[role] and row.get('ack', {}).get('status') == 'api_accepted')
            post = next(row for row in data['POSTs'] if row.get('notice_id') == notice_ids[role])
            start_ns, end_ns = start_rows[0]['host_receive_ns'], end_rows[0]['host_receive_ns']
            ack_ns, post_ns = rec['ack_observed_ns'], post['provider_post_ns']
            release = hold.facts['release_ns']
            windows.append({'role': role, 'kind': kind, 'session_id': peers[role].session_id, 'notice_id': notice_ids[role],
                'ack_notice_id': rec['ack'].get('noticeId'), 'ack_request_id': rec['ack'].get('requestId'), 'ack_status': rec['ack']['status'],
                'provider_notice_id': notice_ids[role], 'provider_stage': 'parent', 'scheduler_notice_id': notice_ids[role] if role == 'worker' else None,
                'enqueue_ns': enqueue, 'frame_request_ns': rec['sent_ns'], 'ack_observed_ns': ack_ns, 'agent_start_ns': start_ns,
                'provider_post_ns': post_ns, 'agent_end_ns': end_ns, 'enqueue_to_ack_ms': (ack_ns - enqueue) / 1e6,
                'enqueue_to_start_ms': (start_ns - enqueue) / 1e6, 'enqueue_to_post_ms': (post_ns - enqueue) / 1e6,
                'turn_hold_overlap_ms': max(0, min(end_ns, release) - max(start_ns, hold.facts['validated_post_ns'])) / 1e6})
        wait_for(lambda: (roots['manager'] / 'completed.json').exists(), max(0, deadline - time.monotonic()))
        completed = json.loads((roots['manager'] / 'completed.json').read_text())
        child_event = next((row for row in completed.get('events', []) if row.get('event') == 'owned_child_created'), {})
        measurement = {'kind': 'delivery_turn_overlap', 'clock': 'time.monotonic_ns',
            'session_ids': {'manager': peers['manager'].session_id, 'worker': peers['worker'].session_id, 'child': child_event.get('sessionId')},
            'child_hold_interval_ns': [hold.facts['validated_post_ns'], hold.facts['release_ns']], 'internal_lock_wait': 'unavailable',
            'due_scheduler_status': t60.status, 'notice_windows': windows,
            'max_enqueue_to_start_ms': max(row['enqueue_to_start_ms'] for row in windows),
            'max_enqueue_to_post_ms': max(row['enqueue_to_post_ms'] for row in windows)}
        data['measurement'] = measurement
        if hold.facts.get("explicit_release") is not True:
            raise TimeoutError("child_hold_automatic_release")
        data["Core_periodic_question"]["returned_before_child_release"] = data["Core_periodic_question"]["returned_ns"] < hold.facts["release_ns"]
        wait_for(lambda: (operator / "worktree/PASS.txt").exists(), 8)
        def finite_exit_confirmed():
            state = workflow_run.shell.poll(.02)
            life = state.get("lifecycle", {})
            return life.get("main_exit") == 0 and life.get("lifetime") == "ended"
        wait_for(finite_exit_confirmed, 3)
        data["finite_command"] = {"PASS_file_sha256": sha256((operator / "worktree/PASS.txt").read_bytes()).hexdigest(),
            "shell_state": workflow_run.shell.poll(.05)}
        outcome = completed.get("outcome", {})
        if outcome.get("status") != "summary_result" or completed.get("childCreates") != 1 or completed.get("childDisposed") is not True or completed.get("toolCalls") != 1:
            raise RuntimeError("upper_child_fixed_writer_or_disposal_unobserved")
        child_event = next((event for event in completed.get("events", []) if event.get("event") == "owned_child_created"), {})
        if child_event.get("sessionId") in (peers["manager"].session_id, peers["worker"].session_id) or child_event.get("active") != ["db_summary_save"] or child_event.get("nativeRegistrySame") is not True or child_event.get("globalAbsent") is not True:
            raise RuntimeError("owned_child_identity_or_registry_unobserved")
        if not data["Core_periodic_question"]["returned_before_child_release"]:
            raise RuntimeError("Core_question_not_processed_during_child_hold")
        data["coverage_complete"] = True
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, RuntimeError, AssertionError, subprocess.SubprocessError) as error:
        data['errors'].append(type(error).__name__ + ':' + str(error))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        parent_release.set()
        hold.release.set()
        if port is not None:
            port.release_hold(HOLD_REASON)
        if controller is not None:
            if workflow_run is not None:
                controller.run_ended(workflow_run.run_id)
            controller.close()
        if workflow_run is not None:
            workflow_run.close()
        elif port is not None:
            port.detach()
        pump_stop.set()
        if pump is not None:
            pump.join(2)
        if pane is not None:
            data["shell_close"] = pane.close()
        if raw_store is not None:
            raw_store.close()
        if repository is not None:
            repository.close()
        for role, (process, ref) in owned.items():
            data['launches'][role]['live_peer_observation_ns'] = now()
            try:
                peer_git = data["peer_git_fixture"][role]
                peer_git["head_after"] = git("-C", str(roots[role] / "project"), "rev-parse", "--verify", "HEAD")
                peer_git["status_after"] = git("-C", str(roots[role] / "project"), "status", "--porcelain")
                peer_git["unchanged"] = peer_git["head_after"] == peer_git["head_before"] and peer_git["status_after"] == peer_git["status_before"]
                if rt._identity(process.pid) == ref["start_ticks"]:
                    data["launches"][role]["actual_pid_cwd_after"] = os.readlink(f"/proc/{process.pid}/cwd")
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                data["errors"].append("peer_observation:" + str(error))

        # Both markers share one deadline; capture live cwd/HEAD before either.
        request_ns, request_wall_ns = now(), time.time_ns()
        termination_deadline_ns = request_ns + 5000000000
        for role in owned:
            stop = {"stop_request_ns": request_ns, "stop_request_wall_ns": request_wall_ns,
                "termination_deadline_ns": termination_deadline_ns, "common_deadline": True,
                "registry_view": "observed owned metadata only; missing metadata/unobserved writers unknown"}
            data["launches"][role]["owned_graceful_stop"] = stop
            try:
                write_private_json(roots[role] / "owned-stop.json", {"kind": "owned-graceful-stop"})
                stop["marker_written_ns"] = now()
            except (OSError, ValueError, KeyError, TypeError) as error:
                stop["marker_error"] = type(error).__name__ + ":" + str(error)
                data["errors"].append("cleanup:" + role + ":owned_stop_marker:" + stop["marker_error"])

        for role, (process, ref) in owned.items():
            stop = data["launches"][role]["owned_graceful_stop"]
            stop.update(wait_started_ns=now(), wait_started_wall_ms=time.time_ns() // 1000000)
            try:
                process.wait(timeout=max(0, (termination_deadline_ns - now()) / 1000000000))
                stop["method"] = "graceful_exit"
            except subprocess.TimeoutExpired:
                data["errors"].append("cleanup:" + role + ":public_shutdown_timeout")
                if process.poll() is None:
                    stop["method"] = "sigterm_fallback"
                    stop["fallback_requested_ns"] = now()
                    try:
                        error = rt._stop_leader(process, ref)
                        if error:
                            data["errors"].append(error)
                    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, RuntimeError, AssertionError, subprocess.SubprocessError) as error:
                        data["errors"].append("cleanup:" + role + ":fallback:" + str(error))
                else:
                    stop["method"] = "exited_after_deadline"
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, RuntimeError, AssertionError, subprocess.SubprocessError) as error:
                data["errors"].append("cleanup:" + role + ":" + str(error))
            finally:
                stop.update(returncode=process.returncode, wait_returned_ns=now(),
                    wait_returned_wall_ns=time.time_ns())
                stop["wait_returned_wall_ms"] = stop["wait_returned_wall_ns"] // 1000000
            if process.returncode != 0:
                data["errors"].append("cleanup:" + role + ":public_shutdown_nonzero_or_unobserved")
            stop.update(barrier_started_ns=now(), barrier_started_wall_ns=time.time_ns())
            try:
                while True:
                    states = _daemon_states(roots[role])
                    stop["last_registry_metadata"] = states
                    if all(row["state"] == "exited" for row in states):
                        stop["barrier_terminal"] = now() <= termination_deadline_ns
                        break
                    if now() >= termination_deadline_ns:
                        stop["barrier_terminal"] = False
                        break
                    time.sleep(min(.01, max(0, (termination_deadline_ns - now()) / 1000000000)))
                if not stop["barrier_terminal"]:
                    data["errors"].append("cleanup:" + role + ":owned_registry_common5s_deadline")
            except (OSError, ValueError, KeyError, TypeError) as error:
                stop["barrier_terminal"] = False
                stop["barrier_error"] = type(error).__name__ + ":" + str(error)
                data["errors"].append("cleanup:" + role + ":owned_registry_read:" + stop["barrier_error"])
            finally:
                stop.update(barrier_returned_ns=now(), barrier_returned_wall_ns=time.time_ns())
        parent_observations = {}
        for role, root in roots.items():
            if (root / 'ownerready.json').exists():
                final = json.loads((root / 'ownerready.json').read_text())
                before = readies.get(role, {}).get('before')
                after = final.get('after')
                row = {'before': before, 'after': after, 'afterError': final.get('afterError'), 'afterSource': final.get('afterSource')}
                if before is not None and after is not None:
                    keys = ['model', 'thinkingLevel', 'serviceTierByFamily', 'registry', 'settings', 'roster']
                    row['strict_parent_unchanged'] = all(before[key] == after[key] for key in keys)
                    row['entries_delta'] = after['entries'] - before['entries']
                    before_ids = {entry["id"] for entry in before["allEntries"]}
                    row["new_entry_views"] = [entry for entry in after["allEntries"] if entry["id"] not in before_ids]
                    row["entry_delta_attribution"] = "Actual notice IDs plus Core TASK/periodic message IDs; native entry view only, exact storage identity unknown."
                    row['raw_parent_contamination'] = after['rawUpperMarkerSeen']
                    unchanged_views = {entry["id"]: entry for entry in after["allEntries"]}
                    row["prior_entry_views_unchanged"] = all(unchanged_views.get(entry["id"]) == entry for entry in before["allEntries"])
                    if not row["strict_parent_unchanged"] or row["raw_parent_contamination"] or not row["prior_entry_views_unchanged"]:
                        data["errors"].append(role + ":parent_invariant_or_contamination_changed")
                else:
                    data["errors"].append(role + ":parent_shutdown_observation_unavailable")
                parent_observations[role] = row
            if role == 'manager' and (root / 'completed.json').exists():
                completed = json.loads((root / 'completed.json').read_text())
        if completed and 'prepared' in data:
            try:
                with TaskRepository(layout.tasks) as journal:
                    record = journal.get_summary_request(data['prepared']['request']['requestId'])
                    receipt_count = journal._connection.execute('SELECT COUNT(*) FROM summary_result_receipts_v1').fetchone()[0]
                data['journal'] = {'state': record['state'], 'receipt': record['receipt'], 'receipt_count': receipt_count}
                if completed.get('outcome', {}).get('status') == 'summary_result':
                    outcome = completed['outcome']
                    with ImportantLogStore.open(layout.important_logs, catalog.active_uuid(), readonly=True) as store:
                        entry = store.get_entry(outcome['writer']['entryId'])
                        total = store._connection.execute('SELECT COUNT(*) FROM entries').fetchone()[0]
                    data['writer'] = {'total_catalog_entries': total, 'summary_entries_added': total - data['catalog_initial_entries'],
                        'actual_body': entry['body'], 'subject_matches': entry['subject'] == record['request']['subject'],
                        'sources_matches': entry['sources'] == record['request']['sources'],
                        'receipt_result_digest_matches': record['receipt']['result_digest'] == sha256(canonical_json(outcome['result'])).hexdigest()}
                    expected_body = {"schemaVersion": 1, "facts": ["소유 private scripted fixture의 연결을 확인했다."],
                        "interpretations": [], "unknowns": ["실제 MODEL 및 전체 Q01은 미검증이다."]}
                    data["writer"]["exact_fixture_body_matches"] = entry["body"] == expected_body
                    if record["state"] != "stored" or receipt_count != 1 or total - data["catalog_initial_entries"] != 1 or not all(data["writer"][key] for key in ("subject_matches", "sources_matches", "receipt_result_digest_matches", "exact_fixture_body_matches")):
                        raise RuntimeError("owned_upper_writer_or_receipt_mismatch")
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, RuntimeError, AssertionError, subprocess.SubprocessError) as error:
                data['errors'].append('writer_observation:' + str(error))
        if bridge is not None:
            data['accepted_frames'] = list(bridge.observed)
            data['accepted_trace_overflow'] = bridge.observation_overflow
            bridge.close()
        for role, server in servers.items():
            server.shutdown()
            server.shutdown_accepted()
            handler_threads += tuple(server._threads) if isinstance(server._threads, list) else ()
            server.server_close()
            serves[role].join(3)
        for role, stop in stops.items():
            stop.set()
            drains[role].join(2)
        for master in masters.values():
            os.close(master)
        for thread in tuple(threading.enumerate()):
            if thread is not threading.main_thread():
                thread.join(.5)
        groups = {role: ref['pgid'] for role, (process, ref) in owned.items()}
        if "owned_shell" in data:
            groups["host_shell"] = data["owned_shell"]["pgid"]
        if "managed_supervisor" in data:
            groups["managed_supervisor"] = data["managed_supervisor"]["pgid"]
        child_group = data.get("owned_managed_lifecycle", {}).get("child_group")
        if type(child_group) is int and child_group > 0:
            groups["managed_child_group"] = child_group
        remaining = _remaining_until(groups, termination_deadline_ns)
        live = [thread.name for thread in threading.enumerate() if thread is not threading.main_thread()]
        sockets = sum(server.accepted_count() for server in servers.values())
        safe = (not any(remaining.values()) and not live and not sockets
                and not data.get("shell_close", {}).get("survivors")
                and all(process.returncode is not None for process, ref in owned.values())
                and not any(error.startswith(("cleanup:", "owned_process_identity", "owned_cleanup")) for error in data["errors"]))
        data['cleanup'] = {'remaining_pgid_members': remaining, 'owned_live_threads': live, 'http_sockets_remaining': sockets,
            'http_handlers_alive': sum(thread.is_alive() for thread in handler_threads), 'complete': safe,
            'returncodes': {role: process.returncode for role, (process, ref) in owned.items()}}
        if not safe:
            data["retained_private_root"] = str(top)
        data["coverage_complete"] = data["coverage_complete"] and safe and not data["errors"] and not violations
        data.update(completed=completed, parents=parent_observations, hold=hold.facts, provider_violations=violations,
            product_child_POSTs=child_posts, tty=tty, elapsed_seconds=time.monotonic() - started,
            artifacts={path.name: artifact(path) for path in OUT.iterdir() if path.name.endswith(('.py', '.ts', '.log'))})
        data["limitations"].append("Successful owned root cleanup removes private definitions/prepared bytes/native cache/DB; bounded hashes and observations remain.")
        write_private_json(top / "priority-result.json", data)
    return data

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--owned-priority" or not sys.flags.safe_path or not sys.flags.no_user_site:
        raise SystemExit(2)
    root = Path(sys.argv[2])
    rt._private_root(root)
    if root != root.resolve() or Path(os.environ["HOME"]).resolve() != root / "home" or Path.cwd().resolve() != root / "project" or os.environ.get("PYTHONPATH") != str(root / "runtime/src"):
        raise SystemExit("unsafe_private_helper_environment")
    owner = _owned_json(root / "priority-owner.json")
    if owner["root"] != str(root):
        raise SystemExit("owned_priority_binding_unavailable")
    binary = Path(owner["binary"])
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK) or sha256(binary.read_bytes()).hexdigest() != owner["binary_sha256"]:
        raise SystemExit("owned_binary_identity_unavailable")
    def abort(signum, frame):
        raise TimeoutError("priority_inner90s_deadline")
    signal.signal(signal.SIGALRM, abort)
    signal.signal(signal.SIGTERM, abort)
    body_budget = owner["body_budget_seconds"]
    if type(body_budget) not in (int, float) or not 0 < body_budget <= 90:
        raise SystemExit("owned_priority_budget_unavailable")
    signal.setitimer(signal.ITIMER_REAL, body_budget)
    _execute(root, binary, body_budget)
