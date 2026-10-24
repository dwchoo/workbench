"""TI-15 feasibility probe: can the installed OMP connect to exactly one Streamable HTTP MCP server with a Bearer
header through a Workbench overlay, while every other MCP source stays off and the isolation leak check holds?

Manual run only (not part of any unittest suite), from the repo root::

    env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python tests/hub/probes/omp_mcp_probe.py \\
        --report docs/features/task-inbox/PROBE-OMP-MCP.md

What it does (no model call, no provider request, nothing outside one temp root):

- a temp HOME with a placeholder ``~/.omp/agent/agent.db`` (empty file, never opened by the probe) and decoy MCP
  configurations in every location OMP's capability providers read (user ``~/.omp``, project ``.mcp.json``/``mcp.json``/
  ``.omp/mcp.json``, Claude/Cursor/Codex/Gemini/VS Code files), plus decoy native items (SYSTEM.md, AGENTS.md, skills,
  rules, commands) in the project dir and the user's ``~/.omp``;
- a Board-agent-shaped Workbench OMP home (``omp_home.prepare_omp_home`` under ``<hub state>/board-agent``);
- a stdlib ``http.server`` stub MCP server on 127.0.0.1 (initialize, tools/list, tools/call of one tool) that records
  every request (path, JSON-RPC method, whether the Bearer header matched; never the key itself). Decoy servers point
  at ``/decoy/<name>`` of the same stub, so any connection to a decoy is recorded;
- the real OMP started exactly like the backend's start-up isolation check (``launcher.check_isolation``: RPC mode,
  ``get_state``/``get_available_commands``/``get_login_providers`` only, ``--no-session``), every provider request
  blocked by pointing HTTP(S)/ALL_PROXY at a closed port (loopback is exempt through NO_PROXY).

Scenarios: ``control_manager``/``control_worker`` (the manager/worker isolation unchanged, with an mcp.json planted
in their agent dir),
``board_env`` (the candidate configuration, header from an environment reference), ``board_literal`` (header value
written into the file), ``board_project_cwd`` (the candidate started in a project dir: what else comes back with the
``native`` provider), ``board_continue`` (``--continue`` instead of ``--no-session``, twice).
"""
from __future__ import annotations

import argparse
import datetime
import http.server
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from workbench.backend import launcher, omp_home  # noqa: E402

SERVER_NAME = "workbench-hub"
TOOL_NAME = "probe_echo"
KEY_ENV = "WB_BOARD_MCP_KEY"
BLOCKED = "http://127.0.0.1:9"
FAKE_PROVIDER_KEY = "sk-ant-fake-ti15-never-used"  # makes OMP list a model; no provider is contacted (proxy blocked)
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
BOARD_PROVIDER_IDS = tuple(item for item in launcher.ISOLATION_PROVIDER_IDS if item != "native")
DECOY_MCP = ("user_agent_dir", "user_omp_root", "home_dotmcp", "project_dotmcp", "project_mcp", "project_omp",
             "project_omp_dot", "claude_user", "claude_project", "cursor_user", "cursor_project", "codex_user",
             "gemini_user", "vscode_project")
DECOY_SKILLS = ("decoy-user-real-skill", "decoy-home-omp-skill", "decoy-project-skill")
MARKERS = ("DECOY-USER-REAL-SYSTEM", "DECOY-USER-REAL-AGENTS", "DECOY-HOME-OMP-SYSTEM", "DECOY-HOME-OMP-AGENTS",
           "DECOY-PROJECT-SYSTEM", "DECOY-PROJECT-AGENTS", "DECOY-PROJECT-RULE")


# -- stub MCP server ---------------------------------------------------------------------------------------------------
class StubMcp:
    """Streamable HTTP MCP stub (JSON responses only). Records requests; the key is compared, never stored."""

    def __init__(self, key: str):
        self.key = key
        self.records: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # quiet
                pass

            def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None):
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _record(self, method: str, rpc: str | None, auth_ok: bool | None):
                auth = self.headers.get("Authorization")
                with stub.lock:
                    stub.records.append({
                        "http": method, "path": self.path, "rpc": rpc,
                        "authorization": None if auth is None else (
                            "Bearer <key matched>" if auth == f"Bearer {stub.key}" else
                            f"<{auth.split(' ', 1)[0]} ... not matching, {len(auth)} chars>"),
                        "auth_ok": auth_ok, "session": self.headers.get("Mcp-Session-Id") is not None,
                        "protocol_header": self.headers.get("MCP-Protocol-Version"),
                        "accept": self.headers.get("Accept"), "user_agent": self.headers.get("User-Agent"),
                    })

            def _hub(self) -> bool:
                return self.path.split("?", 1)[0] == "/mcp"

            def _authorized(self) -> bool:
                return self.headers.get("Authorization") == f"Bearer {stub.key}"

            def do_GET(self):
                self._record("GET", None, self._authorized() if self._hub() else None)
                self._send(405, b"", {"Allow": "POST, DELETE"})

            def do_DELETE(self):
                self._record("DELETE", None, self._authorized() if self._hub() else None)
                self._send(204)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    message = json.loads(raw)
                except ValueError:
                    message = None
                rpc = message.get("method") if isinstance(message, dict) else None
                hub = self._hub()
                ok = self._authorized() if hub else None
                self._record("POST", rpc, ok)
                if hub and not ok:
                    body = json.dumps({"jsonrpc": "2.0", "id": (message or {}).get("id"),
                                       "error": {"code": -32001, "message": "unauthorized"}}).encode()
                    self._send(401, body, {"Content-Type": "application/json",
                                           "WWW-Authenticate": 'Bearer realm="workbench-hub"'})
                    return
                if not isinstance(message, dict):
                    self._send(400, b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"parse error"}}',
                               {"Content-Type": "application/json"})
                    return
                if "id" not in message:  # notification
                    self._send(202)
                    return
                name = "probe_echo" if hub else "decoy_" + self.path.rsplit("/", 1)[-1]
                if rpc == "initialize":
                    version = (message.get("params") or {}).get("protocolVersion")
                    result = {"protocolVersion": version if version in ("2025-06-18", "2025-03-26") else "2025-06-18",
                              "capabilities": {"tools": {"listChanged": False}},
                              "serverInfo": {"name": "workbench-hub" if hub else name, "version": "probe"}}
                    headers = {"Mcp-Session-Id": secrets.token_hex(8)}
                elif rpc == "tools/list":
                    result = {"tools": [{"name": name, "description": "TI-15 probe tool: echoes its text argument.",
                                         "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                                                         "required": ["text"]}}]}
                    headers = {}
                elif rpc == "tools/call":
                    text = ((message.get("params") or {}).get("arguments") or {}).get("text", "")
                    result = {"content": [{"type": "text", "text": f"echo:{text}"}], "isError": False}
                    headers = {}
                elif rpc == "ping":
                    result, headers = {}, {}
                else:
                    body = json.dumps({"jsonrpc": "2.0", "id": message["id"],
                                       "error": {"code": -32601, "message": f"method not found: {rpc}"}}).encode()
                    self._send(200, body, {"Content-Type": "application/json"})
                    return
                body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
                self._send(200, body, {"Content-Type": "application/json", **headers})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, name="stub-mcp", daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def take(self) -> list[dict[str, Any]]:
        with self.lock:
            records, self.records = self.records, []
        return records

    def url(self, path: str = "/mcp") -> str:
        return f"http://127.0.0.1:{self.port}{path}"


# -- fixture -----------------------------------------------------------------------------------------------------------
def write(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.chmod(path, mode)
    return path


def mcp_json(name: str, url: str, headers: dict[str, str] | None = None) -> str:
    server: dict[str, Any] = {"type": "http", "url": url}
    if headers:
        server["headers"] = headers
    return json.dumps({"mcpServers": {name: server}}, indent=2) + "\n"


def skill(name: str) -> str:
    return f"---\nname: {name}\ndescription: TI-15 decoy skill {name}\n---\n\nDecoy.\n"


def plant_decoys(home: Path, project: Path, stub: StubMcp) -> dict[str, str]:
    """Every MCP/native location a disabled provider or the user's own OMP dir would read. Returns path -> decoy."""
    planted: dict[str, str] = {}

    def mcp(path: Path, name: str):
        write(path, mcp_json(f"decoy_{name}", stub.url(f"/decoy/{name}")))
        planted[str(path)] = name

    real_agent = home / ".omp" / "agent"
    mcp(real_agent / "mcp.json", "user_agent_dir")
    mcp(home / ".omp" / "mcp.json", "user_omp_root")
    mcp(home / ".mcp.json", "home_dotmcp")
    mcp(project / ".mcp.json", "project_dotmcp")
    mcp(project / "mcp.json", "project_mcp")
    mcp(project / ".omp" / "mcp.json", "project_omp")
    mcp(project / ".omp" / ".mcp.json", "project_omp_dot")
    mcp(home / ".claude.json", "claude_user")
    mcp(project / ".claude" / "mcp.json", "claude_project")
    mcp(home / ".cursor" / "mcp.json", "cursor_user")
    mcp(project / ".cursor" / "mcp.json", "cursor_project")
    write(home / ".codex" / "config.toml",
          f'[mcp_servers.decoy_codex_user]\nurl = "{stub.url("/decoy/codex_user")}"\n')
    planted[str(home / ".codex" / "config.toml")] = "codex_user"
    mcp(home / ".gemini" / "settings.json", "gemini_user")
    write(project / ".vscode" / "mcp.json",
          json.dumps({"servers": {"decoy_vscode_project": {"type": "http",
                                                           "url": stub.url("/decoy/vscode_project")}}}))
    planted[str(project / ".vscode" / "mcp.json")] = "vscode_project"
    # native-provider decoys (what re-enabling `native` could bring back)
    write(real_agent / "SYSTEM.md", "DECOY-USER-REAL-SYSTEM\n")
    write(real_agent / "AGENTS.md", "DECOY-USER-REAL-AGENTS\n")
    write(real_agent / "skills" / "decoy-user-real-skill" / "SKILL.md", skill("decoy-user-real-skill"))
    write(home / ".omp" / "SYSTEM.md", "DECOY-HOME-OMP-SYSTEM\n")
    write(home / ".omp" / "AGENTS.md", "DECOY-HOME-OMP-AGENTS\n")
    write(home / ".omp" / "skills" / "decoy-home-omp-skill" / "SKILL.md", skill("decoy-home-omp-skill"))
    write(project / ".omp" / "SYSTEM.md", "DECOY-PROJECT-SYSTEM\n")
    write(project / ".omp" / "AGENTS.md", "DECOY-PROJECT-AGENTS\n")
    write(project / ".omp" / "skills" / "decoy-project-skill" / "SKILL.md", skill("decoy-project-skill"))
    write(project / ".omp" / "rules" / "decoy-rule.md",
          "---\ndescription: decoy rule\nalwaysApply: true\n---\nDECOY-PROJECT-RULE\n")
    write(project / ".omp" / "commands" / "decoy-cmd.md", "---\ndescription: decoy command\n---\nDecoy.\n")
    return planted


def board_overlay(*, startup_ms: int | None) -> dict[str, Any]:
    """The candidate Board overlay (second ``--config`` after omp-isolation.yml).

    ``disabledProviders`` replaces the isolation list (a later overlay's array wins) without ``native``: the native
    provider is the only one that reads an MCP file from the Workbench agent dir (``<agent dir>/mcp.json``, level
    "user"); project-level native MCP files stay off through ``mcp.enableProjectConfig: false``. No Workbench skills
    (no skill directory) and OMP 18.12's ``seance`` agent off, as for the manager/worker. Memory back on (D-25).
    """
    mcp: dict[str, Any] = {"enableProjectConfig": False}
    if startup_ms is not None:
        mcp["startupTimeoutMs"] = startup_ms
    return {"disabledProviders": list(BOARD_PROVIDER_IDS), "mcp": mcp,
            "skills": {"customDirectories": [], "includeSkills": [], "ignoredSkills": []},
            "task": {"disabledAgents": ["seance"]},
            "memory": {"backend": "local"}, "memories": {"enabled": True}}


# -- one OMP run -------------------------------------------------------------------------------------------------------
def ticks(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return None if fields[0] in "ZX" else fields[19]


def owned_processes(root: Path) -> list[int]:
    """Live processes whose command line names our temp root (cmdline only)."""
    needle, found = str(root).encode(), []
    for entry in os.listdir("/proc"):
        if entry.isdigit() and int(entry) != os.getpid():
            try:
                if needle in Path(f"/proc/{entry}/cmdline").read_bytes() and ticks(int(entry)):
                    found.append(int(entry))
            except OSError:
                continue
    return sorted(found)


def hub_tool_names(observed: dict[str, Any]) -> list[str]:
    return [name for name in observed.get("mcp_tools") or [] if "workbench" in name and TOOL_NAME in name]


def run_check(omp: str, *, cwd: Path, env: dict[str, str], overlays: list[Path], extra: list[str],
              timeout: float, role: str = "board", allowed_skills: tuple[str, ...] = ()) -> dict[str, Any]:
    command = [omp]
    for overlay in overlays:
        command += ["--config", str(overlay)]
    command += ["--no-extensions", "--append-system-prompt", "", "--no-title", *extra]
    result = launcher.check_isolation(command, cwd=cwd, environment=env, role=role, allowed_skills=allowed_skills,
                                      omp_version=launcher.omp_version(omp), timeout=timeout, keep_raw=True)
    raw = result.pop("raw", None) or {}
    state = raw.get("wb-iso-state") or {}
    prompt = state.get("systemPrompt") or ""
    if isinstance(prompt, list):
        prompt = "\n".join(str(item) for item in prompt)
    result["argv"] = [*command, "--mode", "rpc", "--no-session", "--no-title"]
    result["markers_in_prompt"] = [marker for marker in MARKERS if marker in prompt]
    result["state_keys"] = sorted(state)
    result["session"] = {key: state.get(key) for key in ("sessionId", "sessionFile", "sessionName") if key in state}
    tools = [tool for tool in state.get("dumpTools") or [] if isinstance(tool, dict)
             and str(tool.get("name", "")).startswith("mcp__")]
    result["mcp_tool_dump"] = [{"name": tool.get("name"), "description": str(tool.get("description") or "")[:120]}
                               for tool in tools]
    result["prompt_mcp_lines"] = [line.strip()[:200] for line in prompt.splitlines() if "mcp__" in line][:10]
    return result


def rpc_session(omp: str, command: list[str], *, cwd: Path, env: dict[str, str], requests: list[dict[str, Any]],
                timeout: float) -> dict[str, Any]:
    """One RPC-mode OMP: send ``requests`` one at a time, collect the answers, stop our own group exactly."""
    import select
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    start = ticks(process.pid)
    answers: dict[str, Any] = {}
    try:
        buffer = b""
        deadline = time.monotonic() + timeout
        for request in requests:
            process.stdin.write(json.dumps(request).encode() + b"\n")
            process.stdin.flush()
            while request["id"] not in answers and time.monotonic() < deadline and process.poll() is None:
                ready, _, _ = select.select([process.stdout], [], [], 0.2)
                if not ready:
                    continue
                chunk = os.read(process.stdout.fileno(), 1 << 16)
                if not chunk:
                    break
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, dict) and item.get("type") == "response" and item.get("id"):
                        answers[item["id"]] = item
    finally:
        try:  # graceful first: end of input, then our own group only
            process.stdin.close()
            process.wait(3)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if process.poll() is not None or ticks(process.pid) != start:
                break
            try:
                os.killpg(process.pid, sig)
            except OSError:
                break
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                continue
        process.wait()
        try:
            process.stdout.close()
        except OSError:
            pass
    return {"answers": answers, "returncode": process.returncode}


def session_of(answer: dict[str, Any] | None) -> dict[str, Any]:
    data = (answer or {}).get("data") or {}
    return {key: data.get(key) for key in ("sessionId", "sessionFile", "sessionName", "messageCount")}


def run_continue(omp: str, *, cwd: Path, env: dict[str, str], overlays: list[Path], timeout: float) -> dict[str, Any]:
    """``--continue`` with sessions kept: (1) a fresh session only read, (2) the session named through RPC
    ``set_session_name`` plus one RPC ``bash`` (``echo ti15-probe``: a user shell entry recorded in the session,
    no model call), (3) a third start: which session does each start use?"""
    command = [omp]
    for overlay in overlays:
        command += ["--config", str(overlay)]
    command += ["--no-extensions", "--append-system-prompt", "", "--no-title", "--allow-home", "--continue",
                "--mode", "rpc"]
    first = rpc_session(omp, command, cwd=cwd, env=env, timeout=timeout,
                        requests=[{"id": "s1", "type": "get_state"}])
    second = rpc_session(omp, command, cwd=cwd, env=env, timeout=timeout,
                         requests=[{"id": "s1", "type": "get_state"},
                                   {"id": "n", "type": "set_session_name", "name": "ti15-probe"},
                                   {"id": "b", "type": "bash", "command": "echo ti15-probe"},
                                   {"id": "s2", "type": "get_state"}])
    third = rpc_session(omp, command, cwd=cwd, env=env, timeout=timeout,
                        requests=[{"id": "s1", "type": "get_state"}])
    return {"argv": command, "runs": [
        {"start": 1, "requests": ["get_state"], "session": session_of(first["answers"].get("s1")), "exit": first["returncode"]},
        {"start": 2, "requests": ["get_state", "set_session_name", "bash", "get_state"],
         "session": session_of(second["answers"].get("s1")), "rename_ok": (second["answers"].get("n") or {}).get("success"),
         "bash_ok": (second["answers"].get("b") or {}).get("success"),
         "session_after_rename": session_of(second["answers"].get("s2")), "exit": second["returncode"]},
        {"start": 3, "requests": ["get_state"], "session": session_of(third["answers"].get("s1")), "exit": third["returncode"]}]}


# -- probe -------------------------------------------------------------------------------------------------------------
def tree_size(path: Path) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    return total


def run_probe(omp: str, timeout: float, keep: bool) -> dict[str, Any]:
    started = time.monotonic()
    omp_tmp = Path("/tmp") / f"omp-workbench-{os.geteuid()}"
    omp_tmp_before = sorted(os.listdir(omp_tmp)) if omp_tmp.is_dir() else None
    root = Path(tempfile.mkdtemp(prefix="ti15-"))
    key = secrets.token_urlsafe(24)
    report: dict[str, Any] = {"_key": key, "omp": omp, "omp_version": launcher.omp_version(omp), "root": str(root),
                              "started_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                              "scenarios": {}}
    try:
        home = root / "home"
        project = home / "work" / "proj"
        project.mkdir(parents=True)
        runtime = root / "r"
        runtime.mkdir(mode=0o700)
        write(home / ".omp" / "agent" / "agent.db", "")  # placeholder store: linked, never opened by the probe
        base = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": str(runtime),
                "WORKBENCH_HUB_AUTOSTART": "0", "ANTHROPIC_API_KEY": FAKE_PROVIDER_KEY,
                **{name: BLOCKED for name in PROXY_KEYS}, "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost"}
        with StubMcp(key) as stub:
            report["stub"] = {"url": stub.url(), "decoy_base": stub.url("/decoy/")}
            planted = plant_decoys(home, project, stub)
            report["decoys"] = {"mcp": planted, "skills": list(DECOY_SKILLS), "prompt_markers": list(MARKERS)}

            # control: the manager/worker isolation unchanged (native disabled), mcp.json planted in its agent dir
            control_home = omp_home.prepare_omp_home(root / "data", base, skills_dir=launcher.default_skills_dir(),
                                                     provider_ids=launcher.ISOLATION_PROVIDER_IDS)
            write(control_home.agent_dir / "mcp.json",
                  mcp_json(SERVER_NAME, stub.url(), {"Authorization": f"Bearer ${{{KEY_ENV}}}"}))
            env = {**omp_home.home_environment(base, control_home.environment()), KEY_ENV: key}
            for role in launcher.OMP_ROLE_NAMES:
                control_overlay = launcher.write_role_overlay(root / "data", role, launcher.role_overlay(
                    role, project_dir=project, home=home, environment=env))
                stub.take()
                result = run_check(omp, cwd=project, env=env, overlays=[launcher.default_isolation_overlay(),
                                                                        control_overlay],
                                   extra=launcher.role_tool_args(role), timeout=timeout, role=role,
                                   allowed_skills=launcher.role_skill_allowlist(role))
                result["cwd"] = str(project)
                result["requests"] = stub.take()
                report["scenarios"][f"control_{role}"] = result

            # board: Workbench home under <hub state>/board-agent, mcp.json in its agent dir
            empty = root / "hub-state" / "board-agent" / "empty"
            empty.mkdir(parents=True)
            board_home = omp_home.prepare_omp_home(root / "hub-state" / "board-agent", base, skills_dir=empty,
                                                   provider_ids=BOARD_PROVIDER_IDS, agents_dir=empty)
            report["board_home"] = {"root": str(board_home.root), "agent_dir": str(board_home.agent_dir),
                                    "config_dir": board_home.config_dir, "linked": board_home.linked,
                                    "environment": board_home.environment()}
            overlay = root / "hub-state" / "board-agent" / "omp-board.yml"
            write(overlay, json.dumps(board_overlay(startup_ms=None), indent=2) + "\n")
            report["board_overlay"] = board_overlay(startup_ms=None)
            mcp_file = board_home.agent_dir / "mcp.json"
            env_header = {"Authorization": f"Bearer ${{{KEY_ENV}}}"}
            write(mcp_file, mcp_json(SERVER_NAME, stub.url(), env_header))
            report["board_mcp_json"] = json.loads(mcp_file.read_text())
            env = {**omp_home.home_environment(base, board_home.environment()), KEY_ENV: key}
            overlays = [launcher.default_isolation_overlay(), overlay]
            for name, cwd, extra in (("board_env", home, ["--allow-home"]),
                                     ("board_project_cwd", project, [])):
                stub.take()
                result = run_check(omp, cwd=cwd, env=env, overlays=overlays, extra=extra, timeout=timeout)
                result["cwd"] = str(cwd)
                result["requests"] = stub.take()
                report["scenarios"][name] = result

            write(mcp_file, mcp_json(SERVER_NAME, stub.url(), {"Authorization": f"Bearer {key}"}))
            stub.take()
            result = run_check(omp, cwd=home, env={k: v for k, v in env.items() if k != KEY_ENV}, overlays=overlays,
                               extra=["--allow-home"], timeout=timeout)
            result["cwd"] = str(home)
            result["requests"] = stub.take()
            report["scenarios"]["board_literal"] = result

            write(mcp_file, mcp_json(SERVER_NAME, stub.url(), env_header))
            stub.take()
            result = run_continue(omp, cwd=home, env=env, overlays=overlays, timeout=timeout)
            result["requests"] = stub.take()
            sessions = board_home.agent_dir / "sessions"
            result["sessions_dir"] = str(sessions)
            result["session_files"] = sorted(str(p.relative_to(board_home.agent_dir)) for p in sessions.rglob("*")
                                             if p.is_file()) if sessions.is_dir() else []
            report["scenarios"]["board_continue"] = result
            report["leftover_processes"] = owned_processes(root)
            natives = home / ".omp" / "natives"
            report["natives"] = {"dir": str(natives), "bytes": tree_size(natives) if natives.is_dir() else 0,
                                 "entries": sorted(os.listdir(natives)) if natives.is_dir() else []}
    finally:
        for _ in range(50):
            if not owned_processes(root):
                break
            time.sleep(0.1)
        report["leftover_processes_final"] = owned_processes(root)
        if keep:
            report["kept"] = True
        elif not report["leftover_processes_final"]:
            shutil.rmtree(root, ignore_errors=True)
        report["root_removed"] = not root.exists()
        after = sorted(os.listdir(omp_tmp)) if omp_tmp.is_dir() else None
        report["omp_tmp"] = {"dir": str(omp_tmp), "before": len(omp_tmp_before or []), "after": len(after or []),
                             "new": sorted(set(after or []) - set(omp_tmp_before or []))}
        report["duration"] = round(time.monotonic() - started, 1)
    return report


# -- verdict & report --------------------------------------------------------------------------------------------------
def judge(report: dict[str, Any]) -> dict[str, Any]:
    s = report["scenarios"]
    checks: dict[str, bool] = {}
    for role in launcher.OMP_ROLE_NAMES:
        control = s[f"control_{role}"]
        checks[f"control_{role}: existing isolation check ok (0 leaks)"] = (
            control["state"] in ("ok", "warning") and not control["leaks"])
        checks[f"control_{role}: no MCP tool, no stub request"] = (
            not (control.get("observed") or {}).get("mcp_tools") and not control["requests"])
    for name in ("board_env", "board_literal"):
        res = s[name]
        observed = res.get("observed") or {}
        tools = observed.get("mcp_tools") or []
        hub = hub_tool_names(observed)
        other_leaks = [leak for leak in res["leaks"] if leak not in {f"mcp:{tool}" for tool in hub}]
        hub_requests = [r for r in res["requests"] if r["path"].split("?")[0] == "/mcp"]
        decoys = [r for r in res["requests"] if r["path"].startswith("/decoy/")]
        checks[f"{name}: exactly one MCP tool, from {SERVER_NAME}"] = len(tools) == 1 and tools == hub
        checks[f"{name}: no leak besides that tool"] = not res.get("error") and not other_leaks
        checks[f"{name}: stub saw initialize + tools/list with the Bearer key"] = (
            {"initialize", "tools/list"} <= {r["rpc"] for r in hub_requests if r["auth_ok"]})
        checks[f"{name}: every hub request carried the key"] = bool(hub_requests) and all(r["auth_ok"] for r in hub_requests)
        checks[f"{name}: no decoy server contacted"] = not decoys
        checks[f"{name}: no decoy prompt marker"] = not res.get("markers_in_prompt")
    checks["decoys: every MCP decoy planted"] = sorted(report["decoys"]["mcp"].values()) == sorted(DECOY_MCP)
    checks["processes: none left"] = not report.get("leftover_processes_final")
    passed = all(checks.values())
    return {"verdict": "passed" if passed else "failed", "checks": checks}


def scrub(report: dict[str, Any]) -> dict[str, Any]:
    """Temp root and repo paths shortened; the key must not appear anywhere (checked, never printed)."""
    key = report.pop("_key")
    text = json.dumps(report, ensure_ascii=False)
    if key in text:
        raise SystemExit("probe error: the MCP key reached the observation record")
    text = text.replace(report["root"], "<root>").replace(str(REPO), "<repo>")
    return json.loads(text)


SOURCE_FINDINGS = """\
설치된 OMP 18.12.6 바이너리에 포함된 번들 소스(읽기 전용 검색, `packages/coding-agent/src/…` 주석 기준)에서 확인한 사실:

- MCP 서버 목록은 capability `mcps`를 제공하는 capability provider가 모은다(`mcp/config.ts`, `capability/mcp.ts`).
  설정 overlay(`config.yml`/`--config`)에는 서버를 직접 적는 key가 없다. `mcp.*` 설정은 `enableProjectConfig`,
  `startupTimeoutMs`(기본 250), `notifications`, `notificationDebounceMs`, `renderMarkdownResults`뿐이다
  (`mcp/settings.ts`).
- `disabledProviders`는 provider 단위로 모든 capability에 함께 적용된다(`capability/index.ts`). MCP만 따로 켜는
  capability별 스위치는 없다.
- `native` provider(`discovery/builtin.ts`)가 읽는 MCP 파일: `<cwd>/.omp/mcp.json`, `<cwd>/.omp/.mcp.json`
  (level project), `<agent dir>/mcp.json`, `<agent dir>/.mcp.json`(level user). agent dir은 `PI_CODING_AGENT_DIR`
  (Workbench home에서는 `<omp-root>/agent`)이다. Project level 항목은 `mcp.enableProjectConfig:false`이면 걸러진다
  (`mcp/config.ts`의 filter).
- `mcp-json` provider(`discovery/mcp-json.ts`)는 `<cwd>/mcp.json`·`<cwd>/.mcp.json`(level project)만 읽는다.
  지정 파일을 줄 방법이 없어 Board agent에는 쓸 수 없고 계속 비활성으로 둔다.
- MCP 설정의 문자열 값(url, headers, env, args 등)은 `${VAR}`·`${VAR:-default}`를 OMP 프로세스 환경 변수로
  치환한다(`discovery/helpers.ts`). 따라서 header는 값을 직접 적거나 환경 변수를 참조할 수 있다.
- user `mcp.json`의 `disabledServers`/`enabledServers` 배열과 서버별 `enabled:false`로 서버를 끌 수 있다
  (`mcp/config.ts`).
- `native`를 켜면 그 provider의 다른 capability(SYSTEM.md, AGENTS.md, skills, rules, commands, prompts, hooks,
  tools, settings)도 같이 켜진다. Project level은 `<cwd>/.omp/`(cwd가 HOME이면 건너뜀)와 cwd에서 repo root/HOME까지의
  상위 디렉터리(HOME 자체는 제외)를 본다(`Ka`, `NOe`). cwd가 HOME이면 OMP는 `~/tmp`·`/tmp`로 cwd를 옮기므로
  (`cli/startup-cwd.ts`) `--allow-home`이 필요하다.
- HTTP transport(`mcp/transports/http.ts`)는 설정 header를 모든 POST/GET/DELETE에 싣고, `initialize` 응답의
  `Mcp-Session-Id`를 이후 요청에 붙이며, `GET`(SSE listener)에 405가 오면 조용히 넘어간다.
"""


def _fence(value: Any) -> str:
    return "```json\n" + json.dumps(value, indent=2, ensure_ascii=False) + "\n```\n"


def render(report: dict[str, Any]) -> str:
    j = report["judgement"]
    s = report["scenarios"]
    out = ["# PROBE-OMP-MCP: OMP MCP 연결 feasibility probe(TI-15)\n",
           f"판정: **{j['verdict']}**. OMP `{report['omp_version']}`, 실행 {report['started_at']}, "
           f"{report['duration']} s. 이 파일은 `tests/hub/probes/omp_mcp_probe.py --report …`가 생성한다.\n",
           "모델·provider 호출 없음(RPC `get_state`·`get_available_commands`·`get_login_providers`·"
           "`set_session_name`·`bash echo`만. HTTP(S)/ALL_PROXY는 닫힌 port, loopback만 NO_PROXY). temp HOME과 "
           "placeholder `agent.db`(빈 파일, symlink만 만들고 열지 않음)를 썼다. 사용자 `~/.omp`는 읽지도 쓰지도 않았다.\n",
           "## 판정 기준과 결과\n", "| 확인 | 결과 |", "|---|---|"]
    out += [f"| {name} | {'ok' if ok else 'FAIL'} |" for name, ok in j["checks"].items()]
    out += ["", "## TI-16이 쓸 구성\n",
            "Board agent home: `omp_home.prepare_omp_home(<hub state>/board-agent, …, provider_ids=<isolation 목록 − "
            "native>)`(`skills_dir`·`agents_dir`는 빈 Workbench 디렉터리). 그 agent dir에 `mcp.json`(0600)을 둔다.\n",
            "argv(cwd = `$HOME`):\n",
            "```\nomp --config omp_bridge/omp-isolation.yml --config <board overlay> --no-extensions "
            "--append-system-prompt <role prompt> --no-title --allow-home [--continue] [--model …]\n```\n",
            "Board overlay(두 번째 `--config`. 나중 overlay의 배열이 앞의 것을 대체한다는 것을 이 probe가 확인했다):\n",
            _fence(report["board_overlay"]),
            "`<agent dir>/mcp.json`(header는 환경 변수 참조, key는 파일에 들어가지 않는다):\n",
            _fence(report["board_mcp_json"]),
            f"환경: `omp_home.home_environment(base, home.environment())` + `{KEY_ENV}=<시스템 editor key>`. "
            "`board_literal`처럼 key 값을 파일에 직접 적어도 동작한다(그 경우 파일 0600, 시작마다 다시 쓴다). 어느 쪽이든 "
            "같은 uid로 도는 Board agent의 bash는 key를 읽을 수 있다(ADR-0003의 범위).\n",
            "Board home(관측):\n", _fence(report["board_home"]),
            "Tool 이름: `mcp__<서버 이름, `-`→`_`>_<tool 이름>`. 서버 `workbench-hub`의 tool `probe_echo`는 "
            "`mcp__workbench_hub_probe_echo`다. OMP 18.12에서 MCP tool은 RPC `dumpTools`에 나오지 않고 system prompt의 "
            "discovery 목록(`xd://mcp__…`)으로만 보인다. `launcher.observe_isolation`은 둘 다 보므로 그대로 잡는다.\n",
            "격리 확인(TI-16): `check_isolation` 결과의 leak 중 `mcp:mcp__workbench_hub_*`만 기대값이다. 다른 leak이나 다른 "
            "`mcp__` 이름이 있으면 `isolation_failed`.\n",
            "## 관측\n"]
    for name, res in s.items():
        out.append(f"### {name}\n")
        if name == "board_continue":
            out.append("`--continue`(session 유지)로 세 번 시작했다. 두 번째에 `set_session_name`과 RPC `bash echo`를 보냈다.\n")
            out.append(_fence({"argv": res["argv"], "runs": res["runs"], "session_files": res["session_files"],
                               "hub_requests": len(res["requests"]),
                               "hub_requests_all_with_key": all(r["auth_ok"] for r in res["requests"])}))
            continue
        observed = res.get("observed") or {}
        out.append(_fence({
            "cwd": res.get("cwd"), "argv": res.get("argv"), "state": res.get("state"), "error": res.get("error"),
            "leaks": res.get("leaks"), "warnings": res.get("warnings"),
            "mcp_tools": observed.get("mcp_tools"), "prompt_mcp_lines": res.get("prompt_mcp_lines"),
            "tools": observed.get("tools"), "skills": observed.get("skills"),
            "task_agents": observed.get("task_agents"), "other_commands": observed.get("other_commands"),
            "context_files": observed.get("context_files"), "decoy_markers_in_prompt": res.get("markers_in_prompt"),
            "system_prompt_default": observed.get("system_prompt_default"),
            "omp_home": {k: (observed.get("omp_home") or {}).get(k) for k in
                         ("config_root_in_use", "agent_dir_in_use", "user_paths", "authenticated_providers")},
            "cleanup": res.get("cleanup"),
            "stub_requests": res.get("requests")}))
    out += ["## 미끼(decoy)\n",
            "모든 미끼 MCP 서버는 같은 stub의 `/decoy/<이름>`을 가리킨다. 접속하면 stub 기록에 남는다.\n",
            _fence(report["decoys"]),
            "## OMP 소스에서 확인한 설정 경로\n", SOURCE_FINDINGS,
            "## 결과 해석\n",
            "- 단일 서버: isolation overlay를 그대로 두고 두 번째 overlay가 `disabledProviders`에서 `native`만 빼면, OMP는 "
            "Workbench agent dir의 `mcp.json`에 적은 Streamable HTTP 서버 하나에 연결하고 그 tool만 보인다. 사용자 "
            "`~/.omp`(agent dir·root), 프로젝트 `.mcp.json`/`mcp.json`/`.omp/mcp.json`, Claude·Cursor·Codex·Gemini·"
            "VS Code 미끼에는 접속하지 않았다(`board_env`, `board_literal`).",
            "- Header: stub은 `initialize`, `notifications/initialized`, `GET`(SSE, 405), `tools/list` 모두에서 "
            "`Authorization: Bearer <key>`를 받았다. `${VAR}` 참조와 값 직접 기입 둘 다 동작한다.",
            "- Manager·worker: 기존 isolation(`native` 비활성)은 그 agent dir에 `mcp.json`을 두어도 MCP tool·접속이 없고 "
            "누출 검사가 0건이다(`control_manager`, `control_worker`). 이 probe는 제품 코드를 바꾸지 않았다.",
            "- 주의(cwd): `native`를 켜면 project `.omp/`의 SYSTEM.md·AGENTS.md·commands·rules도 돌아온다"
            "(`board_project_cwd`: `system_prompt:replaced`, `context_file`, `command:decoy-cmd`). Board agent는 cwd를 "
            "`$HOME`으로 하고 `--allow-home`을 준다(HOME에서는 project lookup이 없다). 다른 cwd를 쓰려면 그 디렉터리와 "
            "상위(HOME 전까지)에 `.omp/`가 없어야 하고, 시작 격리 확인이 그것을 잡는다.",
            "- Project skill 미끼(`.omp/skills`)는 isolation의 `skills.enablePiProject:false`로 이미 막혀 보이지 않았다.",
            "- `--continue`: session은 `<agent dir>/sessions/<cwd slug>/`에 놓인다. 모델 응답 없이(`set_session_name`, "
            "RPC `bash`)는 session 파일이 써지지 않아 다음 `--continue`가 새 session을 열었다. 이전 대화를 잇는지는 모델 "
            "turn이 있어야 확인되므로 이 probe에서는 **미확인**이다(실제 OMP·모델 확인으로 남김, P2-AC-33 `pending`).",
            "- `memory.backend:\"local\"`, `memories.enabled:true`는 overlay에 넣었고 시작·격리 확인이 통과했다. 실제 "
            "기억 생성은 모델 turn이 필요해 **미확인**이다.",
            "- `tools/call`: stub은 구현했지만 OMP가 tool을 부르려면 모델 turn이 필요해 이 probe에서는 부르지 않았다.",
            "- `mcp.startupTimeoutMs`는 기본값(250 ms)으로도 loopback stub의 tool이 첫 `get_state`에 보였다. Hub 내부 "
            "listener가 느리면 tool이 늦게 보일 수 있으므로 TI-16의 격리 확인은 Hub listener가 열린 뒤 시작한다.\n",
            "## 정리\n",
            _fence({"leftover_processes_final": report["leftover_processes_final"],
                    "root_removed": report["root_removed"], "omp_tmp": report["omp_tmp"],
                    "natives": report.get("natives")}),
            "natives는 temp HOME의 `~/.omp/natives/<version>`에 추출되고 temp root와 함께 지워진다(크기는 위 bytes). "
            "남은 OMP 프로세스는 `/proc/*/cmdline`에서 temp root 경로로 찾아 없음을 확인했다.\n"]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, help="write the Markdown result record here")
    parser.add_argument("--json", type=Path, help="also write the raw observation JSON here")
    parser.add_argument("--omp", default=shutil.which("omp") or os.path.expanduser("~/.local/bin/omp"))
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--keep", action="store_true", help="keep the temp root (debugging)")
    args = parser.parse_args(argv)
    if not os.access(args.omp, os.X_OK):
        print(f"omp not found: {args.omp}", file=sys.stderr)
        return 2
    report = scrub(run_probe(args.omp, args.timeout, args.keep) | {})
    report["judgement"] = judge(report)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    if args.report:
        args.report.write_text(render(report))
    print(json.dumps({"verdict": report["judgement"]["verdict"], "checks": report["judgement"]["checks"],
                      "omp_version": report["omp_version"], "duration": report["duration"],
                      "omp_tmp": report["omp_tmp"], "root_removed": report["root_removed"]}, indent=2))
    return 0 if report["judgement"]["verdict"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
