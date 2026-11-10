#!/usr/bin/env python3
"""Apply the backup schedule to root's crontab - run on the HOST by host/actions.sh.

Only the block between the BEGIN/END marker lines is ever rewritten. The command in
that block is fixed here (the portal only supplies the 5 time fields, which are
validated again). Any *active* line elsewhere that runs this backup.sh is commented
out so backups never run twice. The new crontab is installed with `crontab -`; if
anything fails, the existing crontab is left as it was.

Usage: schedule.py REQUEST_JSON LOG_FILE STATUS_JSON BACKUP_DIR [CRON_LOG]
       CRON_LOG is where scheduled runs write their output (default
       /var/log/backup_sets_cron.log; host/actions.sh passes its own CRON_LOG so portal
       "Run now" and scheduled runs log to the same file).
       (CRONTAB_CMD env var overrides the crontab command, for tests)
"""
import json
import os
import re
import subprocess
import sys
import time

BEGIN = "# BEGIN backup_sets schedule (managed by the backup portal - change it there)"
END = "# END backup_sets schedule"
MOVED = "#[moved to portal schedule] "

_FIELD = re.compile(r"^(\*|\d{1,2}(-\d{1,2})?)(/\d{1,2})?(,(\*|\d{1,2}(-\d{1,2})?)(/\d{1,2})?)*$")
_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


def validate(expr):
    """Same rules as portal/app/schedule.py validate() (a test keeps the two in step)."""
    if not isinstance(expr, str) or any(c in expr for c in "\n\r\0"):
        return False
    fields = expr.split(" ")
    if len(fields) != 5:
        return False
    for f, (lo, hi) in zip(fields, _RANGES):
        if not _FIELD.fullmatch(f):  # fullmatch: "$" alone would allow a trailing newline
            return False
        for part in f.split(","):
            base, _, step = part.partition("/")
            if step and int(step) == 0:
                return False
            if base != "*":
                a, _, b = base.partition("-")
                if not all(lo <= int(v) <= hi for v in (a, b or a)) or (b and int(b) < int(a)):
                    return False
    return True


def rewrite(text, enabled, expr, command, script):
    """Return the new crontab text."""
    lines = text.splitlines()
    out, block_at, inside = [], None, False
    for line in lines:
        s = line.strip()
        if s == BEGIN:
            if inside or block_at is not None:
                raise ValueError("the crontab has more than one portal schedule block")
            inside, block_at = True, len(out)
            continue
        if s == END:
            if not inside:
                raise ValueError("the crontab has an END marker without a BEGIN marker")
            inside = False
            continue
        if inside:
            continue
        if s and not s.startswith("#") and script in s:
            line = MOVED + line
        out.append(line)
    if inside:
        raise ValueError("the crontab has a BEGIN marker without an END marker")
    block = [BEGIN, ("" if enabled else "#") + f"{expr} {command}", END]
    if block_at is None:
        if out and out[-1].strip():
            out.append("")
        out.extend(block)
    else:
        out[block_at:block_at] = block
    return "\n".join(out) + "\n"


def main(request_json, log_file, status_path, backup_dir, cron_log="/var/log/backup_sets_cron.log"):
    crontab = os.environ.get("CRONTAB_CMD", "crontab")
    req = {}

    def finish(state, message):
        st = {"status": state, "message": message, "user": req.get("user"), "id": req.get("id"),
              "enabled": req.get("enabled"), "expr": req.get("expr"), "updated": time.time()}
        tmp = status_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, status_path)
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Portal action: schedule {message}"
        print(line)
        try:
            with open(log_file, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
        return 0 if state == "applied" else 1

    try:
        req = json.loads(request_json)
        if not isinstance(req, dict):
            raise ValueError
    except ValueError:
        req = {}
        return finish("failed", "refused: unreadable request")
    user = req.get("user") if re.match(r"^[A-Za-z0-9._-]{1,64}$", str(req.get("user"))) else "unknown"
    req["user"] = user
    expr, enabled = req.get("expr"), req.get("enabled")
    if not isinstance(enabled, bool):
        return finish("failed", f"refused (requested by {user}): 'enabled' must be true or false")
    if not validate(expr):
        return finish("failed", f"refused (requested by {user}): invalid schedule {expr!r}")

    script = os.path.join(backup_dir, "backup.sh")
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", cron_log or ""):
        return finish("failed", f"refused: unsafe cron log path {cron_log!r}")
    command = f"bash {script} >> {cron_log} 2>&1"

    cur = subprocess.run([crontab, "-l"], capture_output=True, text=True)
    if cur.returncode != 0:
        if "no crontab" in (cur.stderr or "").lower():
            current = ""
        else:
            return finish("failed", f"refused: could not read the crontab: {cur.stderr.strip()}")
    else:
        current = cur.stdout
    try:
        new = rewrite(current, enabled, expr, command, script)
    except ValueError as e:
        return finish("failed", f"refused (requested by {user}): {e} - fix it with crontab -e")
    if new == current:
        return finish("applied", f"unchanged ({'on' if enabled else 'off'}, {expr}) - requested by {user}")
    inst = subprocess.run([crontab, "-"], input=new, capture_output=True, text=True)
    if inst.returncode != 0:
        return finish("failed", f"refused: crontab rejected the change: {inst.stderr.strip()}")
    check = subprocess.run([crontab, "-l"], capture_output=True, text=True)
    want = [BEGIN, ("" if enabled else "#") + f"{expr} {command}", END]
    got = [l.strip() for l in check.stdout.splitlines()] if check.returncode == 0 else []
    if not any(got[i:i + 3] == want for i in range(len(got))):
        return finish("failed", "the crontab did not read back as written - check it with crontab -l")
    moved = sum(1 for l in new.splitlines() if l.startswith(MOVED)) - \
        sum(1 for l in current.splitlines() if l.startswith(MOVED))
    note = f"; commented out {moved} old backup line(s)" if moved > 0 else ""
    state = f'set to "{expr}"' if enabled else f'turned off (kept "{expr}")'
    return finish("applied", f"{state} by {user}{note}")


if __name__ == "__main__":
    if len(sys.argv) not in (5, 6):
        print(__doc__)
        sys.exit(2)
    sys.exit(main(*sys.argv[1:]))
