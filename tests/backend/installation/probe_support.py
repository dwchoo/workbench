"""Small live observation over existing Q01 scripted product fixtures.

This deliberately measures one disposable child. It is not Q01 certification,
provider compatibility certification or authentication readiness.
"""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import threading

from tests.backend.q01_product_adapter_probe.composition import Phase, reserve, wait
from tests.backend.q01_sdk_full_probe.full_probe import _request
from workbench.backend.summary_adapter import skill_bytes
from workbench.storage.important_log import ImportantLogReader
from workbench.tasks.repository import TaskRepository

HERE = Path(__file__).resolve().parent


def observe_distribution(executable: Path, output: Path) -> dict:
    """Load the real entry and observe one public SDK child + Python writer."""
    if output.exists():
        raise ValueError("refuse_to_overwrite_probe_evidence")
    output.mkdir(parents=True, mode=0o700)
    r = Phase(output)
    r.extension = HERE / "probes/wi01/entry_child.ts"
    project = r.private / "project"
    project.mkdir(parents=True, mode=0o700)
    temporary = r.private / "tmp"
    temporary.mkdir(mode=0o700)
    r.extra_env["TMPDIR"] = str(temporary)
    shim = output / "bin"
    shim.mkdir(mode=0o700)
    (shim / "omp").symlink_to(executable.resolve(strict=True))
    previous_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(shim) + os.pathsep + previous_path
    # Relative Unix socket addresses avoid the worktree's long absolute prefix.
    sock_path = output / "g3.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(os.path.relpath(sock_path, Path.cwd()))
    listener.listen(1)
    listener.settimeout(0.2)
    stop = threading.Event()
    frames = []

    def receive():
        conn = None
        try:
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                    break
                except TimeoutError:
                    pass
            if conn is None:
                return
            conn.settimeout(0.2)
            pending = b""
            while not stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    frames.append(json.loads(line))
        finally:
            if conn is not None:
                conn.close()

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()
    r.extra_env.update({
        "WORKBENCH_G3_BRIDGE_SOCKET": os.path.relpath(sock_path, project),
        "WORKBENCH_G3_ROLE": "manager", "WORKBENCH_G3_TOKEN": "wi01-local-fixture",
        "WORKBENCH_G3_GENERATION": "1",
    })
    report = {"checks": {}, "executable": str(executable)}
    try:
        r.start()
        text = "설치된 SDK child의 결과와 Python writer 저장 확인"
        with TaskRepository(r.private / "data/tasks.sqlite3") as repo:
            os.chmod(r.private / "data/tasks.sqlite3", 0o600)
            request, _ = _request(repo, text, sha256(skill_bytes()).hexdigest())
            prepared = r.prepare("positive", request, text)
            reserve(repo, prepared)
        r.launch({"positive": True})
        result = r.result("positive")
        r.advance("positive")
        snapshot = r.finish()
        entries = ImportantLogReader(r.private / "data/important-logs").show(request["subject"])["entries"]
        entry_file = r.private / "entry.jsonl"
        entry = [json.loads(line) for line in entry_file.read_text().splitlines()] if entry_file.exists() else []
        wait(r, lambda: bool(frames))
        report.update({"snapshot": snapshot, "entry": entry, "bridge_frames": frames,
                       "result": result, "entries": entries})
        report["checks"] = {
            "real_entry_registered": any(x["event"] == "real_entry_registered" for x in entry),
            "real_bridge_hello": any(x.get("kind") == "hello" and x.get("role") == "manager" for x in frames),
            "bundled_task_definition": any(x["filePath"] == "embedded:task.md" and x["systemPromptBytes"] > 0 for x in entry),
            "one_private_child": len([e for e in r.events() if e.get("event") == "actual_created"]) == 1,
            "summary_return": result.get("status") == "summary_result",
            "python_writer_persisted": len(entries) == 1 and entries[0]["entry"]["body"]["facts"] == ["실행한 작업과 확인한 검증 결과"],
            "loopback_scripted_requests": bool(r.posts) and all(p["case"] == "positive" and p["tools"] == ["db_summary_save"] for p in r.posts),
            "parent_process_group_stopped": snapshot["pgid_absent"],
        }
    except Exception as error:
        report["error"] = type(error).__name__ + ": " + str(error)
        report["events"] = r.events()
    finally:
        r.close()
        os.environ["PATH"] = previous_path
        stop.set()
        listener.close()
        thread.join(2)
        sock_path.unlink(missing_ok=True)
        report["process"] = {"pid": r.process.pid if r.process else None,
                             "returncode": r.process.returncode if r.process else None,
                             "socket_thread_stopped": not thread.is_alive(),
                             "provider_thread_stopped": r.thread is None or not r.thread.is_alive()}
        (output / "distribution-observation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report
