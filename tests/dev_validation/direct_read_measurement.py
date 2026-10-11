"""Synthetic feasibility check for LG-D18 direct active SQLite reads."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from uuid import uuid4

from workbench.storage.important_log import ImportantLogCatalog, ImportantLogStore


OUT = (Path(__file__).resolve().parents[2] / ".workflow/dev-validation-log-retention/runs/"
       "dvl07-direct-20261010/impl01")


def content(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def identity(path: Path) -> dict | None:
    if not path.exists():
        return None
    info = path.stat()
    return {"dev": info.st_dev, "ino": info.st_ino, "owner": info.st_uid,
            "mode": stat.S_IMODE(info.st_mode), "links": info.st_nlink}


def scenario(root: Path, name: str, *, wal: bool, stale_shm: bool = False) -> dict:
    directory = root / name
    catalog = ImportantLogCatalog(directory, create=True)
    active = catalog.active_uuid()
    main = directory / f"{active}.sqlite3"
    wal_path = directory / f"{active}.sqlite3-wal"
    shm_path = directory / f"{active}.sqlite3-shm"
    writer = None
    latest = None
    if wal:
        writer = sqlite3.connect(main, isolation_level=None)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE synthetic_probe (payload BLOB NOT NULL)")
        latest = "latest-" + uuid4().hex
        writer.execute("INSERT INTO synthetic_probe(payload) VALUES(?)", (latest,))
    if stale_shm and shm_path.exists():
        # An intentionally stale wal-index on a disposable fixture.  The WAL
        # stays intact; ro recovery is allowed to fail closed.
        size = shm_path.stat().st_size
        with shm_path.open("r+b") as target:
            target.write(bytes(size))
    before_main, before_wal = content(main), content(wal_path)
    before_names = sorted(path.name for path in directory.iterdir())
    expected_db_count = len(list(directory.glob("*.sqlite3")))
    observation = {"beforeNames": before_names, "walPresentBefore": wal_path.exists(),
                   "shmPresentBefore": shm_path.exists(), "staleShmInjected": stale_shm}
    try:
        with catalog._lease():
            with ImportantLogStore.open(directory, active, readonly=True) as store:
                assert store._connection.execute("PRAGMA query_only").fetchone()[0] == 1
                observed = (store._connection.execute(
                    "SELECT payload FROM synthetic_probe ORDER BY rowid DESC LIMIT 1").fetchone()[0]
                    if wal else store._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
                observation["queryResult"] = observed
                observation["readSucceeded"] = True
                observation["openedSidecars"] = {suffix: identity(directory / f"{active}.sqlite3-{suffix}")
                                                 for suffix in ("wal", "shm")}
    except Exception as exc:
        observation["readSucceeded"] = False
        observation["error"] = {"type": type(exc).__name__, "message": str(exc)}
    observation["latestWalReflected"] = observed == latest if observation["readSucceeded"] and wal else None
    observation["mainUnchanged"] = before_main == content(main)
    observation["walUnchanged"] = before_wal == content(wal_path)
    observation["walCreatedBytes"] = (wal_path.stat().st_size if before_wal is None and wal_path.exists()
                                      else None)
    observation["existingWalContentUnchanged"] = before_wal is None or before_wal == content(wal_path)
    observation["newDbCount"] = len(list(directory.glob("*.sqlite3"))) - expected_db_count
    observation["afterNames"] = sorted(path.name for path in directory.iterdir())
    sidecars = [value for value in observation.get("openedSidecars", {}).values() if value is not None]
    observation["openedSidecarsPrivate"] = all(
        value["owner"] == os.geteuid() and value["mode"] == 0o600 and value["links"] == 1
        for value in sidecars)
    if writer is not None:
        writer.close()
    try:
        reopened = ImportantLogCatalog(directory)
        observation["writerReopenSucceeded"] = reopened.active_uuid() == active
    except Exception as exc:
        observation["writerReopenSucceeded"] = False
        observation["writerReopenError"] = {"type": type(exc).__name__, "message": str(exc)}
    return observation


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    result = {"cases": {}, "fixtureCleaned": False}
    try:
        with tempfile.TemporaryDirectory(dir=OUT, prefix="direct-feasibility-") as temporary:
            root = Path(temporary)
            for name, wal, stale in (("sidecars_absent", False, False),
                                     ("latest_wal", True, False),
                                     ("stale_shm", True, True)):
                result["cases"][name] = scenario(root, name, wal=wal, stale_shm=stale)
    except Exception as exc:
        result["harnessError"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        result["fixtureCleaned"] = not any(OUT.glob("direct-feasibility-*"))
        path = OUT / "feasibility-detail.json"
        path.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
        print(json.dumps({"result": str(path), "harnessError": result.get("harnessError")}, sort_keys=True))


if __name__ == "__main__":
    main()
