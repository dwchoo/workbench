"""Paginated metadata queries over the append-only work record store."""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
import re
import sqlite3

from workbench.contracts.records_v1 import (
    DEFAULT_LIMIT, MAX_LIMIT, MAX_QUERY_CHARS, PREVIEW_CHARS, RecordContractError,
    parse_record, record_id as validate_record_id,
)
from .store import RecordStoreError, _not_found, _unavailable


_METADATA = """SELECT r.record_id,r.created_at,v.revision,v.title,v.body,v.task_id,
    v.recorded_at,v.actor,v.sequence FROM records r JOIN revisions v USING(record_id)"""
_LATEST = _METADATA + """ JOIN (SELECT record_id,MAX(revision) AS revision FROM revisions GROUP BY record_id) latest
    ON latest.record_id=v.record_id AND latest.revision=v.revision"""
_CURSOR_KEYS = {"v", "scope", "record_id", "recorded_at", "sequence"}
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z")


def _reject(reason: str) -> dict:
    return {"status": "rejected", "reason": reason}


def _validate_list(task_id: object, limit: object) -> str | None:
    if task_id is not None:
        if not isinstance(task_id, str) or not task_id or task_id.strip() != task_id:
            return "invalid_argument"
        try:
            task_id.encode("utf-8")
        except UnicodeError:
            return "invalid_argument"
    if type(limit) is not int:
        return "invalid_argument"
    if limit > MAX_LIMIT:
        return "limit_too_large"
    if limit < 1:
        return "invalid_argument"
    return None


def _scope(action: str, filters: dict) -> str:
    raw = json.dumps({"action": action, "filters": filters}, ensure_ascii=False,
                     sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _encode_cursor(row: sqlite3.Row, scope: str) -> str:
    value = {"v": 1, "scope": scope, "record_id": row["record_id"],
             "recorded_at": row["recorded_at"], "sequence": row["sequence"]}
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(token: object, scope: str) -> tuple[str, int, str] | None:
    if token is None:
        return None
    if type(token) is not str or not 1 <= len(token) <= 512 or "=" in token:
        raise RecordContractError("invalid_cursor")
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"))
        if (type(value) is not dict or set(value) != _CURSOR_KEYS
                or type(value["v"]) is not int or value["v"] != 1
                or value["scope"] != scope
                or type(value["record_id"]) is not str
                or type(value["recorded_at"]) is not str
                or type(value["sequence"]) is not int or value["sequence"] < 1
                or value["sequence"] > 2**63 - 1
                or _TIMESTAMP.fullmatch(value["recorded_at"]) is None):
            raise ValueError("invalid cursor fields")
        validate_record_id(value["record_id"])
        parsed = datetime.fromisoformat(value["recorded_at"].replace("Z", "+00:00"))
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("invalid cursor timestamp")
        canonical = _encode_cursor({
            "record_id": value["record_id"], "recorded_at": value["recorded_at"],
            "sequence": value["sequence"],
        }, scope)
        if canonical != token:
            raise ValueError("noncanonical cursor")
        return value["recorded_at"], value["sequence"], value["record_id"]
    except (ValueError, TypeError, KeyError, UnicodeError, RecordContractError, json.JSONDecodeError) as exc:
        raise RecordContractError("invalid_cursor") from exc


def _preview(row: sqlite3.Row) -> dict:
    record = parse_record({
        "record_id": row["record_id"], "revision": row["revision"],
        "title": row["title"], "body": row["body"], "task_id": row["task_id"],
        "created_at": row["created_at"], "recorded_at": row["recorded_at"],
        "actor": row["actor"],
    })
    body = record["body"]
    return {
        "record_id": record["record_id"], "revision": record["revision"], "title": record["title"],
        "task_id": record["task_id"], "created_at": record["created_at"],
        "recorded_at": record["recorded_at"], "actor": record["actor"],
        "preview": body[:PREVIEW_CHARS], "preview_truncated": len(body) > PREVIEW_CHARS,
    }


def _page(store, *, action: str, filters: dict, result_key: str, query: str,
          arguments: tuple, limit: int, after: str | None,
          missing_record_id: str | None = None) -> dict:
    scope = _scope(action, filters)
    try:
        cursor = _decode_cursor(after, scope)
    except RecordContractError:
        return _reject("invalid_cursor")
    try:
        with store._connection() as connection:
            if connection is None:
                if missing_record_id is not None and cursor is None:
                    return _not_found()
                if cursor is not None:
                    return _reject("invalid_cursor")
                return {"status": "ok", result_key: [], "has_more": False, "next_after": None}
            if cursor is not None:
                recorded_at, sequence, record_id = cursor
                position = connection.execute(
                    "SELECT 1 FROM revisions WHERE record_id=? AND recorded_at=? AND sequence=?",
                    (record_id, recorded_at, sequence)).fetchone()
                if position is None:
                    return _reject("invalid_cursor")
                query += " AND (v.recorded_at<? OR (v.recorded_at=? AND v.sequence<?))"
                arguments += (recorded_at, recorded_at, sequence)
            if (missing_record_id is not None
                    and connection.execute("SELECT 1 FROM records WHERE record_id=?",
                                           (missing_record_id,)).fetchone() is None):
                return _not_found()
            rows = connection.execute(query + " ORDER BY v.recorded_at DESC,v.sequence DESC LIMIT ?",
                                      arguments + (limit + 1,)).fetchall()
            page = rows[:limit]
            more = len(rows) > limit
            return {"status": "ok", result_key: [_preview(row) for row in page],
                    "has_more": more,
                    "next_after": _encode_cursor(page[-1], scope) if more and page else None}
    except (OSError, sqlite3.Error, RecordStoreError, RecordContractError):
        return _unavailable()


def list_records(store, *, task_id: str | None = None, limit: int = DEFAULT_LIMIT,
                 after: str | None = None) -> dict:
    """List each record's latest snapshot as metadata and a bounded preview."""
    invalid = _validate_list(task_id, limit)
    if invalid:
        return _reject(invalid)
    filters = {"task_id": task_id}
    where = " WHERE v.task_id=?" if task_id is not None else ""
    arguments = (task_id,) if task_id is not None else ()
    return _page(store, action="list", filters=filters, result_key="records",
                 query=_LATEST + where, arguments=arguments, limit=limit, after=after)


def search_records(store, query: str, *, task_id: str | None = None, limit: int = DEFAULT_LIMIT,
                   after: str | None = None) -> dict:
    """Search only latest revisions with case-sensitive literal substring matching."""
    invalid = _validate_list(task_id, limit)
    if invalid:
        return _reject(invalid)
    if not isinstance(query, str) or not query:
        return _reject("invalid_argument")
    try:
        query.encode("utf-8")
    except UnicodeError:
        return _reject("invalid_argument")
    if len(query) > MAX_QUERY_CHARS:
        return _reject("query_too_large")
    filters = {"query": query, "task_id": task_id}
    where = " WHERE "
    clauses = ["(instr(v.title,?)>0 OR instr(v.body,?)>0)"]
    arguments: tuple = (query, query)
    if task_id is not None:
        clauses.append("v.task_id=?")
        arguments += (task_id,)
    return _page(store, action="search", filters=filters, result_key="records",
                 query=_LATEST + where + " AND ".join(clauses), arguments=arguments,
                 limit=limit, after=after)


def history_records(store, record_id: str, *, limit: int = DEFAULT_LIMIT,
                    after: str | None = None) -> dict:
    """Return revision metadata, newest recorded revision first."""
    try:
        validate_record_id(record_id)
    except RecordContractError:
        return _reject("invalid_argument")
    invalid = _validate_list(None, limit)
    if invalid:
        return _reject(invalid)
    query = _METADATA + " WHERE r.record_id=?"
    return _page(store, action="history", filters={"record_id": record_id},
                 result_key="revisions", query=query, arguments=(record_id,),
                 limit=limit, after=after, missing_record_id=record_id)
