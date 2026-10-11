"""Transmission-free installed-OMP readback for the probe-local Q01 port."""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BODY = HERE.parent / "q01_sdk_private_probe/agent-body.txt"
SKILL = HERE.parent / "q01_sdk_private_probe/summary-skill.txt"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(out: Path, *, extension: Path | None = None) -> dict:
    extension = extension or HERE / "prepare_extension.ts"
    omp_name = shutil.which("omp")
    if omp_name is None:
        raise RuntimeError("installed OMP not found")
    omp = Path(omp_name).resolve()
    out = out.resolve()
    if out.exists():
        raise ValueError("refuse to overwrite prepare evidence")
    out.mkdir(parents=True, mode=0o700)
    private = out / "private"
    for name in ("home", "agent", "config", "cwd"):
        (private / name).mkdir(parents=True, mode=0o700)
    posts: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            posts.append({"path": self.path, "body_sha256": hashlib.sha256(body).hexdigest()})
            self.send_response(503)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def close_provider() -> None:
        server.shutdown()
        server.server_close()
        thread.join(2)

    (private / "agent/models.yml").write_text(
        "providers:\n  q01startup:\n" f"    baseUrl: http://127.0.0.1:{server.server_port}/v1\n"
        "    api: openai-completions\n    auth: none\n    models:\n"
        "      - id: scripted\n        name: scripted\n        contextWindow: 32768\n        maxTokens: 8192\n")
    (private / "overlay.yml").write_text("startup:\n  setupWizard: false\n")
    proxy = "http://127.0.0.1:9"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(private / "home"),
           "TERM": "dumb", "LANG": "C.UTF-8", "PI_CODING_AGENT_DIR": str(private / "agent"),
           "PI_CONFIG_DIR": str(private / "config"), "XDG_CONFIG_HOME": str(private / "xdg-config"),
           "XDG_DATA_HOME": str(private / "xdg-data"), "XDG_STATE_HOME": str(private / "xdg-state"),
           "XDG_CACHE_HOME": str(private / "xdg-cache"), "Q01_FULL_PREPARE_TRACE": str(private / "trace.jsonl"),
           "Q01_AGENT_BODY": str(BODY), "Q01_SUMMARY_SKILL": str(SKILL),
           "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    env.update({key: proxy for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                       "http_proxy", "https_proxy", "all_proxy")})
    argv = [str(omp), "--mode", "rpc", "--no-session", "--no-title", "--no-extensions",
            "--no-skills", "--no-rules", "--no-tools", "--no-pty", "--config", str(private / "overlay.yml"),
            "--extension", str(extension), "--model", "q01startup/scripted",
            "--cwd", str(private / "cwd")]
    try:
        version = subprocess.run([str(omp), "--version"], env=env, capture_output=True, text=True, timeout=10)
    except BaseException:
        close_provider()
        raise
    result = {"omp_version": version.stdout.strip(), "omp_binary_sha256": sha(omp), "argv": argv,
              "environment_keys": sorted(env), "source_sha": {str(p): sha(p) for p in
              (extension, HERE / "run_prepare.py", BODY, SKILL)},
              "provider_port": server.server_port, "provider_posts": posts}
    frames: list[dict] = []
    trace = private / "trace.jsonl"
    with (out / "omp-stderr.log").open("wb") as stderr:
        try:
            proc = subprocess.Popen(argv, cwd=private / "cwd", env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
        except BaseException:
            close_provider()
            raise
        result["process"] = {"pid": proc.pid}

        def pump() -> None:
            pending = b""
            while True:
                ready, _, _ = select.select([proc.stdout.fileno()], [], [], 0.2)
                if not ready:
                    if proc.poll() is not None:
                        return
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    return
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    try:
                        frames.append(json.loads(line))
                    except ValueError:
                        pass

        pump_thread = threading.Thread(target=pump, daemon=True)
        pump_started = False
        try:
            pump_thread.start()
            pump_started = True
            command = {"id": "full-prepare-state", "type": "get_state"}
            proc.stdin.write((json.dumps(command) + "\n").encode())
            proc.stdin.flush()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and proc.poll() is None:
                if trace.exists() and '"event":"disposed"' in trace.read_text():
                    break
                time.sleep(0.1)
            result["stop_reason"] = None if trace.exists() and '"event":"disposed"' in trace.read_text() else "timeout_or_exit"
            result["trace"] = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
            result["rpc_frames"] = [{"id": f.get("id"), "type": f.get("type"), "error": f.get("error")}
                                    for f in frames[:20]]
        finally:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(5)
            if pump_started:
                pump_thread.join(2)
            result["process"] = {"pid": proc.pid, "returncode": proc.returncode,
                                 "pump_stopped": not pump_thread.is_alive(),
                                 "remaining_owned": [] if proc.poll() is not None else [proc.pid]}
            close_provider()
    readbacks = [e for e in result["trace"] if e.get("event") == "readback"]
    done = [e for e in result["trace"] if e.get("event") == "done"]
    result["checks"] = {"post_zero": len(posts) == 0, "one_readback": len(readbacks) == 1,
                        "readback_private_sole_db": len(readbacks) == 1 and
                        readbacks[0]["active"] == ["db_summary_save"] and
                        readbacks[0]["settingsIdentity"] and readbacks[0]["privateHas"] and
                        not readbacks[0]["globalHas"],
                        "model_readback": len(readbacks) == 1 and
                        readbacks[0]["model"]["provider"] == "q01startup" and
                        readbacks[0]["model"]["contextWindow"] == 32768 and
                        readbacks[0]["model"]["maxTokens"] == 8192,
                        "disposed": any(e.get("event") == "disposed" for e in result["trace"]),
                        "done": len(done) == 1 and done[0]["ok"] is True,
                        "owned_stopped": not result["process"]["remaining_owned"] and
                        result["process"]["pump_stopped"]}
    (out / "prepare-evidence.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if result["stop_reason"] or not all(result["checks"].values()):
        raise RuntimeError("transmission-free OMP prepare readback failed: " + str(result["checks"]))
    return result


if __name__ == "__main__":
    run(Path(sys.argv[1]))
