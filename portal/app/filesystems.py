"""Filesystem fullness, read from the host's mount table via /host."""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

from .history import connect
from .settings import FS_SNAPSHOT_INTERVAL, hostpath

REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "vfat", "exfat", "ntfs",
           "ntfs3", "fuseblk", "fuse.mergerfs", "nfs", "nfs4", "cifs", "smb3"}
SKIP_PREFIXES = ("/proc", "/sys", "/run", "/snap", "/var/lib/docker", "/dev")
# statvfs on a hung network/FUSE mount blocks in the kernel and can't be cancelled, so:
#  - results are cached briefly (one measurement serves every caller in a request),
#  - a mount whose previous statvfs is still stuck is reported "not responding" without
#    tying up another thread, and the pool is large enough for several hung mounts.
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="statvfs")
CACHE_SECONDS = 30
STATVFS_TIMEOUT = 3
_inflight = {}  # mount -> Future of a statvfs that may still be stuck
_cache = {"ts": 0.0, "data": None}
_cache_lock = threading.Lock()


def _unescape(s):
    return s.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def mounts():
    """[(device, mountpoint, fstype)] for real filesystems on the host."""
    seen = {}
    with open(hostpath("/proc/1/mounts")) as f:
        for line in f:
            parts = line.split()
            if len(parts) < 3:
                continue
            dev, mp, fstype = _unescape(parts[0]), _unescape(parts[1]), parts[2]
            if fstype not in REAL_FS or mp.startswith(SKIP_PREFIXES):
                continue
            seen[mp] = (dev, mp, fstype)
    return list(seen.values())


def _statvfs(mp):
    st = os.statvfs(hostpath(mp))
    size = st.f_blocks * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    pct = (used / (used + avail) * 100) if (used + avail) else 0.0
    ipct = ((st.f_files - st.f_ffree) / st.f_files * 100) if st.f_files else None
    return {"size": size, "used": used, "avail": avail, "pct": round(pct, 1),
            "inodes_pct": round(ipct, 1) if ipct is not None else None}


def usage(max_age=CACHE_SECONDS):
    """Size/used/free of every real filesystem. Callers get their own copies."""
    with _cache_lock:
        if _cache["data"] is None or time.time() - _cache["ts"] >= max_age:
            _cache["data"] = _measure()
            _cache["ts"] = time.time()
        return [dict(e) for e in _cache["data"]]


def _measure():
    started = {}
    out = []
    for dev, mp, fstype in mounts():
        entry = {"device": dev, "mount": mp, "fstype": fstype}
        prev = _inflight.get(mp)
        if prev is not None and not prev.done():
            entry.update(ok=False, error="not responding")  # still stuck from last time
        else:
            started[mp] = _inflight[mp] = _pool.submit(_statvfs, mp)
        out.append(entry)
    deadline = time.time() + STATVFS_TIMEOUT  # one deadline for all mounts, not 3 s each
    for entry in out:
        fut = started.get(entry["mount"])
        if fut is None:
            continue
        try:
            entry.update(fut.result(timeout=max(0.0, deadline - time.time())))
            entry["ok"] = True
        except FutTimeout:
            entry.update(ok=False, error="not responding")
        except OSError as e:
            entry.update(ok=False, error=str(e))
    out.sort(key=lambda e: e["mount"])
    return out


def reset_cache():
    with _cache_lock:
        _cache["data"] = None


def mount_for(path, fs_list):
    """Longest mountpoint that contains path."""
    best = None
    for e in fs_list:
        mp = e["mount"]
        if path == mp or path.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) > len(best["mount"]):
                best = e
    return best


def level(pct):
    if pct is None:
        return "unknown"
    if pct >= 95:
        return "critical"
    if pct >= 85:
        return "warning"
    return "good"


# ------------------------------------------------------------- snapshots
SNAPSHOT_RETENTION_DAYS = 730


def snapshot():
    ts = time.time()
    rows = [(ts, e["mount"], e["size"], e["used"], e["avail"]) for e in usage(max_age=0) if e.get("ok")]
    with connect() as conn:
        conn.executemany("INSERT INTO fs_snapshots(ts, mount, size, used, avail) VALUES(?,?,?,?,?)", rows)
        conn.execute("DELETE FROM fs_snapshots WHERE ts < ?", (ts - SNAPSHOT_RETENTION_DAYS * 86400,))


def _snapshot_loop():
    while True:
        try:
            with connect() as conn:
                last = conn.execute("SELECT MAX(ts) FROM fs_snapshots").fetchone()[0] or 0
            wait = FS_SNAPSHOT_INTERVAL - (time.time() - last)
            if wait <= 0:
                snapshot()
                wait = FS_SNAPSHOT_INTERVAL
        except Exception as e:  # keep the thread alive
            print(f"fs snapshot error: {e}", flush=True)
            wait = 300
        time.sleep(max(wait, 30))


def start_snapshot_thread():
    threading.Thread(target=_snapshot_loop, name="fs-snapshots", daemon=True).start()


def trend(days=30):
    """{mount: [[ts, used, size], ...]}, downsampled to one point/day beyond 14 days."""
    cutoff = time.time() - days * 86400
    with connect() as conn:
        rows = conn.execute("SELECT ts, mount, size, used FROM fs_snapshots WHERE ts >= ? ORDER BY ts",
                            (cutoff,)).fetchall()
    out = {}
    for r in rows:
        series = out.setdefault(r["mount"], [])
        if days > 14 and series and int(series[-1][0] // 86400) == int(r["ts"] // 86400):
            series[-1] = [r["ts"], r["used"], r["size"]]
        else:
            series.append([r["ts"], r["used"], r["size"]])
    return out


def snapshot_growth_rate(mount, days=30):
    """Least-squares slope of used bytes (bytes/day) from snapshots, if >= 2 days of data."""
    cutoff = time.time() - days * 86400
    with connect() as conn:
        rows = conn.execute("SELECT ts, used FROM fs_snapshots WHERE mount=? AND ts >= ? ORDER BY ts",
                            (mount, cutoff)).fetchall()
    if len(rows) < 3 or rows[-1]["ts"] - rows[0]["ts"] < 2 * 86400:
        return None
    xs = [r["ts"] / 86400 for r in rows]
    ys = [r["used"] for r in rows]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if not den:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
