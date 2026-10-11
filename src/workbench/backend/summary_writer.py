"""Fixed DB-only summary writer, invoked once after SDK session validation.

Only the trusted host selects data_root and request ID. The model supplies the
three body arrays through the bound tool, never SQL, paths or identity fields.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
from uuid import UUID

from workbench.backend.paths import DataLayout
from workbench.backend.summary_adapter import validate_result
from workbench.contracts.logs_v1 import canonical_json, decode_json
from workbench.storage.important_log import ImportantLogCatalog
from workbench.tasks.repository import TaskRepository


def _owned(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    if ((not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
            or (not directory and info.st_nlink != 1)):
        raise ValueError("unsafe_summary_storage")


def store_summary(data_root: Path, request_id: str, result: object) -> dict:
    """Never reappend after a durable or uncertain write; no cross-DB retry."""
    if str(UUID(request_id)) != request_id or not data_root.is_absolute():
        raise ValueError("invalid_writer_binding")
    _owned(data_root, directory=True)
    layout = DataLayout(data_root)
    _owned(layout.tasks)
    _owned(layout.important_logs, directory=True)
    with TaskRepository(layout.tasks) as journal:
        record = journal.get_summary_request(request_id)
        if record is None or record["receipt"] is not None:
            return {"saved": False, "reason": "unknown_or_duplicate_request"}
        if record["state"] not in {"sending", "executed_observed", "unknown", "storage_failed"}:
            return {"saved": False, "reason": "request_not_dispatched"}
        request = record["request"]
        parsed = validate_result(request, result)
        body = {"schemaVersion": 1, "facts": parsed["facts"],
                "interpretations": parsed["interpretations"], "unknowns": parsed["unknowns"]}
        catalog = ImportantLogCatalog(layout.important_logs, create=False)
        written = catalog.append(request["subject"], "cumulative", request["sources"], body,
                                 source_gaps=request["sourceGaps"])
        entry_id = (written.get("entry") or {}).get("entryId") or written.get("durableEntryId")
        if entry_id is None:
            return {"saved": False, "reason": written.get("state", "storage_failed"),
                    "writeOutcome": "not_confirmed"}
        try:
            accepted = journal.record_summary_result_receipt(parsed, entry_id)
        except Exception:
            # The important entry exists. Re-running append would duplicate it.
            return {"saved": False, "reason": "receipt_outcome_unknown", "durableEntryId": entry_id,
                    "writeOutcome": "durable", "retryAppend": False}
        if not accepted:
            return {"saved": False, "reason": "duplicate_receipt", "durableEntryId": entry_id,
                    "writeOutcome": "durable", "retryAppend": False}
        return {"saved": True, "entryId": entry_id,
                "maintenanceUnknown": written.get("rolloverStateUnknown", False)}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        return 2
    try:
        result = decode_json(sys.stdin.buffer.read(8193), max_bytes=8192)
        outcome = store_summary(Path(args[0]), args[1], result)
    except Exception as exc:
        outcome = {"saved": False, "reason": type(exc).__name__, "writeOutcome": "unknown"}
    sys.stdout.buffer.write(canonical_json(outcome) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
