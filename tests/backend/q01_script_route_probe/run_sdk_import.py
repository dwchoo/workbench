"""One private real OMP launch to inspect the public SDK import, with no prompts."""

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
OUT = Path(sys.argv[1]).resolve()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Provider(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.requests.append({"path": self.path, "length": length, "sha256": hashlib.sha256(body).hexdigest()})
        self.send_response(503)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        pass


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    private = OUT / "private"
    private.mkdir(mode=0o700)
    home, agent, config, cwd = (private / x for x in ("home", "agent", "config", "cwd"))
    for path in (home, agent, config, cwd):
        path.mkdir(mode=0o700)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    (agent / "models.yml").write_text(
        "providers:\n  q01import:\n"
        f"    baseUrl: http://127.0.0.1:{server.server_port}/v1\n"
        "    api: openai-completions\n    auth: none\n    models:\n"
        "      - id: scripted\n        name: scripted\n        contextWindow: 32768\n        maxTokens: 1024\n")
    (private / "overlay.yml").write_text("startup:\n  setupWizard: false\n")
    omp = Path(shutil.which("omp") or "")
    proxy = "http://127.0.0.1:9"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "TERM": "dumb", "LANG": "C.UTF-8",
           "PI_CODING_AGENT_DIR": str(agent), "PI_CONFIG_DIR": str(config),
           "XDG_CONFIG_HOME": str(private / "xdg-config"), "XDG_DATA_HOME": str(private / "xdg-data"),
           "XDG_STATE_HOME": str(private / "xdg-state"), "XDG_CACHE_HOME": str(private / "xdg-cache"),
           "Q01_SDK_IMPORT_TRACE": str(private / "import-trace.jsonl"), "PYTHONDONTWRITEBYTECODE": "1",
           "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    env.update({key: proxy for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")})
    argv = [str(omp), "--mode", "rpc", "--no-session", "--no-title", "--no-extensions", "--no-skills",
            "--no-rules", "--no-tools", "--no-pty", "--config", str(private / "overlay.yml"),
            "--extension", str(HERE / "sdk_import_extension.ts"), "--model", "q01import/scripted", "--cwd", str(cwd)]
    version = subprocess.run([str(omp), "--version"], env=env, text=True, capture_output=True, timeout=10)
    result: dict[str, object] = {"invocation": "q01-sdk-import01", "omp_version": version.stdout.strip(),
        "omp_binary_sha256": sha(omp), "source_sha256": {p.name: sha(p) for p in (HERE / "sdk_import_extension.ts", HERE / "run_sdk_import.py")},
        "module_specifier": "@oh-my-pi/pi-coding-agent", "argv": argv,
        "environment_keys": sorted(env), "provider_port": server.server_port, "rpc_sent": [], "rpc_frames": [], "process": {}}
    frames: list[dict] = []
    condition = threading.Condition()
    with (OUT / "omp-stderr.log").open("wb") as stderr:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=stderr, start_new_session=True)
        result["process"] = {"pid": proc.pid}

        def pump() -> None:
            pending = b""
            while True:
                ready, _, _ = select.select([proc.stdout.fileno()], [], [], 0.2)
                if not ready:
                    if proc.poll() is not None:
                        return
                    continue
                data = os.read(proc.stdout.fileno(), 65536)
                if not data:
                    return
                pending += data
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    try:
                        frame = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(frame, dict):
                        with condition:
                            frames.append(frame)
                            condition.notify_all()

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        try:
            command = {"id": "sdk-import-state", "type": "get_state"}
            proc.stdin.write((json.dumps(command) + "\n").encode())
            proc.stdin.flush()
            result["rpc_sent"].append(command)
            deadline = time.monotonic() + 25
            trace = private / "import-trace.jsonl"
            while time.monotonic() < deadline and proc.poll() is None:
                if trace.exists() and '"event":"import_result"' in trace.read_text():
                    break
                with condition:
                    condition.wait(0.2)
            result["timed_out"] = not trace.exists() or '"event":"import_result"' not in trace.read_text()
            result["module_trace"] = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
            result["rpc_frames"] = [{"type": f.get("type"), "id": f.get("id"), "error": f.get("error")}
                                    for f in frames[:200]]
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
            thread.join(2)
            result["process"] = {"pid": proc.pid, "returncode": proc.returncode,
                                 "stdout_pump_stopped": not thread.is_alive(),
                                 "remaining_owned": [] if proc.poll() is not None else [proc.pid]}
            server.shutdown()
            server.server_close()
            server_thread.join(2)
    result["provider_posts"] = Provider.requests
    result["model_prompts_sent"] = 0
    (OUT / "runtime-evidence.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    imported = next((x for x in result["module_trace"] if x.get("event") == "import_result"), None)
    return 0 if imported and imported.get("ok") and imported.get("createAgentSessionType") == "function" \
        and not result["timed_out"] and not Provider.requests and not result["process"]["remaining_owned"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
