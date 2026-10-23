"""Canonical product policy identity and private compatibility admission records.

The canonical JSON is the sole closure policy. Development verdicts are never
imported. Complete owned evidence grants neither work authority nor raw activation.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import platform
import stat
import sys
from threading import RLock
from typing import Any, Callable
from uuid import UUID, uuid4

from workbench.assets import canonical_path, runtime_root

from workbench.backend.paths import DataDirError, InstanceLock, ensure_private_dir, write_private_json, write_private_bytes
from workbench.backend.summary_adapter import SummaryDeferred, _resolved_metadata
from workbench.backend.summary_compat_provider import Q01_ITEMS, REQUIRED_CHECKS, IO_CAVEATS, ScriptedCompatibilityProvider, item_rows as derive_item_rows

POLICY_PATH = "src/workbench/contracts/summary_compat_policy_v1.json"
RECORD_LIMIT = 1 << 20
def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _digest(value: Any) -> str:
    return sha256(_canonical(value)).hexdigest()


def _hex_digest(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _complete_identity(value: Any) -> bool:
    return (isinstance(value, dict) and type(value.get("policy_version")) is int
            and value["policy_version"] == 1 and value.get("unknowns") == []
            and all(_hex_digest(value.get(key)) for key in
                    ("policy_sha256", "runtime_digest", "closure_digest"))
            and isinstance(value.get("stable_host"), dict)
            and all(isinstance(value["stable_host"].get(key), str)
                    and bool(value["stable_host"][key].strip())
                    for key in ("os_family", "architecture", "python_minor")))


def _complete_omp(value: Any) -> bool:
    return (isinstance(value, dict) and isinstance(value.get("version"), str)
            and bool(value["version"].strip())
            and all(_hex_digest(value.get(key)) for key in ("binary_sha256", "bundled_sha256")))


def _bound_install_assets(value: Any) -> bool:
    selection = value.get("asset_selection") if isinstance(value, dict) else None
    return (isinstance(selection, dict) and set(selection) == {"PI_PACKAGE_DIR", "OMP_NATIVE_LIBRARY_PATH"}
            and all(item is None or isinstance(item, str) for item in selection.values())
            and isinstance(value.get("binary_path"), str) and Path(value["binary_path"]).is_absolute())


def checkout_root() -> Path:
    return runtime_root()


def _relative(value: Any) -> str:
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError("unsafe canonical policy path")
    return value


def collect_runtime_identity(root: Path | None = None) -> dict[str, Any]:
    """Read runtime and static Python closure; unknown imports invalidate identity."""
    root = (root or checkout_root()).resolve()

    def checked_path(relative: str) -> Path:
        parts = Path(_relative(relative)).parts
        for index in range(1, len(parts) + 1):
            path = canonical_path(Path(*parts[:index]).as_posix(), root)
            if path.is_symlink():
                raise ValueError("symlink in runtime identity path")
        return path

    policy_file = checked_path(POLICY_PATH)
    if not stat.S_ISREG(policy_file.lstat().st_mode):
        raise ValueError("unsafe canonical policy")
    raw = policy_file.read_bytes()
    policy = json.loads(raw)
    if (policy.get("schema_version") != 1 or policy.get("projection_version") != 1
            or policy.get("unknown_dynamic_import") != "invalidate"
            or policy.get("include_python_package_initializers") is not True):
        raise ValueError("unsupported canonical policy")
    entries: dict[str, dict] = {}
    unknowns: list[str] = []

    def include(relative: str) -> None:
        relative = _relative(relative)
        if relative in entries:
            return
        try:
            path = checked_path(relative)
        except ValueError:
            entries[relative] = {"kind": "unsupported"}
            unknowns.append("unsafe_runtime_input:" + relative)
            return
        try:
            info = path.lstat()
        except FileNotFoundError:
            entries[relative] = {"kind": "absent"}
            return
        item: dict[str, Any] = {"mode": stat.S_IMODE(info.st_mode)}
        if stat.S_ISREG(info.st_mode):
            item.update(kind="file", sha256=sha256(path.read_bytes()).hexdigest())
        elif stat.S_ISDIR(info.st_mode):
            item["kind"] = "directory"
        else:
            item["kind"] = "unsupported"
            unknowns.append("unsafe_runtime_input:" + relative)
        entries[relative] = item

    def walk(relative: str) -> None:
        include(relative)
        if entries[relative]["kind"] != "directory":
            unknowns.append("missing_runtime_root:" + relative)
            return
        base = checked_path(relative)
        for path in sorted(base.rglob("*")):
            suffix = path.relative_to(base)
            # Collected resources have their own canonical omp_bridge names.
            if ("__pycache__" not in suffix.parts and "_assets" not in suffix.parts
                    and path.suffix != ".pyc"):
                include((Path(relative) / suffix).as_posix())

    for relative in policy["product_runtime_roots"]:
        walk(_relative(relative))
    for relative in (*policy["product_runtime_files"], *policy["required_seams"]):
        include(_relative(relative))
        if entries[relative]["kind"] != "file":
            unknowns.append("missing_required_seam:" + relative)
    full_entries = dict(entries)
    entries = {}
    for relative in (*policy["runtime_roots"], *policy["python_closure_roots"]):
        walk(_relative(relative))
    for relative in (*policy["runtime_files"], *policy["required_seams"]):
        include(_relative(relative))

    def module_paths(name: str) -> list[str]:
        if name != "workbench" and not name.startswith("workbench."):
            return []
        base = "src/" + name.replace(".", "/")

        def candidate(p: str) -> bool:
            try:
                return checked_path(p).is_file()
            except ValueError:
                # ValueError means checked_path rejected the path: a symlink, an absolute path or
                # a ``..`` escape. Such unsafe input is never followed; keep it as a candidate so
                # include() records it as an explicit unsafe_runtime_input unknown.
                return True

        return [p for p in (base + ".py", base + "/__init__.py") if candidate(p)]

    pending = [p for p, item in entries.items() if p.endswith(".py") and item["kind"] == "file"]
    visited: set[str] = set()
    while pending:
        relative = pending.pop()
        if relative in visited:
            continue
        visited.add(relative)
        for parent in Path(relative).parents:
            if parent.as_posix().startswith("src/workbench"):
                init = (parent / "__init__.py").as_posix()
                include(init)
                if entries[init]["kind"] == "file" and init not in visited:
                    pending.append(init)
        try:
            tree = ast.parse(checked_path(relative).read_bytes())
        except (SyntaxError, UnicodeError):
            unknowns.append("unreadable_python_closure:" + relative)
            continue
        module = relative.removeprefix("src/").removesuffix(".py").replace("/", ".")
        package = module.removesuffix(".__init__") if relative.endswith("/__init__.py") else module.rpartition(".")[0]
        loaders = {"__import__"}
        importlib_names, builtin_names = {"importlib"}, {"builtins"}
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name)
                    if alias.name == "importlib":
                        importlib_names.add(alias.asname or alias.name)
                    if alias.name == "builtins":
                        builtin_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom):
                try:
                    base = importlib.util.resolve_name("." * node.level + (node.module or ""), package) if node.level else node.module or ""
                except ImportError:
                    unknowns.append("unresolved_relative_import:" + relative)
                    continue
                names.add(base)
                names.update(base + "." + a.name for a in node.names if a.name != "*")
                if node.module in ("importlib", "builtins"):
                    loaders.update(a.asname or a.name for a in node.names
                                   if a.name in ("import_module", "__import__"))

        def loader(func: ast.expr) -> bool:
            return ((isinstance(func, ast.Name) and func.id in loaders)
                    or (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                        and ((func.attr == "import_module" and func.value.id in importlib_names)
                             or (func.attr == "__import__" and func.value.id in builtin_names))))

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and loader(node.value):
                loaders.update(t.id for t in node.targets if isinstance(t, ast.Name))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and loader(node.func):
                if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str) or node.args[0].value.startswith("."):
                    unknowns.append("unknown_dynamic_import:" + relative)
                else:
                    names.add(node.args[0].value)
        for name in names:
            if name.startswith(("tools", "tests", "docs", ".workflow")):
                unknowns.append("development_import:" + relative + ":" + name)
            children = module_paths(name)
            for child in children:
                include(child)
                if child not in visited and entries[child]["kind"] == "file":
                    pending.append(child)
            if name.startswith("workbench.") and not children and not module_paths(name.rpartition(".")[0]):
                unknowns.append("unresolved_workbench_import:" + relative + ":" + name)
    for relative in policy["workbench_owned_assets"]:
        if entries.get(relative, {}).get("kind") not in ("file", "directory"):
            unknowns.append("missing_workbench_asset:" + relative)
    stable_host = {"os_family": platform.system(), "architecture": platform.machine(),
                   "python_minor": f"{sys.version_info.major}.{sys.version_info.minor}"}
    return {"policy_version": policy["projection_version"], "policy_sha256": sha256(raw).hexdigest(),
            "runtime_digest": _digest(full_entries), "closure_digest": _digest(entries),
            "runtime_entries": full_entries, "closure_entries": entries,
            "stable_host": {key: stable_host[key] for key in policy["stable_host_fields"]},
            "volatile_host": {"kernel_release": platform.release(), "python_patch": platform.python_version()},
            "unknowns": sorted(set(unknowns))}


def _read_owned(path: Path) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > RECORD_LIMIT):
            raise DataDirError("unsafe compatibility record")
        value = json.loads(os.read(fd, RECORD_LIMIT + 1))
    finally:
        os.close(fd)
    if not isinstance(value, dict):
        raise ValueError("invalid compatibility record")
    return value


def _verified_log(directory: Path, evidence: Any) -> bool:
    """Check a bounded private file by descriptor; records cannot name outside paths."""
    if not isinstance(evidence, dict):
        return False
    name, expected = evidence.get("path"), evidence.get("sha256")
    if (not isinstance(name, str) or name in ("", ".", "..")
            or Path(name).parts != (name,) or "\0" in name
            or not isinstance(expected, str) or len(expected) != 64
            or any(c not in "0123456789abcdef" for c in expected)):
        return False
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(parent)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            return False
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_size > RECORD_LIMIT):
                return False
            hasher, total = sha256(), 0
            while block := os.read(fd, 65536):
                total += len(block)
                if total > RECORD_LIMIT:
                    return False
                hasher.update(block)
            after = os.fstat(fd)
            return (total == before.st_size == after.st_size
                    and before.st_mtime_ns == after.st_mtime_ns
                    and before.st_ctime_ns == after.st_ctime_ns
                    and hasher.hexdigest() == expected)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


class SummaryCompatibility:
    def __init__(self, directory: Path, *, identity: Callable[[], dict] = collect_runtime_identity,
                 omp_identity: Callable[[], dict | None] = lambda: None,
                 boundary: Callable[[], dict | None] = lambda: None):
        self.directory, self.identity = directory, identity
        self.omp_identity, self.boundary = omp_identity, boundary
        self._lock = RLock()
        self._last_observation = {"state": "unavailable", "reason": "compatibility_not_checked",
                                  "lastCheckedAt": None}

    def _state(self) -> dict:
        if self.directory.exists() or self.directory.is_symlink():
            info = self.directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise DataDirError("unsafe compatibility directory")
        state = _read_owned(self.directory / "state.json")
        if state is None:
            return {"latest_record": None, "record_sha256": None, "protection": None, "episode": 0}
        if (any(key not in state or (state[key] is not None and not isinstance(state[key], str))
                for key in ("latest_record", "record_sha256", "protection"))
                or type(state.get("episode")) is not int or state["episode"] < 0):
            raise ValueError("invalid compatibility state")
        return state

    def _save_state(self, state: dict) -> None:
        ensure_private_dir(self.directory)
        write_private_json(self.directory / "state.json", state)

    @contextmanager
    def _mutation(self):
        with self._lock:
            ensure_private_dir(self.directory)
            lease = InstanceLock(self.directory / "record.lock")
            lease.acquire()
            try:
                yield
            finally:
                lease.release()

    def verify_scripted(self) -> dict:
        """Explicit user operation; no transport import or copied development pass."""
        with self._mutation():
            try:
                before = self.identity()
            except (OSError, ValueError, KeyError, TypeError):
                before = {"unknowns": ["runtime_identity_unavailable"]}
            # Unknown checkout identity cannot authorize owned runtime launch.
            if _complete_identity(before):
                observed = ScriptedCompatibilityProvider().observe()
            else:
                from workbench.backend.summary_compat_runtime import unavailable
                observed = unavailable("runtime_identity_unavailable")
            try:
                after = self.identity()
            except (OSError, ValueError, KeyError, TypeError):
                after = {"unknowns": ["runtime_identity_unavailable"]}
            unknowns = list(observed["unknowns"]) + before["unknowns"]
            if before != after:
                unknowns.append("runtime_changed_during_verification")
            allowed_item_results = {"pass", "failed", "inconclusive", "not_run"}
            rows_by_item = {}
            raw_rows = observed.get("item_rows")
            if isinstance(raw_rows, list):
                for row in raw_rows:
                    if (isinstance(row, dict) and row.get("item") in Q01_ITEMS
                            and row.get("result") in allowed_item_results):
                        rows_by_item.setdefault(row["item"], row)
            item_rows = []
            items = {}
            for name in Q01_ITEMS:
                row = rows_by_item.get(name)
                if row is None:
                    # Legacy item flags cannot turn a bare pass into evidence.
                    result = "inconclusive" if observed.get("items", {}).get(name) == "inconclusive" else "not_run"
                    row = {"item": name, "result": result, "checks": {},
                           "unknowns": ["per_item_observation_row_unavailable"], "evidence_refs": []}
                row = deepcopy(row)
                item_rows.append(row)
                items[name] = row["result"]
                if isinstance(row.get("unknowns"), list):
                    unknowns.extend(value for value in row["unknowns"] if isinstance(value, str))
            record = {"id": str(uuid4()), "schema_version": 1, "validator_version": 1,
                      "created_at": datetime.now(timezone.utc).isoformat(), "identity": before,
                      "omp": observed.get("omp") or self.omp_identity(), "boundary": self.boundary(),
                      "result": "inconclusive" if observed["level"] == "inconclusive" else "unavailable",
                      "observations": observed["observations"],
                      "item_rows": item_rows, "items": items,
                      "model_metadata": observed["model_metadata"], "real_model_contract": "unverified",
                      "unknowns": sorted(set(unknowns)), "io": observed["io"]}
            record["caveats"] = sorted(set(observed.get("caveats", [])) & IO_CAVEATS)
            configured = next((row for row in record["observations"]
                               if row.get("kind") == "owned_configured_metadata"), None)
            metadata_valid = False
            try:
                _resolved_metadata(record["model_metadata"])
                metadata_valid = (configured is not None and configured.get("result") == "pass"
                    and configured.get("cleanup_complete") is True and configured.get("checks")
                    and all(value is True for value in configured["checks"].values())
                    and configured.get("metadata") == record["model_metadata"]
                    and configured.get("omp") == record["omp"])
            except SummaryDeferred:
                pass
            if not metadata_valid:
                record["unknowns"] = sorted(set(record["unknowns"] + ["configured_model_readback_unavailable"]))
            if observed["level"] == "failed" or any(row["result"] == "failed" for row in item_rows):
                record["result"] = "failed"
            elif (not record["unknowns"] and before == after and _complete_identity(before)
                  and _complete_omp(record["omp"]) and record["boundary"] and metadata_valid
                  and item_rows == derive_item_rows(record["observations"])
                  and all(row["result"] == "pass" and all(row.get("checks", {}).get(key) is True
                          for key in REQUIRED_CHECKS[row["item"]]) for row in item_rows)):
                record["result"] = "pass"
            ensure_private_dir(self.directory)
            log = {"record_id": record["id"], "item_rows": item_rows,
                   "observations": [{"kind": row.get("kind"), "sha256": _digest(row)} for row in record["observations"]],
                   "model_metadata_sha256": _digest(record["model_metadata"]), "io": record["io"], "caveats": record["caveats"]}
            log_bytes = _canonical(log) + b"\n"
            if len(log_bytes) > RECORD_LIMIT:
                raise ValueError("compatibility evidence exceeds bound")
            log_name = record["id"] + ".log.jsonl"
            write_private_bytes(self.directory / log_name, log_bytes)
            record["evidence"] = [{"path": log_name, "sha256": sha256(log_bytes).hexdigest()}]
            if len(_canonical(record)) > RECORD_LIMIT:
                raise ValueError("compatibility record exceeds bound")
            write_private_bytes(self.directory / (record["id"] + ".json"), _canonical(record))
            state = self._state()
            state.update(latest_record=record["id"], record_sha256=_digest(record))
            if record["result"] == "pass":
                state.update(protection=None, notice_pending=False)
            # A failed/inconclusive verify cannot clear a previous protection episode.
            self._save_state(state)
            return {key: record[key] for key in
                    ("id", "result", "items", "item_rows", "model_metadata", "unknowns", "caveats", "io")}

    def _verified_record(self, ref: str) -> dict | None:
        if not isinstance(ref, str) or str(UUID(ref)) != ref:
            return None
        state = self._state()
        if state["latest_record"] != ref:
            return None
        record = _read_owned(self.directory / (ref + ".json"))
        if (record is None or _digest(record) != state["record_sha256"]
                or record.get("schema_version") != 1 or record.get("validator_version") != 1
                or record.get("id") != ref or record.get("result") != "pass"
                or record.get("unknowns") != [] or record.get("items") != {key: "pass" for key in Q01_ITEMS}
                or not _complete_identity(record.get("identity")) or not _complete_omp(record.get("omp"))
                or not isinstance(record.get("model_metadata"), dict)
                or not record.get("observations") or not record.get("boundary")):
            return None
        metadata = record["model_metadata"]
        unknowns = metadata.get("unknowns")
        budget_unknowns = {"current_context_unavailable", "output_ceiling_enforcement_unavailable",
                           "reasoning_budget_semantics_unavailable"}
        if (not isinstance(unknowns, list) or any(not isinstance(reason, str) for reason in unknowns)
                or len(unknowns) != len(set(unknowns)) or not set(unknowns) <= budget_unknowns
                or metadata.get("status") not in ("pass", "inconclusive")
                or (metadata["status"] == "pass" and unknowns)
                or (metadata["status"] == "inconclusive" and not unknowns)):
            return None
        if metadata["status"] == "pass":
            meaning = metadata.get("outputMeaning")
            if (any(type(metadata.get(key)) is not int or metadata[key] < 0
                    for key in ("existingContextTokens", "reasoningTokens", "visibleResultTokens"))
                    or type(metadata.get("outputLimitEnforced")) is not bool
                    or not isinstance(meaning, str) or not meaning.strip() or meaning.strip() == "unknown"):
                return None
        if ("current_context_unavailable" in unknowns
                and ("existingContextTokens" not in metadata or metadata["existingContextTokens"] is not None)):
            return None
        if ("output_ceiling_enforcement_unavailable" in unknowns
                and ("outputLimitEnforced" not in metadata or metadata["outputLimitEnforced"] is not None)):
            return None
        if ("reasoning_budget_semantics_unavailable" in unknowns
                and (any(key not in metadata or metadata[key] is not None
                         for key in ("reasoningTokens", "visibleResultTokens"))
                     or metadata.get("outputMeaning") != "unknown")):
            return None
        try:
            _resolved_metadata(metadata)
        except SummaryDeferred:
            return None
        created = record.get("created_at")
        if not isinstance(created, str) or datetime.fromisoformat(created).utcoffset() is None:
            return None
        raw = record["observations"]
        observations = record.get("item_rows")
        if (not isinstance(raw, list) or not raw or not isinstance(observations, list) or len(observations) != len(Q01_ITEMS)
                or any(not isinstance(row, dict) or row.get("item") not in Q01_ITEMS
                       or row.get("result") != "pass" or row.get("unknowns") != []
                       or any(row.get("checks", {}).get(key) is not True for key in REQUIRED_CHECKS[row["item"]])
                       for row in observations)
                or {row["item"] for row in observations} != set(Q01_ITEMS)
                or observations != derive_item_rows(raw)):
            return None
        for row in observations:
            refs = row.get("evidence_refs")
            if not isinstance(refs, list) or not refs:
                return None
            for evidence_ref in refs:
                index = evidence_ref.get("observation_index")
                if (type(index) is not int or not 0 <= index < len(raw)
                        or evidence_ref.get("kind") != raw[index].get("kind")
                        or evidence_ref.get("sha256") != _digest(raw[index])):
                    return None
        configured = [row for row in raw if row.get("kind") == "owned_configured_metadata"]
        if (len(configured) != 1 or configured[0].get("result") != "pass"
                or configured[0].get("cleanup_complete") is not True
                or configured[0].get("metadata") != metadata or configured[0].get("omp") != record["omp"]
                or not configured[0].get("checks") or any(value is not True for value in configured[0]["checks"].values())
                or not isinstance(record.get("caveats"), list) or not set(record["caveats"]) <= IO_CAVEATS):
            return None
        boundary = record["boundary"]
        if (not isinstance(boundary, dict) or not isinstance(boundary.get("adoption_ref"), str)
                or not boundary["adoption_ref"].strip()):
            return None
        evidence = record.get("evidence")
        if (not isinstance(evidence, list) or not evidence
                or any(not _verified_log(self.directory, item) for item in evidence)):
            return None
        return record

    def observed_status(self) -> dict:
        """Detached last check for display; no file I/O or admission decision."""
        with self._lock:
            return deepcopy(self._last_observation)

    def _observe(self, result: dict) -> None:
        observation = deepcopy(result)
        observation.pop("admitted", None)  # A stale display never grants admission.
        observation["lastCheckedAt"] = datetime.now(timezone.utc).isoformat()
        self._last_observation = observation

    def status(self) -> dict:
        """Live check, preserving the admission API; rendering uses observed_status."""
        with self._lock:
            result = self._checked_status()
            try:
                self._observe(result)
            except RecursionError:
                result = {"state": "unavailable", "reason": "compatibility_record_invalid", "admitted": False}
                self._observe(result)
            return result

    def _checked_status(self) -> dict:
        with self._lock:
            try:
                state = self._state()
                if state.get("protection"):
                    return {"state": "protected", "reason": state["protection"], "episode": state["episode"],
                            "admitted": False, "latest_record": state["latest_record"]}
                ref = state.get("latest_record")
                record = self._verified_record(ref) if ref else None
                if record is None:
                    return {"state": "unavailable", "reason": "verified_record_unavailable", "admitted": False,
                            "latest_record": ref}
                current = self.identity()
                old = record["identity"]
                if (not _complete_identity(current) or current["policy_sha256"] != old["policy_sha256"]
                        or current["closure_digest"] != old["closure_digest"]
                        or current["stable_host"] != old["stable_host"] or self.boundary() != record["boundary"]):
                    return {"state": "revalidation_required", "reason": "related_boundary_changed", "admitted": False}
                omp = self.omp_identity()
                if (not isinstance(omp, dict) or not _hex_digest(omp.get("binary_sha256"))
                        or (omp.get("version") is not None and (not isinstance(omp["version"], str)
                            or not omp["version"].strip()))
                        or (omp.get("bundled_sha256") is not None and not _hex_digest(omp["bundled_sha256"]))
                        or (omp.get("asset_selection") is not None and not _bound_install_assets(omp))):
                    return {"state": "unavailable", "reason": "omp_identity_unavailable", "admitted": False}
                status = "verified"
                fields = ("version", "binary_sha256", "bundled_sha256")
                assets_exact = (_bound_install_assets(omp) and _bound_install_assets(record["omp"])
                    and all(omp[key] == record["omp"][key] for key in ("binary_path", "asset_selection")))
                exact = _complete_omp(omp) and assets_exact and all(omp[key] == record["omp"][key] for key in fields)
                if (any(omp.get(key) is not None and omp[key] != record["omp"][key] for key in fields)
                        or (_bound_install_assets(omp) and _bound_install_assets(record["omp"]) and not assets_exact)):
                    status = "version_drift_unverified"
                elif current["runtime_digest"] != old["runtime_digest"]:
                    status = "unrelated_change_observed"
                return {"state": status, "admitted": True, "latest_record": ref,
                        "verified_omp": record["omp"], "verified_record": ref,
                        "current_omp": omp, "current_omp_exact": exact,
                        "current_omp_unknowns": [key + "_unavailable" for key in fields if omp.get(key) is None]
                            + ([] if _bound_install_assets(omp) else ["asset_selection_unavailable"])}
            except (OSError, ValueError, KeyError, TypeError, DataDirError, RecursionError):
                return {"state": "unavailable", "reason": "compatibility_record_invalid", "admitted": False}

    def admission(self) -> bool:
        return self.status()["admitted"] is True

    def activation_preflight(self, ref: str) -> bool:
        """Cheap original/current checks before an explicit activation getter."""
        try:
            with self._lock:
                status = self._checked_status()
                if not status.get("admitted") or status.get("latest_record") != ref:
                    return False
                record = self._verified_record(ref)
                if record is None or not _bound_install_assets(record["omp"]):
                    return False
                current, omp = self.identity(), self.omp_identity()
                return bool(_complete_identity(current) and current == record["identity"]
                    and record.get("boundary") == self.boundary() and _bound_install_assets(omp)
                    and all(omp[key] == record["omp"][key]
                            for key in ("binary_path", "binary_sha256", "asset_selection")))
        except (OSError, ValueError, KeyError, TypeError, DataDirError, RecursionError):
            return False

    def verify_reference(self, ref: str) -> bool:
        try:
            with self._lock:
                record = self._verified_record(ref)
                if record is None:
                    return False
                current, omp = self.identity(), self.omp_identity()
                return bool(_complete_identity(current) and _complete_omp(omp)
                            and _bound_install_assets(omp) and _bound_install_assets(record["omp"])
                            and record["identity"] == current and all(record["omp"][key] == omp[key]
                                for key in ("version", "binary_sha256", "bundled_sha256", "binary_path", "asset_selection"))
                            and record.get("boundary") == self.boundary())
        except (OSError, ValueError, KeyError, TypeError, DataDirError, RecursionError):
            return False

    def protect(self, incident: dict) -> None:
        """Persist an existing journal incident; repeated delivery does not reopen it."""
        with self._mutation():
            state = self._state()
            key = _digest(incident)
            if state.get("incident_key") == key:
                return
            if state.get("protection"):
                # The same active protection episode has already been announced.
                # A cause update is retained without re-opening its notice.
                state.update(incident_key=key, incident=incident)
                self._save_state(state)
                return
            state["episode"] += 1
            state.update(protection=str(incident.get("reason") or "guard_confirmation_unavailable")[:256],
                         incident_key=key, incident=incident, notice_pending=True)
            self._save_state(state)
            self._observe({"state": "protected", "reason": state["protection"],
                           "episode": state["episode"], "latest_record": state["latest_record"]})

    def take_notice(self) -> dict | None:
        with self._mutation():
            state = self._state()
            if not state.get("notice_pending"):
                return None
            state["notice_pending"] = False
            self._save_state(state)
            return {"episode": state["episode"], "reason": state["protection"]}
