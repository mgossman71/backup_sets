#!/usr/bin/env python3
"""
One-time (re-runnable) backfill of backup history from the text log.

Parses /var/log/backup_sets.log (written by backup.sh) and emits the same
JSON-lines records that backup.sh now writes natively, into a separate file
(default /var/lib/backup_sets/history_backfill.jsonl) which the portal ingests.

Runs that were written by the new backup.sh (they log a "RUN_ID:" line) are
skipped, since they already have native history records.

Usage:  python3 backfill_history.py [--log FILE] [--out FILE]
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (.*)$")
PREFIX_RE = re.compile(r"^\[[^\]\s]+\] ")  # "[Set1] " prefix used by the old concurrent version
STAT_LABELS = {
    "Number of files": "files",
    "Number of created files": "created",
    "Number of deleted files": "deleted",
    "Number of regular files transferred": "transferred_files",
    "Total file size": "total_size",
    "Total transferred file size": "transferred_bytes",
}
NUM_RE = re.compile(r"^\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
SENT_RE = re.compile(r"^sent ([0-9,]+) bytes\s+received [0-9,]+ bytes\s+([0-9,.]+) bytes/sec")
START_JOB_RE = re.compile(r"^Starting backup: (.+?)(?: -> (.+))?$")


def to_num(s):
    s = s.replace(",", "")
    return float(s) if "." in s else int(s)


def iso(ts):
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").astimezone().isoformat()


class Run:
    def __init__(self, ts, n):
        self.run_id = "bf-" + ts.replace("-", "").replace(":", "").replace(" ", "T") + f"-{n}"
        self.start = ts
        self.end = ts
        self.jobs = []
        self.job = None  # current job dict
        self.source = None
        self.dest = None

    def emit(self, out, status, reason=None):
        for j in self.jobs:
            out.write(json.dumps(j) + "\n")
        out.write(json.dumps({
            "type": "run", "run_id": self.run_id, "start": iso(self.start), "end": iso(self.end),
            "status": status, "reason": reason, "trigger": "cron", "config": None,
            "jobs_completed": sum(1 for j in self.jobs if j["status"] == "success"),
            "backfilled": True,
        }) + "\n")


def new_job(run, ts, name):
    job = {
        "type": "job", "run_id": run.run_id, "name": name, "source": None, "dest": None,
        "start": iso(ts), "end": None, "status": "incomplete", "rc": None, "reason": None,
        "stats": {k: None for k in list(STAT_LABELS.values()) + ["sent_bytes", "bytes_per_sec"]},
    }
    run.jobs.append(job)
    run.job = job
    return job


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="/var/log/backup_sets.log")
    ap.add_argument("--out", default="/var/lib/backup_sets/history_backfill.jsonl")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    tmp = args.out + ".tmp"
    counts = {"success": 0, "failed": 0, "skipped": 0, "incomplete": 0, "native_skipped": 0}
    n = 0
    pending = None   # run that has started but not yet taken the running flag
    active = None    # run that holds the running flag (does the rsync work)
    saw_run_id = False
    last_ts = None

    with open(args.log, "r", errors="replace") as f, open(tmp, "w") as out:
        def finish(run, status, reason=None):
            run.end = last_ts or run.end
            run.emit(out, status, reason)
            counts[status] += 1

        for raw in f:
            line = raw.rstrip("\n")
            m = TS_RE.match(line)
            if not m:
                # untimestamped line: rsync output belonging to the active job
                if active is None or active.job is None:
                    continue
                body = PREFIX_RE.sub("", line)
                if ":" in body[:40]:
                    label, _, rest = body.partition(":")
                    key = STAT_LABELS.get(label)
                    if key:
                        nm = NUM_RE.match(rest)
                        if nm:
                            active.job["stats"][key] = to_num(nm.group(1))
                        continue
                sm = SENT_RE.match(body)
                if sm:
                    active.job["stats"]["sent_bytes"] = to_num(sm.group(1))
                    active.job["stats"]["bytes_per_sec"] = to_num(sm.group(2))
                continue

            ts, msg = m.group(1), m.group(2)
            last_ts = ts
            msg = PREFIX_RE.sub("", msg) if msg.startswith("[") else msg

            if msg.startswith("RUN_ID:"):
                saw_run_id = True
                continue

            if msg == "Backup script started":
                if pending is not None:  # previous pending run never resolved
                    finish(pending, "incomplete", "Run ended without a result in the log")
                n += 1
                pending = Run(ts, n)
                if saw_run_id:  # new-style run: already has native records
                    pending.native = True
                saw_run_id = False
                continue

            if pending is not None:
                if getattr(pending, "native", False):
                    if msg.startswith("Created running flag"):
                        # native run takes over; drop any stale active run
                        if active is not None:
                            finish(active, "incomplete", "Run ended without a result in the log")
                        active = None
                        pending = None
                        counts["native_skipped"] += 1
                    elif ("already running" in msg or msg.startswith("Stop flag detected")
                          or msg.startswith("ERROR:")):
                        pending = None
                        counts["native_skipped"] += 1
                    continue
                if msg.startswith("Backup script is already running"):
                    finish(pending, "skipped", "Already running")
                    pending = None
                    continue
                if msg.startswith("Stop flag detected"):
                    finish(pending, "skipped", "Stop flag present")
                    pending = None
                    continue
                if msg.startswith("ERROR:") and (active is None or "Mount point" in msg
                                                 or "Invalid mount_point" in msg):
                    finish(pending, "failed", msg[len("ERROR: "):])
                    pending = None
                    continue
                if msg.startswith("Created running flag"):
                    if active is not None:
                        finish(active, "incomplete", "Run ended without a result in the log")
                    active = pending
                    pending = None
                    continue
                if active is None:
                    continue  # config/mount-check chatter for the pending run

            if active is None:
                continue

            # --- lines belonging to the active run ---
            sj = START_JOB_RE.match(msg)
            if sj:
                src, dst = sj.group(1), sj.group(2)
                name = os.path.basename(src.rstrip("/")) if src.startswith("/") else src
                job = new_job(active, ts, name)
                if src.startswith("/"):
                    job["source"] = src
                if dst:
                    job["dest"] = dst
                continue
            if msg.startswith("Source: ") and active.job is not None:
                active.job["source"] = msg[len("Source: "):]
                continue
            if msg.startswith("Destination: ") and active.job is not None:
                active.job["dest"] = msg[len("Destination: "):]
                continue
            if msg.startswith("Successfully completed backup: "):
                name = msg[len("Successfully completed backup: "):]
                job = next((j for j in reversed(active.jobs) if j["name"] == name), active.job)
                if job is not None:
                    job["status"] = "success"
                    job["rc"] = 0
                    job["end"] = iso(ts)
                continue
            if msg == "All backups completed successfully!":
                finish(active, "success")
                active = None
                continue
            if msg.startswith("ERROR:"):
                reason = msg[len("ERROR: "):]
                if active.job is not None and active.job["status"] == "incomplete":
                    active.job["status"] = "failed"
                    active.job["reason"] = reason
                    active.job["end"] = iso(ts)
                finish(active, "failed", reason)
                active = None
                continue

        if active is not None:
            finish(active, "incomplete", "Run ended without a result in the log")
        if pending is not None and not getattr(pending, "native", False):
            finish(pending, "incomplete", "Run ended without a result in the log")

    os.replace(tmp, args.out)
    print(json.dumps(counts))


if __name__ == "__main__":
    sys.exit(main())
