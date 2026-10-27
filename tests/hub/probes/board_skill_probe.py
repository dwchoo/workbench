"""WBX-B4 probe (WBX-O02): under a Board overlay, does the installed OMP see exactly the ``wb-inbox`` skill (no MCP)
and does the model use it through bash to read the request list from a loopback Hub REST API?

Manual run only (not part of any unittest suite), from the repo root::

    env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python tests/hub/probes/board_skill_probe.py \\
        --report docs/features/web-board-ux/PROBE-BOARD-SKILL.md [--stage1-only]

Stage 1 (no model call): a temp HOME with decoy skills/MCP files, a Board home (``omp_home.prepare_omp_home`` under
``<hub state>/board-agent``) whose skill directory is a copy of ``skills/wb-inbox`` (``SKILL.md`` + ``wb_inbox.py``,
no ``.env``), a stale ``mcp.json`` left in its agent dir (an older Board version), and the candidate overlay. The real
OMP runs exactly like the start-up isolation check (``launcher.check_isolation(..., allowed_skills=("wb-inbox",))``).
A negative control runs the same with an empty skill directory (what ``skill_missing:wb-inbox`` must see).

Stage 2 (one real model turn, WBX-D27): a real loopback Hub web app with ``/api/v1/`` (test seam S2,
``tests/hub/support/rest_hub.py``) with seeded requests; the OMP gets ``WB_HUB_URL`` (loopback) and ``WB_API_KEY`` (a
new system editor key) in its environment and one RPC prompt "use the wb-inbox skill to list requests". Observed: the
tool calls (bash command lines, key scrubbed), every Hub request (method, path, status, whether it carried the system
key; never the key), the key's ``last_used_at``, and whether the final answer names the seeded titles (booleans only;
the answer text is not kept).

Login: the Board home's ``agent.db`` is the product's symlink to the user's own OMP auth store (``omp_home``; the
probe never opens it). Located through ``PI_CODING_AGENT_DIR`` of the user's own environment, so HOME can stay a
temp dir: nothing else under the user's ``~/.omp`` is used (natives are extracted into the temp HOME). Provider API
key variables of the caller are not passed on.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import secrets
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests" / "hub"))

from workbench.backend import launcher, omp_home  # noqa: E402

SKILL = "wb-inbox"
SKILL_FILES = ("SKILL.md", "wb_inbox.py")
DECOY_SKILLS = ("decoy-home-omp-skill", "decoy-home-agent-skill", "decoy-claude-skill", "decoy-codex-skill",
                "decoy-agents-skill", "decoy-board-agentdir-skill")
PROMPT = ("Use the wb-inbox skill to list the requests in the Workbench inbox. "
          "Reply with how many requests there are and their titles.")
SEEDED = (("alpha", "B4 probe request one"), ("alpha", "B4 probe request two"), ("beta", "B4 probe request three"))
CLOSED = "http://127.0.0.1:9"


# -- fixture -----------------------------------------------------------------------------------------------------------
def write(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.chmod(path, mode)
    return path


def skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: WBX-B4 decoy skill {name}\n---\n\nDecoy.\n"


def plant_decoys(home: Path, board_agent_dir: Path) -> list[str]:
    """Skills and MCP files in every ambient location a Board agent with cwd=$HOME could read."""
    write(home / ".omp" / "skills" / "decoy-home-omp-skill" / "SKILL.md", skill_md("decoy-home-omp-skill"))
    write(home / ".omp" / "agent" / "skills" / "decoy-home-agent-skill" / "SKILL.md", skill_md("decoy-home-agent-skill"))
    write(home / ".claude" / "skills" / "decoy-claude-skill" / "SKILL.md", skill_md("decoy-claude-skill"))
    write(home / ".codex" / "skills" / "decoy-codex-skill" / "SKILL.md", skill_md("decoy-codex-skill"))
    write(home / ".agents" / "skills" / "decoy-agents-skill" / "SKILL.md", skill_md("decoy-agents-skill"))
    write(board_agent_dir / "skills" / "decoy-board-agentdir-skill" / "SKILL.md",
          skill_md("decoy-board-agentdir-skill"))
    mcp = json.dumps({"mcpServers": {"decoy": {"type": "http", "url": CLOSED + "/mcp"}}}) + "\n"
    planted = []
    for path in (home / ".omp" / "agent" / "mcp.json", home / ".omp" / "mcp.json", home / ".mcp.json",
                 home / ".claude.json"):
        write(path, mcp)
        planted.append(str(path))
    write(home / ".omp" / "agent" / "AGENTS.md", "DECOY-HOME-AGENTS\n")
    write(home / ".omp" / "SYSTEM.md", "DECOY-HOME-SYSTEM\n")
    return planted


def copy_skill(target: Path) -> Path:
    """``<board skills>/wb-inbox/`` (0700) from the repo's ``skills/wb-inbox`` (SKILL.md, wb_inbox.py only)."""
    folder = target / SKILL
    folder.mkdir(parents=True, mode=0o700)
    os.chmod(target, 0o700)
    os.chmod(folder, 0o700)
    for name in SKILL_FILES:
        shutil.copyfile(REPO / "skills" / SKILL / name, folder / name)
        os.chmod(folder / name, 0o600)
    return target


def board_overlay(skills_dir: Path) -> dict[str, Any]:
    """Candidate overlay (second ``--config`` after omp-isolation.yml): the full isolation provider list (``native``
    off again: no ``mcp.json`` to read), the Board skill copy as the only custom skill directory, ``seance`` off,
    memory on (D-25)."""
    return {"disabledProviders": list(launcher.ISOLATION_PROVIDER_IDS), "mcp": {"enableProjectConfig": False},
            "skills": {"customDirectories": [str(skills_dir)], "includeSkills": [SKILL], "ignoredSkills": []},
            "task": {"disabledAgents": ["seance"]},
            "memory": {"backend": "local"}, "memories": {"enabled": True}}


def board_argv(omp: str, overlay: Path, *, model: str | None) -> list[str]:
    argv = [omp, "--config", str(launcher.default_isolation_overlay()), "--config", str(overlay), "--no-extensions",
            "--append-system-prompt", "", "--no-title", "--allow-home"]
    if model:
        argv += ["--model", model]
    return argv


# -- processes ---------------------------------------------------------------------------------------------------------
def ticks(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return None if fields[0] in "ZX" else fields[19]


def owned_processes(root: Path) -> list[int]:
    needle, found = str(root).encode(), []
    for entry in os.listdir("/proc"):
        if entry.isdigit() and int(entry) != os.getpid():
            try:
                if needle in Path(f"/proc/{entry}/cmdline").read_bytes() and ticks(int(entry)):
                    found.append(int(entry))
            except OSError:
                continue
    return sorted(found)


def stop_group(process: subprocess.Popen, start: str | None) -> list[int]:
    try:
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
    try:  # anything the bash tool left in the group
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    process.wait()
    for stream in (process.stdout,):
        try:
            stream.close()
        except OSError:
            pass
    return [process.returncode]


# -- stage 1 -----------------------------------------------------------------------------------------------------------
def run_check(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> dict[str, Any]:
    result = launcher.check_isolation(argv, cwd=cwd, environment=env, role="board", allowed_skills=(SKILL,),
                                      omp_version=launcher.omp_version(argv[0]), timeout=timeout, keep_raw=True)
    raw = result.pop("raw", None) or {}
    state = raw.get("wb-iso-state") or {}
    prompt = state.get("systemPrompt") or ""
    if isinstance(prompt, list):
        prompt = "\n".join(str(item) for item in prompt)
    observed = result.get("observed") or {}
    lines = [line.strip()[:240] for line in prompt.splitlines() if SKILL in line or "skill://" in line][:12]
    return {"state": result.get("state"), "error": result.get("error"), "leaks": result.get("leaks"),
            "warnings": result.get("warnings"), "skills": observed.get("skills"),
            "skill_commands": observed.get("skill_commands"), "mcp_tools": observed.get("mcp_tools"),
            "tools": observed.get("tools"), "task_agents": observed.get("task_agents"),
            "other_commands": observed.get("other_commands"), "context_files": observed.get("context_files"),
            "system_prompt_default": observed.get("system_prompt_default"), "model": observed.get("model"),
            "omp_home": {k: (observed.get("omp_home") or {}).get(k) for k in
                         ("config_root_in_use", "agent_dir_in_use", "user_paths", "authenticated_providers")},
            "decoy_markers_in_prompt": [m for m in ("DECOY-HOME-AGENTS", "DECOY-HOME-SYSTEM") if m in prompt],
            "decoy_skills_in_prompt": [name for name in DECOY_SKILLS if name in prompt],
            "skill_prompt_lines": lines, "cleanup": result.get("cleanup"), "argv": [*argv, "--mode", "rpc",
                                                                                     "--no-session", "--no-title"]}


# -- stage 2 -----------------------------------------------------------------------------------------------------------
class _Case:
    """The ``addCleanup`` half of ``unittest.TestCase`` that ``RestHub`` needs."""

    def __init__(self):
        self.cleanups: list = []

    def addCleanup(self, function, *args, **kwargs):  # noqa: N802 - unittest name
        self.cleanups.append((function, args, kwargs))

    def cleanup(self):
        while self.cleanups:
            function, args, kwargs = self.cleanups.pop()
            try:
                function(*args, **kwargs)
            except Exception:  # noqa: BLE001 - best effort teardown
                pass


def start_hub(case: _Case, records: list[dict[str, Any]], system_key: list[str]):
    """``RestHub`` (S2) with an ``on_response_prepare`` recorder: method, path, status, key matched (never the key)."""
    from support import rest_hub
    original = rest_hub.create_app

    async def record(request, response):
        auth = request.headers.get("Authorization")
        records.append({"method": request.method, "path": request.path, "status": response.status,
                        "query_keys": sorted(set(request.query.keys())),
                        "authorization": None if auth is None else (
                            "Bearer <system key>" if system_key and auth == f"Bearer {system_key[0]}" else
                            "<other>"), "user_agent": (request.headers.get("User-Agent") or "")[:40]})

    def recorder(app):
        app.on_response_prepare.append(record)

    def create_app(services, *, routes=()):
        return original(services, routes=[*routes, recorder])

    rest_hub.create_app = create_app
    try:
        hub = rest_hub.RestHub(case, flush_interval=0.2)
    finally:
        rest_hub.create_app = original
    return hub


def seed(hub) -> list[str]:
    seeder = hub.key("b4-seeder")
    client = hub.rest(seeder)
    ids = []
    for index, (project, title) in enumerate(SEEDED):
        reply = client.post("/requests", {"project_id": hub.a if project == "alpha" else hub.b, "title": title,
                                          "body": f"Seeded by the WBX-B4 probe ({index}).",
                                          "idempotency_key": f"b4-seed-{index}"})
        if reply.status not in (200, 201):
            raise SystemExit(f"probe error: seeding failed with {reply.status}")
        ids.append(reply.json.get("id") or reply.json.get("request_id"))
    return ids


def summarize_args(name: str, args: Any, key: str) -> dict[str, Any]:
    if not isinstance(args, dict):
        return {}
    keep = {}
    for field in ("command", "path", "url", "uri", "file_path", "name", "skill", "pattern"):
        if field in args and isinstance(args[field], str):
            keep[field] = args[field].replace(key, "<system key>")[:300]
    keep["arg_keys"] = sorted(args)
    return keep


def model_turn(argv: list[str], *, cwd: Path, env: dict[str, str], key: str, timeout: float) -> dict[str, Any]:
    command = [*argv, "--mode", "rpc", "--no-session"]
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    start = ticks(process.pid)
    types: dict[str, int] = {}
    tools: list[dict[str, Any]] = []
    tool_ends: list[dict[str, Any]] = []
    texts: list[str] = []
    errors: list[str] = []
    response: dict[str, Any] | None = None
    ended = False
    key_in_stream = False
    ui_requests: list[dict[str, Any]] = []
    sent_at: float | None = None
    ready_after: float | None = None

    def send(frame: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(frame).encode() + b"\n")
        process.stdin.flush()

    try:
        # The prompt goes out only after OMP answered get_state (start-up finished); a refused prompt ends the turn.
        send({"id": "s", "type": "get_state"})
        buffer = b""
        deadline = started + timeout
        while not ended and time.monotonic() < deadline and process.poll() is None:
            if response is not None and response.get("success") is False:
                break
            ready, _, _ = select.select([process.stdout], [], [], 0.5)
            if not ready:
                continue
            chunk = os.read(process.stdout.fileno(), 1 << 16)
            if not chunk:
                break
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                if key.encode() in line:
                    key_in_stream = True
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(frame, dict):
                    continue
                kind = str(frame.get("type"))
                types[kind] = types.get(kind, 0) + 1
                if kind == "response" and frame.get("id") == "s" and sent_at is None:
                    ready_after = round(time.monotonic() - started, 1)
                    sent_at = time.monotonic()
                    send({"id": "p", "type": "prompt", "message": PROMPT})
                elif kind == "extension_ui_request":
                    ui_requests.append({"method": frame.get("method"),
                                        "title": str(frame.get("title") or "")[:80].replace(key, "<k>")})
                elif kind == "response" and frame.get("id") == "p":
                    response = {"success": frame.get("success"), "error": str(frame.get("error") or "")[:200]}
                elif kind == "tool_execution_start":
                    tools.append({"tool": frame.get("toolName"),
                                  **summarize_args(str(frame.get("toolName")), frame.get("args"), key)})
                elif kind == "tool_execution_end":
                    result = frame.get("result")
                    text = json.dumps(result, ensure_ascii=False) if result is not None else ""
                    tool_ends.append({"tool": frame.get("toolName"), "isError": frame.get("isError"),
                                      "result_chars": len(text),
                                      "mentions_seeded_title": any(t in text for _, t in SEEDED)})
                elif kind == "message_end":
                    message = frame.get("message") or {}
                    if message.get("role") == "assistant":
                        for part in message.get("content") or []:
                            if isinstance(part, dict) and part.get("type") == "text":
                                texts.append(str(part.get("text") or ""))
                        if message.get("errorMessage") or message.get("stopReason") == "error":
                            errors.append(str(message.get("errorMessage") or "error")[:200].replace(key, "<k>"))
                elif kind == "agent_end":
                    ended = True
    finally:
        stop_group(process, start)
    answer = "\n".join(texts)
    return {"argv": command, "prompt": PROMPT, "duration": round(time.monotonic() - started, 1),
            "timed_out": not ended and time.monotonic() - started >= timeout, "agent_end": ended,
            "state_answered_after_s": ready_after, "ui_requests": ui_requests,
            "prompt_response": response, "event_types": dict(sorted(types.items())), "tool_calls": tools,
            "tool_results": tool_ends, "assistant_errors": errors,
            "answer": {"chars": len(answer), "titles_named": {t: (t in answer) for _, t in SEEDED},
                       "count_3_stated": any(word in answer.lower() for word in ("3 request", "three request",
                                                                                 "3 open", "three open", "3개",
                                                                                 "**3**", "3 total", ": 3"))},
            "key_in_rpc_stream": key_in_stream, "returncode": process.returncode}


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


def run_probe(omp: str, *, timeout: float, turn_timeout: float, model: str | None, stage1_only: bool,
              keep: bool) -> dict[str, Any]:
    started = time.monotonic()
    omp_tmp = Path("/tmp") / f"omp-workbench-{os.geteuid()}"
    omp_tmp_before = sorted(os.listdir(omp_tmp)) if omp_tmp.is_dir() else []
    root = Path(tempfile.mkdtemp(prefix="wbx-b4-")).resolve()
    runtime = Path(tempfile.mkdtemp(prefix="r", dir="/tmp"))
    os.chmod(runtime, 0o700)
    secret: list[str] = []
    report: dict[str, Any] = {"_secret": secret, "omp": omp, "omp_version": launcher.omp_version(omp),
                              "root": str(root), "runtime": str(runtime), "scenarios": {},
                              "started_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    case = _Case()
    try:
        home = root / "home"
        home.mkdir()
        user_store = omp_home.user_auth_store(os.environ)
        state = omp_home.auth_store_state(user_store)
        report["user_auth_store"] = {"state": state.detail, "ok": state.ok, "path": str(user_store)}
        base = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8",
                "XDG_RUNTIME_DIR": str(runtime), "WORKBENCH_HUB_AUTOSTART": "0",
                "PI_CODING_AGENT_DIR": str(user_store.parent),  # locates the user's store only (USER_LOCATION_KEYS)
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        board = root / "hub-state" / "board-agent"
        board.mkdir(parents=True, mode=0o700)
        empty = board / "empty"
        empty.mkdir(mode=0o700)
        skills = copy_skill(board / "skills")
        home_obj = omp_home.prepare_omp_home(board, base, skills_dir=skills,
                                             provider_ids=launcher.ISOLATION_PROVIDER_IDS, agents_dir=empty)
        report["board_home"] = {"root": str(home_obj.root), "agent_dir": str(home_obj.agent_dir),
                                "config_dir": home_obj.config_dir, "linked": home_obj.linked,
                                "notes": list(home_obj.notes), "environment": home_obj.environment()}
        report["decoy_mcp"] = plant_decoys(home, home_obj.agent_dir)
        stale = write(home_obj.agent_dir / "mcp.json", json.dumps({"mcpServers": {"workbench-hub": {
            "type": "http", "url": CLOSED + "/mcp", "headers": {"Authorization": "Bearer ${WB_BOARD_MCP_KEY}"}}}}) + "\n")
        report["stale_mcp_json"] = str(stale)
        overlay = write(board / "omp-board.yml", json.dumps(board_overlay(skills), indent=2) + "\n")
        report["overlay"] = board_overlay(skills)
        report["skill_dir_listing"] = sorted(p.name for p in (skills / SKILL).iterdir())
        env = omp_home.home_environment(base, home_obj.environment())
        check_env = {**env, **omp_home.withheld_user_values(base)}
        argv = board_argv(omp, overlay, model=model)

        report["scenarios"]["board_skill"] = run_check(argv, cwd=home, env=check_env, timeout=timeout)
        # negative control: the same overlay, the skill dir empty (what skill_missing must see)
        empty_overlay = write(board / "omp-board-empty.yml", json.dumps(board_overlay(empty), indent=2) + "\n")
        report["scenarios"]["control_no_skill"] = run_check(board_argv(omp, empty_overlay, model=model), cwd=home,
                                                            env=check_env, timeout=timeout)

        # get_login_providers is optional in the check and sometimes unanswered in time: unknown -> try the turn.
        seen = [s["omp_home"].get("authenticated_providers") for s in report["scenarios"].values()]
        authenticated = sorted({p for item in seen if item for p in item})
        provider = (model or "").split("/", 1)[0]
        report["login"] = {"model": model, "provider": provider, "authenticated_providers": authenticated,
                           "usable": bool(provider) and (provider in authenticated or not authenticated)}
        if not stage1_only and report["login"]["usable"] and report["scenarios"]["board_skill"]["state"] == "ok":
            records: list[dict[str, Any]] = []
            holder: list[str] = []
            hub = start_hub(case, records, holder)
            issued = hub.keys.rotate_system_key(now=hub.now[0])
            holder.append(issued.raw)
            secret.append(issued.raw)
            ids = seed(hub)
            records.clear()  # the seeding requests are not the agent's
            before = hub.key_row(issued.raw)[0]
            turn_env = {**env, "WB_HUB_URL": f"http://127.0.0.1:{hub.port}", "WB_API_KEY": issued.raw}
            turn = model_turn(argv, cwd=home, env=turn_env, key=issued.raw, timeout=turn_timeout)
            time.sleep(0.6)  # last_used_at flush interval 0.2 s
            after = hub.key_row(issued.raw)[0]
            turn["hub_requests"] = list(records)
            turn["system_key_last_used"] = {"before": before, "after": after}
            turn["seeded"] = len(ids)
            turn["hub_url"] = "http://127.0.0.1:<port>"
            report["scenarios"]["model_turn"] = turn
        elif not stage1_only:
            report["model_turn_skipped"] = "login not usable or stage 1 not ok"
        report["leftover_processes"] = owned_processes(root)
        natives = home / ".omp" / "natives"
        report["natives"] = {"bytes": tree_size(natives) if natives.is_dir() else 0,
                             "entries": sorted(os.listdir(natives)) if natives.is_dir() else []}
    finally:
        case.cleanup()
        for _ in range(50):
            if not owned_processes(root):
                break
            time.sleep(0.1)
        report["leftover_processes_final"] = owned_processes(root)
        if keep:
            report["kept"] = True
        elif not report["leftover_processes_final"]:
            shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(runtime, ignore_errors=True)
        report["root_removed"] = not root.exists()
        after_tmp = sorted(os.listdir(omp_tmp)) if omp_tmp.is_dir() else []
        report["omp_tmp"] = {"dir": str(omp_tmp), "before": len(omp_tmp_before), "after": len(after_tmp),
                             "new": sorted(set(after_tmp) - set(omp_tmp_before))}
        report["duration"] = round(time.monotonic() - started, 1)
    return report


# -- verdict & report --------------------------------------------------------------------------------------------------
def judge(report: dict[str, Any], stage1_only: bool) -> dict[str, Any]:
    s = report["scenarios"]
    board, control = s["board_skill"], s["control_no_skill"]
    checks: dict[str, bool] = {
        "stage1 board_skill: isolation check ok, 0 leaks": board["state"] == "ok" and not board["leaks"],
        "stage1 board_skill: skills == {wb-inbox}": board["skills"] == [SKILL],
        "stage1 board_skill: no MCP tool": not board["mcp_tools"],
        "stage1 board_skill: bash tool present": "bash" in (board["tools"] or []),
        "stage1 board_skill: no decoy skill / prompt marker": not board["decoy_skills_in_prompt"]
        and not board["decoy_markers_in_prompt"],
        "stage1 control_no_skill: no skill visible (skill_missing detectable)": control["skills"] == []
        and control["state"] == "ok",
        "processes: none left": not report.get("leftover_processes_final"),
        "omp tmp dir: unchanged": not report["omp_tmp"]["new"],
    }
    inconclusive = []
    if stage1_only:
        inconclusive.append("stage 2 not run (--stage1-only)")
    elif not report["login"]["usable"]:
        inconclusive.append(f"login not usable for provider {report['login']['provider']!r}")
    elif "model_turn" not in s:
        inconclusive.append(report.get("model_turn_skipped", "model turn not run"))
    elif "No API key found" in ((s["model_turn"].get("prompt_response") or {}).get("error") or ""):
        inconclusive.append("login unavailable: OMP refused the prompt (" +
                            s["model_turn"]["prompt_response"]["error"].splitlines()[0] + ")")
    else:
        turn = s["model_turn"]
        if not turn["agent_end"]:
            inconclusive.append("model turn did not reach agent_end (timeout or exit)")
        if turn["assistant_errors"] and not turn["tool_calls"]:
            inconclusive.append("the model call failed before any tool call (login/provider): "
                                + "; ".join(turn["assistant_errors"]))
        bash = [c for c in turn["tool_calls"] if c.get("tool") == "bash"]
        hub = turn["hub_requests"]
        listed = [r for r in hub if r["method"] == "GET" and r["path"] == "/api/v1/requests"]
        checks.update({
            "stage2: the prompt was accepted": bool((turn["prompt_response"] or {}).get("success")),
            "stage2: model ran wb_inbox.py through bash": any("wb_inbox.py" in c.get("command", "") for c in bash),
            "stage2: hub served GET /api/v1/requests 200 with the system key": any(
                r["status"] == 200 and r["authorization"] == "Bearer <system key>" for r in listed),
            "stage2: every hub request carried the system key": bool(hub) and all(
                r["authorization"] == "Bearer <system key>" for r in hub),
            "stage2: system key last_used_at recorded": turn["system_key_last_used"]["before"] is None
            and turn["system_key_last_used"]["after"] is not None,
            "stage2: answer names every seeded title": all(turn["answer"]["titles_named"].values()),
            "stage2: no MCP tool called": not any(str(c.get("tool", "")).startswith("mcp__")
                                                  for c in turn["tool_calls"]),
            "stage2: key not in RPC stream": not turn["key_in_rpc_stream"],
        })
    if not all(checks.values()):
        verdict = "failed"
    elif inconclusive:
        verdict = "inconclusive"
    else:
        verdict = "passed"
    return {"verdict": verdict, "checks": checks, "inconclusive": inconclusive}


def scrub(report: dict[str, Any]) -> dict[str, Any]:
    secret = report.pop("_secret")
    text = json.dumps(report, ensure_ascii=False)
    for value in secret:
        if value in text:
            raise SystemExit("probe error: the system key reached the observation record")
    for path, name in ((report["root"], "<root>"), (report["runtime"], "<runtime>"), (str(REPO), "<repo>"),
                       (str(Path.home()), "<user home>")):
        text = text.replace(path, name)
    return json.loads(text)


def _fence(value: Any) -> str:
    return "```json\n" + json.dumps(value, indent=2, ensure_ascii=False) + "\n```\n"


def render(report: dict[str, Any], notes: str) -> str:
    j = report["judgement"]
    s = report["scenarios"]
    out = ["# PROBE-BOARD-SKILL: Board overlay 아래 `wb-inbox` skill probe(WBX-B4, WBX-O02)\n",
           f"판정: **{j['verdict']}**. OMP `{report['omp_version']}`, 모델 selector `{report['login']['model']}`, "
           f"실행 {report['started_at']}, {report['duration']} s. 이 파일의 표·관측 절은 "
           "`tests/hub/probes/board_skill_probe.py --report …`가 생성한다.\n"]
    if j["inconclusive"]:
        out.append("Inconclusive 사유: " + "; ".join(j["inconclusive"]) + "\n")
    out += ["## 판정 기준과 결과\n", "| 확인 | 결과 |", "|---|---|"]
    out += [f"| {name} | {'ok' if ok else 'FAIL'} |" for name, ok in j["checks"].items()]
    out += ["", notes, "## 환경\n",
            _fence({"omp": report["omp"], "omp_version": report["omp_version"], "login": report["login"],
                    "user_auth_store": report["user_auth_store"], "board_home": report["board_home"],
                    "skill_dir_listing": report["skill_dir_listing"], "stale_mcp_json": report["stale_mcp_json"],
                    "decoy_mcp": report["decoy_mcp"], "decoy_skills": list(DECOY_SKILLS)}),
            "Overlay(두 번째 `--config`):\n", _fence(report["overlay"]),
            "## 관측\n"]
    for name, res in s.items():
        out.append(f"### {name}\n")
        out.append(_fence(res))
    out += ["## 정리\n",
            _fence({"leftover_processes_final": report["leftover_processes_final"],
                    "root_removed": report["root_removed"], "omp_tmp": report["omp_tmp"],
                    "natives": report.get("natives")})]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, help="write the Markdown result record here")
    parser.add_argument("--notes", type=Path, help="Markdown inserted after the verdict table (interpretation)")
    parser.add_argument("--json", type=Path, help="also write the raw observation JSON here")
    parser.add_argument("--omp", default=shutil.which("omp") or os.path.expanduser("~/.local/bin/omp"))
    parser.add_argument("--model", default=None, help="model selector (default: the manager default, as the Board)")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--turn-timeout", type=float, default=300.0)
    parser.add_argument("--stage1-only", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep the temp root (debugging)")
    parser.add_argument("--from-json", type=Path, help="re-render a saved --json observation (no OMP, no model)")
    args = parser.parse_args(argv)
    if args.from_json:
        report = json.loads(args.from_json.read_text())
        report["judgement"] = judge(report, False)
        if args.report:
            args.report.write_text(render(report, args.notes.read_text() if args.notes else ""))
        print(report["judgement"]["verdict"])
        return 0
    if not os.access(args.omp, os.X_OK):
        print(f"omp not found: {args.omp}", file=sys.stderr)
        return 2
    model = args.model or launcher.load_model_table()["manager"]["models"]["default"]
    report = scrub(run_probe(args.omp, timeout=args.timeout, turn_timeout=args.turn_timeout, model=model,
                             stage1_only=args.stage1_only, keep=args.keep))
    report["judgement"] = judge(report, args.stage1_only)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    if args.report:
        notes = args.notes.read_text() if args.notes else ""
        args.report.write_text(render(report, notes))
    print(json.dumps({"verdict": report["judgement"]["verdict"], "checks": report["judgement"]["checks"],
                      "inconclusive": report["judgement"]["inconclusive"], "omp_version": report["omp_version"],
                      "duration": report["duration"], "omp_tmp": report["omp_tmp"],
                      "root_removed": report["root_removed"]}, indent=2))
    return 0 if report["judgement"]["verdict"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
