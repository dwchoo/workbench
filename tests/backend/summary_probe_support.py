"""Development-only Q01 projection and observation contracts.

The product reads the JSON policy itself. Nothing under src/workbench imports this
module or a development check record.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys


Q01_ITEMS = (
    "Q01-route", "Q01-tools", "Q01-skill", "Q01-result",
    "Q01-context", "Q01-priority", "Q01-recovery", "Q01-origin",
)
POLICY_PATH = "src/workbench/contracts/summary_compat_policy_v1.json"
OVERLAY_VERSION = 1
OVERLAY_FILES = (
    "tools/validate_feature_gate.py",
    "tools/q01_recheck.py",
    "tests/backend/summary_probe_support.py",
    "tests/backend/live_summary_feasibility.py",
    "tests/backend/test_summary_probe.py",
    "tests/backend/test_summary_gate_validator.py",
    "tests/integration/cw16_harness.py",
)
CORE_MINIMUM = {
    "runtime_roots": frozenset(("src/workbench/backend", "src/workbench/ipc",
                                "src/workbench/contracts", "omp_bridge")),
    "runtime_files": frozenset(("pyproject.toml",)),
    "python_closure_roots": frozenset(("src/workbench/tasks", "src/workbench/storage",
                                       "src/workbench/workflow")),
    "required_seams": frozenset(("src/workbench/backend/service.py",
                                 "src/workbench/backend/automation.py",
                                 "src/workbench/backend/flow_tasks.py",
                                 "src/workbench/backend/flow_terminal.py",
                                 "src/workbench/ipc/bridge_g3/mailbox.py",
                                 "omp_bridge/g3/bridge.ts")),
    "workbench_owned_assets": frozenset(("omp_bridge/agents", "omp_bridge/skills",
                                         "omp_bridge/omp-models.yml")),
    "product_runtime_roots": frozenset(("src/workbench", "omp_bridge")),
    "product_runtime_files": frozenset(("pyproject.toml",)),
    "stable_host_fields": frozenset(("os_family", "architecture", "python_minor")),
}


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_digest(value: object) -> str:
    return digest_bytes(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode() + b"\n")


def policy_at(root: Path) -> tuple[dict, str]:
    target = root / POLICY_PATH
    raw = target.read_bytes()
    policy = json.loads(raw)
    if policy.get("schema_version") != 1 or policy.get("projection_version") != 1:
        raise ValueError("unsupported canonical summary compatibility policy")
    required = set(CORE_MINIMUM) | {"route_identity_fields", "route_identity_sources",
                                     "unknown_dynamic_import", "omp_owned_assets",
                                     "include_python_package_initializers"}
    if not required <= policy.keys():
        raise ValueError("incomplete canonical summary compatibility policy")
    for field in CORE_MINIMUM:
        if not isinstance(policy[field], list) or not policy[field] or any(
                not isinstance(p, str) or not p or Path(p).is_absolute() or ".." in Path(p).parts
                for p in policy[field]):
            raise ValueError(f"invalid policy paths: {field}")
        if not CORE_MINIMUM[field] <= set(policy[field]) or len(policy[field]) != len(set(policy[field])):
            raise ValueError(f"canonical core minimum removed: {field}")
    if policy["unknown_dynamic_import"] != "invalidate" or policy["omp_owned_assets"] != "installed_executable_and_bundled_agent_tool_prompt_bytes":
        raise ValueError("canonical import or OMP ownership policy weakened")
    if policy["include_python_package_initializers"] is not True:
        raise ValueError("canonical package initializer closure disabled")
    required_route = {"route", "role", "agent_definition_sha256", "skill_sha256",
                      "resolved_model", "provider", "context_limit_tokens", "harness",
                      "selection_epoch", "source"}
    if (not isinstance(policy["route_identity_fields"], list)
            or not required_route <= set(policy["route_identity_fields"])
            or not isinstance(policy["route_identity_sources"], list)
            or not {"omp_readback", "product_adapter_readback"} <= set(policy["route_identity_sources"])):
        raise ValueError("canonical route read-back identity weakened")
    return policy, digest_bytes(raw)


def _entry(root: Path, relative: str) -> dict:
    path = root / relative
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"kind": "absent"}
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISREG(info.st_mode):
        return {"kind": "file", "mode": mode, "sha256": digest_bytes(path.read_bytes())}
    if stat.S_ISLNK(info.st_mode):
        return {"kind": "symlink", "mode": mode, "target": os.readlink(path)}
    if stat.S_ISDIR(info.st_mode):
        return {"kind": "directory", "mode": mode}
    return {"kind": "unsupported", "mode": mode}


def _walk(root: Path, relative: str, entries: dict[str, dict], unknowns: list[str]) -> None:
    target = root / relative
    entries[relative] = _entry(root, relative)
    if entries[relative]["kind"] == "absent":
        unknowns.append("missing required projection root: " + relative)
        return
    if entries[relative]["kind"] != "directory":
        unknowns.append("projection root is not a directory: " + relative)
        return
    for path in sorted(target.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        name = path.relative_to(root).as_posix()
        entries[name] = _entry(root, name)
        if entries[name]["kind"] in ("symlink", "unsupported"):
            unknowns.append("unresolved projection input: " + name)


def _module_paths(root: Path, name: str) -> tuple[str, ...]:
    if not name.startswith("workbench.") and name != "workbench":
        return ()
    base = "src/" + name.replace(".", "/")
    options = (base + ".py", base + "/__init__.py")
    present = tuple(p for p in options if (root / p).is_file())
    return present


def _static_imports(root: Path, relative: str) -> tuple[set[str], list[str]]:
    try:
        tree = ast.parse((root / relative).read_bytes(), filename=relative)
    except (OSError, SyntaxError, UnicodeError) as exc:
        return set(), [f"unreadable Python closure: {relative}: {type(exc).__name__}"]
    module = relative.removeprefix("src/").removesuffix(".py").replace("/", ".")
    package = module.removesuffix(".__init__") if relative.endswith("/__init__.py") else module.rpartition(".")[0]
    names: set[str] = set()
    unknowns: list[str] = []
    importlib_aliases = {"importlib"}
    builtins_aliases = {"builtins"}
    dynamic_names = {"__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or alias.name)
                if alias.name == "builtins":
                    builtins_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module in ("importlib", "builtins"):
            for alias in node.names:
                if (node.module == "importlib" and alias.name == "import_module") or (
                        node.module == "builtins" and alias.name == "__import__"):
                    dynamic_names.add(alias.asname or alias.name)

    def dynamic_loader(func: ast.expr) -> bool:
        return (isinstance(func, ast.Name) and func.id in dynamic_names) or (
            isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and ((func.attr == "import_module" and func.value.id in importlib_aliases)
                 or (func.attr == "__import__" and func.value.id in builtins_aliases)))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and dynamic_loader(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    dynamic_names.add(target.id)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            try:
                base = importlib.util.resolve_name("." * node.level + (node.module or ""), package) if node.level else (node.module or "")
            except ImportError:
                unknowns.append("unresolved relative import: " + relative)
                continue
            names.add(base)
            names.update(base + "." + alias.name for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.Call):
            if dynamic_loader(node.func):
                if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                    unknowns.append("unknown dynamic import: " + relative)
                elif node.args[0].value.startswith("."):
                    unknowns.append("unknown relative dynamic import: " + relative)
                else:
                    names.add(node.args[0].value)
    return names, unknowns


def _closure(root: Path, entries: dict[str, dict], unknowns: list[str]) -> None:
    pending = [name for name, item in entries.items() if name.endswith(".py") and item["kind"] == "file"
               and name.startswith("src/workbench/")]

    def include_initializers(relative: str) -> None:
        current = Path(relative)
        if not current.as_posix().startswith("src/workbench/"):
            return
        directory = current if entries.get(relative, {}).get("kind") == "directory" else current.parent
        while directory == Path("src/workbench") or Path("src/workbench") in directory.parents:
            initializer = (directory / "__init__.py").as_posix()
            if initializer not in entries:
                entries[initializer] = _entry(root, initializer)
                if entries[initializer]["kind"] == "file":
                    pending.append(initializer)
                elif entries[initializer]["kind"] in ("symlink", "unsupported"):
                    unknowns.append("unresolved package initializer: " + initializer)
            if directory == Path("src/workbench"):
                break
            directory = directory.parent

    for relative in tuple(entries):
        include_initializers(relative)
    visited: set[str] = set()
    while pending:
        relative = pending.pop()
        if relative in visited:
            continue
        visited.add(relative)
        imports, errors = _static_imports(root, relative)
        unknowns.extend(errors)
        for name in imports:
            for child in _module_paths(root, name):
                if child not in entries:
                    entries[child] = _entry(root, child)
                    pending.append(child)
                include_initializers(child)
            if name.startswith("workbench.") and not _module_paths(root, name):
                # It may be a symbol from a known module. Its parent must exist.
                parent = name.rpartition(".")[0]
                if not _module_paths(root, parent):
                    unknowns.append("unresolved Workbench import: " + relative + " -> " + name)


def collect_projection(root: Path, *, omp_executable: Path | None,
                       omp_bundle_identity: str | None = None,
                       route_identity: dict | None = None) -> dict:
    root = root.resolve()
    policy, policy_sha = policy_at(root)
    entries: dict[str, dict] = {}
    unknowns: list[str] = []
    for relative in (*policy["runtime_roots"], *policy["python_closure_roots"]):
        _walk(root, relative, entries, unknowns)
    for relative in (*policy["runtime_files"], *policy["required_seams"]):
        entries[relative] = _entry(root, relative)
        if entries[relative]["kind"] != "file":
            unknowns.append("missing required projection file: " + relative)
    _closure(root, entries, unknowns)
    overlay = {name: _entry(root, name) for name in OVERLAY_FILES}
    for name, item in overlay.items():
        if item["kind"] != "file":
            unknowns.append("missing development overlay: " + name)
    if omp_executable is None:
        unknowns.append("OMP executable unresolved")
        omp = None
    else:
        executable = omp_executable.resolve()
        if not executable.is_file():
            unknowns.append("OMP executable missing")
            omp = None
        else:
            observed = subprocess.run([str(executable), "--version"], capture_output=True,
                                      text=True, timeout=10, check=False)
            version = observed.stdout.strip() if observed.returncode == 0 else None
            if not version:
                unknowns.append("OMP version unavailable")
            omp = {"path": str(executable), "mode": stat.S_IMODE(executable.stat().st_mode),
                   "sha256": digest_bytes(executable.read_bytes()), "version": version,
                   "bundled_assets_identity": omp_bundle_identity}
    if not isinstance(omp_bundle_identity, str) or len(omp_bundle_identity) != 64 or any(
            c not in "0123456789abcdef" for c in omp_bundle_identity):
        unknowns.append("OMP bundled agent/tool/prompt identity unverified")
    required_route = tuple(policy["route_identity_fields"])
    if not isinstance(route_identity, dict) or set(route_identity) != set(required_route):
        unknowns.append("route/role/model/context identity unavailable")
    elif (any(not isinstance(route_identity[key], str) or not route_identity[key]
              for key in required_route if key != "context_limit_tokens")
          or type(route_identity["context_limit_tokens"]) is not int
          or route_identity["context_limit_tokens"] <= 0
          or route_identity["source"] not in policy["route_identity_sources"]):
        unknowns.append("route identity is not an observed OMP context")
    core = {"policy_version": policy["projection_version"], "policy_sha256": policy_sha,
            "entries": dict(sorted(entries.items())),
            "development_overlay": {"version": OVERLAY_VERSION, "entries": overlay},
            "omp": omp, "route_identity": route_identity,
            "host": {"os_family": platform.system(), "architecture": platform.machine(),
                     "python_minor": f"{sys.version_info.major}.{sys.version_info.minor}"}}
    return {"schema_version": 1, "digest": canonical_digest(core), "identity": core,
            "unknowns": sorted(set(unknowns)),
            "volatile_host": {"kernel_release": platform.release(),
                              "python_patch": platform.python_version()}}


def same_projection(original: dict, current: dict) -> tuple[bool, list[str]]:
    issues: list[str] = []
    for label, value in (("original", original), ("current", current)):
        if not isinstance(value, dict) or not isinstance(value.get("unknowns"), list):
            issues.append(label + " projection malformed")
            continue
        issues.extend(value["unknowns"])
        identity = value.get("identity")
        if (value.get("schema_version") != 1 or not isinstance(identity, dict)
                or not {"policy_version", "policy_sha256", "entries", "development_overlay",
                        "omp", "route_identity", "host"} <= identity.keys()
                or value.get("digest") != canonical_digest(identity)):
            issues.append(label + " projection identity invalid")
    if not isinstance(original, dict) or not isinstance(current, dict) or original.get("digest") != current.get("digest"):
        issues.append("projection changed")
    return not issues, sorted(set(issues))
