"""Bounded maintenance of the active important-log database only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
import time
from typing import Any

from workbench.contracts.logs_v1 import canonical_json, decode_json
from .store import ImportantLogStore, ImportantLogStoreError

TARGET_BYTES = 250_000_000
LOW_WATER_BYTES = 225_000_000
RETENTION_DAYS = 120
MAX_DELETE_ROWS = 100
MAX_VACUUM_PAGES = 256
CHECK_INTERVAL = timedelta(hours=24)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("aware UTC time required")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


class ImportantLogMaintenance:
    """No archive writes; each prune call deletes at most 100 entry rows."""

    def __init__(self, store: ImportantLogStore, *, target_bytes: int = TARGET_BYTES,
                 low_water_bytes: int = LOW_WATER_BYTES):
        if store.readonly:
            raise ImportantLogStoreError("maintenance needs an active writer")
        if not (type(target_bytes) is int and type(low_water_bytes) is int
                and 0 < low_water_bytes < target_bytes):
            raise ValueError("invalid decimal logical allocation targets")
        self.store = store
        self.target_bytes = target_bytes
        self.low_water_bytes = low_water_bytes
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        conn = self.store._connection
        conn.execute("""CREATE TABLE IF NOT EXISTS important_maintenance_v1
            (id INTEGER PRIMARY KEY CHECK(id=1), last_quick_check TEXT,
             last_maintenance TEXT, last_result TEXT,
             pressure_active INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("INSERT OR IGNORE INTO important_maintenance_v1(id) VALUES(1)")
        # The pressure fallback walks intermediate rows in time order; a
        # partial index keeps its LIMIT bounded as retained history grows.
        conn.execute("""CREATE INDEX IF NOT EXISTS entries_intermediate_by_time_v1
            ON entries(recorded_at,entry_id) WHERE kind='intermediate'""")

    def pressure_active(self) -> bool:
        return bool(self.store._connection.execute(
            "SELECT pressure_active FROM important_maintenance_v1 WHERE id=1").fetchone()[0])

    def logical_live_bytes(self) -> int:
        conn = self.store._connection
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        size = conn.execute("PRAGMA page_size").fetchone()[0]
        return (pages - free) * size

    def _lifecycle_exists(self) -> bool:
        return self.store._connection.execute("""SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='subject_lifecycle_v1'""").fetchone() is not None

    def _candidate_rows(self, *, now: datetime, pressure: bool, limit: int) -> tuple[str, list[sqlite3.Row]]:
        if not self._lifecycle_exists():
            return "none", []
        conn = self.store._connection
        cutoff = _stamp(_utc(now) - timedelta(days=RETENTION_DAYS))
        # Pick the oldest eligible subject first, then use entries_by_subject
        # to fetch only this pass's entry budget. Sorting the entire joined
        # backlog before LIMIT grows linearly with entries retained.
        def closed_rows(*, expired: bool) -> list[sqlite3.Row]:
            deadline = " AND julianday(s.archival_at)<=julianday(?)" if expired else ""
            parameters: tuple[Any, ...] = (cutoff,) if expired else ()
            subject = conn.execute("""SELECT s.subject_kind,s.subject_id
                FROM subject_lifecycle_v1 s JOIN subject_stats st
                ON st.subject_kind=s.subject_kind AND st.subject_id=s.subject_id
                WHERE s.archival_state='closed' AND s.archival_at IS NOT NULL
                AND st.entry_count>0""" + deadline + """
                ORDER BY julianday(s.archival_at),s.subject_kind,s.subject_id LIMIT 1""",
                parameters).fetchone()
            if subject is None:
                return []
            return conn.execute("""SELECT e.entry_id,e.subject_kind,e.subject_id,e.kind,e.recorded_at,
                    e.sources_json,e.source_gaps_json,e.stored_bytes
                    FROM entries e JOIN subject_lifecycle_v1 s
                    ON e.subject_kind=s.subject_kind AND e.subject_id=s.subject_id
                    WHERE e.subject_kind=? AND e.subject_id=? AND s.archival_state='closed'
                    ORDER BY e.recorded_at,e.entry_id LIMIT ?""",
                    (subject["subject_kind"], subject["subject_id"], limit)).fetchall()

        rows = closed_rows(expired=True)
        if rows:
            return "expired", rows
        if not pressure:
            return "none", []
        rows = closed_rows(expired=False)
        if rows:
            return "closed_pressure", rows
        # An open subject retains its start and all cumulative entries. The
        # latter is conservative about which cumulative is latest, and avoids
        # making a stale lifecycle row a deletion authority.
        rows = conn.execute("""SELECT e.entry_id,e.subject_kind,e.subject_id,e.kind,e.recorded_at,
                   e.sources_json,e.source_gaps_json,e.stored_bytes
            FROM entries e LEFT JOIN subject_lifecycle_v1 s
            ON e.subject_kind=s.subject_kind AND e.subject_id=s.subject_id
            WHERE e.kind='intermediate' AND COALESCE(s.archival_state,'open')='open'
            ORDER BY e.recorded_at,e.entry_id LIMIT ?""", (limit,)).fetchall()
        return ("open_intermediate", rows) if rows else ("none", [])

    def candidate_count(self, *, now: datetime, pressure: bool) -> int:
        if not self._lifecycle_exists():
            return 0
        conn = self.store._connection
        cutoff = _stamp(_utc(now) - timedelta(days=RETENTION_DAYS))

        def closed_count(*, expired: bool) -> int:
            deadline = " AND julianday(s.archival_at)<=julianday(?)" if expired else ""
            parameters: tuple[Any, ...] = (cutoff,) if expired else ()
            counts = conn.execute("""SELECT st.entry_count FROM subject_lifecycle_v1 s
                JOIN subject_stats st ON st.subject_kind=s.subject_kind AND st.subject_id=s.subject_id
                WHERE s.archival_state='closed' AND s.archival_at IS NOT NULL
                AND st.entry_count>0""" + deadline + """
                ORDER BY julianday(s.archival_at),s.subject_kind,s.subject_id LIMIT 101""",
                parameters)
            return min(101, sum(row[0] for row in counts))

        expired = closed_count(expired=True)
        if expired or not pressure:
            return expired
        closed = closed_count(expired=False)
        if closed:
            return closed
        _, rows = self._candidate_rows(now=now, pressure=True, limit=101)
        return len(rows)

    def prune_pass(self, *, now: datetime, pressure: bool) -> dict[str, Any]:
        """One bounded transaction. Gap entries record every evicted source span."""
        now = _utc(now)
        conn = self.store._connection
        mode, rows = self._candidate_rows(now=now, pressure=pressure, limit=MAX_DELETE_ROWS)
        if not rows:
            conn.execute("UPDATE important_maintenance_v1 SET pressure_active=0 WHERE id=1")
            return {"mode": "none", "deleted": 0, "remaining": False,
                    "logicalLiveBytes": self.logical_live_bytes()}
        deleted = 0
        with self.store.transaction():
            for row in rows:
                subject = {"schemaVersion": 1, "kind": row["subject_kind"], "id": row["subject_id"]}
                if mode == "open_intermediate":
                    sources = decode_json(row["sources_json"], max_bytes=1_000_000)
                    earlier = decode_json(row["source_gaps_json"], max_bytes=1_000_000)
                    gaps = [*earlier, *({"schemaVersion": 1, "source": span["source"],
                                          "start": span["start"], "end": span["end"],
                                          "reason": "evicted"} for span in sources)]
                    if len(gaps) > 128 or len(canonical_json(gaps)) > 500_000:
                        continue
                    marker = self.store.make_entry(subject, "gap", [],
                        {"schemaVersion": 1, "facts": [f"retention evicted entry {row['entry_id']}"],
                         "interpretations": [], "unknowns": []},
                        source_gaps=gaps, recorded_at=row["recorded_at"])
                    self.store.put_entry(marker)
                conn.execute("DELETE FROM entries WHERE entry_id=?", (row["entry_id"],))
                conn.execute("""UPDATE subject_stats SET entry_count=entry_count-1,
                    stored_bytes=stored_bytes-? WHERE subject_kind=? AND subject_id=?""",
                    (row["stored_bytes"], row["subject_kind"], row["subject_id"]))
                conn.execute("""DELETE FROM subject_stats WHERE subject_kind=? AND subject_id=?
                    AND entry_count=0""", (row["subject_kind"], row["subject_id"]))
                deleted += 1
            conn.execute("""UPDATE important_maintenance_v1 SET last_maintenance=?,last_result=?
                WHERE id=1""", (_stamp(now), f"{mode}:{deleted}"))
        more = self._candidate_rows(now=now, pressure=pressure, limit=1)[1]
        live = self.logical_live_bytes()
        remaining = bool(more) and deleted > 0
        conn.execute("UPDATE important_maintenance_v1 SET pressure_active=? WHERE id=1",
                     (int(pressure and remaining and live > self.low_water_bytes),))
        return {"mode": mode, "deleted": deleted, "remaining": remaining,
                "logicalLiveBytes": live, "unprunable": bool(more) and not deleted}

    def quick_check(self, *, now: datetime, force: bool = False,
                    deadline_seconds: float = 2.0) -> dict[str, Any]:
        now = _utc(now)
        conn = self.store._connection
        last = conn.execute("SELECT last_quick_check FROM important_maintenance_v1 WHERE id=1").fetchone()[0]
        if not force and last is not None:
            checked = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if timedelta(0) <= now - checked < CHECK_INTERVAL:
                return {"status": "cached", "checkedAt": last}
        deadline = time.monotonic() + deadline_seconds
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            result = conn.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            return {"status": "incomplete", "error": type(exc).__name__, "checkedAt": last}
        finally:
            conn.set_progress_handler(None, 0)
        if result != "ok":
            return {"status": "corrupt", "error": str(result)[:128], "checkedAt": last}
        checked_at = _stamp(now)
        conn.execute("UPDATE important_maintenance_v1 SET last_quick_check=? WHERE id=1", (checked_at,))
        self.store._last_quick_check = checked_at
        return {"status": "ok", "checkedAt": checked_at}

    def maintenance_pass(self, *, now: datetime, pressure: bool = False) -> dict[str, Any]:
        """Checkpoint and shrink are separate bounded, lower priority steps."""
        result: dict[str, Any] = {"prune": self.prune_pass(now=now, pressure=pressure)}
        conn = self.store._connection
        deadline = time.monotonic() + 2.0
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            checkpoint_status = ("busy" if checkpoint[0] else
                                 "incomplete" if checkpoint[1] > checkpoint[2] else "ok")
            result["checkpoint"] = {"busy": bool(checkpoint[0]), "logPages": checkpoint[1],
                                     "checkpointedPages": checkpoint[2],
                                     "status": checkpoint_status}
            if checkpoint_status != "ok":
                # Expose the last attempted maintenance outcome through the
                # public HealthSnapshot instead of leaving a prior prune result
                # to imply that this pass completed cleanly.
                conn.execute("""UPDATE important_maintenance_v1
                    SET last_maintenance=?,last_result=? WHERE id=1""",
                    (_stamp(now), f"{checkpoint_status}:checkpoint"))
        except sqlite3.DatabaseError as exc:
            result["checkpoint"] = {"status": "incomplete", "error": type(exc).__name__}
            conn.execute("""UPDATE important_maintenance_v1
                SET last_maintenance=?,last_result=? WHERE id=1""",
                (_stamp(now), "incomplete:checkpoint"))
        try:
            conn.execute(f"PRAGMA incremental_vacuum({MAX_VACUUM_PAGES})")
            result["shrink"] = {"status": "attempted", "maxPages": MAX_VACUUM_PAGES}
        except sqlite3.DatabaseError as exc:
            result["shrink"] = {"status": "incomplete", "error": type(exc).__name__}
        finally:
            conn.set_progress_handler(None, 0)
        if result["checkpoint"].get("status") == "ok":
            if result["shrink"].get("status") == "incomplete":
                last_result = "incomplete:shrink"
            else:
                last_result = f"{result['prune']['mode']}:{result['prune']['deleted']}"
            conn.execute("""UPDATE important_maintenance_v1
                SET last_maintenance=?,last_result=? WHERE id=1""",
                (_stamp(now), last_result))
        return result
