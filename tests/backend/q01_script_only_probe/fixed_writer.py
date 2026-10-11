"""Probe-only writer. The database location is fixed beside this private copy."""

import json
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        raise ValueError("one JSON data argument required")
    data = json.loads(sys.argv[1])
    if not isinstance(data, dict) or set(data) != {"summary"}:
        raise ValueError("only summary data accepted")
    summary = data["summary"]
    if not isinstance(summary, str) or not (1 <= len(summary) <= 2000):
        raise ValueError("summary must be 1..2000 characters")
    db = Path(__file__).resolve().parent / "summary.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS summaries (id INTEGER PRIMARY KEY, summary TEXT NOT NULL)")
        connection.execute("INSERT INTO summaries (summary) VALUES (?)", (summary,))
    print('{"saved":true}')
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"saved": False, "error": str(exc)}))
        raise SystemExit(2)
