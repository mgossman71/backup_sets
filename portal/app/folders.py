"""Top-level folder sizes for a backup entry (source vs. its copy on the backup disk)."""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .history import connect
from .settings import hostpath

CACHE_TTL = 6 * 3600
SCAN_BUDGET = 45          # seconds per request; unfinished folders are sized on the next call
MAX_WALKS_PER_REQUEST = 5000  # bounds work per request (Movies needs ~2,500: 1,240 folders x 2 sides)
SQL_CHUNK = 500
_pool = ThreadPoolExecutor(max_workers=8)
_schema_lock = threading.Lock()
_schema_done = False


def _ensure_schema():
    global _schema_done
    with _schema_lock:
        if not _schema_done:
            with connect() as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS folder_sizes "
                             "(path TEXT PRIMARY KEY, size INTEGER, files INTEGER, ts REAL)")
            _schema_done = True


def _walk_size(container_path, deadline):
    """Apparent size (sum of file sizes, like rsync's 'Total file size') and file count."""
    size = files = 0
    stack = [container_path]
    while stack:
        if time.time() > deadline:
            raise TimeoutError
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            size += e.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return size, files


def _list_top(host_dir):
    """(dir names, loose file count, loose file bytes) directly under a host path."""
    dirs, loose_n, loose_b = [], 0, 0
    try:
        with os.scandir(hostpath(host_dir)) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        dirs.append(e.name)
                    elif e.is_file(follow_symlinks=False):
                        loose_n += 1
                        loose_b += e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except FileNotFoundError:
        return None, 0, 0
    return dirs, loose_n, loose_b


def invalidate(host_path):
    _ensure_schema()
    with connect() as conn:
        conn.execute("DELETE FROM folder_sizes WHERE path = ?", (host_path,))


def scan(source, destination, refresh=False):
    _ensure_schema()
    dest_base = os.path.join(destination, os.path.basename(source.rstrip("/")))
    src_dirs, loose_n, loose_b = _list_top(source)
    if src_dirs is None:
        raise FileNotFoundError(source)
    dest_dirs, _, _ = _list_top(dest_base)
    dest_dirs = dest_dirs or []

    paths = [os.path.join(source, n) for n in src_dirs] + [os.path.join(dest_base, n) for n in dest_dirs]
    now = time.time()
    cached = {}
    with connect() as conn:
        for i in range(0, len(paths), SQL_CHUNK):
            chunk = paths[i:i + SQL_CHUNK]
            for r in conn.execute("SELECT path, size, files, ts FROM folder_sizes WHERE path IN (%s)"
                                  % ",".join("?" * len(chunk)), chunk):
                cached[r["path"]] = r
    sizes, todo = {}, []
    for p in paths:
        r = cached.get(p)
        if r and not refresh and now - r["ts"] < CACHE_TTL:
            sizes[p] = (r["size"], r["files"], r["ts"])
        else:
            todo.append(p)

    deadline = time.time() + SCAN_BUDGET
    futures = {p: _pool.submit(_walk_size, hostpath(p), deadline) for p in todo[:MAX_WALKS_PER_REQUEST]}
    fresh = []
    for p, fut in futures.items():
        try:
            size, files = fut.result(timeout=max(0.1, deadline - time.time() + 1))
            sizes[p] = (size, files, time.time())
            fresh.append((p, size, files, sizes[p][2]))
        except Exception:
            fut.cancel()
    if fresh:
        with connect() as conn:
            conn.executemany("INSERT OR REPLACE INTO folder_sizes(path, size, files, ts) VALUES(?,?,?,?)", fresh)

    folders = []
    src_set, dest_set = set(src_dirs), set(dest_dirs)
    for name in sorted(src_set | dest_set, key=str.lower):
        sp, dp = os.path.join(source, name), os.path.join(dest_base, name)
        s = sizes.get(sp) if name in src_set else None
        d = sizes.get(dp) if name in dest_set else None
        folders.append({
            "name": name,
            "in_source": name in src_set,
            "src_size": s[0] if s else None,
            "src_files": s[1] if s else None,
            "on_dest": name in dest_set,
            "dest_size": d[0] if d else None,
        })
    oldest = min((v[2] for v in sizes.values()), default=now)
    return {
        "source": source, "destination": destination, "dest_folder": dest_base,
        "folders": folders, "loose": {"count": loose_n, "size": loose_b},
        "partial": any(f["in_source"] and f["src_size"] is None or f["on_dest"] and f["dest_size"] is None
                       for f in folders),
        "sizes_as_of": oldest,
    }
