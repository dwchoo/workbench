"""manager-worker-roles MWR-D06 probe stub: does a restarted worker OMP keep using the Workbench `terminal` tool?

Manual run only (not part of any unittest suite; nothing here starts OMP or calls a model), from the repo root::

    env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python tests/backend/probes/live_worker_restart_role_probe.py

Stage 1 (implemented, offline): print what the launcher puts in each role's argv -- whether the always-on role prompt
is in ``--append-system-prompt``, its length, and the worker ``--tools`` list -- as JSON.

Stage 2 (``--live``; real model, a few turns; user-approved 2026-10-29 for Q68): a real backend (``wb start``) for a
throwaway project in a sandbox (temp HOME/XDG/TMPDIR, short private ``XDG_RUNTIME_DIR``, ``WORKBENCH_HUB_AUTOSTART=0``).
The Workbench OMP home links ``agent.db`` to the user's own store as the product does (located through
``PI_CODING_AGENT_DIR``; never opened, copied or modified here: ``stat`` only before/after). Steps:
  1. Login query (RPC ``get_login_providers``) before any other OMP start: the baseline provider list.
  2. ``wb start``; record the role argv (``/proc/<pid>/cmdline``) and the providers each isolation check saw.
  3. Ctrl+D into the worker OMP pane, wait for its exit, ``restart_pane``; the new argv must equal the old one.
  4. With no Task and no manager message, ask the restarted worker to run ``ls -la`` in the project. Observe the tool
     calls in its OMP session file (names, arguments) until the turn ends.
  5. One work order to a fresh manager; observe its tool calls until it delegates (``to_worker``), ends its turn or
     a time limit, then Escape (interrupt).
  6. Escape + Ctrl+D both OMP panes (graceful), ``wb shutdown --yes``, login query again. Every provider list is
     compared with the baseline: a shrink stops the probe at once. Kept in ``--out`` (default ``/tmp/mwr-probe``):
     redacted session files, OMP logs, the manager's ``.wb/plans``/``.wb/scripts``, ``report.json``. The sandbox and
     the runtime dir are removed; ``/tmp/omp-workbench-<uid>`` must hold the same entries before and after.

    env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python tests/backend/probes/live_worker_restart_role_probe.py --live
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from workbench.backend import launcher, omp_home
from workbench.backend.client import UiClient
from workbench.contracts.ui_v1 import ClientType
from workbench.contracts.v1 import PaneId

PLAN = launcher.LaunchPlan(None, "omp", "omp/probe", "bridge.ts")  # type: ignore[arg-type]


def stage1() -> dict:
    report = {}
    for role, rules in (("manager", launcher.MANAGER_ROLE_PROMPT), ("worker", launcher.WORKER_ROLE_PROMPT)):
        argv = launcher.omp_command(PLAN, Path(f"omp-isolation-{role}.yml"))
        prompt = argv[argv.index("--append-system-prompt") + 1]
        report[role] = {"role_prompt_in_argv": prompt.startswith(rules), "role_prompt_chars": len(rules),
                        "append_prompt_chars": len(prompt),
                        "tools": argv[argv.index("--tools") + 1].split(",") if "--tools" in argv else None}
    return report


# -- stage 2 (live) ----------------------------------------------------------------------------------------------------
REPO = Path(__file__).resolve().parents[3]
OUT_DEFAULT = Path("/tmp/mwr-probe")
WORKER_PROMPT = "Run `ls -la` in the project directory and tell me what's there."
MANAGER_PROMPT = ("Work order: in this project, (1) count the lines of each .txt file under data/, (2) find the "
                  "longest file, (3) write the counts and the longest file name to report.md. Plan it and get it "
                  "done with the worker.")
ESC, CTRL_D, ENTER = b"\x1b", b"\x04", b"\r"


def _probe_helpers():
    sys.path.insert(0, str(REPO / "tests" / "hub" / "probes"))
    import board_skill_probe as board  # noqa: PLC0415 - store_stat, login_query, Recorder (WBX-B12 stop)
    return board


def _cmdline(pid: int) -> list[str]:
    return Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").decode().split("\0")


class Sandbox:
    def __init__(self, omp: str, out: Path):
        self.board = _probe_helpers()
        self.omp, self.out = omp, out
        self.root = Path(tempfile.mkdtemp(prefix="mwr-", dir="/tmp")).resolve()
        self.runtime = Path(tempfile.mkdtemp(prefix="r", dir="/tmp"))
        os.chmod(self.runtime, 0o700)
        self.home, self.proj = self.root / "home", self.root / "proj"
        self.data = self.proj / ".wb"
        for path in (self.home, self.proj / "data", self.root / "tmp"):
            path.mkdir(parents=True)
        (self.proj / "README.md").write_text("MWR probe throwaway project.\n")
        for name, lines in (("a.txt", 3), ("b.txt", 7), ("c.txt", 5)):
            (self.proj / "data" / name).write_text("".join(f"line {i}\n" for i in range(lines)))
        self.store = omp_home.user_auth_store(os.environ)
        xdg = self.root / "xdg"
        self.env = {"PATH": f"{Path.home() / '.local' / 'bin'}:/usr/local/bin:/usr/bin:/bin", "HOME": str(self.home),
                    "LANG": "C.UTF-8", "TERM": "xterm-256color", "SHELL": "/usr/bin/bash",
                    "XDG_RUNTIME_DIR": str(self.runtime), "XDG_CONFIG_HOME": str(xdg / "config"),
                    "XDG_STATE_HOME": str(xdg / "state"), "XDG_DATA_HOME": str(xdg / "data"),
                    "XDG_CACHE_HOME": str(xdg / "cache"), "TMPDIR": str(self.root / "tmp"),
                    "WORKBENCH_HUB_AUTOSTART": "0", "PI_CODING_AGENT_DIR": str(self.store.parent),
                    "PYTHONPATH": str(REPO / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        self.rec = self.board.Recorder(out, omp, self.store)
        self.report: dict = {"omp": omp, "omp_version": launcher.omp_version(omp), "root": str(self.root),
                             "store_before": self.board.store_stat(self.store), "starts": self.rec.starts}
        self.seen: dict[int, str] = {}

    # backend ----------------------------------------------------------------------------------------------------------
    def cli(self, *args: str, timeout: float = 180) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "workbench", *args], env=self.env, cwd=self.proj,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)

    def status(self) -> dict | None:
        out = self.cli("status", "--data-dir", str(self.data), "--json", timeout=30)
        try:
            body = json.loads(out.stdout)
        except ValueError:
            return None
        return body.get("snapshot") if body.get("running") else None

    def remember(self) -> None:
        self.seen.update({pid: t for pid in self.board.owned_processes(self.root) if (t := self.board.ticks(pid))})

    def wait(self, predicate, what: str, timeout: float = 120.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.remember()
            snap = self.status()
            if snap and predicate(snap):
                return snap
            time.sleep(0.5)
        raise RuntimeError(f"timeout waiting for {what}")

    def ui(self, *actions: tuple[str, PaneId, bytes]) -> list[dict]:
        results = []
        with UiClient(self.data / "ui.sock", name="mwr-probe", timeout=30) as client:
            client.attach((40, 160))
            for kind, pane, data in actions:
                reply = client.paste(pane, data) if kind == "paste" else client.input(pane, data)
                results.append({k: v for k, v in reply.items() if k in ("ok", "reason")})
                time.sleep(0.3)
        return results

    def say(self, pane: PaneId, text: str) -> list[dict]:
        return self.ui(("paste", pane, text.encode()), ("input", pane, ENTER))

    def check_providers(self, snap: dict, label: str) -> None:
        roles = (snap.get("omp_isolation") or {}).get("roles") or {}
        for role, value in roles.items():
            providers = ((value.get("observed") or {}).get("omp_home") or {}).get("authenticated_providers")
            self.rec.after(f"{label}-{role}", providers, None)

    def login(self, label: str) -> None:
        login_root = self.root / f"login-{label}"
        login_root.mkdir()
        home = omp_home.prepare_omp_home(login_root, self.env, skills_dir=login_root,
                                         provider_ids=launcher.ISOLATION_PROVIDER_IDS, agents_dir=login_root)
        env = omp_home.home_environment(self.env, home.environment())
        argv = [self.rec.wrapper(label), "--config", str(launcher.default_isolation_overlay()), "--no-extensions",
                "--append-system-prompt", "", "--no-title"]
        self.rec.log_dirs = [home.root / "logs"]
        result = self.board.login_query(argv, cwd=login_root, env=env, timeout=60)
        if self.rec.baseline is None:
            self.rec.baseline = result["providers"]
        self.rec.after(label, result["providers"], result["cleanup"])

    # sessions ---------------------------------------------------------------------------------------------------------
    def session_files(self) -> list[Path]:
        return sorted((omp_home.omp_root(self.data) / "agent" / "sessions").rglob("*.jsonl"))

    @staticmethod
    def turn(path: Path, marker: str) -> dict:
        """Tool calls and texts after the user message containing ``marker``; ``ended`` once the model stopped."""
        calls, results, texts, started, ended = [], [], [], False, False
        for line in path.read_text(errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") if isinstance(entry, dict) else None
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            parts = content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
            parts = [p for p in parts if isinstance(p, dict)]
            role = message.get("role")
            if role == "user" and any(marker in str(p.get("text", "")) for p in parts):
                started, ended = True, False
                continue
            if not started:
                continue
            if role == "assistant":
                for part in parts:
                    if part.get("type") == "toolCall":
                        calls.append({"name": part.get("name"), "arguments": part.get("arguments")})
                    elif part.get("type") == "text" and part.get("text"):
                        texts.append(part["text"])
                ended = message.get("stopReason") in ("stop", "error", "aborted", "length")
                if message.get("errorMessage"):
                    texts.append("ERROR: " + str(message["errorMessage"])[:300])
            elif role == "toolResult":
                text = "".join(str(p.get("text", "")) for p in parts)
                results.append({"name": message.get("toolName"), "isError": message.get("isError"),
                                "text": text[:600]})
        return {"started": started, "ended": ended, "tool_calls": calls, "tool_results": results, "texts": texts}

    def await_turn(self, marker: str, timeout: float, stop_on=lambda turn: False) -> dict:
        deadline, turn = time.monotonic() + timeout, {"started": False}
        while time.monotonic() < deadline:
            self.remember()
            for path in self.session_files():
                candidate = self.turn(path, marker)
                if candidate["started"]:
                    turn = {**candidate, "session": path.name}
            if turn.get("started") and (turn["ended"] or stop_on(turn)):
                return {**turn, "timed_out": False}
            time.sleep(1.0)
        return {**turn, "timed_out": True}

    # run --------------------------------------------------------------------------------------------------------------
    def run(self) -> dict:
        r = self.report
        self.login("login-start")
        out = self.cli("start", "--data-dir", str(self.data), "--omp", self.omp, "--no-attach")
        r["start"] = {"exit": out.returncode, "stdout": out.stdout[-800:], "stderr": out.stderr[-800:]}
        if out.returncode:
            raise RuntimeError(f"wb start failed: {out.stdout[-400:]} {out.stderr[-400:]}")
        snap = self.wait(lambda s: s["phase"] == "ready" and s["omp_isolation"].get("checked"), "ready", 180)
        r["isolation_start"] = {k: snap["omp_isolation"].get(k) for k in ("state", "leaks", "warnings")}
        self.check_providers(snap, "backend-check")
        manager, worker = (snap["panes"][p]["process"] for p in ("manager_omp", "worker_omp"))
        argv_before = _cmdline(worker["pid"])
        manager_argv = _cmdline(manager["pid"])

        def prompt_of(argv):
            return argv[argv.index("--append-system-prompt") + 1]
        r["argv"] = {"worker_has_worker_role": prompt_of(argv_before).startswith(launcher.WORKER_ROLE_PROMPT),
                     "worker_has_manager_role": launcher.MANAGER_ROLE_PROMPT in prompt_of(argv_before),
                     "manager_has_manager_role": prompt_of(manager_argv).startswith(launcher.MANAGER_ROLE_PROMPT),
                     "manager_has_worker_role": launcher.WORKER_ROLE_PROMPT in prompt_of(manager_argv),
                     "worker_tools": argv_before[argv_before.index("--tools") + 1]}
        time.sleep(3)
        r["ctrl_d"] = self.ui(("input", PaneId.WORKER_OMP, CTRL_D))
        try:
            self.wait(lambda s: not s["panes"]["worker_omp"]["alive"], "worker exit", 20)
        except RuntimeError:
            r["ctrl_d_second"] = self.ui(("input", PaneId.WORKER_OMP, CTRL_D))
            self.wait(lambda s: not s["panes"]["worker_omp"]["alive"], "worker exit", 30)
        with UiClient(self.data / "ui.sock", name="mwr-probe", timeout=30) as client:
            client.attach((40, 160))
            result = client.request(ClientType.RESTART_PANE, pane="worker_omp", timeout=30)
        r["restart"] = {k: result.get(k) for k in ("ok", "result", "reason")}
        snap = self.wait(lambda s: s["panes"]["worker_omp"]["alive"] and s["phase"] == "ready"
                         and "rechecking" not in s["omp_isolation"] and s["omp_isolation"].get("checked")
                         and s["panes"]["worker_omp"]["process"]["pid"] != worker["pid"], "restart", 180)
        r["isolation_restart"] = {k: snap["omp_isolation"].get(k) for k in ("state", "leaks", "warnings")}
        self.check_providers(snap, "restart-check")
        argv_after = _cmdline(snap["panes"]["worker_omp"]["process"]["pid"])
        r["argv"]["restart_identical"] = argv_after == argv_before
        time.sleep(4)
        r["worker_input"] = self.say(PaneId.WORKER_OMP, WORKER_PROMPT)
        r["worker_turn"] = self.await_turn("Run `ls -la` in the project", 240)
        r["manager_input"] = self.say(PaneId.MANAGER_OMP, MANAGER_PROMPT)
        r["manager_turn"] = self.await_turn(
            "Work order: in this project", 180,
            stop_on=lambda t: any(c["name"] == "to_worker" for c in t["tool_calls"]) or len(t["tool_calls"]) >= 12)
        r["stop_inputs"] = self.ui(("input", PaneId.MANAGER_OMP, ESC), ("input", PaneId.WORKER_OMP, ESC))
        time.sleep(5)
        r["manager_turn_final"] = self.await_turn("Work order: in this project", 1)
        return r

    def finish(self) -> None:
        r = self.report
        try:
            if self.status():
                try:
                    r["exit_inputs"] = self.ui(("input", PaneId.MANAGER_OMP, ESC), ("input", PaneId.WORKER_OMP, ESC),
                                               ("input", PaneId.MANAGER_OMP, CTRL_D),
                                               ("input", PaneId.WORKER_OMP, CTRL_D))
                    self.wait(lambda s: not s["panes"]["worker_omp"]["alive"]
                              and not s["panes"]["manager_omp"]["alive"], "OMP exit", 30)
                    r["omp_graceful_exit"] = True
                except Exception as exc:  # noqa: BLE001
                    r["omp_graceful_exit"] = repr(exc)
                out = self.cli("shutdown", "--data-dir", str(self.data), "--yes", timeout=120)
                r["shutdown"] = {"exit": out.returncode, "tail": (out.stdout + out.stderr)[-300:]}
        finally:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and any(self.board.ticks(p) == t for p, t in self.seen.items()):
                time.sleep(0.2)
            r["survivors"] = [p for p, t in self.seen.items() if self.board.ticks(p) == t]
            for pid in r["survivors"]:
                try:
                    if self.board.ticks(pid) == self.seen[pid]:
                        os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            self.keep()

    def keep(self) -> None:
        redact = self.board.redact_log
        for path in self.session_files():
            target = self.out / "sessions" / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(redact(path.read_bytes()))
        logs = omp_home.omp_root(self.data) / "logs"
        if logs.is_dir():
            for item in logs.rglob("*"):
                if item.is_file() and not item.is_symlink():
                    target = self.out / "logs" / "backend-omp" / item.relative_to(logs)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(redact(item.read_bytes()))
        for sub in ("plans", "scripts"):
            if (self.data / sub).is_dir():
                shutil.copytree(self.data / sub, self.out / "project-wb" / sub, dirs_exist_ok=True)
        if (self.proj / "report.md").is_file():
            shutil.copyfile(self.proj / "report.md", self.out / "project-report.md")


def stage2(omp: str, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    omp_tmp = Path("/tmp") / f"omp-workbench-{os.geteuid()}"
    before = sorted(os.listdir(omp_tmp)) if omp_tmp.is_dir() else []
    box = Sandbox(omp, out)
    report = box.report
    try:
        box.run()
    except Exception as exc:  # noqa: BLE001 - ProbeStop included: everything is kept
        report["stopped"] = repr(exc)
    finally:
        box.finish()
        if not report.get("stopped", "").startswith("ProbeStop"):
            try:
                box.login("login-end")
            except Exception as exc:  # noqa: BLE001
                report["login_end_error"] = repr(exc)
        report["store_after"] = box.board.store_stat(box.store)
        leftover = box.board.owned_processes(box.root)
        report["leftover_processes"] = leftover
        if not leftover:
            shutil.rmtree(box.root, ignore_errors=True)
            shutil.rmtree(box.runtime, ignore_errors=True)
        report["root_removed"] = not box.root.exists()
        after = sorted(os.listdir(omp_tmp)) if omp_tmp.is_dir() else []
        report["omp_tmp"] = {"before": len(before), "after": len(after), "new": sorted(set(after) - set(before))}
        (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n")
    return report


if __name__ == "__main__":
    if "--live" in sys.argv[1:]:
        result = stage2(shutil.which("omp") or os.path.expanduser("~/.local/bin/omp"), OUT_DEFAULT)
        print(json.dumps({k: result.get(k) for k in ("stopped", "argv", "restart", "omp_tmp", "root_removed",
                                                    "survivors")}, indent=2, default=str))
    else:
        print(json.dumps(stage1(), indent=2))
