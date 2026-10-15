"""Restart-safe local state and private raw archive, never added to Git."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from . import __version__

ACTIVITY_STATUSES = ("downloaded", "pending", "uploaded", "remote_exists", "failed", "skipped")

# States a later *failure* must never downgrade: an upload whose outcome is
# unknown stays pending (manual reconciliation), a completed upload stays done.
GUARDED_STATUSES = ("pending", "uploaded")

# Retry schedule for activities whose processing raised: quick retries for
# transient errors, then rare ones, so an activity that can never be processed
# (e.g. Garmin has no device file for it) costs one request a week instead of
# aborting every run. The last interval repeats. The first retry comes soon:
# right after the watch syncs, Garmin sometimes answers with a server error for
# an activity it serves fine minutes later.
RETRY_BACKOFF_SECONDS = (600, 3600, 6 * 3600, 24 * 3600, 7 * 86400)


class InstanceBusy(RuntimeError):
    """Another bridge process holds the lock for this scope."""


class Store:
    def __init__(self, base: Path):
        self.base = base
        self.base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(base / "bridge.db"))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS activity (
            garmin_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            sha256 TEXT, intervals_id TEXT, updated REAL NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS wellness (
            day TEXT PRIMARY KEY, fetched REAL NOT NULL, written REAL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, command TEXT NOT NULL, started REAL NOT NULL,
            metrics TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY, value TEXT, updated REAL NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS intervals_seen (
            intervals_id TEXT PRIMARY KEY, external_id TEXT, source TEXT, first_seen REAL NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS enrichment (
            garmin_id TEXT PRIMARY KEY, intervals_id TEXT NOT NULL, sha256 TEXT,
            fields TEXT, streams TEXT, updated REAL NOT NULL)""")
        # Additive schema migration for databases created by v0.1.x.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(activity)")}
        for name, ddl in (("attempts", "INTEGER NOT NULL DEFAULT 0"),
                          ("next_retry", "REAL"), ("error", "TEXT")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE activity ADD COLUMN {name} {ddl}")
        if "written" not in {row[1] for row in self.db.execute("PRAGMA table_info(wellness)")}:
            self.db.execute("ALTER TABLE wellness ADD COLUMN written REAL")   # 0.2.0: fetch and write kept apart
        if "mapping" not in {row[1] for row in self.db.execute("PRAGMA table_info(enrichment)")}:
            self.db.execute("ALTER TABLE enrichment ADD COLUMN mapping TEXT")  # 0.2.0: field definitions used
        self.db.commit()

    def activity_intervals_id(self, garmin_id: str) -> str | None:
        """The Intervals activity the bridge enriched or uploaded for this Garmin activity, if any."""
        for table in ("enrichment", "activity"):
            row = self.db.execute(f"SELECT intervals_id FROM {table} WHERE garmin_id=?", (garmin_id,)).fetchone()
            if row and row[0]:
                return str(row[0])
        return None

    def activity_row(self, garmin_id: str) -> tuple[str | None, str | None]:
        """(status, Intervals id) of the activity table alone – what an upload or swap did."""
        row = self.db.execute("SELECT status, intervals_id FROM activity WHERE garmin_id=?", (garmin_id,)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def activity_status(self, garmin_id: str) -> str | None:
        row = self.db.execute("SELECT status FROM activity WHERE garmin_id=?", (garmin_id,)).fetchone()
        return row[0] if row else None

    def record_activity(self, garmin_id: str, status: str, sha: str | None = None,
                        intervals_id: str | None = None) -> None:
        """Record a non-failure outcome; this also clears any earlier failure bookkeeping."""
        assert status in ACTIVITY_STATUSES and status != "failed"
        self.db.execute("""INSERT INTO activity (garmin_id,status,sha256,intervals_id,updated)
            VALUES (?,?,?,?,?) ON CONFLICT(garmin_id) DO UPDATE SET
            status=excluded.status,sha256=excluded.sha256,
            intervals_id=excluded.intervals_id,updated=excluded.updated,
            attempts=0,next_retry=NULL,error=NULL""",
                        (garmin_id, status, sha, intervals_id, time.time()))
        self.db.commit()

    def record_failure(self, garmin_id: str, error: str) -> int:
        """Mark one activity as failed with exponential backoff; returns the attempt count.

        Guarded states are left untouched so that an upload with an unknown
        outcome is never turned into a retryable failure.
        """
        row = self.db.execute("SELECT status, attempts FROM activity WHERE garmin_id=?",
                              (garmin_id,)).fetchone()
        if row and row[0] in GUARDED_STATUSES:
            return int(row[1])
        attempts = (int(row[1]) if row else 0) + 1
        delay = RETRY_BACKOFF_SECONDS[min(attempts, len(RETRY_BACKOFF_SECONDS)) - 1]
        now = time.time()
        self.db.execute("""INSERT INTO activity (garmin_id,status,sha256,intervals_id,updated,attempts,next_retry,error)
            VALUES (?,'failed',NULL,NULL,?,?,?,?) ON CONFLICT(garmin_id) DO UPDATE SET
            status='failed',updated=excluded.updated,attempts=excluded.attempts,
            next_retry=excluded.next_retry,error=excluded.error""",
                        (garmin_id, now, attempts, now + delay, error[:200]))
        self.db.commit()
        return attempts

    def is_deferred(self, garmin_id: str, now: float | None = None) -> bool:
        """True while a failed activity is still inside its backoff window."""
        row = self.db.execute("SELECT status, next_retry FROM activity WHERE garmin_id=?",
                              (garmin_id,)).fetchone()
        if not row or row[0] != "failed" or row[1] is None:
            return False
        return (now if now is not None else time.time()) < row[1]

    def clear_failure(self, garmin_id: str) -> None:
        """A later success ends the failure bookkeeping; guarded states are not touched."""
        self.db.execute("""UPDATE activity SET status='downloaded', attempts=0, next_retry=NULL, error=NULL,
                           updated=? WHERE garmin_id=? AND status='failed'""", (time.time(), garmin_id))
        self.db.commit()

    def failed_activities(self) -> list[dict]:
        rows = self.db.execute("""SELECT garmin_id, attempts, next_retry, error FROM activity
                                  WHERE status='failed' ORDER BY next_retry""")
        return [{"garmin_id": r[0], "attempts": r[1], "next_retry": r[2], "error": r[3]} for r in rows]

    def wellness_recent(self, day: date, hours: int, since: float | None = None) -> bool:
        """Fetched within `hours`, and (if given) not before `since`.

        `since` lets the caller force one fresh fetch after a boundary: a day
        fetched at 23:00 is still "recent" at 00:30, but its totals only became
        final at midnight, so it must be read once more.
        """
        row = self.db.execute("SELECT fetched FROM wellness WHERE day=?", (day.isoformat(),)).fetchone()
        if not row:
            return False
        if since is not None and row[0] < since:
            return False
        return time.time() - row[0] < hours * 3600

    def mark_wellness(self, day: date, *, written: bool = False) -> None:
        """Record a Garmin fetch of this day; with `written`, that Intervals received its values.

        The two are kept apart so a dry run throttles Garmin like a real run, while the
        first `--apply` can still map the archived snapshot instead of fetching again. A new
        fetch is unwritten until its own write succeeded (a failed write is tried again)."""
        now = time.time()
        if written:
            self.db.execute("INSERT INTO wellness(day,fetched,written) VALUES (?,?,?) "
                            "ON CONFLICT(day) DO UPDATE SET fetched=excluded.fetched, written=excluded.written",
                            (day.isoformat(), now, now))
        else:
            self.db.execute("INSERT INTO wellness(day,fetched,written) VALUES (?,?,NULL) "
                            "ON CONFLICT(day) DO UPDATE SET fetched=excluded.fetched, written=NULL",
                            (day.isoformat(), now))
        self.db.commit()

    def wellness_state(self, day: date) -> tuple[float | None, float | None]:
        """(last Garmin fetch, last Intervals write) as timestamps, None when never."""
        row = self.db.execute("SELECT fetched, written FROM wellness WHERE day=?", (day.isoformat(),)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def load_snapshot(self, day: date) -> dict | None:
        path = self.base / "raw" / f"{day.isoformat()}.json"
        if not path.is_file():
            return None
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None

    def pending_activities(self) -> list[str]:
        return [row[0] for row in self.db.execute("SELECT garmin_id FROM activity WHERE status='pending'")]

    @staticmethod
    def atomic_save(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".partial-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def fit_path(self, garmin_id: str) -> Path:
        # Never put unsanitized Garmin-originated identifiers into paths.
        if not garmin_id.isdecimal():
            raise ValueError("Garmin activity ID must be numeric")
        return self.base / "fits" / f"{garmin_id}.fit"

    def record_run(self, command: str, metrics: dict, started: float | None = None) -> None:
        # which version ran it: the web page may be an older process than the bridge
        self.db.execute("INSERT INTO runs (command, started, metrics) VALUES (?,?,?)",
                        (command, started or time.time(),
                         json.dumps({**metrics, "version": __version__}, default=str)))
        self.db.execute("DELETE FROM runs WHERE started < ?", (time.time() - 30 * 86400,))
        self.db.commit()

    def recent_runs(self, hours: float, command: str | None = None) -> list[dict]:
        rows = self.db.execute("SELECT command, started, metrics FROM runs WHERE started >= ? "
                               + ("AND command = ? " if command else "") + "ORDER BY started",
                               (time.time() - hours * 3600,) + ((command,) if command else ()))
        return [{"command": r[0], "started": r[1], "metrics": json.loads(r[2])} for r in rows]

    def last_run(self, command: str) -> float | None:
        row = self.db.execute("SELECT MAX(started) FROM runs WHERE command=?", (command,)).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def set_meta(self, key: str, value: str = "") -> None:
        self.db.execute("INSERT INTO meta VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                        "updated=excluded.updated", (key, value, time.time()))
        self.db.commit()

    def meta_updated(self, key: str) -> float | None:
        row = self.db.execute("SELECT updated FROM meta WHERE key=?", (key,)).fetchone()
        return float(row[0]) if row else None

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def meta_items(self, pattern: str) -> list[tuple[str, str]]:
        """Bookkeeping keys matching a GLOB `pattern`, with their values."""
        return [(str(k), str(v or "")) for k, v in
                self.db.execute("SELECT key, value FROM meta WHERE key GLOB ?", (pattern,))]

    def delete_meta(self, pattern: str, older_than: float) -> int:
        """Remove bookkeeping keys matching a GLOB `pattern` last touched before `older_than`."""
        cursor = self.db.execute("DELETE FROM meta WHERE key GLOB ? AND updated < ?", (pattern, older_than))
        self.db.commit()
        return cursor.rowcount

    def intervals_seen(self, intervals_id: str) -> bool:
        row = self.db.execute("SELECT 1 FROM intervals_seen WHERE intervals_id=?", (intervals_id,)).fetchone()
        return row is not None

    def intervals_seen_source(self, intervals_id: str) -> str | None:
        """The source an Intervals activity had when the watch last saw it; None if never seen."""
        row = self.db.execute("SELECT source FROM intervals_seen WHERE intervals_id=?", (intervals_id,)).fetchone()
        return None if row is None else str(row[0] or "")

    def last_new_activity(self) -> float | None:
        """When the watch last saw an activity for the first time."""
        row = self.db.execute("SELECT MAX(first_seen) FROM intervals_seen").fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def mark_intervals_seen(self, intervals_id: str, external_id: str | None, source: str | None) -> None:
        """Remember an activity (or its new source and external id); when it was first seen stays."""
        self.db.execute("INSERT INTO intervals_seen VALUES (?,?,?,?) ON CONFLICT(intervals_id) DO UPDATE SET "
                        "external_id=excluded.external_id, source=excluded.source",
                        (intervals_id, external_id, source, time.time()))
        self.db.commit()

    def record_enrichment(self, garmin_id: str, intervals_id: str, sha: str,
                          fields: list[str], streams: list[str], mapping: str | None = None) -> None:
        self.db.execute("""INSERT INTO enrichment (garmin_id,intervals_id,sha256,fields,streams,updated,mapping)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(garmin_id) DO UPDATE SET intervals_id=excluded.intervals_id,
            sha256=excluded.sha256,fields=excluded.fields,streams=excluded.streams,updated=excluded.updated,
            mapping=excluded.mapping""",
                        (garmin_id, intervals_id, sha, json.dumps(sorted(fields)),
                         json.dumps(sorted(streams)), time.time(), mapping))
        self.db.commit()

    def drop_enrichment(self, garmin_id: str) -> None:
        """After a swap: what the bridge wrote went with Garmin's deleted copy."""
        self.db.execute("DELETE FROM enrichment WHERE garmin_id=?", (garmin_id,))
        self.db.commit()

    def enrichment(self, garmin_id: str) -> dict | None:
        row = self.db.execute("SELECT intervals_id, sha256, fields, streams, updated, mapping FROM enrichment "
                              "WHERE garmin_id=?", (garmin_id,)).fetchone()
        if not row:
            return None
        return {"intervals_id": row[0], "sha256": row[1], "fields": json.loads(row[2] or "[]"),
                "streams": json.loads(row[3] or "[]"), "updated": row[4], "mapping": row[5]}

    def partner_path(self, garmin_id: str) -> Path:
        """The copy Intervals holds for this activity, kept next to the original for audits."""
        return self.fit_path(garmin_id).with_suffix(".partner.fit")

    def activity_json_path(self, garmin_id: str) -> Path:
        return self.fit_path(garmin_id).parent.parent / "raw" / "activities" / f"{garmin_id}.json"

    def save_activity_json(self, garmin_id: str, obj: dict) -> Path:
        path = self.activity_json_path(garmin_id)
        self.atomic_save(path, (json.dumps(obj, indent=2, ensure_ascii=False, default=str, allow_nan=False)
                                + "\n").encode())
        return path

    def activity_extras_path(self, garmin_id: str) -> Path:
        return self.activity_json_path(garmin_id).with_suffix(".extras.json")

    def save_activity_extras(self, garmin_id: str, obj: dict) -> Path:
        path = self.activity_extras_path(garmin_id)
        self.atomic_save(path, (json.dumps(obj, indent=2, ensure_ascii=False, default=str, allow_nan=False)
                                + "\n").encode())
        return path

    def wellness_files_path(self, day: date) -> Path:
        """The day's original wellness files from the device, as Garmin's ZIP."""
        return self.base / "wellness-files" / f"{day.isoformat()}.zip"

    def wellness_index_path(self, day: date) -> Path:
        return self.wellness_files_path(day).with_suffix(".json")

    def save_wellness_files(self, day: date, data: bytes | None, index: dict) -> None:
        """ZIP first, index last: an index on disk means the day is mirrored (or has no files)."""
        if data:
            self.atomic_save(self.wellness_files_path(day), data)
        self.atomic_save(self.wellness_index_path(day),
                         (json.dumps(index, indent=2, sort_keys=True, allow_nan=False) + "\n").encode())

    def load_wellness_index(self, day: date) -> dict | None:
        path = self.wellness_index_path(day)
        if not path.is_file():
            return None
        try:
            obj = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None

    def save_account_snapshot(self, obj: dict) -> Path:
        path = self.base / "raw" / "account" / f"{date.today().isoformat()}.json"
        self.atomic_save(path, (json.dumps(obj, indent=2, ensure_ascii=False, default=str, allow_nan=False)
                                + "\n").encode())
        return path

    def save_snapshot(self, day: date, snapshot: dict) -> Path:
        path = self.base / "raw" / f"{day.isoformat()}.json"
        self.atomic_save(path, (json.dumps(snapshot, indent=2, ensure_ascii=False,
                                           default=str, allow_nan=False) + "\n").encode())
        return path

    def close(self) -> None:
        self.db.close()


@contextmanager
def single_instance(data_dir: Path, scopes: tuple[str, ...] = ("activities", "wellness")):
    """Avoid overlapping runs per scope: a long wellness backfill must not block the
    activity watcher. SQLite (WAL) is safe for both to share."""
    try:
        import fcntl

        def take(fh):
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def drop(fh):
            fcntl.flock(fh, fcntl.LOCK_UN)
        busy: tuple = (BlockingIOError,)
    except ImportError:                       # Windows: lock the first byte of the file
        import msvcrt

        def take(fh):
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

        def drop(fh):
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        busy = (OSError,)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    handles = []
    try:
        for scope in scopes:
            lock = (data_dir / f".bridge-{scope}.lock").open("a+")
            handles.append(lock)
            try:
                take(lock)
            except busy as exc:
                raise InstanceBusy(f"Another bridge instance is running ({scope})") from exc
        yield
    finally:
        for lock in handles:
            try:
                drop(lock)
            except OSError:
                pass
            finally:
                lock.close()
