"""Observed OMP identity, without borrowing historical values as current facts."""
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

from workbench.backend.launcher import (LaunchPlan, choose_shell, default_isolation_overlay, load_model_table,
    omp_command, omp_environment, role_overlay, selector_model, selector_level, write_role_overlay)
from workbench.backend.omp_home import config_dir_value
from workbench.backend.summary_adapter import _resolved_metadata


ASSET_SELECTORS = ("PI_PACKAGE_DIR", "OMP_NATIVE_LIBRARY_PATH")


def current_omp_identity(binary=None, *, version=None, instructions=None, environment=None):
    environment = os.environ if environment is None else environment
    if version in ("unknown", "unobserved"):
        version = None
    selected = binary or shutil.which("omp", path=environment.get("PATH", os.defpath))
    digest = binary_path = None
    if selected:
        try:
            path = Path(selected).resolve(strict=True)
            binary_path = str(path)
            with path.open("rb") as stream:
                digest = file_digest(stream, "sha256").hexdigest()
        except OSError:
            pass
    return {"binary_sha256": digest, "version": version,
            "binary_path": binary_path,
            "asset_selection": {key: environment.get(key) for key in ASSET_SELECTORS},
            "bundled_sha256": sha256(instructions.encode()).hexdigest() if isinstance(instructions, str) and instructions else None,
            "unknowns": [key + "_unavailable" for key, value in
                         (("binary", digest), ("version", version), ("bundled", instructions)) if value is None]}


def compatibility_boundary():
    table = load_model_table()
    return {"route": "private-manager-task", "adoption_ref": "LG-D22",
            "model_table_digest": sha256(json.dumps(table, sort_keys=True, ensure_ascii=False,
                                                    separators=(",", ":")).encode()).hexdigest()}


def observe_configured(source: Path, *, environment=None) -> dict:
    """One credential-free public Native getter; no prompt or auth-store copy."""
    from workbench.backend import summary_compat_runtime as rt
    row = {"kind": "owned_configured_metadata", "result": "inconclusive", "checks": {},
           "metadata": None, "omp": None, "cleanup_complete": False}
    supplied = os.environ if environment is None else environment
    environment = {key: supplied[key] for key in ("PATH", *ASSET_SELECTORS) if key in supplied}
    name = shutil.which("omp", path=environment.get("PATH", os.defpath))
    if name is None:
        row["error"] = "configured_native_unavailable"
        return row
    root = Path(tempfile.mkdtemp(prefix="wb-summary-model-")).resolve()
    process = ref = None
    try:
        selected = Path(name).absolute()
        before = current_omp_identity(selected, environment=environment)
        if before["binary_sha256"] is None:
            raise ValueError("configured_binary_unavailable")
        os.chmod(root, 0o700)
        rt._private_root(root)
        for name in ("home", "agent", "config", "project", "tmp", "xdg-config", "xdg-data", "xdg-state", "xdg-cache"):
            (root / name).mkdir(mode=0o700)
        runtime = rt._copy_runtime(source, root)
        env = rt._environment(root)
        env.update({key: value for key, value in before["asset_selection"].items() if value is not None})
        binary = Path(before["binary_path"])
        table = load_model_table(runtime / "omp_bridge/omp-models.yml")
        manager, worker = table["manager"]["models"]["default"], table["worker"]["models"]["default"]
        plan = LaunchPlan(choose_shell(env), str(binary), "unobserved", str(runtime / "omp_bridge/g3/summary_verification.ts"),
                          ("--mode", "rpc", "--no-session", "--model", selector_model(manager)))
        env = omp_environment(env, plan, role="manager", token=uuid4().hex, bridge_socket=root / "absent.sock",
            home={"PI_CONFIG_DIR": config_dir_value(root / "config", env), "PI_CODING_AGENT_DIR": str(root / "agent")}, models=table)
        overlay = write_role_overlay(root / "config", "manager", role_overlay("manager", project_dir=root / "project",
            home=root / "home", skills_dir=runtime / "omp_bridge/skills", environment=env, models=table))
        (root / "fixture.json").write_text(json.dumps({"root": str(root), "metadataOnly": True}))
        (root / "fixture.json").chmod(0o600)
        argv = [str(runtime / "omp_bridge/omp-isolation.yml") if arg == str(default_isolation_overlay()) else arg
                for arg in omp_command(plan, overlay, role="manager")]
        process = subprocess.Popen(argv, cwd=root / "project", env=env, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        ref = {"pid": process.pid, "pgid": process.pid, "start_ticks": rt._identity(process.pid)}
        if ref["start_ticks"] is None:
            raise ValueError("configured_process_identity_unavailable")
        deadline = time.monotonic() + 50
        while not (root / "completed.json").exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise TimeoutError("configured_readback_unavailable")
            time.sleep(.02)
        completed = json.loads((root / "completed.json").read_text())
        metadata = _resolved_metadata(completed.get("metadata"))
        selection = metadata["childSelection"]
        expected = json.loads(env["WORKBENCH_G3_SUMMARY_SELECTION"])
        checks = {"public_parent_readback": metadata["source"] == "parent_session_readback",
            "configured_manager": metadata["model"]["provider"] + "/" + metadata["model"]["id"] == selector_model(manager)
                and metadata["thinkingLevel"] == selector_level(manager),
            "configured_worker_default": selection["selector"] == worker and selection["modelTableDigest"] == expected["modelTableDigest"]
                and selection["model"]["provider"] + "/" + selection["model"]["id"] == selector_model(worker)
                and selection["thinkingLevel"] == selector_level(worker)}
        after = current_omp_identity(selected, environment=environment)
        checks["binary_epoch_stable"] = all(before[key] == after[key] for key in ("binary_path", "binary_sha256"))
        identity = current_omp_identity(selected, version=completed.get("version"), environment=environment)
        identity["bundled_sha256"] = completed.get("definitionDigest")
        checks["actual_install_identity"] = (isinstance(identity["version"], str) and bool(identity["version"].strip())
            and isinstance(identity["bundled_sha256"], str) and len(identity["bundled_sha256"]) == 64
            and all(char in "0123456789abcdef" for char in identity["bundled_sha256"])
            and all(identity[key] == before[key] for key in ("binary_path", "binary_sha256", "asset_selection")))
        identity.pop("unknowns")
        row.update(metadata=metadata, checks=checks, omp=identity)
    except Exception as error:
        row["error"] = type(error).__name__
    finally:
        stopped = True
        if process is not None:
            try:
                stopped = rt._stop_leader(process, ref) is None and process.poll() is not None and not rt._remaining(process.pid)
            except Exception:
                stopped = False
            if process.stdin:
                process.stdin.close()
        if stopped:
            shutil.rmtree(root)
        row["cleanup_complete"] = stopped and not root.exists()
        if root.exists():
            row["retained_private_root"] = str(root)
    if row["checks"] and all(value is True for value in row["checks"].values()) and row["cleanup_complete"]:
        row["result"] = "pass"
    return row
