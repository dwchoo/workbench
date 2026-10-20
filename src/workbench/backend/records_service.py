"""Host-bound explicit work record operations, separate from execution/handoff."""
from __future__ import annotations

from pathlib import Path
import json
import os
import sqlite3
import stat

from workbench.backend.flow import HandoffService
from workbench.backend.paths import DataLayout
from workbench.contracts.records_v1 import DEFAULT_LIMIT, MAX_CONTEXT_BYTES, MAX_RESPONSE_BYTES, RECENT_LIMIT, RecordContractError, parse_request
from workbench.storage.work_records.store import RecordStore
from workbench.storage.work_records.exporter import export_record
from workbench.tasks.repository import TaskRepository


class RecordService:
    """The authenticated G3 peer supplies role/session; model args supply content only."""
    def __init__(self, layout: DataLayout, *, project_dir: str | Path):
        self.layout = layout
        self.project_dir = Path(project_dir)

    def handle(self, role, request: dict) -> dict:
        result = self._handle(role, request)
        # G3 escapes non-ASCII in its JSON frame. Never turn an oversized body
        # into a successful truncated snapshot; the stored original stays intact.
        if len(json.dumps(result, allow_nan=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
            return {"status": "unavailable", "reason": "bridge_response_bound"}
        return result

    def _handle(self, role, request: dict) -> dict:
        parsed = HandoffService._parse(role, request)
        if isinstance(parsed, dict):
            return parsed
        if parsed.tool != "workbench_records":
            return {"status": "rejected", "reason": "unknown_tool"}
        try:
            args = parse_request(parsed.args)
        except RecordContractError:
            return {"status": "rejected", "reason": "invalid_arguments"}
        if args["action"] in {"create", "revise"}:
            return self._write(args, parsed.role.value)
        store = RecordStore(self.layout.records)
        if args["action"] == "show":
            return store.get(args["record_id"], args["revision"])
        if args["action"] == "export":
            if args["output_path"] is None:
                return {"status": "needs_output_path", "reason": "needs_output_path"}
            result = store.get(args["record_id"], args["revision"])
            if result["status"] != "ok":
                return result
            path = Path(args["output_path"])
            if not path.is_absolute():
                path = self.project_dir / path
            return export_record(result["record"], Path(os.path.abspath(path)))
        if args["action"] == "recent":
            return self._recent(store)
        page = dict(limit=args["limit"] or DEFAULT_LIMIT, after=args["after"])
        if args["action"] == "list":
            return store.list(task_id=args["task_id"], **page)
        if args["action"] == "search":
            return store.search(args["query"], task_id=args["task_id"], **page)
        if args["action"] == "history":
            return store.history(args["record_id"], **page)
        return {"status": "rejected", "reason": "unknown_action"}

    def _write(self, args: dict, actor: str) -> dict:
        repository = None
        try:
            if args["task_id"] is not None:
                # Open only an existing host Task DB, in this request's thread.
                # Taskless notes never create or open metadata as a side effect.
                info = self.layout.tasks.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("host task database unavailable")
                repository = TaskRepository(self.layout.tasks)
            store = RecordStore(self.layout.records, task_repository=repository)
            snapshot = dict(title=args["title"], body=args["body"], task_id=args["task_id"], actor=actor)
            if args["action"] == "create":
                return store.create(**snapshot)
            return store.revise(args["record_id"], expected_revision=args["expected_revision"], **snapshot)
        except (OSError, sqlite3.Error, ValueError):
            return {"status": "unavailable", "reason": "task_lookup_unavailable"}
        finally:
            if repository is not None:
                repository.close()

    @staticmethod
    def _recent(store: RecordStore) -> dict:
        page = store.list(limit=RECENT_LIMIT)
        if page["status"] != "ok":
            return page
        result = {"status": "ok", "records": page["records"], "has_more": page["has_more"],
                  "omitted": page["has_more"], "context_truncated": False}
        # Match G3's escaped-ASCII JSON size; this also bounds UTF-8 JSON clients.
        # Drop metadata entries explicitly rather than changing a saved full body.
        while len(json.dumps(result).encode("utf-8")) > MAX_CONTEXT_BYTES:
            result["records"].pop()
            result.update(has_more=True, omitted=True, context_truncated=True)
        return result
