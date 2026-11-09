"""Live state: flag files, schedule, log tail, and portal action requests."""
import json
import os
import re
import time
import uuid
from datetime import datetime

from croniter import croniter

from .settings import BROWSE_ROOTS, CRONTAB_FILE, REQUEST_DIR, STATE_DIR, hostpath

ACTIONS = {"run", "stop_on", "stop_off", "clear_fail"}
REMOVE_RE = re.compile(r"^remove-([0-9a-f]{32})$")
REMOVAL_DIR = os.path.join(STATE_DIR, "removals")
PROGRESS_RE = re.compile(r"^\s*[\d,]+\s+\d{1,3}%\s")
STATS_RE = re.compile(r"^(Number of |Total |Literal data|Matched data|File list |sent |total size )")
TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (.*)$")


def _flag(path):
    if not path:
        return None
    try:
        st = os.stat(hostpath(path))
        return {"path": path, "since": st.st_mtime}
    except (FileNotFoundError, ValueError):
        return None


def flags(cfg):
    return {
        "running": _flag(cfg.get("running_flag")),
        "failed": _flag(cfg.get("fail_flag")),
        "stopped": _flag(cfg.get("stop_flag")),
    }


def schedules():
    """Cron entries in root's crontab that run backup.sh, with their next run time."""
    out = []
    try:
        with open(hostpath(CRONTAB_FILE)) as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "backup.sh" not in s:
            continue
        parts = s.split(None, 5)
        if s.startswith("@"):
            expr, cmd = parts[0], " ".join(parts[1:])
        elif len(parts) >= 6:
            expr, cmd = " ".join(parts[:5]), parts[5]
        else:
            continue
        try:
            nxt = croniter(expr, datetime.now().astimezone()).get_next(datetime)
            out.append({"expr": expr, "command": cmd, "next": nxt.timestamp()})
        except (ValueError, KeyError):
            out.append({"expr": expr, "command": cmd, "next": None})
    return out


def log_tail(log_file, lines=200, max_bytes=1_000_000):
    """Last N meaningful log lines (rsync per-file progress counters stripped)."""
    if not log_file:
        return []
    try:
        path = hostpath(log_file)
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return []
    out = []
    for raw in data.splitlines()[1:] if size > max_bytes else data.splitlines():
        line = raw.split("\r")[-1].rstrip()
        if not line or PROGRESS_RE.match(line):
            continue
        out.append(line[:500])
    return out[-lines:]


def current_activity(tail):
    """From the log tail: the run in progress, its current job, jobs finished."""
    start_idx = None
    for i in range(len(tail) - 1, -1, -1):
        if tail[i].endswith("] Backup script started"):
            start_idx = i
            break
    if start_idx is None:
        return {}
    current, done, files = None, [], 0
    last_file = None
    for line in tail[start_idx:]:
        m = TS_RE.match(line)
        if m:
            msg = m.group(2)
            if msg.startswith("Starting backup: "):
                current, files = msg[len("Starting backup: "):], 0
            elif msg.startswith("Successfully completed backup: "):
                done.append(msg[len("Successfully completed backup: "):])
                current = None
        elif current and not STATS_RE.match(line) and line != "sending incremental file list":
            last_file = line
            files += 1
    return {"current_job": current, "completed_jobs": done, "last_file": last_file}


def request_action(name, user, payload=None):
    """Drop a request file for the host bridge (atomic: written elsewhere, then renamed in)."""
    if name not in ACTIONS and not REMOVE_RE.match(name):
        raise ValueError("unknown action")
    os.makedirs(REQUEST_DIR, exist_ok=True)
    tmp = os.path.join(STATE_DIR, f".req-{name}-{os.getpid()}-{time.time_ns()}")
    with open(tmp, "w") as f:
        if payload is not None:
            json.dump(dict(payload, user=user), f)
        else:
            f.write(f"user={user}\n")
    os.replace(tmp, os.path.join(REQUEST_DIR, name))


def request_removal(source, destination, folder, user):
    rid = uuid.uuid4().hex
    request_action(f"remove-{rid}", user, {"source": source, "destination": destination, "folder": folder})
    return rid


def removals(limit=20):
    """Recent destination-folder removals: queued (request not yet picked up) + host status files."""
    out = []
    for name in pending_actions():
        m = REMOVE_RE.match(name)
        if not m:
            continue
        try:
            with open(os.path.join(REQUEST_DIR, name)) as f:
                req = json.load(f)
        except (OSError, ValueError):
            req = {}
        out.append({"id": m.group(1), "status": "queued", "message": "Waiting for the server",
                    "folder": req.get("folder"), "source": req.get("source"),
                    "destination": req.get("destination"), "updated": time.time()})
    try:
        names = os.listdir(REMOVAL_DIR)
    except FileNotFoundError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(REMOVAL_DIR, name)) as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda r: r.get("updated") or 0, reverse=True)
    return out[:limit]


def pending_actions():
    try:
        return sorted(os.listdir(REQUEST_DIR))
    except FileNotFoundError:
        return []


def browse(path):
    """List subdirectories of a host path, restricted to BROWSE_ROOTS."""
    path = os.path.normpath(path or BROWSE_ROOTS[0])
    if not any(path == r or path.startswith(r.rstrip("/") + "/") for r in BROWSE_ROOTS):
        raise PermissionError(f"browsing is limited to {', '.join(BROWSE_ROOTS)}")
    entries = []
    with os.scandir(hostpath(path)) as it:
        for e in it:
            try:
                if e.is_dir(follow_symlinks=False):
                    entries.append(e.name)
            except OSError:
                continue
            if len(entries) >= 5000:
                break
    entries.sort(key=str.lower)
    parent = os.path.dirname(path)
    can_up = any(parent == r or parent.startswith(r.rstrip("/") + "/") for r in BROWSE_ROOTS)
    return {"path": path, "parent": parent if can_up and path not in BROWSE_ROOTS else None,
            "dirs": entries, "roots": BROWSE_ROOTS}
