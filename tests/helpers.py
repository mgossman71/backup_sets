"""Shared helpers for the shell-script tests (backup.sh, host/actions.sh, backfill tool).

Every test gets its own temporary directory with its own source/destination trees,
config file, flag files, log and history file - nothing outside tmp_path is touched.
"""
import json
import os
import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parent.parent
BACKUP_SH = REPO / "backup.sh"
ACTIONS_SH = REPO / "host" / "actions.sh"
BACKFILL = REPO / "tools" / "backfill_history.py"


def write_tree(root, spec):
    """spec: {"relative/path": "content" | b"bytes" | None (empty dir)}"""
    root = pathlib.Path(root)
    for rel, content in spec.items():
        p = root / rel
        if content is None:
            p.mkdir(parents=True, exist_ok=True)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content)


def tree(root):
    """{relative path: content} of every file under root (dirs end with '/')."""
    root = pathlib.Path(root)
    out = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out[rel] = "-> " + os.readlink(p)
        elif p.is_dir():
            out[rel + "/"] = None
        else:
            out[rel] = p.read_bytes()
    return out


class BackupEnv:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.src = self.root / "src"
        self.dst = self.root / "dst"
        self.src.mkdir()
        self.dst.mkdir()
        self.config_path = self.root / "config.yaml"
        self.log = self.root / "backup.log"
        self.history_path = self.root / "state" / "history.jsonl"
        self.running_flag = self.root / ".running"
        self.fail_flag = self.root / ".failed"
        self.stop_flag = self.root / ".stop"
        self.backups = []
        self.mounts = []
        self.rsync_opts = "-a --stats"
        self.extra = {}

    # ---- config
    def add(self, source, destination=None, **selection):
        entry = {"source": str(source), "destination": str(destination or self.dst)}
        entry.update(selection)
        self.backups.append(entry)
        return entry

    def write_config(self):
        cfg = {
            "log_file": str(self.log),
            "rsync_opts": self.rsync_opts,
            "running_flag": str(self.running_flag),
            "fail_flag": str(self.fail_flag),
            "stop_flag": str(self.stop_flag),
            "history_file": str(self.history_path),
            "required_mount_points": [str(m) for m in self.mounts],
            "backups": self.backups,
        }
        cfg.update(self.extra)
        # JSON is valid YAML, and avoids any quoting ambiguity in test data
        self.config_path.write_text(json.dumps(cfg, indent=2))

    # ---- running
    def env(self, **extra):
        e = dict(os.environ)
        e["BACKUP_CONFIG"] = str(self.config_path)
        e.pop("BACKUP_TRIGGER", None)
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def run(self, write=True, **env):
        if write:
            self.write_config()
        return subprocess.run(["bash", str(BACKUP_SH)], env=self.env(**env), capture_output=True,
                              text=True, timeout=120, stdin=subprocess.DEVNULL)

    def popen(self, write=True, **env):
        if write:
            self.write_config()
        return subprocess.Popen(["bash", str(BACKUP_SH)], env=self.env(**env), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, stdin=subprocess.DEVNULL,
                                start_new_session=True)

    # ---- results
    def records(self):
        if not self.history_path.exists():
            return []
        return [json.loads(line) for line in self.history_path.read_text().splitlines() if line.strip()]

    def runs(self):
        return [r for r in self.records() if r["type"] == "run"]

    def jobs(self):
        return [r for r in self.records() if r["type"] == "job"]

    def last_run(self):
        runs = self.runs()
        assert runs, "no run record written"
        return runs[-1]

    def log_text(self):
        return self.log.read_text() if self.log.exists() else ""


