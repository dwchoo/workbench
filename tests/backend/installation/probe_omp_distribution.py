"""Credential-free actual OMP distribution / SDK feasibility measurement.

By default resolve and download official stable once. --executable measures an
existing binary; --verify-stable additionally binds it to today's official
metadata without re-downloading. All output lives beneath the fresh run root.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from hashlib import file_digest
import json
import os
from pathlib import Path
import platform

from workbench.backend.install_recipe import download_verified, host_target, inspect_existing, resolve_stable
from tests.backend.installation.probe_support import observe_distribution

ROOT = Path(__file__).resolve().parents[3]


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return file_digest(source, "sha256").hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--verify-stable", action="store_true")
    args = parser.parse_args()
    output = args.run_root.resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    temporary = output / "tmp"
    temporary.mkdir(mode=0o700)
    os.environ["TMPDIR"] = str(temporary)
    report = {"gate": "G-WI-01", "status": "inconclusive", "host": {
        "system": platform.system(), "machine": platform.machine(), "libc": platform.libc_ver()},
        "boundary": "source-checkout feasibility only; no wheel/setup/auth/paid-provider/Q01 certification"}
    try:
        target = host_target()
        report["host"]["target"] = target
        recipe = resolve_stable(target=target) if args.executable is None or args.verify_stable else None
        if recipe:
            report["recipe"] = {k: v for k, v in asdict(recipe).items() if k != "download_url"}
        executable = args.executable.resolve(strict=True) if args.executable else output / "prefix/bin/omp"
        if args.executable is None:
            executable.parent.mkdir(parents=True, mode=0o700)
            download_verified(recipe, executable)
        with executable.open("rb") as source:
            actual_sha = file_digest(source, "sha256").hexdigest()
        report["actual_binary_sha256"] = actual_sha
        existing = inspect_existing(executable)
        report["executable"] = {**asdict(existing), "path": str(existing.path)}
        if existing.status != "executable":
            raise RuntimeError(existing.reason or existing.status)
        if recipe and (actual_sha != recipe.sha256 or executable.stat().st_size != recipe.size
                       or existing.version != recipe.version):
            raise RuntimeError("resolved_stable_artifact_mismatch")
        observation = observe_distribution(executable, output / "observation")
        report["observation"] = observation
        report["source_sha256"] = {str(path.relative_to(ROOT)): digest(path)
            for path in (Path(__file__).resolve(), ROOT / "tests/backend/installation/probe_support.py",
                         ROOT / "tests/backend/installation/probes/wi01/entry_child.ts",
                         ROOT / "omp_bridge/g3/entry.ts", ROOT / "omp_bridge/g3/summary_sdk.ts",
                         ROOT / "tests/backend/q01_product_adapter_probe/composition_extension.ts",
                         ROOT / "tests/backend/q01_product_adapter_probe/runtime.py",
                         ROOT / "src/workbench/backend/install_recipe.py")}
        checks = observation.get("checks", {})
        report["status"] = "passed" if checks and all(checks.values()) and not observation.get("error") else "failed"
    except Exception as error:
        # Signed URLs and user environment content are intentionally not serialized.
        report["error"] = type(error).__name__ + ": " + str(getattr(error, "code", None) or error)
        report["status"] = "inconclusive"
    (output / "probe-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"gate": report["gate"], "status": report["status"], "report": str(output / "probe-result.json")}))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
