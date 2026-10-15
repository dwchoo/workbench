"""Credential-free installed OMP readback probe for the DVL-14 MODEL supplier.

This observes the real Manager getter and configured Worker.default registry
selection. A successful exit proves getter capability and cleanup only; the
metadata remains inconclusive when the host cannot expose budget semantics.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
from uuid import uuid4

from workbench.backend import summary_compat_runtime as rt
from workbench.backend.launcher import (
    LaunchPlan,
    choose_shell,
    load_model_table,
    omp_command,
    omp_environment,
    role_overlay,
    selector_level,
    selector_model,
    write_role_overlay,
)
from workbench.backend.summary_adapter import SDKSummaryAdapter, SummaryDeferred, _resolved_metadata
from workbench.ipc.bridge_g3.mailbox import BridgeDisconnected, G3BridgeServer


_ROOT = Path(__file__).resolve().parents[2]
_PEER_WAIT_SECONDS = 50.0
_MAX_LOG_BYTES = 8192
_MAX_RESULT_BYTES = 32768
_MODEL_FIELDS = ("id", "provider", "api", "baseUrl", "contextWindow", "maxTokens", "tokenizer")
_METADATA_FIELDS = (
    "source", "parentSessionId", "epoch", "model", "childSelection", "thinkingLevel",
    "serviceTierByFamily", "role", "inheritanceSource", "existingContextTokens", "outputCap",
    "reasoningTokens", "visibleResultTokens", "outputMeaning", "outputLimitEnforced",
    "outputLimitSource", "estimator", "status", "unknowns",
)
_REQUIRED_UNKNOWNS = {
    "current_context_unavailable",
    "output_ceiling_enforcement_unavailable",
    "reasoning_budget_semantics_unavailable",
}
_LIMITATIONS = [
    "This probe observes the credential-free installed OMP Manager getter with one explicit Manager.default --model discriminator.",
    "The parent session launched here is not the user's operating session and does not prove full MODEL gate completion.",
    "No prompt, summary request, SDK child start, auth copy/read, provider provisioning, or paid model request is performed by the harness.",
    "Normal OMP startup and model-list lookup I/O are not attributed or proven to be zero by this probe.",
    "Native stdout/stderr are discarded; an early exit is reported by phase and process exit code only.",
    "Provider output enforcement, reasoning budget semantics, quality, truncation, and cost remain unverified.",
    "C2 remains on hold, raw rotation remains inactive, and Q01/full-gate status is inconclusive.",
]


class _ProbeFailure(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _private_output(path: Path) -> None:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise _ProbeFailure("output_must_be_fresh_absolute_directory")
    if not path.parent.is_dir():
        raise _ProbeFailure("output_parent_missing")
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise _ProbeFailure("unsafe_output_directory")


def _private_root(root: Path) -> None:
    for name in (
        "home", "project", "tmp", "agent", "config", "xdg-config", "xdg-data",
        "xdg-state", "xdg-cache",
    ):
        (root / name).mkdir(mode=0o700)
    rt._private_root(root)
    if any(stat.S_IMODE((root / name).stat().st_mode) != 0o700 for name in (
        "home", "project", "tmp", "agent", "config", "xdg-config", "xdg-data",
        "xdg-state", "xdg-cache",
    )):
        raise _ProbeFailure("unsafe_private_environment")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _table_digest(table: dict[str, Any]) -> str:
    encoded = json.dumps(table, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _digest(encoded)


def _safe_model(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {key: value.get(key) for key in _MODEL_FIELDS}


def _safe_metadata(value: dict[str, Any]) -> dict[str, Any]:
    result = {key: value.get(key) for key in _METADATA_FIELDS}
    result["model"] = _safe_model(value.get("model"))
    selection = value.get("childSelection")
    if isinstance(selection, dict):
        result["childSelection"] = {
            key: selection.get(key) for key in (
                "source", "selector", "modelTableDigest", "model", "descriptorSource",
                "thinkingLevel", "serviceTierSource",
            )
        }
        result["childSelection"]["model"] = _safe_model(selection.get("model"))
    return result


def _event(events: list[dict[str, Any]], started: float, name: str, **fields: Any) -> None:
    if len(events) >= 24:
        return
    events.append({"event": name, "elapsed_ms": round((time.monotonic() - started) * 1000), **fields})


def _wait_for_manager(bridge: G3BridgeServer, process: subprocess.Popen[bytes], timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        if process.poll() is not None:
            raise _ProbeFailure("native_exited_before_manager_peer")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _ProbeFailure("manager_peer_timeout")
        try:
            return bridge.peer("manager", timeout=min(0.25, remaining))
        except BridgeDisconnected:
            continue


def _write_bounded(path: Path, content: bytes, limit: int) -> None:
    if len(content) > limit:
        raise _ProbeFailure("bounded_artifact_limit_exceeded")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def run_model_readback(output: str | Path) -> tuple[int, dict[str, Any]]:
    """Run one actual getter probe into a new private absolute output directory."""
    output = Path(output)
    _private_output(output)
    started = time.monotonic()
    events: list[dict[str, Any]] = []
    root: Path | None = None
    bridge: G3BridgeServer | None = None
    process: subprocess.Popen[bytes] | None = None
    process_ref: dict[str, Any] | None = None
    socket_path: Path | None = None
    capability_observed = False
    first: dict[str, Any] | None = None
    safe_metadata: dict[str, Any] | None = None
    failure_reason: str | None = None
    error_type: str | None = None
    binary_sha256: str | None = None
    model_table_file_sha256: str | None = None
    native_version: str | None = None
    peer_record: dict[str, Any] | None = None
    instructions_sha256: str | None = None
    instructions_bytes: int | None = None
    cleanup: dict[str, Any] = {
        "status": "incomplete", "leader_cleanup_error": None, "remaining_owned": [],
        "bridge_closed": False, "private_root_removed": False,
    }
    result: dict[str, Any] = {
        "schema_version": 1,
        "kind": "actual_configured_model_parent_readback",
        "status": "inconclusive",
        "capability_status": "inconclusive",
        "metadata_status": "inconclusive",
        "overall_gate_status": "inconclusive",
        "raw_activation": False,
        "prompt_submissions": 0,
        "summary_requests": 0,
        "sdk_child_starts_requested": 0,
        "paid_model_calls_requested": 0,
        "native_output_captured": False,
        "native_output_bytes": None,
        "native_output_sha256": None,
        "failure_reason": None,
        "error_type": None,
        "limitations": _LIMITATIONS,
        "cleanup": cleanup,
    }
    _event(events, started, "harness_started", source="actual_installed_omp", mode="manager_parent_readback")

    try:
        root = Path(tempfile.mkdtemp(prefix="dvl-model-readback-")).resolve()
        os.chmod(root, 0o700)
        _private_root(root)
        runtime = rt._copy_runtime(_ROOT, root)
        env = rt._environment(root)

        omp_path = shutil.which("omp")
        if not omp_path:
            raise _ProbeFailure("omp_missing")
        binary = Path(omp_path).resolve(strict=True)
        binary_sha256 = _digest(binary.read_bytes())
        try:
            version_result = subprocess.run(
                [str(binary), "--version"], cwd=root / "project", env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=15, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise _ProbeFailure("omp_version_timeout") from exc
        if version_result.returncode != 0 or not version_result.stdout:
            raise _ProbeFailure("omp_version_failed")
        version_text = version_result.stdout[:4096].decode("utf-8", "replace").strip()
        native_version = version_text.splitlines()[0][:128] if version_text else None
        if not native_version:
            raise _ProbeFailure("omp_version_unavailable")

        table_path = runtime / "omp_bridge/omp-models.yml"
        table_bytes = table_path.read_bytes()
        model_table_file_sha256 = _digest(table_bytes)
        table = load_model_table(table_path)
        manager_selector = table["manager"]["models"]["default"]
        child_selector = table["worker"]["models"]["default"]
        if not isinstance(manager_selector, str) or not isinstance(child_selector, str):
            raise _ProbeFailure("configured_selector_unavailable")
        manager_model = selector_model(manager_selector)
        manager_level = selector_level(manager_selector)
        expected_selection = {
            "source": "configured_worker_default",
            "selector": child_selector,
            "modelTableDigest": _table_digest(table),
        }

        socket_path = root / "b.sock"
        if len(os.fsencode(str(socket_path))) > 107:
            raise _ProbeFailure("bridge_socket_path_too_long")
        tokens = {"manager": uuid4().hex, "worker": uuid4().hex}
        bridge = G3BridgeServer(socket_path, tokens)
        bridge.start()
        plan = LaunchPlan(
            choose_shell(env), str(binary), native_version,
            str(runtime / "omp_bridge/g3/entry.ts"),
            ("--mode", "rpc", "--no-session", "--model", manager_model),
        )
        child_env = omp_environment(
            env, plan, role="manager", token=tokens["manager"],
            bridge_socket=socket_path, models=table,
        )
        actual_selection = json.loads(child_env["WORKBENCH_G3_SUMMARY_SELECTION"])
        if actual_selection != expected_selection:
            raise _ProbeFailure("launcher_child_selection_mismatch")
        overlay = write_role_overlay(
            root / "config", "manager",
            role_overlay(
                "manager", project_dir=root / "project", home=root / "home",
                skills_dir=runtime / "omp_bridge/skills", environment=child_env, models=table,
            ),
        )
        argv = omp_command(plan, overlay, role="manager")
        process = subprocess.Popen(
            argv, cwd=root / "project", env=child_env, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        process_ref = {
            "pid": process.pid, "pgid": process.pid,
            "start_ticks": rt._identity(process.pid),
        }
        if process_ref["start_ticks"] is None:
            raise _ProbeFailure("owned_process_identity_unavailable")
        _event(events, started, "native_started", pid=process.pid, binary_sha256=binary_sha256)

        peer = _wait_for_manager(bridge, process, _PEER_WAIT_SECONDS)
        if peer.pid != process.pid:
            raise _ProbeFailure("manager_peer_process_mismatch")
        expected_peer = (peer.session_id, peer.generation)
        peer_record = asdict(peer)
        adapter = SDKSummaryAdapter(bridge, timeout=15)
        first = adapter.read_runtime(expected_peer=expected_peer)
        second = adapter.read_runtime(expected_peer=expected_peer)
        if first != second or first.get("expected_peer") != expected_peer:
            raise _ProbeFailure("consecutive_readbacks_differ")
        if bridge.peer("manager") != peer:
            raise _ProbeFailure("manager_peer_changed")
        try:
            bridge.peer("worker", timeout=0)
        except BridgeDisconnected:
            pass
        else:
            raise _ProbeFailure("unexpected_worker_peer")

        metadata = first["metadata"]
        parsed = _resolved_metadata(metadata)
        selection = parsed["childSelection"]
        parent_model = parsed["model"]
        if (parent_model["provider"] + "/" + parent_model["id"] != manager_model
                or parsed["thinkingLevel"] != manager_level):
            raise _ProbeFailure("parent_model_does_not_match_manager_default")
        if (selection.get("selector") != child_selector
                or selection.get("source") != "configured_worker_default"
                or selection.get("modelTableDigest") != expected_selection["modelTableDigest"]
                or selection.get("descriptorSource") != "manager_model_registry_getAll_all"
                or selection.get("serviceTierSource") != "parent_session_readback"
                or selection.get("thinkingLevel") != selector_level(child_selector)):
            raise _ProbeFailure("child_selection_does_not_match_shipped_table")
        child_model = selection.get("model")
        if (not isinstance(child_model, dict)
                or child_model.get("provider") + "/" + child_model.get("id") != selector_model(child_selector)):
            raise _ProbeFailure("child_descriptor_does_not_match_shipped_table")
        if parsed.get("status") != "inconclusive" or set(parsed.get("unknowns", ())) != _REQUIRED_UNKNOWNS:
            raise _ProbeFailure("metadata_unknowns_not_preserved")
        if parsed.get("epoch") != second["metadata"].get("epoch"):
            raise _ProbeFailure("readback_epoch_changed")

        instructions = first.get("instructions")
        if not isinstance(instructions, str) or not instructions:
            raise _ProbeFailure("bundled_instructions_unavailable")
        encoded_instructions = instructions.encode("utf-8")
        if len(encoded_instructions) > 16384:
            raise _ProbeFailure("bundled_instructions_over_limit")
        instructions_sha256 = _digest(encoded_instructions)
        instructions_bytes = len(encoded_instructions)
        safe_metadata = _safe_metadata(parsed)
        capability_observed = True
        result.update({
            "capability_status": "observed",
            "metadata_status": parsed["status"],
            "parent_peer": peer_record,
            "consecutive_readbacks_equal": True,
            "same_peer_bound_twice": True,
            "resolved_preflight_parser_accepted": True,
            "configured_manager_selector": manager_selector,
            "explicit_omp_model": manager_model,
            "configured_child_selector": child_selector,
            "configured_child_model_table_digest": expected_selection["modelTableDigest"],
            "model_table_file_sha256": model_table_file_sha256,
            "binary_sha256": binary_sha256,
            "omp_version": native_version,
            "instructions_sha256": instructions_sha256,
            "instructions_bytes": instructions_bytes,
            "metadata": safe_metadata,
        })
        _event(events, started, "getter_observed", peer_generation=peer.generation,
               stable_epoch=True, metadata_status=parsed["status"])
    except _ProbeFailure as exc:
        failure_reason = exc.reason
        _event(events, started, "probe_inconclusive", reason=exc.reason)
    except SummaryDeferred:
        failure_reason = "model_getter_unavailable"
        _event(events, started, "probe_inconclusive", reason=failure_reason)
    except Exception as exc:
        failure_reason = "unexpected_probe_error"
        error_type = type(exc).__name__
        _event(events, started, "probe_inconclusive", reason=failure_reason, error_type=error_type)
    finally:
        if process is not None:
            if process_ref is not None and process_ref.get("start_ticks") is None and process.poll() is None:
                process_ref["start_ticks"] = rt._identity(process.pid)
            try:
                cleanup["leader_cleanup_error"] = rt._stop_leader(process, process_ref)
            except Exception as exc:
                cleanup["leader_cleanup_error"] = "cleanup_helper_error"
                error_type = error_type or type(exc).__name__
            try:
                cleanup["leader_exit_code"] = process.poll()
            except Exception:
                cleanup["leader_exit_code"] = None
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        if bridge is not None:
            try:
                bridge.close()
                thread = getattr(bridge, "_thread", None)
                cleanup["bridge_closed"] = not socket_path.exists() and (thread is None or not thread.is_alive())
            except Exception:
                cleanup["bridge_closed"] = False
        else:
            cleanup["bridge_closed"] = True
        if process is not None and process_ref is not None:
            try:
                cleanup["remaining_owned"] = rt._remaining(process_ref["pgid"])
            except Exception:
                cleanup["remaining_owned"] = ["ownership_scan_unavailable"]
        else:
            cleanup["remaining_owned"] = []
        root_removed = False
        leader_complete = (
            process is None
            or (cleanup.get("leader_cleanup_error") is None and cleanup.get("leader_exit_code") is not None
                and cleanup.get("remaining_owned") == [])
        )
        cleanup_complete = (
            leader_complete
            and cleanup.get("bridge_closed") is True
        )
        if root is not None and cleanup_complete:
            try:
                shutil.rmtree(root)
                root_removed = not root.exists()
            except OSError:
                root_removed = False
        cleanup["private_root_removed"] = root_removed
        cleanup["status"] = "complete" if cleanup_complete and root_removed else "incomplete"
        if root is not None and not root_removed:
            cleanup["retained_private_root"] = str(root)
        result["cleanup"] = cleanup
        if capability_observed and cleanup["status"] == "complete":
            result["status"] = "readback_observed"
            result["capability_status"] = "observed"
            result["overall_gate_status"] = "inconclusive"
        else:
            result["failure_reason"] = failure_reason or "owned_cleanup_incomplete"
            result["error_type"] = error_type
        _event(events, started, "cleanup", status=cleanup["status"],
               leader_exit_code=cleanup.get("leader_exit_code"),
               remaining_owned=cleanup.get("remaining_owned", []), private_root_removed=root_removed)

    log_bytes = b"".join(
        (json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        for item in events
    )
    if len(log_bytes) > _MAX_LOG_BYTES:
        return 2, {"status": "inconclusive", "failure_reason": "bounded_log_limit_exceeded"}
    _write_bounded(output / "run.log.jsonl", log_bytes, _MAX_LOG_BYTES)
    result["sanitized_log_sha256"] = _digest(log_bytes)
    result_bytes = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(result_bytes) > _MAX_RESULT_BYTES:
        result = {
            "schema_version": 1, "kind": "actual_configured_model_parent_readback",
            "status": "inconclusive", "capability_status": "inconclusive",
            "metadata_status": "inconclusive", "overall_gate_status": "inconclusive",
            "failure_reason": "bounded_result_limit_exceeded", "cleanup": cleanup,
            "sanitized_log_sha256": _digest(log_bytes), "limitations": _LIMITATIONS,
        }
        result_bytes = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _write_bounded(output / "result.json", result_bytes, _MAX_RESULT_BYTES)
    return (0 if result.get("status") == "readback_observed" else 2), result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Credential-free installed OMP model metadata getter probe")
    parser.add_argument("--output", required=True, help="fresh private absolute output directory")
    args = parser.parse_args(argv)
    try:
        code, result = run_model_readback(args.output)
    except _ProbeFailure as exc:
        print(json.dumps({"status": "inconclusive", "failure_reason": exc.reason}, sort_keys=True), file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps({"status": "inconclusive", "failure_reason": "output_or_environment_error",
                          "error_type": type(exc).__name__}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps({
        "status": result.get("status"),
        "capability_status": result.get("capability_status"),
        "metadata_status": result.get("metadata_status"),
        "overall_gate_status": result.get("overall_gate_status"),
        "failure_reason": result.get("failure_reason"),
        "cleanup_status": result.get("cleanup", {}).get("status"),
        "sanitized_log_sha256": result.get("sanitized_log_sha256"),
    }, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
