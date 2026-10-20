"""Read-only command-line access to project work records."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from workbench.backend.paths import DataDirError, DataLayout
from workbench.backend.project import resolve_project_context
from workbench.storage.work_records.store import RecordStore
from workbench.storage.work_records.exporter import export_record


EXIT_FAILURE = 1


def _human(result: dict, action: str) -> None:
    if result.get("status") == "exported":
        print(f"기록을 내보냈습니다: {result['path']} · revision {result['revision']}")
        return
    if result.get("status") != "ok":
        print(f"기록 조회 실패: {result.get('status')} ({result.get('reason', 'unknown')})",
              file=sys.stderr)
        if result.get("path"):
            print(f"출력 경로: {result['path']}", file=sys.stderr)
        return
    if action == "show":
        record = result["record"]
        print(f"{record['title']} · {record['record_id']} · revision {record['revision']} · {record['recorded_at']}")
        sys.stdout.write(record["body"])
        if not record["body"].endswith("\n"):
            sys.stdout.write("\n")
        return
    key = "revisions" if action == "history" else "records"
    rows = result[key]
    if not rows:
        print("기록이 없습니다.")
    for row in rows:
        print(f"{row['title']} · {row['record_id']} · revision {row['revision']} · {row['recorded_at']}")
        if action != "history" and row.get("preview"):
            print(f"  {row['preview']}{'…' if row['preview_truncated'] else ''}")
    if result.get("next_after"):
        print(f"다음 페이지: {result['next_after']}")


def _emit(args, result: dict) -> int:
    if args.json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    else:
        _human(result, args.records_action)
    return 0 if result.get("status") in {"ok", "exported"} else EXIT_FAILURE


def cmd_records(args) -> int:
    """Resolve current project identity without starting or contacting a backend."""
    try:
        context = resolve_project_context(Path.cwd(), os.environ, args.data_dir)
    except (DataDirError, OSError, ValueError):
        return _emit(args, {"status": "unavailable", "reason": "project_unavailable"})
    if context.needs_initialization:
        return _emit(args, {"status": "unavailable", "reason": "project_not_initialized"})

    store = RecordStore(DataLayout(context.data_root).records)
    if args.records_action == "list":
        result = store.list(task_id=args.task, limit=args.limit, after=args.page)
    elif args.records_action == "search":
        result = store.search(args.query, task_id=args.task, limit=args.limit, after=args.page)
    elif args.records_action == "export":
        result = store.get(args.record_id, args.revision)
        if result["status"] == "ok":
            result = export_record(result["record"], Path(os.path.abspath(args.output)))
    elif args.records_action == "show":
        result = store.get(args.record_id, args.revision)
    else:
        result = store.history(args.record_id, limit=args.limit, after=args.page)
    return _emit(args, result)
