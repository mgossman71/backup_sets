"""Backup history: incrementally ingests the JSON-lines records written by
backup.sh (and the one-time backfill) into a SQLite index for fast queries."""
import json
import os
import sqlite3
import threading
import time
from datetime import datetime

from .settings import DB_PATH, HISTORY_FILES

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, start TEXT, end TEXT, start_ts REAL, end_ts REAL, day TEXT,
    status TEXT, reason TEXT, trigger TEXT, jobs_completed INTEGER, backfilled INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS runs_start ON runs(start_ts);
CREATE INDEX IF NOT EXISTS runs_day ON runs(day);
CREATE TABLE IF NOT EXISTS jobs (
    run_id TEXT, job_key TEXT, name TEXT, source TEXT, dest TEXT, start TEXT, end TEXT,
    start_ts REAL, end_ts REAL, day TEXT, status TEXT, rc INTEGER, reason TEXT,
    files INTEGER, created INTEGER, deleted INTEGER, transferred_files INTEGER,
    total_size INTEGER, transferred_bytes INTEGER, sent_bytes INTEGER, bytes_per_sec REAL,
    PRIMARY KEY (run_id, job_key)
);
CREATE INDEX IF NOT EXISTS jobs_day ON jobs(day);
CREATE INDEX IF NOT EXISTS jobs_source ON jobs(job_key, start_ts);
CREATE TABLE IF NOT EXISTS ingest_state (path TEXT PRIMARY KEY, inode INTEGER, offset INTEGER);
CREATE TABLE IF NOT EXISTS fs_snapshots (ts REAL, mount TEXT, size INTEGER, used INTEGER, avail INTEGER);
CREATE INDEX IF NOT EXISTS fs_snap_mount ON fs_snapshots(mount, ts);
"""

STAT_FIELDS = ["files", "created", "deleted", "transferred_files", "total_size",
               "transferred_bytes", "sent_bytes", "bytes_per_sec"]


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ts(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


class History:
    def __init__(self):
        self._lock = threading.Lock()
        self.indexing = False
        self.last_ingest = 0.0
        with connect() as conn:
            conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ ingest
    def ingest(self, min_interval=0.0):
        """Read new lines from the history files. Cheap when nothing changed."""
        if min_interval and time.time() - self.last_ingest < min_interval:
            return
        if not self._lock.acquire(blocking=False):
            return  # another thread is ingesting
        try:
            conn = connect()
            for path in HISTORY_FILES:
                self._ingest_file(conn, path)
            conn.close()
            self.last_ingest = time.time()
        finally:
            self.indexing = False
            self._lock.release()

    def _ingest_file(self, conn, path):
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return
        row = conn.execute("SELECT inode, offset FROM ingest_state WHERE path=?", (path,)).fetchone()
        offset = 0
        if row and row["inode"] == st.st_ino and row["offset"] <= st.st_size:
            offset = row["offset"]
        if offset == st.st_size:
            return
        if st.st_size - offset > 5_000_000:
            self.indexing = True
        with open(path, "rb") as f:
            f.seek(offset)
            batch = 0
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # partial line still being written
                offset += len(raw)
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                self._apply(conn, rec)
                batch += 1
                if batch >= 5000:
                    self._save_offset(conn, path, st.st_ino, offset)
                    conn.commit()
                    batch = 0
        self._save_offset(conn, path, st.st_ino, offset)
        conn.commit()

    @staticmethod
    def _save_offset(conn, path, inode, offset):
        conn.execute("INSERT INTO ingest_state(path, inode, offset) VALUES(?,?,?) "
                     "ON CONFLICT(path) DO UPDATE SET inode=excluded.inode, offset=excluded.offset",
                     (path, inode, offset))

    @staticmethod
    def _apply(conn, rec):
        kind = rec.get("type")
        if kind == "run":
            conn.execute(
                "INSERT INTO runs(run_id,start,end,start_ts,end_ts,day,status,reason,trigger,"
                "jobs_completed,backfilled) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET start=excluded.start, end=excluded.end, "
                "start_ts=excluded.start_ts, end_ts=excluded.end_ts, day=excluded.day, "
                "status=excluded.status, reason=excluded.reason, trigger=excluded.trigger, "
                "jobs_completed=excluded.jobs_completed, backfilled=excluded.backfilled",
                (rec["run_id"], rec.get("start"), rec.get("end"), _ts(rec.get("start")),
                 _ts(rec.get("end")), (rec.get("start") or "")[:10], rec.get("status"),
                 rec.get("reason"), rec.get("trigger"), rec.get("jobs_completed"),
                 1 if rec.get("backfilled") else 0))
        elif kind == "job":
            stats = rec.get("stats") or {}
            key = rec.get("source") or rec.get("name") or "?"
            name = rec.get("name") or os.path.basename((rec.get("source") or "").rstrip("/")) or key
            start = rec.get("start")
            conn.execute(
                "INSERT OR IGNORE INTO runs(run_id,start,start_ts,day,status,trigger,backfilled) "
                "VALUES(?,?,?,?, 'running', NULL, ?)",
                (rec["run_id"], start, _ts(start), (start or "")[:10], 1 if rec.get("backfilled") else 0))
            conn.execute(
                "INSERT OR REPLACE INTO jobs(run_id,job_key,name,source,dest,start,end,start_ts,end_ts,"
                "day,status,rc,reason," + ",".join(STAT_FIELDS) + ") VALUES(" + ",".join("?" * 21) + ")",
                (rec["run_id"], key, name, rec.get("source"), rec.get("dest"), start, rec.get("end"),
                 _ts(start), _ts(rec.get("end")), (start or "")[:10], rec.get("status"), rec.get("rc"),
                 rec.get("reason"), *[stats.get(k) for k in STAT_FIELDS]))

    # ----------------------------------------------------------------- queries
    def runs(self, days=30, status=None, only_changes=False, limit=50, offset=0):
        where = ["r.start_ts >= ?"]
        params = [time.time() - days * 86400]
        if status:
            where.append("r.status = ?")
            params.append(status)
        having = "HAVING COALESCE(SUM(j.transferred_bytes),0) > 0 OR r.status != 'success'" if only_changes else ""
        base = (f"FROM runs r LEFT JOIN jobs j ON j.run_id = r.run_id WHERE {' AND '.join(where)} "
                f"GROUP BY r.run_id {having}")
        with connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM (SELECT r.run_id {base})", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT r.*, SUM(j.transferred_bytes) AS transferred_bytes, "
                f"SUM(j.transferred_files) AS transferred_files, COUNT(j.run_id) AS jobs "
                f"{base} ORDER BY r.start_ts DESC LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        return total, [self._run_dict(r) for r in rows]

    def run_detail(self, run_id):
        with connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            jobs = conn.execute("SELECT * FROM jobs WHERE run_id=? ORDER BY start_ts", (run_id,)).fetchall()
        if not run:
            return None
        d = self._run_dict(run)
        d["jobs"] = [dict(j) for j in jobs]
        return d

    def latest_runs(self, n=1, include_skipped=False):
        cond = "" if include_skipped else "WHERE status != 'skipped'"
        with connect() as conn:
            rows = conn.execute(f"SELECT * FROM runs {cond} ORDER BY start_ts DESC LIMIT ?", (n,)).fetchall()
        return [self._run_dict(r) for r in rows]

    def last_with_status(self, status):
        with connect() as conn:
            r = conn.execute("SELECT * FROM runs WHERE status=? ORDER BY start_ts DESC LIMIT 1",
                             (status,)).fetchone()
        return self._run_dict(r) if r else None

    def daily(self, days=30):
        cutoff = time.time() - days * 86400
        with connect() as conn:
            runs = conn.execute(
                "SELECT day, COUNT(*) n, SUM(status='success') ok, SUM(status='failed') failed, "
                "SUM(status='skipped') skipped, SUM(status NOT IN ('success','failed','skipped')) other, "
                "MAX(CASE WHEN status='success' THEN end_ts - start_ts END) max_duration "
                "FROM runs WHERE start_ts >= ? GROUP BY day ORDER BY day", (cutoff,)).fetchall()
            jobs = {r["day"]: dict(r) for r in conn.execute(
                "SELECT day, SUM(transferred_bytes) bytes, SUM(transferred_files) files "
                "FROM jobs WHERE start_ts >= ? GROUP BY day", (cutoff,))}
        out = []
        for r in runs:
            d = dict(r)
            j = jobs.get(r["day"], {})
            d["bytes"] = j.get("bytes") or 0
            d["files"] = j.get("files") or 0
            out.append(d)
        return out

    def summary(self, days=30):
        cutoff = time.time() - days * 86400
        with connect() as conn:
            r = conn.execute(
                "SELECT COUNT(*) n, SUM(status='success') ok, SUM(status='failed') failed, "
                "SUM(status='skipped') skipped FROM runs WHERE start_ts >= ?", (cutoff,)).fetchone()
            j = conn.execute("SELECT SUM(transferred_bytes) b, SUM(transferred_files) f "
                             "FROM jobs WHERE start_ts >= ?", (cutoff,)).fetchone()
        return {"runs": r["n"] or 0, "success": r["ok"] or 0, "failed": r["failed"] or 0,
                "skipped": r["skipped"] or 0, "bytes": j["b"] or 0, "files": j["f"] or 0}

    def latest_by_source(self):
        """Most recent successful job per source (size, files, when)."""
        with connect() as conn:
            rows = conn.execute(
                "SELECT j.* FROM jobs j JOIN (SELECT job_key, MAX(start_ts) m FROM jobs "
                "WHERE status='success' AND total_size IS NOT NULL GROUP BY job_key) x "
                "ON j.job_key = x.job_key AND j.start_ts = x.m").fetchall()
        return {r["job_key"]: dict(r) for r in rows}

    def source_growth(self, days=90, sources=None):
        """Daily max total_size per source -> {source: [[day, bytes], ...]}"""
        cutoff = time.time() - days * 86400
        with connect() as conn:
            rows = conn.execute(
                "SELECT job_key, day, MAX(total_size) size FROM jobs WHERE status='success' "
                "AND total_size IS NOT NULL AND start_ts >= ? GROUP BY job_key, day ORDER BY day",
                (cutoff,)).fetchall()
        out = {}
        for r in rows:
            if sources is not None and r["job_key"] not in sources:
                continue
            out.setdefault(r["job_key"], []).append([r["day"], r["size"]])
        return out

    def source_growth_rate(self, source, days=30):
        """Bytes/day growth of a source's total size over the last `days` days."""
        series = self.source_growth(days=days, sources={source}).get(source, [])
        if len(series) < 2:
            return None
        d0 = datetime.fromisoformat(series[0][0])
        d1 = datetime.fromisoformat(series[-1][0])
        span = (d1 - d0).days
        if span < 3:
            return None
        return (series[-1][1] - series[0][1]) / span

    @staticmethod
    def _run_dict(r):
        d = dict(r)
        if d.get("start_ts") and d.get("end_ts"):
            d["duration"] = d["end_ts"] - d["start_ts"]
        else:
            d["duration"] = None
        return d
