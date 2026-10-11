"""Private LG-D24 command work-scope probe over the existing OMP contract harness.

The terminal record and selected range are synthetic, trusted fixture inputs;
the OMP tool call, typed result, and backend receipt are actual local runtime events.
"""

from __future__ import annotations

from hashlib import sha256
import io
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from tests.backend.q01_sdk_full_probe import run_contract
from workbench.contracts.logs_v1 import parse_summary_request, summary_input_key
from workbench.storage.important_log import ImportantLogStore, SubjectLifecycle


STAMP = "2026-10-11T00:00:00Z"
END = "2026-10-11T00:00:01Z"


def _sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _health() -> dict:
    return {"schemaVersion": 1, "dbUuid": str(uuid4()), "measuredAt": STAMP,
            "stale": False, "mainBytes": 1, "walBytes": 0, "shmBytes": 0,
            "totalBytes": 1, "logicalLiveBytes": 1, "freelistBytes": 0,
            "subjectStats": [], "cleanupCandidates": None, "lastWrite": None,
            "lastLock": None, "lastMaintenance": None, "diskFreeBytes": None,
            "lastQuickCheck": None, "metadataOutsideBudget": True}


class RawProvider(run_contract.Provider):
    """Capture the actual local HTTP request before the unchanged scripted handler."""

    def __init__(self, private: Path, requests: dict):
        super().__init__(private, requests)
        handler = self.RequestHandlerClass
        provider = self

        class Capture(handler):
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with provider.lock:
                    index = len(list(private.glob("raw-post-*.json"))) + 1
                    (private / f"raw-post-{index:02d}.json").write_bytes(raw)
                self.rfile = io.BytesIO(raw)
                super().do_POST()

        self.RequestHandlerClass = Capture


def run(out: Path) -> dict:
    out = out.resolve()
    if out.exists():
        raise ValueError("refuse to overwrite origin evidence")
    out.mkdir(parents=True, mode=0o700)
    important_dir = out / "actual" / "private" / "important"
    life: SubjectLifecycle | None = None
    db_uuid: str | None = None
    cases: dict[str, dict] = {}
    negative: dict[str, dict] = {}
    original_request = run_contract._request
    original_provider = run_contract.Provider

    def request_factory(_repo, summary: str, skill_sha: str) -> tuple[dict, dict]:
        nonlocal life, db_uuid
        if life is None:
            with ImportantLogStore.create(important_dir) as store:
                db_uuid = store.db_uuid
            life = SubjectLifecycle(important_dir, db_uuid)
        case = "positive" if not cases else "saved_then_bash"
        command_id = str(uuid4())
        subject = {"schemaVersion": 1, "kind": "command", "id": command_id}
        seq = 1 if case == "positive" else 3
        start = {"seq": seq, "at": STAMP, "type": "terminal_started",
                 "command_id": command_id, "task_id": None, "started_at": STAMP}
        life.record_terminal(start)
        raw = summary.encode("utf-8")
        source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
                  "commandId": command_id}
        span = {"schemaVersion": 1, "source": source, "start": 0, "end": len(raw),
                "digest": sha256(raw).hexdigest()}
        life.record_terminal_range(subject, str(uuid4()), span, observed_at=STAMP)
        if case == "positive":
            life.record_terminal({"seq": 2, "at": END, "type": "terminal_ended",
                                  "command_id": command_id, "status": "exited", "ended_at": END})
        closing = case == "positive"
        grant = life.authority(subject, active=not closing, paused=False,
                               revoked=False, has_new_input=True, closing_event=closing)
        if not grant["allowed"]:
            raise AssertionError(f"confirmed command denied: {grant}")
        request = parse_summary_request({"schemaVersion": 1, "requestId": str(uuid4()),
            "subject": subject, "revision": None,
            "authorizationRef": grant["authorization_ref"],
            "authorizationEpoch": grant["authorization_epoch"],
            "inputKey": summary_input_key(subject, None, [span], [], skill_sha),
            "skillName": "q01-private-summary", "skillDigest": skill_sha,
            "sources": [span], "sourceGaps": [], "priorCumulativeEntryId": None,
            "dbHealth": _health(), "requestedAt": STAMP})
        admitted = life.authorize_summary_request(request, active=not closing, paused=False,
            revoked=False, has_new_input=True, closing_event=closing)
        if not admitted["allowed"]:
            raise AssertionError(f"canonical request denied: {admitted}")
        cases[case] = {"command_id": command_id, "terminal_start": start,
                       "selected_confirmed_span": span, "authorization": grant,
                       "admission": admitted, "closing": closing}
        if case == "positive":
            spoof = {**request, "authorizationRef": str(uuid4())}
            wrong_range = {**span, "digest": "0" * 64}
            unrelated = {**request, "sources": [wrong_range],
                         "inputKey": summary_input_key(subject, None, [wrong_range], [], skill_sha)}
            negative.update({
                "wrong_ref": life.authorize_summary_request(spoof, active=False, paused=False,
                    revoked=False, has_new_input=True, closing_event=True),
                "spoofed_digest": life.authorize_summary_request(unrelated, active=False,
                    paused=False, revoked=False, has_new_input=True, closing_event=True),
                "paused": life.authorize_summary_request(request, active=False, paused=True,
                    revoked=False, has_new_input=True, closing_event=True),
                "revoked": life.authorize_summary_request(request, active=False, paused=False,
                    revoked=True, has_new_input=True, closing_event=True),
            })
            unknown = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
            negative["notice_only"] = life.authority(unknown, active=True, paused=False,
                revoked=False, has_new_input=True)
        return request, span

    try:
        # The existing runner calls this fixture factory before reserve/send. The
        # private SDK guard, fixed DB writer and typed backend remain unchanged.
        run_contract._request = request_factory
        run_contract.Provider = RawProvider
        runtime = run_contract.run(out / "actual")
    finally:
        run_contract._request = original_request
        run_contract.Provider = original_provider

    private = out / "actual" / "private"
    raw_posts = []
    for path in sorted(private.glob("raw-post-*.json")):
        raw_posts.append({"path": str(path), "bytes": path.stat().st_size,
                          "sha256": _sha(path)})
    typed = json.loads((private / "typed-positive.json").read_text()) if (private / "typed-positive.json").exists() else None
    life.mark_command_end_summary_processed(cases["positive"]["command_id"])
    post_end = life.authority({"schemaVersion": 1, "kind": "command",
                               "id": cases["positive"]["command_id"]}, active=False,
                              paused=False, revoked=False, has_new_input=True, closing_event=True)
    posts = runtime.get("provider_posts", [])
    checks = {
        "canonical_origin_admitted": all(case["admission"]["allowed"] for case in cases.values()),
        "negative_scopes_denied": all(value["allowed"] is False for value in negative.values()),
        "actual_typed_same_scope": typed is not None and typed["requestId"] == runtime["requests"]["positive"]["requestId"]
            and typed["sources"] == [cases["positive"]["selected_confirmed_span"]],
        "actual_db_only": all(p["tools"] == ["db_summary_save"] for p in posts),
        "raw_http_attributed": len(raw_posts) == len(posts) and
            sorted(item["sha256"] for item in raw_posts) == sorted(p["body_sha"] for p in posts),
        "end_once": post_end["allowed"] is False and post_end["reason"] == "command_summary_scope_expired",
        "contract_runtime_checks": runtime.get("stop_reason") is None and all(runtime.get("checks", {}).values()),
        "process_stopped": not runtime.get("process", {}).get("remaining_owned") and
            runtime.get("process", {}).get("pump_stopped"),
    }
    result = {"invocation": "lg24-alignment-writer01", "source": "synthetic confirmed terminal journal + actual installed OMP contract runtime",
              "candidate_status": "candidate_ready" if all(checks.values()) else "partial",
              "important_db_uuid": db_uuid, "cases": cases, "negative": negative,
              "post_end": post_end, "checks": checks, "raw_posts": raw_posts,
              "actual_runtime": str(out / "actual" / "contract-evidence.json"),
              "process": runtime.get("process"), "omp_version": runtime.get("omp_version"),
              "omp_binary_sha": runtime.get("omp_binary_sha"), "argv": runtime.get("argv"),
              "source_sha256": {str(p): _sha(p) for p in (Path(__file__), Path(run_contract.__file__),
                  Path(__file__).parents[3] / "src/workbench/storage/important_log/lifecycle.py",
                  Path(__file__).parents[3] / "tools/q01_recheck.py")}}
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


if __name__ == "__main__":
    output = run(Path(sys.argv[1]))
    print(json.dumps({"checks": output["checks"], "candidate_status": output["candidate_status"]},
                     ensure_ascii=False))
    raise SystemExit(0 if all(output["checks"].values()) else 2)
