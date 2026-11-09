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
