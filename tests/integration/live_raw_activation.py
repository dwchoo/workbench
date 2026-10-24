"""Owned real product verification/activation; never touches the user's raw policy."""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

from workbench.backend import summary_compat_runtime as rt
from workbench.backend.summary_compat_identity import compatibility_boundary, current_omp_identity
from workbench.backend.summary_compatibility import SummaryCompatibility, collect_runtime_identity


REPO = Path(__file__).resolve().parents[2]


def write_json(path: Path, value: dict) -> None:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    if len(raw) > 1_000_000:
        raise ValueError("bounded feature evidence exceeded")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def read_json(path: Path) -> dict:
    info = path.lstat()
    if path.is_symlink() or not path.is_file() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe feature evidence")
    raw = path.read_bytes()
    if len(raw) > 1_000_000:
        raise ValueError("bounded feature evidence exceeded")
    return json.loads(raw)


def feature_context() -> tuple[Path, str, str]:
    from tools.dev_validate import _input_manifest
    directory = Path(os.environ["WB_DVL_FEATURE_EVIDENCE"])
    if not directory.is_absolute() or directory.resolve() != directory:
        raise ValueError("feature evidence must have an absolute private path")
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("feature evidence must be owned and private")
    return directory, os.environ["WB_DVL_INVOCATION_ID"], _input_manifest()["sha256"]


def selected_binary() -> dict:
    binary = Path(os.environ["WB_DVL_OMP"]).resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("selected OMP must be executable")
    return {"binary_path": str(binary), "binary_sha256": sha256(binary.read_bytes()).hexdigest()}


def selected_path(work: Path) -> tuple[str, dict]:
    """Pin public PATH-based getters to the selected executable, whatever its basename."""
    selected = selected_binary()
    private_bin = work / "bin"
    private_bin.mkdir(mode=0o700)
    (private_bin / "omp").symlink_to(selected["binary_path"])
    path = str(private_bin) + ":/usr/bin:/bin"
    if Path(shutil.which("omp", path=path)).resolve(strict=True) != Path(selected["binary_path"]):
        raise ValueError("selected OMP alias resolution differs")
    return path, selected


def require_selected(identity: dict, selected: dict) -> None:
    if not isinstance(identity, dict) or any(identity.get(key) != value for key, value in selected.items()):
        raise ValueError("observed OMP differs from selected executable")


def retain_nonpass(source: Path, reference: str, directory: Path) -> dict:
    """Preserve the original failed record/log bytes, never a successful producer."""
    if not isinstance(reference, str) or not reference or Path(reference).name != reference:
        return {"available": False, "reason": "compatibility_record_id_unavailable"}
    record_name = reference + ".json"
    record = read_json(source / record_name)
    retained = directory / "product-nonpass"
    retained.mkdir(mode=0o700)
    artifacts = []
    for name, expected in [(record_name, None), *[(row["path"], row["sha256"]) for row in record["evidence"]]]:
        if not name or Path(name).name != name:
            raise ValueError("unsafe product artifact name")
        read_json(source / name)
        raw = (source / name).read_bytes()
        digest = sha256(raw).hexdigest()
        if len(raw) > 1_000_000 or (expected is not None and digest != expected):
            raise ValueError("changed or oversized compatibility evidence")
        fd = os.open(retained / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        artifacts.append({"path": name, "sha256": digest})
    return {"available": True, "directory": str(retained), "record_id": reference,
        "original_result": record["result"], "artifacts": artifacts,
        "boundary": "Original nonpass evidence only; no producer or activation receipt."}


def prepare_activation(work: Path, data: Path) -> dict:
    """Run the unchanged product CLI in a runtime-only copy, then keep small evidence."""
    directory, invocation, candidate = feature_context()
    runtime = rt._copy_runtime(REPO, work)
    # The runtime identity includes source modes. The enclosing work root is
    # private; preserve public source modes so the later actual Backend has the
    # same closure identity, without changing any original path or policy.
    for path in runtime.rglob("*"):
        path.chmod((REPO / path.relative_to(runtime)).stat().st_mode & 0o777)
    if collect_runtime_identity(runtime) != collect_runtime_identity(REPO):
        raise ValueError("runtime-only source identity differs")
    for name in ("home", "agent", "config", "project", "tmp", "xdg-config", "xdg-data", "xdg-state", "xdg-cache"):
        (work / name).mkdir(mode=0o700)
    path, selected = selected_path(work)
    env = rt._environment(work)
    env.update(PATH=path, PYTHONPATH=str(runtime / "src"),
               PYTHONDONTWRITEBYTECODE="1", WORKBENCH_HUB_AUTOSTART="0")
    def cli(*args: str, timeout: float = 180) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "workbench", *args, "--data-dir", str(data)],
            cwd=work / "project", env=env, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=timeout)
    verified = cli("logs", "summary-compat", "verify", "--scripted", "--json")
    if len(verified.stdout.encode()) > 1_000_000:
        raise ValueError("product CLI evidence exceeded")
    result = json.loads(verified.stdout)
    source = data / "summary-compatibility"
    def nonpass_archive():
        try:
            return retain_nonpass(source, result.get("id"), directory)
        except (OSError, ValueError, KeyError, TypeError) as error:
            return {"available": False, "reason": type(error).__name__}
    if verified.returncode != 0 or result.get("result") != "pass":
        return {"result": "failed" if result.get("result") == "failed" else "inconclusive",
                "verification": result, "exit_code": verified.returncode,
                "nonpass_archive": nonpass_archive()}
    reference = result["id"]
    record = read_json(source / (reference + ".json"))
    require_selected(record.get("omp"), selected)
    require_selected(selected_binary(), selected)
    activated = cli("logs", "raw-rotation", "activate", "--yes", "--evidence", reference, timeout=65)
    require_selected(selected_binary(), selected)
    if activated.returncode != 0:
        return {"result": "inconclusive", "verification": result,
                "activation_exit_code": activated.returncode, "nonpass_archive": nonpass_archive()}
    policy = read_json(data / "raw-rotation.json")
    if policy.get("evidence") != reference or policy.get("activated") is not True:
        raise AssertionError("actual CLI did not publish the owned activation policy")
    retained = directory / "product"
    retained.mkdir(mode=0o700)
    artifacts = []
    for name in (reference + ".json", record["evidence"][0]["path"], "state.json"):
        raw = (source / name).read_bytes()
        if len(raw) > 1_000_000:
            raise ValueError("product evidence exceeded bound")
        target = retained / name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        artifacts.append({"path": name, "sha256": sha256(raw).hexdigest()})
    after = selected_binary()
    require_selected(after, selected)
    envelope = {"invocation": invocation, "candidate": candidate, "record_id": reference,
        "artifacts": artifacts, "record_omp": record["omp"], "result": "passed",
        "selected_binary": selected, "selected_binary_after": after,
        "execution_binding": "Private PATH-first omp alias; original public getter path/digest and selected file before/after, not /proc executable observation.",
        "activation": {"exit_code": activated.returncode, "warning_observed": "경고" in activated.stderr,
                       "evidence": policy["evidence"]},
        "runtime_only": all(not (runtime / name).exists() for name in ("tests", "tools", "docs", ".workflow")),
        "unknowns": record["unknowns"], "caveats": record.get("caveats", []), "io": record["io"]}
    write_json(directory / "product-producer.json", envelope)
    return envelope


def consume_product() -> dict:
    """Revalidate this invocation's actual record and log; never borrow current identity."""
    directory, invocation, candidate = feature_context()
    producer = read_json(directory / "product-producer.json")
    if producer.get("invocation") != invocation or producer.get("candidate") != candidate:
        raise ValueError("stale product producer")
    retained = directory / "product"
    for artifact in producer["artifacts"]:
        name = artifact["path"]
        if Path(name).name != name:
            raise ValueError("unsafe product artifact name")
        read_json(retained / name)
        if sha256((retained / name).read_bytes()).hexdigest() != artifact["sha256"]:
            raise ValueError("changed product producer evidence")
    selected = selected_binary()
    for binding in (producer.get("selected_binary"), producer.get("selected_binary_after"), producer.get("record_omp")):
        require_selected(binding, selected)
    reference = producer["record_id"]
    record = read_json(retained / (reference + ".json"))
    require_selected(record.get("omp"), selected)
    service = SummaryCompatibility(retained, identity=lambda: collect_runtime_identity(REPO),
        omp_identity=lambda: current_omp_identity(os.environ["WB_DVL_OMP"]), boundary=compatibility_boundary)
    original_valid = service._verified_record(reference) is not None
    status = service.status()
    admitted = service.admission()
    exact = service.verify_reference(reference)
    require_selected(selected_binary(), selected)
    checks = {"original_record_complete": original_valid, "selected_binary_binding": True,
        "runtime_only": producer["runtime_only"] is True,
        "actual_activation": producer["activation"]["exit_code"] == 0
            and producer["activation"]["warning_observed"] is True}
    return {"result": "passed" if all(checks.values()) else "inconclusive", "checks": checks,
        "status": status, "verify_reference": exact, "record_id": reference,
        "operating_admitted": admitted, "selected_binary": selected,
        "current_identity_limit": "Cheap current getter leaves version/bundle unknown; operational admission/current_exact are descriptive, not strict selected-binary proof.",
        "artifacts": producer["artifacts"], "caveats": producer["caveats"], "io": producer["io"],
        "cleanup": {"complete": True, "remaining_owned": []}}
