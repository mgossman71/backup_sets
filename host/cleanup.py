#!/usr/bin/env python3
"""Backup Folder Cleanup - executed on the HOST (as root) by host/actions.sh.

Deletes from a backup folder the items listed in a cleanup plan made by the portal,
after re-validating every one of them against the *saved* config.yaml and the disks
as they are right now. The plan can only ever narrow what is deleted:

  * the plan must be < 1 hour old and name a backup entry that exists in config.yaml
  * no backup may be running; the source folder must exist and not be empty
  * the backup folder must be a real directory (not a symlink)
  * each item must be a clean relative path (no '..', not absolute)
  * no component of its parent path may be a symlink or a non-directory
  * the item must still exist in the backup and must NOT exist in the source
  * its top-level folder must not be protected (deselected but still in the source)

Anything failing a check is skipped and reported; it is never deleted.

Usage: cleanup.py PLAN_JSON STATUS_JSON CONFIG_YAML LOG_FILE USER
"""
import json
import os
import shutil
import stat
import sys
import time

import yaml

PLAN_MAX_AGE = 3600


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Run:
    def __init__(self, plan_path, status_path, config_path, log_file, user):
        self.plan_path, self.status_path = plan_path, status_path
        self.config_path, self.log_file, self.user = config_path, log_file, user
        self.plan = {}
        self.deleted, self.skipped, self.freed = [], [], 0

    # ------------------------------------------------------------ output
    def log(self, msg):
        line = f"[{now_str()}] Portal action: cleanup {msg}"
        print(line)
        if self.log_file:
            try:
                with open(self.log_file, "a") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def write_status(self, state, message):
        st = {"id": self.plan.get("id"), "status": state, "message": message, "user": self.user,
              "source": self.plan.get("source"), "destination": self.plan.get("destination"),
              "deleted": len(self.deleted), "skipped": len(self.skipped), "freed": self.freed,
              "skipped_items": self.skipped[:200], "updated": time.time()}
        tmp = self.status_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, self.status_path)

    def fail(self, message):
        self.log(f"refused (requested by {self.user}): {message}")
        self.write_status("failed", message)
        return 1

    # ------------------------------------------------------------ checks
    def main(self):
        try:
            with open(self.plan_path) as f:
                self.plan = json.load(f)
        except (OSError, ValueError):
            return self.fail("cleanup plan not found or unreadable")
        p = self.plan
        if time.time() - float(p.get("created", 0)) > PLAN_MAX_AGE:
            return self.fail("the comparison is more than an hour old - run it again")

        try:
            with open(self.config_path) as f:
                cfg = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            return self.fail(f"cannot read config.yaml: {e}")
        running = cfg.get("running_flag")
        if running and os.path.exists(running):
            return self.fail("a backup is running - try again when it finishes")

        source, destination = p.get("source"), p.get("destination")
        entry = next((b for b in cfg.get("backups") or []
                      if b.get("source") == source and b.get("destination") == destination), None)
        if not entry or not source or not destination:
            return self.fail("no backup entry with this source and destination in config.yaml")
        if not os.path.isdir(source) or os.path.islink(source):
            return self.fail(f"source folder {source} not found - is its disk mounted?")
        if not os.listdir(source):
            return self.fail(f"source folder {source} is empty - is its disk mounted?")
        base = os.path.join(destination, os.path.basename(source.rstrip("/")))
        if os.path.islink(base) or not os.path.isdir(base):
            return self.fail(f"{base} is not a real backup folder")

        src_top = {e.name for e in os.scandir(source) if e.is_dir(follow_symlinks=False)}
        include = [str(x) for x in entry.get("include") or []]
        exclude = [str(x) for x in entry.get("exclude") or []]
        if include and exclude:
            return self.fail("backup entry has both include and exclude lists")
        if exclude:
            protected = {n for n in exclude if n in src_top}
        elif include:
            protected = {n for n in src_top if n not in include}
        else:
            protected = set()

        items = p.get("items") or []
        self.log(f"of {base} started by {self.user}: {len(items)} item(s) to check")
        self.write_status("running", f"Checking and deleting {len(items)} item(s)…")
        last = time.time()
        for item in items:
            reason = self.remove(item, source, base, protected)
            if reason:
                self.skipped.append({"path": item.get("path"), "reason": reason})
            if time.time() - last > 2:
                self.write_status("running", f"Deleted {len(self.deleted)} of {len(items)}…")
                last = time.time()

        audit = self.status_path.replace(".status.json", ".deleted.txt")
        with open(audit, "w") as f:
            f.write("".join(f"{x}\n" for x in self.deleted))
        msg = f"Deleted {len(self.deleted)} item(s), freed {self.freed} bytes"
        if self.skipped:
            msg += f"; skipped {len(self.skipped)}"
        self.log(f"of {base} finished: {msg} (list: {audit})")
        self.write_status("done", msg)
        return 0

    def remove(self, item, source, base, protected):
        """Delete one plan item after checking it. Returns None, or the reason it was skipped."""
        rel = item.get("path") if isinstance(item, dict) else None
        if not isinstance(rel, str) or not rel or "\0" in rel or rel.startswith("/"):
            return "invalid path"
        parts = rel.split("/")
        if any(x in ("", ".", "..") for x in parts):
            return "invalid path"
        if parts[0] in protected:
            return "protected (not selected for backup)"
        # every parent must be a real directory inside the backup folder
        cur = base
        for comp in parts[:-1]:
            cur = os.path.join(cur, comp)
            try:
                if not stat.S_ISDIR(os.lstat(cur).st_mode):
                    return "a parent is not a real folder"
            except FileNotFoundError:
                return "already gone"
        target = os.path.join(base, rel)
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            return "already gone"
        if os.path.lexists(os.path.join(source, rel)):
            return "exists in the source again"
        try:
            if stat.S_ISDIR(st.st_mode):
                size = item.get("size") or 0
                shutil.rmtree(target)  # does not follow symlinks
            else:
                size = st.st_size
                os.unlink(target)
        except OSError as e:
            return f"delete failed: {e.strerror}"
        self.deleted.append(rel)
        self.freed += int(size)
        return None


if __name__ == "__main__":
    if len(sys.argv) != 6:
        print(__doc__)
        sys.exit(2)
    sys.exit(Run(*sys.argv[1:]).main())
