"""Development-only fail-closed Q01 gate over the full p5 requirements identity.

This is a scoped dependency decision. It never upgrades an older check to a
current full-candidate pass; final validation reruns on the final candidate.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / ".agents/skills/workflow-ledger/scripts"))
sys.path.insert(0, str(ROOT / "tests/backend"))
import workflow_tools as common  # noqa: E402
from summary_probe_support import Q01_ITEMS, collect_projection, same_projection  # noqa: E402

APPROVED_REQUIREMENTS_IDENTITY = "84d8dc604d543fb92116b25b00198ee43dc1b330466403534c377f1791d6eb18"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_q01(*, requirements: dict, plan: dict, checks: list[dict],
                 original_snapshot: dict, current_snapshot: dict,
                 original_projection: dict, current_projection: dict,
                 mode: str) -> dict:
    """Validate observed records; the CLI independently captures current inputs."""
    _require(mode in ("exact", "revalidate"), "unsupported validation mode")
    identity = common.requirements(requirements)
    _require(identity == APPROVED_REQUIREMENTS_IDENTITY,
             "not the approved full p5 requirements identity")
    _require(plan.get("requirements_identity") == identity, "full requirements identity changed")
    gates = {item["id"]: item for item in plan.get("gate_items", [])
             if isinstance(item, dict) and item.get("id") in Q01_ITEMS}
    full_gates = {item["id"]: item for item in requirements["gate_items"]}
    _require(set(gates) == set(Q01_ITEMS), "PLAN Q01 inventory incomplete")
    for item_id in Q01_ITEMS:
        gate = gates[item_id]
        full = full_gates.get(item_id)
        _require(gate.get("owner_ticket") == "DVL-08"
                 and gate.get("required_evidence_level") == "runtime"
                 and full is not None
                 and set(gate.get("acceptance_ids", [])) == set(full["acceptance_ids"])
                 and full["required_evidence_level"] == "runtime",
                 "Q01 gate requirement differs from approved full requirements: " + item_id)
    ticket = next((x for x in plan.get("tickets", []) if x.get("id") == "DVL-08"), None)
    _require(ticket is not None and set(Q01_ITEMS) <= set(ticket.get("required_checks", []))
             and "Q01-validator" in ticket.get("required_checks", []), "DVL-08 check list changed")

    for label, snapshot in (("original", original_snapshot), ("current", current_snapshot)):
        _require(snapshot.get("schema_version") == 1 and snapshot.get("stable") is True
                 and snapshot.get("unknowns") == []
                 and snapshot.get("candidate") == common.digest(snapshot.get("identity")),
                 label + " candidate snapshot is missing, unstable or forged")
    original_id = original_snapshot["candidate"]
    current_id = current_snapshot["candidate"]
    _require(original_projection.get("candidate_id") == original_id,
             "original projection is not bound to original candidate")
    equivalent, projection_issues = same_projection(original_projection, current_projection)
    _require(equivalent, "Q01 projection invalidated: " + "; ".join(projection_issues))
    if mode == "exact":
        _require(current_id == original_id, "exact candidate changed")

    _require(isinstance(checks, list) and bool(checks), "missing Q01 check records")
    observed: dict[str, str] = {}
    for check in checks:
        _require(not common.check_errors(check), "unrecognized shared check schema")
        _require(check["requirements_identity"] == identity,
                 "historical or changed requirements identity")
        _require(check["before"] == original_id and check["after"] == original_id
                 and check["stable"] is True, "stale or moving original candidate")
        _require(check["cwd"] == original_snapshot.get("workspace"), "check workspace mismatch")
        _require(check["exit_code"] == 0 and check["timed_out"] is False,
                 "failed or timed-out check")
        _require(common.log_error(check) is None, "check log missing or changed")
        _require(Path(check["log"]).stat().st_size > 0, "empty check log")
        _require(check["items"], "empty check collection")
        for item in check["items"]:
            item_id = item["id"]
            _require(item_id in Q01_ITEMS, "unowned item in scoped Q01 check")
            _require(item_id not in observed, "duplicate or conflicting Q01 evidence")
            _require(set(item["acceptance_ids"]) == set(full_gates[item_id]["acceptance_ids"]),
                     "item acceptance set changed")
            _require(item["result"] == "passed" and item["unknowns"] == []
                     and item["observed_evidence_level"] == "runtime",
                     "failed, unknown or fixture-only Q01 item: " + item_id)
            observed[item_id] = check["check_id"]
    _require(set(observed) == set(Q01_ITEMS), "missing Q01 runtime item(s)")
    return {"schema_version": 1,
            "kind": "q01_exact_gate" if mode == "exact" else "q01_scoped_revalidation",
            "passed": True, "requirements_identity": identity,
            "original_candidate": original_id, "current_candidate": current_id,
            "projection_digest": current_projection["digest"],
            "check_ids": {name: observed[name] for name in Q01_ITEMS},
            "full_candidate_gate_pass": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--original-snapshot", type=Path, required=True)
    parser.add_argument("--original-projection", type=Path, required=True)
    parser.add_argument("--checks", type=Path, nargs="+", required=True)
    parser.add_argument("--omp", type=Path, required=True)
    parser.add_argument("--omp-bundle-sha256", required=True)
    parser.add_argument("--route-identity", type=Path, required=True)
    parser.add_argument("--mode", choices=("exact", "revalidate"), default="exact")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        _require(args.requirements.resolve() == root / "docs/features/dev-validation-log-retention/requirements-r0.9-p5.json"
                 and args.plan.resolve() == root / "docs/features/dev-validation-log-retention/PLAN.json",
                 "validator must use current approved requirements and PLAN paths")
        original = json.loads(args.original_snapshot.read_text())
        identity = original["identity"]
        current = common.snapshot(root, watch=identity["watch"], exclude=identity["exclude"])
        projection = collect_projection(root, omp_executable=args.omp,
                                        omp_bundle_identity=args.omp_bundle_sha256,
                                        route_identity=json.loads(args.route_identity.read_text()))
        report = validate_q01(
            requirements=json.loads(args.requirements.read_text()),
            plan=json.loads(args.plan.read_text()),
            checks=[json.loads(path.read_text()) for path in args.checks],
            original_snapshot=original, current_snapshot=current,
            original_projection=json.loads(args.original_projection.read_text()),
            current_projection=projection, mode=args.mode)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"passed": False, "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
