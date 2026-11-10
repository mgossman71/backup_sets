"""Runtime settings (environment driven) and host-path helpers."""
import os

HOST_ROOT = os.environ.get("HOST_ROOT", "/host").rstrip("/")
CONFIG_FILE = os.environ.get("CONFIG_FILE", "/opt/backup_sets/config.yaml")
STATE_DIR = os.environ.get("STATE_DIR", "/var/lib/backup_sets")
ALLOWED_GROUP = os.environ.get("ALLOWED_GROUP", "sudo")
ALLOWED_USERS = [u for u in os.environ.get("ALLOWED_USERS", "root").split(",") if u]
BROWSE_ROOTS = [r.rstrip("/") or "/" for r in os.environ.get("BROWSE_ROOTS", "/mnt").split(",") if r]
CRONTAB_FILE = os.environ.get("CRONTAB_FILE", "/var/spool/cron/crontabs/root")  # host path
FS_SNAPSHOT_INTERVAL = int(os.environ.get("FS_SNAPSHOT_INTERVAL", "3600"))
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "12"))

REQUEST_DIR = os.path.join(STATE_DIR, "requests")
DB_PATH = os.path.join(STATE_DIR, "portal.db")
HISTORY_FILES = [
    os.path.join(STATE_DIR, "history_backfill.jsonl"),
    os.path.join(STATE_DIR, "history.jsonl"),
]


def hostpath(path):
    """Translate an absolute host path into the container's view of it."""
    if not path or not path.startswith("/"):
        raise ValueError(f"not an absolute path: {path!r}")
    return HOST_ROOT + os.path.normpath(path)


def host_timezone():
    """The host's IANA time zone (e.g. "America/Chicago"), which is what cron schedules use.

    Read from the /etc/localtime symlink (what timedatectl sets), then /etc/timezone,
    then the TZ variable; falls back to UTC.
    """
    try:
        target = os.readlink(hostpath("/etc/localtime"))
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except (OSError, ValueError):
        pass
    try:
        with open(hostpath("/etc/timezone")) as f:
            name = f.read().strip()
            if name:
                return name
    except (OSError, ValueError):
        pass
    return os.environ.get("TZ") or "UTC"


def host_now():
    """Current time as an aware datetime in the host's time zone."""
    from datetime import datetime
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return datetime.now(ZoneInfo(host_timezone()))
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now().astimezone()

