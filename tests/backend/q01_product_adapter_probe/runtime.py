"""Fresh installed SDK/product writer observations, through the frozen product port.

Missing observations remain unknown. This driver never imports an old verdict
or substitutes the probe-local SDK implementation for product dispatch.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4

from workbench.backend.summary_adapter import SourcePiece, prepare_summary, skill_bytes
from workbench.contracts.logs_v1 import canonical_json
from workbench.storage.important_log import ImportantLogCatalog, ImportantLogReader
from workbench.tasks.repository import InvalidTransitionError, TaskRepository
from tests.backend.q01_sdk_full_probe.full_probe import _request  # fixture construction only

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
CASES = ("positive", "positive2", "positive3", "startup_rebuild", "startup_repeat",
         "missing_sdk", "worker_role", "saved_then_bash", "duplicate", "request_mismatch",
         "source_mismatch", "selected_mismatch", "prior_mismatch", "late_before_commit",
         "startup_missing", "startup_noop", "startup_stale", "startup_api",
         "runtime_roster", "runtime_model", "runtime_stale", "runtime_policy",
         "metadata_epoch", "metadata_thinking", "metadata_tier", "metadata_nested_model",
         "metadata_replace", "prepared_metadata", "sdk_tier_clone")
NO_SEND = set(CASES) - {"positive", "positive2", "positive3", "startup_rebuild",
                       "saved_then_bash", "duplicate", "late_before_commit", "sdk_tier_clone"}


def text_content(value):
    if isinstance(value, str):
        return value
    return "".join(part.get("text", "") for part in value or [] if part.get("type") == "text")


class Provider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, runtime):
        self.runtime = runtime
        super().__init__(("127.0.0.1", 0), Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        owner = self.server.runtime
        size = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(size)
        request = json.loads(raw)
        text = json.dumps(request)
        case = next((name for name, value in owner.requests.items() if value["requestId"] in text), "unattributed")
        notice_id=next((n for n in getattr(owner,"notice_ids",[]) if n in text),None)
        if notice_id: case="ordinary_parent"
        if case == "unattributed" and "Q01_PRODUCT_MAIN_AFTER_FAILURE" in text:
            case = "ordinary_parent"
        tools = [tool.get("function", {}).get("name") for tool in request.get("tools", [])]
        system = [text_content(message.get("content")) for message in request.get("messages", [])
                  if message.get("role") == "system"]
        definition = (owner.private / "definition.txt").read_text() if (owner.private / "definition.txt").exists() else ""
        row = {"case": case, "notice_id": notice_id, "monotonic_ns": time.monotonic_ns(), "body_bytes": size,
               "sha256": sha256(raw).hexdigest(), "tools": tools,
               "cap": request.get("max_completion_tokens"),
               "system_exact": system == [definition + "\n\n" + skill_bytes().decode()]}
        with owner.lock:
            owner.posts.append(row)
            index = len(owner.posts)
            row["body_path"] = str(owner.private / f"post-{index:03d}.json")
        Path(row["body_path"]).write_bytes(raw)
        if case in getattr(owner, "no_send", NO_SEND) or case == "unattributed" or (case == "ordinary_parent" and tools != ["read"]) or (
                case != "ordinary_parent" and tools != ["db_summary_save"]):
            owner.stop_reason = "forbidden_provider_post:" + case
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if getattr(owner, "held_case", None) == case and not owner.held.is_set():
            owner.held.set()
            owner.release.wait(5)
        count = sum(message.get("role") == "tool" for message in request.get("messages", []))
        name = None if case == "ordinary_parent" else "db_summary_save" if count == 0 or case == "duplicate" and count == 1 else (
            "bash" if case == "saved_then_bash" and count == 1 else None)
        params = {"facts": ["실행한 작업과 확인한 검증 결과"], "interpretations": [], "unknowns": []}
        if name == "bash":
            params = {"command": "echo CHANGED > " + str(owner.private / "project/sentinel")}
        if name:
            call = {"id": case + "-" + str(count), "type": "function",
                    "function": {"name": name, "arguments": json.dumps(params, ensure_ascii=False)}}
            delta = {"role": "assistant", "tool_calls": [{"index": 0, **call}]}
        else:
            delta = {"role": "assistant", "content": "done"}
        frame = {"id": "owned-product", "object": "chat.completion.chunk", "created": 0,
                 "model": "scripted", "choices": [{"index": 0, "delta": delta,
                  "finish_reason": "tool_calls" if name else "stop"}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        data = ("data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


class ProductRuntime:
    """Live process transport, not a facts-list invocation envelope."""
    def __init__(self, output: Path):
        self.output = output.resolve()
        self.private = self.output / "private"
        self.requests = {}
        self.prepared = {}
        self.posts = []
        self.lock = threading.Lock()
        self.stop_reason = None
        self.process = None
        self.provider = None
        self.thread = None
        self.streams = []
        self.closed = False
        self.route = None
        self.report = None

    def prepare_route(self, context):
        if self.process is not None:
            raise RuntimeError("runtime_cannot_be_reused")
        self.provider = Provider(self)
        self.thread = threading.Thread(target=self.provider.serve_forever, daemon=True)
        self.thread.start()
        for name in ("agent", "config"):
            (self.private / name).mkdir(mode=0o700, exist_ok=True)
        mirror = self.private / "product-mirror"
        mirrors = ("omp_bridge/g3/summary_sdk.ts", "omp_bridge/contract/logs_v1.ts",
                   "omp_bridge/skills/important-log-summary/SKILL.md",
                   "src/workbench/contracts/summary_compat_policy_v1.json")
        self.mirror_hashes = {}
        for name in mirrors:
            target = mirror / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(ROOT / name, target)
            digest = sha256(target.read_bytes()).hexdigest()
            if digest != sha256((ROOT / name).read_bytes()).hexdigest():
                raise RuntimeError("product_mirror_not_byte_identical")
            self.mirror_hashes[name] = digest
        (self.private / "project/sentinel").write_text("ORIGINAL")
        (self.private / "agent/models.yml").write_text(
            "providers:\n  currentproduct:\n    api: openai-completions\n    auth: none\n"
            f"    baseUrl: http://127.0.0.1:{self.provider.server_port}/v1\n    models:\n"
            "      - id: scripted\n        name: scripted\n        contextWindow: 131072\n"
            "        maxTokens: 8192\n        reasoning: true\n")
        (self.private / "overlay.yml").write_text("startup:\n  setupWizard: false\n")
        proxy = "http://127.0.0.1:9"
        env = {"PATH": os.environ["PATH"], "HOME": str(self.private / "home"), "TERM": "dumb", "LANG": "C.UTF-8",
               "PI_CODING_AGENT_DIR": str(self.private / "agent"), "PI_CONFIG_DIR": str(self.private / "config"),
               "Q01_PRODUCT_PRIVATE": str(self.private), "Q01_PRODUCT_PYTHON": sys.executable,
               "Q01_PRODUCT_SRC": str(ROOT / "src"), "Q01_PRODUCT_SKILL": str(ROOT / "omp_bridge/skills/important-log-summary/SKILL.md"),
               "Q01_PRODUCT_MIRROR": str(mirror),
               "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1", "PYTHONDONTWRITEBYTECODE": "1"}
        for kind in ("CONFIG", "DATA", "STATE", "CACHE"):
            env["XDG_" + kind + "_HOME"] = str(self.private / ("xdg-" + kind.lower()))
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[name] = proxy
        env.update(getattr(self, "extra_env", {}))
        argv = [shutil.which("omp"), "--thinking", "off", "--mode", "rpc", "--no-session", "--no-title",
                "--no-extensions", "--no-skills", "--no-rules", "--no-pty", "--config",
                str(self.private / "overlay.yml"), "--extension", str(getattr(self, "extension", HERE / "extension.ts")),
                "--model", "currentproduct/scripted", "--cwd", str(self.private / "project")]
        self.streams = [(self.output / name).open("wb") for name in ("omp-stdout.log", "omp-stderr.log")]
        self.env = env
        if getattr(self,"tui",False):
            from .priority import launch_tui
            argv.remove("--mode");argv.remove("rpc")
            argv.extend(["--extension",str(ROOT/"omp_bridge/g3/entry.ts")])
            self.process,self.master,self.pump,self.pump_stop=launch_tui(argv,self.private,env,self.output/"tui.raw")
        else:
            self.process = subprocess.Popen(argv, cwd=self.private / "project", env=env, stdin=subprocess.PIPE,
                                            stdout=self.streams[0], stderr=self.streams[1], start_new_session=True)
            self.process.stdin.write(b'{"id":"start","type":"get_state"}\n')
            self.process.stdin.flush()
        deadline = time.monotonic() + 25
        while not (self.private / "metadata.json").exists() or not any(
                row.get("event") == "ready" for row in self.events()):
            if self.process.poll() is not None or time.monotonic() >= deadline or any(
                    row.get("event") == "fatal" for row in self.events()):
                raise RuntimeError("actual_parent_readback_unavailable")
            time.sleep(0.025)
        if self.posts:
            raise RuntimeError("prepare_provider_post_observed")
        metadata = json.loads((self.private / "metadata.json").read_text())
        ready = next(row for row in self.events() if row.get("event") == "ready")
        self.route = {"route": "existing-parent/private-sdk-child", "role": "Manager/task",
                      "agent_definition_sha256": ready["definitionDigest"], "skill_sha256": ready["skillDigest"],
                      "resolved_model": metadata["model"]["provider"] + "/" + metadata["model"]["id"],
                      "provider": metadata["model"]["provider"], "context_limit_tokens": metadata["model"]["contextWindow"],
                      "harness": "current-product-" + sha256(b"".join(
                          file.read_bytes() for file in (HERE / "extension.ts", HERE / "runtime.py"))).hexdigest(),
                      "selection_epoch": metadata["epoch"], "source": "product_adapter_readback"}
        self.argv = argv
        return self.route

    def events(self):
        file = self.private / "trace.jsonl"
        rows = []
        if file.exists():
            for line in file.read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass  # Concurrent last line only, never used as a fact.
        return rows

    def collect_observations(self):
        metadata = json.loads((self.private / "metadata.json").read_text())
        definition = (self.private / "definition.txt").read_text()
        catalog = ImportantLogCatalog(self.private / "data/important-logs", create=True)
        with TaskRepository(self.private / "data/tasks.sqlite3") as journal:
            os.chmod(self.private / "data/tasks.sqlite3", 0o600)
            for case in CASES:
                text = "작업 명세와 변경 검증 결과 " + case
                request, span = _request(journal, text, sha256(skill_bytes()).hexdigest())
                request["skillName"] = "important-log-summary"
                request["dbHealth"] = catalog.health_snapshot()["active"]
                value = prepare_summary(request, [SourcePiece(span, text, "start_spec")],
                                        metadata=metadata, instructions=definition)
                self.prepared[case] = value
                self.requests[case] = deepcopy(value["request"])
                journal.reserve_summary_request(value["request"])
                journal.record_summary_state(request["requestId"], "sending")
        (self.private / "prepared.json").write_text(json.dumps(self.prepared, ensure_ascii=False))
        deadline = time.monotonic() + 110
        while time.monotonic() < deadline and self.process.poll() is None:
            if self.stop_reason or any(row.get("event") in {"done", "fatal"} for row in self.events()):
                break
            time.sleep(0.025)
        if not self.stop_reason and not any(row.get("event") == "done" for row in self.events()):
            self.stop_reason = "bounded_runtime_incomplete"
        self.stop_process()
        events = self.events()
        results = {file.stem.removeprefix("result-"): json.loads(file.read_text())
                   for file in self.private.glob("result-*.json")}
        with TaskRepository(self.private / "data/tasks.sqlite3") as journal:
            records = {name: journal.get_summary_request(request["requestId"])
                       for name, request in self.requests.items()}
            controls = {}
            actual = results.get("positive", {}).get("result")
            if actual:
                span = actual["sources"][0]
                negatives = {"wrong_id_rejected": {**actual, "requestId": str(uuid4())},
                             "wrong_revision_rejected": {**actual, "sources": [{**span,
                                "source": {**span["source"], "revision": 2}}]},
                             "wrong_range_rejected": {**actual, "sources": [{**span, "end": span["end"] + 1}]}}
                for name, value in negatives.items():
                    try:
                        journal.record_summary_result_receipt(value, str(uuid4()))
                    except (InvalidTransitionError, ValueError):
                        controls[name] = True
                    else:
                        controls[name] = False
                controls["duplicate_rejected"] = journal.record_summary_result_receipt(actual, str(uuid4())) is False
        entries = {name: ImportantLogReader(self.private / "data/important-logs").show(request["subject"])["entries"]
                   for name, request in self.requests.items()}
        self.report = {"argv": self.argv, "route": self.route, "posts": self.posts, "events": events,
                       "results": results, "records": records, "entries": entries, "result_controls": controls,
                       "stop_reason": self.stop_reason, "process": {"pid": self.process.pid,
                       "returncode": self.process.returncode, "remaining_owned": []},
                       "private_product_mirror_before_hashes": self.mirror_hashes,
                       "boundary": "Fresh actual installedSDK/product dispatch/fixed writer; private scripted capability only"}
        (self.output / "runtime-report.json").write_text(json.dumps(self.report, ensure_ascii=False, indent=2))
        if not self.stop_reason:
            from .composition import origin, recovery
            from .priority import run as priority
            self.report["composition"] = {}
            for name, runner in (("origin", origin), ("recovery", recovery), ("priority", priority)):
                try:
                    self.report["composition"][name] = runner(self.output / "composition" / name)
                except Exception as error:
                    self.report["composition"][name] = {"error": type(error).__name__ + ": " + str(error),
                                                         "checks": {}, "boundary": "measurement incomplete; no inferred pass"}
        (self.output / "runtime-report.json").write_text(json.dumps(self.report, ensure_ascii=False, indent=2))
        return self.observations()

    def observations(self):
        from tools.q01_recheck import REQUIRED_FACTS
        report = self.report
        events, posts, results = report["events"], report["posts"], report["results"]
        child_posts = [row for row in posts if row["case"] != "ordinary_parent"]
        def ev(name, case=None):
            return [row for row in events if row.get("event") == name and (case is None or row.get("case") == case)]
        normal = all(results.get(name, {}).get("status") == "summary_result" for name in CASES[:3])
        done = ev("case_done")
        creates = ev("actual_created")
        facts = {name: {key: None for key in required} for name, required in REQUIRED_FACTS.items()}
        facts["Q01-route"].update(existing_agent=normal and ev("ready")[0]["definition"]["filePath"] == "embedded:task.md",
                                   isolated_turn=bool(creates) and all(row["childSessionId"] != ev("ready")[0]["metadata"]["parentSessionId"] for row in creates),
                                   typed_return=normal, no_new_agent=normal, no_standing_process=self.process.poll() is not None)
        facts["Q01-skill"].update(exact_bytes_in_context=bool(child_posts) and all(row["system_exact"] for row in child_posts),
                                   digest_matched=bool(ev("startup_selfcheck")) and all(row["skillDigest"] == self.route["skill_sha256"] for row in ev("startup_selfcheck")),
                                   role_filter_enforced=results.get("worker_role", {}).get("sent") is False and not any(row["case"] == "worker_role" for row in posts),
                                   isolated_skill_source=bool(creates) and all(row["privateRegistry"] and not row["globalRegistry"] for row in creates))
        facts["Q01-result"].update(typed_channel=normal, **report["result_controls"])
        receipts = ev("parent_receipt")
        facts["Q01-context"].update(three_turns_observed=normal and all(len(ev("actual_prompt", name)) == 1 for name in CASES[:3]),
            parent_receipt_within_4k=len(receipts) >= 3 and all(row["receiptBytes"] <= 4096 for row in receipts),
            result_within_8k=normal and all(len(canonical_json(results[name]["result"])) <= 8192 for name in CASES[:3]),
            body_within_32k=bool(child_posts) and all(row["body_bytes"] <= 32768 for row in child_posts),
            collector_within_1m=bool(self.prepared) and all(value["budget"]["collectedBytes"] <= 1_048_576 for value in self.prepared.values()),
            token_preflight=bool(ev("dispatch_preflight")) and all(row["inputEstimate"] <= row["inputLimit"] for row in ev("dispatch_preflight")),
            output_ceiling_enforced=bool(child_posts) and all(row["cap"] == 8192 for row in child_posts),
            full_output_reserved=all(value["budget"]["outputReserve"] == 8192 for value in self.prepared.values()),
            no_main_raw_accumulation=bool(done) and all(row["parentEntriesBefore"] == row["parentEntriesAfter"] for row in done))
        repeat = results.get("startup_repeat", {})
        rebuild = ev("startup_selfcheck", "startup_rebuild")
        facts["Q01-tools"].update(startup_private_config_verified=bool(creates) and all(row["privateRegistry"] and not row["globalRegistry"] and row["settingsSame"] for row in creates),
            startup_sole_db_tool_readback=normal and all(row["active"] == ["db_summary_save"] for row in ev("startup_selfcheck", "positive")),
            startup_api_mismatch_prompt_sent_zero=results.get("missing_sdk", {}).get("sent") is False and not ev("actual_prompt", "missing_sdk"),
            startup_failed_session_disposed=bool(ev("actual_disposed", "startup_repeat")) and len(ev("actual_disposed", "startup_repeat")) == len(ev("actual_created", "startup_repeat")),
            startup_rebuild_once_reverified=len(rebuild) == 2 and rebuild[0]["admitted"] is False and rebuild[1]["admitted"] is True,
            startup_repeated_failure_prompt_sent_zero=repeat.get("sent") is False and not ev("actual_prompt", "startup_repeat"),
            startup_repeated_failure_summary_deferred=repeat.get("status") == "deferred",
            runtime_request_mismatch_sent_zero=not any(row["case"] == "request_mismatch" for row in posts) if ev("owner_request_changed") else None,
            malicious_tools_blocked=bool(ev("guard_violation_observed", "saved_then_bash")) and not report["entries"]["saved_then_bash"],
            sentinel_unchanged=(self.private / "project/sentinel").read_text() == "ORIGINAL",
            post_receipt_violation_rejected=results.get("saved_then_bash", {}).get("status") == "rejected" and report["records"]["saved_then_bash"]["receipt"] is None,
            forbidden_event_immediate_protection=results.get("saved_then_bash", {}).get("protected") is True,
            no_extra_model_turn=normal and all(len(ev("actual_prompt", name)) == 1 for name in CASES[:3]))
        def blocked(case, reason=None):
            guarded = any(row.get("reason") == reason for row in ev("stream_guard", case)) if reason else True
            return results.get(case, {}).get("sent") is False and not any(row["case"] == case for row in child_posts) and guarded
        main_post = [row for row in posts if row["case"] == "ordinary_parent"]
        ready = ev("ready")[0]
        facts["Q01-tools"].update(
            receipt_request_bound=normal and blocked("request_mismatch", "request_mismatch"),
            session_turn_epoch_bound=normal and blocked("runtime_stale", "stale_binding"),
            receipt_route_bound=normal and blocked("worker_role"),
            policy_nonce_bound=blocked("runtime_policy", "policy_mismatch") and results.get("duplicate", {}).get("protected") is True,
            startup_guard_effective_verified=normal and bool(ev("startup_selfcheck", "positive")) and all(
                row["admitted"] for row in ev("startup_selfcheck", "positive")),
            startup_missing_guard_prompt_sent_zero=blocked("startup_missing") and not ev("actual_prompt", "startup_missing"),
            startup_noop_guard_prompt_sent_zero=blocked("startup_noop") and not ev("actual_prompt", "startup_noop"),
            startup_api_mismatch_prompt_sent_zero=blocked("startup_api") and not ev("actual_prompt", "startup_api"),
            startup_stale_receipt_prompt_sent_zero=blocked("startup_stale") and not ev("actual_prompt", "startup_stale"),
            startup_repeated_failure_main_continues=bool(ev("parent_continuation_done")) and bool(main_post),
            runtime_binding_pre_dispatch=all(blocked(case, reason) for case, reason in (
                ("request_mismatch", "request_mismatch"), ("runtime_roster", "roster_mismatch"),
                ("runtime_model", "model_mismatch"), ("runtime_policy", "policy_mismatch"), ("runtime_stale", "stale_binding"))),
            runtime_roster_mismatch_sent_zero=blocked("runtime_roster", "roster_mismatch"),
            runtime_model_mismatch_sent_zero=blocked("runtime_model", "model_mismatch"),
            runtime_policy_mismatch_sent_zero=blocked("runtime_policy", "policy_mismatch"),
            upper_turn_unaffected=bool(main_post) and all(row["tools"] == ["read"] for row in main_post),
            normal_permissions_restored=ready["parentReadbackTools"] == ["read"] and bool(done) and all(
                row["parentTools"] == ready["parentTools"] for row in done) and bool(ev("parent_continuation_done")))
        for name in ("origin", "recovery", "priority"):
            observed = report.get("composition", {}).get(name, {}).get("checks", {})
            facts["Q01-" + name].update(observed)
        rows = []
        for name, values in facts.items():
            missing = [key for key, value in values.items() if value is None and not (name == "Q01-priority" and key in ("max_block_recorded", "measured_block_ms") and values.get("shared_session_or_lock") is False)]
            rows.append({"id": name, "adapter": "product", "source": "product_adapter", "evidence_level": "runtime",
                         "evidence_ref": str(self.output / "runtime-report.json"), "facts": values,
                         "unknowns": ["not_observed_current_product:" + key for key in missing]})
        return {"observations": rows, "unknowns": []}

    def stop_process(self):
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(5)

    def close(self):
        self.stop_process()
        if self.process is not None and self.process.stdin is not None:
            self.process.stdin.close()
        if self.provider is not None:
            self.provider.shutdown()
            self.provider.server_close()
            self.provider = None
        if self.thread is not None:
            self.thread.join(2)
        if hasattr(self,"pump"):
            self.pump_stop.set();self.pump.join(2);os.close(self.master);del self.pump
        for stream in self.streams:
            stream.close()
        self.closed = True


def execute(output: Path):
    from tools.q01_recheck import run
    output = output.resolve()
    if not output.is_relative_to(ROOT / ".workflow/dvl-local") or output.exists():
        raise ValueError("runtime output must be a new private .workflow/dvl-local path")
    output.mkdir(parents=True, mode=0o700)
    private = output / "private"
    for name in ("home", "project", "data", "socket-dir"):
        (private / name).mkdir(parents=True, mode=0o700, exist_ok=True)
    runtime = ProductRuntime(output)
    context = {"isolated_root": str(private), "home": str(private / "home"), "project": str(private / "project"),
               "data": str(private / "data"), "socket_dir": str(private / "socket-dir"), "product_runtime": runtime}
    omp = Path(shutil.which("omp")).resolve()
    result = run(root=ROOT, requirements=json.loads((ROOT / "docs/features/dev-validation-log-retention/requirements-r0.9-p5.json").read_text()),
                 adapter="product", context=context, omp_executable=omp, omp_bundle_identity=sha256(omp.read_bytes()).hexdigest(),
                 output=output / "q01-check.json", log=output / "q01-observations.json", projection_output=output / "projection.json",
                 snapshot_output=output / "snapshot.json")
    if not runtime.closed:
        raise RuntimeError("product runtime was not closed by frozen runner")
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    result = execute(Path(sys.argv[1]))
    print(json.dumps({"passed": result["passed"], "check": result["check"], "verdicts": result["verdicts"]}, ensure_ascii=False))
    sys.exit(0 if result["passed"] else 2)
