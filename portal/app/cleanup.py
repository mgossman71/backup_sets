"""Backup Folder Cleanup: find what is in a backup folder but no longer in its source.

The portal only *plans* (read-only, through /host). The plan is saved to the state dir;
deletion is done by the host bridge (host/cleanup.py), which re-validates every path.

Rules (the host applies the same ones):
  * a path in <destination>/<source name>/ is extraneous when the same relative path
    does not exist in the source; only the topmost extraneous path is listed
    (a deleted show is one item, not every episode in it)
  * folders that are deselected for backup but still exist in the source are protected
    and never looked at
  * symlinks are never followed; an extraneous symlink is itself the item
  * an empty or missing source is refused - that usually means a disk is not mounted
"""
import json
import os
import stat
import time
import uuid

from .settings import STATE_DIR, hostpath

PLAN_DIR = os.path.join(STATE_DIR, "cleanup")
PLAN_MAX_AGE = 3600
MAX_ITEMS_RETURNED = 2000


class CleanupError(Exception):
    pass


def protected_folders(entry, source_top):
    """Top-level folder names deselected for backup that still exist in the source."""
    names = set(source_top)
    if entry["mode"] == "exclude":
        return sorted(n for n in entry["folders"] if n in names)
    if entry["mode"] == "include":
        return sorted(n for n in names if n not in entry["folders"])
    return []


def _size_of(path):
    """(bytes, files) under a host path, not following symlinks."""
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size, 1
    size = files = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            size += e.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return size, files


def _top_dirs(path):
    with os.scandir(path) as it:
        return [e.name for e in it if e.is_dir(follow_symlinks=False)]


PLAN_RETENTION_DAYS = 30


def prune_old_plans(now=None):
    """Delete cleanup plans, statuses and deleted-path lists older than PLAN_RETENTION_DAYS."""
    cutoff = (now or time.time()) - PLAN_RETENTION_DAYS * 86400
    try:
        names = os.listdir(PLAN_DIR)
    except FileNotFoundError:
        return 0
    removed = 0
    for n in names:
        path = os.path.join(PLAN_DIR, n)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.unlink(path)
                removed += 1
        except OSError:
            continue
    return removed


def plan(entry, deadline_s=60):
    """Compare a saved backup entry's folder with its source. Returns the plan dict."""
    source, destination = entry["source"], entry["destination"]
    src_h = hostpath(source)
    dest_folder = os.path.join(destination, os.path.basename(source.rstrip("/")))
    dst_h = hostpath(dest_folder)
    if not os.path.isdir(src_h):
        raise CleanupError(f"The source folder {source} was not found - is its disk mounted?")
    if not os.listdir(src_h):
        raise CleanupError(f"The source folder {source} is empty. That usually means its disk is not "
                           f"mounted, so cleanup is refused (it would delete the entire backup).")
    if os.path.islink(dst_h):
        raise CleanupError(f"{dest_folder} is a symbolic link - cleanup only works on a real backup folder.")
    if not os.path.isdir(dst_h):
        raise CleanupError(f"There is no backup folder at {dest_folder} yet.")

    protected = set(protected_folders(entry, _top_dirs(src_h)))
    deadline = time.time() + deadline_s
    items, kept_files, kept_bytes = [], 0, 0

    stack = [""]
    while stack:
        if time.time() > deadline:
            raise CleanupError("Comparing took too long - try again")
        rel_dir = stack.pop()
        with os.scandir(os.path.join(dst_h, rel_dir)) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            rel = os.path.join(rel_dir, e.name) if rel_dir else e.name
            if not rel_dir and e.name in protected:
                continue
            src_path = os.path.join(src_h, rel)
            if not os.path.lexists(src_path):
                size, files = _size_of(e.path)
                kind = "link" if e.is_symlink() else "folder" if e.is_dir(follow_symlinks=False) else "file"
                items.append({"path": rel, "type": kind, "size": size, "files": files})
            elif e.is_dir(follow_symlinks=False) and os.path.isdir(src_path) and not os.path.islink(src_path):
                stack.append(rel)
            else:
                st = e.stat(follow_symlinks=False)
                kept_files += 1
                kept_bytes += st.st_size
    items.sort(key=lambda i: i["path"])
    total = sum(i["size"] for i in items)
    backup_size, _ = _size_of(dst_h)
    p = {
        "id": uuid.uuid4().hex,
        "created": time.time(),
        "source": source,
        "destination": destination,
        "dest_folder": dest_folder,
        "confirm_name": os.path.basename(source.rstrip("/")),
        "protected": sorted(protected),
        "items": items,
        "total_items": len(items),
        "total_size": total,
        "total_files": sum(i["files"] for i in items),
        "backup_size": backup_size,
        "share_of_backup": (total / backup_size) if backup_size else 0,
    }
    os.makedirs(PLAN_DIR, exist_ok=True)
    prune_old_plans()
    tmp = os.path.join(PLAN_DIR, f".{p['id']}.tmp")
    with open(tmp, "w") as f:
        json.dump(p, f)
    os.replace(tmp, os.path.join(PLAN_DIR, f"{p['id']}.json"))
    return p


def summary(p, max_items=MAX_ITEMS_RETURNED):
    out = {k: v for k, v in p.items() if k != "items"}
    out["items"] = p["items"][:max_items]
    out["items_truncated"] = len(p["items"]) > max_items
    return out


def load_plan(plan_id):
    if not (len(plan_id) == 32 and all(c in "0123456789abcdef" for c in plan_id)):
        raise CleanupError("Unknown cleanup plan")
    try:
        with open(os.path.join(PLAN_DIR, f"{plan_id}.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        raise CleanupError("Unknown cleanup plan - run the comparison again")


def status(plan_id):
    try:
        with open(os.path.join(PLAN_DIR, f"{plan_id}.status.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
