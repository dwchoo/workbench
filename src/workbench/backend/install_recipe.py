"""Official OMP distribution recipe and bounded executable inspection.

A successful --version is executable evidence only, never SDK compatibility.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import subprocess
import os
import signal
import re
import json
import platform
import tempfile
import hashlib
import time
import selectors
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


@dataclass(frozen=True)
class ExistingOMP:
    path: Path
    status: str
    version: str | None = None
    reason: str | None = None


def inspect_existing(path: Path, *, timeout: float = 10, environment=None) -> ExistingOMP:
    path = Path(path).absolute()
    try:
        path.lstat()
    except FileNotFoundError:
        return ExistingOMP(path, "missing")
    except OSError as exc:
        return ExistingOMP(path, "broken", reason=str(exc))
    try:
        process = subprocess.Popen([str(path), "--version"], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True, env=environment)
    except OSError as exc:
        return ExistingOMP(path, "broken", reason=str(exc))
    collected = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ExistingOMP(path, "broken", reason="version_timeout")
                for key, _mask in selector.select(remaining):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    collected[key.data].extend(chunk)
                    if sum(map(len, collected.values())) > 65536:
                        return ExistingOMP(path, "broken", reason="version_output_limit")
        # Keep the unreaped leader reserving its PID/PGID until group cleanup.
        # This executable inspection is supported on the Linux product host.
        while True:
            if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ExistingOMP(path, "broken", reason="version_timeout")
            time.sleep(min(0.01, remaining))
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
    stdout, stderr = (collected[name].decode("utf-8", errors="replace") for name in ("stdout", "stderr"))
    if process.returncode != 0:
        return ExistingOMP(path, "broken", reason=(stderr or stdout).strip() or f"exit_{process.returncode}")
    version = re.fullmatch(r"omp/([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)", stdout.strip())
    if not version:
        return ExistingOMP(path, "broken", reason="invalid_version_output")
    return ExistingOMP(path, "executable", version.group(1))


@dataclass(frozen=True)
class BinaryRecipe:
    version: str
    target: str
    artifact_name: str
    sha256: str
    size: int
    download_url: str
    metadata_url: str
    method: str = "official_binary"


class RecipeError(ValueError):
    """Machine-readable recipe failure; never contains a signed download URL."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_TARGETS = {
    "linux-x86_64": ("linux", "x86_64", "omp-linux-x64"),
    "linux-arm64": ("linux", "arm64", "omp-linux-arm64"),
    "linux-musl-x86_64": ("linux-musl", "x86_64", "omp-linux-musl-x64"),
    "linux-musl-arm64": ("linux-musl", "arm64", "omp-linux-musl-arm64"),
}


def _https(url: str) -> None:
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.fragment and parsed.port in (None, 443)
                 and (parsed.hostname == "build.stencil.so"
                      or parsed.hostname.endswith(".r2.cloudflarestorage.com")))
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise RecipeError("untrusted_artifact_url")


class _OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_https(url: str, *, timeout: float):
    _https(url)
    return build_opener(_OfficialRedirect()).open(
        Request(url, headers={"User-Agent": "omp-workbench/0.1"}), timeout=timeout)


def _fetch_json(url: str) -> dict:
    with _open_https(url, timeout=30) as response:
        _https(response.geturl())
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise RecipeError("metadata_too_large")
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise RecipeError("invalid_metadata") from exc


def resolve_stable(*, target: str | None = None, fetch_json=None) -> BinaryRecipe:
    """Resolve stable once; this immutable recipe pins CLI/SDK artifact together."""
    target = target or host_target()
    if target not in _TARGETS:
        raise RecipeError("unsupported_target")
    metadata_url = f"https://build.stencil.so/api/products/omp/latest/{target}?channel=stable"
    metadata = (fetch_json or _fetch_json)(metadata_url)
    try:
        artifact = metadata["file"]
        build = metadata["build"]
        expected_platform, expected_arch, expected_name = _TARGETS[target]
        if (not isinstance(build["version"], str)
                or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", build["version"])
                or build["channel"] != "stable"
                or artifact["platform"] != expected_platform or artifact["arch"] != expected_arch
                or artifact["name"] != expected_name
                or not isinstance(artifact["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
                or type(artifact["size"]) is not int or not 0 < artifact["size"] <= 1024**3):
            raise RecipeError("invalid_metadata")
        _https(metadata["download"])
        return BinaryRecipe(build["version"], target, artifact["name"], artifact["sha256"],
                            artifact["size"], metadata["download"], metadata_url)
    except (KeyError, TypeError) as exc:
        raise RecipeError("invalid_metadata") from exc


def host_target(*, system: str | None = None, machine: str | None = None,
                libc: str | None = None) -> str:
    """Official Linux binary target; actual runtime evidence remains host-specific."""
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    if system != "Linux":
        raise RecipeError("unsupported_os")
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if arch is None:
        raise RecipeError("unsupported_architecture")
    if libc is None:
        libc = platform.libc_ver()[0]
        if Path("/etc/alpine-release").exists() or list(Path("/lib").glob("ld-musl-*.so.1")):
            libc = "musl"
    if libc not in {"glibc", "musl"}:
        raise RecipeError("unsupported_libc")
    return f"{'linux-musl' if libc == 'musl' else 'linux'}-{arch}"


def download_verified(recipe: BinaryRecipe, destination: Path, *, open_url=None,
                      timeout: float = 60, total_timeout: float = 900) -> Path:
    """Download into a sibling staging file, then publish the new executable."""
    _https(recipe.download_url)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(str(destination))
    deadline = time.monotonic() + total_timeout
    size = 0
    digest = hashlib.sha256()
    fd, temporary = tempfile.mkstemp(prefix=".omp-download-", dir=destination.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as output, (open_url or _open_https)(recipe.download_url, timeout=min(timeout, total_timeout)) as response:
            _https(response.geturl())
            read = getattr(response, "read1", response.read)
            while chunk := read(64 * 1024):
                if time.monotonic() >= deadline:
                    raise RecipeError("download_timeout")
                size += len(chunk)
                if size > recipe.size:
                    raise RecipeError("artifact_size_mismatch")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if time.monotonic() >= deadline:
            raise RecipeError("download_timeout")
        if size != recipe.size:
            raise RecipeError("artifact_size_mismatch")
        if digest.hexdigest() != recipe.sha256:
            raise RecipeError("artifact_checksum_mismatch")
        temporary.chmod(0o700)
        # link is an atomic no-clobber publication, including a racing symlink.
        os.link(temporary, destination, follow_symlinks=False)
        temporary.unlink()
        parent_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return destination
    finally:
        temporary.unlink(missing_ok=True)
