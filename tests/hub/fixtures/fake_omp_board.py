"""A fake ``omp`` for TI-16 Board agent tests (no model, no provider, no network).

Every start appends one JSON line to ``$FAKE_BOARD_RECORD``: mode (``rpc`` for the isolation check, ``tty`` for the
Board agent itself), argv, pid/ppid/session id, cwd, the Board-relevant environment (the system key included, so a
test can verify it against the Hub key store), and the content of the overlay (second ``--config``) and of
``$PI_CODING_AGENT_DIR/mcp.json``.

``--mode rpc``: answers ``get_state`` / ``get_available_commands`` / ``get_login_providers`` like OMP 18.12 does for
``launcher.check_isolation``. The system prompt lists the MCP tools named by ``$FAKE_BOARD_TOOLS`` (JSON list, default
one Hub tool) as ``xd://`` discovery lines, plus ``$FAKE_BOARD_PROMPT_EXTRA`` verbatim.

TTY mode: prints ``fake-board-agent ready`` and echoes input lines as ``agent> <line>``. Lines: ``exit <n>`` exits
with that status; ``session`` writes a session file under the agent dir (as a model turn would); ``bg`` starts a
background child in the same session that ignores SIGHUP/SIGTERM (its pid goes to the record as ``bg``);
``flood <n>`` writes ``n`` bytes of output. ``$FAKE_BOARD_RPC_SLEEP`` delays the RPC answers (seconds).
"""
import json
import os
import signal
import subprocess
import sys
import time

argv = sys.argv[1:]
if argv[:1] == ["--version"]:
    print("omp/18.12.6")
    sys.exit(0)

RECORD = os.environ.get("FAKE_BOARD_RECORD")
ENV_KEYS = ("HOME", "PI_CONFIG_DIR", "PI_CODING_AGENT_DIR", "BUN_RUNTIME_TRANSPILER_CACHE_PATH", "TERM",
            "WB_BOARD_MCP_KEY", "WORKBENCH_DATA_DIR", "PATH")


def record(**extra):
    if not RECORD:
        return
    overlay = None
    configs = [argv[i + 1] for i, item in enumerate(argv[:-1]) if item == "--config"]
    if len(configs) >= 2:
        try:
            overlay = open(configs[1]).read()
        except OSError:
            overlay = None
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
    try:
        mcp_json = open(os.path.join(agent_dir, "mcp.json")).read() if agent_dir else None
    except OSError:
        mcp_json = None
    row = {"argv": argv, "pid": os.getpid(), "ppid": os.getppid(), "sid": os.getsid(0), "pgid": os.getpgrp(),
           "cwd": os.getcwd(), "env": {key: os.environ.get(key) for key in ENV_KEYS},
           "overlay": overlay, "mcp_json": mcp_json, "isatty": os.isatty(0), **extra}
    with open(RECORD, "a") as stream:
        stream.write(json.dumps(row) + "\n")


def rpc():
    record(mode="rpc")
    tools = json.loads(os.environ.get("FAKE_BOARD_TOOLS") or '["mcp__workbench_hub_inbox_list"]')
    lines = [f'- "{name}" -> `xd://{name}` - fake tool' for name in tools]
    prompt = "You are omp's coding agent.\n" + "\n".join(lines) + "\n" + os.environ.get("FAKE_BOARD_PROMPT_EXTRA", "")
    delay = float(os.environ.get("FAKE_BOARD_RPC_SLEEP") or 0)
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        time.sleep(delay)
        kind = request.get("type")
        if kind == "get_state":
            data = {"systemPrompt": prompt, "dumpTools": [{"name": "read"}, {"name": "bash"}],
                    "model": {"provider": "anthropic", "id": "fake"}}
        elif kind == "get_available_commands":
            data = {"commands": []}
        elif kind == "get_login_providers":
            data = {"providers": [{"id": "anthropic", "authenticated": True}]}
        else:
            data = {}
        sys.stdout.write(json.dumps({"type": "response", "id": request.get("id"), "command": kind,
                                     "success": True, "data": data}) + "\n")
        sys.stdout.flush()


def tty():
    record(mode="tty")
    out = sys.stdout.buffer
    out.write(b"fake-board-agent ready\r\n")
    out.flush()
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        out.write(f"agent> {line}\r\n".encode())
        out.flush()
        if line.startswith("exit "):
            sys.exit(int(line.split()[1]))
        elif line == "session":
            directory = os.path.join(os.environ["PI_CODING_AGENT_DIR"], "sessions", "-home")
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, f"{os.getpid()}.jsonl"), "w") as stream:
                stream.write("{}\n")
        elif line == "bg":
            child = subprocess.Popen([sys.executable, "-c",
                                      "import signal,time\n"
                                      "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
                                      "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                                      "time.sleep(600)\n"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, process_group=0)
            record(mode="bg", bg=child.pid)
        elif line.startswith("flood "):
            out.write(b"x" * int(line.split()[1]) + b"\r\n")
            out.flush()


if "--mode" in argv and argv[argv.index("--mode") + 1] == "rpc":
    rpc()
else:
    signal.signal(signal.SIGHUP, signal.SIG_DFL)
    tty()
